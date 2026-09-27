"""Demo API (TASK-42, AC#7): event order, access control, voice slot, same Delegator path.

Every test drives the real app (``create_app``) and the real test database; only the upstream
chat model, the assent classifier and ElevenLabs are doubled.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.demo import api as demo_api
from relay.delegator.demo.events import BUS
from relay.delegator.hooks.scope import ScopeHook
from relay.store.db import create_engine, create_sessionmaker

from ..commitment.harness import build_convo, dispatch, finalised, propose, settle
from ..conftest import AUTH, ScriptedChatModel, make_settings, parse_sse, text

PASSCODE = "demo-pass"
API_KEY = "sk-eleven-SUPERSECRET-0123456789"
TOKEN_URL = f"{demo_api.ELEVENLABS_API}/v1/convai/conversation/token"
SIGNED_URL = f"{demo_api.ELEVENLABS_API}/v1/convai/conversation/get-signed-url"
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
def clean_bus() -> Iterator[None]:
    """BUS is a process-wide singleton: never leak an enabled feed into other test modules."""
    yield
    BUS.enabled = False
    BUS._channels.clear()
    BUS.watched_tasks.clear()


def demo_app(db: async_sessionmaker[AsyncSession], **settings: Any) -> FastAPI:
    values: dict[str, Any] = {"demo_passcode": PASSCODE, "demo_web_dist": ""}
    values.update(settings)
    return create_app(
        make_settings(**values),
        chat_model=ScriptedChatModel([]),
        hooks=[ScopeHook()],
        sessionmaker=db,
        warm_dbos=False,
    )


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def login(c: httpx.AsyncClient) -> dict[str, str]:
    resp = await c.post("/demo/auth", json={"passcode": PASSCODE})
    assert resp.status_code == 204
    set_cookie = resp.headers["set-cookie"]
    assert "HttpOnly" in set_cookie and "Path=/demo" in set_cookie
    return {"cookie": f"{demo_api.COOKIE}={resp.cookies[demo_api.COOKIE]}"}


def demo_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every (method, concrete path) under /demo, path params filled with a random UUID.

    Read from the OpenAPI schema so routes on nested/included routers are enumerated too.
    """
    out: list[tuple[str, str]] = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith("/demo"):
            concrete = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
            out.extend((m.upper(), concrete) for m in sorted(ops))
    return out


# --- 1. event order through the real Delegator path -------------------------------------


