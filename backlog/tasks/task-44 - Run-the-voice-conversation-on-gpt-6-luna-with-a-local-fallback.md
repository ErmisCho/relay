---
id: TASK-44
title: Run the voice conversation on gpt-6-luna with a local fallback
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-26 17:38'
updated_date: '2026-09-26 18:49'
labels:
  - phase-1
  - delegator
  - router
milestone: m-0
dependencies:
  - TASK-27
references:
  - SPEC.md
priority: high
type: feature
ordinal: 21000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Owner decision (2026-09-26): the main voice agent (the Delegator's conversational model) should be OpenAI gpt-6-luna, with local ollama:gemma4:e4b as fallback. The OpenAI key is in .env; settings already parse provider-prefixed model refs. Voice needs low time-to-first-token, and gpt-6-luna's streaming/reasoning behaviour and latency are unmeasured. Safety-critical local calls (assent classifier, scope checks) stay on local gemma. Conversation content now leaves the machine to OpenAI, which the owner accepted by choosing this.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 DELEGATOR_MODEL=openai:gpt-6-luna and DELEGATOR_FALLBACK_MODEL=ollama:gemma4:e4b work end to end through the Delegator (streaming, internal tools, ElevenLabs system tools)
- [x] #2 Time-to-first-token for gpt-6-luna through the Delegator is measured on a real socket over at least 10 turns and recorded; reasoning/latency settings are chosen so the median is under 1 s, or the gap is reported
- [x] #3 An OpenAI error or a missed first-delta deadline falls back to local gemma without the user hearing an error, verified by test
- [x] #4 The assent classifier, ready scorer and scope checks keep using local models regardless of DELEGATOR_MODEL
- [x] #5 The existing live scope tests (in-scope answered, out-of-scope refused) pass with gpt-6-luna as the delegator model
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
llm/factory: openai provider with reasoning_effort=none for the delegator (configurable), keep 5 s first-delta fallback to gemma; verify streaming+internal tools+system tools with gpt-6-luna; real-socket TTFT ≥10 turns; assent/ready/scope stay local; live scope tests with gpt-6-luna.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 session 85558081 W1 verification at 7a8b383 (no code changes needed). Live (real socket, DELEGATOR_MODEL=openai:gpt-6-luna, fallback ollama:gemma4:e4b, reasoning_effort default none): TTFT ms over 12 turns [721, 771, 819, 904, 905, 920, 938, 970, 1064, 1147, 1574, 1682]; p50 929, p90 1574, min 721, max 1682; all 12 served by gpt-6-luna, no fallback. 4/12 turns > 1 s (median target met, tail not). Live scope: in-scope answered, email/booking/Slack refused ("I can't do that yet"); propose_commitment ran server-side 3/3. Offline: test_openai_failure_falls_back_to_gemma, test_missed_first_delta_deadline_falls_back, test_fallback_is_silent_and_recorded_through_the_delegator, test_classifier_models_never_follow_delegator_model all pass; build_model call sites: assent/ready/summary use their own settings. Gates: 412 passed/13 skipped, mypy 0, ruff 0. OPEN: AC#1 ElevenLabs system tools (end_call) only proven offline — close in owner live voice run. Note: owner .env currently DELEGATOR_MODEL=ollama:qwen3:30b-a3b-instruct-2507-q4_K_M, so the running app is not on luna yet.
<!-- SECTION:NOTES:END -->
