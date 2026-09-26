"""Turn orchestration: session identity, hooks, the server-side tool loop, persistence.

From ElevenLabs' point of view this is the LLM. Internal tools run here and never
reach the SSE stream; only the model's speech and ElevenLabs' own system tools
(e.g. ``end_call``) are sent downstream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator import persistence
from relay.delegator.adapters.openai_compat import (
    chat_completion,
    chat_completion_chunk,
    content_text,
    tool_call_payload,
)
from relay.delegator.contracts import (
    SessionStore,
    ToolContext,
    ToolRegistry,
    TurnContext,
    TurnHook,
)
from relay.delegator.llm.base import ChatModel
from relay.delegator.sse import SSE_DONE, sse_frame

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_TOOL_ROUNDS = 5
APOLOGY_TEXT = (
    "Sorry, I'm having trouble thinking right now. Could you give me a moment and say that again?"
)
SESSION_HEADER = "x-relay-session-id"
_WARNED_SESSIONS_MAX = 1024
# How long a new request waits for the same session's previous turn to finish
# persisting / running after_response, so hooks observe turns in order.
PREVIOUS_TURN_WAIT_S = 1.0
# Upper bound for flushing detached finalisation on shutdown.
DRAIN_TIMEOUT_S = 10.0


class ChatCompletionRequest(BaseModel):
    """OpenAI chat-completions body as ElevenLabs sends it; unknown keys are kept, not rejected."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    elevenlabs_extra_body: dict[str, Any] | None = None
    session_id: str | None = None
    user_id: str | None = None


def _coerce_session_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, "relay-session:" + raw)


def _history_prefix_session_id(messages: list[dict[str, Any]]) -> uuid.UUID | None:
    """Deterministic id from the stable opening of the conversation.

    ElevenLabs resends the full history each turn, so the first system message, the
    greeting (an assistant message *before* the first user message, if any) and the
    first user message are identical on every request of one call. An assistant
    message after the first user message is deliberately excluded: it is absent on
    the first request and would split the session in two.
    """
    system = next(
        (content_text(m.get("content")) for m in messages if m.get("role") == "system"), None
    )
    greeting: str | None = None
    for message in messages:
        role = message.get("role")
        if role == "user":
            key = json.dumps([system, greeting, content_text(message.get("content"))])
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            return uuid.uuid5(uuid.NAMESPACE_URL, "relay-session-prefix:" + digest)
        if role == "assistant" and greeting is None:
            greeting = content_text(message.get("content"))
    return None


def resolve_session_id(
    body: ChatCompletionRequest, headers: Mapping[str, str]
) -> tuple[uuid.UUID, str]:
    """Return ``(session_id, source)``.

    Precedence: extra_body.session_id > top-level session_id > X-Relay-Session-Id >
    ElevenLabs conversation id > history prefix > fresh uuid4. ``source`` is
    ``"explicit"``, ``"history"`` or ``"fresh"``.
    """
    extra = body.elevenlabs_extra_body or {}
    for raw in (
        extra.get("session_id"),
        body.session_id,
        headers.get(SESSION_HEADER),
        extra.get("conversation_id"),
    ):
        if raw:
            return _coerce_session_id(str(raw)), "explicit"
    derived = _history_prefix_session_id(body.messages)
    if derived is not None:
        return derived, "history"
    return uuid.uuid4(), "fresh"


def _tool_name(tool: dict[str, Any]) -> str | None:
    fn = tool.get("function")
    name = fn.get("name") if isinstance(fn, dict) else None
    return name if isinstance(name, str) else None


@dataclass
class _Call:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _Turn:
    """Everything one request needs once prepared."""

    ctx: TurnContext
    upstream_messages: list[dict[str, Any]]
    all_tools: list[dict[str, Any]]
    external_tools: list[dict[str, Any]]
    temperature: float | None
    max_tokens: int | None
    tool_choice: Any
    received_at: float
    # Wall-clock moment the response started; the assistant row's ``ts``.
    response_started: datetime
    completion_id: str
    created: int
    model: str
    # Outcome, filled in while running.
    text_parts: list[str] = field(default_factory=list)
    external_calls: list[_Call] = field(default_factory=list)
    model_used: str | None = None
    first_token_at: float | None = None
    # True once the model's answer finished streaming (vs. a barge-in / disconnect).
    completed: bool = False


