"""Upstream outages degrade instead of dropping the call (SPEC section 10)."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import ToolRegistry
from relay.delegator.llm import ChatDelta, FallbackChatModel
from relay.delegator.llm.fallback import FIRST_DELTA_TIMEOUT_S, SAME_MODEL_FIRST_DELTA_TIMEOUT_S
from relay.delegator.service import APOLOGY_TEXT

from .conftest import ScriptedChatModel, load_request, make_settings, parse_sse, post, text


async def _drain(model: FallbackChatModel) -> list[ChatDelta]:
    return [d async for d in model.stream([{"role": "user", "content": "hi"}], None)]


async def test_fallback_serves_when_primary_fails_before_first_delta() -> None:
    primary = ScriptedChatModel([ConnectionError("ollama down")], name="ollama:primary")
    backup = ScriptedChatModel([text("Still here.")], name="ollama:backup")
    deltas = await _drain(FallbackChatModel(primary, backup))
    assert "".join(d.content or "" for d in deltas) == "Still here."
    assert {d.model for d in deltas} == {"ollama:backup"}


async def test_hung_primary_falls_back_within_first_delta_budget() -> None:
    class Hung(ScriptedChatModel):
        async def stream(self, *args, **kwargs):  # type: ignore[no-untyped-def,override]
            await asyncio.Event().wait()  # accepted the request, never answers
            yield ChatDelta(content="never")

    backup = ScriptedChatModel([text("Still here.")], name="ollama:backup")
    model = FallbackChatModel(Hung([], name="ollama:hung"), backup, first_delta_timeout=0.2)
    started = time.perf_counter()
    deltas = await asyncio.wait_for(_drain(model), timeout=5)
    assert time.perf_counter() - started < 1.0
    assert "".join(d.content or "" for d in deltas) == "Still here."
    assert {d.model for d in deltas} == {"ollama:backup"}


async def test_failure_after_first_delta_propagates() -> None:
    class Flaky(ScriptedChatModel):
        async def stream(self, *args, **kwargs):  # type: ignore[no-untyped-def,override]
            yield ChatDelta(content="Half a sen")
            raise ConnectionError("dropped")

    backup = ScriptedChatModel([text("unrelated")], name="ollama:backup")
    with pytest.raises(ConnectionError):
        await _drain(FallbackChatModel(Flaky([]), backup))
    assert backup.calls == []


async def test_total_upstream_failure_speaks_an_apology_not_a_500(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    model = FallbackChatModel(
        ScriptedChatModel([RuntimeError("a")]), ScriptedChatModel([RuntimeError("b")])
    )
    app = create_app(
        make_settings(), chat_model=model, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db
    )
    resp = await post(app, load_request())
    assert resp.status_code == 200
    chunks = parse_sse(resp.content)
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == APOLOGY_TEXT
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_first_delta_deadline_depends_on_whether_fallback_is_a_different_model() -> None:
    # Same model: a cold load must not be abandoned at 5 s only to restart the same model.
    same = FallbackChatModel(ScriptedChatModel([], name="ollama:g"), ScriptedChatModel(
        [], name="ollama:g"))
    other = FallbackChatModel(ScriptedChatModel([], name="anthropic:c"), ScriptedChatModel(
        [], name="ollama:g"))
    explicit = FallbackChatModel(ScriptedChatModel([], name="ollama:g"), ScriptedChatModel(
        [], name="ollama:g"), first_delta_timeout=0.5)
    assert same.first_delta_timeout == SAME_MODEL_FIRST_DELTA_TIMEOUT_S
    assert other.first_delta_timeout == FIRST_DELTA_TIMEOUT_S
    assert explicit.first_delta_timeout == 0.5
