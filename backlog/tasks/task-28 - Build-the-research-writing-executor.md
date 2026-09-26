---
id: TASK-28
title: Build the research & writing executor
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 13:39'
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
- [ ] #1 Dispatching a research commitment produces a Markdown file with title, body and cited source URLs
- [ ] #2 An `artifacts` row of kind `document` links the file to its task and the idea status becomes `delivered`
- [ ] #3 Scope exclusions from the commitment are respected in the output
- [ ] #4 Primary model failure falls back to the alternate provider without failing the task
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 model config (user: use local LLMs, providers via .env): benchmarked installed Ollama models via OpenAI-compatible API at localhost:11434. gemma4:e4b — correct tool calls, warm TTFT ~0.2 s, 4/4 correct assent/hedge classifications → default for Delegator turns and assent classifier. glm-4.7-flash — correct tool calls but a thinking model, warm TTFT 4–12 s → too slow for voice; default for the research executor. All model ids/base URLs/keys come from .env (e.g. DELEGATOR_MODEL, ASSENT_MODEL, RESEARCH_MODEL, RESEARCH_FALLBACK_MODEL); frontier providers swap in by config only.
<!-- SECTION:NOTES:END -->
