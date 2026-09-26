---
id: TASK-37
title: Route small_local turns to Ollama using the active router backend
status: In Progress
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 19:49'
labels:
  - phase-3
  - router
  - delegator
milestone: m-2
dependencies:
  - TASK-35
  - TASK-36
  - TASK-41
references:
  - SPEC.md#5-components
  - SPEC.md#8-phasing
documentation:
  - SPEC.md
priority: low
type: feature
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Once the shadow comparison has picked a backend (zero-shot Laya or the LLM router), it is activated to choose the model per turn and cut token spend. This reduces token cost only, not ElevenLabs agent minutes (SPEC §9).

**Technical details**
- With `ROUTER_ACTIVE=laya|llm`: `decision = routers[ROUTER_ACTIVE].decide(utterance, context)` on the request path (budgeted), logged with `is_active=true`. If `decision and decision.difficulty=='small_local' and caps.can_run_local`, stream from Ollama's OpenAI-compatible endpoint (`{local_base_url}/v1/chat/completions`, `caps.local_model`); otherwise `FRONTIER_MODEL`. A `None` decision always means frontier.
- Non-active backends keep running in shadow for ongoing comparison.
- Commitment-protocol calls (ready scoring, read-back, assent classification) always use the frontier model in v1, whatever the router says.
- Fallback: if the local stream errors or exceeds a configurable first-token timeout, restart the turn on the frontier model transparently.
- Log `route`, `model_used`, `latency_ms` per turn; admin toggle to force frontier.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 With either backend active, turns routed `small_local` on a capable device are served by Ollama and logged with `model_used`
- [x] #2 Switching `ROUTER_ACTIVE` between `laya`, `llm` and `none` needs no code change
- [x] #3 Router failure, local failure or timeout falls back to the frontier model without the user hearing an error
- [x] #4 Assent classification is never served by the local model or the router
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 (commit after 751ca58): live with ROUTER_ACTIVE=laya: turn 1 Laya still loading → 151 ms timeout → frontier; turns 2-3 small_local served by local:gemma4:e4b (359/377 ms), Laya 127/140 ms. Tests test_active.py (11 cases). Gate 500 passed/15 skipped, mypy 0, ruff 0. Notes: ROUTER_ACTIVE switch needs a restart; frontier difficulty labels for TASK-36 AC5 still missing.
<!-- SECTION:NOTES:END -->
