"""OpenAI-compatible upstream (Ollama's ``/v1`` endpoint and OpenAI itself)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

from openai import AsyncOpenAI, AsyncStream, BadRequestError, Timeout
from openai.types.chat import ChatCompletionChunk

from relay.delegator.llm.base import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    ChatDelta,
    ToolCallDelta,
)

log = logging.getLogger(__name__)

MaxTokensParam = Literal["max_tokens", "max_completion_tokens"]

# Optional request params a model may reject with a 400 ``unsupported_parameter`` /
# ``unsupported_value``. They only tune the answer, so dropping one beats failing every
# turn over to the fallback model. ``model``/``messages``/``tools`` are never dropped.
_DROPPABLE_PARAMS = frozenset(
    {"temperature", "reasoning_effort", "max_tokens", "max_completion_tokens", "tool_choice"}
)
_UNSUPPORTED_CODES = frozenset({"unsupported_parameter", "unsupported_value"})


class OpenAICompatChatModel:
    """Streams from any OpenAI-compatible ``/chat/completions`` endpoint.

    ``max_tokens_param`` names the token-cap field (OpenAI reasoning models reject
    ``max_tokens`` and want ``max_completion_tokens``); ``send_temperature=False`` omits
    ``temperature`` for models that only accept the default. If the server still rejects
    one of the optional params with a 400, the param is dropped for the lifetime of this
    model (logged once) and the request is retried, instead of every turn failing over.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str,
        base_url: str | None = None,
        name: str | None = None,
        client: AsyncOpenAI | None = None,
        reasoning_effort: str | None = None,
        max_tokens_param: MaxTokensParam = "max_tokens",
        send_temperature: bool = True,
    ) -> None:
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._max_tokens_param: MaxTokensParam = max_tokens_param
        self._send_temperature = send_temperature
        self._dropped: set[str] = set()
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

    def describe(self) -> str:
        """One-line summary of the request params this model sends (for startup logs)."""
        effort = self._reasoning_effort or "provider default"
        temperature = "caller's" if self._send_temperature else "omitted"
        return (
            f"{self._name} (reasoning_effort={effort}, token cap={self._max_tokens_param}, "
            f"temperature={temperature})"
        )

    def _request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
        tool_choice: Any,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
        if temperature is not None and self._send_temperature:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs[self._max_tokens_param] = max_tokens
        if self._reasoning_effort is not None:
            kwargs["reasoning_effort"] = self._reasoning_effort
        for param in self._dropped:
            kwargs.pop(param, None)
        return kwargs

    def _drop_rejected(self, exc: BadRequestError, kwargs: dict[str, Any]) -> bool:
        """Adapt to a 400 naming an optional param we sent; True when a retry makes sense."""
        param = getattr(exc, "param", None)
        code = getattr(exc, "code", None)
        if code not in _UNSUPPORTED_CODES or param not in _DROPPABLE_PARAMS or param not in kwargs:
            return False
        if param == "max_tokens" and code == "unsupported_parameter":
            # "Use 'max_completion_tokens' instead": keep the cap, rename the field.
            self._max_tokens_param = "max_completion_tokens"
            log.warning("%s rejects max_tokens; sending max_completion_tokens", self._name)
        else:
            self._dropped.add(param)
            log.warning(
                "%s rejected %s=%r (%s); omitting it from now on",
                self._name,
                param,
                kwargs[param],
                code,
            )
        return True

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: Any = None,
    ) -> AsyncIterator[ChatDelta]:
        # Each adaptation removes or renames one param, so this loop is bounded.
        for _ in range(len(_DROPPABLE_PARAMS) + 1):
            kwargs = self._request(messages, tools, temperature, max_tokens, tool_choice)
            try:
                response = cast(
                    AsyncStream[ChatCompletionChunk],
                    await self._client.chat.completions.create(**kwargs),
                )
            except BadRequestError as exc:
                if self._drop_rejected(exc, kwargs):
                    continue
                raise
            break
        else:  # pragma: no cover - every iteration either breaks, continues on progress or raises
            raise RuntimeError(f"{self._name}: could not build an accepted request")
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
