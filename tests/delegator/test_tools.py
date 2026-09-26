"""Server-side tool loop: system tools pass through, internal tools stay invisible."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import SessionStore, ToolContext, ToolRegistry, ToolResult
from relay.delegator.llm import ChatDelta, ToolCallDelta
from relay.delegator.service import MAX_TOOL_ROUNDS

from .conftest import ScriptedChatModel, load_request, make_settings, parse_sse, post, text

SESSION_ID = uuid.UUID(load_request()["elevenlabs_extra_body"]["session_id"])


@dataclass
class SecretTool:
    name: str = "secret_lookup"
    description: str = "Look something up in the idea graph."
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {"q": {"type": "string"}}}
    )
    calls: list[tuple[dict[str, Any], ToolContext]] = field(default_factory=list)

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append((args, ctx))
        return ToolResult(content="TOPSECRET-42")


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> list[ChatDelta]:
    half = len(arguments) // 2
    return [
        ChatDelta(tool_calls=[ToolCallDelta(0, id=call_id, name=name, arguments=arguments[:half])]),
        ChatDelta(tool_calls=[ToolCallDelta(0, arguments=arguments[half:])]),
        ChatDelta(finish_reason="tool_calls"),
    ]


def registry_with(tool: SecretTool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


async def test_system_tool_is_passed_through(dead_db: async_sessionmaker[AsyncSession]) -> None:
    model = ScriptedChatModel([tool_call("end_call", '{"reason":"user said bye"}')])
    app = create_app(
        make_settings(), chat_model=model, registry=ToolRegistry(), hooks=[], sessionmaker=dead_db
    )
    chunks = parse_sse((await post(app, load_request())).content)

    calls = [tc for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls", [])]
    assert calls == [
        {
            "index": 0,
            "id": "call_1",
            "type": "function",
            "function": {"name": "end_call", "arguments": '{"reason":"user said bye"}'},
        }
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert len(model.calls) == 1


async def test_internal_tool_runs_server_side_and_never_streams(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    tool = SecretTool()
    store = SessionStore()
    settings = make_settings()
    model = ScriptedChatModel([tool_call("secret_lookup", '{"q":"bike lock"}'), text("Found it.")])
    app = create_app(
        settings,
        chat_model=model,
        registry=registry_with(tool),
        hooks=[],
        session_store=store,
        sessionmaker=dead_db,
    )
    body = (await post(app, load_request())).content

    ((args, ctx),) = tool.calls
    assert args == {"q": "bike lock"}
    assert ctx.state is store.get(SESSION_ID) and ctx.state.user_turn_index == 1
    assert ctx.settings is settings and ctx.db is dead_db
    # The model saw the internal tool and, on its second call, the tool result.
    assert {t["function"]["name"] for t in model.calls[0]["tools"]} == {"secret_lookup", "end_call"}
    followup = model.calls[1]["messages"]
    assert followup[-2]["tool_calls"][0]["function"]["name"] == "secret_lookup"
    assert followup[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "TOPSECRET-42"}
    # Nothing internal leaks downstream.
    assert b"secret_lookup" not in body and b"TOPSECRET" not in body and b"bike lock" not in body
    chunks = parse_sse(body)
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Found it."
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


async def test_invalid_internal_tool_arguments_are_reported_to_the_model(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    tool = SecretTool()
    model = ScriptedChatModel([tool_call("secret_lookup", '{"q": "unterminated'), text("Hmm.")])
    app = create_app(
        make_settings(), chat_model=model, registry=registry_with(tool), hooks=[],
        sessionmaker=dead_db,
    )
    resp = await post(app, load_request())

    assert resp.status_code == 200 and tool.calls == []
    result = model.calls[1]["messages"][-1]
    assert result["role"] == "tool" and "not a valid JSON object" in result["content"]


async def test_tool_loop_is_capped(dead_db: async_sessionmaker[AsyncSession]) -> None:
    tool = SecretTool()
    looping = [
        tool_call("secret_lookup", json.dumps({"q": str(i)})) for i in range(MAX_TOOL_ROUNDS)
    ]
    model = ScriptedChatModel([*looping, text("Okay.")])
    app = create_app(
        make_settings(), chat_model=model, registry=registry_with(tool), hooks=[],
        sessionmaker=dead_db,
    )
    chunks = parse_sse((await post(app, {**load_request(), "tool_choice": "required"})).content)

    # tool_choice is forwarded, but re-forcing "required" after a tool round would loop.
    assert [c["tool_choice"] for c in model.calls[:2]] == ["required", "auto"]

    assert len(model.calls) == MAX_TOOL_ROUNDS + 1
    # The final round offers only downstream tools, so the model has to speak.
    assert [t["function"]["name"] for t in model.calls[-1]["tools"]] == ["end_call"]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Okay."
