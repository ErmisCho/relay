"""ElevenLabs re-sends a user turn after a (false) barge-in; the commitment must follow the
final utterance, never a partial one, and a cut request must never orphan a commitment."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.commitment import AssentLabel, CommitmentHook
from relay.delegator.commitment.protocol import (
    DISPATCH_GRACE_S,
    NEXT_NOTE_KEY,
    NOTE_ALREADY_STARTING,
    NOTE_CANCELLED,
    NOTE_CANCELLED_SHUTDOWN,
    NOTE_INTERRUPTED,
    RESERVATION_KEY,
)
from relay.delegator.commitment.reconcile import start_orphaned_commitments
from relay.executor.dispatch import start_task
from relay.store import ideas_repo
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Commitment, RouterDecision, Task, Turn

from ..conftest import make_settings
from .harness import FakeLabelModel, StubDBOSClient, build_convo, dispatch, propose, text


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


async def test_extended_resend_with_a_hedge_cancels_the_dispatch_of_the_partial_yes(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    # "Yes" -> dispatch; ElevenLabs drops the answer mid-stream and re-sends the full utterance.
    await c.cut_turn("Yes", dispatch(), text("Okay, ", "starting on that now."))
    await c.resend("Yes, but what about Europe?", text("I haven't started it."))
    assert NOTE_CANCELLED[AssentLabel.HEDGE] in c.notes()
    await c.assert_nothing_dispatched()


async def test_duplicate_resend_of_a_yes_dispatches_exactly_once(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.cut_turn("Yes, go ahead.", dispatch(), text("Okay, ", "starting."))
    await c.resend(None, dispatch(), text("It's underway."))
    assert NOTE_ALREADY_STARTING in c.notes()
    assert c.tool_results()[0].startswith("Already started")
    (row,) = await c.commitments()
    assert row.assent_utterance == "Yes, go ahead."
    assert await c.task_count() == 1 and len(c.client.enqueued) == 1


async def test_yes_cut_before_the_dispatch_call_survives_the_resend(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.cut_turn("Yes, go ahead.", text("On", " it"))  # cut before the model dispatched
    await c.resend(None, dispatch(), text("ok"))
    assert len(await c.commitments()) == 1


async def test_replay_after_dispatch_says_already_started_not_nothing_started(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.turn("Yes.", dispatch(), text("On it."))
    assert len(await c.commitments()) == 1
    await c.resend(None, dispatch(), text("ok"))  # identical re-send after completion
    assert NOTE_ALREADY_STARTING in c.notes()
    assert c.tool_results()[0].startswith("Already started")
    assert "no work has started" not in c.tool_results()[0]
    assert len(await c.commitments()) == 1


async def test_request_cancelled_during_dispatch_still_creates_the_task(
    db: async_sessionmaker[AsyncSession],
) -> None:
    client = StubDBOSClient()
    entered = asyncio.Event()

    async def slow_start(dbm: Any, **kw: Any) -> Any:
        entered.set()
        await asyncio.sleep(0.2)
        return await start_task(dbm, client=client, **kw)

    c = build_convo(db, grace_s=0.1)
    c.app.state.service.registry.get("dispatch_task")._start = slow_start
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    consumer = asyncio.create_task(c.cut_turn("Yes, go ahead.", dispatch(), text("Ok", "ay")))
    await asyncio.wait_for(entered.wait(), 5)
    consumer.cancel()  # ElevenLabs drops the request while start_task is in flight
    await asyncio.gather(consumer, return_exceptions=True)
    await c.app.state.service.drain()
    assert len(await c.commitments()) == 1
    assert await c.task_count() == 1 and len(client.enqueued) == 1


async def _orphan(
    db: async_sessionmaker[AsyncSession], artifact: str = "document", age: timedelta | None = None
) -> uuid.UUID:
    idea_id = await ideas_repo.create_idea(db, "orphan " + uuid.uuid4().hex)
    async with db() as s, s.begin():
        row = Commitment(
            idea_id=idea_id,
            goal="orphan",
            scope_excludes="x",
            artifact_kind=artifact,
            readback_text="rb",
            assent_utterance="yes",
            assented_at=datetime.now(UTC),
        )
        if age is not None:
            row.created_at = datetime.now(UTC) - age
        s.add(row)
        await s.flush()
        return row.id


async def _task_for(db: async_sessionmaker[AsyncSession], cid: uuid.UUID) -> Any:
    async with db() as s:
        return await s.scalar(select(Task).where(Task.commitment_id == cid))


async def test_reconcile_starts_recent_in_scope_commitments_without_a_task(
    db: async_sessionmaker[AsyncSession],
) -> None:
    fresh = await _orphan(db)
    code = await _orphan(db, artifact="pull_request")  # code is not enabled
    old = await _orphan(db, age=timedelta(hours=25))
    client = StubDBOSClient()
    start = functools.partial(start_task, client=client)
    started = await start_orphaned_commitments(db, make_settings(), start)
    assert fresh in started and code not in started and old not in started
    assert await _task_for(db, fresh) is not None
    assert await _task_for(db, code) is None and await _task_for(db, old) is None
    assert client.enqueued and all(e[2] is None for e in client.enqueued)  # no session id
    again = await start_orphaned_commitments(db, make_settings(), start)
    assert fresh not in again  # idempotent


async def test_proposal_turn_cut_mid_stream_is_not_assentable_even_if_history_has_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.cut_turn("research it", propose(c.goal), text(c.readback()[:20], c.readback()[20:]))
    # Suppose ElevenLabs recorded the full read-back anyway (untruncated history).
    c.history.append({"role": "assistant", "content": c.readback()})
    await c.turn("yes", dispatch(), text("ok"))
    assert NOTE_INTERRUPTED in c.notes()
    await c.assert_nothing_dispatched()


async def test_readback_followed_by_more_speech_is_not_delivered(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn(
        "research it",
        propose(c.goal),
        text(c.readback(), " Great, I've started on it already."),
    )
    await c.turn("okay", dispatch(), text("ok"))
    await c.assert_nothing_dispatched()


async def test_ready_scorer_never_runs_during_an_active_conversation(
    db: async_sessionmaker[AsyncSession],
) -> None:
    ready = FakeLabelModel("keep_talking")
    hook = CommitmentHook(assent_model=FakeLabelModel(), ready_model=ready, ready_idle_s=0.4)
    c = build_convo(db, hook=hook)
    for i in range(4):
        await c.turn(f"thought {i} {c.tag}", text("mm"))
        await asyncio.sleep(0.1)
    assert ready.calls == []  # never while the user keeps talking
    await asyncio.sleep(0.8)  # idle: the whole backlog is scored in one batch
    assert len(ready.calls) == 4
    async with db() as s:
        rows = list(
            await s.scalars(
                select(RouterDecision)
                .join(Turn, Turn.id == RouterDecision.turn_id)
                .where(Turn.text.contains(c.tag))
            )
        )
    assert len(rows) == 4 and {r.ready for r in rows} == {"keep_talking"}


async def test_drain_scores_the_backlog_but_bounded(db: async_sessionmaker[AsyncSession]) -> None:
    ready = FakeLabelModel("ready_to_execute")
    hook = CommitmentHook(assent_model=FakeLabelModel(), ready_model=ready, ready_idle_s=60)
    c = build_convo(db, hook=hook)
    await c.turn(f"idea {c.tag}", text("mm"))
    assert ready.calls == []
    await hook.drain()
    assert len(ready.calls) == 1


# --- the grace window: nothing is written until the user is really done -------------------


async def test_completed_stream_then_hedge_resend_0_3s_later_does_not_commit(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db, grace_s=3.5)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.turn("Yes", dispatch(), text("Okay, starting on that now."))  # stream COMPLETED
    await asyncio.sleep(0.3)
    await c.resend("Yes, but what about Europe?", text("I haven't started."))
    assert NOTE_CANCELLED[AssentLabel.HEDGE] in c.notes()
    await c.assert_nothing_dispatched()


async def test_asr_revision_to_a_hedge_cancels(db: async_sessionmaker[AsyncSession]) -> None:
    c = build_convo(db, grace_s=3.5)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.cut_turn("Yeah go ahead", dispatch(), text("Okay, ", "starting."))
    # Not a prefix extension: ASR revised the words.
    await c.resend("Yes go ahead but skip Europe", text("ok"))
    await c.assert_nothing_dispatched()


async def test_resend_with_the_partial_reply_kept_in_history_is_still_judged(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db, grace_s=3.5)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.cut_turn("Yes", dispatch(), text("Okay, ", "star"))
    c.history.append({"role": "assistant", "content": "Okay, star"})  # history hash differs
    await c.turn("but leave out Europe", text("ok"))
    await c.assert_nothing_dispatched()


async def test_new_unrelated_turn_during_the_reservation_cancels_it(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db, grace_s=3.5)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.turn("Yes please", dispatch(), text("Okay, starting on that now."))
    c.assent.answer = "new_information"
    await c.turn("what's the weather like tomorrow", text("Sunny."))
    assert len(c.assent.calls) == 2  # the new turn was classified against the read-back
    assert NOTE_CANCELLED[AssentLabel.NEW_INFORMATION] in c.notes()
    await c.assert_nothing_dispatched()


async def test_hedge_resend_3s_later_still_cancels(db: async_sessionmaker[AsyncSession]) -> None:
    c = build_convo(db)  # harness default grace is short; use the production default here
    c.app.state.service.registry.get("dispatch_task")._grace_s = DISPATCH_GRACE_S
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.cut_turn("Yes", dispatch(), text("Okay, ", "starting."))
    await asyncio.sleep(3.0)
    await c.resend("Yes, hmm, actually wait", text("ok"))
    await c.assert_nothing_dispatched()


async def test_affirmative_resend_restarts_the_grace(db: async_sessionmaker[AsyncSession]) -> None:
    c = build_convo(db, grace_s=0.6)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.cut_turn("Yes", dispatch(), text("Okay, ", "starting."))
    await asyncio.sleep(0.4)
    await c.resend("Yes, go ahead", text("It's starting."))
    await asyncio.sleep(0.35)  # 0.75 s after the first request, 0.35 s after the re-send
    res = c.store.get(uuid.UUID(c.session_id)).extra[RESERVATION_KEY]
    assert res.status == "reserved"
    (row,) = await c.commitments()
    assert row.assent_utterance == "Yes, go ahead"  # the final, complete utterance


async def test_drain_cancels_a_reserved_dispatch(
    db: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    c = build_convo(db, grace_s=5.0)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.turn("Yes please", dispatch(), text("Okay, starting on that now."))
    caplog.set_level(logging.WARNING)
    await c.app.state.service.drain()  # shutdown inside the grace window
    await c.assert_nothing_dispatched()
    assert any("NOT STARTED" in r.getMessage() for r in caplog.records)
    state = c.store.get(uuid.UUID(c.session_id))
    assert state.extra[NEXT_NOTE_KEY] == NOTE_CANCELLED_SHUTDOWN


# --- a slow classifier must not let the grace expire mid-judgement -------------------------


@pytest.mark.parametrize(("label", "expected"), [("new_information", 0), ("affirmative", 1)])
async def test_request_near_the_deadline_holds_the_commit_while_classifying(
    db: async_sessionmaker[AsyncSession], label: str, expected: int
) -> None:
    grace = 1.5
    c = build_convo(db, grace_s=grace)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    t0 = time.monotonic()
    await c.cut_turn("Yes", dispatch(), text("Okay, ", "starting."))
    c.assent.answer, c.assent.delay = label, 1.2  # outlasts the remaining grace
    await asyncio.sleep(max(0.0, grace - 0.5 - (time.monotonic() - t0)))
    arrived = time.monotonic()
    await c.resend("Yes, and include Europe too.", text("ok"))
    res = c.store.get(uuid.UUID(c.session_id)).extra[RESERVATION_KEY]
    assert time.monotonic() - t0 > grace  # the original deadline has passed...
    if expected:
        assert res.status == "reserved"  # ...and the grace restarted from the new request
        assert time.monotonic() < arrived + grace
    assert len(await c.commitments()) == expected


async def test_pleasantry_after_the_spoken_confirmation_keeps_the_dispatch(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db, grace_s=1.0)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.turn("Yes, go ahead.", dispatch(), text("Okay, starting on that now."))
    c.assent.answer = "hedge"
    await c.turn("Thanks.", text("You're welcome."))
    assert NOTE_ALREADY_STARTING in c.notes()
    assert len(await c.commitments()) == 1


async def test_negative_after_the_spoken_confirmation_still_cancels(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db, grace_s=1.0)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.turn("Yes, go ahead.", dispatch(), text("Okay, starting on that now."))
    await c.turn("No wait, don't.", text("Okay, I won't."))
    await c.assert_nothing_dispatched()


async def test_hedge_resend_of_the_assent_turn_keeps_the_strict_rule(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db, grace_s=1.0)
    await c.turn("research it, skip pricing", propose(c.goal), text(c.readback()))
    await c.turn("Yes, go ahead.", dispatch(), text("Okay, starting on that now."))
    c.assent.answer = "hedge"
    await c.resend("Yes, go ahead. I guess.", text("ok"))  # re-send: no reply in between
    await c.assert_nothing_dispatched()
