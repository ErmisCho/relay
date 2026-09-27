"""Shutdown must drain hooks' own background work, not only turn finalisation."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.contracts import ToolRegistry, TurnContext
from relay.delegator.service import DelegatorService


class SlowDrainHook:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.drained = False

    async def before_model(self, ctx: TurnContext) -> list[str]:
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None

    async def drain(self) -> None:
        await asyncio.sleep(self.delay)
        self.drained = True


class NoDrainHook:
    async def before_model(self, ctx: TurnContext) -> list[str]:
        return []

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None


def _service(hooks: list[object], dead_db: async_sessionmaker[AsyncSession]) -> DelegatorService:
    from relay.delegator.contracts import SessionStore

    return DelegatorService(
        settings=Settings(delegator_shared_secret="test-secret"),
        chat_model=None,  # type: ignore[arg-type]  # drain never touches the model
        registry=ToolRegistry(),
        hooks=hooks,  # type: ignore[arg-type]
        session_store=SessionStore(),
        sessionmaker=dead_db,
    )


async def test_drain_waits_for_hook_drain(dead_db: async_sessionmaker[AsyncSession]) -> None:
    hook = SlowDrainHook(delay=0.05)
    await _service([NoDrainHook(), hook], dead_db).drain(timeout=2.0)
    assert hook.drained


async def test_drain_is_bounded_by_timeout(dead_db: async_sessionmaker[AsyncSession]) -> None:
    hook = SlowDrainHook(delay=5.0)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await _service([hook], dead_db).drain(timeout=0.2)
    assert loop.time() - start < 1.0
    assert not hook.drained
