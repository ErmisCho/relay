"""The durable code runner: route, workspace, checkout, agent, commit, run_tests, push, open_pr.

Every stage is a DBOS step (the agent a child workflow whose model requests and tool calls
are steps), so a worker killed mid-task resumes after the last completed step. The terminal
artifact is a DRAFT pull request (kind ``pull_request``) on the task's own ``relay/`` branch,
never merged; without a GitHub token (or when publishing fails) it is the committed branch in
the idea's project folder (kind ``branch``). Test failures are reported, never hidden, and never
fail the task.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
from dbos import DBOS, SetWorkflowTimeout
from dbos._error import DBOSAwaitedWorkflowCancelledError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import UsageLimits

from relay.config import get_settings
from relay.executor.agent.agent import (
    ExecutorDeps,
    build_prompt,
    get_code_agent,
    shell_denied_read_roots,
    shell_env,
)
from relay.executor.agent.runner import (
    REQUEST_LIMIT,
    executor_timeout_s,
    record_route_step,
    route_step,
    served_model_name,
    workspace_step,
)
from relay.executor.agent.sandbox import sandbox_argv, sandbox_env
from relay.executor.code import git, github
from relay.executor.runners import ArtifactSpec, TaskContext, register_runner

WORKFLOW_NAME = "relay.code.run"
TEST_TIMEOUT_S = 300
TEST_OUTPUT_CHARS = 6_000


def _sandbox(folder: Path) -> tuple[list[str] | None, dict[str, str]]:
    roots = shell_denied_read_roots()
    return sandbox_argv(folder, roots), sandbox_env(shell_env(), folder, roots)


@DBOS.step(name="relay.code.checkout")
def checkout_step(workspace: str, branch: str) -> dict[str, str]:
    return git.checkout(Path(workspace), branch)


@DBOS.workflow(name=WORKFLOW_NAME)
def code_workflow(
    goal: str, scope_excludes: str, route: str | None, workspace: str
) -> dict[str, Any]:
    """Run the code agent in ``workspace``; returns its summary plus the model that served it."""
    result = get_code_agent().run_sync(
        build_prompt(goal, scope_excludes),
        model=route,
        deps=ExecutorDeps(workspace=Path(workspace)),
        usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
    )
    responses = [m for m in result.all_messages() if isinstance(m, ModelResponse)]
    served = served_model_name(responses[-1]) if responses else None
    return {**result.output.model_dump(mode="json"), "served_model": served}


@DBOS.step(name="relay.code.commit")
def commit_step(workspace: str, branch: str, base: str, message: str) -> dict[str, str]:
    folder = Path(workspace)
    prefix, env = _sandbox(folder)
    if prefix is None:
        raise RuntimeError("sandbox-exec unavailable: refusing to run git on agent-written files")
    sha = git.commit_all(folder, branch, message, prefix, env)
    return {"sha": sha, "diffstat": git.diff_stat(folder, base, branch)}


def detect_test_command(folder: Path) -> str | None:
    """The project's test command, conservatively: pytest for Python, ``npm test`` for Node."""
    if (folder / "package.json").is_file():
        try:
            scripts = json.loads((folder / "package.json").read_text()).get("scripts") or {}
        except (ValueError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict) and scripts.get("test"):
            return "npm test --silent"
    python = any((folder / f).is_file() for f in ("pyproject.toml", "setup.py", "pytest.ini"))
    has_tests = (folder / "tests").is_dir() or any(folder.glob("test_*.py"))
    return "python3 -m pytest -q" if python and has_tests else None


@DBOS.step(name="relay.code.run_tests")
def run_tests_step(workspace: str) -> dict[str, Any]:
    """Run the test command in the sandbox (no network); record, never raise on failure."""
    folder = Path(workspace)
    command = detect_test_command(folder)
    if command is None:
        return {"command": None, "exit_code": None, "output": "No test command found."}
    prefix, env = _sandbox(folder)
    if prefix is None:
        return {"command": command, "exit_code": None, "output": "Not run: no sandbox available."}
    try:
        proc = subprocess.run(
            [*prefix, "/bin/sh", "-c", command],
            cwd=folder,
            env=env,
            capture_output=True,
            text=True,
            timeout=TEST_TIMEOUT_S,
            check=False,
        )
        code: int | None = proc.returncode
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout
        partial = out.decode(errors="replace") if isinstance(out, bytes) else (out or "")
        code, output = None, f"Timed out after {TEST_TIMEOUT_S} s.\n{partial}"
    return {"command": command, "exit_code": code, "output": output[-TEST_OUTPUT_CHARS:]}


def _retry_publish(exc: BaseException) -> bool:
    if isinstance(exc, git.ProtectedBranchError):
        return False
    return isinstance(exc, git.GitError | github.GitHubError | httpx.HTTPError)


