---
id: TASK-24
title: Implement the openWakeWord listener with silence auto-close
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
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
- [ ] #2 A session with no speech for SILENCE_TIMEOUT closes automatically and `sessions.ended_at` is set
- [ ] #3 After a session closes the listener resumes and can open a new session
- [ ] #4 The wake listener does not hold the microphone while a voice session is active
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 False-trigger rate measured over 1 h of background audio and recorded in task notes
<!-- DOD:END -->
