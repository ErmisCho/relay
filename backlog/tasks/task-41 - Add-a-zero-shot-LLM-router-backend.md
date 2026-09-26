---
id: TASK-41
title: Add a zero-shot LLM router backend
status: In Progress
assignee: []
created_date: '2026-09-26 13:14'
updated_date: '2026-09-26 19:29'
labels:
  - phase-3
  - router
  - delegator
milestone: m-2
dependencies:
  - TASK-35
  - TASK-36
references:
  - SPEC.md#2-laya--jev-evaluation--measured-not-estimated
  - SPEC.md#5-components
documentation:
  - SPEC.md
priority: low
type: feature
ordinal: 16500
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Laya is fast and free but uncalibrated and can't be improved without labels. A general-purpose LLM prompted zero-shot with the same questions may make better routing and ready decisions, because it reads conversational context and needs no model-specific tooling. The trade-off is latency (hundreds of ms rather than 7–32 ms) and per-call cost when it runs in the cloud. It runs next to Laya in shadow mode so the two can be compared on the same turns.

**Technical details**
- `LLMRouter` implements `Router`. It makes one call per turn returning structured JSON `{"difficulty": …, "ready": …, "intent": …}` validated into `RouterDecision`. Use a Pydantic AI `Agent(output_type=RouterDecisionOut)` for cloud models, or Ollama `/api/chat` with a `format` JSON schema for local ones.
- Prompt is generated from the shared `questions.py` (instructions + descriptive criteria per option) so it asks exactly what Laya is asked; includes the last N turns (bounded, e.g. ≤1k tokens) as context. `temperature=0`.
- Model config `ROUTER_LLM_MODEL`: a local Ollama model (`gemma4:e4b` / `glm-4.7-flash`) when the capability profile allows it, otherwise a small cloud model (e.g. `claude-haiku-4-5`). Never the frontier model itself, which would defeat the purpose.
- Hard timeout budget `ROUTER_LLM_TIMEOUT_MS`; on timeout, invalid JSON or error returns `None`, which callers treat as `frontier`. `confidence=None`; logprobs are not used as thresholds either.
- Logs to `router_decisions(backend='llm')` with latency and token cost.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 `LLMRouter` implements `Router` and uses the same question definitions as `LayaRouter`
- [ ] #2 Works with both a local Ollama model and a cloud small model, selected by configuration
- [x] #3 Timeouts, errors and malformed output return no decision and the turn goes to the frontier model
- [ ] #4 Decisions, latency and per-call cost are logged to `router_decisions` with backend `llm` in shadow mode
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26: LLMRouter shares questions.py with Laya; ollama:* → /api/chat JSON-schema, think=false, temp 0 (live gemma4 515–1200 ms warm); openai:/anthropic: → pydantic-ai structured output (TestModel only, no live cloud call). Timeout/HTTP 500/non-JSON/unknown label/missing question → None within budget. Open: AC2 live cloud model; AC4 tokens/cost only in logs (no router_decisions columns).
<!-- SECTION:NOTES:END -->
