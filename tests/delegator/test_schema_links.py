"""Schema links the Phase 1 / routing audits rely on, written through the real Delegator path.

Bugs pinned: a commitment without its session / yes-turn forces the audit back onto a time
window (wrong when sessions overlap) and leaves the task router nothing to link to; token usage
that is dropped (or invented as 0 when a provider reports none) makes the cost line lie.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.llm import ChatDelta
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Turn

from .commitment.harness import build_convo, dispatch, propose, text


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


@dataclass
class UsageDelta(ChatDelta):
    """A delta from a provider that reports usage (``ChatDelta`` has no field for it yet)."""

    usage: dict[str, int] | None = None


async def _turns(db: async_sessionmaker[AsyncSession], session_id: str, role: str) -> list[Turn]:
    async with db() as s:
        rows = await s.scalars(
            select(Turn)
            .where(Turn.session_id == uuid.UUID(session_id), Turn.role == role)
            .order_by(Turn.ts)
        )
        return list(rows)


async def test_commitment_records_its_session_and_the_yes_turn(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("Research bike locks for me, skip pricing.", propose(c.goal), text(c.readback()))
    await c.turn("Yes, go ahead!", dispatch(), text("On it."))

    (row,) = await c.commitments()
    yes_turn = [t for t in await _turns(db, c.session_id, "user") if t.text == "Yes, go ahead!"]
    assert row.session_id == uuid.UUID(c.session_id)
    assert len(yes_turn) == 1 and row.assent_turn_id == yes_turn[0].id


async def test_token_usage_is_summed_when_reported_and_absent_otherwise(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    # A rejected proposal (no exclusion) keeps the model going for a second round; a valid one
    # would end the turn with the server-spoken read-back after a single model call.
    tool_round = propose(c.goal, excludes="  ")
    tool_round[-1] = UsageDelta(
        finish_reason="tool_calls",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    reply = [
        ChatDelta(content="What should I leave out?"),
        UsageDelta(finish_reason="stop", usage={"input_tokens": 20, "output_tokens": 7}),
    ]
    await c.turn("Research bike locks for me, skip pricing.", tool_round, reply)
    await c.turn("Hmm, let me think.", text("Sure, take your time."))  # reports nothing

    first, second = await _turns(db, c.session_id, "assistant")
    # Summed over both model calls of the turn; total only where the provider reported it.
    assert first.meta["usage"] == {"input_tokens": 30, "output_tokens": 12, "total_tokens": 15}
    assert "usage" not in second.meta
