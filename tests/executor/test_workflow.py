"""Dispatch, durable execution across SIGKILL, idempotency, failure and status."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from dbos import DBOSClient
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.config import Settings
from relay.delegator.contracts import SessionState, ToolContext
from relay.delegator.tools.status import GetStatusTool
from relay.executor.common import WORKFLOW_NAME
from relay.executor.dispatch import start_task
from relay.executor.status import get_status
from tests.executor.conftest import Seed, Worker, task_row, wait_for

Db = async_sessionmaker[AsyncSession]


def _fixed(client: DBOSClient) -> Callable[[Settings], Awaitable[DBOSClient | None]]:
    async def factory(_settings: Settings) -> DBOSClient | None:
        return client

    return factory


def _count(engine: Engine, sql: str, **params: object) -> int:
    with engine.connect() as c:
        return int(c.execute(text(sql), params).scalar_one())


async def test_dispatch_returns_fast_and_persists_workflow_id(
    db: Db, seed: Callable[[str], Seed], dbos_client: DBOSClient, sync_engine: Engine
) -> None:
    s = seed("beach wedding venues")
    t0 = time.perf_counter()
    started = await start_task(
        db,
        commitment_id=s.commitment_id,
        kind="research",
        session_id=s.session_id,
        client=dbos_client,
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, f"dispatch took {elapsed * 1000:.0f} ms"
    assert started.created and started.workflow_id == f"task-{started.task_id}"
    assert task_row(sync_engine, started.task_id)["dbos_workflow_id"] == started.workflow_id

    wait_for(lambda: task_row(sync_engine, started.task_id)["status"] == "succeeded")
    n = _count(sync_engine, "SELECT count(*) FROM artifacts WHERE task_id = :t", t=started.task_id)
    assert n == 1
    with sync_engine.connect() as c:
        idea_status, report_session = c.execute(
            text(
                "SELECT i.status, r.session_id FROM ideas i JOIN pending_reports r "
                "ON r.idea_id = i.id WHERE r.task_id = :t"
            ),
            {"t": started.task_id},
        ).one()
    assert (idea_status, report_session) == ("delivered", s.session_id)


async def test_same_commitment_twice_starts_one_workflow(
    db: Db,
    seed: Callable[[str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: Worker,
) -> None:
    s = seed("SLOW honeymoon ideas")
    kw = {"commitment_id": s.commitment_id, "kind": "research", "client": dbos_client}
    first, second = await asyncio.gather(start_task(db, **kw), start_task(db, **kw))  # type: ignore[arg-type]
    third = await start_task(db, **kw)  # type: ignore[arg-type]

    assert first.task_id == second.task_id == third.task_id
    assert [first.created, second.created, third.created].count(True) == 1
    sql = "SELECT count(*) FROM tasks WHERE commitment_id = :c"
    assert _count(sync_engine, sql, c=s.commitment_id) == 1
    wfs = await dbos_client.list_workflows_async(
        workflow_id_prefix=f"task-{first.task_id}", name=WORKFLOW_NAME
    )
    assert len(wfs) == 1
    (worker.flag_dir / f"release-{first.task_id}").touch()
    wait_for(lambda: task_row(sync_engine, first.task_id)["status"] == "succeeded")


async def test_sigkill_mid_workflow_resumes_after_restart(
    db: Db,
    seed: Callable[[str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: Worker,
) -> None:
    s = seed("SLOW crash-proof research")
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="research", client=dbos_client
    )
    marker = worker.flag_dir / f"started-{started.task_id}"
    wait_for(marker.exists, what="runner to start")

    worker.kill()
    assert task_row(sync_engine, started.task_id)["status"] == "running"
    worker.start()
    # DBOS recovery re-enters the interrupted runner step in the new process.
    wait_for(lambda: marker.read_text().count("started") == 2, what="recovered runner")
    (worker.flag_dir / f"release-{started.task_id}").touch()

    wait_for(lambda: task_row(sync_engine, started.task_id)["status"] == "succeeded")
    row = task_row(sync_engine, started.task_id)
    assert row["started_at"] is not None and row["finished_at"] is not None
    for table in ("artifacts", "pending_reports"):
        sql = f"SELECT count(*) FROM {table} WHERE task_id = :t"
        assert _count(sync_engine, sql, t=started.task_id) == 1


async def test_runner_failure_marks_task_failed_and_status_says_so(
    db: Db, seed: Callable[[str], Seed], dbos_client: DBOSClient, sync_engine: Engine
) -> None:
    s = seed("FAIL doomed research")
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="research", client=dbos_client
    )
    wait_for(lambda: task_row(sync_engine, started.task_id)["status"] == "failed")
    row = task_row(sync_engine, started.task_id)
    assert "runner exploded" in str(row["error"]) and row["finished_at"] is not None
    sql = "SELECT count(*) FROM artifacts WHERE task_id = :t"
    assert _count(sync_engine, sql, t=started.task_id) == 0

    (status,) = await get_status(db, task_id=started.task_id, client=dbos_client)
    assert status.status == "failed" and status.workflow_status == "SUCCESS"

    tool = GetStatusTool(client_factory=_fixed(dbos_client))
    state = SessionState(session_id=s.session_id, current_idea_id=s.idea_id)
    ctx = ToolContext(state=state, db=db, settings=Settings())
    result = await tool({}, ctx)
    assert "failed" in result.content and "runner exploded" in result.content


async def test_get_status_tool_reports_done_task_for_current_idea(
    db: Db, seed: Callable[[str], Seed], dbos_client: DBOSClient, sync_engine: Engine
) -> None:
    s = seed("mountain cabins")
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="research", client=dbos_client
    )
    wait_for(lambda: task_row(sync_engine, started.task_id)["status"] == "succeeded")

    tool = GetStatusTool(client_factory=_fixed(dbos_client))
    state = SessionState(session_id=s.session_id, current_idea_id=s.idea_id)
    result = await tool({}, ToolContext(state=state, db=db, settings=Settings()))
    assert "is done" in result.content and "three candidate venues" in result.content
    by_task = await tool(
        {"task_id": str(started.task_id)}, ToolContext(state=state, db=db, settings=Settings())
    )
    assert by_task.content == result.content


async def test_worker_startup_enqueues_task_orphaned_between_commit_and_enqueue(
    seed: Callable[[str], Seed], dbos_client: DBOSClient, sync_engine: Engine, worker: Worker
) -> None:
    s = seed("orphaned lighthouse research")
    task_id = uuid.uuid4()
    # What start_task leaves behind if the Delegator dies right after COMMIT.
    with sync_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO tasks (id, commitment_id, kind, status, dbos_workflow_id) "
                "VALUES (:id, :c, 'research', 'queued', :wf)"
            ),
            {"id": task_id, "c": s.commitment_id, "wf": f"task-{task_id}"},
        )
    worker.stop()
    worker.start()
    wait_for(lambda: task_row(sync_engine, task_id)["status"] == "succeeded", what="reconcile")
    wfs = await dbos_client.list_workflows_async(workflow_ids=[f"task-{task_id}"])
    assert len(wfs) == 1
