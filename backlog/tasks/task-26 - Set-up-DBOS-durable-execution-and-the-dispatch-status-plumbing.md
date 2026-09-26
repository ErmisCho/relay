---
id: TASK-26
title: Set up DBOS durable execution and the dispatch/status plumbing
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 14:55'
labels:
  - phase-1
  - executor
  - infra
milestone: m-0
dependencies:
  - TASK-21
references:
  - SPEC.md#6-the-commitment-protocol
  - 'https://docs.dbos.dev/python/programming-guide'
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Dispatch must return immediately so the conversation continues uninterrupted, and long tasks must survive crashes and restarts (SPEC §10). DBOS gives database-backed durable workflows without a separate workflow engine and shares the idea-graph Postgres.

**Technical details**
- `DBOS(config={"name": "relay", "database_url": DATABASE_URL})`, `DBOS.launch()` in the FastAPI lifespan of a dedicated executor worker process (separate from the Delegator so voice latency is isolated).
- `start_task(commitment_id, kind)`: insert `tasks(status='queued')`, then `with SetWorkflowID(f"task-{task_id}"): handle = DBOS.start_workflow(WORKFLOWS[kind], task_id)`; store `handle.workflow_id` in `tasks.dbos_workflow_id`. Deterministic IDs make dispatch idempotent.
- Workflows update `tasks.status` (queued → running → succeeded|failed) and `started_at`/`finished_at`/`error` in `@DBOS.step()`s; on success insert an `artifacts` row and set `ideas.status='delivered'`.
- `get_status(task_id|idea_id)` reads `tasks` joined with `DBOS.get_workflow_status(workflow_id)`.
- Completion reports: on workflow completion enqueue a `pending_report` for the session; the Delegator drains it on the next user turn by injecting a system note ("task X finished: <summary>, link stored") so the announcement lands at a natural turn boundary, never mid-sentence. The artifact link is in the idea graph whether or not it is spoken.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Dispatching a task returns within 200 ms with a workflow id persisted in `tasks.dbos_workflow_id`
- [x] #2 Killing the executor process mid-workflow and restarting it resumes the workflow to completion
- [x] #3 Dispatching the same commitment twice does not start two workflows
- [x] #4 A completed task is announced on the next agent turn after completion, not mid-response, and the artifact row exists regardless
- [x] #5 If the user barges in during a completion announcement, the report stays pending and is re-offered briefly at the next turn boundary (decision-3)
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. executor worker process (FastAPI lifespan, DBOS.launch, queue) separate from the Delegator; DBOS system DB set explicitly.
2. dispatch.start_task(commitment_id, kind): tasks row + DBOSClient enqueue with deterministic workflow id task-<id>; idempotent per commitment; <200 ms.
3. Generic durable task workflow: status queued→running→succeeded|failed in steps, per-kind runner registry, artifact row + ideas.status=delivered + pending_report on success.
4. get_status internal tool (tasks joined with DBOS workflow status).
5. Pending-report TurnHook: offer at next turn boundary, confirm delivery from the next request's history, re-offer if interrupted (decision-3).
6. Tests incl. kill -9 / restart resume, double dispatch, <200 ms, report delivery + barge-in re-offer.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 W2: executor worker process (python -m relay.executor) launches DBOS 3.1 with system tables in the app DB's dbos schema (no separate _dbos_sys DB), application_version pinned (relay-1). dispatch.start_task: commitment row FOR UPDATE + deterministic workflow id task-<id> + return-existing → idempotent; DBOSClient only on the Delegator side (async, failure-cached, warmed at Delegator startup). Generic run_task workflow with retrying idempotent DB steps; runner registry (register_runner; research runner module to be added to BUILTIN_RUNNER_MODULES). Orphaned queued tasks reconciled on worker startup. ReportsHook: offers only on user turns; delivery confirmed by exact normalised-prefix match of generated text vs ElevenLabs-recorded assistant text; interrupted → brief re-offer, capped at 2 (decision-3). Evidence: tests/executor — dispatch <200 ms, SIGKILL mid-workflow + restart resumes, double dispatch → one workflow, next-turn announcement, barge-in re-offer, failure → failed, get_status tool, orphan reconcile. Review: 2 rounds, verdict PROCEED.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
DBOS durable execution in a separate executor worker; idempotent sub-200 ms dispatch via DBOSClient; retrying idempotent workflow steps with artifact + idea delivered + pending report; get_status tool; completion reports announced at the next turn boundary and re-offered after barge-in. Verified by tests against real Postgres incl. SIGKILL/restart and double dispatch.
<!-- SECTION:FINAL_SUMMARY:END -->
