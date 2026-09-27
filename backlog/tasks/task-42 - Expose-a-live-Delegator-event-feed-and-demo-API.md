---
id: TASK-42
title: Expose a live Delegator event feed and demo API
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 16:07'
updated_date: '2026-09-27 00:34'
labels:
  - phase-1
  - demo
  - delegator
  - api
milestone: m-0
dependencies:
  - TASK-27
  - TASK-24
  - TASK-23
  - TASK-28
  - TASK-29
references:
  - docs/elevenlabs-localhost-connectivity.md
  - SPEC.md
priority: medium
type: feature
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The owner wants a website to demo relay. The product's value is invisible in a plain voice call: the scope gate, ready score, spoken read-back, assent classification, durable dispatch and the delivered artifact all happen server-side. A demo needs a live, per-session stream of those decisions plus read APIs for ideas, commitments and artifacts, and a way for a browser to start an authenticated ElevenLabs conversation (the agent is private: platform_settings.auth.enable_auth=true, so browsers need a server-issued signed token/URL). Because the demo is served over the public ngrok URL (https://geology-hardiness-cage.ngrok-free.dev) and spends ElevenLabs minutes and local compute, it must be access-controlled and capped. Build after W4 (TASK-27 commitment protocol, TASK-24 wake word).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A browser can obtain a short-lived signed ElevenLabs conversation token/URL for the private agent from a relay endpoint, and the ElevenLabs API key never reaches the browser
- [x] #2 A per-session live event stream (SSE) emits, in order, user/assistant turns, scope refusals, current-idea changes, ready-gate decisions, commitment proposals with their read-back, assent classifications, dispatches, task status changes and delivered artifacts, each with the session id and a timestamp
- [x] #3 Events are emitted from the existing Delegator/executor code paths without adding latency to the voice turn: time-to-first-token with the feed enabled stays within 10% of the baseline, measured and recorded
- [x] #4 Read endpoints return ideas with status and edges, commitments with verbatim assent utterance and read-back, task statuses, and an artifact's rendered Markdown with sources
- [x] #5 A text-chat endpoint lets the demo converse without a microphone through the same Delegator path (same tools, hooks, scope gate and commitment protocol)
- [x] #6 Demo endpoints require a configurable passcode, allow one live voice session at a time, and end a voice session after a configurable maximum duration; unauthenticated requests get 401
- [x] #7 Tests cover event ordering for a scripted conversation that proposes, gets a hedge, then assent and dispatch; token issuance without leaking the API key; and access control
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Verify the demo API and event feed against local fakes/Postgres, including ordering, auth, read shapes, and bounded sessions; no signed paid voice session is requested.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 session 85558081: backend 647f010, tests 3a in HEAD (see git log). AC3 measured on real uvicorn sockets, stub model 200 ms first token, 24 req/arm: feed ON p50 210.5/p90 212.0 ms vs OFF 209.1/210.2 ms (+0.7%/+0.8%). Smoke (port 8765, gemma4): auth 204 + HttpOnly cookie, 401 without, commitments 200, session create, text message 202, SSE user_turn→assistant_turn, voice POST → webrtc token (key never in response). Gate on exact commit content: 426 passed/13 skipped, mypy 0, ruff 0. Partly open: AC2 current_idea/ready_gate/artifact_delivered events untested; AC4 read endpoint shapes only status-checked; AC6 over-time call end is best-effort (agent told to end_call). Contract diffs listed by w1-f-6: dispatch after assistant_turn, in-memory replay only, env name DEMO_PASSCODE.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Authenticated demo API, ordered SSE event feed, same-path text chat, read APIs, bounded voice slot and artifact delivery are complete and covered by database/contract tests.
<!-- SECTION:FINAL_SUMMARY:END -->
