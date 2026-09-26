"""Demo API (TASK-42) AC#2 and AC#4 gaps: current_idea, ready_gate and artifact_delivered on the
feed, and the response shapes of the read endpoints the website (``web/src/api/contract.ts``)
relies on.

Real app, real test database; only the chat model and the label models are doubled. Rows the
executor worker would write (tasks, artifacts) are inserted directly, which is exactly what the
Delegator's task poller observes in production.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.commitment import CommitmentHook
from relay.delegator.demo import api as demo_api
from relay.delegator.demo.events import BUS
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Artifact, Commitment, Idea, IdeaEdge, PendingReport, Task

from ..commitment.harness import FakeLabelModel, build_convo
from ..conftest import ScriptedChatModel, make_settings, text
from ..test_tools import tool_call
from .test_demo import PASSCODE, client, demo_app, login

# Key sets of web/src/api/contract.ts; a rename on either side must fail here, not in a browser.
IDEA_KEYS = {"id", "title", "summary", "status", "created_at", "updated_at"}
EDGE_KEYS = {"from_idea", "to_idea", "relation"}
COMMITMENT_KEYS = {
    "id",
    "idea_id",
    "goal",
    "scope_excludes",
    "artifact_kind",
    "readback_text",
    "assent_utterance",
    "assented_at",
    "created_at",
}
TASK_KEYS = {
    "id",
    "commitment_id",
    "kind",
    "status",
    "dbos_workflow_id",
    "started_at",
    "finished_at",
    "error",
    "artifact_id",
}
ARTIFACT_KEYS = {
    "id",
    "task_id",
    "idea_id",
    "kind",
    "title",
    "summary",
    "markdown",
    "sources",
    "created_at",
}
DELIVERED_KEYS = {"artifact_id", "task_id", "idea_id", "title", "summary", "markdown", "sources"}

BRIEF = """# Bike locks that know your phone

Summary line.

