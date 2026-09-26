"""Opt-in live TTFT measurement against the configured local model (RELAY_LLM_TESTS=1)."""

from __future__ import annotations

import logging
import os
import statistics

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import ToolRegistry

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
