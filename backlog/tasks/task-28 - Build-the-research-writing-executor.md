---
id: TASK-28
title: Build the research & writing executor
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 16:19'
labels:
  - phase-1
  - executor
  - research
milestone: m-0
dependencies:
  - TASK-26
references:
  - SPEC.md#3-scope
  - SPEC.md#5-components
  - 'https://ai.pydantic.dev/durable_execution/dbos/'
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Phase 1 ships exactly one executor vertical: research & writing. Its terminal artifact is a Markdown document that is never sent or published — the user reviews it.

**Technical details**
- Pydantic AI `Agent` wrapped for durability with `DBOSAgent` (`pydantic_ai.durable_exec.dbos`) so each model/tool call is a DBOS step and resumes after a crash.
- Model: `FallbackModel(AnthropicModel(FRONTIER_MODEL), <alternate provider>)` to survive a frontier outage.
- Tools: web search (provider built-in `WebSearchTool` or a search API) and `fetch_url` (httpx + readability extraction, size/timeout limits). No tools that send, post or publish.
- Structured output `ResearchBrief(title: str, summary: str, body_markdown: str, sources: list[HttpUrl])`; the workflow renders it to `artifacts/<idea_id>/<task_id>.md` with a sources section and records `artifacts(kind='document', url, summary)`.
- Prompt receives the commitment's `goal` and `scope_excludes` verbatim; the agent must stay inside the exclusions.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Dispatching a research commitment produces a Markdown file with title, body and cited source URLs
- [x] #2 An `artifacts` row of kind `document` links the file to its task and the idea status becomes `delivered`
- [x] #3 Scope exclusions from the commitment are respected in the output
- [x] #4 Primary model failure falls back to the alternate provider without failing the task
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Pydantic AI research agent (DBOSAgent durable), FallbackModel research_model→research_fallback_model via Ollama, tools: DuckDuckGo search + fetch_url (trafilatura, limits), ResearchBrief output rendered to artifacts/<idea>/<task>.md with sources; register research runner; tests with pydantic-ai TestModel/FunctionModel + fallback + scope exclusions.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 model config (user: use local LLMs, providers via .env): benchmarked installed Ollama models via OpenAI-compatible API at localhost:11434. gemma4:e4b — correct tool calls, warm TTFT ~0.2 s, 4/4 correct assent/hedge classifications → default for Delegator turns and assent classifier. glm-4.7-flash — correct tool calls but a thinking model, warm TTFT 4–12 s → too slow for voice; default for the research executor. All model ids/base URLs/keys come from .env (e.g. DELEGATOR_MODEL, ASSENT_MODEL, RESEARCH_MODEL, RESEARCH_FALLBACK_MODEL); frontier providers swap in by config only.

2026-09-26 owner: ~6 minutes per research brief on local qwen3.8 is acceptable (async, voice not blocked). No tuning of REQUEST_LIMIT/SEARCH_MAX_RESULTS needed.

2026-09-26 W3: research runner = DBOS child workflow running a pydantic-ai agent with DBOSDurability (DBOSAgent is deprecated in 2.51), FallbackModel ollama:qwen3.8 → gemma4 (120 s read timeout, no SDK retries), DuckDuckGo + fetch_url as DBOS steps (SSRF-safe: every hop resolved and must be global, max 5 redirects), 20-min workflow timeout, served-model log line. Evidence: e2e through the real worker with stub models (file, artifact row, idea delivered, exclusions in prompt, fallback, timeout), live run 212 s, 3 sources, exclusion respected, served by qwen,qwen,gemma. Residual: DNS rebinding (documented).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Durable research & writing executor producing a sourced Markdown brief per research commitment, with local-model fallback, safe fetch tooling and a workflow timeout. Verified end-to-end via the executor worker and one live local-model run.
<!-- SECTION:FINAL_SUMMARY:END -->
