---
id: TASK-54
title: Start plainly instructed tasks without a read-back
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-27 05:37'
updated_date: '2026-09-27 05:49'
labels:
  - phase-1
  - delegator
  - commitment
milestone: m-0
dependencies: []
priority: high
type: feature
ordinal: 27000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Owner request (2026-09-27): the voice agent repeated every request back and waited for a yes even when the user had plainly told it to do the work. A fresh request judged 'directive' server-side now starts work directly; requests for feedback/ideas keep the read-back.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A fresh user request classified directive by the server starts the task without a read-back and records the request as the assent utterance
- [x] #2 Requests for feedback, ideas or opinions, classifier failures, replies to a read-back and DIRECT_DISPATCH=0 keep the read-back protocol
- [x] #3 A re-send during the grace window that is no longer a directive cancels the start
- [x] #4 The live story passes with direct starts, and pytest, mypy and ruff pass
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
classify.classify_directive (assent model, precheck only away from directive); protocol: DirectRequest set in CommitmentHook.before_model for fresh turns, ProposeCommitmentTool._directive/_start_now reserve with direct=True, same-turn re-sends re-judged via _redirect; Settings.direct_dispatch (DIRECT_DISPATCH, default on). Live gpt-6-luna check 11/11 phrases, ~0.6 s per proposing turn. Tests: tests/delegator/commitment/test_direct.py (8). Live story: 1 passed, 2 direct starts. Gate: 562 passed/16 skipped, mypy 0, ruff 0.

2026-09-27 owner feedback 'still too much just to confirm' (live session log): (1) 'Could you suggest something?' got a read-back because an exploring label fell back to the read-back; (2) the instruction that followed answered that read-back, and replies to a read-back were never allowed to start directly. Changed: a confident 'exploring' now drops the proposal with no speech (NOT_YET_RESULT; the model answers in its own words); only a failed check reads back. A non-negative reply to a read-back is offered for a direct start. Live replay of the owner's session (tests/story::test_live_voice_session_asks_nothing_back): suggestions answered, then 'On it.'; no read-back. Gate: 563 passed/17 skipped, mypy 0, ruff 0.
<!-- SECTION:NOTES:END -->
