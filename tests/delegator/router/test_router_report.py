"""The router report compares each backend with the frontier on the SAME turn only."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "router_report.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("router_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(turn: str | None, backend: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {"turn_id": turn, "task_id": None, "backend": backend}
    base.update(kw)
    return base


def test_agreement_is_per_turn_and_ignores_task_rows() -> None:
    """Bug caught: agreement computed over label totals instead of matched turns, or the
    task-difficulty router's ``llm`` rows (task_id set, easy/hard) polluting the turn report."""
    rows = [
        _row("t1", "frontier", ready="ready_to_execute"),
        _row("t2", "frontier", ready="keep_talking"),
        # swapped labels: totals match the frontier, but no single turn agrees
        _row("t1", "llm", ready="keep_talking", latency_ms=500, router_status="ok"),
        _row("t2", "llm", ready="ready_to_execute", latency_ms=700, router_status="ok"),
        _row("t3", "llm", latency_ms=2000, router_status="timeout"),
        {"turn_id": None, "task_id": "k1", "backend": "llm", "difficulty": "easy", "latency_ms": 9},
    ]
    report = _load().build_report(rows)
    llm = report["llm"]
    assert llm["calls"] == 3
    assert llm["agreement"]["ready"]["pairs"] == 2
    assert llm["agreement"]["ready"]["agreement"] == 0.0
    assert llm["agreement"]["ready"]["confusion"] == {
        "keep_talking": {"ready_to_execute": 1},
        "ready_to_execute": {"keep_talking": 1},
    }
    assert llm["agreement"]["difficulty"]["pairs"] == 0
    assert llm["timeout_rate"] == 1 / 3
    assert llm["latency_ms"] == {"p50": 700.0, "p95": 2000.0}
