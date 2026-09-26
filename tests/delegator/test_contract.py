"""Wire contract with ElevenLabs' custom-LLM client: exact SSE framing, auth, unknown keys."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.auth import ALLOW_DEV_SECRET_ENV, DEV_SECRET
from relay.delegator.contracts import ToolRegistry

from .conftest import ScriptedChatModel, load_request, make_settings, parse_sse, post, text


async def test_recorded_request_streams_exact_openai_sse(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    model = ScriptedChatModel([text("Oh, ", "a proximity unlock — ", "who's it for?")])
    app = create_app(
        make_settings(), chat_model=model, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db
    )
    resp = await post(app, load_request())

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"

    chunks = parse_sse(resp.content)
    for chunk in chunks:
        assert set(chunk) == {"id", "object", "created", "model", "choices"}
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["id"] == chunks[0]["id"] and chunk["id"].startswith("chatcmpl-")
        assert isinstance(chunk["created"], int)
        assert chunk["model"] == "relay-delegator"
        (choice,) = chunk["choices"]
        assert choice["index"] == 0 and isinstance(choice["delta"], dict)
        assert "finish_reason" in choice
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    spoken = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert spoken == "Oh, a proximity unlock — who's it for?"
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None, None, None, "stop"]
    # Request history and ElevenLabs' system tool reach the upstream model.
    (call,) = model.calls
    assert call["messages"] == load_request()["messages"]
    assert [t["function"]["name"] for t in call["tools"]] == ["end_call"]
    assert call["temperature"] == 0.5 and call["max_tokens"] == 300
    assert call["tool_choice"] == "auto"


async def test_non_streaming_request_returns_chat_completion(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        make_settings(),
        chat_model=ScriptedChatModel([text("Hi ", "there.")]),
        registry=ToolRegistry(),
        hooks=[],
        sessionmaker=dead_db,
    )
    resp = await post(app, {**load_request(), "stream": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Hi there."}
    assert body["choices"][0]["finish_reason"] == "stop"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-secret"}])
async def test_rejects_missing_or_wrong_bearer(
    headers: dict[str, str], dead_db: async_sessionmaker[AsyncSession]
) -> None:
    model = ScriptedChatModel([])
    app = create_app(
        make_settings(), chat_model=model, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db
    )
    resp = await post(app, load_request(), headers=headers)
    assert resp.status_code == 401
    assert model.calls == []


def test_refuses_dev_secret_unless_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    # Localhost public URL is the usual tunnel setup, so it must not unlock the placeholder.
    dev = make_settings(delegator_shared_secret=DEV_SECRET)
    monkeypatch.delenv(ALLOW_DEV_SECRET_ENV, raising=False)
    with pytest.raises(RuntimeError, match="dev placeholder"):
        create_app(dev, chat_model=ScriptedChatModel([]), sessionmaker=dead_db)
    monkeypatch.setenv(ALLOW_DEV_SECRET_ENV, "1")
    create_app(dev, chat_model=ScriptedChatModel([]), sessionmaker=dead_db)


@pytest.mark.parametrize("warm", [True, False])
async def test_startup_warms_dbos_client_unless_disabled(
    warm: bool, monkeypatch: pytest.MonkeyPatch, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    calls: list[object] = []

    async def fake_warm(settings: object) -> bool:
        calls.append(settings)
        return False  # DBOS unreachable must not block startup

    monkeypatch.setattr("relay.delegator.app.warm_dbos_client", fake_warm)
    settings = make_settings()
    app = create_app(
        settings, chat_model=ScriptedChatModel([]), sessionmaker=dead_db, warm_dbos=warm
    )
    async with app.router.lifespan_context(app):
        pass
    assert calls == ([settings] if warm else [])
