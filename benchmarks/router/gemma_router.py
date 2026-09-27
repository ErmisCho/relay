"""gemma4:e4b as a zero-shot binary difficulty router (via local Ollama).

Uses Ollama's OpenAI-compatible endpoint with stdlib ``urllib`` only, so it
runs in the project env without new dependencies. ``reasoning_effort="none"``
is required: without it gemma4 spends its output on hidden reasoning.

Any invalid output, timeout or transport error resolves to ``hard`` (fail
safe) and is reported with a non-"ok" status so it can be counted separately.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from common import FAIL_SAFE, INSTRUCTIONS, LABELS, VARIANTS, task_text

BASE_URL = "http://localhost:11434"
MODEL = "gemma4:e4b"
DEFAULT_TIMEOUT_S = 30.0


def system_prompt(variant: str) -> str:
    criteria = VARIANTS[variant]
    lines = [INSTRUCTIONS, "", "Labels:"]
    lines += [f'- "{label}": {desc}' for label, desc in criteria.items()]
    lines += [
        "",
        'Answer with JSON only, exactly: {"difficulty": "easy"} or {"difficulty": "hard"}.',
    ]
    return "\n".join(lines)


def _post(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def unload() -> None:
    """Evict gemma from memory so the next call is a true cold call."""
    _post("/api/generate", {"model": MODEL, "keep_alive": 0}, timeout=60)
    # Wait until Ollama reports the model gone (eviction is asynchronous).
    for _ in range(50):
        with urllib.request.urlopen(BASE_URL + "/api/ps", timeout=10) as resp:
            loaded = [m["name"] for m in json.loads(resp.read()).get("models", [])]
        if MODEL not in loaded:
            return
        time.sleep(0.2)


def parse(content: str) -> str | None:
    """Return 'easy'/'hard' from the model's JSON reply, or None if invalid."""
    try:
        obj = json.loads(content.strip())
    except (json.JSONDecodeError, AttributeError):
        return None
    value = obj.get("difficulty") if isinstance(obj, dict) else None
    value = value.strip().lower() if isinstance(value, str) else None
    return value if value in LABELS else None


def classify(
    item: dict[str, Any], variant: str, timeout: float = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """Classify one task. Returns {choice, status, latency_ms, raw}."""
    payload = {
        "model": MODEL,
        "temperature": 0,
        "seed": 0,
        "reasoning_effort": "none",
        "response_format": {"type": "json_object"},
        "max_tokens": 20,
        "messages": [
            {"role": "system", "content": system_prompt(variant)},
            {"role": "user", "content": "task:\n" + task_text(item)},
        ],
    }
    start = time.perf_counter()
    raw: str | None = None
    try:
        body = _post("/v1/chat/completions", payload, timeout)
        raw = body["choices"][0]["message"].get("content") or ""
        choice = parse(raw)
        status = "ok" if choice else "invalid"
    except TimeoutError:
        choice, status = None, "timeout"
    except urllib.error.URLError as exc:
        is_timeout = isinstance(exc.reason, TimeoutError)
        choice, status = None, "timeout" if is_timeout else "error"
        raw = raw or repr(exc)
    except Exception as exc:  # noqa: BLE001 - any failure must fail safe
        choice, status, raw = None, "error", repr(exc)
    latency_ms = (time.perf_counter() - start) * 1000
    return {"choice": choice or FAIL_SAFE, "status": status, "latency_ms": latency_ms, "raw": raw}


def run(items: list[dict[str, Any]], variant: str, cold: bool = True) -> dict[str, Any]:
    """One repetition over the set. The first call is cold when ``cold`` is set."""
    if cold:
        unload()
    results = []
    for item in items:
        res = classify(item, variant)
        results.append({"id": item["id"], **res})
    return {"load_ms": None, "results": results}


if __name__ == "__main__":
    import argparse

    from common import load_labels

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", default="v1", choices=sorted(VARIANTS))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    out = run(load_labels()[: args.limit], args.variant)
    print(json.dumps(out, indent=2))
