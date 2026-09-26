"""Provider-agnostic upstream chat model contract.

Every provider adapter streams OpenAI-shaped deltas (content text and tool-call
fragments) so the Delegator's tool loop never sees provider specifics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCallDelta:
    """One fragment of a streamed tool call, keyed by ``index`` (OpenAI semantics)."""

    index: int
    id: str | None = None
    name: str | None = None
    arguments: str | None = None


@dataclass
class ChatDelta:
    """One streamed upstream event."""

    content: str | None = None
    tool_calls: list[ToolCallDelta] = field(default_factory=list)
    finish_reason: str | None = None
    # Model that produced this delta (``<provider>:<model>``); lets a fallback wrapper
    # report which model actually served the turn.
    model: str | None = None


# Transport budgets for voice: a dead host must fail fast so the fallback can take over.
# The first-delta deadline that bounds a *hung* host lives in ``FallbackChatModel``.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 20.0


class ChatModel(Protocol):
    """An upstream LLM that streams OpenAI-format deltas."""

    @property
    def model_name(self) -> str:
        """Identifier recorded in ``turns.model_used``."""
        ...

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = None,
    ) -> AsyncIterator[ChatDelta]:
        """Stream one completion for OpenAI-format ``messages``, ``tools`` and ``tool_choice``."""
        ...
