---
id: TASK-21
title: Scaffold the relay service and Postgres idea-graph schema
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 13:14'
labels:
  - phase-1
  - data
  - infra
milestone: m-0
dependencies: []
references:
  - SPEC.md#7-data-model-idea-graph
  - SPEC.md#5-components
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Every other component (Delegator, executor, DBOS, commitment audit trail) reads or writes the same Postgres database, so the schema and project layout must exist first. Routing and gate decisions are logged from day one so Phase 3 can compare router backends (zero-shot Laya vs. a zero-shot LLM router) against the frontier model's decisions. This is a debugging/comparison log, not a training corpus — no labeling, calibration or fine-tuning is planned.

**Technical details**
- Python 3.12 monorepo managed with `uv`: packages `relay/delegator` (FastAPI), `relay/executor` (Pydantic AI + DBOS), `relay/store` (SQLAlchemy 2.x async models + Alembic), `relay/client` (wake word + voice). `docker-compose.yml` runs Postgres 16 locally; config via `pydantic-settings` (`DATABASE_URL`, `ANTHROPIC_API_KEY`, `FRONTIER_MODEL`, `ELEVENLABS_*`).
- Alembic migration creating (SPEC §7): `ideas(id uuid pk, title, summary, status, maturity, created_at, updated_at)`; `sessions(id, started_at, ended_at, wake_trigger)`; `turns(id, session_id fk, idea_id fk null, role, text, ts, route, model_used, latency_ms)`; `commitments(id, idea_id fk, goal, scope_excludes, artifact_kind, readback_text, assent_utterance NOT NULL, assented_at NOT NULL)`; `tasks(id, commitment_id fk NOT NULL, kind, dbos_workflow_id unique, status, started_at, finished_at, error)`; `artifacts(id, task_id fk, kind, url, summary, reviewed_at)`; `idea_edges(from_idea, to_idea, relation, pk(from_idea,to_idea,relation))`.
- Deviation from SPEC §7: the `laya_intent/laya_ready/laya_confidence` columns on `turns` are replaced by `router_decisions(id, turn_id fk, backend, difficulty, ready, intent, confidence null, latency_ms, is_active bool)` so several backends (frontier, laya, llm) can log a decision for the same turn in shadow mode. `is_active` marks the decision that actually drove routing.
- Enums as Postgres CHECK constraints: ideas.status ∈ {exploring, committed, executing, delivered, abandoned}; tasks.kind ∈ {code, research}; artifacts.kind ∈ {pull_request, document}; idea_edges.relation ∈ {refines, supersedes, blocks, spun_off_from}; turns.route ∈ {small_local, frontier}; router_decisions.backend ∈ {frontier, laya, llm}.
- Indexes: `turns(session_id, ts)`, `turns(idea_id)`, `tasks(status)`, `router_decisions(turn_id)`, GIN `to_tsvector('english', title || ' ' || summary)` on ideas for recall.
- DBOS keeps its own system schema in the same database (it creates it on `DBOS.launch()`); do not model it in Alembic.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 `docker compose up` plus `alembic upgrade head` produces the SPEC §7 tables (with the `router_decisions` deviation) and all listed columns
- [ ] #2 Inserting a commitment with NULL `assent_utterance` or a task without a `commitment_id` fails at the database level
- [ ] #3 Invalid enum values for ideas.status, tasks.kind, artifacts.kind, idea_edges.relation and router_decisions.backend are rejected by CHECK constraints
- [ ] #4 Multiple `router_decisions` rows from different backends can reference the same turn
- [ ] #5 `alembic downgrade base` cleanly removes the schema
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Migration tested against a fresh Postgres container
- [ ] #2 README documents local setup
<!-- DOD:END -->
