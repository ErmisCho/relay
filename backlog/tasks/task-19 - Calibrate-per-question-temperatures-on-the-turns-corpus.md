---
id: TASK-19
title: Calibrate per-question temperatures on the turns corpus
status: To Do
assignee: []
created_date: '2026-09-26 12:49'
labels:
  - phase-4
  - calibration
milestone: m-3
dependencies:
  - TASK-18
references:
  - spec-v1-draft.md#8-phasing
priority: low
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Fit per-question temperatures on your own labeled turns data - this is what makes Laya confidence usable and unlocks abstention. The turns table logged from Phase 1 onward is the training corpus.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Per-question temperature fit completed on real turns data
- [ ] #2 Confidence-based abstention enabled only after calibration, never before
<!-- AC:END -->
