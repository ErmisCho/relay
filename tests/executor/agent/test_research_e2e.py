"""Research commitments end to end: real worker subprocess, real runner, stub models."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dbos import DBOSClient
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.executor.dispatch import start_task
from tests.executor.agent.conftest import ResearchWorker
from tests.executor.agent.stub_models import STUB_BODY, STUB_SOURCES, STUB_TITLE
from tests.executor.conftest import Seed, task_row, wait_for

Db = async_sessionmaker[AsyncSession]


def _calls(worker: ResearchWorker, marker: str) -> list[dict[str, str]]:
    lines = (worker.flag_dir / "calls.jsonl").read_text().splitlines()
    return [c for c in map(json.loads, lines) if marker in c["prompt"]]


async def _run(
    db: Db, client: DBOSClient, engine: Engine, s: Seed, expect: str = "succeeded"
) -> uuid.UUID:
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="research", session_id=s.session_id, client=client
    )
    wait_for(
        lambda: task_row(engine, started.task_id)["status"] in ("succeeded", "failed"),
        timeout=60,
    )
    row = task_row(engine, started.task_id)
    assert row["status"] == expect, row["error"]
    return started.task_id


def _artifact(engine: Engine, task_id: uuid.UUID) -> tuple[str, str, str]:
    with engine.connect() as c:
        kind, url, summary = c.execute(
            text("SELECT kind, url, summary FROM artifacts WHERE task_id = :t"), {"t": task_id}
        ).one()
    return kind, url, summary


async def test_research_commitment_writes_markdown_document(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    goal = "Compare solar kettles for camping (e2e-doc)"
    excludes = "Anything about prices, shops or where to buy; do NOT mention brand Zebrakettle."
    s = seed_research(goal, excludes)
    task_id = await _run(db, dbos_client, sync_engine, s)

    kind, url, summary = _artifact(sync_engine, task_id)
    path = Path(unquote(urlsplit(url).path))
    assert kind == "document" and url.startswith("file://")
    assert path == worker.artifacts_dir.resolve() / str(s.idea_id) / f"{task_id}.md"
    doc = path.read_text()
    assert doc.startswith(f"# {STUB_TITLE}\n")
    assert STUB_BODY in doc and summary in doc
    sources = doc.split("## Sources", 1)[1]
    assert all(f"<{u}>" in sources for u in STUB_SOURCES)
    # Rendering adds nothing beyond the brief: no excluded content sneaks in.
    assert "Zebrakettle" not in doc and "price" not in doc.lower()

    with sync_engine.connect() as c:
        idea_status: str = c.execute(
            text("SELECT status FROM ideas WHERE id = :i"), {"i": s.idea_id}
        ).scalar_one()
    assert idea_status == "delivered"

    # The model saw the goal and the exclusions verbatim, and used the fetch tool step.
    calls = _calls(worker, "e2e-doc")
    assert calls and all(goal in c["prompt"] and excludes in c["prompt"] for c in calls)
    assert "Out of scope" in calls[0]["prompt"] and "never send" in calls[0]["instructions"]
    # No routing marker: the stub router says hard, so the hard primary served everything.
    assert len(calls) == 2 and {c["model"] for c in calls} == {"stub-hard"}
    assert task_row(sync_engine, task_id)["served_model"] == "stub-hard"

    # Durability: model requests and the tool call are checkpointed steps of the agent's
    # child workflow, so a crash mid-research resumes instead of re-asking the model.
    with sync_engine.connect() as c:
        steps: list[str] = list(
            c.execute(
                text(
                    "SELECT o.function_name FROM dbos.operation_outputs o "
                    "JOIN dbos.workflow_status w ON w.workflow_uuid = o.workflow_uuid "
                    "WHERE w.name = 'relay.research.run' AND w.workflow_uuid LIKE :wf "
                    "ORDER BY o.function_id"
                ),
                {"wf": f"task-{task_id}%"},
            )
            .scalars()
            .all()
        )
    assert steps.count("relay_research__model.request") == 2, steps
    assert "relay_research__dynamic_toolset__executor.call_tool" in steps, steps
    # The harness fetch really ran (not an unknown-tool retry); test_tools pins the refusal.
    fetches = (worker.flag_dir / "fetch_results.jsonl").read_text().splitlines()
    fetched = [json.loads(ln)["result"] for ln in fetches if "e2e-doc" in ln]
    assert fetched and "Unknown tool" not in fetched[0], fetched
    # The routing decision is a step of the task workflow itself, recorded before research.
    with sync_engine.connect() as c:
        task_steps: list[str] = list(
            c.execute(
                text(
                    "SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = :wf "
                    "ORDER BY function_id"
                ),
                {"wf": f"task-{task_id}"},
            )
            .scalars()
            .all()
        )
    assert task_steps[:3] == [
        "relay.task.start",
        "relay.research.route",
        "relay.research.record_route",
    ], task_steps


async def test_primary_model_failure_falls_back_without_failing_task(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    s = seed_research("PRIMARY_DOWN tide tables for Brittany (e2e-fallback)", "fishing")
    task_id = await _run(db, dbos_client, sync_engine, s)

    kind, url, _ = _artifact(sync_engine, task_id)
    assert kind == "document" and Path(unquote(urlsplit(url).path)).is_file()
    models = [c["model"] for c in _calls(worker, "e2e-fallback")]
    assert models == ["stub-hard", "stub-hard-fallback", "stub-hard", "stub-hard-fallback"]
    # The worker log names the model that actually served each request.
    served = [ln for ln in worker.log.read_text().splitlines() if "served by" in ln]
    assert any("['stub-hard-fallback', 'stub-hard-fallback']" in ln for ln in served), served
    # ...and the task records the fallback as the served model.
    assert task_row(sync_engine, task_id)["served_model"] == "stub-hard-fallback"


async def test_research_run_is_bounded_by_workflow_timeout(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    # The first model request outlives the worker's 3 s research deadline.
    s = seed_research("HANG forever on the first model call (e2e-timeout)", "none")
    task_id = await _run(db, dbos_client, sync_engine, s, expect="failed")
    assert "did not finish within 3 s" in str(task_row(sync_engine, task_id)["error"])
    with sync_engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM artifacts WHERE task_id = :t"), {"t": task_id})
        assert n.scalar_one() == 0