async def test_feed_orders_propose_hedge_repropose_assent_dispatch(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Catches: an emit placed at the wrong decision point (e.g. dispatch emitted at reservation
    # instead of commit, assent emitted after the drop, a missing proposal_dropped on hedge).
    # grace_s=1.0 keeps the commit clearly after the dispatch turn is finalised, as in production
    # (DISPATCH_GRACE_S=3.5); settle() then waits the grace out.
    c = build_convo(db, grace_s=1.0, demo_passcode=PASSCODE)
    assert BUS.enabled
    await c.turn("Research bike locks, skip pricing.", propose(c.goal), text(c.readback()))
    c.assent.answer = "hedge"
    await c.turn("Hmm, maybe, not sure.", text("No problem, what should change?"))
    await c.turn("Okay, same thing, skip pricing.", propose(c.goal), text(c.readback()))
    c.assent.answer = "affirmative"
    await c.turn("Yes, go ahead.", dispatch(), text("On it."))
    await settle()
    assert await c.task_count() == 1

    events = BUS.history(c.session_id)
    got = []
    for e in events:
        t, d = e["type"], e["data"]
        detail = {
            "assent": d.get("label"),
            "proposal_dropped": d.get("reason"),
            "task_status": d.get("status"),
        }.get(t)
        got.append(f"{t}({detail})" if detail else t)
    assert got == [
        "user_turn",
        "proposal",
        "assistant_turn",
        "user_turn",
        "assent(hedge)",
        "proposal_dropped(not_affirmative)",
        "assistant_turn",
        "user_turn",
        "proposal",
        "assistant_turn",
        "user_turn",
        "assent(affirmative)",
        "assistant_turn",
        "dispatch",
        "task_status(queued)",
    ]
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    for e in events:
        assert e["session_id"] == c.session_id and TS.match(e["ts"])
    first, second = (e["data"]["proposal_id"] for e in events if e["type"] == "proposal")
    assert first != second
    dropped = next(e for e in events if e["type"] == "proposal_dropped")
    assert dropped["data"]["proposal_id"] == first
    by_type = {e["type"]: e["data"] for e in events}  # last of each type
    assert by_type["assent"]["proposal_id"] == by_type["dispatch"]["proposal_id"] == second
    assert by_type["assent"]["utterance"] == "Yes, go ahead."
    assert by_type["task_status"]["task_id"] == by_type["dispatch"]["task_id"]


# --- 2. access control -------------------------------------------------------------------


async def test_every_demo_route_needs_the_cookie(db: async_sessionmaker[AsyncSession]) -> None:
    # Catches: a new /demo route registered on the open router (or without the dependency),
    # and a forged/unsigned cookie being accepted.
    app = demo_app(db)
    routes = demo_routes(app)
    assert len(routes) >= 11
    async with client(app) as c:
        for method, path in routes:
            if (method, path) == ("POST", "/demo/auth"):
                continue
            for headers in ({}, {"cookie": f"{demo_api.COOKIE}=123.deadbeef"}):
                resp = await c.request(method, path, headers=headers)
                assert resp.status_code == 401, (method, path, headers)
        cookie = await login(c)
        assert (await c.get("/demo/auth", headers=cookie)).status_code == 204
        assert (await c.get("/demo/ideas", headers=cookie)).status_code == 200


async def test_local_browser_skips_the_passcode_but_the_tunnel_does_not(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Catches: the local no-passcode shortcut leaking to the public tunnel. ngrok's agent
    # also connects from 127.0.0.1, so only Host + X-Forwarded-For tell them apart.
    app = demo_app(db)
    local = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8010"
    )
    async with local as c:
        assert (await c.get("/demo/auth")).status_code == 204
        assert (await c.get("/demo/ideas")).status_code == 200
        tunnelled = {"x-forwarded-for": "203.0.113.7"}
        assert (await c.get("/demo/ideas", headers=tunnelled)).status_code == 401
    public = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://relay.ngrok-free.dev"
    )
    async with public as c:
        assert (await c.get("/demo/ideas")).status_code == 401


async def test_every_demo_route_is_404_when_disabled(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Catches: demo routes (or the event feed) left reachable when DEMO_PASSCODE is empty.
    routes = demo_routes(demo_app(db))
    off = demo_app(db, demo_passcode="")
    assert not BUS.enabled
    async with client(off) as c:
        for method, path in routes:
            resp = await c.request(method, path, json={"passcode": ""})
            assert resp.status_code == 404, (method, path)


async def test_wrong_passcode_refused_then_rate_limited(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches: a wrong passcode issuing a cookie, and brute force not being throttled (the
    # limit must hold even for the right passcode once tripped, then lift after the window).
    clock = [1000.0]
    monkeypatch.setattr(demo_api, "_now", lambda: clock[0])
    app = demo_app(db)
    async with client(app) as c:
        for _ in range(demo_api.AUTH_MAX_FAILURES):
            resp = await c.post("/demo/auth", json={"passcode": "guess"})
            assert resp.status_code == 401 and "set-cookie" not in resp.headers
        resp = await c.post("/demo/auth", json={"passcode": PASSCODE})
        assert resp.status_code == 429 and "set-cookie" not in resp.headers
        clock[0] += demo_api.AUTH_WINDOW_S + 1
        await login(c)


# --- 3. voice ------------------------------------------------------------------------------


def _assert_no_key(resp: httpx.Response) -> None:
    assert API_KEY not in resp.text
    for name, value in resp.headers.items():
        assert API_KEY not in value, name


@pytest.mark.parametrize("path", ["webrtc", "websocket", "failure"])
async def test_voice_credentials_never_leak_the_api_key(
    db: async_sessionmaker[AsyncSession], path: str
) -> None:
    # Catches: the ElevenLabs key (or its raw error body) being passed through to the browser
    # on the token path, the signed-URL fallback, or the error path.
    app = demo_app(db, elevenlabs_api_key=API_KEY, elevenlabs_agent_id="agent_1")
    with respx.mock(assert_all_called=False) as mock:
        token = mock.get(TOKEN_URL)
        signed = mock.get(SIGNED_URL)
        if path == "webrtc":
            token.mock(return_value=httpx.Response(200, json={"token": "conv-tok"}))
        else:
            token.mock(return_value=httpx.Response(401, json={"detail": f"bad key {API_KEY}"}))
        if path == "websocket":
            signed.mock(return_value=httpx.Response(200, json={"signed_url": "wss://x/y?t=1"}))
        else:
            signed.mock(return_value=httpx.Response(500, text=f"boom {API_KEY}"))
        async with client(app) as c:
            cookie = await login(c)
            sid = (await c.post("/demo/sessions", headers=cookie)).json()["session_id"]
            resp = await c.post(f"/demo/sessions/{sid}/voice", headers=cookie)
        assert token.calls.last.request.headers["xi-api-key"] == API_KEY
    _assert_no_key(resp)
    body = resp.json()
    if path == "webrtc":
        assert resp.status_code == 200 and body["conversation_token"] == "conv-tok"
        assert body["transport"] == "webrtc" and body["max_duration_s"] == 600
    elif path == "websocket":
        assert resp.status_code == 200 and body["signed_url"] == "wss://x/y?t=1"
    else:
        assert resp.status_code == 503 and body["error"] == "voice_unavailable"


async def test_one_voice_slot_frees_after_the_max_duration(
    db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Catches: two concurrent live voice sessions (cost/abuse), and a slot that never frees
    # when a tab dies without calling DELETE.
    clock = [5000.0]
    monkeypatch.setattr(demo_api, "_now", lambda: clock[0])
    app = demo_app(
        db, elevenlabs_api_key=API_KEY, elevenlabs_agent_id="agent_1", demo_max_voice_seconds=60
    )
    with respx.mock as mock:
        mock.get(TOKEN_URL).mock(return_value=httpx.Response(200, json={"token": "t"}))
        async with client(app) as c:
            cookie = await login(c)
            a = (await c.post("/demo/sessions", headers=cookie)).json()["session_id"]
            b = (await c.post("/demo/sessions", headers=cookie)).json()["session_id"]
            assert (await c.post(f"/demo/sessions/{a}/voice", headers=cookie)).status_code == 200
            busy = await c.post(f"/demo/sessions/{b}/voice", headers=cookie)
            assert busy.status_code == 409 and busy.json()["error"] == "voice_busy"
            _assert_no_key(busy)
            # The holder may refresh its own credentials.
            assert (await c.post(f"/demo/sessions/{a}/voice", headers=cookie)).status_code == 200
            clock[0] += 59
            assert (await c.post(f"/demo/sessions/{b}/voice", headers=cookie)).status_code == 409
            clock[0] += 2  # past the 60 s cap measured from a's last acquire
            assert (await c.post(f"/demo/sessions/{b}/voice", headers=cookie)).status_code == 200
            # An explicit DELETE frees it at once.
            assert (await c.delete(f"/demo/sessions/{b}/voice", headers=cookie)).status_code == 204
            assert (await c.post(f"/demo/sessions/{a}/voice", headers=cookie)).status_code == 200


# --- 4. text chat and voice share one path --------------------------------------------------


async def _wait_for(session_id: str, type_: str, timeout: float = 5.0) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while True:
            for e in BUS.history(session_id):
                if e["type"] == type_:
                    return e
            await asyncio.sleep(0.01)


async def test_out_of_scope_refused_identically_over_text_and_voice(
    db: async_sessionmaker[AsyncSession],
) -> None:
    # Catches: the text-chat endpoint bypassing the Delegator's hooks/tools (e.g. calling the
    # model directly), so the scope gate that protects voice would not protect typed input.
    utterance = "send an email to my boss"
    reply = "Sorry, I can't do that yet; I only think things through and delegate research."
    app = demo_app(db)
    model: ScriptedChatModel = app.state.service.chat_model
    model.scripts = [text(reply), text(reply)]
    async with client(app) as c:
        cookie = await login(c)
        text_sid = (await c.post("/demo/sessions", headers=cookie)).json()["session_id"]
        resp = await c.post(
            f"/demo/sessions/{text_sid}/messages", json={"text": utterance}, headers=cookie
        )
        assert resp.status_code == 202 and resp.json()["turn_id"]
        await _wait_for(text_sid, "assistant_turn")
        await finalised(app)

        voice_sid = str(uuid.uuid4())
        body = {
            "model": "relay-delegator",
            "stream": True,
            "messages": [
                {"role": "system", "content": "You are relay."},
                {"role": "user", "content": utterance},
            ],
            "elevenlabs_extra_body": {"session_id": voice_sid},
        }
        voice = await c.post("/v1/chat/completions", json=body, headers=AUTH)
        spoken = "".join(
            ch["choices"][0]["delta"].get("content") or "" for ch in parse_sse(voice.content)
        )
        await finalised(app)

    text_call, voice_call = model.calls
    # Identical per-turn notes (scope refusal) and tools; only the caller's system prompt differs.
    assert text_call["messages"][1:] == voice_call["messages"][1:]
    assert text_call["tools"] == voice_call["tools"]
    assert any("out of scope" in m["content"] for m in voice_call["messages"][1:-1])

    def refusal(sid: str) -> dict[str, Any]:
        (e,) = (e for e in BUS.history(sid) if e["type"] == "scope_refusal")
        return {k: v for k, v in e["data"].items() if k != "turn_id"}

    assert refusal(text_sid) == refusal(voice_sid)
    assert refusal(text_sid)["utterance"] == utterance
    turns = {
        sid: next(e for e in BUS.history(sid) if e["type"] == "user_turn")["data"]["channel"]
        for sid in (text_sid, voice_sid)
    }
    assert turns == {text_sid: "text", voice_sid: "voice"}
    said = [
        next(e for e in BUS.history(sid) if e["type"] == "assistant_turn")["data"]["text"]
        for sid in (text_sid, voice_sid)
    ]
    assert said == [reply, reply] and spoken == reply
