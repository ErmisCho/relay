"""Registry of task runners, one per task kind.

A runner module registers itself at import time::

    from relay.executor.runners import ArtifactSpec, TaskContext, register_runner

    async def run_my_kind(ctx: TaskContext) -> ArtifactSpec: ...

    register_runner("my_kind", run_my_kind)

The worker imports every module listed in ``BUILTIN_RUNNER_MODULES`` plus the
comma-separated ``EXECUTOR_RUNNER_MODULES`` environment variable before DBOS launches.

Two registration modes:

* ``durable=False`` (default): the runner (sync or async) is wrapped in ONE DBOS step. After
  a crash the whole runner re-executes, so it should be restartable.
* ``durable=True``: the runner is called directly from the workflow body. It must be a
  *synchronous* DBOS workflow (child workflow) or step, or a sync function composed only of
  DBOS steps, so DBOS checkpoints its progress at a finer grain.
"""

from __future__ import annotations

import importlib
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

TASK_RUNNER_MODULES_ENV = "EXECUTOR_RUNNER_MODULES"
# Built-in runner modules, imported by every worker before any EXECUTOR_RUNNER_MODULES.
BUILTIN_RUNNER_MODULES: tuple[str, ...] = ("relay.executor.agent", "relay.executor.code")


@dataclass(frozen=True)
class TaskContext:
    """Everything a runner needs; picklable so DBOS can checkpoint it."""

    task_id: uuid.UUID
    kind: str
    commitment_id: uuid.UUID
    idea_id: uuid.UUID
    goal: str
    scope_excludes: str
    artifact_kind: str
    artifacts_dir: str


@dataclass(frozen=True)
class ArtifactSpec:
    """What a runner produced; becomes an ``artifacts`` row."""

    kind: str  # "document" | "pull_request" | "branch"
    url: str
    summary: str | None = None
    # `<provider>:<model>` that produced the result; stored as tasks.served_model.
    served_model: str | None = None


Runner = Callable[[TaskContext], "ArtifactSpec | Awaitable[ArtifactSpec]"]


@dataclass(frozen=True)
class RegisteredRunner:
    kind: str
    fn: Runner
    durable: bool


_RUNNERS: dict[str, RegisteredRunner] = {}


def register_runner(kind: str, runner: Runner, *, durable: bool = False) -> None:
    """Register ``runner`` for task ``kind``; re-registering the same kind replaces it."""
    _RUNNERS[kind] = RegisteredRunner(kind=kind, fn=runner, durable=durable)


def get_runner(kind: str) -> RegisteredRunner:
    try:
        return _RUNNERS[kind]
    except KeyError:
        raise LookupError(f"no runner registered for task kind {kind!r}") from None


def registered_kinds() -> list[str]:
    return sorted(_RUNNERS)


def load_runner_modules(extra: str | None = None) -> list[str]:
    """Import built-in runner modules plus those named in ``EXECUTOR_RUNNER_MODULES``."""
    raw = extra if extra is not None else os.environ.get(TASK_RUNNER_MODULES_ENV, "")
    names = [*BUILTIN_RUNNER_MODULES, *(m.strip() for m in raw.split(",") if m.strip())]
    for name in names:
        importlib.import_module(name)
    return names
