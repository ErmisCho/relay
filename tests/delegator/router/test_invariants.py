"""SPEC section 2 constraints that must hold repo-wide, checked on the source itself."""

from __future__ import annotations

import ast
from pathlib import Path

from relay.delegator.router.questions import ALL_QUESTIONS, ROUTING_QUESTIONS

SRC = Path(__file__).resolve().parents[3] / "src" / "relay"
ROUTER_PKG = SRC / "delegator" / "router"


def test_only_choice_questions_exist() -> None:
    """Bug caught: a ``score`` question (collapses to a constant zero-shot) sneaking into the
    shared definitions or any router backend."""
    assert set(ROUTING_QUESTIONS) == {"difficulty", "ready"}
    assert all(q.type == "choice" and 2 <= len(q.criteria) <= 10 for q in ALL_QUESTIONS.values())
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in ROUTER_PKG.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Constant) and node.value == "score"
    ]
    assert offenders == []


def _mentions_confidence(expr: ast.AST) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.Attribute) and node.attr == "confidence":
            return True
        if isinstance(node, ast.Name) and "confidence" in node.id:
            return True
        if isinstance(node, ast.Constant) and node.value == "confidence":
            return True
    return False


def test_no_code_branches_on_confidence() -> None:
    """Bug caught: thresholding the uncalibrated ``confidence`` (observed 0.01-0.58 even when
    correct), e.g. ``if decision.confidence > 0.5``."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            tests: list[ast.AST] = []
            if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
                tests.append(node.test)
            elif isinstance(node, ast.comprehension):
                tests.extend(node.ifs)
            elif isinstance(node, ast.Match):
                tests.append(node.subject)
            elif isinstance(node, ast.Compare):
                tests.append(node)
            if any(_mentions_confidence(t) for t in tests):
                offenders.append(f"{path.relative_to(SRC)}:{getattr(node, 'lineno', '?')}")
    assert offenders == []
