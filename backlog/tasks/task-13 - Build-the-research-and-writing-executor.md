---
id: TASK-13
title: Build the research and writing executor
status: To Do
assignee: []
created_date: '2026-09-26 13:18'
labels:
  - phase-1
  - executor
milestone: m-0
dependencies:
  - TASK-9
  - TASK-10
references:
  - docs/spec-v1-draft.md#3-scope
priority: high
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Pydantic AI agent, one tool (web research to synthesis to Markdown draft), durable via DBOS so dispatch_task returns immediately and survives a process restart.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 dispatch_task writes a commitment row plus a DBOS workflow handle and returns immediately
- [ ] #2 Completed artifact link lands in the idea graph regardless of whether it was spoken
- [ ] #3 Killing the process mid-task and restarting resumes the workflow
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) executors/registry.py: maps vertical name to workflow entrypoint (research now; Phase 2 adds code with zero signature changes)  2) executors/research/agent.py: Pydantic AI agent with one web-research tool  3) executors/research/workflows.py: @DBOS.workflow orchestrates the agent, each externally-visible effect (web call, LLM call, writing the artifacts row) is its own @DBOS.step so crash-recovery replay stays deterministic  4) DBOS configured on its own schema (dbos), same Postgres instance as idea_graph's schema, neither migration tool manages the other's tables  5) test: call dispatch_task directly (no voice loop) and confirm a commitment+task+artifact row and a real Markdown doc  6) test: kill the process mid-research-task and confirm DBOS.launch() resumes it on restart
<!-- SECTION:PLAN:END -->
