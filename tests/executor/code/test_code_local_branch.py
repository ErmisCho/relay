"""No GitHub token: the code task still succeeds, as a committed branch in the project folder."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from dbos import DBOSClient
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.executor.dispatch import start_task
from tests.executor.agent.conftest import ResearchWorker
from tests.executor.code.conftest import code_worker, sh
from tests.executor.conftest import Seed, task_row, wait_for


@pytest.fixture(scope="module")
def worker(exec_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ResearchWorker]:
    w = code_worker(exec_db_url, tmp_path_factory, token="")
    try:
        yield w
    finally:
        w.stop()


async def test_no_token_leaves_a_local_branch_artifact_and_succeeds(
    db: async_sessionmaker[AsyncSession],
    seed_code: Callable[[str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    """Bugs caught: failing the task (or calling GitHub) when no token is configured, and a
    fresh project folder that is not made a repository with the change on its task branch."""
    s = seed_code("add a hello function (code-local)")
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="code", session_id=s.session_id, client=dbos_client
    )
    wait_for(lambda: task_row(sync_engine, started.task_id)["status"] != "queued", timeout=5)
    wait_for(
        lambda: task_row(sync_engine, started.task_id)["status"] in ("succeeded", "failed"),
        timeout=120,
    )
    row = task_row(sync_engine, started.task_id)
    assert row["status"] == "succeeded", row["error"]

    folder = worker.flag_dir / "projects" / str(s.idea_id)
    branch = sh(folder, "git", "branch", "--show-current")
    assert branch.startswith(f"relay/{started.task_id.hex[:8]}-")
    assert "hello.py" in sh(folder, "git", "show", "--name-only", "--format=", branch)
    with sync_engine.connect() as c:
        kind, url, summary = c.execute(
            text("SELECT kind, url, summary FROM artifacts WHERE task_id = :t"),
            {"t": started.task_id},
        ).one()
    assert kind == "branch" and url == folder.resolve().as_uri()
    assert branch in summary and "no GitHub token" in summary and "changed" in summary
    assert not (worker.flag_dir / "github.jsonl").exists()
