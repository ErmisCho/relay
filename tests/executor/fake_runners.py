"""Test runner for kind "research", imported by the worker via EXECUTOR_RUNNER_MODULES.

Behaviour is driven by the commitment goal: "FAIL" raises; "SLOW" appends a line to
``started-<task_id>`` in ``RELAY_FAKE_FLAG_DIR`` and blocks until ``release-<task_id>`` exists.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from relay.executor.runners import ArtifactSpec, TaskContext, register_runner


def summary_for(goal: str) -> str:
    return f"Found three candidate venues for {goal} near the coast. Details are in the doc."


def run(ctx: TaskContext) -> ArtifactSpec:
    flags = Path(os.environ["RELAY_FAKE_FLAG_DIR"])
    if "FAIL" in ctx.goal:
        raise RuntimeError("runner exploded")
    if "SLOW" in ctx.goal:
        with open(flags / f"started-{ctx.task_id}", "a") as fh:
            fh.write("started\n")
        while not (flags / f"release-{ctx.task_id}").exists():
            time.sleep(0.05)
    return ArtifactSpec("document", f"file:///artifacts/{ctx.task_id}.md", summary_for(ctx.goal))


register_runner("research", run)
