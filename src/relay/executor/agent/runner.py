"""The durable executor runner: route step, workspace step, agent child workflow, render step.

Routing (TASK-46): before any work, the task is classified easy/hard ONCE by a DBOS step
(``relay.research.route``), so a crash or replay reuses the recorded decision instead of asking
the router again; a second step (``relay.research.record_route``) logs it to
``router_decisions`` at most once per task. Routing only picks the model.

DBOS workflow and step names (``relay.research.*``) are persisted in DBOS history, so they keep
their pre-rename values.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from dbos import DBOS, SetWorkflowTimeout
from dbos._error import DBOSAwaitedWorkflowCancelledError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import UsageLimits
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from relay.config import get_settings
from relay.executor import workspace
from relay.executor.agent.agent import (
    ExecutorDeps,
    ResearchBrief,
    build_prompt,
    get_executor_agent,
    route_model_refs,
)
from relay.executor.routing import route_task
from relay.executor.runners import ArtifactSpec, TaskContext, register_runner
from relay.executor.workflows import db_step, engine
from relay.store.models import Commitment, Idea, RouterDecision, Task

WORKFLOW_NAME = "relay.research.run"
REQUEST_LIMIT = 30
# Wall-clock bound for one executor run (the agent child workflow). DBOS persists the deadline,
# so it also holds across a crash and recovery; the run is cancelled at its next step boundary.
TIMEOUT_ENV = "RELAY_RESEARCH_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 20 * 60


def executor_timeout_s() -> float:
    return float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)


@DBOS.step(name="relay.research.route")
def route_step(goal: str, scope_excludes: str) -> dict[str, Any]:
    """Classify the task easy/hard (never raises: any router failure is ``hard``).

    Returns JSON-able data; DBOS records it, so a replay never re-classifies.
    """
    settings = get_settings()
    decision = route_task(goal, scope_excludes, settings)
    primary, fallback = route_model_refs(decision.difficulty, settings)
    return {
        "difficulty": decision.difficulty,
        "valid": decision.valid,
        "status": decision.status,
        "latency_ms": decision.latency_ms,
        "router_model": decision.router_model,
        "raw": (decision.raw or "")[:500],
        "model_chosen": primary,
        "fallback_model": fallback,
    }


@db_step("relay.research.record_route")
def record_route_step(task_id: str, route: dict[str, Any]) -> dict[str, Any]:
    """Log the decision to ``router_decisions`` once per task; return the recorded one.

    Idempotent: the partial unique index on (task_id) WHERE is_active turns a re-run into a
    no-op, and the row already stored (not the argument) is what routes the task.
    """
    tid = uuid.UUID(task_id)
    with engine().begin() as c:
        c.execute(
            pg_insert(RouterDecision)
            .values(
                task_id=tid,
                # The owner's spoken yes (TASK-46 AC1); NULL for commitments recorded before 0003.
                turn_id=select(Commitment.assent_turn_id)
                .join(Task, Task.commitment_id == Commitment.id)
                .where(Task.id == tid)
                .scalar_subquery(),
                backend="llm",
                difficulty=route["difficulty"],
                latency_ms=route["latency_ms"],
                is_active=True,
                model_chosen=route["model_chosen"],
                router_status=route["status"],
            )
            .on_conflict_do_nothing(index_elements=["task_id"], index_where=sa_text("is_active"))
        )
        difficulty, model_chosen = c.execute(
            select(RouterDecision.difficulty, RouterDecision.model_chosen).where(
                RouterDecision.task_id == tid, RouterDecision.is_active
            )
        ).one()
    return {**route, "difficulty": difficulty, "model_chosen": model_chosen}


@DBOS.step(name="relay.executor.workspace")
def workspace_step(idea_id: str) -> str:
    """The idea's project folder (created if missing); recorded, so a replay reuses it."""
    iid = uuid.UUID(idea_id)
    with engine().connect() as c:
        title = c.execute(select(Idea.title).where(Idea.id == iid)).scalar_one()
    # Attribute lookup at call time, so tests can stub `workspace.project_dir`.
    return str(workspace.project_dir(iid, title, get_settings()))


