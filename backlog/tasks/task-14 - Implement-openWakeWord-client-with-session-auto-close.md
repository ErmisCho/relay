---
id: TASK-14
title: Implement openWakeWord client with session auto-close
status: To Do
assignee: []
created_date: '2026-09-26 13:18'
labels:
  - phase-1
  - client
  - cost
milestone: m-0
dependencies:
  - TASK-12
references:
  - docs/spec-v1-draft.md#9-cost-model
priority: high
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Stock pretrained openWakeWord model opens a session; sessions auto-close on silence since ElevenLabs bills about 0.08 USD/min wall-clock (a forgotten open session bills about 4.80 USD/hour). Decision: stock model, not a custom-trained phrase.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Wake phrase opens a session with no API key/metering
- [ ] #2 Session auto-closes after a defined silence window
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) client/src/relay_client/wake_word.py: load openWakeWord's stock pretrained model, listen loop  2) session_manager.py: silence-timeout auto-close wired to the WebRTC session lifecycle from the ElevenLabs task  3) test: simulated silence window triggers close; wake phrase opens a session with no API key/metering
<!-- SECTION:PLAN:END -->
