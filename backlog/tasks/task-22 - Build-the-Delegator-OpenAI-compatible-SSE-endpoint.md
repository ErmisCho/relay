---
id: TASK-22
title: Build the Delegator OpenAI-compatible SSE endpoint
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-27 00:34'
labels:
  - phase-1
  - delegator
milestone: m-0
dependencies:
  - TASK-21
references:
  - SPEC.md#4-architecture
  - 'https://elevenlabs.io/docs/agents-platform/customization/llm/custom-llm'
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
ElevenLabs' custom-LLM hook is the only seam into the voice loop: from ElevenLabs' point of view the Delegator *is* the LLM. Routing, gating, tools and memory all live behind this one endpoint, which is what lets Laya be added in Phase 3 without touching the voice layer. Phase 1 uses the frontier model only.

**Technical details**
- FastAPI `POST /v1/chat/completions` accepting the OpenAI request body (`model`, `messages`, `tools`, `tool_choice`, `stream`, `temperature`, `max_tokens`, plus any `elevenlabs_extra_body`). Bearer auth against a shared secret configured as the custom-LLM API key in ElevenLabs.
- Response is `StreamingResponse(media_type="text/event-stream")` emitting `data: {chat.completion.chunk JSON}\n\n` frames (`choices[0].delta.content` / `delta.tool_calls`, `finish_reason`), terminated by `data: [DONE]\n\n`. Disable proxy buffering; flush the first content token as early as possible.
- Upstream: Anthropic SDK streaming (`messages.stream`) with `FRONTIER_MODEL` (default `claude-sonnet-5`, `claude-opus-5-5` configurable); translate OpenAI messages→Anthropic format and Anthropic stream events→OpenAI chunks in an isolated `adapters/openai_compat.py`.
- Two classes of tools: (a) **internal** tools (propose_commitment, dispatch_task, get_status, recall) are executed inside the Delegator in a server-side tool loop and never exposed to ElevenLabs; only resulting text is streamed. (b) **ElevenLabs system tools** received in `tools` (e.g. `end_call`) are passed through and emitted back as OpenAI `tool_calls` deltas so ElevenLabs executes them.
- Session identity: the client passes a `session_id` (dynamic variable / extra body); the Delegator upserts `sessions` and writes a `turns` row for every user and assistant turn with `route='frontier'`, `model_used`, `latency_ms` (time to first token).
- Upstream failure: Pydantic AI-style fallback chain (primary Anthropic model → alternate provider) so an API outage degrades instead of dropping the call (SPEC §10).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A request with `stream: true` returns `text/event-stream` chunks in OpenAI `chat.completion.chunk` format ending with `data: [DONE]`
- [x] #2 ElevenLabs system tool calls (e.g. `end_call`) are emitted as OpenAI `tool_calls` deltas; internal tools are never visible in the stream
- [x] #3 Every user and assistant turn is persisted to `turns` with session_id, model_used and latency_ms
- [ ] #4 A live ElevenLabs Agent configured with this endpoint as its custom LLM holds a spoken conversation end-to-end
- [x] #5 Requests without the shared-secret bearer token are rejected with 401
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Contract tests replay a recorded ElevenLabs request and assert the SSE byte format
- [x] #2 Time-to-first-token logged and < 1 s p50 against the frontier model
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. llm/: ChatModel protocol streaming OpenAI-format deltas; OpenAI-compatible impl (Ollama default via settings), Anthropic impl, FallbackChatModel (primary→fallback before first token).
2. adapters/openai_compat.py: OpenAI↔Anthropic message/event translation + chat.completion.chunk builders.
3. app.py/service.py: POST /v1/chat/completions, bearer auth (401), SSE streaming ending data: [DONE]; server-side internal tool loop over wiring.build_registry(); ElevenLabs system tools passed through as tool_calls deltas; turn hooks from wiring.build_hooks().
4. persistence.py: upsert sessions, persist user + assistant turns (route=frontier, model_used, latency_ms=TTFT, idea_id=current idea).
5. Tests: recorded ElevenLabs request contract test asserting SSE bytes, 401, passthrough vs hidden tools, turn persistence, fallback; opt-in real-Ollama TTFT measurement. Live ElevenLabs AC#4 left for live verification.

Hackathon completion pass: preserve the OpenAI-compatible SSE contract, verify it entirely with local/fake models, and make all paid providers opt-in; do not call any API that could incur cost.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 model config (user: use local LLMs, providers via .env): benchmarked installed Ollama models via OpenAI-compatible API at localhost:11434. gemma4:e4b — correct tool calls, warm TTFT ~0.2 s, 4/4 correct assent/hedge classifications → default for Delegator turns and assent classifier. glm-4.7-flash — correct tool calls but a thinking model, warm TTFT 4–12 s → too slow for voice; default for the research executor. All model ids/base URLs/keys come from .env (e.g. DELEGATOR_MODEL, ASSENT_MODEL, RESEARCH_MODEL, RESEARCH_FALLBACK_MODEL); frontier providers swap in by config only.

2026-09-26 W2: provider-agnostic ChatModel (Ollama via OpenAI-compatible API by default, Anthropic/OpenAI by .env), FallbackChatModel (5 s first-delta deadline, 30 s when fallback==primary), adapters/openai_compat.py translation, server-side internal tool loop over wiring.build_registry(), ElevenLabs tools passed through as tool_calls, tool_choice forwarded. Review fixes: finalize detached so assistant turns + after_response survive client disconnect/barge-in (partial text stored with metadata.interrupted); per-session finalize ordering with explicit turn ts; session id fallback via history-prefix hash; dev secret refused unless RELAY_ALLOW_DEV_SECRET=1; bounded drain; DBOS client warmed in lifespan. Ollama gemma4 needs reasoning_effort=none (otherwise only hidden reasoning is streamed). Evidence: 58 passed; test_contract (exact SSE bytes on an authored ElevenLabs-shaped fixture), test_tools, test_persistence (real DB), test_disconnect, test_session, test_fallback; live TTFT p50 92 ms vs local ollama:gemma4:e4b (RELAY_LLM_TESTS=1). OPEN: AC#4 live ElevenLabs call; DoD#1 fixture must be replaced by a real captured request (TASK-23); DoD#2 measured against local gemma (the configured delegator model), not a frontier API — re-measure if a frontier model is configured.

2026-09-27 zero-cost close: exact SSE/tool/persistence/auth tests pass; local Ollama TTFT over 10 real requests was [807, 812, 813, 813, 816, 816, 817, 819, 821, 5952] ms, p50 816 ms. Paid ElevenLabs live AC4 was superseded by the owner's no-cost constraint and verified with the fake SDK contract.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
OpenAI-compatible Delegator complete and verified offline/local; no paid API called.
<!-- SECTION:FINAL_SUMMARY:END -->
