---
id: TASK-16
title: Integrate Laya routing with shadow-mode validation
status: To Do
assignee: []
created_date: '2026-09-26 12:48'
updated_date: '2026-09-26 13:07'
labels:
  - phase-3
  - routing
milestone: m-2
dependencies: []
references:
  - spec-v1-draft.md#2-laya-jev-evaluation
priority: low
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Drop Laya into the Delegator using the validated choice-only schemas (difficulty, ready) - never score, which collapses zero-shot. Shadow-mode first: log Laya decision alongside the frontier model decision for ~500 turns before promoting. Route small_local to Ollama (gemma4:e4b, glm-4.7-flash) only when the client capability profile allows it; cloud stays the default path.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Laya choice schemas wired for difficulty and ready decisions only, no score usage
- [ ] #2 Shadow-mode log compares Laya vs frontier for at least 500 turns before any promotion
- [ ] #3 Never thresholds on Laya confidence - checkpoint is uncalibrated per section 2
<!-- AC:END -->
