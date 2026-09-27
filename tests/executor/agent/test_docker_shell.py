"""The Windows/Linux executor shell: every command in a network-less Docker container."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from relay.executor.agent import ExecutorDeps, docker_shell, executor_capability
from relay.executor.agent.docker_shell import docker_path, run_in_container

needs_docker = pytest.mark.skipif(docker_path() is None, reason="Docker daemon not available")


def _offered(workspace: Path) -> list[str]:
    offered: list[str] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model), deps_type=ExecutorDeps, capabilities=[executor_capability()]
    )
    agent.run_sync("go", deps=ExecutorDeps(workspace=workspace))
    return offered


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS uses the sandbox-exec shell")
def test_no_shell_without_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: no Docker means no shell at all, never an unconfined one."""
    monkeypatch.setattr(docker_shell, "docker_path", lambda: None)
    offered = _offered(tmp_path)
    assert "run_command" not in offered
    assert {"machine_hardware", "current_weather", "web_fetch"} <= set(offered)


@needs_docker
@pytest.mark.skipif(sys.platform == "darwin", reason="macOS uses the sandbox-exec shell")
def test_run_command_is_offered_with_docker(tmp_path: Path) -> None:
    assert "run_command" in _offered(tmp_path)


@needs_docker
def test_commands_have_no_network_and_see_only_the_task_folder(tmp_path: Path) -> None:
    """The container is the security boundary: no network, and nothing of the host but the
    task folder (the relay repository must be invisible)."""
    docker = docker_path()
    assert docker is not None
    (tmp_path / "mine.txt").write_text("task file")
    net = "python -c \"import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=5)\""
    out = asyncio.run(run_in_container(docker, tmp_path, net))
    assert "[exit code" in out and ("unreachable" in out or "URLError" in out), out
    out = asyncio.run(run_in_container(docker, tmp_path, "ls /work; ls / | tr '\n' ' '"))
    assert "mine.txt" in out
    assert "relay" not in out.split("stdout:", 1)[1].replace("mine.txt", "")
