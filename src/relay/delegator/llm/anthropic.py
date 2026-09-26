"""Anthropic upstream; OpenAI <-> Anthropic translation lives in ``adapters.openai_compat``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from anthropic import AsyncAnthropic, Timeout

from relay.delegator.adapters.openai_compat import (
    AnthropicStreamTranslator,
    openai_messages_to_anthropic,
    openai_tool_choice_to_anthropic,
    openai_tools_to_anthropic,
)
from relay.delegator.llm.base import CONNECT_TIMEOUT_S, READ_TIMEOUT_S, ChatDelta

DEFAULT_MAX_TOKENS = 1024


class AnthropicChatModel:
    """Streams from the Anthropic Messages API, emitting OpenAI-shaped deltas."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        name: str | None = None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._name = name or model
        # Explicit timeout (SDK default is 600 s); FallbackChatModel is the retry.
        self._client = client or AsyncAnthropic(
            api_key=api_key,
            timeout=Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return self._name

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = None,
    ) -> AsyncIterator[ChatDelta]:
        system, converted = openai_messages_to_anthropic(messages)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": converted,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = openai_tools_to_anthropic(tools)
            choice = openai_tool_choice_to_anthropic(tool_choice)
            if choice is not None:
                kwargs["tool_choice"] = choice
        if temperature is not None:
            kwargs["temperature"] = temperature
        translator = AnthropicStreamTranslator()
        async with self._client.messages.stream(**kwargs) as events:
            async for event in events:
                delta = translator.translate(event)
                if delta is not None:
                    yield replace(delta, model=self._name)
