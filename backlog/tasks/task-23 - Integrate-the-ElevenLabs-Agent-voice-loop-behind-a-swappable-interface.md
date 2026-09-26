---
id: TASK-23
title: Integrate the ElevenLabs Agent voice loop behind a swappable interface
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
labels:
  - phase-1
  - voice
  - client
milestone: m-0
dependencies:
  - TASK-22
references:
  - SPEC.md#5-components
  - SPEC.md#9-cost-model
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
ElevenLabs Agents owns STT, VAD, turn-taking, barge-in and streaming TTS — the hardest part of the stack. Turn-taking must never move to Laya (measured 50% = chance; it lives in prosody). Because agent minutes are the dominant cost ($0.08/min, SPEC §9), the voice layer must sit behind an interface so a self-hosted STT/TTS pipeline can replace it later.

**Technical details**
- ElevenLabs Agent config (checked in as JSON/YAML and applied via API): LLM = Custom LLM pointing at the Delegator URL + secret; TTS model Flash v2.5; system prompt carrying the scope rules and commitment-protocol instructions; `end_call` system tool enabled; server-side silence end-call timeout set.
- Client: `relay/client/voice.py` defines `VoiceSession` protocol (`start(session_id)`, `stop()`, `on_user_transcript`, `on_agent_response`, `on_end`). `ElevenLabsVoiceSession` implements it with the ElevenLabs Agents SDK (WebRTC connection where supported), passing `session_id` as a dynamic variable so the Delegator can key turns.
- Owns the microphone while active; releases it on end so the wake-word listener can resume.
- Emits `last_activity_ts` on every transcript/response event for the auto-close watchdog.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Starting a `VoiceSession` connects to the configured Agent and a spoken exchange round-trips through the Delegator
- [ ] #2 Barge-in interrupts agent speech mid-sentence
- [ ] #3 The `session_id` passed by the client appears on the corresponding `sessions`/`turns` rows
- [ ] #4 No module outside `ElevenLabsVoiceSession` imports the ElevenLabs SDK
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Agent configuration is versioned in the repo, not only in the ElevenLabs dashboard
<!-- DOD:END -->
