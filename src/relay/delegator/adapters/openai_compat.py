"""Isolated OpenAI wire-format translation.

- OpenAI chat messages/tools -> Anthropic Messages API request shapes.
- Anthropic stream events -> OpenAI-shaped ``ChatDelta``s.
- Builders for the ``chat.completion.chunk`` / ``chat.completion`` JSON objects
  the Delegator sends to ElevenLabs.
"""

from __future__ import annotations

import json
from typing import Any

from relay.delegator.llm.base import ChatDelta, ToolCallDelta

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


def content_text(content: Any) -> str:
    """Text of an OpenAI message ``content`` (a string or a list of content parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def openai_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(system, messages)`` for the Anthropic Messages API.

    System messages (anywhere in the history) are hoisted into ``system``;
    ``tool_calls`` become ``tool_use`` blocks; ``tool`` messages become
    ``tool_result`` blocks in a user message. Consecutive same-role messages are
    merged because Anthropic requires strict user/assistant alternation.
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message.get("role")
        text = content_text(message.get("content"))
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif role == "user":
            if text:
                push("user", [{"type": "text", "text": text}])
        elif role == "assistant":
            blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": fn.get("name"),
                        "input": _parse_arguments(fn.get("arguments")),
                    }
                )
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id"),
                        "content": text,
                    }
                ],
            )
    # ElevenLabs histories usually open with the agent's greeting; Anthropic wants a user first.
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(call started)"}]})
    return "\n\n".join(system_parts), out


def openai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI ``{"type": "function", "function": {...}}`` tools -> Anthropic tool definitions."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") or {}
        if not fn.get("name"):
            continue
        converted.append(
            {
                "name": fn["name"],
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def openai_tool_choice_to_anthropic(tool_choice: Any) -> dict[str, Any] | None:
    """OpenAI ``tool_choice`` -> Anthropic ``tool_choice`` (None = provider default)."""
    if tool_choice in ("auto", "none"):
        return {"type": tool_choice}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


class AnthropicStreamTranslator:
    """Stateful translation of raw Anthropic stream events into ``ChatDelta``s.

    Anthropic numbers content blocks across text and tool_use; OpenAI numbers only
    tool calls. The translator keeps that mapping. The SDK's synthesized ``text`` /
    ``input_json`` convenience events are ignored (they duplicate the raw deltas).
    """

    def __init__(self) -> None:
        self._tool_index: dict[int, int] = {}

    def translate(self, event: Any) -> ChatDelta | None:
        etype = getattr(event, "type", None)
        if etype == "content_block_start":
            block = event.content_block
            if getattr(block, "type", None) == "tool_use":
                idx = len(self._tool_index)
                self._tool_index[event.index] = idx
                return ChatDelta(
                    tool_calls=[ToolCallDelta(index=idx, id=block.id, name=block.name)]
                )
        elif etype == "content_block_delta":
            delta = event.delta
            dtype = getattr(delta, "type", None)
            if dtype == "text_delta" and delta.text:
                return ChatDelta(content=delta.text)
            if dtype == "input_json_delta" and delta.partial_json:
                tool_idx = self._tool_index.get(event.index)
                if tool_idx is not None:
                    return ChatDelta(
                        tool_calls=[ToolCallDelta(index=tool_idx, arguments=delta.partial_json)]
                    )
        elif etype == "message_delta":
            reason = getattr(event.delta, "stop_reason", None)
            if reason:
                return ChatDelta(finish_reason=_STOP_REASONS.get(reason, "stop"))
        return None


def tool_call_payload(index: int, call_id: str, name: str, arguments: str) -> dict[str, Any]:
    """An OpenAI ``tool_calls[]`` entry (complete, not a fragment)."""
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def chat_completion_chunk(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """One ``chat.completion.chunk`` object."""
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def chat_completion(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
    finish_reason: str,
) -> dict[str, Any]:
    """A non-streaming ``chat.completion`` object."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in tool_calls]
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