@DBOS.step(
    name="relay.code.push",
    retries_allowed=True,
    max_attempts=3,
    interval_seconds=2.0,
    should_retry=_retry_publish,
)
def push_step(workspace: str, branch: str) -> dict[str, str | None]:
    """Push the task branch to the GitHub repository; ``repo`` None = keep it local (+ why)."""
    settings = get_settings()
    if not settings.github_token:
        return {"repo": None, "note": "no GitHub token is configured"}
    folder = Path(workspace)
    origin = git.origin_url(folder)
    repo = github.parse_repo(origin)
    if origin is None:
        with github.GitHub(settings.github_token, settings.github_api_url) as gh:
            created = gh.ensure_private_repo(settings.github_owner, folder.name)
        repo = str(created["full_name"])
        git.git(folder, "remote", "add", "origin", github.https_url(repo))
    elif repo is None:
        return {"repo": None, "note": f"the project's origin {origin} is not on GitHub"}
    assert repo is not None
    git.push_branch(
        folder, github.https_url(repo), branch, github.push_auth_env(settings.github_token)
    )
    return {"repo": repo, "note": None}


@DBOS.step(
    name="relay.code.open_pr",
    retries_allowed=True,
    max_attempts=3,
    interval_seconds=2.0,
    should_retry=_retry_publish,
)
def open_pr_step(repo: str, branch: str, base: str, title: str, body: str) -> str:
    settings = get_settings()
    with github.GitHub(settings.github_token, settings.github_api_url) as gh:
        pr = gh.open_draft_pr(repo, branch, base, title, body)
    return str(pr["html_url"])


def test_verdict(tests: dict[str, Any]) -> str:
    """ "tests passed" / "tests failed (exit N)" / why they were not run."""
    if tests["command"] is None:
        return "no tests were found to run"
    if tests["exit_code"] == 0:
        return "tests passed"
    if tests["exit_code"] is None:
        return "tests did not complete"
    return f"tests failed (exit {tests['exit_code']})"


def pr_body(
    goal: str, excludes: str, change: dict[str, Any], tests: dict[str, Any], stat: str
) -> str:
    changes = "\n".join(f"- {c}" for c in change.get("changes") or []) or "- (none listed)"
    command = f"`{tests['command']}`" if tests["command"] else "(none)"
    return (
        f"## Goal\n\n{goal}\n\n"
        f"## Out of scope\n\n{excludes.strip() or '(nothing explicitly excluded)'}\n\n"
        f"## What changed\n\n{change['summary'].strip()}\n\n{changes}\n\n{stat}\n\n"
        f"## How to run it\n\n{change['how_to_run'].strip()}\n\n"
        f"## Test results\n\n{test_verdict(tests)}: {command}, exit code "
        f"{tests['exit_code']}\n\n```\n{tests['output'].strip()}\n```\n\n"
        "---\nOpened by relay as a draft for review. relay never merges it.\n"
    )


def run_code_task(ctx: TaskContext) -> ArtifactSpec:
    """Durable runner (called from the ``run_task`` workflow body)."""
    route = record_route_step(str(ctx.task_id), route_step(ctx.goal, ctx.scope_excludes))
    workspace = workspace_step(str(ctx.idea_id))
    co = checkout_step(workspace, git.branch_for(str(ctx.task_id), ctx.goal))
    branch, base = co["branch"], co["base"]
    timeout_s = executor_timeout_s()
    try:
        with SetWorkflowTimeout(timeout_s):
            change = code_workflow(ctx.goal, ctx.scope_excludes, route["difficulty"], workspace)
    except DBOSAwaitedWorkflowCancelledError as exc:
        raise TimeoutError(f"{ctx.kind} did not finish within {timeout_s:g} s") from exc
    title = str(change["title"]).strip()
    message = (
        f"{title}\n\nGoal: {ctx.goal}\nOut of scope: {ctx.scope_excludes}\n\n"
        f"relay task {ctx.task_id}"
    )
    commit = commit_step(workspace, branch, base, message)
    tests = run_tests_step(workspace)
    verdict = test_verdict(tests)
    served = change.get("served_model")
    details = f"{commit['diffstat']}. {change['summary'].strip()}"
    try:
        pushed = push_step(workspace, branch)
    except Exception as exc:  # publishing failed after retries: the branch is still there
        pushed = {"repo": None, "note": f"publishing failed ({type(exc).__name__}: {exc})"}
    if pushed["repo"] is not None:
        body = pr_body(ctx.goal, ctx.scope_excludes, change, tests, commit["diffstat"])
        url = open_pr_step(pushed["repo"], branch, base, title, body)
        summary = f"{title} is ready as a draft pull request; {verdict}. {details}"
        return ArtifactSpec("pull_request", url, summary, str(served) if served else None)
    summary = (
        f"{title} is on branch {branch} in the project folder; {verdict}. "
        f"Folder: {workspace}. Not pushed: {pushed['note']}. {details}"
    )
    return ArtifactSpec(
        "branch", Path(workspace).as_uri(), summary, str(served) if served else None
    )


register_runner("code", run_code_task, durable=True)
