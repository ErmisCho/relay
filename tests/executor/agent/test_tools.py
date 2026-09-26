"""The executor's harness tools keep their boundaries: no secrets in the shell, no file access
outside the project folder, no fetches of private addresses. No network, no LLM.

Each test drives the production tool set (``executor_capability``) through a real agent run
with a FunctionModel that makes one tool call, and inspects what the tool returned.
"""

from __future__ import annotations

import http.server
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from relay.executor.agent import ExecutorDeps, ResearchBrief, executor_capability
from relay.executor.agent.agent import INSTRUCTIONS

# No provider-native tools (like the local Ollama models), so web_fetch runs the harness's own
# local fetch; FunctionModel otherwise claims native web fetch and the local tool is not offered.
LOCAL_PROFILE = {"supported_native_tools": frozenset()}

SECRET_ENV = (
    "DATABASE_URL",
    "DELEGATOR_SHARED_SECRET",
    "ELEVENLABS_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)
SECRET = "s3cret-canary-7d1f"


def _tool_result(workspace: Path, tool: str, args: dict[str, Any]) -> str:
    """Run one ``tool`` call with ``args`` through the executor tools; return what came back."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool, args)])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model, profile=LOCAL_PROFILE),
        deps_type=ExecutorDeps,
        capabilities=[executor_capability()],
    )
    result = agent.run_sync("go", deps=ExecutorDeps(workspace=workspace))
    parts = [
        p
        for m in result.all_messages()
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name == tool
    ]
    assert len(parts) == 1, parts
    return str(parts[0].content)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "project"
    ws.mkdir()
    return ws


def test_shell_environment_carries_no_worker_secrets(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in SECRET_ENV:
        monkeypatch.setenv(name, SECRET)
    out = _tool_result(workspace, "run_command", {"command": "env"})
    assert "PATH=" in out, out  # the command really ran and printed its environment
    assert SECRET not in out
    assert not any(f"{name}=" in out for name in SECRET_ENV), out


@pytest.mark.parametrize("escape", ["../outside.txt", "ABSOLUTE"])
def test_file_tools_refuse_paths_outside_the_project_folder(workspace: Path, escape: str) -> None:
    outside = workspace.parent / "outside.txt"
    outside.write_text(SECRET)
    path = str(outside) if escape == "ABSOLUTE" else escape
    assert SECRET not in _tool_result(workspace, "read_file", {"path": path})


@pytest.fixture
def private_server() -> Iterator[tuple[str, list[str]]]:
    """An HTTP server on loopback serving ``SECRET``; yields its URL and the paths requested."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(SECRET.encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/internal", hits
    finally:
        server.shutdown()
        server.server_close()


def test_web_fetch_refuses_a_private_address(
    workspace: Path, private_server: tuple[str, list[str]]
) -> None:
    url, hits = private_server
    out = _tool_result(workspace, "web_fetch", {"url": url})
    assert SECRET not in out
    assert hits == []  # refused before connecting, not merely filtered afterwards


def test_instructions_name_only_tools_the_local_route_has(workspace: Path) -> None:
    """A tool the instructions name but the local route lacks makes local models call an
    unknown tool, burn the run's retries and fail (it said `web_search`; locally the search
    tool is `duckduckgo_search`)."""
    offered: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model, profile=LOCAL_PROFILE),
        deps_type=ExecutorDeps,
        capabilities=[executor_capability()],
    )
    agent.run_sync("go", deps=ExecutorDeps(workspace=workspace))
    # Backticked spans are shell commands (`sw_vers`), not tool names.
    prose = re.sub(r"`[^`]*`", "", INSTRUCTIONS)
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", prose)) - set(ResearchBrief.model_fields)
    assert named, INSTRUCTIONS  # the check really looks at tool names
    assert named <= set(offered), (named - set(offered), offered)
