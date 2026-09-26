---
id: TASK-15
title: Implement the commitment protocol
status: To Do
assignee: []
created_date: '2026-09-26 13:18'
labels:
  - phase-1
  - commitment-protocol
milestone: m-0
dependencies:
  - TASK-10
  - TASK-11
  - TASK-13
references:
  - docs/spec-v1-draft.md#6-the-commitment-protocol
priority: high
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Score to Propose to Assent to Dispatch to Report. The gate may only propose; dispatch requires explicit spoken assent to a spoken read-back naming goal/scope/artifact. Decision: drop (not queue) the rest of a Report announcement on barge-in.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Gate never dispatches without a spoken read-back plus explicit assent
- [ ] #2 Every commitment row records the exact assent utterance
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) propose.py: propose_commitment tool, speaks a read-back (goal, scope boundary, terminal artifact)  2) assent.py: frontier-model classifier - hedges like 'sure, I guess' are not assent  3) dispatch.py: on real assent, writes the commitments row (non-null assent_utterance) and calls dispatch_task from the registry  4) report.py: announces completion at the next turn boundary; on barge-in, drops the rest of the announcement (decided, no queue/resume state machine)  5) test: hedge inputs never trigger dispatch; every dispatched commitment has a recorded assent_utterance
<!-- SECTION:PLAN:END -->
