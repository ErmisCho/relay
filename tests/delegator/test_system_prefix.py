"""Static hook instructions must sit in a stable prompt prefix so the local model's
prompt cache is reused; per-turn notes go next to the latest user message."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.app import create_app
from relay.delegator.contracts import ToolRegistry, TurnContext

from .conftest import ScriptedChatModel, make_settings, post, text

RULES = "STATIC RULES"


class PrefixHook:
    def system_prefix(self, settings: Settings) -> str:
        return RULES

    async def before_model(self, ctx: TurnContext) -> list[str]:
        return [f"note for turn {ctx.state.user_turn_index}"]

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None


def _body(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": "x",
        "stream": True,
        "messages": messages,
        "elevenlabs_extra_body": {"session_id": "prefix-session"},
    }


async def test_static_prefix_is_stable_and_notes_trail(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    model = ScriptedChatModel([text("a"), text("b")])
    app = create_app(
        make_settings(),
        chat_model=model,
        registry=ToolRegistry(),
        hooks=[PrefixHook()],
        sessionmaker=dead_db,
    )
    system = {"role": "system", "content": "agent prompt"}
    turn1 = [system, {"role": "user", "content": "hi"}]
    turn2 = [*turn1, {"role": "assistant", "content": "a"}, {"role": "user", "content": "more"}]
    await post(app, _body(turn1))
    await post(app, _body(turn2))

    first, second = (call["messages"] for call in model.calls)
    # Rules right after the conversation's own system prompt, on every turn.
    assert first[:2] == second[:2] == [system, {"role": "system", "content": RULES}]
    # Turn 1's full upstream prompt, minus its trailing per-turn note, is an exact
    # prefix of turn 2's: nothing before the new tail changed, so the cache applies.
    note1 = {"role": "system", "content": "note for turn 1"}
    assert first[-2:] == [note1, {"role": "user", "content": "hi"}]
    assert second[:3] == [system, {"role": "system", "content": RULES}, turn1[1]]
    assert second[-2:] == [{"role": "system", "content": "note for turn 2"}, turn2[-1]]
    assert sum(m["content"] == RULES for m in second) == 1


def test_production_hooks_carry_the_voice_rules() -> None:
    """Without this hook the agent re-reads old answers or hangs up when the user goes quiet."""
    from relay.delegator.hooks.voice import VOICE_RULES
    from relay.delegator.wiring import build_hooks

    settings = make_settings()
    prefixes = [h.system_prefix(settings) for h in build_hooks() if hasattr(h, "system_prefix")]
    assert VOICE_RULES in prefixes