- [Lock review](https://example.org/locks) says the bolt holds.
- Again [the same review](https://example.org/locks) and [BLE spec](https://bt.example/spec).
- A local path is not a source: [notes](file:///Users/x/notes.md).
"""


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture(autouse=True)
def clean_bus() -> Iterator[None]:
    yield
    BUS.enabled = False
    BUS._channels.clear()
    BUS.watched_tasks.clear()


def types(session_id: str) -> list[str]:
    return [e["type"] for e in BUS.history(session_id)]


async def wait_for(session_id: str, type_: str, n: int = 1, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while types(session_id).count(type_) < n:
            await asyncio.sleep(0.01)


async def seed(
    db: async_sessionmaker[AsyncSession], artifacts_dir: Path, *, status: str = "succeeded"
) -> dict[str, Any]:
    """Two linked ideas, a commitment with verbatim assent, a task and (optionally) its brief."""
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)
    brief = artifacts_dir / f"{tag}.md"
    brief.write_text(BRIEF, encoding="utf-8")
    async with db() as s, s.begin():
        parent = Idea(title=f"bike locks {tag}", status="exploring")
        child = Idea(title=f"phone-proximity lock {tag}", status="delivered", summary="BLE")
        s.add_all([parent, child])
        await s.flush()
        s.add(IdeaEdge(from_idea=child.id, to_idea=parent.id, relation="refines"))
        commitment = Commitment(
            idea_id=child.id,
            goal=f"research {tag} locks",
            scope_excludes="pricing",
            artifact_kind="document",
            readback_text=f"Just to confirm: research {tag} locks. Should I start on it?",
            assent_utterance="Yeah, sure, go for it.",
            assented_at=now,
        )
        s.add(commitment)
        await s.flush()
        task = Task(
            commitment_id=commitment.id,
            kind="research",
            status=status,
            dbos_workflow_id=f"task-{tag}",
        )
        s.add(task)
        await s.flush()
    return {
        "parent": parent,
        "child": child,
        "commitment": commitment,
        "task": task,
        "brief": brief,
    }


async def add_artifact(
    db: async_sessionmaker[AsyncSession], task_id: uuid.UUID, url: str
) -> Artifact:
    async with db() as s, s.begin():
        art = Artifact(task_id=task_id, kind="document", url=url, summary="Two locks qualify.")
        s.add(art)
    return art


# --- AC#2: current_idea ----------------------------------------------------------------


async def test_focus_idea_emits_current_idea_with_status_and_edges(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # Catches: current_idea missing from the feed (the thinking panel's "current idea" never
    # fills), emitted after the reply instead of at the tool call, the wrong `change` label, or
    # edges not loaded (the trace cannot show "refines …").
    rows = await seed(db, tmp_path)
    child: Idea = rows["child"]
    app = demo_app(db)
    model: ScriptedChatModel = app.state.service.chat_model
    new_title = f"zq lock idea {uuid.uuid4().hex[:6]}"
    model.scripts = [
        tool_call("focus_idea", json.dumps({"idea_id": str(child.id)})),
        text("Back on the phone lock."),
        tool_call("focus_idea", json.dumps({"new_title": new_title}), "call_2"),
        text("New idea started."),
    ]
    async with client(app) as c:
        cookie = await login(c)
        sid = (await c.post("/demo/sessions", headers=cookie)).json()["session_id"]
        for n, line in enumerate(["Back to the phone lock idea.", "New idea: a zq lock."], 1):
            resp = await c.post(
                f"/demo/sessions/{sid}/messages", json={"text": line}, headers=cookie
            )
            assert resp.status_code == 202
            await wait_for(sid, "assistant_turn", n)

    assert types(sid) == [
        "user_turn",
        "current_idea",
        "assistant_turn",
        "user_turn",
        "current_idea",
        "assistant_turn",
    ]
    switched, created = (e["data"] for e in BUS.history(sid) if e["type"] == "current_idea")
    assert switched == {
        "idea_id": str(child.id),
        "title": child.title,
        "status": "delivered",
        "change": "switched",
        "edges": [
            {"from_idea": str(child.id), "to_idea": str(rows["parent"].id), "relation": "refines"}
        ],
    }
    assert created["change"] == "created" and created["title"] == new_title
    assert created["status"] == "exploring" and created["edges"] == []


# --- AC#2: ready_gate --------------------------------------------------------------------


async def test_ready_score_is_emitted_as_ready_gate(db: async_sessionmaker[AsyncSession]) -> None:
    # Catches: the ready score being logged to router_decisions but never reaching the feed, or
    # reaching it with a verdict the site does not know (it renders keep_talking /
    # ready_to_execute only).
    hook = CommitmentHook(
        assent_model=FakeLabelModel("affirmative"),
        ready_model=FakeLabelModel("ready_to_execute"),
        score_ready=True,
        ready_idle_s=0.05,
    )
    c = build_convo(db, hook=hook, demo_passcode=PASSCODE)
    await c.turn("Research bike locks, skip pricing, I'm ready.", text("Sounds good."))
    await wait_for(c.session_id, "ready_gate")
    assert types(c.session_id) == ["user_turn", "assistant_turn", "ready_gate"]
    (gate,) = (e["data"] for e in BUS.history(c.session_id) if e["type"] == "ready_gate")
    assert gate == {"idea_id": None, "verdict": "ready_to_execute", "reason": None}


# --- AC#2: task_status + artifact_delivered from the worker's rows -------------------------


async def test_poller_turns_worker_rows_into_status_and_delivery_events(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # Catches: task progress written by the (separate) worker process never reaching the
    # session's stream; artifact_delivered emitted before the artifact row exists (empty brief)
    # or never when the row lands a poll later; a finished task left watched (re-emitted
    # forever); sources not extracted, duplicated, or including non-http links.
    rows = await seed(db, tmp_path, status="running")
    task: Task = rows["task"]
    settings = make_settings(demo_passcode=PASSCODE, artifacts_dir=str(tmp_path))
    app = demo_app(db, artifacts_dir=str(tmp_path))
    rt = demo_api.DemoRuntime(settings, app.state.service, db)
    BUS.enabled = True
    sid = str(uuid.uuid4())
    BUS.watched_tasks[task.id] = (sid, "queued")

    await rt.poll_once()
    assert [(e["type"], e["data"]["status"]) for e in BUS.history(sid)] == [
        ("task_status", "running")
    ]
    await rt.poll_once()  # unchanged row: nothing new
    assert len(BUS.history(sid)) == 1

    async with db() as s, s.begin():
        await s.execute(update(Task).where(Task.id == task.id).values(status="succeeded"))
    await rt.poll_once()  # succeeded, but the artifact row is not written yet
    assert types(sid) == ["task_status", "task_status"]
    assert task.id in BUS.watched_tasks

    art = await add_artifact(db, task.id, rows["brief"].as_uri())
    await rt.poll_once()
    assert types(sid) == ["task_status", "task_status", "artifact_delivered"]
    assert task.id not in BUS.watched_tasks
    delivered = BUS.history(sid)[-1]["data"]
    assert set(delivered) == DELIVERED_KEYS
    assert delivered["artifact_id"] == str(art.id)
    assert delivered["task_id"] == str(task.id)
    assert delivered["idea_id"] == str(rows["child"].id)
    assert delivered["title"] == "Bike locks that know your phone"
    assert delivered["summary"] == "Two locks qualify."
    assert delivered["markdown"] == BRIEF
    assert delivered["sources"] == [
        {"title": "Lock review", "url": "https://example.org/locks"},
        {"title": "BLE spec", "url": "https://bt.example/spec"},
    ]
    await rt.poll_once()
    assert len(BUS.history(sid)) == 3


async def test_poller_reports_a_failed_task_once(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # Catches: a failed task's error not reaching the card, or the task staying watched and
    # being polled (and possibly re-emitted) forever.
    rows = await seed(db, tmp_path, status="failed")
    task: Task = rows["task"]
    async with db() as s, s.begin():
        await s.execute(update(Task).where(Task.id == task.id).values(error="model timed out"))
    app = demo_app(db)
    rt = demo_api.DemoRuntime(make_settings(demo_passcode=PASSCODE), app.state.service, db)
    BUS.enabled = True
    sid = str(uuid.uuid4())
    BUS.watched_tasks[task.id] = (sid, "running")
    await rt.poll_once()
    await rt.poll_once()
    assert [e["data"] for e in BUS.history(sid)] == [
        {"task_id": str(task.id), "status": "failed", "error": "model timed out"}
    ]
    assert task.id not in BUS.watched_tasks


# --- AC#4: read endpoint shapes ---------------------------------------------------------


async def test_read_endpoints_return_the_contract_shapes(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # Catches: a read endpoint drifting from web/src/api/contract.ts (renamed/missing key), the
    # audit trail losing the verbatim assent words or the read-back, edges or the idea status
    # missing, a task without its artifact id, the brief served without its Markdown/sources,
    # a file outside ARTIFACTS_DIR being read, and ?session_id= not scoping the task list.
    rows = await seed(db, tmp_path)
    task: Task = rows["task"]
    art = await add_artifact(db, task.id, rows["brief"].as_uri())
    outside = await seed(db, tmp_path / "..", status="succeeded")  # brief outside ARTIFACTS_DIR
    escaped = await add_artifact(db, outside["task"].id, outside["brief"].as_uri())
    app = demo_app(db, artifacts_dir=str(tmp_path))
    async with client(app) as c:
        cookie = await login(c)
        sid = (await c.post("/demo/sessions", headers=cookie)).json()["session_id"]
        async with db() as s, s.begin():
            s.add(PendingReport(session_id=uuid.UUID(sid), task_id=task.id, summary="done"))

        ideas = (await c.get("/demo/ideas", headers=cookie)).json()
        commitments = (await c.get("/demo/commitments", headers=cookie)).json()
        tasks = (await c.get("/demo/tasks", headers=cookie)).json()
        scoped = (await c.get(f"/demo/tasks?session_id={sid}", headers=cookie)).json()
        artifact = await c.get(f"/demo/artifacts/{art.id}", headers=cookie)
        leaked = (await c.get(f"/demo/artifacts/{escaped.id}", headers=cookie)).json()
        missing = await c.get(f"/demo/artifacts/{uuid.uuid4()}", headers=cookie)
        garbage = await c.get("/demo/artifacts/not-a-uuid", headers=cookie)

    assert set(ideas) == {"ideas", "edges"}
    by_id = {i["id"]: i for i in ideas["ideas"]}
    child = by_id[str(rows["child"].id)]
    assert set(child) == IDEA_KEYS
    assert (child["status"], child["summary"]) == ("delivered", "BLE")
    assert by_id[str(rows["parent"].id)]["status"] == "exploring"
    edge = {"from_idea": str(rows["child"].id), "to_idea": str(rows["parent"].id)}
    assert {**edge, "relation": "refines"} in ideas["edges"]
    assert all(set(e) == EDGE_KEYS for e in ideas["edges"])

    (com,) = (x for x in commitments["commitments"] if x["id"] == str(rows["commitment"].id))
    assert set(com) == COMMITMENT_KEYS
    assert com["assent_utterance"] == "Yeah, sure, go for it."
    assert com["readback_text"] == rows["commitment"].readback_text
    assert com["idea_id"] == str(rows["child"].id)

    (t,) = (x for x in tasks["tasks"] if x["id"] == str(task.id))
    assert set(t) == TASK_KEYS
    assert (t["status"], t["artifact_id"], t["dbos_workflow_id"]) == (
        "succeeded",
        str(art.id),
        task.dbos_workflow_id,
    )
    assert [x["id"] for x in scoped["tasks"]] == [str(task.id)]

    assert artifact.status_code == 200
    body = artifact.json()
    assert set(body) == ARTIFACT_KEYS
    assert body["markdown"] == BRIEF and body["title"] == "Bike locks that know your phone"
    assert body["sources"][0] == {"title": "Lock review", "url": "https://example.org/locks"}
    assert body["idea_id"] == str(rows["child"].id) and body["kind"] == "document"
    assert leaked["markdown"] == "" and leaked["sources"] == []
    assert missing.status_code == 404 and missing.json() == {"error": "not_found"}
    assert garbage.status_code == 404
