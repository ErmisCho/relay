"""Executor shell for Windows/Linux: every command runs in a throwaway Docker container.

The macOS shell is confined by ``sandbox-exec`` (sandbox.py), and the harness's own shell is
POSIX-only (``/bin/sh``, process groups). Here each ``run_command`` call is

    docker run --rm --network none -v <task folder>:/work -w /work <image> sh -c <command>

so the command and everything it starts have no network, see only the task folder, and are
gone when it returns. The worker runs ``docker`` directly with an argv list: no host shell
parses the model's command.

Deliberately small: one foreground tool, no background commands (``start_command``); each
command starts a fresh container, so only files in /work survive between commands. Add a
long-lived container per run if tasks need servers or watchers.

Fail closed: without a reachable Docker daemon no shell capability is built.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from pydantic_ai.capabilities import Capability

log = logging.getLogger(__name__)

#: Python + POSIX shell, nothing else. No network inside, so nothing can be installed.
DOCKER_IMAGE = "python:3.13-slim"
DEFAULT_TIMEOUT_S = 60.0
MAX_OUTPUT_CHARS = 20_000

INSTRUCTIONS = """\
run_command runs a shell command (POSIX sh) in a fresh Linux container with Python 3.13 and
no network; your task folder is mounted at /work, the current directory. Only files you write
under /work survive between commands. It cannot see this computer's hardware: use
machine_hardware for that."""


@functools.cache
def docker_path() -> str | None:
    """``docker`` when installed and its daemon answers; logged once when unavailable."""
    exe = shutil.which("docker")
    if exe is not None:
        try:
            subprocess.run([exe, "info"], capture_output=True, timeout=10, check=True)
            # Pull once here, not mid-task: a first pull takes minutes and its progress
            # lines would land in the model's command output.
            if (
                subprocess.run(
                    [exe, "image", "inspect", DOCKER_IMAGE], capture_output=True, timeout=10
                ).returncode
                != 0
            ):
                subprocess.run(
                    [exe, "pull", DOCKER_IMAGE], capture_output=True, timeout=900, check=True
                )
            return exe
        except (OSError, subprocess.SubprocessError):
            pass
    log.warning("docker unavailable: the executor runs WITHOUT a shell tool")
    return None


def docker_argv(docker: str, workspace: Path, name: str, command: str) -> list[str]:
    return [
        docker, "run", "--rm", "--pull", "never", "--name", name,
        "--network", "none",
        "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
        "-v", f"{workspace}:/work", "-w", "/work",
        "-e", "HOME=/work/.tmp", "-e", "TMPDIR=/work/.tmp",
        DOCKER_IMAGE, "sh", "-c", command,
    ]  # fmt: skip


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n[... {len(text) - MAX_OUTPUT_CHARS} chars cut]"


async def run_in_container(
    docker: str, workspace: Path, command: str, timeout_s: float = DEFAULT_TIMEOUT_S
) -> str:
    """Run ``command`` in a fresh container; labelled output, exit code when non-zero."""
    (workspace / ".tmp").mkdir(parents=True, exist_ok=True)
    name = f"relay-exec-{uuid.uuid4().hex[:12]}"
    proc = await asyncio.create_subprocess_exec(
        *docker_argv(docker, workspace, name, command),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        # Killing the docker CLI would leave the container running; kill it by name.
        kill = await asyncio.create_subprocess_exec(
            docker, "kill", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await kill.wait()
        await proc.wait()
        return f"[timed out after {timeout_s:g} s; the container was stopped]"
    text = ""
    if out:
        text += "stdout:\n" + out.decode("utf-8", "replace")
    if err:
        text += ("\n" if text else "") + "stderr:\n" + err.decode("utf-8", "replace")
    if proc.returncode:
        text += f"\n[exit code {proc.returncode}]"
    return _clip(text or "[no output]")


def docker_shell(workspace: Path) -> Capability[Any] | None:
    """The ``run_command`` capability rooted in ``workspace``; None when Docker is unavailable."""
    docker = docker_path()
    if docker is None:
        return None
    root = workspace.resolve()

    async def run_command(command: str, timeout_seconds: float = DEFAULT_TIMEOUT_S) -> str:
        """Run a POSIX shell command in a fresh, network-less Linux container at /work.

        Args:
            command: The shell command, e.g. "python toolbox/disk_usage.py".
            timeout_seconds: Maximum seconds to wait (default 60, at most 600).
        """
        return await run_in_container(docker, root, command, min(timeout_seconds, 600.0))

    return Capability(instructions=INSTRUCTIONS, tools=[run_command])
