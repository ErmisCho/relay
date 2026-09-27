"""Opt-in live TTFT measurement against the configured local model (RELAY_LLM_TESTS=1)."""

from __future__ import annotations

import logging
import os
import statistics
import time
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import ToolContext, ToolRegistry, ToolResult
from relay.delegator.tools.hardware import HardwareCapabilitiesTool, chip_name, total_memory_bytes

from .conftest import load_request, make_settings, parse_sse, post

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1", reason="set RELAY_LLM_TESTS=1 to hit a live model"
)


async def test_live_ttft_p50(
    caplog: pytest.LogCaptureFixture, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    settings = make_settings(
        delegator_model="ollama:gemma4:e4b", delegator_fallback_model="ollama:gemma4:e4b"
    )
    app = create_app(settings, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db)
    caplog.set_level(logging.INFO, logger="relay.delegator.service")
    ttfts: list[float] = []
    for _ in range(10):
        caplog.clear()
        chunks = parse_sse((await post(app, load_request())).content)
        assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks).strip()
        (record,) = [r for r in caplog.records if r.getMessage().startswith("delegator turn")]
        ttft = record.args[1]  # type: ignore[index]
        assert isinstance(ttft, int)
        ttfts.append(ttft)
    p50 = statistics.median(ttfts)
    print(f"\nTTFT ms over 10 requests: {sorted(ttfts)}; p50={p50}")
    assert p50 < 1000  # DoD#2


async def test_live_hardware_question_calls_tool_and_reports_observed_specs(
    monkeypatch: pytest.MonkeyPatch, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    """Use local Ollama and the real hardware probe; no paid API or scripted tool selection."""
    settings = make_settings(
        delegator_model="ollama:gemma4:e4b", delegator_fallback_model="ollama:gemma4:e4b"
    )
    observed_results: list[str] = []
    original = HardwareCapabilitiesTool.__call__

    async def observed_call(
        self: HardwareCapabilitiesTool, args: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:
        result = await original(self, args, ctx)
        observed_results.append(result.content)
        return result

    monkeypatch.setattr(HardwareCapabilitiesTool, "__call__", observed_call)
    app = create_app(settings, hooks=[], sessionmaker=dead_db, warm_dbos=False)
    body = {
        "messages": [
            {"role": "system", "content": "Keep answers brief. Use digits for quantities."},
            {"role": "user", "content": "What are this PC's specs? Give the chip, GPU and RAM."},
        ],
        "stream": True,
        "temperature": 0,
        "max_tokens": 300,
        "tool_choice": "auto",
    }
    started = time.perf_counter()
    response = await post(app, body)
    chunks = parse_sse(response.content)
    spoken = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    memory = total_memory_bytes()
    assert response.status_code == 200 and observed_results
    assert memory is not None and f"{memory / 1024**3:.0f}" in spoken
    assert chip_name().lower() in spoken.lower()
    print(f"\nTool: {observed_results[-1]}\nAnswer: {spoken}")
    print(f"Whole local text/tool turn: {time.perf_counter() - started:.2f}s")
