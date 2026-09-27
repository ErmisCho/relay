"""Demo API under ``/demo`` (TASK-42; contract: ``web/CONTRACT.md``, ``web/src/api/contract.ts``).

Mounted by :func:`install` only when ``DEMO_PASSCODE`` is set; otherwise no ``/demo`` route
exists (404). Every route except ``POST /demo/auth`` needs the signed ``relay_demo`` cookie
(``EventSource`` cannot send headers). Text chat goes through ``DelegatorService.prepare`` +
``stream_sse`` exactly like a voice turn, so the same tools, hooks, scope gate and commitment
protocol apply.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator import persistence
from relay.delegator.contracts import TurnContext
from relay.delegator.demo.events import BUS, emit, iso_ms
from relay.delegator.service import CHANNEL_HEADER, ChatCompletionRequest, DelegatorService
from relay.store.models import Artifact, Commitment, Idea, IdeaEdge, PendingReport, Task
from relay.store.models import Session as SessionRow

log = logging.getLogger(__name__)

COOKIE = "relay_demo"
COOKIE_TTL_S = 12 * 3600
#: Wrong passcodes allowed per client address per window before 429.
AUTH_MAX_FAILURES = 10
AUTH_WINDOW_S = 60.0
KEEPALIVE_S = 15.0
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
ELEVENLABS_API = "https://api.elevenlabs.io"
#: ElevenLabs does not document a lifetime we can rely on; 10 minutes is conservative.
CREDENTIALS_TTL_S = 600
#: Max polls to wait for a succeeded task's artifact row before giving up on it.
ARTIFACT_WAIT_POLLS = 40
TEXT_SYSTEM_PROMPT = (
    "You are relay, a voice-first thinking partner. This conversation is typed in the demo "
    "website instead of spoken; reply exactly as you would out loud. Your job is to gather the "
    "user's thoughts and, once a task is clear enough, hand it to the executor: never say you "
    "can't do work the executor can do (looking into, checking or inspecting something, "
    "including this computer, writing, or changing code); read the commitment back instead."
)
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _now() -> float:
    """Monotonic clock for the voice slot and the auth rate limit (tests patch this, not
    ``time.monotonic``, which the event loop itself relies on)."""
    return time.monotonic()


class DemoError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


def _error_response(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, DemoError)
    body: dict[str, str] = {"error": exc.code}
    if exc.message:
        body["message"] = exc.message
    return JSONResponse(body, status_code=exc.status)


# --- access control ----------------------------------------------------------------------


def _cookie_key(settings: Settings) -> bytes:
    # Changing the passcode (or the shared secret) invalidates every issued cookie.
    seed = f"relay-demo:{settings.demo_passcode}:{settings.delegator_shared_secret}"
    return hashlib.sha256(seed.encode("utf-8")).digest()


def make_cookie(settings: Settings, now: float) -> str:
    issued = str(int(now))
    sig = hmac.new(_cookie_key(settings), issued.encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{sig}"


def cookie_valid(settings: Settings, value: str | None, now: float) -> bool:
    issued, _, sig = (value or "").partition(".")
    if not issued.isdigit() or not sig:
        return False
    expected = hmac.new(_cookie_key(settings), issued.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected) and 0 <= now - int(issued) <= COOKIE_TTL_S


# --- voice slot ----------------------------------------------------------------------------


@dataclass
class VoiceSlot:
    """The single live voice session; expires on its own after ``max_s``."""

    max_s: int
    holder: str | None = None
    deadline: float = 0.0
    #: session id -> voice deadline, kept briefly after expiry for :class:`VoiceLimitHook`.
    deadlines: dict[str, float] = field(default_factory=dict)

    def busy_for(self, session_id: str, now: float) -> bool:
        return self.holder is not None and self.holder != session_id and now < self.deadline

    def acquire(self, session_id: str, now: float) -> None:
        self.holder, self.deadline = session_id, now + self.max_s
        self.deadlines[session_id] = self.deadline

    def release(self, session_id: str) -> None:
        if self.holder == session_id:
            self.holder = None
        self.deadlines.pop(session_id, None)


class VoiceLimitHook:
    """Turn hook: once a demo voice session is past its cap, tell the agent to end the call.

    The browser ends the call at the cap and the slot frees itself; this covers a tab that keeps
    the call open. Best effort: it relies on the agent calling ElevenLabs' ``end_call``.
    """

    NOTE = (
        "DEMO TIME LIMIT: this demo voice session has reached its maximum length. Say goodbye "
        "in one short sentence and call end_call now."
    )
    #: Stop nagging this long after the cap (the call has surely ended by then).
    GRACE_S = 120.0

    def __init__(self, slot: VoiceSlot) -> None:
        self.slot = slot

    async def before_model(self, ctx: TurnContext) -> list[str]:
        sid = str(ctx.state.session_id)
        deadline = self.slot.deadlines.get(sid)
        now = _now()
        if deadline is None or now < deadline:
            return []
        if now > deadline + self.GRACE_S:
            self.slot.deadlines.pop(sid, None)
            return []
        return [self.NOTE]

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        return None


async def fetch_voice_credentials(settings: Settings) -> dict[str, Any]:
    """A WebRTC conversation token (preferred) or, failing that, a signed WebSocket URL.

    The API key only ever goes into the outbound request header; the returned dict holds just
    the token/URL.
    """
    assert settings.elevenlabs_api_key and settings.elevenlabs_agent_id
    headers = {"xi-api-key": settings.elevenlabs_api_key}
    params = {"agent_id": settings.elevenlabs_agent_id}
    async with httpx.AsyncClient(base_url=ELEVENLABS_API, timeout=10.0) as client:
        resp = await client.get("/v1/convai/conversation/token", params=params, headers=headers)
        if resp.status_code == 200 and resp.json().get("token"):
            return {"transport": "webrtc", "conversation_token": str(resp.json()["token"])}
        log.warning("demo: conversation token request failed (%d)", resp.status_code)
        resp = await client.get(
            "/v1/convai/conversation/get-signed-url", params=params, headers=headers
        )
        resp.raise_for_status()
        return {"transport": "websocket", "signed_url": str(resp.json()["signed_url"])}


def agent_url_mismatch(agent: dict[str, Any], public_url: str) -> str | None:
    """Why voice turns would bypass this Delegator, or None when the agent calls it.

    ElevenLabs calls the agent's ``custom_llm.url``; if that is another tunnel (an old one, or a
    teammate's), voice gets answers from a different Delegator: nothing reaches this server's
    event feed and nothing is dispatched here, while typed text still works.
    """
    prompt = agent.get("conversation_config", {}).get("agent", {}).get("prompt", {})
    url = str((prompt.get("custom_llm") or {}).get("url") or "")
    if prompt.get("llm") != "custom-llm" or not url:
        return "the ElevenLabs agent does not use a custom LLM"
    if not url.rstrip("/").removesuffix("/v1").startswith(public_url.rstrip("/")):
        return f"the ElevenLabs agent calls {url}, not DELEGATOR_PUBLIC_URL {public_url}"
    return None


async def check_agent_url(settings: Settings) -> None:
    """Log a warning when the voice agent would call another Delegator (never raises)."""
    if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id):
        return
    try:
        async with httpx.AsyncClient(base_url=ELEVENLABS_API, timeout=10.0) as client:
            resp = await client.get(
                f"/v1/convai/agents/{settings.elevenlabs_agent_id}",
                headers={"xi-api-key": settings.elevenlabs_api_key},
            )
            resp.raise_for_status()
            problem = agent_url_mismatch(resp.json(), settings.delegator_public_url)
    except Exception as exc:
        log.warning("demo: could not check the ElevenLabs agent URL: %s", type(exc).__name__)
        return
    if problem is not None:
        log.warning(
            "demo: VOICE WILL NOT REACH THIS DELEGATOR: %s. Run "
            "`uv run python scripts/apply_agent_config.py --apply`.",
            problem,
        )


# --- helpers -------------------------------------------------------------------------------


def _iso(ts: datetime | None) -> str | None:
    return iso_ms(ts) if ts is not None else None


def _parse_session(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise DemoError(404, "not_found") from None


def artifact_markdown(url: str, artifacts_dir: str) -> str | None:
    """The artifact's Markdown if its ``file://`` URL points inside ``artifacts_dir``."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    path = Path(url2pathname(parsed.path)).resolve()
    root = Path(artifacts_dir).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def markdown_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def markdown_sources(markdown: str) -> list[dict[str, str]]:
    """Cited links, derived from the Markdown (the store has no structured sources)."""
    seen: dict[str, str] = {}
    for title, url in _MD_LINK.findall(markdown):
        seen.setdefault(url, title.strip())
    return [{"title": t, "url": u} for u, t in seen.items()]


