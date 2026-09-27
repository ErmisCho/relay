"""Worker-side test module: installs deterministic FunctionModels and a stub router.

Imported by the worker via EXECUTOR_RUNNER_MODULES (after the built-in executor module, before
DBOS launches). Every model call is appended to ``calls.jsonl`` and every router call to
``router_calls.jsonl`` in ``RELAY_FAKE_FLAG_DIR``, so tests can assert on exactly what the
models and the router received. It also stands in for ``relay.executor.workspace.project_dir``:
project folders are created under ``RELAY_FAKE_FLAG_DIR/projects``.

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
import uuid
from pathlib import Path

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel

from relay.config import Settings
from relay.executor import workspace
from relay.executor.agent import configure_executor_agent
from relay.executor.routing import RouteDecision, configure_classifier

STUB_SOURCES = ["https://example.com/solar-kettles", "https://example.org/review"]
HANG_S = 6.0
STUB_TITLE = "Stub research brief"
STUB_BODY = "## Findings\n\nKettles heat water. The fetch tool refused a loopback URL."
# Loopback: the harness fetch must refuse it (SSRF) before any connection is made.
PRIVATE_URL = "http://127.0.0.1:9/private"
# Like the local Ollama models: no provider-native tools, so web_search/web_fetch run locally
# (FunctionModel otherwise claims native web fetch and the local tool is never offered).
LOCAL_PROFILE = {"supported_native_tools": frozenset()}


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
    fetched = [
        p
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name == "web_fetch"
    ]
    if not fetched:  # exercise a tool step without network: a refused private address
        return ModelResponse(parts=[ToolCallPart("web_fetch", {"url": PRIVATE_URL})])
    with open(_flags() / "fetch_results.jsonl", "a") as fh:
        fh.write(json.dumps({"prompt": _prompt(messages), "result": str(fetched[0].content)}))
        fh.write("\n")
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

    return FunctionModel(respond, model_name=name, profile=LOCAL_PROFILE)


def stub_router(goal: str, scope_excludes: str, settings: Settings) -> RouteDecision:
    with open(_flags() / "router_calls.jsonl", "a") as fh:
        fh.write(json.dumps({"goal": goal, "scope_excludes": scope_excludes}) + "\n")
    if "ROUTER_DOWN" in goal:
        raise ConnectionError("stub router unreachable")
    difficulty = "easy" if "ROUTE_EASY" in goal else "hard"
    raw = json.dumps({"difficulty": difficulty})
    return RouteDecision(difficulty, 7, True, "ok", raw, settings.router_model)


def stub_project_dir(idea_id: uuid.UUID, title: str, settings: Settings) -> Path:
    path = _flags() / "projects" / str(idea_id)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


# Install the stubs only inside the stub worker (the only process that sets the flag dir). Tests
# also import this module in-process for its STUB_* constants; patching there would leak the
# stub project_dir and agent into every later test in the pytest run.
if "RELAY_FAKE_FLAG_DIR" in os.environ:
    workspace.project_dir = stub_project_dir

    configure_classifier(stub_router)
    configure_executor_agent(
        FallbackModel(_plain("stub-default"), _plain("stub-default-fallback")),
        routes={
            "easy": _plain("stub-easy"),
            "hard": FallbackModel(
                FunctionModel(hard, model_name="stub-hard", profile=LOCAL_PROFILE),
                _plain("stub-hard-fallback"),
            ),
        },
    )
