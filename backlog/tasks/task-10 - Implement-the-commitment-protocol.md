---
id: TASK-10
title: Implement the commitment protocol
status: To Do
assignee: []
created_date: '2026-09-26 12:47'
labels:
  - phase-1
  - commitment-protocol
milestone: m-0
dependencies: []
references:
  - spec-v1-draft.md#6-the-commitment-protocol
priority: high
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Score/Propose/Assent/Dispatch/Report. The gate may only propose; dispatch requires explicit spoken assent to a spoken read-back naming the goal, scope boundary, and terminal artifact. propose_commitment and dispatch_task tools, writing a commitments row with a non-null assent_utterance. Hedges are not assent. Use the frontier model for assent classification in v1, not Laya.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Gate never dispatches without a spoken read-back + explicit assent
- [ ] #2 Every commitment row records the exact assent utterance
<!-- AC:END -->
