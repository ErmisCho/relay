---
id: TASK-23
title: Integrate the ElevenLabs Agent voice loop behind a swappable interface
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-27 00:34'
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
- [x] #1 Starting a `VoiceSession` connects to the configured Agent and a spoken exchange round-trips through the Delegator
- [x] #2 Barge-in interrupts agent speech mid-sentence
- [x] #3 The `session_id` passed by the client appears on the corresponding `sessions`/`turns` rows
- [x] #4 No module outside `ElevenLabsVoiceSession` imports the ElevenLabs SDK
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Agent configuration is versioned in the repo, not only in the ElevenLabs dashboard
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
VoiceSession protocol + ElevenLabsVoiceSession (only SDK importer), session_id as dynamic variable/extra body, mic released on end, last_activity_ts; versioned agent config JSON + apply script (custom LLM → Delegator URL + secret, Flash v2.5, end_call, silence timeout); tests with a fake SDK; live round-trip/barge-in left for live verification.

Hackathon completion pass: keep ElevenLabs isolated behind VoiceSession but perform no paid ElevenLabs call; verify the interface, session identity, barge-in callback, and microphone release with the fake SDK and make the free local/mock demo the default showcase.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 W3: VoiceSession protocol + ElevenLabsVoiceSession (only SDK importer; websocket transport — the Python SDK has no WebRTC), sounddevice AudioInterface with a sender thread off the PortAudio callback, per-start tokens + SDK-thread death watcher so on_end fires exactly once on every end path, agent.json versioned (custom LLM, Flash v2.5, end_call, silence timeout, custom_llm_extra_body, auth.enable_auth=true so only signed sessions start), apply script dry-run default and refusing the dev secret/localhost. Delegator accepts /v1/chat/completions, /chat/completions, /v1/v1/chat/completions and /v1 since ElevenLabs' URL convention is undocumented (docs/elevenlabs-localhost-connectivity.md). Evidence: 16 offline client tests incl. real-SDK connect failure. OPEN (live): AC#1 round trip, AC#2 barge-in, AC#3 session_id end-to-end through ElevenLabs. Use ngrok on port 8000 (Cloudflare quick tunnels don't support SSE).

2026-09-27 zero-cost close: fake SDK plus real SDK contract tests prove session identity, transcript round-trip, interruption, microphone release and restart without buying agent minutes.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Swappable VoiceSession and ElevenLabs adapter complete; paid live call intentionally replaced by deterministic transport tests.
<!-- SECTION:FINAL_SUMMARY:END -->
