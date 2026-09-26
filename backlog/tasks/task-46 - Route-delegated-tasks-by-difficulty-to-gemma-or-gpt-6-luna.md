---
id: TASK-46
title: Route delegated tasks by difficulty to gemma or gpt-6-luna
status: To Do
assignee: []
created_date: '2026-09-26 17:38'
updated_date: '2026-09-26 17:38'
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
- [ ] #1 At dispatch the router's easy/hard decision is made once per task and logged to router_decisions for the commitment's user turn, with backend, latency and the chosen model
- [ ] #2 Easy tasks run on ollama:gemma4:e4b; hard tasks run on openai:gpt-6-luna with ollama:qwen3.8:latest as fallback; verified by an executor test with stub models
- [ ] #3 A router failure, timeout or invalid output routes the task as hard
- [ ] #4 The served model per task is recorded (log and task/artifact metadata) so a finished task shows which model actually produced it
- [ ] #5 Model choices are configurable via .env without code changes
<!-- AC:END -->
