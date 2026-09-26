---
id: TASK-23
title: Fine-tune the ready gate with RLCD
status: To Do
assignee: []
created_date: '2026-09-26 13:20'
labels:
  - phase-4
  - calibration
milestone: m-3
dependencies:
  - TASK-22
references:
  - docs/spec-v1-draft.md#8-phasing
priority: low
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Fine-tune the gate with RLCD using the calibrated turns corpus. Revisit score-type questions only here - not before, since score collapses zero-shot per spec section 2.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Gate fine-tuned with RLCD on the labeled corpus
- [ ] #2 Any reconsideration of score-type questions happens only in this task, not earlier phases
<!-- AC:END -->