class _AuthBody(BaseModel):
    passcode: str


class _MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


async def _body(request: Request, model: type[BaseModel]) -> Any:
    try:
        return model.model_validate(await request.json())
    except (ValidationError, ValueError):
        raise DemoError(422, "invalid_request") from None


# --- runtime -------------------------------------------------------------------------------


class DemoRuntime:
    def __init__(
        self,
        settings: Settings,
        service: DelegatorService,
        db: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.service = service
        self.db = db
        self.slot = VoiceSlot(max_s=settings.demo_max_voice_seconds)
        self._failures: dict[str, deque[float]] = {}
        self._histories: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._poller: asyncio.Task[None] | None = None
        self._artifact_polls: dict[uuid.UUID, int] = {}

    # lifecycle
    def start(self) -> None:
        self._poller = asyncio.create_task(self._poll_forever(), name="demo-task-poller")
        check = asyncio.create_task(check_agent_url(self.settings), name="demo-agent-url")
        self._background.add(check)
        check.add_done_callback(self._background.discard)

    async def stop(self) -> None:
        tasks = [*self._background, *([self._poller] if self._poller else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def rate_limited(self, client: str, now: float) -> bool:
        window = self._failures.setdefault(client, deque())
        while window and now - window[0] > AUTH_WINDOW_S:
            window.popleft()
        return len(window) >= AUTH_MAX_FAILURES

    def record_failure(self, client: str, now: float) -> None:
        self._failures.setdefault(client, deque()).append(now)

    async def require_session(self, raw: str) -> uuid.UUID:
        sid = _parse_session(raw)
        async with self.db() as s:
            if await s.get(SessionRow, sid) is None:
                raise DemoError(404, "not_found")
        return sid

    # text chat: the same Delegator path as a voice turn
    async def send_text(self, sid: uuid.UUID, text: str) -> str | None:
        key = str(sid)
        lock = self._locks.setdefault(key, asyncio.Lock())
        await lock.acquire()  # one text turn at a time per session; released by _reply
        try:
            history = self._histories.setdefault(
                key, [{"role": "system", "content": TEXT_SYSTEM_PROMPT}]
            )
            messages = [*history, {"role": "user", "content": text}]
            body = ChatCompletionRequest(
                model="relay-demo-text",
                messages=messages,
                stream=True,
                elevenlabs_extra_body={"session_id": key},
            )
            turn = await self.service.prepare(body, {CHANNEL_HEADER: "text"}, time.perf_counter())
        except BaseException:
            lock.release()
            raise
        task = asyncio.create_task(self._reply(key, messages, turn, lock), name=f"demo-text:{key}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        user_turn_id = turn.ctx.user_turn_id
        return str(user_turn_id) if user_turn_id is not None else None

    async def _reply(
        self, key: str, messages: list[dict[str, Any]], turn: Any, lock: asyncio.Lock
    ) -> None:
        try:
            async for _ in self.service.stream_sse(turn):
                pass  # the reply reaches the browser as the assistant_turn event
        except Exception:
            log.exception("demo: text turn in session %s failed", key)
        finally:
            spoken = "".join(turn.text_parts)
            self._histories[key] = [*messages, {"role": "assistant", "content": spoken}]
            lock.release()

    # task status / artifacts from the executor worker (a separate process)
    async def _poll_forever(self) -> None:
        # Polling ``tasks`` every DEMO_TASK_POLL_S for the tasks dispatched from this process is
        # fine for a demo with a few sessions; switch to Postgres LISTEN/NOTIFY if it grows.
        while True:
            await asyncio.sleep(self.settings.demo_task_poll_s)
            if BUS.watched_tasks:
                try:
                    await self.poll_once()
                except Exception:
                    log.warning("demo: task poll failed", exc_info=True)

    async def poll_once(self) -> None:
        watched = dict(BUS.watched_tasks)
        async with self.db() as s:
            rows = (await s.execute(select(Task).where(Task.id.in_(watched)))).scalars().all()
            artifacts = {
                a.task_id: a
                for a in (
                    await s.scalars(select(Artifact).where(Artifact.task_id.in_(watched)))
                ).all()
            }
            # Build the dict from rows explicitly: ``dict(result)`` sees ``Result.keys()`` (the
            # column names) and then subscripts the result, which raised on every poll, so no
            # task_status / artifact_delivered ever reached the feed.
            idea_ids = {
                task_id: idea_id
                for task_id, idea_id in (
                    await s.execute(
                        select(Task.id, Commitment.idea_id)
                        .join(Commitment, Commitment.id == Task.commitment_id)
                        .where(Task.id.in_(watched))
                    )
                ).all()
            }
        for task in rows:
            sid, last = watched[task.id]
            if task.status != last:
                emit(
                    sid,
                    "task_status",
                    {"task_id": str(task.id), "status": task.status, "error": task.error},
                )
                BUS.watched_tasks[task.id] = (sid, task.status)
            if task.status == "failed":
                BUS.watched_tasks.pop(task.id, None)
            elif task.status == "succeeded":
                artifact = artifacts.get(task.id)
                polls = self._artifact_polls.get(task.id, 0) + 1
                self._artifact_polls[task.id] = polls
                if artifact is None and polls < ARTIFACT_WAIT_POLLS:
                    continue
                BUS.watched_tasks.pop(task.id, None)
                self._artifact_polls.pop(task.id, None)
                if artifact is not None:
                    emit(sid, "artifact_delivered", self._artifact_event(artifact, idea_ids))

    def _artifact_event(
        self, artifact: Artifact, idea_ids: dict[uuid.UUID, uuid.UUID]
    ) -> dict[str, Any]:
        markdown = artifact_markdown(artifact.url, self.settings.artifacts_dir) or ""
        idea_id = idea_ids.get(artifact.task_id)
        return {
            "artifact_id": str(artifact.id),
            "task_id": str(artifact.task_id),
            "idea_id": str(idea_id) if idea_id is not None else None,
            "title": markdown_title(markdown, artifact.summary or "Brief"),
            "summary": artifact.summary or "",
            "markdown": markdown,
            "sources": markdown_sources(markdown),
        }


# --- routes --------------------------------------------------------------------------------


def _sse_event(event: dict[str, Any]) -> bytes:
    body = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    return f"id: {event['seq']}\ndata: {body}\n\n".encode()


def build_router(rt: DemoRuntime) -> APIRouter:
    settings = rt.settings

    async def require_cookie(request: Request) -> None:
        if not cookie_valid(settings, request.cookies.get(COOKIE), time.time()):
            raise DemoError(401, "unauthorized")

    open_router = APIRouter(prefix="/demo")
    router = APIRouter(prefix="/demo", dependencies=[Depends(require_cookie)])

    @open_router.post("/auth", status_code=204)
    async def login(request: Request) -> Response:
        client = request.client.host if request.client else "unknown"
        now = _now()
        if rt.rate_limited(client, now):
            raise DemoError(429, "rate_limited")
        body = await _body(request, _AuthBody)
        if not hmac.compare_digest(
            body.passcode.encode("utf-8"), settings.demo_passcode.encode("utf-8")
        ):
            rt.record_failure(client, now)
            raise DemoError(401, "unauthorized")
        secure = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https"
        )
        resp = Response(status_code=204)
        resp.set_cookie(
            COOKIE,
            make_cookie(settings, time.time()),
            max_age=COOKIE_TTL_S,
            path="/demo",
            httponly=True,
            samesite="lax",
            secure=secure,
        )
        return resp

    @open_router.get("/auth", status_code=204)
    async def check(request: Request) -> Response:
        await require_cookie(request)
        return Response(status_code=204)

    @router.post("/sessions", status_code=201)
    async def create_session() -> dict[str, Any]:
        sid = uuid.uuid4()
        await persistence.ensure_session(rt.db, sid)
        return {
            "session_id": str(sid),
            "created_at": iso_ms(datetime.now(UTC)),
            "voice_max_seconds": settings.demo_max_voice_seconds,
        }

    @router.post("/sessions/{session_id}/messages", status_code=202)
    async def send_message(session_id: str, request: Request) -> dict[str, Any]:
        body = await _body(request, _MessageBody)
        sid = await rt.require_session(session_id)
        return {"turn_id": await rt.send_text(sid, body.text)}

    @router.post("/sessions/{session_id}/voice")
    async def start_voice(session_id: str) -> dict[str, Any]:
        sid = str(await rt.require_session(session_id))
        if not (settings.elevenlabs_api_key and settings.elevenlabs_agent_id):
            raise DemoError(503, "voice_unavailable", "Voice is not configured on this server.")
        now = _now()
        if rt.slot.busy_for(sid, now):
            raise DemoError(409, "voice_busy", "Another voice session is live. Try again later.")
        rt.slot.acquire(sid, now)  # reserve before the await so a second caller sees it busy
        try:
            creds = await fetch_voice_credentials(settings)
        except Exception as exc:
            rt.slot.release(sid)
            log.warning("demo: voice credential request failed: %s", type(exc).__name__)
            raise DemoError(
                503, "voice_unavailable", "Could not start a voice session right now."
            ) from None
        expires = datetime.now(UTC) + timedelta(seconds=CREDENTIALS_TTL_S)
        return {**creds, "expires_at": iso_ms(expires), "max_duration_s": rt.slot.max_s}

    @router.delete("/sessions/{session_id}/voice", status_code=204)
    async def end_voice(session_id: str) -> Response:
        rt.slot.release(session_id)
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/events")
    async def events(session_id: str, request: Request, after: int = 0) -> StreamingResponse:
        sid = str(await rt.require_session(session_id))
        last_id = request.headers.get("last-event-id", "")
        start = int(last_id) if last_id.isdigit() else after

        async def stream() -> AsyncIterator[bytes]:
            queue = BUS.subscribe(sid)  # before replay, so nothing falls in between
            sent = start
            try:
                for event in BUS.history(sid, start):
                    sent = event["seq"]
                    yield _sse_event(event)
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    if event["seq"] > sent:
                        sent = event["seq"]
                        yield _sse_event(event)
            finally:
                BUS.unsubscribe(sid, queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    @router.get("/ideas")
    async def ideas() -> dict[str, Any]:
        async with rt.db() as s:
            rows = (await s.scalars(select(Idea).order_by(Idea.updated_at.desc()))).all()
            edges = (await s.scalars(select(IdeaEdge))).all()
        return {
            "ideas": [
                {
                    "id": str(i.id),
                    "title": i.title,
                    "summary": i.summary,
                    "status": i.status,
                    "created_at": _iso(i.created_at),
                    "updated_at": _iso(i.updated_at),
                }
                for i in rows
            ],
            "edges": [
                {"from_idea": str(e.from_idea), "to_idea": str(e.to_idea), "relation": e.relation}
                for e in edges
            ],
        }

    @router.get("/commitments")
    async def commitments() -> dict[str, Any]:
        async with rt.db() as s:
            rows = (
                await s.scalars(select(Commitment).order_by(Commitment.created_at.desc()))
            ).all()
        return {
            "commitments": [
                {
                    "id": str(c.id),
                    "idea_id": str(c.idea_id),
                    "goal": c.goal,
                    "scope_excludes": c.scope_excludes,
                    "artifact_kind": c.artifact_kind,
                    "readback_text": c.readback_text,
                    "assent_utterance": c.assent_utterance,
                    "assented_at": _iso(c.assented_at),
                    "created_at": _iso(c.created_at),
                }
                for c in rows
            ]
        }

    @router.get("/tasks")
    async def tasks(session_id: str | None = None) -> dict[str, Any]:
        query = select(Task, Artifact.id).outerjoin(Artifact, Artifact.task_id == Task.id)
        if session_id is not None:
            sid = _parse_session(session_id)
            # Tasks carry no session id: use the dispatches this process saw for the session,
            # plus report rows the executor attributed to it.
            ids = {
                uuid.UUID(e["data"]["task_id"])
                for e in BUS.history(str(sid))
                if e["type"] == "dispatch"
            }
            async with rt.db() as s:
                ids |= set(
                    await s.scalars(
                        select(PendingReport.task_id).where(PendingReport.session_id == sid)
                    )
                )
            query = query.where(Task.id.in_(ids))
        async with rt.db() as s:
            rows = (await s.execute(query.order_by(Task.created_at.desc()))).all()
        return {
            "tasks": [
                {
                    "id": str(t.id),
                    "commitment_id": str(t.commitment_id),
                    "kind": t.kind,
                    "status": t.status,
                    "dbos_workflow_id": t.dbos_workflow_id,
                    "started_at": _iso(t.started_at),
                    "finished_at": _iso(t.finished_at),
                    "error": t.error,
                    "artifact_id": str(a) if a is not None else None,
                }
                for t, a in rows
            ]
        }

    @router.get("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str) -> dict[str, Any]:
        aid = _parse_session(artifact_id)
        async with rt.db() as s:
            row = (
                await s.execute(
                    select(Artifact, Commitment.idea_id)
                    .join(Task, Task.id == Artifact.task_id)
                    .join(Commitment, Commitment.id == Task.commitment_id)
                    .where(Artifact.id == aid)
                )
            ).first()
        if row is None:
            raise DemoError(404, "not_found")
        art, idea_id = row
        event = rt._artifact_event(art, {art.task_id: idea_id})
        return {
            "id": event["artifact_id"],
            "task_id": event["task_id"],
            "idea_id": event["idea_id"],
            "kind": art.kind,
            "title": event["title"],
            "summary": art.summary,
            "markdown": event["markdown"],
            "sources": event["sources"],
            "created_at": _iso(art.created_at),
        }

    combined = APIRouter()
    combined.include_router(open_router)
    combined.include_router(router)
    return combined


def install(
    app: FastAPI,
    settings: Settings,
    service: DelegatorService,
    db: async_sessionmaker[AsyncSession],
) -> DemoRuntime | None:
    """Add the demo routes, the voice-limit hook and the event bus; None when disabled."""
    BUS.enabled = bool(settings.demo_passcode)
    if not settings.demo_passcode:
        return None
    rt = DemoRuntime(settings, service, db)
    service.hooks = [*service.hooks, VoiceLimitHook(rt.slot)]
    app.add_exception_handler(DemoError, _error_response)
    app.include_router(build_router(rt))
    return rt
