"""TASK-45: benchmark gemma4:e4b vs Laya as the easy/hard task router.

    uv run python benchmarks/router/run_benchmark.py            # both routers, v1+v2, 3 reps
    uv run python benchmarks/router/run_benchmark.py --routers gemma --reps 1

Routers run strictly one after the other (never concurrently), and gemma is
unloaded from Ollama before Laya runs, so neither measures the other's
GPU/CPU load. Laya runs in a subprocess in an isolated ``uv run --with
laya-mlx`` env; gemma runs in-process over Ollama's HTTP API.

Writes ``results.json`` (raw per-call data) and ``RESULTS.md`` (tables) next
to this file. A hand-written analysis block in RESULTS.md between
``<!-- analysis -->`` and ``<!-- /analysis -->`` is preserved across runs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gemma_router  # noqa: E402
from common import HERE, VARIANTS, load_labels, percentile  # noqa: E402

LAYA_SPEC = "laya-mlx==0.2.0"
RESULTS_JSON = HERE / "results.json"
RESULTS_MD = HERE / "RESULTS.md"


# --------------------------------------------------------------------------- runs


def environment_snapshot() -> dict[str, Any]:
    """Record what else was running, so latency numbers can be judged."""
    snap: dict[str, Any] = {"loadavg": os.getloadavg(), "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        out = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout
        snap["ollama_ps"] = out.strip().splitlines()[1:]
    except (OSError, subprocess.SubprocessError) as exc:
        snap["ollama_ps"] = repr(exc)
    return snap


def run_gemma(items: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    env_before = environment_snapshot()
    out = gemma_router.run(items, variant, cold=True)
    out["env_before"] = env_before
    return out


def run_laya(items_limit: int | None, variant: str) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        out_path = tmp.name
    cmd = [
        "uv", "run", "--no-project", "--with", LAYA_SPEC,
        "python", str(HERE / "laya_router.py"), "--variant", variant, "--out", out_path,
    ]  # fmt: skip
    if items_limit:
        cmd += ["--limit", str(items_limit)]
    env = {**os.environ, "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE", "0")}
    env_before = environment_snapshot()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"Laya run failed ({proc.returncode}):\n{proc.stderr[-3000:]}")
    data = json.loads(Path(out_path).read_text())
    os.unlink(out_path)
    data["env_before"] = env_before
    return data


# ------------------------------------------------------------------------ metrics


def classification_metrics(gold: dict[str, str], pred: dict[str, str]) -> dict[str, Any]:
    tp = sum(1 for i in gold if gold[i] == "hard" and pred[i] == "hard")
    fn = sum(1 for i in gold if gold[i] == "hard" and pred[i] == "easy")  # costly misroute
    fp = sum(1 for i in gold if gold[i] == "easy" and pred[i] == "hard")
    tn = sum(1 for i in gold if gold[i] == "easy" and pred[i] == "easy")
    n = len(gold)
    return {
        "n": n,
        "accuracy": (tp + tn) / n,
        "precision_hard": tp / (tp + fp) if tp + fp else None,
        "recall_hard": tp / (tp + fn) if tp + fn else None,
        "confusion": {"hard->hard": tp, "hard->easy": fn, "easy->hard": fp, "easy->easy": tn},
        "hard_to_easy_misroutes": fn,
    }


def summarise(items: list[dict[str, Any]], reps: list[dict[str, Any]]) -> dict[str, Any]:
    gold = {it["id"]: it["label"] for it in items}
    per_rep = []
    for rep in reps:
        pred = {r["id"]: r["choice"] for r in rep["results"]}
        per_rep.append(classification_metrics(gold, pred))

    # Headline: majority vote over repetitions (ties -> fail-safe "hard").
    votes: dict[str, Counter[str]] = {i: Counter() for i in gold}
    for rep in reps:
        for r in rep["results"]:
            votes[r["id"]][r["choice"]] += 1
    majority = {i: ("hard" if c["hard"] >= c["easy"] else "easy") for i, c in votes.items()}
    unstable = sorted(i for i, c in votes.items() if len(c) > 1)

    cold = [rep["results"][0]["latency_ms"] for rep in reps]
    warm = [r["latency_ms"] for rep in reps for r in rep["results"][1:]]
    statuses = Counter(r["status"] for rep in reps for r in rep["results"])
    misrouted_hard = sorted(i for i in gold if gold[i] == "hard" and majority[i] == "easy")
    misrouted_easy = sorted(i for i in gold if gold[i] == "easy" and majority[i] == "hard")
    border = [it["id"] for it in items if it.get("borderline")]
    return {
        "majority": classification_metrics(gold, majority),
        "borderline": classification_metrics({i: gold[i] for i in border}, majority)
        if border
        else None,
        "per_rep": per_rep,
        "unstable_items": unstable,
        "hard_misrouted_ids": misrouted_hard,
        "easy_misrouted_ids": misrouted_easy,
        "latency_ms": {
            "load_ms": [rep.get("load_ms") for rep in reps],
            "cold_first_call": cold,
            "warm_p50": percentile(warm, 50),
            "warm_p95": percentile(warm, 95),
            "warm_n": len(warm),
        },
        "status_counts": dict(statuses),
        "failures": sum(n for s, n in statuses.items() if s != "ok"),
    }


# ------------------------------------------------------------------------- report


def fmt(v: Any, pct: bool = False) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:.0f}" if isinstance(v, float) else str(v)


def render_markdown(results: dict[str, Any], analysis: str) -> str:
    lines = [
        "# TASK-45 router benchmark: gemma4:e4b vs Laya",
        "",
        f"Generated by `run_benchmark.py` at {results['meta']['finished']} on "
        f"{results['meta']['machine']}. Set: {results['meta']['n_items']} tasks "
        f"({results['meta']['label_counts']}), {results['meta']['reps']} repetitions per router/variant. "
        "Raw per-call data: `results.json`.",
        "",
        "Headline metrics use the majority vote over repetitions (ties resolve to `hard`). "
        "`hard->easy` is the costly misroute. Cold = first call after the model was evicted "
        "(gemma: Ollama `keep_alive=0`; Laya: fresh process, first `predict` after `load`). "
        "Warm = every other call.",
        "",
        "| Router / variant | Accuracy | Precision (hard) | Recall (hard) | hard->easy | easy->hard "
        "| Warm p50 ms | Warm p95 ms | Cold first call ms (per rep) | Load ms (per rep) | Invalid/timeout/error | Unstable items |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, s in results["summary"].items():
        m, lat = s["majority"], s["latency_ms"]
        cold = ", ".join(fmt(v) for v in lat["cold_first_call"])
        load = ", ".join(fmt(v) for v in lat["load_ms"])
        lines.append(
            f"| {key} | {fmt(m['accuracy'], True)} | {fmt(m['precision_hard'], True)} | "
            f"{fmt(m['recall_hard'], True)} | {m['confusion']['hard->easy']} | {m['confusion']['easy->hard']} | "
            f"{fmt(lat['warm_p50'])} | {fmt(lat['warm_p95'])} | {cold} | {load} | "
            f"{s['failures']} | {len(s['unstable_items'])} |"
        )
    lines += ["", "## Confusion matrices (majority vote)", ""]
    for key, s in results["summary"].items():
        c = s["majority"]["confusion"]
        lines += [
            f"**{key}**",
            "",
            "| gold \\ predicted | hard | easy |",
            "|---|---|---|",
            f"| hard | {c['hard->hard']} | {c['hard->easy']} |",
            f"| easy | {c['easy->hard']} | {c['easy->easy']} |",
            "",
            f"hard tasks routed easy: {', '.join(s['hard_misrouted_ids']) or 'none'}; "
            f"easy tasks routed hard: {', '.join(s['easy_misrouted_ids']) or 'none'}",
            "",
        ]
    lines += [
        "",
        "## Borderline subset only (majority vote)",
        "",
        "The clearly-easy and clearly-hard items separate trivially; the borderline items are where routers differ.",
        "",
        "| Router / variant | n | Accuracy | Recall (hard) | hard->easy | easy->hard |",
        "|---|---|---|---|---|---|",
    ]
    for key, s in results["summary"].items():
        b = s.get("borderline")
        if b:
            lines.append(
                f"| {key} | {b['n']} | {fmt(b['accuracy'], True)} | {fmt(b['recall_hard'], True)} | "
                f"{b['confusion']['hard->easy']} | {b['confusion']['easy->hard']} |"
            )
    lines += [
        "",
        "## Stability across repetitions (per-rep, full set)",
        "",
        "| Router / variant | Accuracy per rep | Recall (hard) per rep | Status counts |",
        "|---|---|---|---|",
    ]
    for key, s in results["summary"].items():
        acc = ", ".join(fmt(r["accuracy"], True) for r in s["per_rep"])
        rec = ", ".join(fmt(r["recall_hard"], True) for r in s["per_rep"])
        lines.append(f"| {key} | {acc} | {rec} | {s['status_counts']} |")
    lines += [
        "",
        "## Analysis and recommendation",
        "",
        "<!-- analysis -->",
        analysis.strip(),
        "<!-- /analysis -->",
        "",
    ]
    return "\n".join(lines)


def preserved_analysis() -> str:
    if RESULTS_MD.exists():
        text = RESULTS_MD.read_text(encoding="utf-8")
        if "<!-- analysis -->" in text and "<!-- /analysis -->" in text:
            return text.split("<!-- analysis -->", 1)[1].split("<!-- /analysis -->", 1)[0]
    return "_Not written yet: fill in after reviewing the numbers above._"


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--routers", default="gemma,laya")
    ap.add_argument("--variants", default=",".join(sorted(VARIANTS)))
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None, help="only the first N items (smoke test)")
    ap.add_argument(
        "--no-write", action="store_true", help="print only, do not overwrite results files"
    )
    args = ap.parse_args()

    items = load_labels()[: args.limit]
    routers = [r.strip() for r in args.routers.split(",") if r.strip()]
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    raw: dict[str, list[dict[str, Any]]] = {}
    summary: dict[str, Any] = {}

    for router in routers:
        if router == "laya":
            gemma_router.unload()  # keep the GPU free of gemma while Laya is timed
        for variant in variants:
            key = f"{router}/{variant}"
            reps = []
            for rep in range(args.reps):
                print(f"[{key}] rep {rep + 1}/{args.reps} ...", flush=True)
                if router == "gemma":
                    reps.append(run_gemma(items, variant))
                elif router == "laya":
                    reps.append(run_laya(args.limit, variant))
                else:
                    raise SystemExit(f"unknown router {router!r}")
            raw[key] = reps
            summary[key] = summarise(items, reps)
            m = summary[key]["majority"]
            print(
                f"[{key}] acc={m['accuracy']:.3f} recall_hard={m['recall_hard']} "
                f"hard->easy={m['hard_to_easy_misroutes']} warm_p50={summary[key]['latency_ms']['warm_p50']:.0f}ms",
                flush=True,
            )

    results = {
        "meta": {
            "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
            "machine": f"{platform.platform()} ({platform.machine()})",
            "n_items": len(items),
            "label_counts": dict(Counter(it["label"] for it in items)),
            "reps": args.reps,
            "gemma_model": gemma_router.MODEL,
            "laya_spec": LAYA_SPEC,
        },
        "summary": summary,
        "raw": raw,
    }
    md = render_markdown(results, preserved_analysis())
    if args.no_write:
        print(md)
    else:
        RESULTS_JSON.write_text(json.dumps(results, indent=1), encoding="utf-8")
        RESULTS_MD.write_text(md, encoding="utf-8")
        print(f"wrote {RESULTS_JSON} and {RESULTS_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
