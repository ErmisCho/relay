"""Shared pieces of the TASK-45 router benchmark (stdlib only).

Imported by both routers, which run in DIFFERENT Python environments
(gemma in the project env, Laya in an isolated ``uv run --with laya-mlx``
env), so this module must not import anything outside the standard library.

Both routers receive the exact same question wording per variant, so a
difference in results is a difference between the models, not the prompts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LABELS_PATH = HERE / "labels.jsonl"

LABELS = ("easy", "hard")
# Any router failure (invalid output, timeout, exception) resolves to this
# label: sending an easy task to the frontier model only costs money, sending
# a hard task to the local model degrades the brief.
FAIL_SAFE = "hard"

INSTRUCTIONS = (
    "Decide how difficult the `task` is. It is a research-and-writing job whose "
    "output is a sourced Markdown research brief. Pick the label that fits."
)

# Variant v1: descriptive criteria only (one sentence of positive evidence each).
# Variant v2: the same criteria plus concrete, GENERIC anchors. The anchors are
# deliberately not taken from labels.jsonl, to avoid tuning to the test set.
VARIANTS: dict[str, dict[str, str]] = {
    "v1": {
        "easy": (
            "a well-known, stable topic that can be summarised from a few "
            "sources: a definition, a simple how-to, or a general overview"
        ),
        "hard": (
            "needs synthesis of many sources, a comparison across several "
            "dimensions, a fast-moving or technical topic, quantitative "
            "analysis, or reconciling conflicting sources"
        ),
    },
    "v2": {
        "easy": (
            "a well-known, stable topic summarised from a few sources, e.g. "
            "'explain what DNS is', 'how to boil an egg', 'overview of the "
            "Roman Empire'; one clear answer, little risk of being wrong"
        ),
        "hard": (
            "careful multi-source work where mistakes are costly, e.g. "
            "'compare five databases on cost, latency and licensing', "
            "'latest state of a fast-moving field', 'weigh conflicting "
            "studies', 'estimate a market size with numbers'"
        ),
    },
}


def task_text(item: dict[str, Any]) -> str:
    """Render a commitment (goal + scope_excludes) as the router input text."""
    excludes = item.get("scope_excludes") or []
    text = f"Goal: {item['goal']}"
    text += (
        f"\nExcluded from scope: {'; '.join(excludes)}"
        if excludes
        else "\nExcluded from scope: nothing"
    )
    return text


def load_labels(path: Path = LABELS_PATH) -> list[dict[str, Any]]:
    """Read the labelled set, one JSON object per line."""
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100 * len(ordered) + 0.4999)))
    return ordered[min(rank, len(ordered)) - 1]
