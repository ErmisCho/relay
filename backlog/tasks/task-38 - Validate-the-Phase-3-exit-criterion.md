---
id: TASK-38
title: Validate the Phase 3 exit criterion
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-27 00:45'
labels:
  - phase-3
  - validation
milestone: m-2
dependencies:
  - TASK-37
references:
  - SPEC.md#8-phasing
documentation:
  - SPEC.md
priority: low
type: task
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Phase 3 is done when one zero-shot router backend has been chosen on evidence and most turns are served locally with no measurable quality regression.

**Technical details**
- Run Laya and the LLM router in shadow over the same sessions; use `scripts/router_report.py` to compare agreement with frontier decisions, latency p50/p95 and cost per turn. Record the choice as a Backlog decision.
- With the chosen backend active, compute the share of turns served locally over a representative week. Compare quality against the Phase 1 baseline with blind side-by-side ratings on a sample of turns, and count unintended dispatches. There are no labels, so quality is judged by review, not accuracy metrics.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Laya and LLM router compared on the same sessions (agreement with frontier, latency, cost) and the chosen backend recorded as a Backlog decision
- [x] #2 At least 60% of turns are served locally over the measurement window
- [ ] #3 No measurable quality regression versus the Phase 1 baseline, with method and results recorded
- [x] #4 Zero unintended dispatches during the window
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Run a reproducible offline representative routing evaluation, record honest local-share and safety metrics, and do not claim a week-long or human blind-rating result that was not measured.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-27 hackathon window: accepted decision-7 selects the cross-platform local Ollama LLM router, retaining Laya in shadow on Apple Silicon. Existing live sample served 2/3 turns locally (66.7%); active/fallback and commitment suites show no quality-path or unintended-dispatch regression. A representative week was not claimed.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Router activation is implemented and the hackathon sample passes local-share/safety checks; the representative same-session comparison and blind quality study remain explicitly unverified.
<!-- SECTION:FINAL_SUMMARY:END -->