def served_model_name(message: ModelResponse) -> str:
    """``<provider>:<model>`` as reported by the response (just the model if no provider)."""
    return (
        f"{message.provider_name}:{message.model_name}"
        if message.provider_name
        else (message.model_name or "?")
    )


@DBOS.workflow(name=WORKFLOW_NAME)
def executor_workflow(
    goal: str, scope_excludes: str, route: str | None = None, workspace: str | None = None
) -> dict[str, Any]:
    """Run the executor agent; each model request / tool call is a checkpointed DBOS step.

    ``route`` (``easy``/``hard``) picks the model registered for it; None runs the agent's
    default model (workflows recorded before routing existed recover that way). ``workspace``
    roots the file tools and the shell; None (pre-executor workflows) uses a fresh temp folder.
    Returns the brief as JSON-able data (stable across pickling and library upgrades) plus
    ``served_by``: the concrete model that answered each request (primary or fallback).
    """
    folder = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="relay-executor-"))
    result = get_executor_agent().run_sync(
        build_prompt(goal, scope_excludes),
        model=route,
        deps=ExecutorDeps(workspace=folder),
        usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
    )
    responses = [m for m in result.all_messages() if isinstance(m, ModelResponse)]
    served_by = [m.model_name for m in responses]
    DBOS.logger.info(f"executor {DBOS.workflow_id}: model requests served by {served_by}")
    served_model = served_model_name(responses[-1]) if responses else None
    return {
        **result.output.model_dump(mode="json"),
        "served_by": served_by,
        "served_model": served_model,
    }


def render_markdown(brief: ResearchBrief) -> str:
    """Title heading, summary, body and a Sources section; only the brief's own content."""
    seen: list[str] = []
    for url in (str(u) for u in brief.sources):
        if url not in seen:
            seen.append(url)
    sources = "\n".join(f"- <{u}>" for u in seen) if seen else "- (no sources cited)"
    return (
        f"# {brief.title.strip()}\n\n"
        f"{brief.summary.strip()}\n\n"
        f"{brief.body_markdown.strip()}\n\n"
        f"## Sources\n\n{sources}\n"
    )


def artifact_path(artifacts_dir: str, idea_id: object, task_id: object) -> Path:
    return Path(artifacts_dir).resolve() / str(idea_id) / f"{task_id}.md"


def write_document(path: Path, markdown: str) -> None:
    """Atomic overwrite, so a re-executed step never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@DBOS.step(name="relay.research.render")
def render_step(artifacts_dir: str, idea_id: str, task_id: str, brief: dict[str, Any]) -> str:
    path = artifact_path(artifacts_dir, idea_id, task_id)
    fields = {k: v for k, v in brief.items() if k in ResearchBrief.model_fields}
    write_document(path, render_markdown(ResearchBrief.model_validate(fields)))
    return path.as_uri()


def run_executor_task(ctx: TaskContext) -> ArtifactSpec:
    """Durable runner (called from the ``run_task`` workflow body): steps + child workflow."""
    route = record_route_step(str(ctx.task_id), route_step(ctx.goal, ctx.scope_excludes))
    DBOS.logger.info(
        f"{ctx.kind} task {ctx.task_id}: routed {route['difficulty']} "
        f"(router {route['status']}, {route['latency_ms']} ms) -> {route['model_chosen']}"
        f"{' then ' + route['fallback_model'] if route['fallback_model'] else ''}"
    )
    workspace = workspace_step(str(ctx.idea_id))
    timeout_s = executor_timeout_s()
    try:
        with SetWorkflowTimeout(timeout_s):
            brief = executor_workflow(ctx.goal, ctx.scope_excludes, route["difficulty"], workspace)
    except DBOSAwaitedWorkflowCancelledError as exc:
        raise TimeoutError(f"{ctx.kind} did not finish within {timeout_s:g} s") from exc
    url = render_step(ctx.artifacts_dir, str(ctx.idea_id), str(ctx.task_id), brief)
    served_model = brief.get("served_model")
    DBOS.logger.info(f"{ctx.kind} task {ctx.task_id}: served by {served_model}")
    return ArtifactSpec(
        kind="document",
        url=url,
        summary=str(brief["summary"]),
        served_model=str(served_model) if served_model else None,
    )


register_runner("research", run_executor_task, durable=True)
