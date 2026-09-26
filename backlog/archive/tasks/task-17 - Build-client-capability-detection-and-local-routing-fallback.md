---
id: TASK-17
title: Build client capability detection and local routing fallback
status: To Do
assignee: []
created_date: '2026-09-26 12:48'
updated_date: '2026-09-26 13:14'
labels:
  - phase-3
  - routing
milestone: m-2
dependencies: []
references:
  - spec-v1-draft.md#5-components
priority: low
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
At startup the client probes for Apple Silicon + MLX, an Ollama daemon, and free VRAM/RAM, then publishes a capability profile. A device with no local capability must remain fully functional by routing everything to cloud - local inference is never on the critical path for correctness.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Capability probe runs at startup and is consulted by the Delegator route decision
- [ ] #2 A device with zero local capability still completes every turn via the frontier model
<!-- AC:END -->
