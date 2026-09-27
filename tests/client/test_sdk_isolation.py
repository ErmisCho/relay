"""Only `relay.client.elevenlabs_session` may import the ElevenLabs SDK (swappable voice seam)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "relay"
ALLOWED = SRC / "client" / "elevenlabs_session.py"


def _imports_elevenlabs(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(name.split(".")[0] == "elevenlabs" for name in names):
            return True
    return False


def test_only_elevenlabs_session_imports_the_sdk() -> None:
    offenders = [
        str(p.relative_to(SRC)) for p in sorted(SRC.rglob("*.py")) if p != ALLOWED
        and _imports_elevenlabs(p)
    ]
    assert offenders == []
    assert _imports_elevenlabs(ALLOWED)  # guards against the scan silently matching nothing
