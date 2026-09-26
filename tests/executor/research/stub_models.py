"""Worker-side test module: installs deterministic FunctionModels and a stub router.

Imported by the worker via EXECUTOR_RUNNER_MODULES (after the built-in research module, before
DBOS launches). Every model call is appended to ``calls.jsonl`` and every router call to
``router_calls.jsonl`` in ``RELAY_FAKE_FLAG_DIR``, so tests can assert on exactly what the
models and the router received.

Routes: ``easy`` runs on ``stub-easy``; ``hard`` on ``stub-hard`` falling back to
``stub-hard-fallback``; runs without a route on ``stub-default`` -> ``stub-default-fallback``.
The stub router answers ``easy`` for goals containing ``ROUTE_EASY``, raises for
``ROUTER_DOWN`` (the runner must then route ``hard``), and answers ``hard`` otherwise.

Prompt markers for the hard primary: ``PRIMARY_DOWN`` fails it with an HTTP 503; ``HANG`` makes
its first request take ``HANG_S`` seconds, longer than the worker's
``RELAY_RESEARCH_TIMEOUT_S``; ``BLOCK-<token>`` makes its first request append to
``started-<token>`` and wait until ``release-<token>`` exists.
"""

from __future__ import annotations

import json
import os
import re
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

from relay.config import Settings
from relay.executor.research import configure_research_agent
from relay.executor.routing import RouteDecision, configure_classifier

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


def _flags() -> Path:
    return Path(os.environ["RELAY_FAKE_FLAG_DIR"])


def _record(name: str, messages: list[ModelMessage], info: AgentInfo) -> None:
    entry = {"model": name, "prompt": _prompt(messages), "instructions": info.instructions}
    with open(_flags() / "calls.jsonl", "a") as fh:
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


def hard(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    _record("stub-hard", messages, info)
    prompt = _prompt(messages)
    if "PRIMARY_DOWN" in prompt:
        raise ModelHTTPError(503, "stub-hard", body="unavailable")
    first = len(messages) == 1
    if "HANG" in prompt and first:
        time.sleep(HANG_S)
    if (m := re.search(r"BLOCK-(\w+)", prompt)) and first:
        with open(_flags() / f"started-{m.group(1)}", "a") as fh:
            fh.write("started\n")
        while not (_flags() / f"release-{m.group(1)}").exists():
            time.sleep(0.05)
    return _respond(messages, info)


def _plain(name: str) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        _record(name, messages, info)
        return _respond(messages, info)

    return FunctionModel(respond, model_name=name)


def stub_router(goal: str, scope_excludes: str, settings: Settings) -> RouteDecision:
    with open(_flags() / "router_calls.jsonl", "a") as fh:
        fh.write(json.dumps({"goal": goal, "scope_excludes": scope_excludes}) + "\n")
    if "ROUTER_DOWN" in goal:
        raise ConnectionError("stub router unreachable")
    difficulty = "easy" if "ROUTE_EASY" in goal else "hard"
    raw = json.dumps({"difficulty": difficulty})
    return RouteDecision(difficulty, 7, True, "ok", raw, settings.router_model)


configure_classifier(stub_router)
configure_research_agent(
    FallbackModel(_plain("stub-default"), _plain("stub-default-fallback")),
    routes={
        "easy": _plain("stub-easy"),
        "hard": FallbackModel(
            FunctionModel(hard, model_name="stub-hard"), _plain("stub-hard-fallback")
        ),
    },
)
