"""The executor's model requests stay bounded however many large tool results pile up.

Bug caught: every earlier tool result is re-sent with each request. With the harness defaults a
long result was spilled and the model read it back with ``read_tool_result``, whose returns are
exempt from every output limit, and old results were cleared only at 70% of a 200k window. One
request of live task 0952c583 grew past 61k tokens after 7 requests: gpt-6-luna answered 429
(TPM) and the local fallback timed out prefilling it.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from relay.executor.agent.agent import ExecutorDeps, executor_capability

READS = 8
LINE_CHARS, LINES = 11_000, 5  # a fetched web page: few, very long lines (55k chars)
HANDLE = re.compile(r"read_tool_result\(handle='([^']+)'")
# ~24k tokens at 4 chars/token; the request that failed live was ~61.6k tokens (246k chars).
MAX_REQUEST_CHARS = 96_000


class Out(BaseModel):
    answer: str


def test_request_size_stays_bounded_after_many_large_tool_results(tmp_path: Path) -> None:
    for i in range(1, READS + 1):
        line = (f"page {i} " + "lorem ipsum " * 1000)[:LINE_CHARS]
        (tmp_path / f"page{i}.txt").write_text("\n".join([line] * LINES))
    sizes: list[int] = []
    reads = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal reads
        sizes.append(len(ModelMessagesTypeAdapter.dump_json(messages)))
        last = messages[-1]
        returned = [p for p in last.parts if isinstance(p, ToolReturnPart)]
        if isinstance(last, ModelRequest) and returned:
            # Like a model that wants the whole page: read a spilled result back.
            if m := HANDLE.search(str(returned[0].content)):
                args = {"handle": m.group(1), "offset": 0, "limit": 200}
                return ModelResponse(parts=[ToolCallPart("read_tool_result", args)])
        if reads < READS:
            reads += 1
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": f"page{reads}.txt"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "ok"})])

    agent = Agent(
        FunctionModel(respond, profile={"supported_native_tools": frozenset()}),
        deps_type=ExecutorDeps,
        output_type=Out,
        capabilities=[executor_capability()],
    )
    agent.run_sync("read every page", deps=ExecutorDeps(workspace=tmp_path))

    print(f"request sizes (chars): first {sizes[0]}, max {max(sizes)}, last {sizes[-1]}")
    assert max(sizes) < MAX_REQUEST_CHARS, sizes
