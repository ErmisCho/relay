---
id: TASK-6
title: Decide repo access strategy for the Phase 2 code executor
status: To Do
assignee: []
created_date: '2026-09-26 13:16'
labels:
  - decision
  - blocks-phase-2
dependencies: []
references:
  - docs/spec-v1-draft.md#11-open-questions
priority: high
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Cloud sandbox clone (durable, needs own creds) vs. an agent on a machine with the existing checkout (no extra secrets, dies if the machine sleeps). Blocks the Phase 2 code executor task only - Phase 1 is unaffected.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Repo access strategy chosen and documented
<!-- AC:END -->
