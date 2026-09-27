---
id: TASK-25
title: Enforce the v1 scope boundary with hard refusal
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 17:28'
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
- [x] #1 Requests to send email, schedule meetings, post to Slack or control a browser produce a spoken refusal and no commitment proposal
- [x] #2 A tool call with an out-of-scope kind or artifact_kind is rejected server-side even if the model emits it
- [x] #3 While `code` is not in ENABLED_KINDS, code requests are refused rather than attempted
- [x] #4 Test suite covers at least 10 out-of-scope utterances with zero proposals
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
scope.py (verticals, ENABLED_KINDS gate, kind/artifact_kind enums, validate_commitment_args, out-of-scope intent detection), prompts/system.md (verticals + exact refusal phrasing), ScopeHook (injects scope rules, flags refused turns in turns.metadata); tests: 10+ out-of-scope utterances with zero proposals, server-side rejection of out-of-scope tool args, code refused while not enabled.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 owner decision (decision-4): 'draft an email/message for me to send' is refused outright, not turned into a writing document; the spoken refusal must add that it might be supported in a future version. Applied in prompts/system.md example and scope.refusal_instruction.

2026-09-26 W3: three layers — system prompt in the cached prefix (ScopeHook.system_prefix), server-side validate_commitment_args/scope_guard (validation-only), precision-first detector adding a refusal note + turns.metadata.refused. Review fix: detector false positives 11/37 → 0/37 and guard no longer vetoes on the flag. Evidence: 73 scope tests; live gemma4 refused 15/15 out-of-scope asks with 0 tool calls. Known recall gaps (prompt covers them): 'Email Sarah the summary', 'Forward this to my boss', 'Publish the doc on our blog', 'Send the brief to the client'.

2026-09-26 live finding (owner call, session 2709997b): gemma4:e4b refused in-scope conversation ('how to run local LLMs on my Mac' → 'I can't give technical advice…') and a research-doc request. The detector did NOT flag (no turns.metadata.refused); the model over-applied the refusal-heavy prompt. Fix (coordinator): prompts/system.md now leads with 'conversation is ALWAYS allowed; refusing is only about taking actions', states research/document requests are in scope via propose_commitment, and adds a spoken style rule (1–3 plain sentences, no markdown). Evidence: new opt-in live test tests/delegator/test_scope_overrefusal.py 12/12 in-scope answered (incl. the owner's exact phrasings); existing live out-of-scope test still 15/15 refused, 0 tool calls.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Hard v1 scope boundary: scope rules in the system prompt (decision-4 refusal wording), server-side kind/artifact validation with ENABLED_KINDS, and a precision-first out-of-scope detector that logs refusals on the turn. Verified by 73 tests and a live gemma run (15/15 refused, zero proposals).
<!-- SECTION:FINAL_SUMMARY:END -->
