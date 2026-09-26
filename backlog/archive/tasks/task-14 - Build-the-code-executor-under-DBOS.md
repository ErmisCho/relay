---
id: TASK-14
title: Build the code executor under DBOS
status: To Do
assignee: []
created_date: '2026-09-26 12:48'
updated_date: '2026-09-26 13:14'
labels:
  - phase-2
  - executor
milestone: m-1
dependencies: []
references:
  - spec-v1-draft.md#8-phasing
priority: medium
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Pydantic AI code agent: repo checkout, branch, write code/refactor, run tests, open a PR. Stops at PR - never merges. Repo access approach depends on how TASK-1 (Q1) is answered.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Executor checks out a repo, makes a change, runs tests, opens a PR
- [ ] #2 No code path in the executor can merge a PR
<!-- AC:END -->
