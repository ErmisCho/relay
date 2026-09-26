---
id: TASK-31
title: Validate the Phase 1 exit criterion
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
labels:
  - phase-1
  - validation
milestone: m-0
dependencies:
  - TASK-24
  - TASK-27
  - TASK-28
  - TASK-29
  - TASK-30
references:
  - SPEC.md#8-phasing
documentation:
  - SPEC.md
priority: high
type: task
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Phase 1 is done only when the whole loop works for real: wake word → ElevenLabs → Delegator (frontier only) → commitment protocol → research executor → idea graph, with no Laya.

**Technical details**
- Run a scripted-but-live session: 10+ minute conversation spanning at least three ideas, including deliberate hedges and borderline moments.
- Afterwards query `commitments` (verify each has assent_utterance), `tasks` (all succeeded), `artifacts` (three documents) and `turns` (full log with latency).
- Record session cost (agent minutes + tokens) and median time-to-first-token as a baseline for Phase 3.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A 10-minute live conversation results in exactly three agreed commitments and three documents the user judges worth reading
- [ ] #2 Zero unintended dispatches during the session, verified from the commitments audit trail
- [ ] #3 The session closed automatically on silence or explicit end
- [ ] #4 Cost and latency baseline recorded in task notes
<!-- AC:END -->
