"""A caller that hangs up mid-answer (barge-in) must still get its turn persisted."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator import persistence
from relay.delegator.app import create_app
from relay.delegator.contracts import ToolRegistry, TurnContext
from relay.delegator.llm import ChatDelta

from .conftest import SECRET, ScriptedChatModel, load_request, make_settings


class StallsAfterTwoDeltas(ScriptedChatModel):
    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatDelta]:
        yield ChatDelta(content="Hello ")
        yield ChatDelta(content="there")
        await asyncio.Event().wait()  # the user barges in while the model keeps going


class AfterHook:
    def __init__(self) -> None:
        self.after: list[str] = []

    async def before_model(self, ctx: TurnContext) -> list[str]:
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        self.after.append(assistant_text)


async def test_disconnect_mid_stream_persists_partial_turn(
    monkeypatch: pytest.MonkeyPatch, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_record_turn(db: object, **kwargs: Any) -> uuid.UUID:
        recorded.append(kwargs)
        return uuid.uuid4()

    monkeypatch.setattr(persistence, "record_turn", fake_record_turn)
    hook = AfterHook()
    app = create_app(
        make_settings(),
        chat_model=StallsAfterTwoDeltas([]),
        registry=ToolRegistry(),
        hooks=[hook],
        sessionmaker=dead_db,
    )
    payload = json.dumps(load_request()).encode()
    # uvicorn reports ASGI spec 2.3, which makes Starlette watch receive() for a disconnect.
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"authorization", f"Bearer {SECRET}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    request_sent = False
    hung_up = asyncio.Event()
    body = bytearray()

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await hung_up.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if b'"there"' in body:
                hung_up.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    await asyncio.wait_for(app.state.service.drain(), timeout=5)

    assert b"[DONE]" not in body
    (assistant,) = [r for r in recorded if r["role"] == "assistant"]
    assert assistant["text"] == "Hello there"
    assert assistant["meta"] == {"interrupted": True}
    assert hook.after == ["Hello there"]
