---
id: TASK-47
title: >-
  Turn the research executor into a general executor on the pydantic-ai-harness
  Coder and Researcher
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 18:34'
updated_date: '2026-09-27 00:34'
labels:
  - phase-1
  - executor
  - refactor
milestone: m-0
dependencies:
  - TASK-46
references:
  - 'https://pydantic.dev/docs/ai/harness/coder/'
  - 'https://pydantic.dev/docs/ai/harness/researcher/'
priority: high
type: feature
ordinal: 24000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Owner decision (2026-09-26): the research executor should also be able to write code, so it becomes one general "executor" instead of a research-only vertical. Rather than hand-rolling tools (today: DuckDuckGo search + fetch_url in executor/research/tools.py), it composes the pydantic-ai-harness capabilities: Researcher (web search with DuckDuckGo fallback, web fetch, sub-agents) and Coder (filesystem read/write/edit, ripgrep list/grep, shell, sub-agents, repo context). Confinement decision (owner): each new idea the executor works on gets its own project folder, independent of the relay/ repo; the Coder is rooted there. Coder shell is unrestricted and has no OS sandbox (harness docs), so the per-idea folder is the working boundary, not a security boundary. pydantic-ai-harness 0.36.0 requires pydantic-ai-slim>=2.44 (installed 2.51) and ships coder/researcher/dbos extras. DBOS durability (DBOSDurability), FallbackModel and the TASK-46 easy/hard model routing must keep working.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 The executor package is named executor (no research-only module or runner name remains in src/ or tests/); the persisted task kind values stay backward compatible with existing rows
- [x] #2 The executor agent is built with pydantic-ai-harness Researcher and Coder capabilities and still runs as a DBOS-durable workflow that resumes after a worker kill
- [x] #3 Each idea gets its own project folder under a configurable root outside the relay repo; the Coder file tools cannot read or write outside that folder, verified by test
- [x] #4 A research commitment still produces the sourced Markdown brief and document artifact (TASK-28 behaviour), verified by the existing e2e test with stub models
- [x] #5 TASK-46 routing still picks the model per task (easy gemma4, hard gpt-6-luna then qwen3.8), verified by the routing e2e test
- [x] #6 Harness dependency added via uv with the needed extras and the quality triad (pytest, mypy, ruff) passes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Session 85558081 (feature, 3 waves).
W1 Impl-Core: (A) executor package executor/research -> executor/agent on Researcher + Coder-equivalent capabilities, Shell with scrubbed env, sub-agents off for local models, DBOSDurability kept, TASK-46 routing kept, tests moved; (B) executor/workspace.py per-idea project folder under EXECUTOR_PROJECTS_ROOT (default ~/relay-projects) + tests.
W2 Impl-Polish+Quality: security review (shell env, confinement, SSRF via harness WebFetch), session review, docs (README, docs/architecture.md), fix pass.
W3 Finalization: full gate, commits, AC check.

Verify the general executor, per-idea workspace confinement, durable workflow, research/code behavior, and local model routing through the full offline quality gate.
<!-- SECTION:PLAN:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
General durable executor complete with harness Researcher/Coder capabilities, confined per-idea workspaces, safe cross-platform Git plumbing and preserved research behavior.
<!-- SECTION:FINAL_SUMMARY:END -->
