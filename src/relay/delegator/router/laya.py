"""Zero-shot Laya router backend (TASK-36; SPEC section 2).

``aac6fef/laya-mlx`` (421M ModernBERT-large, FP16) via ``laya-mlx`` on Apple Silicon, or
``laya`` (ONNX) elsewhere. Neither package is a project dependency yet: when the import
fails the backend is *unavailable*: it logs that once and every ``decide`` returns ``None``.
It activates by itself once the package is installed.

The model loads once, lazily, in a worker thread on the first shadow call (never on the
request path); the checkpoint's clamped-temperature ``RuntimeWarning`` is logged once at
load. Only the argmax ``choice`` of each shared question is used. ``confidence`` is kept for
audit and is never thresholded (observed 0.01-0.58 even when correct).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import time
import warnings
from typing import Any

from relay.config import Settings
from relay.delegator.router.base import RouterDecision, RouterStatus, Turn
from relay.delegator.router.questions import ALL_QUESTIONS, decision_from_answers, render_context

log = logging.getLogger(__name__)

BACKEND = "laya"
MODEL_ID = "aac6fef/laya-mlx"
# Package candidates, in preference order: MLX on Apple Silicon, ONNX elsewhere.
PACKAGES = ("laya_mlx", "laya")
# Laya's English checkpoint reads 512 tokens; ~4 chars/token, utterance first.
MAX_UTTERANCE_CHARS = 600
MAX_CONTEXT_CHARS = 1400
CONTEXT_TURNS = 4


def _number(value: object) -> float | None:
    """``value`` as a float when it is a number (JSON-ish model output), else None."""
    return float(value) if isinstance(value, (int, float)) else None


def find_package() -> str | None:
    """Name of the first importable Laya package, or None."""
    for name in PACKAGES:
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except (ImportError, ValueError):
            continue
    return None


class LayaRouter:
    """``Router`` backed by a local Laya checkpoint; unavailable without the package."""

    name = BACKEND

    def __init__(self, settings: Settings | None = None, *, agent: Any = None) -> None:
        self._agent = agent
        self._package = None if agent is not None else find_package()
        self._lock = asyncio.Lock()
        self._load_failed = False
        self.available = agent is not None or self._package is not None
        if not self.available:
            log.warning(
                "router laya unavailable: neither laya-mlx nor laya is installed; the laya "
                "backend returns no decision (install laya-mlx==0.2.0 to enable it)"
            )

    async def decide(self, utterance: str, context: list[Turn]) -> RouterDecision | None:
        decision, _ = await self.decide_with_status(utterance, context)
        return decision

    async def decide_with_status(
        self, utterance: str, context: list[Turn]
    ) -> tuple[RouterDecision | None, RouterStatus]:
        if not self.available:
            return None, "error"
        agent = await self._load()
        if agent is None:
            return None, "error"
        state = {
            "utterance": " ".join(utterance.split())[:MAX_UTTERANCE_CHARS],
            "context": render_context(context, CONTEXT_TURNS, MAX_CONTEXT_CHARS),
        }
        questions = {key: q.as_schema() for key, q in ALL_QUESTIONS.items()}
        start = time.perf_counter()
        try:
            result = await asyncio.to_thread(agent.predict, state, questions)
            answers = result["answers"]
            choices = {key: (answers.get(key) or {}).get("choice") for key in ALL_QUESTIONS}
            # Recorded for audit only; uncalibrated, never used to decide anything.
            confidence = (answers.get("difficulty") or {}).get("confidence")
        except Exception:  # noqa: BLE001 - a router failure must never reach the turn
            log.warning("router laya predict failed", exc_info=True)
            return None, "error"
        latency_ms = round((time.perf_counter() - start) * 1000)
        decision = decision_from_answers(
            choices,
            backend=BACKEND,
            latency_ms=latency_ms,
            confidence=_number(confidence),
        )
        return decision, "ok" if decision is not None else "invalid"

    async def _load(self) -> Any:
        if self._agent is not None or self._load_failed:
            return self._agent
        async with self._lock:
            if self._agent is None and not self._load_failed:
                try:
                    self._agent = await asyncio.to_thread(self._load_sync)
                except Exception:  # noqa: BLE001 - unavailable, not fatal
                    self._load_failed = True
                    log.warning("router laya: loading %s failed", MODEL_ID, exc_info=True)
        return self._agent

    def _load_sync(self) -> Any:
        assert self._package is not None
        module = importlib.import_module(self._package)
        start = time.perf_counter()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            agent = module.load(MODEL_ID)
        log.info(
            "router laya: loaded %s via %s in %.0f ms",
            MODEL_ID,
            self._package,
            (time.perf_counter() - start) * 1000,
        )
        for warning in caught:
            log.warning("router laya load warning: %s", warning.message)
        return agent
