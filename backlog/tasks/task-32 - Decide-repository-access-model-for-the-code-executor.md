---
id: TASK-32
title: Decide repository access model for the code executor
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 18:34'
labels:
  - phase-2
  - spike
  - executor
milestone: m-1
dependencies:
  - TASK-31
references:
  - SPEC.md#11-open-questions
documentation:
  - SPEC.md
priority: medium
type: spike
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Open question SPEC §11.1: does the code executor run in a cloud sandbox cloning from a git host, or as an agent on a machine with the checkout? This decides secrets handling and whether tasks survive a closed laptop, and blocks the code executor.

**Technical details**
- Compare: (a) cloud sandbox (container per task, clone via GitHub App installation token, survives laptop closure, needs remote DBOS worker) vs. (b) local worker using the user's checkout (no secret sharing, dies with the laptop, but DBOS resumes on reopen).
- Evaluate token scoping: GitHub App / fine-grained PAT with `contents:write` + `pull_requests:write`; branch protection on default branches so PRs cannot be merged without review.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Decision recorded as a Backlog decision covering secrets, durability and sandboxing
- [ ] #2 Code executor task updated with the chosen model
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 owner input: a new idea asked by the user gets a new project folder, independent from relay/. The code executor works in that local folder rather than in a clone of the relay checkout. Still open for this spike: where the folder root lives, how a PR remote is created or chosen for a brand-new project, token scoping, and survival when the laptop is closed.
<!-- SECTION:NOTES:END -->
