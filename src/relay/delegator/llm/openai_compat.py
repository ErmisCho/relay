"""OpenAI-compatible upstream (Ollama's ``/v1`` endpoint and OpenAI itself)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from openai import AsyncOpenAI, AsyncStream, Timeout
from openai.types.chat import ChatCompletionChunk

from relay.delegator.llm.base import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    ChatDelta,
    ToolCallDelta,
)


class OpenAICompatChatModel:
    """Streams from any OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str | None = None,
        name: str | None = None,
        client: AsyncOpenAI | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._name = name or model
        # max_retries=0: FallbackChatModel is the retry; SDK backoff would only add dead air.
        self._client = client or AsyncOpenAI(
            base_url=base_url,
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
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if self._reasoning_effort is not None:
            kwargs["reasoning_effort"] = self._reasoning_effort
        response = cast(
            AsyncStream[ChatCompletionChunk],
            await self._client.chat.completions.create(**kwargs),
        )
        async for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            calls = [
                ToolCallDelta(
                    index=tc.index,
                    id=tc.id,
                    name=tc.function.name if tc.function else None,
                    arguments=tc.function.arguments if tc.function else None,
                )
                for tc in delta.tool_calls or []
            ]
            if delta.content or calls or choice.finish_reason:
                yield ChatDelta(
                    content=delta.content or None,
                    tool_calls=calls,
                    finish_reason=choice.finish_reason,
                    model=self._name,
                )
