---
id: TASK-17
title: Build the code executor under DBOS
status: To Do
assignee: []
created_date: '2026-09-26 13:19'
labels:
  - phase-2
  - executor
milestone: m-1
dependencies:
  - TASK-6
  - TASK-16
references:
  - docs/spec-v1-draft.md#8-phasing
priority: medium
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Pydantic AI code agent: repo checkout (per the repo-access decision), branch, write code/refactor, run tests, open a PR. Stops at PR - never merges. Adds one entry to executors/registry.py; dispatch_task's contract is unchanged from Phase 1.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Executor checks out a repo, makes a change, runs tests, opens a PR
- [ ] #2 No code path in the executor can merge a PR
<!-- AC:END -->
