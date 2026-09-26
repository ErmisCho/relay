---
id: TASK-12
title: Integrate ElevenLabs Agents SDK for the voice loop
status: To Do
assignee: []
created_date: '2026-09-26 13:18'
labels:
  - phase-1
  - voice-loop
milestone: m-0
dependencies:
  - TASK-9
references:
  - docs/spec-v1-draft.md#5-components
priority: high
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
WebRTC voice loop: STT, VAD, turn-taking, barge-in, streaming TTS (Flash v2.5). Developed against a stub SSE endpoint first; converges with the real Delegator at the Phase 1 exit checkpoint.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A live WebRTC session round-trips through an OpenAI-compatible endpoint
- [ ] #2 Barge-in interrupts TTS playback correctly
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) client/src/relay_client/elevenlabs_session.py: WebRTC session pointed at an OpenAI-compatible endpoint (stub locally, real Delegator later)  2) verify barge-in interrupts TTS playback correctly against the stub  3) integration checkpoint against the real Delegator once it exists
<!-- SECTION:PLAN:END -->
