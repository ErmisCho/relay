---
id: TASK-30
title: >-
  Decide wake phrase, multi-device resumption and barge-in-during-report
  behaviour
status: Done
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 13:39'
labels:
  - phase-1
  - spike
  - voice
milestone: m-0
dependencies: []
references:
  - SPEC.md#11-open-questions
documentation:
  - SPEC.md
priority: medium
type: spike
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Three open questions from SPEC §11 affect Phase 1 UX and must be decided before the exit test: (2) pretrained openWakeWord phrase (free, immediate) vs. a custom Porcupine phrase (on-brand, licensed); (3) whether an idea started on one device resumes on another mid-thought or only on explicit request; (4) when the user barges in during a completion report, whether the announcement is abandoned or queued.

**Technical details**
- Wake phrase: benchmark 2–3 pretrained openWakeWord models for false accepts/hour and miss rate; price Porcupine custom keyword.
- Resumption: options are session-scoped current idea (explicit `recall`) vs. user-scoped "last focused idea" carried across devices; implications for `sessions` (add `device_id`?).
- Barge-in: ElevenLabs signals interruption to the client; decide whether the Delegator re-queues the `pending_report` if the report turn was interrupted.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Each of the three questions has a recorded decision with rationale as a Backlog decision
- [x] #2 Any schema or behaviour changes implied by the decisions are captured as tasks or AC updates
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 — Q2 (wake phrase) DECIDED by user: pretrained openWakeWord phrase (free, immediate); custom Porcupine keyword rejected (licensing cost, not needed for v1). Implementation default: `hey_jarvis`, configurable via WAKE_MODEL env var; TASK-24 benchmarks false accepts/hour. Q3 (multi-device resumption) and Q4 (barge-in during completion report) still OPEN — needed before TASK-31.

2026-09-26 — Q3 DECIDED by user (decision-2): session-scoped current idea; cross-device resumption only via explicit recall. No schema change (no sessions.device_id); covered by TASK-29 recall. Q4 DECIDED by user (decision-3): interrupted completion report is re-queued and re-offered at next turn boundary; captured as new TASK-26 AC#5.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
All three SPEC §11 Phase-1 questions decided by the user and recorded as decision-1 (pretrained openWakeWord phrase), decision-2 (resume only on explicit recall), decision-3 (re-queue interrupted completion reports). Implication captured as TASK-26 AC#5; no schema change needed.
<!-- SECTION:FINAL_SUMMARY:END -->
