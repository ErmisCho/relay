"""Degrade instead of dropping the call when the primary upstream is down (SPEC section 10)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from relay.delegator.llm.base import ChatDelta, ChatModel

log = logging.getLogger(__name__)

# A hung primary (accepted the connection, never answers) must hand over quickly:
# the user is listening to silence.
FIRST_DELTA_TIMEOUT_S = 5.0
# When the fallback is the same model, a switch cannot help a slow (e.g. cold-loading)
# model and would only restart it, so only give up on a genuinely dead one.
SAME_MODEL_FIRST_DELTA_TIMEOUT_S = 30.0


async def _first_usable_delta(stream: AsyncIterator[ChatDelta]) -> ChatDelta:
    """A finish marker alone is not an answer (for example reasoning token exhaustion)."""
    finish_reason: str | None = None
    async for delta in stream:
        if (delta.content and delta.content.strip()) or delta.tool_calls:
            return delta
        finish_reason = delta.finish_reason or finish_reason
    raise RuntimeError(f"Model returned no text or tool calls (finish_reason={finish_reason})")


class FallbackChatModel:
    """Try ``primary``; if it fails or stalls before its first delta, serve from ``fallback``.

    "Stalls" means no delta within ``first_delta_timeout`` seconds; by default 5 s, or
    30 s when primary and fallback are the same model ref. A failure after the
    first delta propagates: the caller has already streamed part of that answer, and
    replaying a different model's answer would garble speech.
    """

    def __init__(
        self,
        primary: ChatModel,
        fallback: ChatModel,
        *,
        first_delta_timeout: float | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        if first_delta_timeout is None:
            same = primary.model_name == fallback.model_name
            first_delta_timeout = (
                SAME_MODEL_FIRST_DELTA_TIMEOUT_S if same else FIRST_DELTA_TIMEOUT_S
            )
        self.first_delta_timeout = first_delta_timeout

    @property
    def model_name(self) -> str:
        return self.primary.model_name

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = None,
    ) -> AsyncIterator[ChatDelta]:
        kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tool_choice": tool_choice,
        }
        primary = self.primary.stream(messages, tools, **kwargs)
        try:
            async with asyncio.timeout(self.first_delta_timeout):
                first = await _first_usable_delta(primary)
        except Exception:  # includes TimeoutError from the first-delta deadline
            log.warning(
                "primary model %s failed before its first delta; falling back to %s",
                self.primary.model_name,
                self.fallback.model_name,
                exc_info=True,
            )
            aclose = getattr(primary, "aclose", None)
            try:
                if aclose is not None:
                    await aclose()
            except Exception:
                log.debug("closing the failed primary stream raised", exc_info=True)
        else:
            yield replace(first, model=first.model or self.primary.model_name)
            async for delta in primary:
                yield replace(delta, model=delta.model or self.primary.model_name)
            return
        fallback = self.fallback.stream(messages, tools, **kwargs)
        try:
            async with asyncio.timeout(self.first_delta_timeout):
                first = await _first_usable_delta(fallback)
            yield replace(first, model=first.model or self.fallback.model_name)
            async for delta in fallback:
                yield replace(delta, model=delta.model or self.fallback.model_name)
        finally:
            aclose = getattr(fallback, "aclose", None)
            if aclose is not None:
                await aclose()
