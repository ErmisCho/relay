"""Acceptance tests for TASK-21: the 0001_initial migration against a real Postgres."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from relay.config import to_sync_url
from tests.conftest import alembic_config

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "ideas": {"id", "title", "summary", "status", "maturity", "created_at", "updated_at"},
    "sessions": {"id", "started_at", "ended_at", "wake_trigger"},
    "turns": {
        "id", "session_id", "idea_id", "role", "text", "ts", "route", "model_used",
        "latency_ms", "metadata",
    },
    "commitments": {
        "id", "idea_id", "goal", "scope_excludes", "artifact_kind", "readback_text",
        "assent_utterance", "assented_at", "created_at",
    },
    "tasks": {
        "id", "commitment_id", "kind", "dbos_workflow_id", "status", "started_at",
        "finished_at", "error", "created_at",
    },
    "artifacts": {"id", "task_id", "kind", "url", "summary", "reviewed_at", "created_at"},
    "idea_edges": {"from_idea", "to_idea", "relation", "created_at"},
    "router_decisions": {
        "id", "turn_id", "backend", "difficulty", "ready", "intent", "confidence",
        "latency_ms", "is_active", "created_at",
    },
    "pending_reports": {
        "id", "session_id", "idea_id", "task_id", "summary", "created_at", "offered_at",
        "delivered_at",
    },
}

EXPECTED_INDEXES: dict[str, dict[str, list[str | None]]] = {
    "ideas": {"ix_ideas_fts": [None]},  # expression (GIN) index
    "turns": {"ix_turns_session_id_ts": ["session_id", "ts"], "ix_turns_idea_id": ["idea_id"]},
    "commitments": {"ix_commitments_idea_id": ["idea_id"]},
    "tasks": {"ix_tasks_status": ["status"], "ix_tasks_commitment_id": ["commitment_id"]},
    "artifacts": {"ix_artifacts_task_id": ["task_id"]},
    "idea_edges": {"ix_idea_edges_to_idea": ["to_idea"]},
    "router_decisions": {
        "ix_router_decisions_turn_id": ["turn_id"],
        "uq_router_decisions_active": ["turn_id"],
    },
    "pending_reports": {
        "ix_pending_reports_session_id": ["session_id"],
        "ix_pending_reports_idea_id": ["idea_id"],
        "ix_pending_reports_task_id": ["task_id"],
    },
}


def _insert(conn: Connection, table: str, **values: Any) -> uuid.UUID:
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    body = f"({cols}) VALUES ({params})" if values else "DEFAULT VALUES"
    returning = "from_idea" if table == "idea_edges" else "id"
    row = conn.execute(
        text(f"INSERT INTO {table} {body} RETURNING {returning}"), values
    ).one()
    return uuid.UUID(str(row[0]))


def _idea(conn: Connection) -> uuid.UUID:
    return _insert(conn, "ideas", title="routing thing")


def _commitment(conn: Connection, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = dict(
        idea_id=_idea(conn),
        goal="g",
        scope_excludes="no UI",
        artifact_kind="document",
        readback_text="I'll research X and leave a doc",
        assent_utterance="yes, do it",
        assented_at="2026-09-26T12:00:00Z",
    )
    values.update(overrides)
    return _insert(conn, "commitments", **values)


def _task(conn: Connection, **overrides: Any) -> uuid.UUID:
    values: dict[str, Any] = dict(commitment_id=_commitment(conn), kind="research")
    values.update(overrides)
    return _insert(conn, "tasks", **values)


def _turn(conn: Connection) -> uuid.UUID:
    session_id = _insert(conn, "sessions", wake_trigger="hey_jarvis")
    return _insert(conn, "turns", session_id=session_id, role="user", text="hi")


# --- AC1: upgrade head creates every table, column and index ---------------------------------


def test_upgrade_creates_all_tables_and_columns(db_conn: Connection) -> None:
    insp = inspect(db_conn)
    assert set(insp.get_table_names()) == set(EXPECTED_COLUMNS) | {"alembic_version"}
    for table, expected in EXPECTED_COLUMNS.items():
        assert {c["name"] for c in insp.get_columns(table)} == expected, table


def test_turns_has_no_laya_columns_and_metadata_defaults_to_empty_object(
    db_conn: Connection,
) -> None:
    turn_id = _turn(db_conn)
    meta = db_conn.execute(text("SELECT metadata FROM turns WHERE id = :id"), {"id": turn_id})
    assert meta.scalar_one() == {}


def test_indexes(db_conn: Connection) -> None:
    insp = inspect(db_conn)
    for table, expected in EXPECTED_INDEXES.items():
        found = {
            ix["name"]: ix["column_names"]
            for ix in insp.get_indexes(table)
            if ix["name"] in expected
        }
        assert found == expected, table
    fts = db_conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_ideas_fts'")
    ).scalar_one()
    assert "USING gin" in fts and "to_tsvector" in fts
    unique = {tuple(u["column_names"]) for u in insp.get_unique_constraints("tasks")}
    assert ("dbos_workflow_id",) in unique


# --- AC2: commitment-protocol invariants enforced by the database ----------------------------


def test_commitment_without_assent_utterance_is_rejected(db_conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="assent_utterance"):
        _commitment(db_conn, assent_utterance=None)


def test_commitment_without_assented_at_is_rejected(db_conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="assented_at"):
        _commitment(db_conn, assented_at=None)


def test_task_without_commitment_is_rejected(db_conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="commitment_id"):
        _insert(db_conn, "tasks", kind="research")


def test_task_with_unknown_commitment_is_rejected(db_conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="fk_|foreign key"):
        _insert(db_conn, "tasks", kind="research", commitment_id=uuid.uuid4())


# --- AC3: CHECK constraints reject invalid enum values ---------------------------------------

_BAD: dict[str, Callable[[Connection], object]] = {
    "ck_ideas_status": lambda c: _insert(c, "ideas", title="t", status="done"),
    "ck_tasks_kind": lambda c: _task(c, kind="email"),
    "ck_tasks_status": lambda c: _task(c, status="paused"),
    "ck_artifacts_kind": lambda c: _insert(c, "artifacts", task_id=_task(c), kind="gist", url="u"),
    "ck_idea_edges_relation": lambda c: _insert(
        c, "idea_edges", from_idea=_idea(c), to_idea=_idea(c), relation="likes"
    ),
    "ck_turns_route": lambda c: _insert(
        c, "turns", session_id=_insert(c, "sessions"), role="user", text="t", route="edge"
    ),
    "ck_turns_role": lambda c: _insert(
        c, "turns", session_id=_insert(c, "sessions"), role="bot", text="t"
    ),
    "ck_router_decisions_backend": lambda c: _insert(
        c, "router_decisions", turn_id=_turn(c), backend="jev"
    ),
}


@pytest.mark.parametrize("constraint", sorted(_BAD))
def test_check_constraint_rejects_invalid_value(db_conn: Connection, constraint: str) -> None:
    with pytest.raises(IntegrityError) as exc:
        _BAD[constraint](db_conn)
    assert exc.value.orig.diag.constraint_name == constraint  # type: ignore[union-attr]


def test_check_constraint_names_are_exact(db_conn: Connection) -> None:
    names = db_conn.execute(
        text(
            "SELECT conname FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE c.contype = 'c' AND n.nspname = 'public'"
        )
    ).scalars()
    assert set(names) == set(_BAD)


def test_valid_enum_values_are_accepted(db_conn: Connection) -> None:
    task_id = _task(db_conn, kind="code", status="running")
    _insert(db_conn, "artifacts", task_id=task_id, kind="pull_request", url="https://x/pr/1")
    _insert(db_conn, "idea_edges", from_idea=_idea(db_conn), to_idea=_idea(db_conn),
            relation="spun_off_from")
    session_id = _insert(db_conn, "sessions")
    _insert(db_conn, "turns", session_id=session_id, role="assistant", text="t", route="frontier")
    _insert(db_conn, "pending_reports", task_id=task_id, session_id=session_id, summary="done")


# --- AC4: several backends may log a decision for the same turn ------------------------------


def test_multiple_router_decisions_per_turn(db_conn: Connection) -> None:
    turn_id = _turn(db_conn)
    _insert(db_conn, "router_decisions", turn_id=turn_id, backend="frontier", is_active=True,
            ready="keep_talking", confidence=0.9)
    _insert(db_conn, "router_decisions", turn_id=turn_id, backend="laya", ready="keep_talking")
    _insert(db_conn, "router_decisions", turn_id=turn_id, backend="laya", ready="ready_to_execute")
    count = db_conn.execute(
        text("SELECT count(*) FROM router_decisions WHERE turn_id = :t"), {"t": turn_id}
    ).scalar_one()
    assert count == 3


def test_only_one_active_router_decision_per_turn(db_conn: Connection) -> None:
    turn_id = _turn(db_conn)
    _insert(db_conn, "router_decisions", turn_id=turn_id, backend="laya")
    _insert(db_conn, "router_decisions", turn_id=turn_id, backend="llm")
    _insert(db_conn, "router_decisions", turn_id=turn_id, backend="frontier", is_active=True)
    with pytest.raises(IntegrityError) as exc:
        _insert(db_conn, "router_decisions", turn_id=turn_id, backend="laya", is_active=True)
    assert exc.value.orig.diag.constraint_name == "uq_router_decisions_active"  # type: ignore[union-attr]


# --- AC5: downgrade base removes the schema cleanly ------------------------------------------


def test_downgrade_base_removes_all_app_tables(make_database: Callable[[], str]) -> None:
    url = make_database()
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(to_sync_url(url))
    try:
        with engine.connect() as conn:
            assert set(inspect(conn).get_table_names()) <= {"alembic_version"}
        command.upgrade(cfg, "head")  # and the round trip is repeatable
        with engine.connect() as conn:
            assert set(EXPECTED_COLUMNS) <= set(inspect(conn).get_table_names())
    finally:
        engine.dispose()
