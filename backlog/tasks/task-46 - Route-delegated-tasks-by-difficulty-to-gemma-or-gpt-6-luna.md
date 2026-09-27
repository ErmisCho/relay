---
id: TASK-46
title: Route delegated tasks by difficulty to gemma or gpt-6-luna
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 17:38'
updated_date: '2026-09-27 00:34'
labels:
  - phase-1
  - router
  - executor
milestone: m-0
dependencies:
  - TASK-45
  - TASK-28
priority: high
type: feature
ordinal: 23000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Owner decision (2026-09-26): when a commitment is dispatched, the chosen router (see the benchmark task) classifies the task as easy or hard. Easy tasks run on local gemma4:e4b; hard tasks run on gpt-6-luna with local qwen3.8 as fallback. Today the research executor always uses qwen3.8 → gemma. The routing decision must be auditable and must never affect the commitment invariant (routing only picks the model; dispatch still needs spoken assent).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 At dispatch the router's easy/hard decision is made once per task and logged to router_decisions for the commitment's user turn, with backend, latency and the chosen model
- [x] #2 Easy tasks run on ollama:gemma4:e4b; hard tasks run on openai:gpt-6-luna with ollama:qwen3.8:latest as fallback; verified by an executor test with stub models
- [x] #3 A router failure, timeout or invalid output routes the task as hard
- [x] #4 The served model per task is recorded (log and task/artifact metadata) so a finished task shows which model actually produced it
- [x] #5 Model choices are configurable via .env without code changes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
executor/routing.py: gemma4 v2 easy/hard router (prompt from benchmarks/router/common.py v2), 5 s timeout, failure→hard; research runner picks model per decision (easy: gemma4; hard: gpt-6-luna → qwen3.8 fallback), configurable via .env; migration 0002 lets router_decisions reference the task (task_id, turn_id nullable) and records chosen/served model; served model on artifact/task; tests with stub models + router.

Owner supersession (2026-09-27): route both easy and hard tasks to configurable local Ollama models by default; retain provider abstraction but make paid cloud models opt-in and do not invoke them.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 session 85558081 W2 verification (w2-a-1, no code changes). AC2: defaults config.py router/easy=gemma4:e4b, hard=gpt-6-luna→qwen3.8; test_route_models + e2e easy/hard/fallback. AC3: classify never raises; invalid/timeout/http/unreachable/unsupported → hard (unit) + test_router_failure_routes_hard (e2e). AC4: log "served by <model>" + tasks.served_model (artifacts table has no model column). AC5: .env keys ROUTER_MODEL, ROUTER_TIMEOUT_S, RESEARCH_EASY/HARD/HARD_FALLBACK_MODEL. Live gemma4 router: M4 Pro research goal → hard 552 ms; SSH-key overview → easy 556 ms. Gates: 416 passed/13 skipped, mypy 0, ruff 0. OPEN AC1: decision stored once per task (DBOS step + partial unique index, survives SIGKILL) with backend/latency/model_chosen, but router_decisions.turn_id is always NULL — commitments has no assent turn id. Linking needs migration 0003 commitments.assent_turn_id + protocol.py + runner change and a fix for uq_router_decisions_active; or reword AC1 to task-level. Owner decision.

2026-09-26: AC1 closed — migration 0003 adds commitments.assent_turn_id (written by the commitment protocol) and narrows uq_router_decisions_active to task_id IS NULL; runner.record_route_step sets router_decisions.turn_id to the assent turn. test_easy_task_runs_on_easy_model_and_logs_decision asserts the link (fails with the runner change removed). Gate: 461 passed/14 skipped, mypy 0, ruff 0.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Task difficulty routing is auditable, durable across worker kill, fail-safe to hard, records chosen/served models and defaults both routes to local Ollama.
<!-- SECTION:FINAL_SUMMARY:END -->
