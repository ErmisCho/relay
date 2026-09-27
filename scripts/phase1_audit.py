"""Read-only audit of one live session for the Phase 1 exit check (TASK-31).

    uv run python scripts/phase1_audit.py --latest
    uv run python scripts/phase1_audit.py --session <uuid> --json audit.json
    uv run python scripts/phase1_audit.py --since 2026-09-26T10:00 --until 2026-09-26T11:00

It reads the database the app uses (`DATABASE_URL`, see `relay.config`) inside a READ ONLY
transaction and never writes to it. `--json` writes the report to a local file.

Exit codes: 0 = TASK-31 AC#2 (no unintended dispatch) and AC#3 (session closed) hold for every
audited session, 1 = at least one violation, 2 = usage or database error.

How rows are tied to a session: `turns` and turn-level `router_decisions` carry the session
directly, and so do `commitments` (`session_id`, since migration 0003) and their `tasks`.
Commitments written before 0003 have no `session_id`; only those fall back to the time window
(created between `started_at` and `ended_at` plus a short grace window, since a dispatch can
land a few seconds after the call closes), and sessions overlapping in time share them. Each
commitment in the report says which way it was linked.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from relay.config import get_settings, to_sync_url

# Commitments/tasks recorded this long after `ended_at` still count for the session.
GRACE = "2 minutes"
NOT_RECORDED = "not recorded"
TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def percentiles(values: Sequence[int]) -> dict[str, float | None]:
    """p50/p90 with linear interpolation (same as Postgres `percentile_cont`)."""
    if not values:
        return {"p50": None, "p90": None}
    if len(values) == 1:
        return {"p50": float(values[0]), "p90": float(values[0])}
    q = statistics.quantiles(values, n=10, method="inclusive")
    return {"p50": float(statistics.median(values)), "p90": q[8]}


def _tokens(metas: list[dict[str, Any]]) -> dict[str, int] | str:
    """Sum token counts if turns.metadata carries them (top level or under `usage`)."""
    totals: dict[str, int] = defaultdict(int)
    for meta in metas:
        usage = meta.get("usage")
        for src in (meta, usage if isinstance(usage, dict) else {}):
            for key in TOKEN_KEYS:
                if isinstance(src.get(key), int):
                    totals[key] += src[key]
    return dict(totals) if totals else NOT_RECORDED


def audit_session(conn: Connection, session_id: uuid.UUID) -> dict[str, Any]:
    """Build the audit report for one session. Raises LookupError if it does not exist."""
    sess = (
        conn.execute(
            text(
                "SELECT id, started_at, ended_at, end_reason, wake_trigger, "
                f"coalesce(ended_at, now()) + interval '{GRACE}' AS window_end "
                "FROM sessions WHERE id = :sid"
            ),
            {"sid": session_id},
        )
        .mappings()
        .one_or_none()
    )
    if sess is None:
        raise LookupError(f"session {session_id} not found")
    window = {"sid": session_id, "start": sess["started_at"], "end": sess["window_end"]}
    violations: list[str] = []
    warnings: list[str] = []

    # --- duration / close (AC#3) -----------------------------------------------------------
    ended_at = sess["ended_at"]
    duration_s = (ended_at - sess["started_at"]).total_seconds() if ended_at else None
    if ended_at is None:
        violations.append("session never ended (ended_at is NULL): not auto-closed")

    # --- turns and latency -----------------------------------------------------------------
    turns = (
        conn.execute(
            text(
                "SELECT id, role, model_used, latency_ms, metadata FROM turns "
                "WHERE session_id = :sid ORDER BY ts"
            ),
            {"sid": session_id},
        )
        .mappings()
        .all()
    )
    by_model: dict[str, list[int]] = defaultdict(list)
    for t in turns:
        if t["role"] == "assistant" and t["latency_ms"] is not None:
            by_model[t["model_used"] or "unknown"].append(t["latency_ms"])
    latency = {
        model: {"count": len(vals), **percentiles(vals)} for model, vals in sorted(by_model.items())
    }
    all_latencies = [v for vals in by_model.values() for v in vals]

    # --- commitments and tasks (AC#2) ------------------------------------------------------
    commitments = (
        conn.execute(
            text(
                "SELECT id, goal, readback_text, assent_utterance, assented_at, assent_turn_id, "
                "session_id IS NOT NULL AS linked FROM commitments "
                "WHERE session_id = :sid OR (session_id IS NULL "
                "AND created_at >= :start AND created_at <= :end) ORDER BY created_at"
            ),
            window,
        )
        .mappings()
        .all()
    )
    # A task belongs to its commitment's session. For an unlinked (pre-0003) commitment it
    # counts if it OR the commitment falls in the window, so a stray task created during the
    # call against an old commitment is audited too.
    tasks = (
        conn.execute(
            text(
                "SELECT t.id, t.commitment_id, t.kind, t.status, t.error, t.served_model, "
                "t.created_at, c.assent_utterance, c.readback_text, c.assented_at "
                "FROM tasks t JOIN commitments c ON c.id = t.commitment_id "
                "WHERE c.session_id = :sid OR (c.session_id IS NULL AND ("
                "(t.created_at >= :start AND t.created_at <= :end) "
                "OR (c.created_at >= :start AND c.created_at <= :end))) ORDER BY t.created_at"
            ),
            window,
        )
        .mappings()
        .all()
    )
    task_ids = [t["id"] for t in tasks]
    artifacts: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    task_routes: dict[uuid.UUID, dict[str, Any]] = {}
    if task_ids:
        for a in conn.execute(
            text("SELECT task_id, kind, url FROM artifacts WHERE task_id = ANY(:ids)"),
            {"ids": task_ids},
        ).mappings():
            artifacts[a["task_id"]].append({"kind": a["kind"], "url": a["url"]})
        for r in conn.execute(
            text(
                "SELECT task_id, difficulty, model_chosen, router_status "
                "FROM router_decisions WHERE is_active AND task_id = ANY(:ids)"
            ),
            {"ids": task_ids},
        ).mappings():
            task_routes[r["task_id"]] = dict(r)

    tasks_per_commitment: dict[uuid.UUID, int] = defaultdict(int)
    task_rows = []
    for t in tasks:
        tasks_per_commitment[t["commitment_id"]] += 1
        reasons = []
        if not (t["assent_utterance"] or "").strip():
            reasons.append("commitment has no assent utterance")
        if not (t["readback_text"] or "").strip():
            reasons.append("commitment has no read-back")
        if t["assented_at"] is None or t["assented_at"] > t["created_at"]:
            reasons.append("task created before the assent was recorded")
        for reason in reasons:
            violations.append(f"unintended dispatch: task {t['id']}: {reason}")
        route = task_routes.get(t["id"], {})
        task_rows.append(
            {
                "id": str(t["id"]),
                "commitment_id": str(t["commitment_id"]),
                "kind": t["kind"],
                "status": t["status"],
                "error": t["error"],
                "difficulty": route.get("difficulty"),
                "model_chosen": route.get("model_chosen"),
                "router_status": route.get("router_status"),
                "served_model": t["served_model"],
                "artifacts": artifacts.get(t["id"], []),
                "unintended_dispatch": bool(reasons),
            }
        )
        if t["status"] != "succeeded":
            warnings.append(f"task {t['id']} is {t['status']}, not succeeded")

    commitment_rows = []
    for c in commitments:
        n = tasks_per_commitment.get(c["id"], 0)
        if n > 1:
            violations.append(f"commitment {c['id']} led to {n} tasks (expected exactly one)")
        elif n == 0:
            warnings.append(f"commitment {c['id']} has no task yet")
        commitment_rows.append(
            {
                "id": str(c["id"]),
                "goal": c["goal"],
                "readback_text": c["readback_text"],
                "assent_utterance": c["assent_utterance"],
                "assented_at": _iso(c["assented_at"]),
                "assent_turn_id": str(c["assent_turn_id"]) if c["assent_turn_id"] else None,
                "linked_by": "session_id" if c["linked"] else "time_window",
                "task_count": n,
                "exactly_one_task": n == 1,
            }
        )

    # --- router decisions ------------------------------------------------------------------
    turn_routes = (
        conn.execute(
            text(
                "SELECT r.backend, r.ready, r.difficulty, r.is_active FROM router_decisions r "
                "JOIN turns t ON t.id = r.turn_id WHERE t.session_id = :sid"
            ),
            {"sid": session_id},
        )
        .mappings()
        .all()
    )
    difficulty_counts: dict[str, int] = defaultdict(int)
    for row in task_rows:
        difficulty_counts[str(row["difficulty"] or "none")] += 1

    # --- session id consistency (TASK-23 AC#3) ---------------------------------------------
    derived: int = conn.execute(
        text(
            "SELECT count(*) FROM sessions WHERE wake_trigger IS NULL AND id <> :sid "
            "AND started_at >= :start AND started_at <= :end"
        ),
        window,
    ).scalar_one()
    if sess["wake_trigger"] is None:
        warnings.append("wake_trigger is NULL: the Delegator created this row, not the client")
    if derived:
        warnings.append(
            f"{derived} session row(s) without wake_trigger started during this call: "
            "turns may be stored under a derived session id"
        )

    return {
        "session": {
            "id": str(session_id),
            "started_at": _iso(sess["started_at"]),
            "ended_at": _iso(ended_at),
            "duration_s": duration_s,
            "auto_closed": ended_at is not None,
            "end_reason": sess["end_reason"] or NOT_RECORDED,
            "wake_trigger": sess["wake_trigger"],
        },
        "cost": {
            "agent_minutes": round(duration_s / 60, 2) if duration_s is not None else None,
            "tokens": _tokens([t["metadata"] or {} for t in turns]),
        },
        "turns": {
            "count": len(turns),
            "by_role": {
                role: sum(1 for t in turns if t["role"] == role)
                for role in sorted({t["role"] for t in turns})
            },
            "ttft_ms_all": {"count": len(all_latencies), **percentiles(all_latencies)},
            "ttft_ms_by_model": latency,
        },
        "commitments": commitment_rows,
        "tasks": task_rows,
        "router_decisions": {
            "task_difficulty": dict(difficulty_counts),
            "turn_level": [dict(r) for r in turn_routes],
        },
        "session_id_consistency": {
            "client_created": sess["wake_trigger"] is not None,
            "turns_on_session": len(turns),
            "derived_sessions_during_call": derived,
        },
        "violations": violations,
        "warnings": warnings,
    }


def _select_sessions(conn: Connection, args: argparse.Namespace) -> list[uuid.UUID]:
    if args.session:
        return [args.session]
    if args.latest:
        sql = "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1"
        return [r[0] for r in conn.execute(text(sql))]
    sql = "SELECT id FROM sessions WHERE started_at >= :since"
    params: dict[str, Any] = {"since": args.since}
    if args.until:
        sql += " AND started_at < :until"
        params["until"] = args.until
    return [r[0] for r in conn.execute(text(sql + " ORDER BY started_at"), params)]


def _print(report: dict[str, Any]) -> None:
    s, c, t = report["session"], report["cost"], report["turns"]
    print(f"session {s['id']}  wake_trigger={s['wake_trigger']}")
    print(f"  started {s['started_at']}  ended {s['ended_at'] or '(never: NOT auto-closed)'}")
    dur = f"{s['duration_s']:.0f} s" if s["duration_s"] is not None else "n/a"
    print(f"  duration {dur}  end reason: {s['end_reason']}")
    print(f"  cost: agent minutes {c['agent_minutes']}  tokens {c['tokens']}")
    print(f"  turns {t['count']} {t['by_role']}  TTFT all {t['ttft_ms_all']}")
    for model, stats in t["ttft_ms_by_model"].items():
        print(f"    TTFT {model}: {stats}")
    print(f"  commitments {len(report['commitments'])}")
    for cm in report["commitments"]:
        print(f"    - {cm['goal']!r}  tasks={cm['task_count']}  linked by {cm['linked_by']}")
        print(f"      read-back: {cm['readback_text']!r}")
        print(f"      assent:    {cm['assent_utterance']!r} at {cm['assented_at']}")
    print(f"  tasks {len(report['tasks'])}")
    for tk in report["tasks"]:
        print(
            f"    - {tk['id']} {tk['kind']} {tk['status']} difficulty={tk['difficulty']} "
            f"chosen={tk['model_chosen']} served={tk['served_model']}"
        )
        for a in tk["artifacts"]:
            print(f"      artifact {a['kind']}: {a['url']}")
    rd = report["router_decisions"]
    print(f"  router decisions: tasks {rd['task_difficulty']}, turn-level {len(rd['turn_level'])}")
    print(f"  session id consistency {report['session_id_consistency']}")
    for w in report["warnings"]:
        print(f"  WARN {w}")
    for v in report["violations"]:
        print(f"  VIOLATION {v}")
    print(f"  => {'FAIL' if report['violations'] else 'PASS'} (TASK-31 AC#2/#3)")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Phase 1 (TASK-31) session audit.")
    pick = parser.add_mutually_exclusive_group(required=True)
    pick.add_argument("--session", type=uuid.UUID, help="sessions.id to audit")
    pick.add_argument("--latest", action="store_true", help="audit the most recent session")
    pick.add_argument("--since", type=datetime.fromisoformat, help="sessions started at/after")
    parser.add_argument("--until", type=datetime.fromisoformat, help="with --since: before")
    parser.add_argument("--json", type=Path, help="also write the report to this file")
    parser.add_argument("--database-url", help="override DATABASE_URL")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2

    engine = create_engine(to_sync_url(args.database_url or get_settings().database_url))
    try:
        with engine.connect() as conn, conn.begin() as trans:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            ids = _select_sessions(conn, args)
            reports = [audit_session(conn, sid) for sid in ids]
            trans.rollback()
    except (SQLAlchemyError, LookupError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    if not reports:
        print("no matching session", file=sys.stderr)
        return 2
    for report in reports:
        _print(report)
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, default=str) + "\n")
    return 1 if any(r["violations"] for r in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
