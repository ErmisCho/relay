"""Read-only per-backend router report from ``router_decisions`` (TASK-36 AC5, TASK-41).

    uv run python scripts/router_report.py [--since 2026-09-01] [--json]

For every shadow/active backend (``laya``, ``llm``, ...) on user turns:

* agreement with the frontier reference on ``ready`` and ``difficulty``, with the confusion
  matrix (rows = frontier label, columns = backend label). This is *agreement*, not
  accuracy: there are no ground-truth labels, so the frontier decision is a reference only.
  Turns without a frontier row for a question are left out of that question's matrix.
* latency p50/p95 over every call (answered or not);
* status counts and the error/timeout/invalid rate.

Task-difficulty rows (``task_id`` set, TASK-46) are a different decision and are excluded.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, text

from relay.config import get_settings, to_sync_url

REFERENCE = "frontier"
QUESTIONS = ("ready", "difficulty")


def percentile(values: Sequence[int], pct: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def build_report(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate turn-level ``router_decisions`` rows (dicts with the table's columns)."""
    reference: dict[Any, dict[str, str]] = defaultdict(dict)
    per_backend: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("turn_id") is None or row.get("task_id") is not None:
            continue
        if row["backend"] == REFERENCE:
            for q in QUESTIONS:
                if row.get(q) is not None:
                    reference[row["turn_id"]][q] = row[q]
        else:
            per_backend[row["backend"]].append(row)

    report: dict[str, Any] = {}
    for backend, items in sorted(per_backend.items()):
        statuses = Counter(r.get("router_status") or "ok" for r in items)
        latencies = [r["latency_ms"] for r in items if r.get("latency_ms") is not None]
        agreement: dict[str, Any] = {}
        for q in QUESTIONS:
            matrix: dict[str, Counter[str]] = defaultdict(Counter)
            for r in items:
                ref = reference.get(r["turn_id"], {}).get(q)
                if ref is not None and r.get(q) is not None:
                    matrix[ref][r[q]] += 1
            pairs = sum(sum(c.values()) for c in matrix.values())
            agree = sum(c[label] for label, c in matrix.items())
            agreement[q] = {
                "pairs": pairs,
                "agreement": agree / pairs if pairs else None,
                "confusion": {ref: dict(c) for ref, c in sorted(matrix.items())},
            }
        failures = len(items) - statuses.get("ok", 0)
        report[backend] = {
            "calls": len(items),
            "statuses": dict(statuses),
            "failure_rate": failures / len(items) if items else None,
            "timeout_rate": statuses.get("timeout", 0) / len(items) if items else None,
            "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
            "agreement": agreement,
        }
    return report


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render(report: dict[str, Any]) -> str:
    if not report:
        return "No turn-level router decisions from any backend other than the frontier."
    lines: list[str] = []
    for backend, r in report.items():
        lat = r["latency_ms"]
        lines += [
            f"## {backend}",
            f"calls {r['calls']}  statuses {r['statuses']}  failure rate {_pct(r['failure_rate'])}"
            f"  timeout rate {_pct(r['timeout_rate'])}",
            f"latency p50 {lat['p50']} ms  p95 {lat['p95']} ms",
        ]
        for q, a in r["agreement"].items():
            lines.append(
                f"{q}: agreement with frontier {_pct(a['agreement'])} over {a['pairs']} turns"
                + ("" if a["pairs"] else " (no frontier reference for this question yet)")
            )
            for ref, row in a["confusion"].items():
                lines.append(f"    frontier={ref}: {row}")
        lines.append("")
    return "\n".join(lines)


def fetch_rows(database_url: str, since: datetime | None) -> list[dict[str, Any]]:
    engine = create_engine(to_sync_url(database_url))
    query = (
        "SELECT turn_id, task_id, backend, difficulty, ready, intent, latency_ms, "
        "router_status, is_active FROM router_decisions WHERE turn_id IS NOT NULL "
        "AND task_id IS NULL"
    )
    params: dict[str, Any] = {}
    if since is not None:
        query += " AND created_at >= :since"
        params["since"] = since
    try:
        with engine.connect() as conn, conn.begin() as trans:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            rows = [dict(r._mapping) for r in conn.execute(text(query), params)]
            trans.rollback()
    finally:
        engine.dispose()
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-backend router agreement and latency.")
    parser.add_argument("--since", type=datetime.fromisoformat, help="decisions at/after")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--database-url", help="defaults to DATABASE_URL from the settings")
    args = parser.parse_args(argv)
    rows = fetch_rows(args.database_url or get_settings().database_url, args.since)
    report = build_report(rows)
    print(json.dumps(report, indent=2, default=str) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
