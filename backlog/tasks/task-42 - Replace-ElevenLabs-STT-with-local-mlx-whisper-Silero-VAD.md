---
id: TASK-42
title: Replace ElevenLabs STT with local mlx-whisper + Silero VAD
status: To Do
assignee: []
created_date: '2026-09-26 17:47'
labels:
  - phase-1
  - client
  - voice
  - local-stt
milestone: m-0
dependencies:
  - TASK-23
references:
  - docs/spec-v1-draft.md#9-cost-model
priority: high
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Local STT so voice understanding runs on-device (M4 Pro) instead of round-tripping to ElevenLabs' cloud STT. Implemented behind the same swappable voice-loop interface TASK-23 defines, so the Delegator/SSE contract is unchanged.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Local transcription runs via mlx-whisper with no network call for STT
- [ ] #2 Silero VAD drives turn-taking and barge-in locally
- [ ] #3 Measured latency is at or below ElevenLabs' sub-300ms baseline
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) mlx-whisper distil-large-v3 for streaming transcription (MLX/Metal-accelerated, real-time on M4 Pro)  2) Silero VAD for turn-taking/endpointing, replacing ElevenLabs' bundled VAD  3) reimplement barge-in detection locally (VAD interrupt signal cancels in-flight TTS playback)  4) wire into the same client-side interface TASK-23 exposes, so swapping back to ElevenLabs later is a config change, not a rewrite  5) latency test: measure end-of-utterance to first transcript token, target sub-300ms to match ElevenLabs' own baseline
<!-- SECTION:PLAN:END -->
