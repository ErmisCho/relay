---
id: TASK-4
title: What happens when a completion report gets interrupted?
status: To Do
assignee: []
created_date: '2026-09-26 12:41'
labels:
  - open-question
  - spec-11
dependencies: []
references:
  - spec-v1-draft.md#6-commitment-protocol
priority: medium
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Drop: abandon the announcement, the user's turn wins outright (artifact link still lands in the idea graph either way). Queue: hold the rest of the report and resume at the next natural pause. The spec's commitment protocol (step 5, Report) doesn't say which.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Barge-in behavior during report announcements decided (drop vs queue)
<!-- AC:END -->
