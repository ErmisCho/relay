"""Turns, hooks and model attribution against the real (migrated) test database."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator import persistence
from relay.delegator.app import create_app
from relay.delegator.contracts import ToolRegistry, TurnContext
from relay.delegator.llm import ChatModel, FallbackChatModel
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Session, Turn

from .conftest import AUTH, ScriptedChatModel, load_request, make_settings, post, text


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


@dataclass
class RecordingHook:
    before: list[TurnContext] = field(default_factory=list)
    after: list[tuple[TurnContext, str]] = field(default_factory=list)

    async def before_model(self, ctx: TurnContext) -> list[str]:
        self.before.append(ctx)
        return ["NOTE: the user already has a prototype."]

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        self.after.append((ctx, assistant_text))


async def _run(
    db: async_sessionmaker[AsyncSession], model: ChatModel, raw_session_id: str
) -> tuple[RecordingHook, uuid.UUID, list[Turn]]:
    hook = RecordingHook()
    app = create_app(
        make_settings(), chat_model=model, registry=ToolRegistry(), hooks=[hook], sessionmaker=db
    )
    body = load_request()
    body["elevenlabs_extra_body"]["session_id"] = raw_session_id
    resp = await post(app, body)
    assert resp.status_code == 200
    try:
        session_id = uuid.UUID(raw_session_id)
    except ValueError:
        session_id = uuid.uuid5(uuid.NAMESPACE_URL, "relay-session:" + raw_session_id)
    async with db() as s:
        assert await s.get(Session, session_id) is not None
        turns = (
            await s.scalars(select(Turn).where(Turn.session_id == session_id).order_by(Turn.ts))
        ).all()
    return hook, session_id, list(turns)


async def test_turns_persisted_and_hooks_run(db: async_sessionmaker[AsyncSession]) -> None:
    model = ScriptedChatModel([text("Who ", "rides it?")], name="ollama:gemma4:e4b")
    hook, session_id, turns = await _run(db, model, f"not-a-uuid-{uuid.uuid4().hex}")

    user, assistant = sorted(turns, key=lambda t: t.role != "user")
    assert (user.role, user.text, user.route, user.model_used) == (
        "user", "It unlocks when my phone is nearby.", None, None,
    )
    assert (assistant.text, assistant.route, assistant.model_used) == (
        "Who rides it?", "frontier", "ollama:gemma4:e4b",
    )
    assert assistant.latency_ms is not None and assistant.latency_ms >= 0
    # Hook notes land as a system message right before the latest user message.
    sent = model.calls[0]["messages"]
    assert sent[-2] == {"role": "system", "content": "NOTE: the user already has a prototype."}
    assert sent[-1]["content"] == "It unlocks when my phone is nearby."
    (ctx,) = hook.before
    assert ctx.user_turn_id == user.id and ctx.state.session_id == session_id
    assert ctx.user_text == user.text
    assert [(c, t) for c, t in hook.after] == [(ctx, "Who rides it?")]


async def test_fallback_model_is_recorded(db: async_sessionmaker[AsyncSession]) -> None:
    model = FallbackChatModel(
        ScriptedChatModel([ConnectionError("down")], name="anthropic:primary"),
        ScriptedChatModel([text("Backup here.")], name="ollama:gemma4:e4b"),
    )
    _, _, turns = await _run(db, model, str(uuid.uuid4()))
    (assistant,) = [t for t in turns if t.role == "assistant"]
    assert assistant.model_used == "ollama:gemma4:e4b"
    assert assistant.text == "Backup here."


async def test_next_request_waits_for_previous_turn_finalisation(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    original = persistence.record_turn

    async def slow_record_turn(sm: async_sessionmaker[AsyncSession], **kw: Any) -> uuid.UUID:
        if kw["role"] == "assistant":
            await asyncio.sleep(0.3)  # slow assistant write, as the reviewer forced it
        return await original(sm, **kw)

    class OrderHook:
        async def before_model(self, ctx: TurnContext) -> list[str]:
            events.append(f"before:{ctx.user_text}")
            return []

        async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
            events.append(f"after:{assistant_text}")

    monkeypatch.setattr(persistence, "record_turn", slow_record_turn)
    app = create_app(
        make_settings(),
        chat_model=ScriptedChatModel([text("A1"), text("A2")]),
        registry=ToolRegistry(),
        hooks=[OrderHook()],
        sessionmaker=db,
    )
    session_id = uuid.uuid4()
    first = [{"role": "system", "content": "sys"}, {"role": "user", "content": "U1"}]
    second = [*first, {"role": "assistant", "content": "A1"}, {"role": "user", "content": "U2"}]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for messages in (first, second):  # the next request follows immediately
            body = {**load_request(), "messages": messages,
                    "elevenlabs_extra_body": {"session_id": str(session_id)}}
            resp = await client.post("/v1/chat/completions", json=body, headers=AUTH)
            assert resp.status_code == 200
    await app.state.service.drain()

    assert events == ["before:U1", "after:A1", "before:U2", "after:A2"]
    async with db() as s:
        rows = (
            await s.scalars(select(Turn).where(Turn.session_id == session_id).order_by(Turn.ts))
        ).all()
    assert [t.text for t in rows] == ["U1", "A1", "U2", "A2"]
