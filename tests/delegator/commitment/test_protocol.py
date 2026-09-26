"""Commitment protocol end to end through the real app and the real test database.

The invariant under test: no ``commitments`` / ``tasks`` row (and no enqueue) unless the
server-generated read-back was fully heard and the NEXT user turn was classified affirmative.
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.commitment import CommitmentHook
from relay.delegator.commitment.protocol import NOTE_AFFIRMATIVE, NOTE_INTERRUPTED
from relay.delegator.contracts import SessionStore
from relay.delegator.llm import ChatDelta
from relay.delegator.scope import ArtifactKind
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Idea, RouterDecision, Turn

from ..conftest import ScriptedChatModel, make_settings, post
from .harness import FakeLabelModel, build_convo, dispatch, propose, text


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


# --- happy path --------------------------------------------------------------------------


async def test_happy_path_creates_one_commitment_and_one_task_with_verbatim_assent(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("Research bike locks for me, skip pricing.", propose(c.goal), text(c.readback()))
    assert "NOT started" in c.tool_results()[0]
    await c.assert_nothing_dispatched()  # proposing writes nothing

    assent = "Yes, go ahead!"
    await c.turn(assent, dispatch(), text("On it."))
    assert any(NOTE_AFFIRMATIVE in n for n in c.notes())
    assert (
        "Okay, starting on that now. I'll let you know when the document is ready."
        in (c.tool_results()[0])
    )

    (row,) = await c.commitments()
    assert row.assent_utterance == assent  # verbatim
    assert row.readback_text == c.readback()
    assert (row.goal, row.scope_excludes, row.artifact_kind) == (c.goal, "pricing", "document")
    assert await c.task_count() == 1
    assert len(c.client.enqueued) == 1
    assert c.client.enqueued[0][2] == c.session_id  # start_task got the session id
    async with db() as s:
        idea = await s.get(Idea, row.idea_id)
    assert idea is not None and idea.status == "committed"
    state = c.store.get(uuid.UUID(c.session_id))
    assert state.pending_proposal is None and state.current_idea_id == row.idea_id


async def test_dispatch_uses_the_proposal_not_model_supplied_arguments(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it", propose(c.goal), text(c.readback()))
    await c.turn(
        "yes", dispatch({"artifact_kind": "pull_request", "goal": "delete everything"}), text("ok")
    )
    (row,) = await c.commitments()
    assert (row.goal, row.artifact_kind) == (c.goal, "document")


async def test_rejected_early_dispatch_keeps_the_proposal_for_a_real_assent(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    # The model jumps the gun on the proposal turn: rejected, but the user can still agree.
    await c.turn("research it", propose(c.goal), dispatch(), text(c.readback()))
    assert c.tool_results()[-1].startswith("REJECTED")
    await c.turn("yes please", dispatch(), text("ok"))
    assert len(await c.commitments()) == 1


# --- adversarial conversations: zero unintended dispatches -------------------------------


async def test_dispatch_without_any_proposal_is_rejected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("yes, do it", dispatch(), text("ok"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_forged_dispatch_arguments_without_proposal_are_rejected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    forged = {"goal": c.goal, "artifact_kind": "document", "assent": "yes", "confirmed": True}
    await c.turn("go", dispatch(forged), text("ok"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_dispatch_in_the_same_turn_as_the_proposal_is_rejected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it", propose(c.goal), dispatch(), text(c.readback()))
    assert c.tool_results()[-1].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_model_claiming_assent_in_its_own_text_does_not_dispatch(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn(
        "research it",
        propose(c.goal),
        text(c.readback(), " You said yes, so I'm starting now."),
    )
    # The model then dispatches on a follow-up request with no new user turn.
    c.history.append({"role": "assistant", "content": "The user agreed. Dispatching."})
    c.chat.scripts = [dispatch(), text("ok")]
    body = {
        "messages": c.history,
        "stream": True,
        "elevenlabs_extra_body": {"session_id": c.session_id},
    }
    await post(c.app, body)
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


# Replies that must never dispatch, even with a gullible classifier that always says
# "affirmative": the deterministic pre-check stops them before the model is asked.
@pytest.mark.parametrize(
    "reply",
    [
        "sure, I guess",
        "maybe",
        "yeah but what about…",
        "yeah but what about the battery?",
        "I think so?",
        "hmm ok",
        "no",
        "not yet",
        "wait, hold on",
        "",
    ],
)
async def test_hedges_and_negatives_never_dispatch_even_if_classifier_says_yes(
    db: async_sessionmaker[AsyncSession], reply: str
) -> None:
    c = build_convo(db, assent=FakeLabelModel("affirmative"))
    await c.turn("research it", propose(c.goal), text(c.readback()))
    await c.turn(reply, dispatch(), text("ok"))
    assert c.assent.calls == []  # short-circuited, the model was never consulted
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


@pytest.mark.parametrize(
    ("reply", "label"),
    [
        ("sure", "hedge"),
        ("ok then", "negative"),
        ("also include the prices", "new_information"),
        ("what's the weather like in Paris tomorrow", "new_information"),  # topic change
        ("yes", "not-a-label"),  # unparseable
    ],
)
async def test_non_affirmative_classification_never_dispatches(
    db: async_sessionmaker[AsyncSession], reply: str, label: str
) -> None:
    c = build_convo(db, assent=FakeLabelModel(label))
    await c.turn("research it", propose(c.goal), text(c.readback()))
    await c.turn(reply, dispatch(), text("ok"))
    assert len(c.assent.calls) == 1
    assert not any(NOTE_AFFIRMATIVE in n for n in c.notes())
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()
    # The proposal is gone: a later "yes" cannot revive it.
    c.assent.answer = "affirmative"
    await c.turn("yes", dispatch(), text("ok"))
    await c.assert_nothing_dispatched()


@pytest.mark.parametrize(
    "assent",
    [
        FakeLabelModel(RuntimeError("ollama down")),
        FakeLabelModel("affirmative", delay=1.0),  # slower than the timeout below
        FakeLabelModel(as_text="affirmative, I think"),  # no tool call, no JSON
        FakeLabelModel(as_text='{"label": "affirmative"} {"label": "hedge"}'),  # ambiguous
    ],
    ids=["error", "timeout", "free-text", "ambiguous"],
)
async def test_classifier_failure_fails_safe(
    db: async_sessionmaker[AsyncSession], assent: FakeLabelModel
) -> None:
    hook = CommitmentHook(assent_model=assent, score_ready=False, assent_timeout_s=0.2)
    c = build_convo(db, assent=assent, hook=hook)
    await c.turn("research it", propose(c.goal), text(c.readback()))
    await c.turn("yes", dispatch(), text("ok"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_barge_in_that_cut_off_the_readback_prevents_dispatch(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    rb = c.readback()
    # ElevenLabs recorded only what was spoken before the user barged in.
    await c.turn("research it", propose(c.goal), text(rb), recorded=rb[: len(rb) - 12])
    await c.turn("yes", dispatch(), text("ok"))
    assert any(NOTE_INTERRUPTED in n for n in c.notes())
    assert c.assent.calls == []
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_paraphrased_readback_is_not_a_delivered_readback(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    # The model ignored "word for word" and never mentioned the exclusion or the artifact.
    await c.turn("research it", propose(c.goal), text(f"Want me to {c.goal}?"))
    await c.turn("yes", dispatch(), text("ok"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_readback_delivery_ignores_case_and_punctuation(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    rb = c.readback()
    await c.turn("research it", propose(c.goal), text(rb), recorded=rb.upper().replace(",", ""))
    await c.turn("yes", dispatch(), text("ok"))
    assert len(await c.commitments()) == 1


async def test_assent_two_user_turns_later_is_rejected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it", propose(c.goal), text(c.readback()))
    await c.turn("yes", text("Great."))  # agreed, but the model did not dispatch
    await c.turn("yes, go ahead", dispatch(), text("ok"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_repeated_dispatch_creates_exactly_one_commitment(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it", propose(c.goal), text(c.readback()))
    await c.turn("yes", dispatch(call_id="a"), dispatch(call_id="b"), text("ok"))
    assert c.tool_results()[1].startswith("Already started")
    await c.turn("yes, do it again", dispatch(), text("ok"))
    assert c.tool_results()[0].startswith("Already started")
    assert len(await c.commitments()) == 1
    assert await c.task_count() == 1
    assert len(c.client.enqueued) == 1


async def test_propose_while_assented_is_rejected_and_the_agreed_plan_dispatches(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    await c.turn("research it", propose(c.goal), text(c.readback()))
    # The user said yes to A; the model tries to swap in B instead of dispatching.
    await c.turn("yes", propose(f"write {c.tag} a poem", "rhymes"), dispatch(), text("ok"))
    first, second = c.tool_results()
    assert first.startswith("REJECTED") and "call dispatch_task" in first.lower()
    assert second.startswith("Starting")
    (row,) = await c.commitments()
    assert row.goal == c.goal  # what the user actually agreed to


async def test_newer_proposal_whose_readback_was_not_spoken_cannot_be_dispatched(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db)
    # Two proposals in one turn; only the first read-back is spoken.
    await c.turn(
        "research it",
        propose(c.goal),
        propose(f"write {c.tag} a poem", "rhymes"),
        text(c.readback()),
    )
    await c.turn("yes", dispatch(), text("ok"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


@pytest.mark.parametrize("artifact", ["email", "slack_message", "pull_request"])
async def test_out_of_scope_or_disabled_artifact_is_never_proposed(
    db: async_sessionmaker[AsyncSession], artifact: str
) -> None:
    c = build_convo(db)  # default ENABLED_KINDS = research only: pull_request (code) is off
    await c.turn("do it", propose(c.goal, artifact=artifact), text("I can't do that yet."))
    assert c.tool_results()[0].startswith("REJECTED")
    assert c.store.get(uuid.UUID(c.session_id)).pending_proposal is None
    await c.turn("yes", dispatch(), text("ok"))
    await c.assert_nothing_dispatched()


async def test_kind_disabled_between_proposal_and_dispatch_is_rejected(
    db: async_sessionmaker[AsyncSession],
) -> None:
    c = build_convo(db, enabled_kinds="research,code")
    await c.turn(
        "fix it",
        propose(c.goal, artifact="pull_request"),
        text(c.readback(artifact=ArtifactKind.PULL_REQUEST)),
    )
    c.app.state.service.settings = make_settings(enabled_kinds="research")
    await c.turn("yes", dispatch(), text("ok"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.assert_nothing_dispatched()


async def test_restart_drops_the_pending_proposal(db: async_sessionmaker[AsyncSession]) -> None:
    c = build_convo(db)
    await c.turn("research it", propose(c.goal), text(c.readback()))
    restarted = build_convo(db, store=SessionStore())  # fresh process state
    restarted.tag, restarted.session_id, restarted.history = c.tag, c.session_id, c.history
    await restarted.turn("yes", dispatch(), text("ok"))
    assert restarted.tool_results()[0].startswith("REJECTED")
    await restarted.assert_nothing_dispatched()


async def test_empty_scope_exclusion_is_not_proposed(db: async_sessionmaker[AsyncSession]) -> None:
    c = build_convo(db)
    await c.turn("research it", propose(c.goal, excludes="  "), text("What should I leave out?"))
    assert c.tool_results()[0].startswith("REJECTED")
    await c.turn("yes", dispatch(), text("ok"))
    await c.assert_nothing_dispatched()


# --- ready score -------------------------------------------------------------------------


async def test_ready_score_is_logged_after_the_response_and_never_proposes(
    db: async_sessionmaker[AsyncSession],
) -> None:
    ready = FakeLabelModel("ready_to_execute")
    c = build_convo(
        db, hook=CommitmentHook(assent_model=FakeLabelModel(), ready_model=ready, ready_idle_s=0)
    )
    chat_calls_when_scored: list[int] = []
    ready.on_call = lambda: chat_calls_when_scored.append(len(c.chat.calls))
    await c.turn(f"let's research {c.tag}, yes, go", text("Sounds good."))
    assert chat_calls_when_scored == [1]  # scored after the main model, not before it
    async with db() as s:
        turn_id = await s.scalar(select(Turn.id).where(Turn.text.contains(c.tag)))
        rows = list(
            await s.scalars(select(RouterDecision).where(RouterDecision.turn_id == turn_id))
        )
    assert [(r.backend, r.ready, r.is_active) for r in rows] == [
        ("frontier", "ready_to_execute", False)
    ]
    assert rows[0].latency_ms is not None
    assert c.store.get(uuid.UUID(c.session_id)).pending_proposal is None
    await c.assert_nothing_dispatched()


async def test_ready_score_failure_is_logged_not_raised(
    db: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    ready = FakeLabelModel(RuntimeError("boom"))
    c = build_convo(
        db, hook=CommitmentHook(assent_model=FakeLabelModel(), ready_model=ready, ready_idle_s=0)
    )
    caplog.set_level(logging.WARNING)
    spoken = await c.turn(f"idea {c.tag}", text("Tell me more."))
    assert spoken == "Tell me more."
    assert any(
        "ready_score" in r.getMessage() or "answer_ready" in r.getMessage() for r in caplog.records
    )


# --- executor work is delegated, not refused (owner rule, 2026-09-26) ---------------------


class PromptFollowingModel(ScriptedChatModel):
    """A stub that does what the rendered scope prompt says about a local-machine request.

    If the prompt hands "inspect this computer" work to the executor and forbids refusing it,
    it proposes the user's request verbatim; otherwise it refuses the way the demo model did.
    After a tool result it speaks the read-back / confirmation the tool told it to say.
    """

    def __init__(self, goal: str) -> None:
        super().__init__([])
        self.goal = goal

    async def stream(  # type: ignore[override]
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, **kw: Any
    ) -> AsyncIterator[ChatDelta]:
        self.calls.append({"messages": copy.deepcopy(messages), "tools": tools, **kw})
        last = messages[-1]
        system = " ".join(
            " ".join(str(m["content"]).split()) for m in messages if m["role"] == "system"
        )
        if last["role"] == "tool":
            said = re.search(r'"([^"]+)"', str(last["content"]))
            script = text(said.group(1) if said else "Okay.")
        elif str(last["content"]).lower().startswith("yes"):
            script = dispatch()
        elif "inspect this computer" in system and "Never answer that you can't do it" in system:
            script = propose(self.goal, excludes="changing any settings")
        else:
            script = text(
                "I can't do that yet, I can't check your hardware system settings, though that "
                "might come in a future version."
            )
        for delta in script:
            yield delta


async def test_hardware_check_is_read_back_and_dispatched_only_on_yes(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """The demo answered "check my hardware system settings" with "I can't do that yet ...".
    Through the real app, a model following the prompt must propose it (read-back), and only
    the explicit yes dispatches it as a research task with the request as its goal."""
    c = build_convo(db, enabled_kinds=["research"])
    goal = f"check my {c.tag} hardware system settings"
    c.chat = PromptFollowingModel(goal)
    c.app.state.service.chat_model = c.chat
    readback = c.readback(goal, "changing any settings")

    reply = await c.turn(f"Can you check my {c.tag} hardware system settings?")
    assert "can't do that yet" not in reply.lower() and "future version" not in reply
    assert reply == readback
    await c.assert_nothing_dispatched()

    await c.turn("Yes, go ahead.")
    (row,) = await c.commitments()
    assert (row.goal, row.artifact_kind) == (goal, "document")
    assert row.assent_utterance == "Yes, go ahead."
    assert await c.task_count() == 1 and len(c.client.enqueued) == 1
