"""Shadow routing (TASK-36): run the ``ROUTER_SHADOW`` backends on every user turn, log only.

The backends start in ``after_response``, which the Delegator calls from the detached turn
finalisation once the reply has been streamed and persisted. Nothing here is awaited on the
time-to-first-token path, and a local router model never competes with the voice model for
the GPU while it streams. All shadow backends run concurrently (``asyncio.gather``) in one
background task per turn; each result becomes a ``router_decisions`` row with
``is_active=false``, including failures (``router_status`` timeout/error/invalid, no labels).

``ROUTER_ACTIVE`` is only read and logged here; routing on it is TASK-37.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.contracts import TurnContext
from relay.delegator.router import (
    Router,
    RouterDecision,
    RouterStatus,
    StatusReportingRouter,
    Turn,
    build_router,
    registered_routers,
)
from relay.store.models import ROUTER_BACKENDS
from relay.store.models import RouterDecision as RouterDecisionRow

log = logging.getLogger(__name__)

# Upper bound on one backend call as seen by the hook, on top of each backend's own budget,
# so a misbehaving backend can never hold the drain on shutdown.
BACKEND_DEADLINE_S = 30.0


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("text")
        )
    return ""


def context_turns(messages: list[dict[str, Any]]) -> list[Turn]:
    """User/assistant text turns before the last message, oldest first."""
    turns: list[Turn] = []
    for msg in messages[:-1]:
        role = msg.get("role")
        text = _text(msg.get("content")).strip()
        if text and role in ("user", "assistant"):
            turns.append(Turn(role="user" if role == "user" else "assistant", text=text))
    return turns


class RouterHook:
    """``TurnHook`` running the shadow router backends after each response.

    ``backends`` overrides the ``ROUTER_SHADOW`` lookup (tests); by default the backends are
    built from the registry on the first turn, once.
    """

    def __init__(self, backends: Sequence[Router] | None = None) -> None:
        self._backends: list[Router] | None = list(backends) if backends is not None else None
        self._tasks: set[asyncio.Task[None]] = set()
        self._warned_unstorable: set[str] = set()

    async def before_model(self, ctx: TurnContext) -> list[str]:
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        if not ctx.messages or ctx.messages[-1].get("role") != "user" or not ctx.user_text:
            return  # tool-result follow-up, not a new user turn
        if ctx.user_turn_id is None:
            return  # the user turn was not persisted (DB down): no row to attach to
        backends = self._resolve(ctx.settings)
        if not backends:
            return
        task = asyncio.create_task(
            self._shadow(
                backends, ctx.db, ctx.user_turn_id, ctx.user_text, context_turns(ctx.messages)
            ),
            name=f"router-shadow:{ctx.user_turn_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for in-flight shadow runs (the Delegator bounds this on shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _resolve(self, settings: Settings) -> list[Router]:
        if self._backends is not None:
            return self._backends
        backends: list[Router] = []
        for name in dict.fromkeys(settings.router_shadow):
            if name not in registered_routers():
                log.warning(
                    "ROUTER_SHADOW names unknown backend %r (registered: %s); skipping",
                    name,
                    ", ".join(registered_routers()),
                )
                continue
            try:
                backends.append(build_router(name, settings))
            except Exception:
                log.exception("router backend %r failed to initialise; skipping", name)
        log.info(
            "router: shadow backends=%s active=%s (active routing is not wired yet: TASK-37)",
            [b.name for b in backends] or "none",
            settings.router_active,
        )
        self._backends = backends
        return backends

    async def _shadow(
        self,
        backends: list[Router],
        db: async_sessionmaker[AsyncSession],
        turn_id: uuid.UUID,
        utterance: str,
        context: list[Turn],
    ) -> None:
        results = await asyncio.gather(
            *(self._decide(b, utterance, context) for b in backends), return_exceptions=True
        )
        for backend, result in zip(backends, results, strict=True):
            if isinstance(result, BaseException):
                log.error("router %s shadow run failed", backend.name, exc_info=result)
                continue
            decision, status, latency_ms = result
            await self._record(db, turn_id, backend.name, decision, status, latency_ms)

    async def _decide(
        self, backend: Router, utterance: str, context: list[Turn]
    ) -> tuple[RouterDecision | None, RouterStatus, int]:
        start = time.perf_counter()
        status: RouterStatus
        try:
            async with asyncio.timeout(BACKEND_DEADLINE_S):
                if isinstance(backend, StatusReportingRouter):
                    decision, status = await backend.decide_with_status(utterance, context)
                else:
                    decision = await backend.decide(utterance, context)
                    status = "ok" if decision is not None else "error"
        except TimeoutError:
            decision, status = None, "timeout"
        except Exception:  # noqa: BLE001 - a broken backend must not affect other backends
            log.warning("router %s raised; recording no decision", backend.name, exc_info=True)
            decision, status = None, "error"
        measured = round((time.perf_counter() - start) * 1000)
        return decision, status, decision.latency_ms if decision is not None else measured

    async def _record(
        self,
        db: async_sessionmaker[AsyncSession],
        turn_id: uuid.UUID,
        backend: str,
        decision: RouterDecision | None,
        status: RouterStatus,
        latency_ms: int,
    ) -> None:
        if backend not in ROUTER_BACKENDS:
            if backend not in self._warned_unstorable:
                self._warned_unstorable.add(backend)
                log.warning(
                    "router %s: router_decisions.backend only allows %s; decisions not stored",
                    backend,
                    ROUTER_BACKENDS,
                )
            return
        row = RouterDecisionRow(
            turn_id=turn_id,
            backend=backend,
            difficulty=decision.difficulty if decision else None,
            ready=decision.ready if decision else None,
            intent=decision.intent if decision else None,
            confidence=decision.confidence if decision else None,
            latency_ms=latency_ms,
            router_status=status,
            is_active=False,
        )
        try:
            async with db() as session, session.begin():
                session.add(row)
        except Exception:
            log.exception("router %s: writing router_decisions failed", backend)
