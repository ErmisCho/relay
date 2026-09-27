"""Direct starts: a plain instruction starts work without a read-back and a spoken "yes".

The server, not the model, decides: a fresh user request judged ``directive`` stands in for the
assent. A request for feedback or ideas starts nothing and gets no read-back; a failed check,
a "no" to a read-back and ``DIRECT_DISPATCH`` off keep the read-back protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.commitment.classify import DIRECTIVE_TOOL
from relay.delegator.commitment.protocol import NOT_YET_RESULT
from relay.store.db import create_engine, create_sessionmaker

from .harness import FakeLabelModel, build_convo, propose, text

Db = async_sessionmaker[AsyncSession]
ON_IT = "On it. I'll let you know when the document is ready."


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[Db]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


async def test_plain_instruction_starts_without_readback_and_records_the_request(
    db: Db,
) -> None:
    """Bug caught: a directive still read back and waited, or started without an audit trail."""
    c = build_convo(db, assent=FakeLabelModel("directive"), direct_dispatch=True)
    request = "Look into lo-fi drum patterns and write it up for me, skip pricing."
    spoken = await c.turn(request, propose(c.goal))
    assert spoken == ON_IT and "Just to confirm" not in spoken

    (row,) = await c.commitments()
    assert row.assent_utterance == request and row.assent_turn_id is not None
    assert row.readback_text.startswith("Started directly on the user's instruction")
    assert await c.task_count() == 1 and len(c.client.enqueued) == 1
    # The classifier saw the user's words and the goal, not just the model's arguments.
    (call,) = c.assent.calls
    assert request in call[1]["content"] and c.goal in call[1]["content"]


@pytest.mark.parametrize(
    ("label", "said"),
    [
        ("exploring", "Research bike locks for me."),  # the classifier says not yet
        ("directive", "What do you think about researching bike locks?"),  # precheck
        ("directive", "Could you suggest something? Maybe research bike locks?"),  # precheck
    ],
)
async def test_exploring_starts_nothing_and_skips_the_readback(
    db: Db, label: str, said: str
) -> None:
    """Bug caught: a request for ideas answered with "Just to confirm…" or started unasked."""
    c = build_convo(db, assent=FakeLabelModel(label), direct_dispatch=True)
    spoken = await c.turn(said, propose(c.goal), text("A few ideas: start with the drums."))
    assert spoken == "A few ideas: start with the drums."  # the model's own answer
    assert c.tool_results() == [NOT_YET_RESULT]
    await asyncio.sleep(0.3)  # past the grace window
    await c.assert_nothing_dispatched()


async def test_a_failed_check_falls_back_to_the_readback(db: Db) -> None:
    """Bug caught: a classifier timeout starting work (or silently dropping the request)."""
    c = build_convo(db, assent=FakeLabelModel(TimeoutError("slow")), direct_dispatch=True)
    assert await c.turn("Research bike locks for me.", propose(c.goal)) == c.readback()
    await asyncio.sleep(0.3)
    await c.assert_nothing_dispatched()


async def test_an_instruction_answering_a_readback_starts_directly(db: Db) -> None:
    """Bug caught (live 2026-09-27): "…what I want is X, write it up" after a read-back got a
    second read-back instead of starting."""
    c = build_convo(db, assent=FakeLabelModel("directive"), direct_dispatch=False)
    await c.turn("Research bike locks.", propose(c.goal), text(c.readback()))
    c.app.state.service.settings.direct_dispatch = True
    goal = f"research {c.tag} bike locks in Europe"
    said = "So what I want is a document on bike locks in Europe. Write that up for me."
    assert await c.turn(said, propose(goal)) == ON_IT
    (row,) = await c.commitments()
    assert (row.goal, row.assent_utterance) == (goal, said)


async def test_a_no_to_a_readback_never_starts_directly(db: Db) -> None:
    c = build_convo(db, assent=FakeLabelModel("directive"), direct_dispatch=True)
    c.app.state.service.settings.direct_dispatch = False
    await c.turn("Research bike locks.", propose(c.goal), text(c.readback()))
    c.app.state.service.settings.direct_dispatch = True
    goal = f"research {c.tag} bike locks in Europe"
    spoken = await c.turn("No, not yet. Research Europe instead.", propose(goal))
    assert spoken == c.readback(goal)
    await asyncio.sleep(0.3)
    await c.assert_nothing_dispatched()


async def test_resend_that_is_no_longer_a_directive_cancels(db: Db) -> None:
    """Bug caught: ElevenLabs re-sending "…, wait, not yet" after a direct start was ignored."""
    c = build_convo(db, assent=FakeLabelModel("directive"), direct_dispatch=True, grace_s=3.5)
    await c.turn("Research bike locks for me.", propose(c.goal))
    await c.resend("Research bike locks for me, wait, not yet.", text("Okay, I haven't started."))
    await c.assert_nothing_dispatched()


async def test_a_new_request_during_the_grace_window_starts_as_separate_work(db: Db) -> None:
    """Bug caught: speech split in two ("what models can my PC run? ... by the way, the
    weather?"). The second request arrived while the first waited out its grace, was judged
    only as a reaction to it, and was dropped: one task instead of two."""
    c = build_convo(db, assent=FakeLabelModel("directive"), direct_dispatch=True, grace_s=3.5)
    assert await c.turn("Check which local models my PC can run.", propose(c.goal)) == ON_IT
    weather = f"check the weather {c.tag}"  # commitments() counts goals carrying the tag
    assert await c.turn("By the way, what is the weather?", propose(weather)) == ON_IT
    goals = sorted(row.goal for row in await c.commitments())
    assert goals == sorted([c.goal, weather])


async def test_direct_dispatch_off_always_reads_back(db: Db) -> None:
    c = build_convo(db, assent=FakeLabelModel("directive"), direct_dispatch=False)
    assert await c.turn("Research bike locks for me.", propose(c.goal)) == c.readback()
    assert not any(DIRECTIVE_TOOL in str(call) for call in c.assent.calls)
    await asyncio.sleep(0.3)
    await c.assert_nothing_dispatched()
