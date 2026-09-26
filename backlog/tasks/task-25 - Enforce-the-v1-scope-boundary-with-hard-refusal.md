---
id: TASK-25
title: Enforce the v1 scope boundary with hard refusal
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
labels:
  - phase-1
  - delegator
  - safety
milestone: m-0
dependencies:
  - TASK-22
references:
  - SPEC.md#3-scope
  - SPEC.md#10-risks
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
An unbounded action space is exactly what killed rabbit. v1 is hard-bounded to two verticals — code & repos (terminal artifact: unmerged PR) and research & writing (terminal artifact: unsent Markdown doc). Email, calendar, Slack, browser/GUI automation and anything outward-facing or irreversible must get a clear "I can't do that yet", never a degraded attempt.

**Technical details**
- Defence in depth, three layers: (1) system prompt lists in-scope verticals and the exact refusal phrasing; (2) `propose_commitment` / `dispatch_task` tool schemas constrain `artifact_kind` to an enum (`document` in Phase 1, `pull_request` added in Phase 2) and `kind` to {research, code} — Pydantic validation rejects anything else and returns a refusal instruction to the model; (3) executors are given no tools capable of sending, publishing, merging or driving a browser.
- Maintain `ENABLED_KINDS` config so the code vertical is refused until Phase 2 ships.
- Refusal events are logged on the turn (`route` unchanged, a `refused` flag in turn metadata) for later review.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Requests to send email, schedule meetings, post to Slack or control a browser produce a spoken refusal and no commitment proposal
- [ ] #2 A tool call with an out-of-scope kind or artifact_kind is rejected server-side even if the model emits it
- [ ] #3 While `code` is not in ENABLED_KINDS, code requests are refused rather than attempted
- [ ] #4 Test suite covers at least 10 out-of-scope utterances with zero proposals
<!-- AC:END -->
