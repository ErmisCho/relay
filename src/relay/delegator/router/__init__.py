"""Per-turn routing (TASK-36/41): interface, shared questions, backends registered by name."""

from __future__ import annotations

from relay.delegator.router.base import (
    Router,
    RouterDecision,
    RouterStatus,
    StatusReportingRouter,
    Turn,
    build_router,
    register_router,
    registered_routers,
    unregister_router,
)
from relay.delegator.router.laya import LayaRouter
from relay.delegator.router.llm import LLMRouter

register_router(LayaRouter.name, LayaRouter)
register_router(LLMRouter.name, LLMRouter)

__all__ = [
    "LLMRouter",
    "LayaRouter",
    "Router",
    "RouterDecision",
    "RouterStatus",
    "StatusReportingRouter",
    "Turn",
    "build_router",
    "register_router",
    "registered_routers",
    "unregister_router",
]
