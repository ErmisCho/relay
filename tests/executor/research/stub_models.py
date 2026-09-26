"""Worker-side test module: installs deterministic FunctionModels as the research model.

Imported by the worker via EXECUTOR_RUNNER_MODULES (after the built-in research module, before
DBOS launches). Every model call is appended to ``calls.jsonl`` in ``RELAY_FAKE_FLAG_DIR`` so
tests can assert on exactly what the model received. A prompt containing ``PRIMARY_DOWN``
makes the primary model fail with an HTTP 503; ``HANG`` makes the first primary request take
``HANG_S`` seconds, longer than the worker's ``RELAY_RESEARCH_TIMEOUT_S``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel

from relay.executor.research import configure_research_agent

STUB_SOURCES = ["https://example.com/solar-kettles", "https://example.org/review"]
HANG_S = 6.0
STUB_TITLE = "Stub research brief"
STUB_BODY = "## Findings\n\nKettles heat water. The fetch tool refused a non-http URL."


def _prompt(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    )


def _record(name: str, messages: list[ModelMessage], info: AgentInfo) -> None:
    flags = Path(os.environ["RELAY_FAKE_FLAG_DIR"])
    entry = {"model": name, "prompt": _prompt(messages), "instructions": info.instructions}
    with open(flags / "calls.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    fetched = any(
        isinstance(p, ToolReturnPart) and p.tool_name == "fetch_url"
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
    )
    if not fetched:  # exercise a tool step without network: a rejected scheme
        return ModelResponse(parts=[ToolCallPart("fetch_url", {"url": "ftp://example.invalid/"})])
    brief = {
        "title": STUB_TITLE,
        "summary": "Two stub sources were reviewed.",
        "body_markdown": STUB_BODY,
        "sources": STUB_SOURCES,
    }
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, brief)])


def primary(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    _record("primary", messages, info)
    if "PRIMARY_DOWN" in _prompt(messages):
        raise ModelHTTPError(503, "stub-primary", body="unavailable")
    if "HANG" in _prompt(messages) and len(messages) == 1:
        time.sleep(HANG_S)
    return _respond(messages, info)


def fallback(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    _record("fallback", messages, info)
    return _respond(messages, info)


configure_research_agent(
    FallbackModel(
        FunctionModel(primary, model_name="stub-primary"),
        FunctionModel(fallback, model_name="stub-fallback"),
    )
)
