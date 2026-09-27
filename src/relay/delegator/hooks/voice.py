"""Voice-behaviour rules for every conversation, via the cached static prompt prefix.

A user message of "..." is ElevenLabs re-engaging after the user went quiet.
Without these rules the model re-read its previous answer aloud, or hung up via
``end_call`` (measured 3/5 quiet turns before, 0/8 after, gpt-6-luna, 2026-09-27).
"""

from __future__ import annotations

from relay.config import Settings
from relay.delegator.contracts import TurnContext

VOICE_RULES = (
    "Voice rules: reply in plain spoken sentences, no markdown or lists. Never repeat "
    "something you already said earlier in this conversation unless the user asks for "
    "it again. If the user's message is only '...', they went quiet: reply with one "
    "short nudge such as 'Still there?' and nothing else - never end the call for that. "
    "Only end the call when the user clearly says goodbye."
)


class VoiceRulesHook:
    """``TurnHook`` + ``SystemPrefixProvider`` contributing only static voice rules."""

    async def before_model(self, ctx: TurnContext) -> list[str]:
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None

    def system_prefix(self, settings: Settings) -> str:
        return VOICE_RULES
