---
id: TASK-11
title: Set up the Postgres idea graph schema
status: To Do
assignee: []
created_date: '2026-09-26 12:47'
labels:
  - phase-1
  - data-model
milestone: m-0
dependencies: []
references:
  - spec-v1-draft.md#7-data-model-idea-graph
priority: high
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
ideas, sessions, turns, commitments, tasks, artifacts, idea_edges. turns doubles as the labeling corpus for Phase 3/4 routing calibration, so every routing and gate decision must be logged with its outcome from day one.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 All seven tables from spec section 7 exist with the documented columns
- [ ] #2 Every turn logs route, model_used, latency_ms, laya_intent, laya_ready, laya_confidence (nullable until Phase 3)
<!-- AC:END -->
