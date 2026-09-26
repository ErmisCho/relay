"""Session identity when ElevenLabs sends no session id."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import ToolRegistry, TurnContext

from .conftest import ScriptedChatModel, load_request, make_settings, post, text


class SessionHook:
    def __init__(self) -> None:
        self.sessions: list[uuid.UUID] = []

    async def before_model(self, ctx: TurnContext) -> list[str]:
        self.sessions.append(ctx.state.session_id)
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None


def _anonymous(messages: list[dict[str, Any]]) -> dict[str, Any]:
    body = load_request()
    body.pop("elevenlabs_extra_body")
    body["messages"] = messages
    return body


async def test_requests_without_session_id_share_a_session_per_conversation(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    hook = SessionHook()
    app = create_app(
        make_settings(),
        chat_model=ScriptedChatModel([text("a"), text("b"), text("c")]),
        registry=ToolRegistry(),
        hooks=[hook],
        sessionmaker=dead_db,
    )
    system = {"role": "system", "content": "You are relay."}
    greeting = {"role": "assistant", "content": "Hey, what's on your mind?"}
    first = [system, greeting, {"role": "user", "content": "A bike lock idea."}]
    # Next turn: same opening plus the reply and a new user message.
    second = [*first, {"role": "assistant", "content": "Tell me more."},
              {"role": "user", "content": "Phone unlock."}]
    other = [system, greeting, {"role": "user", "content": "Something else entirely."}]
    for messages in (first, second, other):
        assert (await post(app, _anonymous(messages))).status_code == 200

    assert hook.sessions[0] == hook.sessions[1]
    assert hook.sessions[2] != hook.sessions[0]
    # A proposal made on turn 1 is still visible on turn 2: same state object.
    assert app.state.service.session_store.get(hook.sessions[0]).user_turn_index == 2
