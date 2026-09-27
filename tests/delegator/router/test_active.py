"""Active per-turn routing (TASK-37) through the real Delegator request path and test database.

Routers are stubs registered under the real backend names, so ``ROUTER_ACTIVE`` resolves them
exactly as in production; the device-local model is a scripted stand-in for Ollama.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.client.capabilities import CapabilityProfile
from relay.delegator import capabilities
from relay.delegator import service as service_module
from relay.delegator.app import create_app
from relay.delegator.contracts import PendingProposal, SessionStore, ToolRegistry
from relay.delegator.hooks.router import LAYA_ACTIVE_BUDGET_S, RouterHook
from relay.delegator.llm import factory
from relay.delegator.llm.base import ChatDelta
from relay.delegator.router import (
    LayaRouter,
    LLMRouter,
    RouterDecision,
    Turn,
    register_router,
    unregister_router,
)
from relay.delegator.service import APOLOGY_TEXT
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import RouterDecision as RouterDecisionRow
from relay.store.models import Turn as TurnRow

from ..conftest import AUTH, ScriptedChatModel, load_request, make_settings, text

LOCAL_MODEL = "gemma4:e4b"
LOCAL_NAME = f"local:{LOCAL_MODEL}"
FRONTIER_TEXT = "Frontier here."
LOCAL_TEXT = "Local here."
LOCAL_PROFILE = CapabilityProfile(
    can_run_local=True, local_model=LOCAL_MODEL, local_base_url="http://device:11434/v1"
)


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


class StubRouter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.difficulty: Any = "small_local"
        self.delay = 0.0
        self.error = False
        self.calls = 0

    async def decide(self, utterance: str, context: list[Turn]) -> RouterDecision | None:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise RuntimeError("router down")
        return RouterDecision(
            difficulty=self.difficulty,
            ready="keep_talking",
            intent="explore_idea",
            confidence=None,
            backend=self.name,
            latency_ms=round(self.delay * 1000),
            input_tokens=400 if self.name == "llm" else None,
            output_tokens=25 if self.name == "llm" else None,
            cost_usd=0.0 if self.name == "llm" else None,
        )


@pytest.fixture
def routers() -> Iterator[dict[str, StubRouter]]:
    """Stubs registered as ``laya`` and ``llm``; the real backends are restored afterwards."""
    stubs = {"laya": StubRouter("laya"), "llm": StubRouter("llm")}
    for name, stub in stubs.items():
        unregister_router(name)
        register_router(name, lambda _settings, stub=stub: stub)
    yield stubs
    for name in stubs:
        unregister_router(name)
    register_router(LayaRouter.name, LayaRouter)
    register_router(LLMRouter.name, LLMRouter)


class StallingModel:
    model_name = LOCAL_NAME

    async def stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatDelta]:
        await asyncio.sleep(10)
        yield ChatDelta(content="too late")


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The device's Ollama: records builds; ``model`` is what the next build returns."""
    state: dict[str, Any] = {"builds": [], "model": None}

    def build(base_url: str, model: str) -> Any:
        state["builds"].append((base_url, model))
        return state["model"] or ScriptedChatModel([text(LOCAL_TEXT)], name=LOCAL_NAME)

    monkeypatch.setattr(service_module, "build_local_model", build)
    return state


