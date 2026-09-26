"""Task status: the ``tasks`` row joined with its DBOS workflow status (via ``DBOSClient``)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from dbos import DBOSClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.store.models import Artifact, Commitment, Task

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskStatus:
    task_id: uuid.UUID
    idea_id: uuid.UUID
    kind: str
    goal: str
    status: str  # tasks.status
    workflow_id: str | None
    workflow_status: str | None  # DBOS: ENQUEUED, PENDING, SUCCESS, ERROR, ... or None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    artifact_url: str | None
    artifact_summary: str | None


async def get_status(
    db: async_sessionmaker[AsyncSession],
    *,
    task_id: uuid.UUID | None = None,
    idea_id: uuid.UUID | None = None,
    client: DBOSClient | None = None,
    limit: int = 5,
) -> list[TaskStatus]:
    """Tasks matching ``task_id`` or ``idea_id``, newest first.

    ``client`` is optional: without it (or if DBOS is unreachable) ``workflow_status`` is None.
    """
    if task_id is None and idea_id is None:
        raise ValueError("get_status needs task_id or idea_id")
    stmt = (
        select(Task, Commitment.idea_id, Commitment.goal, Artifact.url, Artifact.summary)
        .join(Commitment, Commitment.id == Task.commitment_id)
        .outerjoin(Artifact, Artifact.task_id == Task.id)
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    if task_id is not None:
        stmt = stmt.where(Task.id == task_id)
    if idea_id is not None:
        stmt = stmt.where(Commitment.idea_id == idea_id)
    async with db() as session:
        rows = (await session.execute(stmt)).all()

    wf_status: dict[str, str] = {}
    wf_ids = [r[0].dbos_workflow_id for r in rows if r[0].dbos_workflow_id]
    if client is not None and wf_ids:
        try:
            for wf in await client.list_workflows_async(
                workflow_ids=wf_ids, load_input=False, load_output=False
            ):
                wf_status[wf.workflow_id] = str(wf.status)
        except Exception:  # status must degrade, never break the voice turn
            log.warning("DBOS workflow status lookup failed", exc_info=True)

    return [
        TaskStatus(
            task_id=t.id,
            idea_id=idea,
            kind=t.kind,
            goal=goal,
            status=t.status,
            workflow_id=t.dbos_workflow_id,
            workflow_status=wf_status.get(t.dbos_workflow_id or ""),
            created_at=t.created_at,
            started_at=t.started_at,
            finished_at=t.finished_at,
            error=t.error,
            artifact_url=url,
            artifact_summary=summary,
        )
        for t, idea, goal, url, summary in rows
    ]
