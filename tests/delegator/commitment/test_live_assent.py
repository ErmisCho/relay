"""Opt-in live eval of the real assent classifier and its TTFT cost (RELAY_LLM_TESTS=1)."""

from __future__ import annotations

import logging
import os
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.commitment import AssentLabel, CommitmentHook, classify_assent
from relay.delegator.commitment.classify import (
    ASSENT_SYSTEM_PROMPT,
    assent_user_prompt,
    structured_label,
)
from relay.delegator.contracts import PendingProposal, SessionStore, ToolRegistry
from relay.delegator.llm.factory import build_model

from ..conftest import make_settings, post
from .utterances import LABELLED, READBACK

pytestmark = pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1", reason="set RELAY_LLM_TESTS=1 to hit a live model"
)


def _precision_recall(pairs: list[tuple[str, str]]) -> tuple[float, float, int]:
    tp = sum(1 for want, got in pairs if want == got == "affirmative")
    fp = sum(1 for want, got in pairs if got == "affirmative" and want != "affirmative")
    fn = sum(1 for want, got in pairs if want == "affirmative" and got != "affirmative")
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return precision, recall, fp


async def test_live_assent_classifier_precision() -> None:
    settings = make_settings()
    model = build_model(settings.assent_model, settings)
    pipeline: list[tuple[str, str]] = []
    raw: list[tuple[str, str]] = []
    latencies: list[int] = []
    for utterance, want in LABELLED:
        result = await classify_assent(model, READBACK, utterance, timeout=15.0)
        pipeline.append((want, result.label.value))
        if result.source == "model":
            latencies.append(result.latency_ms)
        started = time.perf_counter()
        bare = await structured_label(
            model,
            system=ASSENT_SYSTEM_PROMPT,
            user=assent_user_prompt(READBACK, utterance),
            tool_name="classify_assent",
            options=tuple(x.value for x in AssentLabel),
            timeout=15.0,
        )
        raw.append((want, bare or "none"))
        print(
            f"{want:16} pipeline={result.label.value:16} ({result.source:8}) "
            f"model-only={bare} {round((time.perf_counter() - started) * 1000)}ms  {utterance!r}"
        )
    p, r, fp = _precision_recall(pipeline)
    rp, rr, rfp = _precision_recall(raw)
    print(f"\nN={len(LABELLED)} pipeline affirmative precision={p:.3f} recall={r:.3f} fp={fp}")
    print(f"model-only affirmative precision={rp:.3f} recall={rr:.3f} fp={rfp}")
    print(f"model latency ms median={statistics.median(latencies)} max={max(latencies)}")
    assert fp == 0 and p == 1.0


class FirstChunkTimer:
    """Wraps the real upstream model; records when its first chunk (text or tool call) arrives."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.first: float | None = None

    @property
    def model_name(self) -> str:
        return str(self.inner.model_name)

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for delta in self.inner.stream(*args, **kwargs):
            if self.first is None:
                self.first = time.perf_counter()
            yield delta


async def test_live_ttft_added_on_an_assent_turn(
    caplog: pytest.LogCaptureFixture, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    """Time from request start to the upstream model's first chunk, with and without assent.

    Measured inside the process at the model boundary (not via the buffered ASGI response).
    """
    settings = make_settings()
    timer = FirstChunkTimer(build_model("ollama:gemma4:e4b", settings))
    store = SessionStore()
    app = create_app(
        settings,
        chat_model=timer,
        registry=ToolRegistry(),
        hooks=[CommitmentHook(score_ready=False)],
        session_store=store,
        sessionmaker=dead_db,
        warm_dbos=False,
    )
    caplog.set_level(logging.INFO, logger="relay.delegator.commitment")

    async def ttft(with_proposal: bool) -> int:
        sid = uuid.uuid4()
        if with_proposal:
            store.get(sid).pending_proposal = PendingProposal(
                goal="g",
                scope_excludes="pricing",
                artifact_kind="document",
                kind="research",
                readback_text=READBACK,
                idea_id=None,
                proposed_at_user_turn=0,
            )
        body = {
            "messages": [
                {"role": "system", "content": "You are relay, a voice-first thinking partner."},
                {"role": "user", "content": "Research proximity-unlock bike locks, skip pricing."},
                {"role": "assistant", "content": READBACK},
                {"role": "user", "content": "Yes please."},
            ],
            "stream": True,
            "elevenlabs_extra_body": {"session_id": str(sid)},
        }
        caplog.clear()
        timer.first = None
        started = time.perf_counter()
        await post(app, body)
        assert timer.first is not None
        if with_proposal:
            assert any("label=affirmative" in r.getMessage() for r in caplog.records)
        return round((timer.first - started) * 1000)

    await ttft(False)  # warm up
    base, assent = [], []
    for _ in range(6):
        base.append(await ttft(False))
        assent.append(await ttft(True))
    print(f"\nfirst-chunk ms baseline={sorted(base)} p50={statistics.median(base)}")
    print(f"first-chunk ms assent  ={sorted(assent)} p50={statistics.median(assent)}")
    print(f"added p50={statistics.median(assent) - statistics.median(base)} ms")
