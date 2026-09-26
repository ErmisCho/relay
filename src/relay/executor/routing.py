"""Task-difficulty router: classify a dispatched commitment as ``easy`` or ``hard`` (TASK-46).

Decision-5 (TASK-45 benchmark, ``benchmarks/router/``): gemma4:e4b with prompt variant v2, a
zero-shot binary choice answered as JSON at temperature 0 with ``reasoning_effort="none"``.
The prompt wording below is copied from ``benchmarks/router/common.py`` (v2) and
``gemma_router.system_prompt``; ``benchmarks/`` is not importable from the package.

Any failure (timeout, transport error, invalid output) resolves to ``hard``: sending an easy
task to the frontier model only costs money, sending a hard task to the local model degrades
the brief. Routing only picks the model. It never touches the commitment invariant: a task
exists only after spoken assent was recorded by the Delegator.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from relay.config import Settings, parse_model_ref

Difficulty = Literal["easy", "hard"]
RouterStatus = Literal["ok", "invalid", "timeout", "error"]
LABELS: tuple[Difficulty, ...] = ("easy", "hard")
ROUTER_STATUSES: tuple[RouterStatus, ...] = ("ok", "invalid", "timeout", "error")
FAIL_SAFE: Difficulty = "hard"
BACKEND = "llm"  # router_decisions.backend for this router
MAX_TOKENS = 20

INSTRUCTIONS = (
    "Decide how difficult the `task` is. It is a research-and-writing job whose "
    "output is a sourced Markdown research brief. Pick the label that fits."
)
# Variant v2 criteria (descriptive criteria plus generic anchors not taken from the test set).
CRITERIA: dict[Difficulty, str] = {
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
}


def system_prompt() -> str:
    """The v2 system prompt, byte-for-byte what the benchmark sent."""
    lines = [INSTRUCTIONS, "", "Labels:"]
    lines += [f'- "{label}": {desc}' for label, desc in CRITERIA.items()]
    lines += [
        "",
        'Answer with JSON only, exactly: {"difficulty": "easy"} or {"difficulty": "hard"}.',
    ]
    return "\n".join(lines)


def task_text(goal: str, scope_excludes: str) -> str:
    """Render a commitment as the router input (the benchmark's ``task_text`` format).

    The benchmark joined a list of exclusions with ``"; "``; a commitment stores them as one
    text, which is used verbatim.
    """
    excludes = scope_excludes.strip()
    return f"Goal: {goal}\nExcluded from scope: {excludes or 'nothing'}"


def user_prompt(goal: str, scope_excludes: str) -> str:
    return "task:\n" + task_text(goal, scope_excludes)


def parse(content: str | None) -> Difficulty | None:
    """``easy``/``hard`` from the model's JSON reply, or None if the reply is invalid."""
    try:
        obj = json.loads((content or "").strip())
    except json.JSONDecodeError:
        return None
    value = obj.get("difficulty") if isinstance(obj, dict) else None
    value = value.strip().lower() if isinstance(value, str) else None
    if value == "easy":
        return "easy"
    if value == "hard":
        return "hard"
    return None


@dataclass(frozen=True)
class RouteDecision:
    """One router verdict. ``valid`` is False when ``difficulty`` is the fail-safe default."""

    difficulty: Difficulty
    latency_ms: int
    valid: bool
    status: RouterStatus
    raw: str | None
    router_model: str
    backend: str = BACKEND


def _request(goal: str, scope_excludes: str, settings: Settings) -> tuple[str, dict[str, Any], Any]:
    """Build (model name, request kwargs, sync OpenAI client) for the configured router."""
    from openai import OpenAI

    provider, model = parse_model_ref(settings.router_model)
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": user_prompt(goal, scope_excludes)},
    ]
    # reasoning_effort="none": without it gemma4 spends its output budget on hidden reasoning.
    kwargs: dict[str, Any] = {
        "messages": messages,
        "reasoning_effort": "none",
        "response_format": {"type": "json_object"},
    }
    if provider == "ollama":
        client = OpenAI(
            base_url=settings.ollama_base_url,
            api_key="ollama",
            timeout=settings.router_timeout_s,
            max_retries=0,
        )
        kwargs.update(temperature=0, seed=0, max_tokens=MAX_TOKENS)
    elif provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(f"router model {settings.router_model!r} needs OPENAI_API_KEY")
        client = OpenAI(
            api_key=settings.openai_api_key, timeout=settings.router_timeout_s, max_retries=0
        )
        # OpenAI reasoning models want max_completion_tokens; temperature only with effort none.
        kwargs.update(temperature=0, max_completion_tokens=MAX_TOKENS)
    else:
        raise ValueError(f"router provider {provider!r} is not supported (use ollama or openai)")
    return model, kwargs, client


def classify_difficulty(goal: str, scope_excludes: str, settings: Settings) -> RouteDecision:
    """Classify one task; never raises. Invalid output, timeout or error -> ``hard``."""
    import openai

    start = time.perf_counter()
    raw: str | None = None
    choice: Difficulty | None = None
    status: RouterStatus
    try:
        model, kwargs, client = _request(goal, scope_excludes, settings)
        with client:
            completion = client.chat.completions.create(model=model, **kwargs)
        raw = completion.choices[0].message.content or ""
        choice = parse(raw)
        status = "ok" if choice else "invalid"
    except openai.APITimeoutError as exc:
        status, raw = "timeout", repr(exc)
    except Exception as exc:  # noqa: BLE001 - any router failure must fail safe
        status, raw = "error", repr(exc)
    latency_ms = round((time.perf_counter() - start) * 1000)
    return RouteDecision(
        difficulty=choice or FAIL_SAFE,
        latency_ms=latency_ms,
        valid=choice is not None,
        status=status,
        raw=raw,
        router_model=settings.router_model,
    )


Classifier = Callable[[str, str, Settings], RouteDecision]
_classifier: Classifier | None = None
_lock = threading.Lock()


def configure_classifier(classifier: Classifier | None) -> None:
    """Replace the classifier process-wide (tests install a stub before DBOS launches)."""
    global _classifier
    with _lock:
        _classifier = classifier


def route_task(goal: str, scope_excludes: str, settings: Settings) -> RouteDecision:
    """Classify with the configured classifier (``classify_difficulty`` by default).

    A classifier that raises still yields a fail-safe ``hard`` decision.
    """
    with _lock:
        classifier = _classifier or classify_difficulty
    start = time.perf_counter()
    try:
        return classifier(goal, scope_excludes, settings)
    except Exception as exc:  # noqa: BLE001 - any router failure must fail safe
        return RouteDecision(
            difficulty=FAIL_SAFE,
            latency_ms=round((time.perf_counter() - start) * 1000),
            valid=False,
            status="error",
            raw=repr(exc),
            router_model=settings.router_model,
        )
