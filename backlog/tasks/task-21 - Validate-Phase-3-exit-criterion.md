---
id: TASK-21
title: Validate Phase 3 exit criterion
status: To Do
assignee: []
created_date: '2026-09-26 13:20'
labels:
  - phase-3
  - validation
milestone: m-2
dependencies:
  - TASK-19
  - TASK-20
references:
  - docs/spec-v1-draft.md#8-phasing
priority: low
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
At least 60% of turns served locally with no measurable quality regression.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Measured local-serve rate is 60% or higher across a representative sample
- [ ] #2 No measurable quality regression vs. the frontier-only baseline
<!-- AC:END -->
