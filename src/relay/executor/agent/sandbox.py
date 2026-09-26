"""OS sandbox for the executor's shell: every command runs under macOS ``sandbox-exec``.

The shell runs as the owner's user, so its env scrub and command denylist are not a boundary.
This module is: each command the model starts is spawned as
``sandbox-exec -p <profile> /bin/sh -c <command>``, and the kernel enforces the profile on the
command and everything it forks:

- no network at all (``deny network*``), and no DNS lookups (a hostname is an exfil channel);
  the agent's own web_search/web_fetch run in the worker process and are unaffected;
- writes only inside the idea's workspace (``$TMPDIR`` points into it) plus a few ``/dev``
  nodes;
- no reads of the owner's home directory (``~/Documents``, ``~/.ssh``, ``~/.config``,
  keychains, git credentials, ... all of it) or of the relay repository wherever it lives,
  except the workspace itself, which stays readable and writable even under a denied tree
  (SBPL applies the LAST matching rule, so the workspace allow comes after the denies);
- system binaries and libraries (``/bin``, ``/usr``, ``/System``, ``/Library``,
  ``/opt/homebrew``, ``/private/var``) stay readable and executable via ``allow default``.

Seam: a ``ShellToolset`` subclass that wraps the command string in ``run_command`` and
``start_command`` before the harness spawns it. The harness has no spawn hook, its
``limited_command`` helper is module-global (patching it would sandbox every toolset in the
process), and ``/bin/sh`` is hard-wired, so wrapping the string is the least invasive point that
covers both spawning tools while reusing the harness's process-group kill, timeouts and output
handling unchanged. The persistent ``shell`` tool is never registered: its commands outlive the
run under a detached supervisor.

Fail closed: without ``/usr/bin/sandbox-exec`` (or off macOS) no shell capability is built.
"""

from __future__ import annotations

import functools
import logging
import os
import shlex
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai_harness.shell import RUN_SCOPED_TOOL_NAMES, Shell, ShellToolset

log = logging.getLogger(__name__)

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
# $TMPDIR for shell commands: inside the workspace, so the one workspace write rule covers it.
TMPDIR_NAME = ".tmp"
# /dev nodes ordinary commands write to (redirects, terminals, dtrace probes of the runtime).
_DEV_WRITE_RULES = (
    '(literal "/dev/null")',
    '(literal "/dev/zero")',
    '(literal "/dev/tty")',
    '(literal "/dev/dtracehelper")',
    '(regex #"^/dev/fd/")',
    '(regex #"^/dev/ttys[0-9]+$")',
)
# Mach services that would reach the network or another app on the command's behalf:
# name resolution (a looked-up hostname leaks data even with sockets denied) and
# LaunchServices (`open https://...` hands a URL to the browser).
_DENIED_MACH_SERVICES = (
    "com.apple.dnssd.service",
    "com.apple.mDNSResponder",
    "com.apple.coreservices.launchservicesd",
)


def sandbox_exec_path() -> Path | None:
    """``/usr/bin/sandbox-exec`` when this is macOS and it is executable, else None."""
    if sys.platform != "darwin":
        return None
    return SANDBOX_EXEC if os.access(SANDBOX_EXEC, os.X_OK) else None


