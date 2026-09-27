"""Per-turn router interface (TASK-36): one protocol, backends registered by name.

A router answers the shared ``choice`` questions in ``questions.py`` for one user turn.
Every backend is zero-shot and uncalibrated (SPEC section 2), so:

* ``confidence`` is recorded for audit only and no code path may branch on it;
* ``None`` means "no decision" (timeout, error, invalid output); callers treat it as
  ``frontier``, the Phase 1 behaviour.

Adding a backend means implementing ``Router`` and calling ``register_router``; the
Delegator request path never changes (the shadow hook resolves backends by name).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from relay.config import Settings

Difficulty = Literal["small_local", "frontier"]
Ready = Literal["keep_talking", "ready_to_execute"]
# Outcome of one router call; matches ``router_decisions.router_status``.
RouterStatus = Literal["ok", "invalid", "timeout", "error"]


@dataclass(frozen=True)
class Turn:
    """One earlier conversation turn given to a router as context."""

    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class RouterDecision:
    """One backend's answers for a user turn.

    ``confidence`` is uncalibrated (Laya) or absent (LLM); never threshold on it.
    """

    difficulty: Difficulty
    ready: Ready
    intent: str
    confidence: float | None
    backend: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@runtime_checkable
class Router(Protocol):
    """A per-turn routing backend. ``decide`` never raises; failure is ``None``."""

    name: str

    async def decide(self, utterance: str, context: list[Turn]) -> RouterDecision | None: ...


@runtime_checkable
class StatusReportingRouter(Protocol):
    """Optional extra: a backend that can say *why* it returned no decision.

    The shadow hook records the status in ``router_decisions.router_status`` so the router
    report can separate timeouts from errors and invalid output.
    """

    async def decide_with_status(
        self, utterance: str, context: list[Turn]
    ) -> tuple[RouterDecision | None, RouterStatus]: ...


RouterFactory = Callable[[Settings], Router]

_REGISTRY: dict[str, RouterFactory] = {}


def register_router(name: str, factory: RouterFactory) -> None:
    """Register a backend factory under ``name`` (the ``ROUTER_SHADOW``/``ROUTER_ACTIVE`` key)."""
    if name in _REGISTRY:
        raise ValueError(f"router backend {name!r} registered twice")
    _REGISTRY[name] = factory


def unregister_router(name: str) -> None:
    """Remove a backend (tests register throwaway backends)."""
    _REGISTRY.pop(name, None)


def registered_routers() -> Sequence[str]:
    return tuple(_REGISTRY)


def build_router(name: str, settings: Settings) -> Router:
    """Construct the backend registered as ``name``; ``KeyError`` if there is none."""
    return _REGISTRY[name](settings)
