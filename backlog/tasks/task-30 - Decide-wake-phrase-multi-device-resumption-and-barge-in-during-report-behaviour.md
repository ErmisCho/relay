---
id: TASK-30
title: >-
  Decide wake phrase, multi-device resumption and barge-in-during-report
  behaviour
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
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
- [ ] #1 Each of the three questions has a recorded decision with rationale as a Backlog decision
- [ ] #2 Any schema or behaviour changes implied by the decisions are captured as tasks or AC updates
<!-- AC:END -->
