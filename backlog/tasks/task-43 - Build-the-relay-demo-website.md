---
id: TASK-43
title: Build the relay demo website
status: To Do
assignee: []
created_date: '2026-09-26 16:07'
labels:
  - phase-1
  - demo
  - web
milestone: m-0
dependencies:
  - TASK-42
references:
  - docs/elevenlabs-localhost-connectivity.md
  - 'https://github.com/elevenlabs/elevenlabs-js/issues/320'
priority: medium
type: feature
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The owner wants a website to demo relay to other people. A raw voice call hides what makes relay different: talk until we agree, a spoken read-back, an explicit yes, background work, and a reviewable artifact coming back, with hard safety rails (out-of-scope refusal, hedges never dispatch). The site makes those invisible steps visible next to the conversation, works without a microphone, and walks a viewer through the key moments. It consumes the event feed and demo API from TASK-42 and is served over the owner's ngrok URL (https://geology-hardiness-cage.ngrok-free.dev), so it must be usable by someone who has never seen the project. ElevenLabs browser SDK sessions must use the server-issued signed token (the agent is private), which also avoids the localhost allowlist problem in elevenlabs-js issue #320.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 A voice panel starts and stops an ElevenLabs conversation from the browser mic using a signed token, shows the live transcript, and supports barge-in
- [ ] #2 A type-to-talk mode runs the same conversation through the text endpoint when no microphone is available or permission is denied
- [ ] #3 A live 'what relay is thinking' panel shows current idea, ready-gate verdict, scope refusals, the pending proposal with its read-back, the assent label and the dispatch, updating within 1 s of the event
- [ ] #4 A dispatched task appears as a card whose status updates live, and on delivery the Markdown brief renders in the page with clickable sources
- [ ] #5 An ideas view shows ideas with status and links, and each commitment with its verbatim assent utterance and read-back (the audit trail)
- [ ] #6 Guided scenario buttons demonstrate: a hedge ('sure, I guess') that does not dispatch, an out-of-scope request that is refused with the 'might come in a future version' wording, recall of an earlier idea, and a full propose → assent → dispatch → delivered document flow
- [ ] #7 The site is gated by the demo passcode, works in current Chrome and Safari on desktop and on a phone-width screen, and a first-time viewer can complete the full flow without instructions from the owner (checked with one person)
- [ ] #8 A README section documents how to run the demo locally and over the ngrok URL, including the required services (Postgres, Delegator, executor worker, Ollama models, ngrok on the Delegator port)
<!-- AC:END -->
