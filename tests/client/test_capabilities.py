"""Client capability probe and publishing (TASK-35): cloud stays correct when local is absent."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
import pytest

from relay.client import capabilities
from relay.client.capabilities import CLOUD_ONLY, CapabilityPublishingStore, probe
from relay.config import Settings

BUDGET_S = capabilities.OLLAMA_TIMEOUT_S + 1.0


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ollama_base_url": "http://ollama.test:11434/v1",
        "router_model": "ollama:gemma4:e4b",
        "delegator_shared_secret": "s3cret",
        "delegator_public_url": "http://delegator.test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


async def _hangs(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(60)
    raise AssertionError("unreachable")


@pytest.mark.parametrize("handler", [_refused, _hangs], ids=["refused", "hangs"])
async def test_no_ollama_reports_cloud_only_within_budget(handler: Any) -> None:
    """Bug: a down or wedged daemon either raises out of the probe or stalls the wake path."""
    started = time.perf_counter()
    profile = await probe(_settings(), transport=httpx.MockTransport(handler))
    assert time.perf_counter() - started < BUDGET_S
    assert profile.can_run_local is False
    assert profile.local_model is None and profile.local_base_url is None


async def test_real_refused_port_reports_cloud_only() -> None:
    """Same bug through a real socket: nothing listens on port 1."""
    started = time.perf_counter()
    profile = await probe(_settings(ollama_base_url="http://127.0.0.1:1/v1"))
    assert time.perf_counter() - started < BUDGET_S
    assert profile.can_run_local is False


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        (["llama3:8b", "mistral:7b"], None),  # daemon up, no configured model installed
        (["gemma4:e4b"], "gemma4:e4b"),  # control: present -> local
    ],
)
async def test_local_only_when_the_configured_model_is_installed(
    installed: list[str], expected: str | None
) -> None:
    """Bug: a reachable daemon alone must not claim local capability without the model."""
    seen: list[str] = []

    def tags(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": n} for n in installed]})

    profile = await probe(_settings(), transport=httpx.MockTransport(tags))
    # The OpenAI-compatible /v1 base must be stripped before /api/tags.
    assert seen == ["http://ollama.test:11434/api/tags"]
    assert profile.can_run_local is (expected is not None)
    assert profile.local_model == expected
    assert profile.local_base_url == ("http://ollama.test:11434/v1" if expected else None)


class RecordingStore:
    def __init__(self) -> None:
        self.created: list[uuid.UUID] = []

    async def create(self, session_id: uuid.UUID, wake_trigger: str) -> None:
        self.created.append(session_id)

    async def end(self, session_id: uuid.UUID, reason: str) -> None:
        return None

    async def end_stale(self, idle_s: float) -> int:
        return 0


async def test_probe_failure_never_blocks_session_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: awaiting (or propagating) the probe/publish inside session start."""
    release = asyncio.Event()

    async def broken_probe(*args: Any, **kwargs: Any) -> Any:
        await release.wait()
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(capabilities, "probe", broken_probe)
    inner = RecordingStore()
    store = CapabilityPublishingStore(inner, _settings())
    session_id = uuid.uuid4()

    await asyncio.wait_for(store.create(session_id, "hey relay"), 0.5)
    assert inner.created == [session_id]

    release.set()
    await store.aclose(timeout_s=1.0)  # the failure stays inside the background task
    assert store.last == CLOUD_ONLY


async def test_each_wake_publishes_a_fresh_profile_with_bearer_auth() -> None:
    """Bug: publishing without the Delegator secret, or only once instead of per wake."""
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b"}]})
        posts.append(request)
        return httpx.Response(204)

    store = CapabilityPublishingStore(
        RecordingStore(), _settings(), transport=httpx.MockTransport(handler)
    )
    first, second = uuid.uuid4(), uuid.uuid4()
    await store.create(first, "hey relay")
    await store.create(second, "hey relay")
    await store.aclose()

    assert [str(r.url) for r in posts] == ["http://delegator.test/v1/capabilities"] * 2
    assert {r.headers["Authorization"] for r in posts} == {"Bearer s3cret"}
    bodies = [httpx.Response(200, content=r.content).json() for r in posts]
    # Each publish runs in its own background task, so the two POSTs may arrive in either order.
    assert sorted(b["session_id"] for b in bodies) == sorted([str(first), str(second)])
    assert all(b["can_run_local"] is True and b["local_model"] == "gemma4:e4b" for b in bodies)
