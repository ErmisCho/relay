"""Shared Delegator contracts.

Owned by the coordinator: wave agents build against these types but do not edit
this module. Every internal tool and turn hook implements the protocols below,
and ``relay.delegator.wiring`` is the only place they are registered.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings


@dataclass
class PendingProposal:
    """A commitment the agent has proposed but the user has not yet assented to.

    Lives only in session state, never in ``commitments`` (that table requires
    assent). A process restart drops it, which fails safe to "no dispatch".
    """

    goal: str
    scope_excludes: str
    artifact_kind: str
    kind: str
    readback_text: str
    idea_id: uuid.UUID | None
    # ``SessionState.user_turn_index`` at the moment the proposal was made.
    proposed_at_user_turn: int
    proposal_id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class SessionState:
    """Conversational state for one voice session.

    ``session_id`` is the ``sessions.id`` row the client created (or the Delegator
    upserted) for this conversation.
    """

    session_id: uuid.UUID
    current_idea_id: uuid.UUID | None = None
    pending_proposal: PendingProposal | None = None
    # Incremented by the Delegator once per incoming user turn, before hooks run.
    user_turn_index: int = 0
    # Free-form slots for tools and hooks; namespace keys by owner, e.g. "reports.offered".
    extra: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    """In-memory session state. Deliberately not durable; see ``PendingProposal``.

    Bounded: once ``max_sessions`` is exceeded the least recently used session is
    evicted (its pending proposal is dropped, which fails safe to "no dispatch").
    """

    def __init__(self, max_sessions: int = 256) -> None:
        self._states: OrderedDict[uuid.UUID, SessionState] = OrderedDict()
        self._max = max_sessions

    def get(self, session_id: uuid.UUID) -> SessionState:
        state = self._states.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)
            self._states[session_id] = state
            while len(self._states) > self._max:
                self._states.popitem(last=False)
        else:
            self._states.move_to_end(session_id)
        return state

    def drop(self, session_id: uuid.UUID) -> None:
        self._states.pop(session_id, None)


@dataclass
class ToolContext:
    """What an internal tool receives besides its arguments."""

    state: SessionState
    db: async_sessionmaker[AsyncSession]
    settings: Settings
    # The persisted user turn that led to this model call, if any.
    user_turn_id: uuid.UUID | None = None


@dataclass
class ToolResult:
    """Text handed back to the upstream model as the tool result.

    Never streamed to ElevenLabs; only the model's resulting speech is.
    """

    content: str
    # True when a server-side guard (scope, commitment protocol) refused the call.
    rejected: bool = False


@runtime_checkable
class InternalTool(Protocol):
    """A tool executed inside the Delegator's server-side tool loop."""

    name: str
    description: str
    # JSON Schema of the arguments object (OpenAI ``function.parameters``).
    parameters: dict[str, Any]

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class ToolRegistry:
    """Named internal tools, rendered as OpenAI tool definitions for the upstream model."""

    def __init__(self) -> None:
        self._tools: dict[str, InternalTool] = {}

    def register(self, tool: InternalTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"internal tool {tool.name!r} registered twice")
        self._tools[tool.name] = tool

    def get(self, name: str) -> InternalTool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[InternalTool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]


@dataclass
class TurnContext:
    """One incoming chat-completions request, as seen by turn hooks."""

    state: SessionState
    db: async_sessionmaker[AsyncSession]
    settings: Settings
    # OpenAI-format messages exactly as ElevenLabs sent them.
    messages: list[dict[str, Any]]
    # Text of the last user message, "" if the request has none.
    user_text: str
    user_turn_id: uuid.UUID | None = None


@runtime_checkable
class SystemPrefixProvider(Protocol):
    """Optional extra for a TurnHook: static instructions for the fixed prompt prefix.

    The text must not change from turn to turn. The Delegator places it right after
    the conversation's leading system message(s), where it stays in the local
    model's prompt cache. Per-turn notes from ``before_model`` go next to the latest
    user message instead, and should be short.
    """

    def system_prefix(self, settings: Settings) -> str: ...


class TurnHook(Protocol):
    """Runs around every user turn, in registration order."""

    async def before_model(self, ctx: TurnContext) -> list[str]:
        """Return system notes to add to the upstream prompt for this turn (may be empty)."""
        ...

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        """Called once the assistant turn has been fully streamed and persisted."""
        ...
