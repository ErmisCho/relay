"""Regressions for silent or stalled voice turns on the real HTTP/tool-loop path."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator import persistence, service
from relay.delegator.app import create_app
from relay.delegator.contracts import ToolRegistry, TurnContext
from relay.delegator.llm import ChatDelta, FallbackChatModel
from relay.delegator.service import APOLOGY_TEXT
from relay.delegator.wiring import build_registry

from .conftest import ScriptedChatModel, load_request, make_settings, parse_sse, post, text
from .test_tools import tool_call


@pytest.mark.parametrize("stalled_component", ["persistence", "reports"])
async def test_stalled_context_io_cannot_block_speech(
    stalled_component: str,
    monkeypatch: pytest.MonkeyPatch,
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(service, "CONTEXT_IO_TIMEOUT_S", 0.01, raising=False)

    async def stalled_write(*args: Any, **kwargs: Any) -> None:
        await asyncio.Event().wait()

    class StalledReports:
        async def before_model(self, ctx: TurnContext) -> list[str]:
            await asyncio.Event().wait()
            return []

        async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
            pass

    if stalled_component == "persistence":
        monkeypatch.setattr(persistence, "record_turn", stalled_write)
    app = create_app(
        make_settings(),
        chat_model=ScriptedChatModel([text("I can still answer.")]),
        registry=ToolRegistry(),
        hooks=[StalledReports()] if stalled_component == "reports" else [],
        sessionmaker=dead_db,
        warm_dbos=False,
    )
    response = await asyncio.wait_for(post(app, load_request()), timeout=0.5)
    assert b"I can still answer." in response.content


@pytest.mark.parametrize("empty_stream", [[], [ChatDelta(finish_reason="length")]])
@pytest.mark.parametrize("fallback_succeeds", [True, False])
async def test_empty_model_output_tries_fallback_and_never_returns_silence(
    empty_stream: list[ChatDelta],
    fallback_succeeds: bool,
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    primary = ScriptedChatModel([empty_stream], name="ollama:primary")
    fallback = ScriptedChatModel(
        [text("Recovered.") if fallback_succeeds else empty_stream], name="ollama:backup"
    )
    app = create_app(
        make_settings(),
        chat_model=FallbackChatModel(primary, fallback),
        registry=ToolRegistry(),
        hooks=[],
        sessionmaker=dead_db,
        warm_dbos=False,
    )
    chunks = parse_sse((await post(app, load_request())).content)
    spoken = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert spoken == ("Recovered." if fallback_succeeds else APOLOGY_TEXT)
    assert len(fallback.calls) == 1


@pytest.mark.parametrize("followup", [[], RuntimeError("upstream failed after tool")])
async def test_empty_hardware_followup_does_not_end_with_only_a_preamble(
    followup: list[ChatDelta] | Exception, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    model = ScriptedChatModel(
        [
            [ChatDelta(content="I'll check. "), *tool_call("hardware_capabilities", "{}")],
            followup,
        ]
    )
    app = create_app(
        make_settings(),
        chat_model=model,
        registry=build_registry(),
        hooks=[],
        sessionmaker=dead_db,
        warm_dbos=False,
    )
    chunks = parse_sse((await post(app, load_request())).content)
    spoken = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert spoken == "I'll check. " + APOLOGY_TEXT
