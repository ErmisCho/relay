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
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy import literal, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator import persistence
from relay.delegator.adapters.openai_compat import (
    chat_completion,
    chat_completion_chunk,
    content_text,
    tool_call_payload,
)
from relay.delegator.capabilities import get_capabilities
from relay.delegator.contracts import (
    SessionStore,
    SystemPrefixProvider,
    ToolContext,
    ToolRegistry,
    TurnContext,
    TurnHook,
)
from relay.delegator.demo.events import emit
from relay.delegator.hooks.router import ActiveRoute, RouterHook
from relay.delegator.llm.base import ChatModel
from relay.delegator.llm.factory import build_local_model, with_normal_fallback
from relay.delegator.sse import SSE_DONE, sse_frame
from relay.store.models import Turn

log = logging.getLogger(__name__)

T = TypeVar("T")

MAX_TOOL_ROUNDS = 5
APOLOGY_TEXT = (
    "Sorry, I'm having trouble thinking right now. Could you give me a moment and say that again?"
)
#: Spoken when the model returns nothing at all, even after retries.
EMPTY_REPLY_TEXT = "Sorry, could you say that again?"
SESSION_HEADER = "x-relay-session-id"
#: Set to ``text`` by the demo text-chat endpoint; anything else is a voice turn.
CHANNEL_HEADER = "x-relay-channel"
_WARNED_SESSIONS_MAX = 1024
# How long a new request waits for the same session's previous turn to finish
# persisting / running after_response, so hooks observe turns in order.
PREVIOUS_TURN_WAIT_S = 1.0
# Upper bound for flushing detached finalisation on shutdown.
DRAIN_TIMEOUT_S = 10.0
# Optional context I/O must not leave a voice request waiting on database timeouts.
CONTEXT_IO_TIMEOUT_S = 0.5
HARDWARE_TOOL_NOTE = (
    "For questions about this computer's specs, processor, GPU, or RAM, call "
    "hardware_capabilities before answering. It reads the machine running Relay's backend, "
    "which may differ from the voice client. Report only returned facts. This read-only "
    "lookup needs no commitment or background task."
)


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


# ``SessionState.extra`` keys the service maintains for hooks and tools (provisional; the
# coordinator will formalise them in ``contracts``):
#: True when the current request re-sends the previous user turn (same history before the last
#: user message; same or extended utterance). The turn keeps its ``user_turn_index`` and row.
RESENT_KEY = "turn.resent"
#: On a re-sent turn: the utterance of the request it replaces.
PREV_TEXT_KEY = "turn.prev_text"
#: ``{user_turn_index: bool}``, set just before ``after_response``: whether that user turn's
#: latest request finished streaming (False on a barge-in / client disconnect). Keyed per turn so
#: overlapping finalisations of different turns cannot overwrite each other.
COMPLETED_KEY = "turn.completed"
#: Internal: fingerprint of the session's latest user request, for re-send detection.
LAST_USER_REQUEST_KEY = "turn.last_user_request"


@dataclass(frozen=True)
class _UserRequest:
    history_digest: str
    text: str
    turn_id: uuid.UUID | None


def _history_digest(messages: list[dict[str, Any]]) -> str:
    key = json.dumps([[m.get("role"), content_text(m.get("content"))] for m in messages])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _words(text: str) -> list[str]:
    return re.sub(r"[^0-9a-z' ]+", " ", text.lower().replace("’", "'")).replace("'", "").split()


def _is_resend(previous: _UserRequest, digest: str, text: str) -> bool:
    """Same history before the user message, and the new utterance equals or extends the old."""
    if previous.history_digest != digest:
        return False
    old, new = _words(previous.text), _words(text)
    return new[: len(old)] == old


async def _update_resent_turn(
    db: async_sessionmaker[AsyncSession], turn_id: uuid.UUID, text: str, previous: str
) -> None:
    """Replace a re-sent user turn's text, keeping the earlier utterance in its metadata."""
    payload = {"resent": True, "resent_from": previous}
    async with db() as session, session.begin():
        await session.execute(
            update(Turn)
            .where(Turn.id == turn_id)
            .values(text=text, meta=Turn.meta.op("||")(literal(payload, type_=JSONB)))
        )


USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _add_usage(totals: dict[str, int], usage: Any) -> None:
    """Add a delta's reported token counts (a mapping with ``USAGE_KEYS``) to ``totals``.

    ``ChatDelta`` carries no ``usage`` yet, so this reads it defensively: providers that
    report nothing leave ``totals`` empty and the turn stores no usage (never a guessed 0).
    """
    if not isinstance(usage, Mapping):
        return
    for key in USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            totals[key] = totals.get(key, 0) + value


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
    # Token counts summed over the turn's model calls, only for counts a provider reported.
    usage: dict[str, int] = field(default_factory=dict)
    first_token_at: float | None = None
    # True once the model's answer finished streaming (vs. a barge-in / disconnect).
    completed: bool = False
    # ``SessionState.user_turn_index`` of the user turn this request answers.
    user_turn_index: int = 0
    # Model serving this turn; None = the Delegator's normal model (TASK-37 routing).
    chat_model: ChatModel | None = None
    # The active router's result, and the device-local model's name when it was chosen.
    route: ActiveRoute | None = None
    local_model_name: str | None = None
    # True once the device-local model streamed part of the answer.
    served_local: bool = False


def _last_user_index(messages: list[dict[str, Any]]) -> int | None:
    return next(
        (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"),
        None,
    )


def _with_system_prefix(messages: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """Insert static instructions right after the leading system message(s).

    That position is identical on every request of a conversation, so the local
    model reuses its prompt cache; notes placed before the latest user message
    instead force the whole tail to be re-processed each turn (~1 s on gemma4).
    """
    if not prefix:
        return list(messages)
    at = 0
    while at < len(messages) and messages[at].get("role") == "system":
        at += 1
    return [*messages[:at], {"role": "system", "content": prefix}, *messages[at:]]


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
        # Active per-turn routing (TASK-37) runs through the RouterHook, if it is installed.
        self._router = next((h for h in hooks if isinstance(h, RouterHook)), None)
        self._local_models: dict[tuple[str, str], ChatModel] = {}

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
        """Wait (bounded) for detached turn finalisation, then for hooks' own work.

        Call on shutdown, before the engine is disposed. Finalisation runs first
        because ``after_response`` may start hook background work (e.g. summaries).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if self._background:
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
        for hook in self.hooks:
            hook_drain = getattr(hook, "drain", None)
            if hook_drain is None:
                continue
            remaining = max(deadline - loop.time(), 0.1)
            try:
                await asyncio.wait_for(hook_drain(), remaining)
            except TimeoutError:
                log.warning("delegator: %s.drain() timed out", type(hook).__name__)
            except Exception:
                log.exception("delegator: %s.drain() failed", type(hook).__name__)

    def _system_prefix(self) -> str:
        parts: list[str] = []
        for hook in self.hooks:
            if isinstance(hook, SystemPrefixProvider):
                try:
                    parts.append(hook.system_prefix(self.settings))
                except Exception:
                    log.exception("delegator: hook %r system_prefix failed", hook)
        return "\n\n".join(p for p in parts if p)

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
        """Bound optional context I/O; log failures so the voice turn keeps going."""
        try:
            async with asyncio.timeout(CONTEXT_IO_TIMEOUT_S):
                return await op
        except TimeoutError:
            log.warning("delegator: %s timed out after %.2fs", what, CONTEXT_IO_TIMEOUT_S)
            return None
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
        user_idx = _last_user_index(messages)
        user_text = content_text(messages[user_idx].get("content")) if user_idx is not None else ""

        user_turn_id: uuid.UUID | None = None
        if messages and messages[-1].get("role") == "user":
            digest = _history_digest(messages[:-1])
            previous: _UserRequest | None = state.extra.get(LAST_USER_REQUEST_KEY)
            resent = previous is not None and _is_resend(previous, digest, user_text)
            state.extra[RESENT_KEY] = resent
            if resent:
                # ElevenLabs re-sent the same user turn (false barge-in): same history, same or
                # extended utterance. It is still the same user turn: same index, same row.
                assert previous is not None
                state.extra[PREV_TEXT_KEY] = previous.text
                user_turn_id = previous.turn_id
                log.info(
                    "delegator: re-sent user turn %d in session %s (%r -> %r)",
                    state.user_turn_index,
                    session_id,
                    previous.text,
                    user_text,
                )
                if user_turn_id is not None and user_text != previous.text:
                    await self._safe(
                        "updating re-sent user turn",
                        _update_resent_turn(self.db, user_turn_id, user_text, previous.text),
                    )
            else:
                state.extra.pop(PREV_TEXT_KEY, None)
                state.user_turn_index += 1
            if user_turn_id is None:
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
            state.extra[LAST_USER_REQUEST_KEY] = _UserRequest(digest, user_text, user_turn_id)
            if not resent and user_turn_id is not None:
                emit(
                    session_id,
                    "user_turn",
                    {
                        "turn_id": str(user_turn_id),
                        "text": user_text,
                        "channel": "text" if headers.get(CHANNEL_HEADER) == "text" else "voice",
                    },
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
        # The active router runs alongside the before_model hooks, inside its own budget.
        # Commitment-protocol turns (a read-back awaiting assent) are never routed (AC#4).
        route_task = (
            asyncio.create_task(self._router.route_turn(ctx), name=f"route:{session_id}")
            if self._router is not None and state.pending_proposal is None
            else None
        )
        notes: list[str] = [HARDWARE_TOOL_NOTE] if "hardware_capabilities" in self.registry else []
        for hook in self.hooks:
            if getattr(hook, "safety_critical", False):
                try:
                    notes.extend(await hook.before_model(ctx))
                except Exception:
                    log.exception("delegator: safety hook %r before_model failed", hook)
                continue
            hook_notes = await self._safe(
                f"hook {type(hook).__name__} before_model", hook.before_model(ctx)
            )
            if hook_notes:
                notes.extend(hook_notes)
        route = await route_task if route_task is not None else None
        chat_model, local_name = self._select_model(route, session_id)
        upstream = _with_system_prefix(messages, self._system_prefix())
        user_idx = _last_user_index(upstream)
        note_messages = [{"role": "system", "content": n} for n in notes if n]
        insert_at = user_idx if user_idx is not None else len(upstream)
        upstream = upstream[:insert_at] + note_messages + upstream[insert_at:]

        internal_tools = self.registry.openai_tools()
        external_tools = [
            t for t in body.tools or [] if (name := _tool_name(t)) and name not in self.registry
        ]
        return _Turn(
            user_turn_index=state.user_turn_index,
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
            chat_model=chat_model,
            route=route,
            local_model_name=local_name,
        )

    def _select_model(
        self, route: ActiveRoute | None, session_id: uuid.UUID
    ) -> tuple[ChatModel, str | None]:
        """The device's local model for a ``small_local`` turn on a capable device, else the
        normal model. A missing decision (off, timeout, error) always means the normal one."""
        if route is None or route.decision is None or route.decision.difficulty != "small_local":
            return self.chat_model, None
        caps = get_capabilities(session_id)
        if not (caps.can_run_local and caps.local_model and caps.local_base_url):
            return self.chat_model, None
        key = (caps.local_base_url, caps.local_model)
        try:
            local = self._local_models.get(key)
            if local is None:
                local = self._local_models[key] = build_local_model(*key)
        except Exception:
            log.exception("delegator: building the local model %s failed", key)
            return self.chat_model, None
        return with_normal_fallback(local, self.chat_model), local.model_name

    async def _run(self, turn: _Turn) -> AsyncIterator[str | list[_Call]]:
        """Yield speech text as it streams, then at most one list of external tool calls."""
        messages = turn.upstream_messages
        round_no = 0
        empty_attempts = 0
        while round_no <= MAX_TOOL_ROUNDS:
            model = self._model_for_attempt(turn, empty_attempts)
            assert model is not None
            last_round = round_no == MAX_TOOL_ROUNDS
            tools = turn.external_tools if last_round else turn.all_tools
            choice = turn.tool_choice if round_no == 0 else _followup_tool_choice(turn.tool_choice)
            calls: dict[int, _Call] = {}
            round_text: list[str] = []
            try:
                async for delta in model.stream(
                    messages,
                    tools or None,
                    temperature=turn.temperature,
                    max_tokens=turn.max_tokens,
                    tool_choice=choice if tools else None,
                ):
                    if delta.model:
                        turn.model_used = delta.model
                        if delta.model == turn.local_model_name:
                            turn.served_local = True
                    _add_usage(turn.usage, getattr(delta, "usage", None))
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
                if not turn.text_parts or round_no > 0:
                    turn.text_parts.append(APOLOGY_TEXT)
                    yield APOLOGY_TEXT
                return

            ordered = [calls[i] for i in sorted(calls) if calls[i].name]
            if not ordered and not round_text:
                # Empty answer (seen live from gemma4): the agent would say nothing at all.
                empty_attempts += 1
                if self._model_for_attempt(turn, empty_attempts) is not None:
                    log.warning("delegator: empty model output; retry %d", empty_attempts)
                    continue
                log.warning("delegator: empty model output after retries; asking to repeat")
                turn.text_parts.append(EMPTY_REPLY_TEXT)
                yield EMPTY_REPLY_TEXT
                return
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
                results = await asyncio.gather(*(self._execute(call, turn) for call in internal))
                if all(
                    getattr(self.registry.get(call.name), "direct_response", False)
                    for call in internal
                ) and all(not result.startswith("Error:") for result in results):
                    for result in results:
                        if turn.first_token_at is None:
                            turn.first_token_at = time.perf_counter()
                        turn.text_parts.append(result)
                        yield result
                    return
                messages.extend(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                    for call, result in zip(internal, results, strict=True)
                )
                # Tool results (a commitment read-back, recall, status) are answered by the
                # normal model: small_local covers plain conversation only.
                turn.chat_model = self.chat_model
                round_no += 1
                continue
            if internal:
                log.warning("tool-round cap reached; ignoring internal calls %s", internal)
            if external:
                turn.external_calls = external
                yield external
            return

    def _model_for_attempt(self, turn: _Turn, empty_attempts: int) -> ChatModel | None:
        """Model for a round after ``empty_attempts`` empty answers: same model, then fallback.

        For a small_local turn the fallback is the normal model.
        """
        model = turn.chat_model or self.chat_model
        if empty_attempts <= 1:
            return model
        if empty_attempts == 2:
            fallback: ChatModel | None = getattr(model, "fallback", None)
            return fallback
        return None

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
            started = time.perf_counter()
            log.info("delegator: executing internal tool %s", call.name)
            result = await tool(args, ctx)
            log.info(
                "delegator: internal tool %s completed in %.0fms rejected=%s",
                call.name,
                (time.perf_counter() - started) * 1000,
                result.rejected,
            )
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
            "delegator turn session=%s ttft_ms=%s model=%s route=%s",
            state.session_id,
            latency_ms,
            model_used,
            "small_local" if turn.served_local else "frontier",
        )
        meta: dict[str, Any] = {}
        if not turn.completed:
            meta["interrupted"] = True
        if turn.external_calls:
            meta["tool_calls"] = [c.name for c in turn.external_calls]
        if turn.usage:
            meta["usage"] = dict(turn.usage)
        if turn.route is not None:
            decision = turn.route.decision
            meta["router"] = {
                "backend": turn.route.backend,
                "status": turn.route.status,
                "difficulty": decision.difficulty if decision else None,
                "latency_ms": turn.route.latency_ms,
            }
        if turn.local_model_name is not None and not turn.served_local:
            # Routed small_local, but the local model failed or stalled: the normal one served.
            meta["local_fallback"] = True
        turn_id = await self._safe(
            "persisting assistant turn",
            persistence.record_turn(
                self.db,
                session_id=state.session_id,
                role="assistant",
                text=text,
                idea_id=state.current_idea_id,
                route="small_local" if turn.served_local else "frontier",
                model_used=model_used,
                latency_ms=latency_ms,
                meta=meta,
                ts=turn.response_started,
            ),
        )
        emit(
            state.session_id,
            "assistant_turn",
            {
                "turn_id": str(turn_id) if turn_id is not None else None,
                "text": text,
                "interrupted": not turn.completed,
                "ttft_ms": latency_ms,
            },
        )
        completed: dict[int, bool] = state.extra.setdefault(COMPLETED_KEY, {})
        completed[turn.user_turn_index] = turn.completed
        for stale in sorted(completed)[:-8]:
            del completed[stale]
        for hook in self.hooks:
            try:
                await hook.after_response(turn.ctx, text)
            except Exception:
                log.exception("delegator: hook %r after_response failed", hook)
