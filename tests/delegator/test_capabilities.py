"""Delegator capability store and endpoint (TASK-35)."""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.client.capabilities import CapabilityProfile, publish
from relay.delegator.app import create_app
from relay.delegator.capabilities import CapabilityStore, get_capabilities
from relay.delegator.contracts import ToolRegistry

from .conftest import SECRET, ScriptedChatModel, make_settings

LOCAL = CapabilityProfile(
    can_run_local=True,
    local_model="gemma4:e4b",
    local_base_url="http://localhost:11434/v1",
    free_mem_gb=12.5,
)


def test_missing_profile_reads_as_cloud_only() -> None:
    """Bug: an unknown session raising, or defaulting to local, instead of cloud-only."""
    profile = get_capabilities(uuid.uuid4(), CapabilityStore())
    assert profile.can_run_local is False
    assert profile.can_run_laya_mlx is False and profile.local_model is None


def test_store_is_bounded_and_evicts_least_recently_used() -> None:
    """Bug: an unbounded per-session dict growing for the life of the process."""
    store = CapabilityStore(max_sessions=2)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store.put(a, LOCAL)
    store.put(b, LOCAL)
    assert get_capabilities(a, store) == LOCAL  # touch a: b is now least recent
    store.put(c, LOCAL)
    assert len(store) == 2
    assert get_capabilities(b, store).can_run_local is False
    assert get_capabilities(a, store) == LOCAL


async def test_endpoint_needs_auth_and_round_trips_the_client_publish(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    """Bug: an unauthenticated write path, or a client body the route does not accept."""
    app = create_app(
        make_settings(),
        chat_model=ScriptedChatModel([]),
        registry=ToolRegistry(),
        hooks=[],
        sessionmaker=dead_db,
        warm_dbos=False,
    )
    store: CapabilityStore = app.state.capabilities
    transport = httpx.ASGITransport(app=app)
    session_id = uuid.uuid4()

    for secret in ("", "wrong"):
        assert not await publish(
            LOCAL, session_id, delegator_url="http://test", secret=secret, transport=transport
        )
    assert get_capabilities(session_id, store).can_run_local is False

    assert await publish(
        LOCAL, session_id, delegator_url="http://test", secret=SECRET, transport=transport
    )
    assert get_capabilities(session_id, store) == LOCAL
