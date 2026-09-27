"""Startup repair: start commitments that have no task (idempotent via ``start_task``).

A commitment row is only written after spoken assent, so a commitment without a task means the
process died (or ``start_task`` failed) between the commit and the dispatch. Only recent,
still-in-scope commitments are started; older or now out-of-scope ones are logged and skipped.
``start_task`` is idempotent per commitment, so re-running this is harmless.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.commitment.protocol import StartTask
from relay.delegator.scope import validate_commitment_args
from relay.executor.dispatch import start_task
from relay.store.models import Commitment, Task

log = logging.getLogger(__name__)

#: Commitments older than this are not started automatically (the user has moved on).
MAX_ORPHAN_AGE = timedelta(hours=24)


async def start_orphaned_commitments(
    db: async_sessionmaker[AsyncSession],
    settings: Settings,
    start: StartTask | None = None,
    *,
    max_age: timedelta = MAX_ORPHAN_AGE,
) -> list[uuid.UUID]:
    """``start_task`` for each recent, in-scope commitment without a task; returns those started.

    No session id is passed: the dispatching session is gone. Failures are logged per commitment
    and never raised.
    """
    starter: StartTask = start or start_task
    async with db() as session:
        rows = (
            await session.execute(
                select(Commitment.id, Commitment.artifact_kind, Commitment.created_at)
                .outerjoin(Task, Task.commitment_id == Commitment.id)
                .where(Task.id.is_(None))
                .order_by(Commitment.created_at)
            )
        ).all()
    cutoff = datetime.now(UTC) - max_age
    started: list[uuid.UUID] = []
    for commitment_id, artifact_kind, created_at in rows:
        if created_at < cutoff:
            log.warning(
                "reconcile: skipping commitment %s without a task: older than %s (%s)",
                commitment_id,
                max_age,
                created_at.isoformat(),
            )
            continue
        decision = validate_commitment_args({"artifact_kind": artifact_kind}, settings)
        if not decision.allowed or decision.kind is None:
            log.warning(
                "reconcile: skipping commitment %s without a task: out of scope (%s)",
                commitment_id,
                decision.reason,
            )
            continue
        try:
            await starter(db, commitment_id=commitment_id, kind=decision.kind.value)
        except Exception:
            log.exception("reconcile: starting orphaned commitment %s failed", commitment_id)
            continue
        log.warning("reconcile: started orphaned commitment %s", commitment_id)
        started.append(commitment_id)
    return started
