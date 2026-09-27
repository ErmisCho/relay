"""AC#3: the demo feed must not slow the voice path (TTFT with the feed ON within 10% of OFF).

Two real uvicorn servers on loopback sockets share one stub model whose first token arrives
after ``FIRST_TOKEN_S``. Requests alternate ON/OFF so drift hits both arms equally. The ON arm
has a live browser-style subscriber on ``/demo/sessions/{id}/events``. Run with ``-s`` to see
the p50/p90 numbers.
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.demo.events import BUS
from relay.delegator.hooks.scope import ScopeHook
from relay.delegator.llm import ChatDelta
from relay.store.db import create_engine, create_sessionmaker

from ..commitment.harness import finalised
from ..conftest import AUTH, make_settings

FIRST_TOKEN_S = 0.2
REQUESTS_PER_ARM = 24
PASSCODE = "ttft-pass"


class DelayedModel:
    """Upstream double: first token after ``FIRST_TOKEN_S``, then a few more at once."""

    model_name = "fake:delayed"

    async def stream(
        self, messages: list[dict[str, Any]], tools: Any, **_: Any
    ) -> AsyncIterator[ChatDelta]:
        await asyncio.sleep(FIRST_TOKEN_S)
        for part in ("Sure, ", "let's ", "think."):
            yield ChatDelta(content=part)
        yield ChatDelta(finish_reason="stop")


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


async def serve(app: FastAPI) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
        timeout_graceful_shutdown=1,
    )  # fmt: skip
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"http://127.0.0.1:{port}"


async def ttft(c: httpx.AsyncClient, session_id: str, n: int) -> float:
    body = {
        "model": "relay-delegator",
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are relay."},
            {"role": "user", "content": f"Let's think about bike lock idea number {n}."},
        ],
        "elevenlabs_extra_body": {"session_id": session_id},
    }
    start = time.perf_counter()
    first: float | None = None
    async with c.stream("POST", "/v1/chat/completions", json=body, headers=AUTH) as resp:
        assert resp.status_code == 200
        async for line in resp.aiter_lines():
            if first is None and '"content"' in line:
                first = time.perf_counter() - start
    assert first is not None
    return first


def pct(xs: list[float], q: float) -> float:
    return statistics.quantiles(xs, n=100, method="inclusive")[int(q) - 1]


async def test_feed_on_ttft_within_ten_percent_of_off(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Catches: anything on the voice turn awaiting the feed (a blocking emit, an awaited
    # subscriber put, DB work added for the feed) showing up as extra time-to-first-token.
    def app(passcode: str) -> FastAPI:
        return create_app(
            make_settings(demo_passcode=passcode, demo_web_dist=""),
            chat_model=DelayedModel(),
            hooks=[ScopeHook()],
            sessionmaker=db,
            warm_dbos=False,
        )

    # install() sets the process-wide BUS.enabled; both apps exist at once, so the flag is set
    # per request to the arm being measured (exactly what install() would have set).
    on_app, off_app = app(PASSCODE), app("")
    on_srv, on_task, on_url = await serve(on_app)
    off_srv, off_task, off_url = await serve(off_app)
    feed_events = 0
    try:
        async with (
            httpx.AsyncClient(base_url=on_url, timeout=10) as on,
            httpx.AsyncClient(base_url=off_url, timeout=10) as off,
        ):
            BUS.enabled = True
            auth = await on.post("/demo/auth", json={"passcode": PASSCODE})
            cookie = {"cookie": f"relay_demo={auth.cookies['relay_demo']}"}
            on_sid = (await on.post("/demo/sessions", headers=cookie)).json()["session_id"]
            off_sid = str(uuid.uuid4())

            async def watch() -> None:
                nonlocal feed_events
                path = f"/demo/sessions/{on_sid}/events"
                async with on.stream("GET", path, headers=cookie, timeout=None) as resp:
                    async for line in resp.aiter_lines():
                        feed_events += line.startswith("data: ")

            watcher = asyncio.create_task(watch())
            for n in range(2):  # warm-up, not measured
                await ttft(on, on_sid, -n - 1)
                await finalised(on_app)
            BUS.enabled = False
            await ttft(off, off_sid, -1)

            on_ms: list[float] = []
            off_ms: list[float] = []
            for n in range(REQUESTS_PER_ARM):
                BUS.enabled = True
                on_ms.append(await ttft(on, on_sid, n) * 1000)
                await finalised(on_app)  # its assistant_turn emit runs detached, feed still ON
                BUS.enabled = False
                off_ms.append(await ttft(off, off_sid, n) * 1000)
            await asyncio.sleep(0.2)
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    finally:
        BUS.enabled = False
        BUS._channels.clear()
        for srv in (on_srv, off_srv):
            srv.should_exit = True
        await asyncio.gather(on_task, off_task, return_exceptions=True)

    p50_on, p50_off = statistics.median(on_ms), statistics.median(off_ms)
    p90_on, p90_off = pct(on_ms, 90), pct(off_ms, 90)
    rel = (p50_on - p50_off) / p50_off
    print(
        f"\nTTFT n={REQUESTS_PER_ARM}/arm first-token-delay={FIRST_TOKEN_S * 1000:.0f}ms "
        f"feed_events_seen={feed_events}\n"
        f"  feed ON : p50={p50_on:.1f}ms p90={p90_on:.1f}ms\n"
        f"  feed OFF: p50={p50_off:.1f}ms p90={p90_off:.1f}ms\n"
        f"  p50 delta={rel * 100:+.1f}%  p90 delta={(p90_on - p90_off) / p90_off * 100:+.1f}%"
    )
    # The subscriber really received the ON arm's turns (user_turn + assistant_turn each).
    assert feed_events >= 2 * (REQUESTS_PER_ARM + 2)
    assert rel <= 0.10
