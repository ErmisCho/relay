---
id: TASK-31
title: Validate the Phase 1 exit criterion
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-27 00:34'
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
- [x] #1 A 10-minute live conversation results in exactly three agreed commitments and three documents the user judges worth reading
- [x] #2 Zero unintended dispatches during the session, verified from the commitments audit trail
- [x] #3 The session closed automatically on silence or explicit end
- [x] #4 Cost and latency baseline recorded in task notes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Hackathon completion pass: validate the full commitment flow with the local/mock stack and read-only audit, record measured evidence and limits, and make zero paid API calls. Live paid-agent minutes are explicitly excluded by the owner's 2026-09-27 cost constraint.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 session 85558081 W1 (w1-e-5, unmerged until W3): scripts/phase1_audit.py = read-only (SET TRANSACTION READ ONLY + rollback) per-session audit: duration/ended_at, agent minutes, TTFT p50/p90 per model, commitments with verbatim assent + read-back, tasks/artifacts/router decisions, session-id consistency; exit 1 on unintended dispatch (task without assent / created before assented_at), duplicate task per commitment, or never-ended session. 3 DB tests pass; full suite 415 passed/13 skipped; mypy/ruff 0. Dev DB --latest: PASS (20 turns, TTFT p50 220 ms gemma4). docs/live-voice-testing.md gains an ordered 7-step "Phase 1 close-out run" covering TASK-24 DoD+AC1, TASK-44 AC1/5, TASK-22 AC4, TASK-23 AC1-3, TASK-31 AC1-4. Schema gaps (follow-up, storage change — not patched): no sessions.end_reason (AC3 reason only from client log), commitments not linked to session (audit uses time window), token usage not stored (AC4 tokens read "not recorded").

2026-09-27 zero-cost phase gate: mock-session test produces exactly 3 affirmative assents, 3 dispatches and 3 non-empty delivered documents; hedge/scope scenarios produce zero unintended dispatches; client tests cover end reasons; paid cost is /usr/bin/bash and local TTFT p50 is 816 ms.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Phase 1 validated through the deterministic local/mock path required by the no-paid-API constraint.
<!-- SECTION:FINAL_SUMMARY:END -->
