"""Voice behaviour: static rules in the cached prompt prefix, plus a cut-off-reply note.

A user message of "..." is ElevenLabs re-engaging after the user went quiet. Without the rules
the model re-read its previous answer aloud, or hung up via ``end_call`` (measured 3/5 quiet
turns before, 0/8 after, gpt-6-luna, 2026-09-27).

Speech gets split: "what models can my PC run? ... oh, and the weather?" arrives as two turns,
and the second cuts off the reply to the first. The model then answered only the latest
message (5/5 dropped the first question). So the reply this hook saw generated is compared
with what ElevenLabs recorded as spoken; when it was cut off, the user's earlier message is
handed back as still unanswered.
"""

from __future__ import annotations

from typing import Any

from relay.config import Settings
from relay.delegator.adapters.openai_compat import content_text
from relay.delegator.commitment.protocol import RESERVATION_KEY
from relay.delegator.commitment.readback import normalise
from relay.delegator.contracts import TurnContext

VOICE_RULES = (
    "Voice rules: reply in plain spoken sentences, no markdown or lists. Never repeat "
    "something you already said earlier in this conversation unless the user asks for "
    "it again. If the user's message is only '...', they went quiet: reply with one "
    "short nudge such as 'Still there?' and nothing else - never end the call for that. "
    "Only end the call when the user clearly says goodbye."
)
LAST_REPLY_KEY = "voice.last_reply"


def cut_off_question(messages: list[dict[str, Any]], generated: str) -> str | None:
    """The user's message(s) before a reply that was cut off, or None.

    ``messages`` ends with the latest user message(s); the assistant message before them is
    what ElevenLabs recorded as spoken of ``generated``.
    """
    i = len(messages) - 1
    while i >= 0 and messages[i].get("role") == "user":
        i -= 1
    if i < 0 or messages[i].get("role") != "assistant":
        return None
    spoken = normalise(content_text(messages[i].get("content")))
    if not normalise(generated) or spoken == normalise(generated):
        return None
    earlier: list[str] = []
    i -= 1
    while i >= 0 and messages[i].get("role") == "user":
        earlier.append(content_text(messages[i].get("content")).strip())
        i -= 1
    text = " ".join(reversed([e for e in earlier if e and e != "..."]))
    return text or None


class VoiceRulesHook:
    """``TurnHook`` + ``SystemPrefixProvider``: static voice rules and the cut-off note."""

    async def before_model(self, ctx: TurnContext) -> list[str]:
        generated = ctx.state.extra.get(LAST_REPLY_KEY)
        if not isinstance(generated, str):
            return []
        question = cut_off_question(ctx.messages, generated)
        if question is None:
            return []
        started = ctx.state.extra.get(RESERVATION_KEY)
        if (
            getattr(started, "status", "cancelled") not in ("cancelled", "failed")
            and getattr(started, "user_turn_index", None) == ctx.state.user_turn_index - 1
        ):
            # The cut-off reply was the "On it" of a direct start: that question IS handled.
            # Handing it back re-proposed it and dropped the latest message (3/4 live runs).
            goal = started.proposal.goal  # type: ignore[union-attr]
            return [
                "The user cut off your previous reply, but the work for their earlier message is "
                f'already starting ("{goal}"). Do not propose it again; handle only their '
                "latest message."
            ]
        return [
            "The user cut off your previous reply, so this earlier message of theirs is still "
            f'unanswered: "{question}". Handle it together with their latest message: put all '
            "executor work from both into one propose_commitment, and answer the rest in its "
            "answer_first. Drop neither."
        ]

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        ctx.state.extra[LAST_REPLY_KEY] = assistant_text

    def system_prefix(self, settings: Settings) -> str:
        return VOICE_RULES
