---
id: TASK-39
title: Calibrate Laya per-question temperatures on the turns corpus
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
labels:
  - phase-4
  - router
  - ml
milestone: m-3
dependencies:
  - TASK-38
references:
  - SPEC.md#2-laya--jev-evaluation--measured-not-estimated
  - SPEC.md#8-phasing
documentation:
  - SPEC.md
priority: low
type: feature
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Laya's confidence is uncalibrated, which blocks abstention (deferring to the frontier model when unsure). Fitting temperatures on our own labeled turns is what makes `confidence` usable.

**Technical details**
- Build a labeled set from `turns` (Laya logits/decisions vs. frontier decisions and outcomes) and `commitments` (confirmed assent / misfires); split train/held-out by session.
- Fit one temperature per question by minimising NLL on the held-out logits; report ECE and reliability diagrams before/after.
- Only after calibration: add an abstention threshold per question — below it, route to frontier. `score`-type questions may be re-evaluated here and only here.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Per-question temperatures fitted and versioned with ECE before/after reported
- [ ] #2 Abstention threshold implemented only for calibrated questions, with measured frontier-fallback rate
- [ ] #3 Uncalibrated questions still never threshold on confidence
<!-- AC:END -->
