---
id: TASK-8
title: Integrate ElevenLabs Agents SDK for the voice loop
status: To Do
assignee: []
created_date: '2026-09-26 12:46'
labels:
  - phase-1
  - voice-loop
milestone: m-0
dependencies: []
references:
  - spec-v1-draft.md#5-components
priority: high
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
WebRTC voice loop: STT, VAD, turn-taking, barge-in, streaming TTS (Flash v2.5). Owns everything that isn't the Delegator seam.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A live WebRTC session round-trips through the Delegator
- [ ] #2 Barge-in interrupts TTS playback correctly
<!-- AC:END -->
