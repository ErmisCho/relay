---
id: TASK-26
title: Set up DBOS durable execution and the dispatch/status plumbing
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
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
- [ ] #1 Dispatching a task returns within 200 ms with a workflow id persisted in `tasks.dbos_workflow_id`
- [ ] #2 Killing the executor process mid-workflow and restarting it resumes the workflow to completion
- [ ] #3 Dispatching the same commitment twice does not start two workflows
- [ ] #4 A completed task is announced on the next agent turn after completion, not mid-response, and the artifact row exists regardless
<!-- AC:END -->