def _followup_tool_choice(tool_choice: Any) -> Any:
    """``tool_choice`` for rounds after an internal tool call.

    Re-forcing ``required`` or a specific function every round would loop until the cap.
    """
    if tool_choice is None or tool_choice == "none":
        return tool_choice
    return "auto"


class DelegatorService:
    def __init__(
        self,
        *,
        settings: Settings,
        chat_model: ChatModel,
        registry: ToolRegistry,
        hooks: list[TurnHook],
        session_store: SessionStore,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self.chat_model = chat_model
        self.registry = registry
        self.hooks = hooks
        self.session_store = session_store
        self.db = sessionmaker
        # Finalisation runs detached so a client disconnect cannot cancel it; the strong
        # references keep the tasks alive until done.
        self._background: set[asyncio.Task[None]] = set()
        # Latest finalisation per session; the next request of that session waits on it.
        self._finalizing: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._warned_sessions: OrderedDict[uuid.UUID, None] = OrderedDict()

    def _warn_derived_session(self, session_id: uuid.UUID, source: str) -> None:
        if source == "fresh":
            log.warning("chat request carries no session id; using fresh session %s", session_id)
            return
        if session_id in self._warned_sessions:
            self._warned_sessions.move_to_end(session_id)
            return
        self._warned_sessions[session_id] = None
        if len(self._warned_sessions) > _WARNED_SESSIONS_MAX:
            self._warned_sessions.popitem(last=False)
        log.warning(
            "chat request carries no session id; derived session %s from the history prefix",
            session_id,
        )

    async def drain(self, timeout: float = DRAIN_TIMEOUT_S) -> None:
        """Wait (bounded) for detached turn finalisation; call on shutdown."""
        if not self._background:
            return
        _, pending = await asyncio.wait(set(self._background), timeout=timeout)
        if pending:
            log.warning(
                "delegator: abandoning %d unfinished turn finalisations after %.0fs: %s",
                len(pending),
                timeout,
                sorted(t.get_name() for t in pending),
            )
            for task in pending:
                task.cancel()

    async def _await_previous_turn(self, session_id: uuid.UUID) -> None:
        previous = self._finalizing.get(session_id)
        if previous is None or previous.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(previous), PREVIOUS_TURN_WAIT_S)
        except TimeoutError:
            log.warning(
                "delegator: previous turn of session %s still finalising after %.1fs; "
                "continuing without it",
                session_id,
                PREVIOUS_TURN_WAIT_S,
            )
        except Exception:
            log.exception("delegator: previous turn finalisation of %s failed", session_id)

    def _spawn_finalize(self, turn: _Turn) -> None:
        session_id = turn.ctx.state.session_id
        task = asyncio.create_task(self._finalize(turn), name=f"finalize:{session_id}")
        self._background.add(task)
        self._finalizing[session_id] = task

        def _done(t: asyncio.Task[None]) -> None:
            self._background.discard(t)
            if self._finalizing.get(session_id) is t:
                del self._finalizing[session_id]

        task.add_done_callback(_done)

    async def _safe(self, what: str, op: Awaitable[T]) -> T | None:
        """Await a DB write; log and swallow failures so the voice turn keeps going."""
        try:
            return await op
        except Exception:
            log.exception("delegator: %s failed", what)
            return None

    async def prepare(
        self, body: ChatCompletionRequest, headers: Mapping[str, str], received_at: float
    ) -> _Turn:
        session_id, source = resolve_session_id(body, headers)
        if source != "explicit":
            self._warn_derived_session(session_id, source)
        await self._await_previous_turn(session_id)
        state = self.session_store.get(session_id)
        messages = list(body.messages)
        user_idx = next(
            (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
            None,
        )
        user_text = content_text(messages[user_idx].get("content")) if user_idx is not None else ""

        user_turn_id: uuid.UUID | None = None
        if messages and messages[-1].get("role") == "user":
            state.user_turn_index += 1
            user_turn_id = await self._safe(
                "persisting user turn",
                persistence.record_turn(
                    self.db,
                    session_id=session_id,
                    role="user",
                    text=user_text,
                    idea_id=state.current_idea_id,
                    ts=datetime.now(UTC),
                ),
            )
        else:
            await self._safe("upserting session", persistence.ensure_session(self.db, session_id))

        ctx = TurnContext(
            state=state,
            db=self.db,
            settings=self.settings,
            messages=body.messages,
            user_text=user_text,
            user_turn_id=user_turn_id,
        )
        notes: list[str] = []
        for hook in self.hooks:
            try:
                notes.extend(await hook.before_model(ctx))
            except Exception:
                log.exception("delegator: hook %r before_model failed", hook)
        note_messages = [{"role": "system", "content": n} for n in notes if n]
        insert_at = user_idx if user_idx is not None else len(messages)
        upstream = messages[:insert_at] + note_messages + messages[insert_at:]

        internal_tools = self.registry.openai_tools()
        external_tools = [
            t for t in body.tools or [] if (name := _tool_name(t)) and name not in self.registry
        ]
        return _Turn(
            ctx=ctx,
            upstream_messages=upstream,
            all_tools=internal_tools + external_tools,
            external_tools=external_tools,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            tool_choice=body.tool_choice,
            received_at=received_at,
            response_started=datetime.now(UTC),
            completion_id=f"chatcmpl-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=body.model or self.chat_model.model_name,
        )

    async def _run(self, turn: _Turn) -> AsyncIterator[str | list[_Call]]:
        """Yield speech text as it streams, then at most one list of external tool calls."""
        messages = turn.upstream_messages
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            last_round = round_no == MAX_TOOL_ROUNDS
            tools = turn.external_tools if last_round else turn.all_tools
            choice = turn.tool_choice if round_no == 0 else _followup_tool_choice(turn.tool_choice)
            calls: dict[int, _Call] = {}
            round_text: list[str] = []
            try:
                async for delta in self.chat_model.stream(
                    messages,
                    tools or None,
                    temperature=turn.temperature,
                    max_tokens=turn.max_tokens,
                    tool_choice=choice if tools else None,
                ):
                    if delta.model:
                        turn.model_used = delta.model
                    if delta.content:
                        if turn.first_token_at is None:
                            turn.first_token_at = time.perf_counter()
                        round_text.append(delta.content)
                        turn.text_parts.append(delta.content)
                        yield delta.content
                    for frag in delta.tool_calls:
                        call = calls.setdefault(frag.index, _Call())
                        if frag.id and not call.id:
                            call.id = frag.id
                        if frag.name and not call.name:
                            call.name = frag.name
                        if frag.arguments:
                            call.arguments += frag.arguments
            except Exception:
                log.exception("delegator: upstream model failed")
                if not turn.text_parts:
                    turn.text_parts.append(APOLOGY_TEXT)
                    yield APOLOGY_TEXT
                return

            ordered = [calls[i] for i in sorted(calls) if calls[i].name]
            for call in ordered:
                call.id = call.id or f"call_{uuid.uuid4().hex[:24]}"
            internal = [c for c in ordered if c.name in self.registry]
            external = [c for c in ordered if c.name not in self.registry]

            if internal and not last_round:
                if external:
                    log.warning(
                        "dropping external calls %s issued alongside internal tools",
                        [c.name for c in external],
                    )
                messages = messages + [
                    {
                        "role": "assistant",
                        "content": "".join(round_text) or None,
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {"name": c.name, "arguments": c.arguments},
                            }
                            for c in internal
                        ],
                    }
                ]
                for call in internal:
                    result = await self._execute(call, turn)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                continue
            if internal:
                log.warning("tool-round cap reached; ignoring internal calls %s", internal)
            if external:
                turn.external_calls = external
                yield external
            return

    async def _execute(self, call: _Call, turn: _Turn) -> str:
        tool = self.registry.get(call.name)
        if tool is None:  # pragma: no cover - guarded by the caller
            return f"Error: unknown tool {call.name}."
        try:
            args = json.loads(call.arguments or "{}")
        except ValueError:
            args = None
        if not isinstance(args, dict):
            return (
                f"Error: the arguments for {call.name} were not a valid JSON object. "
                "Call the tool again with arguments matching its schema."
            )
        ctx = ToolContext(
            state=turn.ctx.state,
            db=self.db,
            settings=self.settings,
            user_turn_id=turn.ctx.user_turn_id,
        )
        try:
            result = await tool(args, ctx)
        except Exception:
            log.exception("delegator: internal tool %s failed", call.name)
            return f"Error: {call.name} failed internally. Tell the user briefly and move on."
        return result.content

    async def stream_sse(self, turn: _Turn) -> AsyncIterator[bytes]:
        """The SSE body: content/tool-call chunks, a finish chunk, ``[DONE]``.

        Persistence and ``after_response`` are handed to a detached task in ``finally``:
        on a client disconnect (barge-in) Starlette cancels this generator, and anything
        awaited here would be cancelled with it.
        """
        role_sent = False

        def frame(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
            nonlocal role_sent
            if not role_sent:
                delta = {"role": "assistant", **delta}
                role_sent = True
            return sse_frame(
                chat_completion_chunk(
                    completion_id=turn.completion_id,
                    created=turn.created,
                    model=turn.model,
                    delta=delta,
                    finish_reason=finish_reason,
                )
            )

        try:
            async for event in self._run(turn):
                if isinstance(event, str):
                    yield frame({"content": event})
                else:
                    for i, call in enumerate(event):
                        payload = tool_call_payload(i, call.id, call.name, call.arguments)
                        yield frame({"tool_calls": [payload]})
            turn.completed = True
            yield frame({}, "tool_calls" if turn.external_calls else "stop")
            yield SSE_DONE
        finally:
            # Synchronous on purpose: no await may run in a cancelled generator.
            self._spawn_finalize(turn)

    async def complete(self, turn: _Turn) -> dict[str, Any]:
        """Non-streaming ``chat.completion`` response."""
        async for _ in self._run(turn):
            pass
        turn.completed = True
        text = "".join(turn.text_parts)
        calls = [
            tool_call_payload(i, c.id, c.name, c.arguments)
            for i, c in enumerate(turn.external_calls)
        ]
        response = chat_completion(
            completion_id=turn.completion_id,
            created=turn.created,
            model=turn.model,
            content=text or None,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else "stop",
        )
        await self._finalize(turn)
        return response

    async def _finalize(self, turn: _Turn) -> None:
        text = "".join(turn.text_parts)
        latency_ms = (
            round((turn.first_token_at - turn.received_at) * 1000)
            if turn.first_token_at is not None
            else None
        )
        model_used = turn.model_used or self.chat_model.model_name
        state = turn.ctx.state
        log.info(
            "delegator turn session=%s ttft_ms=%s model=%s",
            state.session_id,
            latency_ms,
            model_used,
        )
        meta: dict[str, Any] = {}
        if not turn.completed:
            meta["interrupted"] = True
        if turn.external_calls:
            meta["tool_calls"] = [c.name for c in turn.external_calls]
        await self._safe(
            "persisting assistant turn",
            persistence.record_turn(
                self.db,
                session_id=state.session_id,
                role="assistant",
                text=text,
                idea_id=state.current_idea_id,
                route="frontier",
                model_used=model_used,
                latency_ms=latency_ms,
                meta=meta,
                ts=turn.response_started,
            ),
        )
        for hook in self.hooks:
            try:
                await hook.after_response(turn.ctx, text)
            except Exception:
                log.exception("delegator: hook %r after_response failed", hook)
