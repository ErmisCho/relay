"""Laya (laya-mlx on Apple Silicon) as a binary difficulty router.

Runs in an ISOLATED env so the project's pyproject/uv.lock stay untouched:

    uv run --no-project --with laya-mlx==0.2.0 \\
        python benchmarks/router/laya_router.py --variant v1 --out /tmp/laya.json

Per SPEC section 2 this uses ONLY a binary ``choice`` question with
descriptive criteria. It never reads the ``score`` head and never thresholds
on ``confidence`` (uncalibrated; the checkpoint's temperatures are clamped).
The decision is the argmax ``choice`` label, nothing else.

Each invocation is one repetition in a fresh process, so the model load and
the first ``predict`` call (MLX graph build) are true cold measurements.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from typing import Any

from common import FAIL_SAFE, INSTRUCTIONS, LABELS, VARIANTS, load_labels, task_text

MODEL_ID = "aac6fef/laya-mlx"


def question(variant: str) -> dict[str, Any]:
    # `task` in backticks points the question at the state key, as in the
    # upstream presets ("How hard is `request` ..."); same text gemma gets.
    return {
        "difficulty": {
            "type": "choice",
            "instructions": INSTRUCTIONS,
            "criteria": dict(VARIANTS[variant]),
        }
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", default="v1", choices=sorted(VARIANTS))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", required=True, help="where to write the JSON result")
    args = ap.parse_args()

    import laya_mlx  # imported here so --help works without the isolated env

    items = load_labels()[: args.limit]
    q = question(args.variant)

    t0 = time.perf_counter()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        agent = laya_mlx.load(MODEL_ID)
    load_ms = (time.perf_counter() - t0) * 1000

    results = []
    for item in items:
        start = time.perf_counter()
        try:
            answer = agent.predict({"task": task_text(item)}, q)["answers"]["difficulty"]
            choice = answer.get("choice")
            status = "ok" if choice in LABELS else "invalid"
            raw = answer.get("probabilities")
        except Exception as exc:  # noqa: BLE001 - any failure must fail safe
            choice, status, raw = None, "error", repr(exc)
        latency_ms = (time.perf_counter() - start) * 1000
        results.append(
            {
                "id": item["id"],
                "choice": choice if status == "ok" else FAIL_SAFE,
                "status": status,
                "latency_ms": latency_ms,
                "raw": raw,  # label probabilities, recorded for audit only; never thresholded
            }
        )

    out = {
        "load_ms": load_ms,
        "model_id": MODEL_ID,
        "laya_mlx_version": laya_mlx.__version__,
        "warnings": [str(w.message) for w in caught],
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