async def run_turn(
    db: async_sessionmaker[AsyncSession],
    *,
    active: str,
    profile: CapabilityProfile | None = LOCAL_PROFILE,
    shadow: list[str] | None = None,
    session_store: SessionStore | None = None,
    session_id: uuid.UUID | None = None,
) -> tuple[str, float, ScriptedChatModel, TurnRow, list[RouterDecisionRow]]:
    session_id = session_id or uuid.uuid4()
    if profile is not None:
        capabilities.STORE.put(session_id, profile)
    frontier = ScriptedChatModel([text(FRONTIER_TEXT)], name="openai:gpt-6-luna")
    app = create_app(
        make_settings(router_active=active, router_shadow=shadow or []),
        chat_model=frontier,
        registry=ToolRegistry(),
        hooks=[RouterHook()],
        session_store=session_store,
        sessionmaker=db,
    )
    body = load_request()
    body["elevenlabs_extra_body"]["session_id"] = str(session_id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = time.perf_counter()
        resp = await client.post("/v1/chat/completions", json=body, headers=AUTH)
        elapsed = time.perf_counter() - start
    assert resp.status_code == 200
    await app.state.service.drain()
    async with db() as s:
        assistant = (
            await s.scalars(
                select(TurnRow).where(TurnRow.session_id == session_id, TurnRow.role == "assistant")
            )
        ).one()
        user_id = await s.scalar(
            select(TurnRow.id).where(TurnRow.session_id == session_id, TurnRow.role == "user")
        )
        rows = list(
            (
                await s.scalars(
                    select(RouterDecisionRow).where(RouterDecisionRow.turn_id == user_id)
                )
            ).all()
        )
    return resp.text, elapsed, frontier, assistant, rows


async def test_small_local_turn_on_capable_device_is_served_by_ollama(
    db: async_sessionmaker[AsyncSession], routers: dict[str, StubRouter], local: dict[str, Any]
) -> None:
    """AC#1. Bug caught: the active decision is logged but the normal model still answers, the
    turn is recorded as ``frontier``, or the active backend also runs again in shadow."""
    body, _, frontier, turn, rows = await run_turn(db, active="laya", shadow=["laya", "llm"])
    assert LOCAL_TEXT in body and FRONTIER_TEXT not in body
    assert frontier.calls == []
    assert local["builds"] == [("http://device:11434/v1", LOCAL_MODEL)]
    assert (turn.route, turn.model_used) == ("small_local", LOCAL_NAME)
    assert turn.meta["router"]["backend"] == "laya"
    assert routers["laya"].calls == 1  # reused for the shadow log, not asked twice
    by_backend = {r.backend: r for r in rows}
    assert (by_backend["laya"].is_active, by_backend["laya"].difficulty) == (True, "small_local")
    assert by_backend["llm"].is_active is False


@pytest.mark.parametrize("active", ["laya", "llm", "none"])
async def test_router_active_is_switched_by_settings_alone(
    db: async_sessionmaker[AsyncSession],
    routers: dict[str, StubRouter],
    local: dict[str, Any],
    active: str,
) -> None:
    """AC#2. Bug caught: a backend hard-wired on the request path, or ``none`` (the force-
    frontier toggle) still calling a router."""
    body, _, _, turn, rows = await run_turn(db, active=active)
    assert {n: r.calls for n, r in routers.items()} == {
        n: int(n == active) for n in ("laya", "llm")
    }
    assert turn.route == ("frontier" if active == "none" else "small_local")
    assert (FRONTIER_TEXT if active == "none" else LOCAL_TEXT) in body
    if active == "llm":
        [row] = rows
        assert (row.input_tokens, row.output_tokens, row.cost_usd) == (400, 25, 0.0)


@pytest.mark.parametrize(
    "failure", ["router_timeout", "router_error", "local_error", "local_stall"]
)
async def test_failures_fall_back_to_the_normal_model_without_error_speech(
    db: async_sessionmaker[AsyncSession],
    routers: dict[str, StubRouter],
    local: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """AC#3. Bug caught: a router or local-model failure reaching the user as the apology, or
    a stalled local model leaving the user in silence instead of restarting on the normal one."""
    laya = routers["laya"]
    if failure == "router_timeout":
        laya.delay = LAYA_ACTIVE_BUDGET_S + 0.35
    elif failure == "router_error":
        laya.error = True
    elif failure == "local_error":
        local["model"] = ScriptedChatModel([ConnectionError("ollama down")], name=LOCAL_NAME)
    else:
        monkeypatch.setattr(factory, "LOCAL_FIRST_DELTA_TIMEOUT_S", 0.1)
        local["model"] = StallingModel()
    body, elapsed, frontier, turn, rows = await run_turn(db, active="laya")
    assert FRONTIER_TEXT in body
    assert APOLOGY_TEXT not in body and LOCAL_TEXT not in body
    assert len(frontier.calls) == 1
    assert (turn.route, turn.model_used) == ("frontier", "openai:gpt-6-luna")
    [row] = rows
    assert row.is_active is True
    if failure == "router_timeout":
        assert row.router_status == "timeout" and row.difficulty is None
        assert elapsed < laya.delay  # the turn did not wait for the late router
    elif failure == "router_error":
        assert row.router_status == "error"
    else:
        assert turn.meta.get("local_fallback") is True


async def test_router_just_under_its_budget_still_routes(
    db: async_sessionmaker[AsyncSession], routers: dict[str, StubRouter], local: dict[str, Any]
) -> None:
    """Bug caught: a budget so tight (or measured so wrongly) that a laya answer inside it is
    discarded, so small_local never happens in practice."""
    routers["laya"].delay = LAYA_ACTIVE_BUDGET_S * 0.5
    body, _, _, turn, _ = await run_turn(db, active="laya")
    assert turn.route == "small_local" and LOCAL_TEXT in body


async def test_no_capability_profile_is_never_local(
    db: async_sessionmaker[AsyncSession], routers: dict[str, StubRouter], local: dict[str, Any]
) -> None:
    """Bug caught: a small_local decision routed to Ollama for a device that never published
    a profile (cloud-only)."""
    body, _, frontier, turn, _ = await run_turn(db, active="laya", profile=None)
    assert routers["laya"].calls == 1
    assert local["builds"] == [] and len(frontier.calls) == 1
    assert turn.route == "frontier" and FRONTIER_TEXT in body


async def test_assent_turn_is_never_routed_or_served_locally(
    db: async_sessionmaker[AsyncSession], routers: dict[str, StubRouter], local: dict[str, Any]
) -> None:
    """AC#4. Bug caught: the turn answering a read-back (where assent is classified) sent to
    the router or spoken by the device-local model."""
    session_id = uuid.uuid4()
    store = SessionStore()
    store.get(session_id).pending_proposal = PendingProposal(
        goal="g",
        scope_excludes="",
        artifact_kind="report",
        kind="research",
        readback_text="I'll research g. Shall I start?",
        idea_id=None,
        proposed_at_user_turn=1,
    )
    body, _, frontier, turn, rows = await run_turn(
        db, active="laya", session_store=store, session_id=session_id
    )
    assert routers["laya"].calls == 0 and rows == []
    assert local["builds"] == [] and len(frontier.calls) == 1
    assert turn.route == "frontier" and FRONTIER_TEXT in body
