"""The durable ``research`` runner: agent child workflow, then an idempotent render step."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from dbos import DBOS, SetWorkflowTimeout
from dbos._error import DBOSAwaitedWorkflowCancelledError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import UsageLimits

from relay.executor.research.agent import ResearchBrief, build_prompt, get_research_agent
from relay.executor.runners import ArtifactSpec, TaskContext, register_runner

WORKFLOW_NAME = "relay.research.run"
REQUEST_LIMIT = 30
# Wall-clock bound for one research run (the agent child workflow). DBOS persists the deadline,
# so it also holds across a crash and recovery; the run is cancelled at its next step boundary.
TIMEOUT_ENV = "RELAY_RESEARCH_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 20 * 60


def research_timeout_s() -> float:
    return float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)


@DBOS.workflow(name=WORKFLOW_NAME)
def research_workflow(goal: str, scope_excludes: str) -> dict[str, Any]:
    """Run the research agent; each model request / tool call is a checkpointed DBOS step.

    Returns the brief as JSON-able data (stable across pickling and library upgrades) plus
    ``served_by``: the concrete model that answered each request (primary or fallback).
    """
    result = get_research_agent().run_sync(
        build_prompt(goal, scope_excludes), usage_limits=UsageLimits(request_limit=REQUEST_LIMIT)
    )
    served_by = [m.model_name for m in result.all_messages() if isinstance(m, ModelResponse)]
    DBOS.logger.info(f"research {DBOS.workflow_id}: model requests served by {served_by}")
    return {**result.output.model_dump(mode="json"), "served_by": served_by}


def render_markdown(brief: ResearchBrief) -> str:
    """Title heading, summary, body and a Sources section; only the brief's own content."""
    seen: list[str] = []
    for url in (str(u) for u in brief.sources):
        if url not in seen:
            seen.append(url)
    sources = "\n".join(f"- <{u}>" for u in seen) if seen else "- (no sources cited)"
    return (
        f"# {brief.title.strip()}\n\n"
        f"{brief.summary.strip()}\n\n"
        f"{brief.body_markdown.strip()}\n\n"
        f"## Sources\n\n{sources}\n"
    )


def artifact_path(artifacts_dir: str, idea_id: object, task_id: object) -> Path:
    return Path(artifacts_dir).resolve() / str(idea_id) / f"{task_id}.md"


def write_document(path: Path, markdown: str) -> None:
    """Atomic overwrite, so a re-executed step never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@DBOS.step(name="relay.research.render")
def render_step(artifacts_dir: str, idea_id: str, task_id: str, brief: dict[str, Any]) -> str:
    path = artifact_path(artifacts_dir, idea_id, task_id)
    fields = {k: v for k, v in brief.items() if k in ResearchBrief.model_fields}
    write_document(path, render_markdown(ResearchBrief.model_validate(fields)))
    return path.as_uri()


def run_research(ctx: TaskContext) -> ArtifactSpec:
    """Durable runner (called from the ``run_task`` workflow body): child workflow + step."""
    timeout_s = research_timeout_s()
    try:
        with SetWorkflowTimeout(timeout_s):
            brief = research_workflow(ctx.goal, ctx.scope_excludes)
    except DBOSAwaitedWorkflowCancelledError as exc:
        raise TimeoutError(f"research did not finish within {timeout_s:g} s") from exc
    url = render_step(ctx.artifacts_dir, str(ctx.idea_id), str(ctx.task_id), brief)
    return ArtifactSpec(kind="document", url=url, summary=str(brief["summary"]))


register_runner("research", run_research, durable=True)
