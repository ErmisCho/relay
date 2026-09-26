---
id: TASK-10
title: Set up the Postgres idea graph schema
status: To Do
assignee: []
created_date: '2026-09-26 13:17'
labels:
  - phase-1
  - data-model
milestone: m-0
dependencies:
  - TASK-9
references:
  - docs/spec-v1-draft.md#7-data-model-idea-graph
priority: high
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
SQLAlchemy models plus Alembic migration for ideas, sessions, turns, commitments, tasks, artifacts, idea_edges. turns logs route/model_used/latency_ms/laya_intent/laya_ready/laya_confidence (nullable until Phase 3).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 All seven tables exist with the documented columns
- [ ] #2 turns row can be written with the Phase-3 columns nullable
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) server/src/relay_server/idea_graph/models.py: SQLAlchemy 2.0 declarative models for all 7 tables per spec section 7  2) Alembic revision creating them, scoped to a dedicated schema (not DBOS's own schema, which lives in the same Postgres instance)  3) db.py: async engine/session factory  4) unit tests: create and read a row in each table
<!-- SECTION:PLAN:END -->