def sbpl_string(value: str) -> str:
    """``value`` as an SBPL string literal (backslash and double quote escaped)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _real(path: Path) -> str:
    # The sandbox matches resolved paths (/var -> /private/var, /tmp -> /private/tmp).
    return str(path.expanduser().resolve())


def build_profile(workspace: Path, denied_read_roots: Iterable[Path]) -> str:
    """The SBPL profile confining one run's shell commands to ``workspace``.

    ``denied_read_roots`` are trees whose contents may not be read (the home directory and
    the relay repository); the workspace is re-allowed after them, so it works even inside one.
    """
    ws = Path(_real(workspace))
    denied = [_real(p) for p in denied_read_roots]
    denied_rules = " ".join(f"(subpath {sbpl_string(p)})" for p in denied)
    # Stat-only access to the workspace's ancestors, so path walkers (realpath, `ls -ld ..`)
    # do not trip over a denied parent; no directory listing, no file contents.
    ancestors = " ".join(f"(literal {sbpl_string(str(p))})" for p in ws.parents)
    services = " ".join(f"(global-name {sbpl_string(s)})" for s in _DENIED_MACH_SERVICES)
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        f"(deny mach-lookup {services})",
        "(deny appleevent-send)",
        "(deny file-write*)",
        f"(allow file-write* {' '.join(_DEV_WRITE_RULES)})",
    ]
    if denied_rules:
        lines.append(f"(deny file-read* {denied_rules})")
    lines += [
        f"(allow file-read-metadata {ancestors})",
        f"(allow file-read* file-write* (subpath {sbpl_string(str(ws))}))",
    ]
    return "\n".join(lines) + "\n"


def sandbox_env(
    base: Mapping[str, str], workspace: Path, denied_read_roots: Iterable[Path]
) -> dict[str, str]:
    """``base`` adapted to the sandbox: HOME and TMPDIR inside the workspace, PATH pruned.

    HOME points at the workspace because tools read ``~`` config (git aborts on an unreadable
    ``~/.gitconfig``); PATH drops entries under denied trees, which could only fail to exec.
    """
    ws = Path(_real(workspace))
    denied = [Path(_real(p)) for p in denied_read_roots]
    env = dict(base)
    env["HOME"] = str(ws)
    env["TMPDIR"] = str(ws / TMPDIR_NAME)
    if "PATH" in env:
        keep = [
            entry
            for entry in env["PATH"].split(os.pathsep)
            if entry and not any(Path(entry).is_relative_to(d) for d in denied)
        ]
        env["PATH"] = os.pathsep.join(dict.fromkeys(keep))
    return env


def sandbox_argv(workspace: Path, denied_read_roots: Iterable[Path]) -> list[str] | None:
    """``sandbox-exec -p <profile>`` confining a worker-side command to ``workspace``.

    For commands the WORKER runs on the agent's files (tests, ``git add``/``commit``, which run
    hooks and filters the agent could have written): same profile as the agent's shell. None
    when the sandbox is unavailable.
    """
    exe = sandbox_exec_path()
    if exe is None:
        return None
    root = Path(_real(workspace))
    (root / TMPDIR_NAME).mkdir(parents=True, exist_ok=True)
    return [str(exe), "-p", build_profile(root, list(denied_read_roots))]


class SandboxedShellToolset(ShellToolset[AgentDepsT]):
    """``ShellToolset`` whose spawned commands all run under ``sandbox-exec``."""

    def __init__(self, *, sandbox_exec: Path, profile: str, **kwargs: Any) -> None:
        self._sandbox_exec = sandbox_exec
        self._profile = profile
        self._toolset_kwargs = kwargs
        super().__init__(**kwargs)

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> AbstractToolset[AgentDepsT]:
        """A fresh sandboxed instance per run (the base class would return an unsandboxed one)."""
        return SandboxedShellToolset[AgentDepsT](
            sandbox_exec=self._sandbox_exec, profile=self._profile, **self._toolset_kwargs
        )

    def _wrap(self, command: str) -> str:
        # `exec`: the outer /bin/sh is replaced, so the process-group leader is the sandbox.
        argv = [str(self._sandbox_exec), "-p", self._profile, "/bin/sh", "-c", command]
        return "exec " + shlex.join(argv)

    def _unwrap(self, result: str, command: str, wrapped: str) -> str:
        # Messages that echo the command show the model's command, not the profile.
        return result.replace(repr(wrapped), repr(command)).replace(wrapped, command)

    async def run_command(self, command: str, *, timeout_seconds: float | None = None) -> str:
        """Execute a shell command and return its output.

        Args:
            command: The shell command to run.
            timeout_seconds: Maximum seconds to wait (default: 30).

        Returns:
            Labeled stdout/stderr output with exit code on non-zero exit.
        """
        self._check_command(command)  # the policy judges the model's command, not the wrapper
        wrapped = self._wrap(command)
        result = await super().run_command(wrapped, timeout_seconds=timeout_seconds)
        return self._unwrap(result, command, wrapped)

    async def start_command(self, command: str) -> str:
        """Start a long-running command in the background (e.g. a server or watcher).

        Callers MUST call `stop_command(command_id)` when done to terminate the
        process and clean up temporary output files.

        Args:
            command: The shell command to run in the background.

        Returns:
            A message containing the unique command ID for later check/stop calls.
        """
        self._check_command(command)
        wrapped = self._wrap(command)
        result = await super().start_command(wrapped)
        return self._unwrap(result, command, wrapped)


@dataclass
class SandboxedShell(Shell[AgentDepsT]):
    """``Shell`` capability building a ``SandboxedShellToolset`` (run-scoped tools only)."""

    sandbox_exec: Path = field(kw_only=True)
    profile: str = field(kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        if "shell" in self.tools:
            raise ValueError("the persistent `shell` tool outlives the run; use run-scoped tools")

    def get_toolset(self) -> SandboxedShellToolset[AgentDepsT]:
        return SandboxedShellToolset[AgentDepsT](
            sandbox_exec=self.sandbox_exec,
            profile=self.profile,
            cwd=Path(self.cwd),
            allowed_commands=self.allowed_commands,
            denied_commands=self.denied_commands,
            denied_operators=self.denied_operators,
            default_timeout=self.default_timeout,
            max_output_chars=self.max_output_chars,
            max_file_bytes=self.max_file_bytes,
            persist_cwd=self.persist_cwd,
            allow_interactive=self.allow_interactive,
            env=self.env,
            denied_env_patterns=self.denied_env_patterns,
            tools=self.tools,
        )


@functools.cache
def _warn_unsandboxed() -> None:
    log.warning(
        "sandbox-exec unavailable (platform %s): the executor runs WITHOUT a shell tool",
        sys.platform,
    )


def sandboxed_shell(
    workspace: Path,
    *,
    env: Mapping[str, str],
    denied_read_roots: Iterable[Path],
    denied_env_patterns: Iterable[str],
    denied_commands: Iterable[str],
) -> SandboxedShell[Any] | None:
    """The run-scoped shell for ``workspace`` under ``sandbox-exec``; None (logged once) if the
    sandbox is unavailable. Never returns an unsandboxed shell."""
    exe = sandbox_exec_path()
    if exe is None:
        _warn_unsandboxed()
        return None
    roots = list(denied_read_roots)
    root = Path(_real(workspace))
    (root / TMPDIR_NAME).mkdir(parents=True, exist_ok=True)
    return SandboxedShell[Any](
        cwd=root,
        denied_commands=list(denied_commands),
        allow_interactive=True,
        env=sandbox_env(env, root, roots),
        denied_env_patterns=list(denied_env_patterns),
        tools=RUN_SCOPED_TOOL_NAMES,
        sandbox_exec=exe,
        profile=build_profile(root, roots),
    )
