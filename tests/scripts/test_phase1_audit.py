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


def _session(
    conn: Connection, *, ended: bool = True, start: datetime = T0, reason: str | None = None
) -> uuid.UUID:
    sid = uuid.uuid4()
    conn.execute(
        insert(Session).values(
            id=sid,
            started_at=start,
            ended_at=start + timedelta(minutes=10) if ended else None,
            end_reason=reason,
            wake_trigger="hey_jarvis",
        )
    )
    return sid


def _dispatch(
    conn: Connection,
    *,
    assent: str,
    at: datetime,
    session: uuid.UUID | None = None,
    task_at: datetime | None = None,
    tasks: int = 1,
) -> str:
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
            session_id=session,
        )
        .returning(Commitment.id)
    ).scalar_one()
    for _ in range(tasks):
        conn.execute(
            insert(Task).values(
                commitment_id=cid, kind="research", status="succeeded", created_at=task_at or at
            )
        )
    return str(cid)


def test_task_without_assent_is_an_unintended_dispatch(db_conn: Connection) -> None:
    sid = _session(db_conn)
    ok = _dispatch(db_conn, assent="yes, go ahead", at=T0 + timedelta(minutes=2))
    bad = _dispatch(db_conn, assent="  ", at=T0 + timedelta(minutes=5))

    report = audit.audit_session(db_conn, sid)

    flagged = {t["commitment_id"]: t["unintended_dispatch"] for t in report["tasks"]}
    assert flagged == {ok: False, bad: True}
    assert sum("unintended dispatch" in v for v in report["violations"]) == 1


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


def test_commitments_link_by_session_id_and_fall_back_to_the_window_only_when_unlinked(
    db_conn: Connection,
) -> None:
    # Two overlapping calls: the time window alone would hand each the other's commitment.
    sid = _session(db_conn, start=T0 + timedelta(days=1))
    other = _session(db_conn, start=T0 + timedelta(days=1, minutes=1))
    in_window = T0 + timedelta(days=1, minutes=3)
    mine = _dispatch(db_conn, assent="yes", at=in_window, session=sid)
    _dispatch(db_conn, assent="yes", at=in_window, session=other)
    # Linked but recorded long after the call (a late retry): still this session's.
    late = _dispatch(db_conn, assent="yes", at=T0 + timedelta(days=2), session=sid)
    legacy = _dispatch(db_conn, assent="yes", at=in_window)  # pre-0003 row: no session_id

    report = audit.audit_session(db_conn, sid)

    linked = {c["id"]: c["linked_by"] for c in report["commitments"]}
    assert linked == {mine: "session_id", late: "session_id", legacy: "time_window"}
    assert {t["commitment_id"] for t in report["tasks"]} == set(linked)


def test_commitment_with_two_tasks_is_a_violation(db_conn: Connection) -> None:
    sid = _session(db_conn, start=T0 + timedelta(days=3))
    cid = _dispatch(db_conn, assent="yes", at=T0 + timedelta(days=3, minutes=2), session=sid,
                    tasks=2)

    report = audit.audit_session(db_conn, sid)

    assert report["violations"] == [f"commitment {cid} led to 2 tasks (expected exactly one)"]


def test_task_created_before_the_assent_is_an_unintended_dispatch(db_conn: Connection) -> None:
    sid = _session(db_conn, start=T0 + timedelta(days=4))
    at = T0 + timedelta(days=4, minutes=2)
    _dispatch(db_conn, assent="yes", at=at, session=sid, task_at=at - timedelta(seconds=5))

    report = audit.audit_session(db_conn, sid)

    assert [t["unintended_dispatch"] for t in report["tasks"]] == [True]
    assert any("task created before the assent was recorded" in v for v in report["violations"])


def test_end_reason_and_token_totals_are_reported(db_conn: Connection) -> None:
    sid = _session(db_conn, start=T0 + timedelta(days=5), reason="silence_timeout")
    for usage in ({"input_tokens": 10, "output_tokens": 4}, {"input_tokens": 5}):
        db_conn.execute(
            insert(Turn).values(session_id=sid, role="assistant", text="a", meta={"usage": usage})
        )

    report = audit.audit_session(db_conn, sid)

    assert report["session"]["end_reason"] == "silence_timeout"
    assert report["cost"]["tokens"] == {"input_tokens": 15, "output_tokens": 4}
