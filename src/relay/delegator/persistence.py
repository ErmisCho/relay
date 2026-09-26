"""Session and turn writes for the Delegator."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.store.models import Session, Turn


async def ensure_session(db: async_sessionmaker[AsyncSession], session_id: uuid.UUID) -> None:
    """Upsert the ``sessions`` row (no-op if it exists)."""
    async with db() as s, s.begin():
        await s.execute(insert(Session).values(id=session_id).on_conflict_do_nothing())


async def record_turn(
    db: async_sessionmaker[AsyncSession],
    *,
    session_id: uuid.UUID,
    role: str,
    text: str,
    idea_id: uuid.UUID | None,
    route: str | None = None,
    model_used: str | None = None,
    latency_ms: int | None = None,
    meta: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> uuid.UUID:
    """Upsert the session and insert one ``turns`` row in one transaction; returns its id.

    ``ts`` defaults to the database's ``now()``; pass it explicitly when the row is
    written later than the moment it describes.
    """
    turn_id = uuid.uuid4()
    async with db() as s, s.begin():
        await s.execute(insert(Session).values(id=session_id).on_conflict_do_nothing())
        s.add(
            Turn(
                id=turn_id,
                session_id=session_id,
                idea_id=idea_id,
                role=role,
                text=text,
                route=route,
                model_used=model_used,
                latency_ms=latency_ms,
                meta=meta or {},
                **({"ts": ts} if ts is not None else {}),
            )
        )
    return turn_id
