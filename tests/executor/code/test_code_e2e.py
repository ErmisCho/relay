"""Code commitments end to end: real worker, real code runner, stub agent, fake GitHub."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import pytest
from dbos import DBOSClient
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.executor.code import git
from relay.executor.dispatch import start_task
from tests.executor.agent.conftest import ResearchWorker
from tests.executor.code.conftest import GOAL_EXCLUDES, github_project, sh
from tests.executor.code.stub_code import PR_URL
from tests.executor.conftest import Seed, task_row, wait_for
from tests.executor.test_reports import Conversation

Db = async_sessionmaker[AsyncSession]


async def _dispatch(db: Db, client: DBOSClient, s: Seed) -> uuid.UUID:
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="code", session_id=s.session_id, client=client
    )
    return started.task_id


def _finish(engine: Engine, task_id: uuid.UUID) -> dict[str, object]:
    wait_for(
        lambda: task_row(engine, task_id)["status"] in ("succeeded", "failed"),
        timeout=120,
        what="code task",
    )
    row = task_row(engine, task_id)
    assert row["status"] == "succeeded", row["error"]
    return row


def _github(worker: ResearchWorker) -> list[dict[str, object]]:
    path = worker.flag_dir / "github.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.usefixtures("no_pending_reports")
async def test_code_commitment_opens_draft_pr_on_new_branch_and_announces_it(
    db: Db,
    seed_code: Callable[[str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    """AC1 + AC4. Bugs caught: no branch/commit in the idea folder, a PR that is not a draft or
    whose body drops the goal, exclusions or test results, and no announced pull_request row."""
    goal = "add a hello function to the widget (code-e2e)"
    s = seed_code(goal)
    folder = worker.flag_dir / "projects" / str(s.idea_id)
    bare = github_project(folder, worker.flag_dir)
    task_id = await _dispatch(db, dbos_client, s)
    _finish(sync_engine, task_id)

    branch = git.branch_for(str(task_id), goal)
    assert branch.startswith(f"relay/{task_id.hex[:8]}-add-a-hello")
    assert sh(folder, "git", "branch", "--show-current") == branch
    assert sh(folder, "git", "show", f"{branch}:hello.py").startswith("def hello()")
    # Pushed to the remote as the task branch only; the remote's main is untouched.
    assert sh(bare, "git", "rev-parse", branch) == sh(folder, "git", "rev-parse", branch)
    assert sh(bare, "git", "rev-parse", "main") == sh(folder, "git", "rev-parse", "main")

    calls = _github(worker)
    created = [c for c in calls if c["method"] == "POST"]
    assert len(created) == 1 and created[0]["path"] == "/api/repos/acme/widget/pulls", calls
    pr = created[0]["json"]
    assert isinstance(pr, dict)
    assert pr["draft"] is True and pr["head"] == branch and pr["base"] == "main"
    body = str(pr["body"])
    assert goal in body and GOAL_EXCLUDES in body
    results = body.split("## Test results", 1)[1]
    assert "python3 -m pytest -q" in results and "exit code" in results, results
    assert not any("merge" in str(c["path"]) for c in calls), calls

    with sync_engine.connect() as c:
        kind, url = c.execute(
            text("SELECT kind, url FROM artifacts WHERE task_id = :t"), {"t": task_id}
        ).one()
    assert (kind, url) == ("pull_request", PR_URL)
    # Announced at the next turn boundary through the Delegator's reports hook.
    notes = await Conversation(db, s.session_id).begin("anything new?")
    assert any("draft pull request" in n for n in notes), notes


async def test_sigkill_mid_code_task_resumes_from_last_completed_step(
    db: Db,
    seed_code: Callable[[str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    """AC3. Bug caught: a crash re-running completed steps (the agent's first model request and
    file writes) instead of resuming after them."""
    token = uuid.uuid4().hex[:8]
    s = seed_code(f"BLOCK-{token} add a hello function (code-kill)")
    github_project(worker.flag_dir / "projects" / str(s.idea_id), worker.flag_dir / token)
    task_id = await _dispatch(db, dbos_client, s)
    marker = worker.flag_dir / f"started-{token}"
    wait_for(marker.exists, timeout=60, what="second model request")

    worker.kill()
    worker.start()
    wait_for(lambda: marker.read_text().count("started") == 2, timeout=60, what="recovery")
    (worker.flag_dir / f"release-{token}").touch()
    _finish(sync_engine, task_id)

    lines = (worker.flag_dir / "code_calls.jsonl").read_text().splitlines()
    calls = [c for c in map(json.loads, lines) if token in c["prompt"]]
    # The first request was checkpointed before the kill: replayed from DBOS, not re-asked.
    assert [c["n"] for c in calls].count(1) == 1, calls
    with sync_engine.connect() as c:
        steps = list(
            c.execute(
                text(
                    "SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = :wf "
                    "ORDER BY function_id"
                ),
                {"wf": f"task-{task_id}"},
            ).scalars()
        )
    for name in ("relay.code.checkout", "relay.code.commit", "relay.code.run_tests"):
        assert steps.count(name) == 1, steps
