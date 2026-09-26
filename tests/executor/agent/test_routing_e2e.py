"""Difficulty routing end to end (TASK-46): real worker, real runner, stub router + models.

The stub router (``stub_models.stub_router``) answers easy/hard from goal markers; the model
refs come from the worker's environment, as they would from ``.env``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from dbos import DBOSClient
from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.executor.dispatch import start_task
from tests.executor.agent.conftest import ResearchWorker, start_research_worker
from tests.executor.conftest import Seed, task_row, wait_for

Db = async_sessionmaker[AsyncSession]

# Configured through the environment (as from .env): proves nothing is hard-coded.
MODEL_ENV = {
    "RESEARCH_EASY_MODEL": "ollama:easy-test:1b",
    "RESEARCH_FALLBACK_MODEL": "ollama:easy-test:1b",
    "RESEARCH_HARD_MODEL": "openai:hard-test",
    "RESEARCH_HARD_FALLBACK_MODEL": "ollama:hard-fallback-test:8b",
    "ROUTER_MODEL": "ollama:router-test:4b",
    # The hard primary is an openai: ref; building it needs a key (never used by the stubs).
    "OPENAI_API_KEY": "sk-test-not-used",
}


@pytest.fixture(scope="module")
def worker(exec_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[ResearchWorker]:
    # A generous research deadline: the replay test blocks a model request across a restart.
    w = start_research_worker(exec_db_url, tmp_path_factory, 60, MODEL_ENV)
    try:
        yield w
    finally:
        w.stop()


async def _start(db: Db, client: DBOSClient, s: Seed) -> uuid.UUID:
    started = await start_task(
        db, commitment_id=s.commitment_id, kind="research", session_id=s.session_id, client=client
    )
    return started.task_id


def _finish(engine: Engine, task_id: uuid.UUID) -> dict[str, object]:
    wait_for(lambda: task_row(engine, task_id)["status"] in ("succeeded", "failed"), timeout=60)
    row = task_row(engine, task_id)
    assert row["status"] == "succeeded", row["error"]
    return row


def _decisions(engine: Engine, task_id: uuid.UUID) -> list[dict[str, Any]]:
    with engine.connect() as c:
        rows = c.execute(
            text("SELECT * FROM router_decisions WHERE task_id = :t"), {"t": task_id}
        ).mappings()
        return [dict(r) for r in rows]


def _models_called(worker: ResearchWorker, marker: str) -> set[str]:
    lines = (worker.flag_dir / "calls.jsonl").read_text().splitlines()
    return {c["model"] for c in map(json.loads, lines) if marker in c["prompt"]}


def _router_calls(worker: ResearchWorker, marker: str) -> int:
    path = worker.flag_dir / "router_calls.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return sum(marker in json.loads(ln)["goal"] for ln in lines)


async def test_easy_task_runs_on_easy_model_and_logs_decision(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    s = seed_research("ROUTE_EASY what an SSH key is (route-easy)", "setup guides")
    task_id = await _start(db, dbos_client, s)
    row = _finish(sync_engine, task_id)

    assert _models_called(worker, "route-easy") == {"stub-easy"}
    assert row["served_model"] == "stub-easy"
    (d,) = _decisions(sync_engine, task_id)
    assert d["turn_id"] is None and d["backend"] == "llm" and d["is_active"] is True
    assert (d["difficulty"], d["router_status"], d["latency_ms"]) == ("easy", "ok", 7)
    assert d["model_chosen"] == "ollama:easy-test:1b"
    # The router saw the commitment's goal and exclusions.
    assert _router_calls(worker, "route-easy") == 1
    log = worker.log.read_text()
    assert f"research task {task_id}: routed easy (router ok, 7 ms) -> ollama:easy-test:1b" in log


async def test_hard_task_runs_on_hard_model(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    s = seed_research("Compare five vector databases on cost and latency (route-hard)", "none")
    task_id = await _start(db, dbos_client, s)
    row = _finish(sync_engine, task_id)

    assert _models_called(worker, "route-hard") == {"stub-hard"}
    assert row["served_model"] == "stub-hard"
    (d,) = _decisions(sync_engine, task_id)
    assert (d["difficulty"], d["router_status"]) == ("hard", "ok")
    assert d["model_chosen"] == "openai:hard-test"
    log = worker.log.read_text()
    assert "-> openai:hard-test then ollama:hard-fallback-test:8b" in log


async def test_hard_model_failure_falls_back_and_records_served_model(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    s = seed_research("PRIMARY_DOWN market size of heat pumps (route-fallback)", "none")
    task_id = await _start(db, dbos_client, s)
    row = _finish(sync_engine, task_id)

    assert _models_called(worker, "route-fallback") == {"stub-hard", "stub-hard-fallback"}
    assert row["served_model"] == "stub-hard-fallback"
    (d,) = _decisions(sync_engine, task_id)
    assert d["difficulty"] == "hard" and d["model_chosen"] == "openai:hard-test"


async def test_router_failure_routes_hard(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    # ROUTE_EASY is overridden: the router raises before it can answer.
    s = seed_research("ROUTER_DOWN ROUTE_EASY what DNS is (route-down)", "none")
    task_id = await _start(db, dbos_client, s)
    row = _finish(sync_engine, task_id)

    assert _models_called(worker, "route-down") == {"stub-hard"}
    assert row["served_model"] == "stub-hard"
    (d,) = _decisions(sync_engine, task_id)
    assert (d["difficulty"], d["router_status"]) == ("hard", "error")
    assert d["model_chosen"] == "openai:hard-test"


async def test_decision_is_made_once_across_sigkill_and_replay(
    db: Db,
    seed_research: Callable[[str, str], Seed],
    dbos_client: DBOSClient,
    sync_engine: Engine,
    worker: ResearchWorker,
) -> None:
    token = uuid.uuid4().hex
    s = seed_research(f"BLOCK-{token} latest open-weight LLMs (route-replay)", "none")
    task_id = await _start(db, dbos_client, s)
    started = worker.flag_dir / f"started-{token}"
    # The first model request runs only after the route steps were recorded.
    wait_for(started.exists, what="first hard-model request")
    assert len(_decisions(sync_engine, task_id)) == 1

    worker.kill()
    worker.start()
    # DBOS recovery re-enters the interrupted model request, not the routing steps.
    wait_for(lambda: started.read_text().count("started") == 2, what="recovered request")
    (worker.flag_dir / f"release-{token}").touch()
    row = _finish(sync_engine, task_id)

    assert row["served_model"] == "stub-hard"
    assert _router_calls(worker, "route-replay") == 1
    assert len(_decisions(sync_engine, task_id)) == 1
    with sync_engine.connect() as c:
        names = (
            c.execute(
                text("SELECT function_name FROM dbos.operation_outputs WHERE workflow_uuid = :wf"),
                {"wf": f"task-{task_id}"},
            )
            .scalars()
            .all()
        )
    assert names.count("relay.research.route") == 1
    assert names.count("relay.research.record_route") == 1
