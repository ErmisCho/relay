"""Dispatch API used from the Delegator process (DBOS-free: only ``DBOSClient``)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from dbos import DBOSClient, EnqueueOptions
from sqlalchemy import Engine, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings, get_settings
from relay.executor.common import (
    QUEUE_NAME,
    WORKFLOW_NAME,
    get_dbos_client_async,
    workflow_id_for,
)
from relay.store.models import Commitment, Task

log = logging.getLogger(__name__)


def enqueue_options(workflow_id: str) -> EnqueueOptions:
    return {
        "workflow_name": WORKFLOW_NAME,
        "queue_name": QUEUE_NAME,
        "workflow_id": workflow_id,
        "workflow_id_reuse_policy": "return-existing",
    }


@dataclass(frozen=True)
class StartedTask:
    task_id: uuid.UUID
    workflow_id: str
    created: bool


async def start_task(
    db: async_sessionmaker[AsyncSession],
    *,
    commitment_id: uuid.UUID,
    kind: str,
    session_id: uuid.UUID | None = None,
    client: DBOSClient | None = None,
    settings: Settings | None = None,
) -> StartedTask:
    """Create (or reuse) the task for ``commitment_id`` and enqueue its workflow.

    Idempotency is enforced in Postgres: the commitment row is locked ``FOR UPDATE`` while
    we look for an existing task, so concurrent dispatches of one commitment serialize and
    the second sees the first's task. The workflow id is derived from the task id and DBOS
    treats a reused workflow id as "return existing", so a task never gets two workflows.
    Returns as soon as the workflow is enqueued; the executor runs it asynchronously.
    """
    client = client or await get_dbos_client_async(settings or get_settings())
    async with db() as session, session.begin():
        locked = await session.scalar(
            select(Commitment.id).where(Commitment.id == commitment_id).with_for_update()
        )
        if locked is None:
            raise LookupError(f"commitment {commitment_id} does not exist")
        task = await session.scalar(
            select(Task).where(Task.commitment_id == commitment_id).order_by(Task.created_at)
        )
        created = task is None
        if task is None:
            task_id = uuid.uuid4()
            task = Task(
                id=task_id,
                commitment_id=commitment_id,
                kind=kind,
                status="queued",
                dbos_workflow_id=workflow_id_for(task_id),
            )
            session.add(task)
        task_id, workflow_id, status = task.id, task.dbos_workflow_id, task.status
    assert workflow_id is not None

    # Enqueue after COMMIT so the worker always finds the row. A crash in between leaves a
    # queued task without a workflow; ``reconcile_orphaned_tasks`` (run at worker startup)
    # or a repeat dispatch of this commitment enqueues it.
    if created or status == "queued":
        await asyncio.to_thread(
            client.enqueue,
            enqueue_options(workflow_id),
            str(task_id),
            str(session_id) if session_id else None,
        )
    return StartedTask(task_id=task_id, workflow_id=workflow_id, created=created)


def reconcile_orphaned_tasks(engine: Engine, client: DBOSClient) -> list[str]:
    """Enqueue every ``queued`` task whose workflow does not exist in DBOS (blocking).

    Repairs a crash between ``start_task``'s COMMIT and its enqueue. Uses the same
    deterministic workflow id with "return existing", so racing a live dispatch is harmless
    (the dispatching session id is not stored on the task, so a repaired task's report has
    ``session_id`` NULL; reports are delivered to any session anyway). Returns the ids enqueued.
    """
    with engine.connect() as c:
        rows = c.execute(
            select(Task.id, Task.dbos_workflow_id).where(Task.status == "queued")
        ).all()
    wanted = {tid: (wf or workflow_id_for(tid), wf is None) for tid, wf in rows}
    if not wanted:
        return []
    listed = client.list_workflows(
        workflow_ids=[wf for wf, _ in wanted.values()], load_input=False, load_output=False
    )
    existing = {wf.workflow_id for wf in listed}
    enqueued: list[str] = []
    for tid, (wf_id, missing_id) in wanted.items():
        if wf_id in existing:
            continue
        if missing_id:  # row predates dispatch.py: pin the deterministic workflow id
            with engine.begin() as c:
                c.execute(
                    update(Task)
                    .where(Task.id == tid, Task.dbos_workflow_id.is_(None))
                    .values(dbos_workflow_id=wf_id)
                )
        client.enqueue(enqueue_options(wf_id), str(tid), None)
        enqueued.append(wf_id)
    if enqueued:
        log.warning("re-enqueued %d orphaned queued task(s): %s", len(enqueued), enqueued)
    return enqueued
