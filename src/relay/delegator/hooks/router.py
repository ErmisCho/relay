"""Shadow routing (TASK-36): run the ``ROUTER_SHADOW`` backends on every user turn, log only.

The backends start in ``after_response``, which the Delegator calls from the detached turn
finalisation once the reply has been streamed and persisted. Nothing here is awaited on the
time-to-first-token path, and a local router model never competes with the voice model for
the GPU while it streams. All shadow backends run concurrently (``asyncio.gather``) in one
background task per turn; each result becomes a ``router_decisions`` row with
``is_active=false``, including failures (``router_status`` timeout/error/invalid, no labels).

Active routing (TASK-37): with ``ROUTER_ACTIVE=laya|llm`` the Delegator calls ``route_turn``
on the request path, inside ``active_budget_s``. The active backend's result is recorded after
the response as the turn's ``is_active=true`` row and the backend is not run again in shadow.
``ROUTER_ACTIVE=none`` never calls a router on the request path (force frontier).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
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
# Request-path budget for the active backend. Laya answers in ~50-100 ms once loaded; the llm
# backend uses ROUTER_LLM_TIMEOUT_MS. Over budget the turn goes to the frontier model while the
# call finishes in the background (so a lazily loading model still ends up loaded).
LAYA_ACTIVE_BUDGET_S = 0.150
DEFAULT_ACTIVE_BUDGET_S = 0.150
# Active results waiting for their turn's after_response; bounded against turns that never
# finalise (e.g. cancelled on shutdown).
_MAX_PENDING_ACTIVE = 256


@dataclass(frozen=True)
class ActiveRoute:
    """The active backend's result for one user turn (``decision`` None = frontier)."""

    backend: str
    decision: RouterDecision | None
    status: RouterStatus
    latency_ms: int


def active_budget_s(name: str, settings: Settings) -> float:
    """Request-path budget for the active backend ``name``."""
    if name == "llm":
        return settings.router_llm_timeout_ms / 1000
    if name == "laya":
        return LAYA_ACTIVE_BUDGET_S
    return DEFAULT_ACTIVE_BUDGET_S


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
    """``TurnHook`` running the shadow router backends after each response, and the active
    backend on the request path (``route_turn``).

    ``backends`` overrides the ``ROUTER_SHADOW`` lookup (tests); by default the backends are
    built from the registry on the first turn, once. The active backend is the shadow backend
    of that name if there is one (one instance, run once per turn), else built by name.
    """

    def __init__(self, backends: Sequence[Router] | None = None) -> None:
        self._backends: list[Router] | None = list(backends) if backends is not None else None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._warned_unstorable: set[str] = set()
        self._active_backends: dict[str, Router | None] = {}
        self._pending_active: OrderedDict[uuid.UUID, ActiveRoute] = OrderedDict()

    async def route_turn(self, ctx: TurnContext) -> ActiveRoute | None:
        """Run the ``ROUTER_ACTIVE`` backend for this user turn within its budget.

        None when routing is off (``none``), the request is not a new user turn, or the
        backend is unavailable. Never raises; timeouts and errors come back as a route with
        no decision, which the caller treats as frontier.
        """
        name = ctx.settings.router_active
        if name == "none" or not _is_user_turn(ctx):
            return None
        try:
            backend = self._active_backend(name, ctx.settings)
            if backend is None:
                return None
            budget = active_budget_s(name, ctx.settings)
            start = time.perf_counter()
            task = asyncio.create_task(
                self._decide(backend, ctx.user_text, context_turns(ctx.messages)),
                name=f"router-active:{name}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            # wait() does not cancel on timeout: the call finishes in the background.
            done, _ = await asyncio.wait({task}, timeout=budget)
            if task in done:
                decision, status, latency_ms = task.result()
            else:
                decision, status = None, "timeout"
                latency_ms = round((time.perf_counter() - start) * 1000)
            route = ActiveRoute(backend.name, decision, status, latency_ms)
        except Exception:  # noqa: BLE001 - routing must never fail the turn
            log.exception("router %s: active routing failed; using the frontier model", name)
            return None
        if ctx.user_turn_id is not None:
            self._pending_active[ctx.user_turn_id] = route
            while len(self._pending_active) > _MAX_PENDING_ACTIVE:
                self._pending_active.popitem(last=False)
        return route

    def _active_backend(self, name: str, settings: Settings) -> Router | None:
        if name in self._active_backends:
            return self._active_backends[name]
        backend = next((b for b in self._resolve(settings) if b.name == name), None)
        if backend is None:
            try:
                backend = build_router(name, settings)
            except Exception:
                log.exception("router backend %r (ROUTER_ACTIVE) failed to initialise", name)
        log.info("router: active backend=%s", backend.name if backend else "unavailable")
        self._active_backends[name] = backend
        return backend

    async def before_model(self, ctx: TurnContext) -> list[str]:
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        if not _is_user_turn(ctx):
            return  # tool-result follow-up, not a new user turn
        if ctx.user_turn_id is None:
            return  # the user turn was not persisted (DB down): no row to attach to
        active = self._pending_active.pop(ctx.user_turn_id, None)
        backends = self._resolve(ctx.settings)
        if active is not None:
            # Already asked on the request path: record that answer, never ask twice.
            backends = [b for b in backends if b.name != active.backend]
        if not backends and active is None:
            return
        task = asyncio.create_task(
            self._shadow(
                backends,
                ctx.db,
                ctx.user_turn_id,
                ctx.user_text,
                context_turns(ctx.messages),
                active,
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
            "router: shadow backends=%s active=%s",
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
        active: ActiveRoute | None = None,
    ) -> None:
        if active is not None:
            await self._record(
                db,
                turn_id,
                active.backend,
                active.decision,
                active.status,
                active.latency_ms,
                is_active=True,
            )
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
        *,
        is_active: bool = False,
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
            is_active=is_active,
        )
        try:
            async with db() as session, session.begin():
                session.add(row)
        except IntegrityError:
            if not is_active:
                log.exception("router %s: writing router_decisions failed", backend)
                return
            # uq_router_decisions_active allows one active turn-level row per turn; another
            # writer (e.g. a re-sent turn) got there first. Keep the decision, unmarked.
            log.warning("router %s: turn %s already has an active decision", backend, turn_id)
            await self._record(db, turn_id, backend, decision, status, latency_ms)
        except Exception:
            log.exception("router %s: writing router_decisions failed", backend)


def _is_user_turn(ctx: TurnContext) -> bool:
    return bool(ctx.messages) and ctx.messages[-1].get("role") == "user" and bool(ctx.user_text)
