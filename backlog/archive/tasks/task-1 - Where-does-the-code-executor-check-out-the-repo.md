---
id: TASK-1
title: Where does the code executor check out the repo?
status: To Do
assignee: []
created_date: '2026-09-26 12:41'
labels:
  - open-question
  - spec-11
dependencies: []
references:
  - spec-v1-draft.md#11-open-questions
priority: high
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Cloud sandbox clones from the git host directly (durable, needs its own creds) vs. an agent on a machine with the existing checkout (no extra secrets, dies if the machine sleeps/closes). Determines how secrets are handled and whether a dispatched task survives a closed laptop.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Repo access strategy chosen (cloud sandbox vs local checkout)
- [ ] #2 Secrets handling approach documented for the chosen strategy
<!-- AC:END -->
