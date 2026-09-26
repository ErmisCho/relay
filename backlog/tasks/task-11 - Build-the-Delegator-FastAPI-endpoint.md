---
id: TASK-11
title: Build the Delegator FastAPI endpoint
status: To Do
assignee: []
created_date: '2026-09-26 13:17'
labels:
  - phase-1
  - delegator
milestone: m-0
dependencies:
  - TASK-9
references:
  - docs/spec-v1-draft.md#4-architecture
priority: high
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
OpenAI-compatible endpoint (SSE, OpenAI-format function calling) - the seam ElevenLabs' custom-LLM hook calls into. Frontier model only, no local routing.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Endpoint responds to ElevenLabs custom-LLM hook shape
- [ ] #2 SSE streaming verified end-to-end with a real ElevenLabs Agent session
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) delegator/sse.py: data: {json} framing terminated by [DONE], disable gzip/buffering on the route, confirm uvicorn flushes per chunk  2) delegator/anthropic_backend.py: relay Claude's stream into OpenAI-shaped SSE events, accumulate streamed tool_calls[].function.arguments deltas across chunks before invoking the underlying tool  3) delegator/function_registry.py: OpenAI tool-schema to Python callable registry (commitment_protocol's tools plug into this later)  4) delegator/routes.py: POST /v1/chat/completions  5) integration test against a real ElevenLabs Agent session once the voice-loop task exists (manual checkpoint, not a hard Backlog dependency)
<!-- SECTION:PLAN:END -->
