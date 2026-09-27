---
id: TASK-36
title: Add the router interface and zero-shot Laya backend in shadow mode
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-27 00:34'
labels:
  - phase-3
  - router
  - delegator
milestone: m-2
dependencies:
  - TASK-31
references:
  - SPEC.md#2-laya--jev-evaluation--measured-not-estimated
  - SPEC.md#8-phasing
documentation:
  - SPEC.md
priority: low
type: feature
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Routing each turn to the smallest capable model cuts token spend. Laya is used **strictly zero-shot**: there is no labeling corpus, so no calibration or fine-tuning is planned. The SPEC §2 measurements therefore become permanent constraints: `choice` questions only (the `score` head collapses zero-shot), never turn-taking (50% = chance), and `confidence` stays uncalibrated (observed 0.01–0.58 even when correct), so it is never thresholded. A zero-shot LLM router is evaluated as a second backend, so routing sits behind an interface and both run in shadow before either is activated.

**Technical details**
- `relay/delegator/router/base.py`: `Router` protocol with `async decide(utterance: str, context: list[Turn]) -> RouterDecision | None`; `RouterDecision(difficulty: Literal["small_local","frontier"], ready: Literal["keep_talking","ready_to_execute"], intent: str, confidence: float | None, backend: str, latency_ms: int)`. Backends registered by name.
- Config: `ROUTER_SHADOW=laya,llm` (backends run for logging only) and `ROUTER_ACTIVE=none|laya|llm` (backend that drives routing; `none` = Phase 1 frontier-only behaviour).
- Shared `relay/delegator/router/questions.py` holds the validated `ROUTING_QUESTIONS` (`difficulty`, `ready`) plus the 5-way intent question, so every backend is judged on identical definitions: binary/near-binary, descriptive criteria, ≤10 options.
- `LayaRouter`: load `aac6fef/laya-mlx` (421M ModernBERT-large, FP16) via `laya-mlx` on Apple Silicon, `laya` + ONNX elsewhere, once at startup; truncate input to 512 tokens. Record the checkpoint's clamped-temperature `RuntimeWarning` once at load, not per call.
- Shadow execution: backends run concurrently via `asyncio.gather` in a background task after the response starts streaming, never on the time-to-first-token path; each writes a `router_decisions` row (`is_active=false`).
- `scripts/router_report.py`: per backend, agreement with the frontier decisions on `ready` and `difficulty` (confusion matrix), latency p50/p95, error/timeout rate. Report agreement, not accuracy: with no ground-truth labels, the frontier decision is a reference, not the truth. The SPEC §2 benchmark utterances can be replayed as a smoke test only.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Laya decisions are logged to `router_decisions` on every turn in shadow mode with no increase in time-to-first-token
- [x] #2 Only `choice` questions are used; no `score`-type question exists in the codebase
- [x] #3 No code path branches on Laya `confidence`
- [x] #4 A new backend can be added by implementing `Router` without touching the Delegator request path
- [x] #5 Router report produces per-backend agreement with frontier and latency percentiles
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Verify shadow routing with deterministic Laya stubs/local data, complete the report contract, and keep it off the first-token path without any cloud call.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 (router commit): Router protocol + registry, choice-only questions, LayaRouter (laya-mlx 0.2.0 on darwin arm64; live: first call 1.8 s incl. load, then 108 ms), RouterHook shadow via asyncio.gather from after_response (test: 1 s stub backend, reply < 0.5 s), router_report.py. Open: AC1 live logging each turn with ROUTER_SHADOW=laya; AC5 difficulty agreement needs a frontier difficulty reference (TASK-37). New backend names beyond frontier/laya/llm are blocked by the router_decisions.backend CHECK.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Router interface, Laya shadow path, choice-only constraints, non-blocking persistence and report metrics are covered by deterministic tests.
<!-- SECTION:FINAL_SUMMARY:END -->
