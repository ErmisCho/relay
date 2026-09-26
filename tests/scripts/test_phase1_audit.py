"""phase1_audit: each test pins one way the TASK-31 audit could lie about a live session."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

from sqlalchemy import Connection, insert

from relay.store.models import Commitment, Idea, Session, Task, Turn

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "phase1_audit.py"
# Far in the past, so rows other tests create at now() never fall into the audit window.
T0 = datetime(2020, 1, 1, 10, 0, tzinfo=UTC)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase1_audit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load()


def _session(conn: Connection, *, ended: bool = True) -> uuid.UUID:
    sid = uuid.uuid4()
    conn.execute(
        insert(Session).values(
            id=sid,
            started_at=T0,
            ended_at=T0 + timedelta(minutes=10) if ended else None,
            wake_trigger="hey_jarvis",
        )
    )
    return sid


def _dispatch(conn: Connection, *, assent: str, at: datetime) -> str:
    idea = conn.execute(insert(Idea).values(title="idea").returning(Idea.id)).scalar_one()
    cid = conn.execute(
        insert(Commitment)
        .values(
            idea_id=idea,
            goal="compare vector DBs",
            scope_excludes="pricing",
            artifact_kind="document",
            readback_text="Just to confirm: ... Should I start on it?",
            assent_utterance=assent,
            assented_at=at,
            created_at=at,
        )
        .returning(Commitment.id)
    ).scalar_one()
    task = conn.execute(
        insert(Task)
        .values(commitment_id=cid, kind="research", status="succeeded", created_at=at)
        .returning(Task.id)
    ).scalar_one()
    return str(task)


def test_task_without_assent_is_an_unintended_dispatch(db_conn: Connection) -> None:
    sid = _session(db_conn)
    ok = _dispatch(db_conn, assent="yes, go ahead", at=T0 + timedelta(minutes=2))
    bad = _dispatch(db_conn, assent="  ", at=T0 + timedelta(minutes=5))

    report = audit.audit_session(db_conn, sid)

    flagged = {t["id"]: t["unintended_dispatch"] for t in report["tasks"]}
    assert flagged == {ok: False, bad: True}
    assert any("unintended dispatch" in v and bad in v for v in report["violations"])


def test_latency_percentiles_are_per_model(db_conn: Connection) -> None:
    sid = _session(db_conn)
    rows = [("gemma", ms) for ms in (100, 200, 300, 400, 500)] + [("luna", 1000), ("luna", 3000)]
    for i, (model, ms) in enumerate(rows):
        db_conn.execute(
            insert(Turn).values(
                session_id=sid,
                role="assistant",
                text="hi",
                ts=T0 + timedelta(seconds=i),
                model_used=model,
                latency_ms=ms,
            )
        )
    # User turns carry no TTFT and must not dilute the numbers.
    db_conn.execute(insert(Turn).values(session_id=sid, role="user", text="q", latency_ms=9999))

    by_model = audit.audit_session(db_conn, sid)["turns"]["ttft_ms_by_model"]

    assert by_model["gemma"] == {"count": 5, "p50": 300.0, "p90": 460.0}
    assert by_model["luna"] == {"count": 2, "p50": 2000.0, "p90": 2800.0}


def test_session_that_never_ended_is_not_auto_closed(db_conn: Connection) -> None:
    sid = _session(db_conn, ended=False)

    report = audit.audit_session(db_conn, sid)

    assert report["session"]["auto_closed"] is False
    assert any("not auto-closed" in v for v in report["violations"])
    assert report["cost"]["tokens"] == "not recorded"
