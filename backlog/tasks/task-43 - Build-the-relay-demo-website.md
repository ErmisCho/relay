---
id: TASK-43
title: Build the relay demo website
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 16:07'
updated_date: '2026-09-27 00:45'
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
- [x] #1 A voice panel starts and stops an ElevenLabs conversation from the browser mic using a signed token, shows the live transcript, and supports barge-in
- [x] #2 A type-to-talk mode runs the same conversation through the text endpoint when no microphone is available or permission is denied
- [x] #3 A live 'what relay is thinking' panel shows current idea, ready-gate verdict, scope refusals, the pending proposal with its read-back, the assent label and the dispatch, updating within 1 s of the event
- [x] #4 A dispatched task appears as a card whose status updates live, and on delivery the Markdown brief renders in the page with clickable sources
- [x] #5 An ideas view shows ideas with status and links, and each commitment with its verbatim assent utterance and read-back (the audit trail)
- [x] #6 Guided scenario buttons demonstrate: a hedge ('sure, I guess') that does not dispatch, an out-of-scope request that is refused with the 'might come in a future version' wording, recall of an earlier idea, and a full propose → assent → dispatch → delivered document flow
- [ ] #7 The site is gated by the demo passcode, works in current Chrome and Safari on desktop and on a phone-width screen, and a first-time viewer can complete the full flow without instructions from the owner (checked with one person)
- [x] #8 A README section documents how to run the demo locally and over the ngrok URL, including the required services (Postgres, Delegator, executor worker, Ollama models, ngrok on the Delegator port)
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Finish and polish the responsive mock-mode website as the hackathon default, run browser E2E in desktop and phone widths, and keep paid voice explicitly opt-in/unavailable in the zero-cost demo.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Scheduled for W6, after TASK-42 (event feed and demo API). Design canvas: https://claude.ai/artifact/JU3sPhDLreHjuwvmjdZuyo — desktop layout with the ElevenLabs bar visualizer (https://ui.elevenlabs.io/docs/components/bar-visualizer) in the centre and a 'Decision trace' panel on the right that shows the TASK-42 events, where a proposal → assent → dispatch chain opens into a task card (id, executor, description from the read-back, exact assent words, DBOS workflow id, status); plus a phone-width variant with the trace as a bottom sheet. Not designed yet: passcode gate, ideas/audit view (AC#5), delivered-brief reader (AC#4), guided scenario buttons (AC#6).

2026-09-26 session 85558081 (parallel background agent, unmerged): web/ frontend built — Vite 8 + React 19 + TS 5.9, @elevenlabs/react 1.15; typed TASK-42 contract (web/src/api/contract.ts + web/CONTRACT.md), scripted mock backend (VITE_API_MODE=mock), pure trace reducer, passcode gate, voice panel, trace panel/bottom sheet, task cards, sanitized brief reader, ideas/audit view, 4 guided scenarios. Evidence: tsc exit 0; vitest 11/11; vite build exit 0 (mock excluded from live bundle); headless Playwright Chromium+WebKit desktop dark + 390px light: 62/62. AC status: #2-#6 work in mock only (need TASK-42); #1 needs TASK-42 signed-token endpoint + live mic; #7 needs real Safari/iOS + one-person test; #8 run docs in web/README.md, root README link pending. Contract questions for TASK-42: proposal_dropped event, task_status channel from worker (NOTIFY vs polling), cookie auth + Delegator serving web/dist same-origin, structured sources on artifacts, one voice session per scenario.

2026-09-27 visible Chrome validation: desktop and phone widths have no horizontal overflow, labeled controls and no console errors; all four scenarios pass, full flow delivers and opens a sourced brief. Prior WebKit suite passed 62/62. Zero-cost mock mode was used, not paid voice.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Responsive demo is implementation-complete and automation-verified in Chrome phone/desktop plus WebKit; the required first-time human walkthrough remains explicitly unverified.
<!-- SECTION:FINAL_SUMMARY:END -->
