"""Opt-in live TTFT measurement against the configured local model (RELAY_LLM_TESTS=1)."""

from __future__ import annotations

import logging
import os
import statistics
import time
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings, parse_model_ref
from relay.delegator.app import create_app
from relay.delegator.contracts import ToolContext, ToolRegistry, ToolResult
from relay.delegator.tools.hardware import HardwareCapabilitiesTool, chip_name, total_memory_bytes

from .conftest import SECRET, load_request, make_settings, parse_sse, post

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
    """Use .env's real models and the real hardware probe; no scripted tool selection.

    Run on the machine hosting Relay/Ollama. This isolates the text/tool loop from
    ElevenLabs audio, DBOS and database availability; it is not a voice benchmark.
    """
    settings = Settings(delegator_shared_secret=SECRET)
    assert all(
        parse_model_ref(ref)[0] == "ollama"
        for ref in (settings.delegator_model, settings.delegator_fallback_model)
    ), "This opt-in smoke check is for local Ollama models only."
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
            {"role": "user", "content": "What are this PC's specs? Give the chip and RAM."},
        ],
        "stream": True,
        "temperature": 0,
        "max_tokens": 300,
        "tool_choice": "auto",
    }
    started = time.perf_counter()
    response = await post(app, body)
    assert response.status_code == 200
    chunks = parse_sse(response.content)
    spoken = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert observed_results, f"The real model did not call hardware_capabilities: {spoken!r}"
    memory = total_memory_bytes()
    assert memory is not None, f"Hardware probe could not read RAM: {observed_results}"
    assert f"{memory / 1024**3:.0f}" in spoken, f"RAM missing or incorrect: {spoken!r}"
    assert chip_name().lower() in spoken.lower(), f"Chip missing or incorrect: {spoken!r}"
    print(f"\nModel: {settings.delegator_model}")
    print(f"Tool: {observed_results[-1]}")
    print(f"Answer: {spoken}")
    print(f"Whole text/tool turn: {time.perf_counter() - started:.2f}s (not speech latency)")
