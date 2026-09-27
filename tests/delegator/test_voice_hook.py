"""VoiceRulesHook: a reply the user cut off leaves their earlier question unanswered."""

from __future__ import annotations

import uuid
from typing import Any

from relay.delegator.contracts import SessionState, TurnContext
from relay.delegator.hooks.voice import VoiceRulesHook, cut_off_question

from .conftest import make_settings

Q1 = "I want to run local LLMs on my PC, what models does my system support?"
Q2 = "By the way, what is the weather?"
GENERATED = "It depends mainly on your memory and graphics card."


def history(spoken: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "prompt"},
        {"role": "assistant", "content": "Hey, I'm listening."},
        {"role": "user", "content": Q1},
        {"role": "assistant", "content": spoken},
        {"role": "user", "content": Q2},
    ]


def test_cut_off_reply_hands_back_the_earlier_question() -> None:
    # Live bug: speech split into two turns, the second cut off the reply to the first, and
    # the model answered only the latest (5/5 dropped the first question).
    assert cut_off_question(history("It depends..."), GENERATED) == Q1
    assert cut_off_question(history(""), GENERATED) == Q1


def test_fully_spoken_reply_is_not_treated_as_cut_off() -> None:
    assert cut_off_question(history(GENERATED), GENERATED) is None
    assert cut_off_question(history(GENERATED.upper().rstrip(".")), GENERATED) is None


async def test_hook_notes_the_unanswered_question_only_after_a_cut_off() -> None:
    hook = VoiceRulesHook()
    state = SessionState(session_id=uuid.uuid4())

    def ctx(messages: list[dict[str, Any]]) -> TurnContext:
        return TurnContext(
            state=state, db=None, settings=make_settings(), messages=messages,  # type: ignore[arg-type]
            user_text=Q2, user_turn_id=None,
        )  # fmt: skip

    assert await hook.before_model(ctx(history("It depends..."))) == []  # nothing generated yet
    await hook.after_response(ctx(history("")), GENERATED)
    (note,) = await hook.before_model(ctx(history("It depends...")))
    assert Q1 in note and "propose_commitment" in note
    assert await hook.before_model(ctx(history(GENERATED))) == []
