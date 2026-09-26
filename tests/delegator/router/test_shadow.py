"""Shadow routing through the real Delegator request path and the migrated test database."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import SessionState, ToolRegistry, TurnContext
from relay.delegator.hooks.router import RouterHook
from relay.delegator.router import (
    RouterDecision,
    RouterStatus,
    Turn,
    register_router,
    unregister_router,
)
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import RouterDecision as RouterDecisionRow
from relay.store.models import Turn as Turn_

from ..conftest import AUTH, ScriptedChatModel, load_request, make_settings, text

DELAY_S = 1.0


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


class SlowRouter:
    """Answers after ``DELAY_S``: far longer than a streamed reply takes here."""

    name = "laya"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = False
        self.utterances: list[str] = []
        self.contexts: list[list[Turn]] = []

    async def decide(self, utterance: str, context: list[Turn]) -> RouterDecision | None:
        self.started.set()
        self.utterances.append(utterance)
        self.contexts.append(context)
        await asyncio.sleep(DELAY_S)
        self.finished = True
        return RouterDecision(
            difficulty="small_local",
            ready="keep_talking",
            intent="explore_idea",
            confidence=0.3,
            backend=self.name,
            latency_ms=round(DELAY_S * 1000),
        )


class TimingOutRouter:
    name = "llm"

    async def decide(self, utterance: str, context: list[Turn]) -> RouterDecision | None:
        return None

    async def decide_with_status(
        self, utterance: str, context: list[Turn]
    ) -> tuple[RouterDecision | None, RouterStatus]:
        return None, "timeout"


async def test_shadow_run_does_not_delay_the_response_and_logs_inactive_rows(
    db: async_sessionmaker[AsyncSession],
) -> None:
    """Bug caught: shadow routers awaited on the request path (TTFT grows by their latency),
    or shadow rows written as ``is_active=true`` even when ROUTER_ACTIVE names the backend."""
    slow, failing = SlowRouter(), TimingOutRouter()
    hook = RouterHook(backends=[slow, failing])
    app = create_app(
        make_settings(router_active="llm", router_shadow=["laya", "llm"]),
        chat_model=ScriptedChatModel([text("Who ", "rides it?")]),
        registry=ToolRegistry(),
        hooks=[hook],
        sessionmaker=db,
    )
    body = load_request()
    session_id = uuid.uuid4()
    body["elevenlabs_extra_body"]["session_id"] = str(session_id)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = time.perf_counter()
        resp = await client.post("/v1/chat/completions", json=body, headers=AUTH)
        elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    # The whole streamed body (an upper bound on time-to-first-token) arrived well before the
    # shadow backend could have answered.
    assert elapsed < DELAY_S / 2
    assert not slow.finished

    await app.state.service.drain()
    assert slow.finished
    assert slow.utterances == ["It unlocks when my phone is nearby."]
    assert [t.role for t in slow.contexts[0]] == ["assistant", "user", "assistant"]

    async with db() as s:
        user_turn_id = await s.scalar(
            select(Turn_.id).where(Turn_.session_id == session_id, Turn_.role == "user")
        )
        rows = (
            await s.scalars(
                select(RouterDecisionRow).where(RouterDecisionRow.turn_id == user_turn_id)
            )
        ).all()
    by_backend = {r.backend: r for r in rows}
    assert set(by_backend) == {"laya", "llm"}
    laya, llm = by_backend["laya"], by_backend["llm"]
    assert (laya.is_active, laya.difficulty, laya.ready, laya.intent, laya.router_status) == (
        False,
        "small_local",
        "keep_talking",
        "explore_idea",
        "ok",
    )
    assert laya.latency_ms == round(DELAY_S * 1000)
    assert (llm.is_active, llm.difficulty, llm.ready, llm.router_status) == (
        False,
        None,
        None,
        "timeout",
    )


class CountingRouter:
    name = "unit_test_backend"

    def __init__(self, settings: object) -> None:
        self.calls = 0

    async def decide(self, utterance: str, context: list[Turn]) -> RouterDecision | None:
        self.calls += 1
        return None


async def test_new_backend_registers_by_name_without_touching_the_request_path(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    """Bug caught: backends hard-wired in the hook or service, so ROUTER_SHADOW cannot name a
    newly registered backend."""
    built: list[CountingRouter] = []

    def factory(settings: object) -> CountingRouter:
        built.append(CountingRouter(settings))
        return built[-1]

    register_router(CountingRouter.name, factory)
    try:
        hook = RouterHook()
        ctx = TurnContext(
            state=SessionState(session_id=uuid.uuid4()),
            db=dead_db,
            settings=make_settings(router_shadow="unit_test_backend,not_a_backend"),
            messages=[{"role": "user", "content": "rename foo to bar"}],
            user_text="rename foo to bar",
            user_turn_id=uuid.uuid4(),
        )
        await hook.after_response(ctx, "ok")
        await hook.drain()
    finally:
        unregister_router(CountingRouter.name)
    assert len(built) == 1 and built[0].calls == 1
