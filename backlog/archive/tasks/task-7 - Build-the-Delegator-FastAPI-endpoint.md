---
id: TASK-7
title: Build the Delegator FastAPI endpoint
status: To Do
assignee: []
created_date: '2026-09-26 12:46'
labels:
  - phase-1
  - delegator
milestone: m-0
dependencies: []
references:
  - spec-v1-draft.md#4-architecture
priority: high
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
OpenAI-compatible endpoint (/v1/chat/completions or /v1/responses), SSE streaming (text/event-stream, data: {json}\n\n chunks, terminated by data: [DONE]\n\n), and OpenAI-format function calling. This is the seam ElevenLabs' custom-LLM hook calls into — frontier model only in Phase 1, no Laya yet.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Endpoint responds to ElevenLabs' custom-LLM hook shape
- [ ] #2 SSE streaming verified end-to-end with a real ElevenLabs Agent session
<!-- AC:END -->
