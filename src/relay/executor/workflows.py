"""The durable task workflow. Imported only by the executor worker (never the Delegator).

``run_task`` is one generic DBOS workflow for every task kind. Each database write is a
DBOS step that is idempotent under replay: DBOS records a step's output only after it
returns, so a crash between our COMMIT and that record re-runs the step, and every step
guards on the task's current status inside a row-locked transaction.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Callable
from typing import ParamSpec, TypeVar, cast

from dbos import DBOS
from sqlalchemy import Engine, create_engine, func, insert, select, update
from sqlalchemy.exc import InterfaceError, OperationalError

from relay.config import get_settings, to_sync_url
from relay.executor.common import WORKFLOW_NAME, one_sentence
from relay.executor.runners import ArtifactSpec, TaskContext, get_runner
from relay.store.models import Artifact, Commitment, Idea, PendingReport, Task

_engine: Engine | None = None
P = ParamSpec("P")
R = TypeVar("R")


def configure_engine(database_url: str) -> Engine:
    """Point the workflow steps at ``database_url`` (any SQLAlchemy Postgres URL form)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(to_sync_url(database_url), pool_pre_ping=True)
    return _engine


def _transient(exc: BaseException) -> bool:
    """Connection loss, serialization failure, deadlock: worth retrying (steps are idempotent)."""
    return isinstance(exc, (OperationalError, InterfaceError))


def db_step(name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """DBOS step for DB writes: retried on transient errors, 5 attempts, 1 s doubling (~15 s)."""
    return DBOS.step(
        name=name,
        retries_allowed=True,
        max_attempts=5,
        interval_seconds=1.0,
        backoff_rate=2.0,
        should_retry=_transient,
    )


def engine() -> Engine:
    """The engine the steps use (configured by the worker, else from settings)."""
    return _engine if _engine is not None else configure_engine(get_settings().database_url)


@db_step("relay.task.start")
def start_task_step(task_id: str) -> TaskContext:
    """Mark the task running (first start wins for ``started_at``) and load its context."""
    tid = uuid.UUID(task_id)
    with engine().begin() as c:
        c.execute(
            update(Task)
            .where(Task.id == tid, Task.status.in_(("queued", "running")))
            .values(status="running", started_at=func.coalesce(Task.started_at, func.now()))
        )
        row = c.execute(
            select(
                Task.kind,
                Commitment.id,
                Commitment.idea_id,
                Commitment.goal,
                Commitment.scope_excludes,
                Commitment.artifact_kind,
            )
            .join(Commitment, Commitment.id == Task.commitment_id)
            .where(Task.id == tid)
        ).one()
    return TaskContext(
        task_id=tid,
        kind=row[0],
        commitment_id=row[1],
        idea_id=row[2],
        goal=row[3],
        scope_excludes=row[4],
        artifact_kind=row[5],
        artifacts_dir=get_settings().artifacts_dir,
    )


@DBOS.step(name="relay.task.run_runner")
def run_runner_step(ctx: TaskContext) -> ArtifactSpec:
    """Run a non-durable runner as a single checkpointed step."""
    result = get_runner(ctx.kind).fn(ctx)
    if inspect.isawaitable(result):
        # DBOS runs sync workflows in worker threads without an event loop.
        return asyncio.run(_await(result))
    return result


async def _await(aw: object) -> ArtifactSpec:
    return cast(ArtifactSpec, await aw)  # type: ignore[misc]


@db_step("relay.task.complete")
def complete_task_step(task_id: str, session_id: str | None, spec: ArtifactSpec) -> None:
    """Artifact + succeeded + idea delivered + pending report, atomically and at most once."""
    tid = uuid.UUID(task_id)
    with engine().begin() as c:
        row = c.execute(
            select(Task.status, Commitment.idea_id, Commitment.goal)
            .join(Commitment, Commitment.id == Task.commitment_id)
            .where(Task.id == tid)
            .with_for_update(of=Task)
        ).one()
        status, idea_id, goal = row
        if status == "succeeded":
            return  # step replay after a crash: already applied
        c.execute(
            insert(Artifact).values(task_id=tid, kind=spec.kind, url=spec.url, summary=spec.summary)
        )
        c.execute(
            update(Task)
            .where(Task.id == tid)
            .values(
                status="succeeded",
                finished_at=func.now(),
                error=None,
                served_model=spec.served_model,
            )
        )
        # Core UPDATE bypasses the ORM onupdate, so set updated_at explicitly.
        c.execute(
            update(Idea).where(Idea.id == idea_id).values(status="delivered", updated_at=func.now())
        )
        c.execute(
            insert(PendingReport).values(
                session_id=uuid.UUID(session_id) if session_id else None,
                idea_id=idea_id,
                task_id=tid,
                summary=one_sentence(spec.summary, f"The work on {goal!r} is finished."),
            )
        )


@db_step("relay.task.fail")
def fail_task_step(task_id: str, error: str) -> None:
    """Record failure unless the task already reached a terminal state."""
    with engine().begin() as c:
        c.execute(
            update(Task)
            .where(Task.id == uuid.UUID(task_id), Task.status.in_(("queued", "running")))
            .values(status="failed", error=error[:2000], finished_at=func.now())
        )


@DBOS.workflow(name=WORKFLOW_NAME)
def run_task(task_id: str, session_id: str | None = None) -> str:
    """Execute one task end to end; returns the final task status."""
    try:
        ctx = start_task_step(task_id)
        registered = get_runner(ctx.kind)
        spec = registered.fn(ctx) if registered.durable else run_runner_step(ctx)
        if inspect.isawaitable(spec):
            raise TypeError("durable runners must be synchronous DBOS workflows/steps")
        if not isinstance(spec, ArtifactSpec):
            raise TypeError(f"runner for {ctx.kind!r} returned {type(spec).__name__}")
    except Exception as exc:  # DBOS cancellation derives from BaseException, not caught
        fail_task_step(task_id, f"{type(exc).__name__}: {exc}")
        return "failed"
    try:
        complete_task_step(task_id, session_id, spec)
    except Exception as exc:  # retries exhausted; the step's transaction rolled back
        fail_task_step(task_id, f"could not record result: {type(exc).__name__}: {exc}")
        return "failed"
    return "succeeded"
