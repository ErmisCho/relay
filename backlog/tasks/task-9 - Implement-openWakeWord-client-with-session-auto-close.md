---
id: TASK-9
title: Implement openWakeWord client with session auto-close
status: To Do
assignee: []
created_date: '2026-09-26 12:46'
updated_date: '2026-09-26 12:46'
labels:
  - phase-1
  - client
  - cost
milestone: m-0
dependencies: []
references:
  - spec-v1-draft.md#9-cost-model
priority: high
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Local, CPU wake-word detection opens the session; sessions must auto-close on silence since ElevenLabs bills per wall-clock minute ($0.08/min, $0.16 burst) - a forgotten open session bills at $4.80/hour.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Wake phrase opens a session with no API key/metering
- [ ] #2 Session auto-closes after a defined silence window
<!-- AC:END -->
