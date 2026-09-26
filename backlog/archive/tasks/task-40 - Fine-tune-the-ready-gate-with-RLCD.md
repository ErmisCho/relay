---
id: TASK-40
title: Fine-tune the ready gate with RLCD
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 13:14'
labels:
  - phase-4
  - router
  - ml
  - protocol
milestone: m-3
dependencies: []
references:
  - SPEC.md#6-the-commitment-protocol
  - SPEC.md#8-phasing
documentation:
  - SPEC.md
priority: low
type: feature
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The zero-shot `ready` gate measured 88%. Fine-tuning on our own conversations with RLCD should improve it, but the gate remains a *hint*: it still may only propose, and dispatch still requires spoken assent to a read-back.

**Technical details**
- Construct contrastive pairs from the audit trail: turns preceding confirmed assent (positive) vs. turns preceding hedges, clarifying questions or rejected proposals (negative).
- Fine-tune the Laya checkpoint's `ready` head; evaluate on a held-out session split with emphasis on false-positive rate (proposals the user rejected).
- Ship behind a flag; A/B against the calibrated zero-shot gate.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Fine-tuned gate beats the calibrated zero-shot gate on held-out accuracy
- [ ] #2 False-positive proposal rate does not increase
- [ ] #3 The dispatch path still requires affirmative assent to a read-back (regression test)
<!-- AC:END -->
