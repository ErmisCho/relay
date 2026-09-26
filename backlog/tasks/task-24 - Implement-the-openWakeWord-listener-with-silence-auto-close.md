---
id: TASK-24
title: Implement the openWakeWord listener with silence auto-close
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 18:51'
labels:
  - phase-1
  - client
  - voice
milestone: m-0
dependencies:
  - TASK-23
references:
  - SPEC.md#5-components
  - SPEC.md#9-cost-model
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Wake word is not native to ElevenLabs and must run client-side. The session model matters financially: a forgotten open session bills $4.80/hour, so sessions must auto-close on silence (SPEC §9).

**Technical details**
- `openwakeword.model.Model(wakeword_models=[WAKE_MODEL])` on CPU; read 16 kHz mono int16 audio in 1280-sample (80 ms) frames via `sounddevice`; trigger when `predict(frame)[name] >= WAKE_THRESHOLD` (default 0.5, configurable) with a debounce/refractory window.
- On trigger: create `session_id`, write `sessions(wake_trigger=<model name>)`, stop the listener, hand mic to `VoiceSession.start()`.
- Auto-close watchdog (asyncio task): if `now - last_activity_ts > SILENCE_TIMEOUT` (default 60 s) call `VoiceSession.stop()`, set `sessions.ended_at`, resume wake listening. Also honour a spoken "that's all" via the `end_call` tool. Belt and braces with the server-side ElevenLabs silence timeout.
- Must run on macOS, Windows and Linux; Porcupine can be swapped in behind the same `WakeWordDetector` interface if a custom phrase is chosen.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Saying the wake phrase opens a voice session within 500 ms of the phrase ending
- [x] #2 A session with no speech for SILENCE_TIMEOUT closes automatically and `sessions.ended_at` is set
- [x] #3 After a session closes the listener resumes and can open a new session
- [x] #4 The wake listener does not hold the microphone while a voice session is active
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 False-trigger rate measured over 1 h of background audio and recorded in task notes
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
WakeWordDetector interface + openWakeWord impl (pretrained hey_jarvis, threshold, debounce) on 16 kHz 80 ms frames; listener state machine: listen → trigger → sessions row (wake_trigger) → release mic → VoiceSession.start → silence watchdog (SILENCE_TIMEOUT_S) / end_call → sessions.ended_at → resume; python -m relay.client; false-trigger measurement script; tests with fake detector/audio/VoiceSession; live latency + 1 h false-trigger run left to owner.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 W4: WakeListener (openwakeword 0.4.0 hey_jarvis via wakeword_model_paths, 2 s refractory, silence-flush reset), mic hand-off before VoiceSession.start, watchdog, ended_at written first in a shielded shutdown (double SIGINT under uv run), stale-session reconciler, trigger→connected latency. Live: wake word works (sessions.wake_trigger=hey_jarvis); trigger→start() 146 ms, trigger→connected 1002 ms (signed-URL fetch + websocket; follow-up: prefetch the signed URL while listening). OPEN: AC#1 <500 ms to connected (live 1.0 s), DoD 1 h false-trigger run (owner).

2026-09-26 session 85558081 W1 (w1-c-3, unmerged until W3): SignedUrlCache prefetches the ElevenLabs signed URL via VoiceSession.prepare() when the listener enters LISTENING; start() takes it once if younger than SIGNED_URL_MAX_AGE_S=300 (refresh at 240 s; real TTL undocumented), else the SDK fetches on demand; prefetch failure logs once and never blocks listening. New log line "session <id>: signed URL prefetched|fetched on demand". Gates in worktree: tests/client 43 passed; full 415 passed/13 skipped; mypy 0; ruff 0. Known fragility: overrides private SDK Conversation._get_signed_url (elevenlabs 2.69) — W2 adds a contract test. OPEN AC#1: owner live run — expect "prefetched" and trigger -> connected < 500 ms, twice in a row.
<!-- SECTION:NOTES:END -->
