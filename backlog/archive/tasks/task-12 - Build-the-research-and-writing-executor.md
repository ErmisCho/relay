---
id: TASK-12
title: Build the research and writing executor
status: To Do
assignee: []
created_date: '2026-09-26 12:47'
labels:
  - phase-1
  - executor
milestone: m-0
dependencies: []
references:
  - spec-v1-draft.md#3-scope
priority: high
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Pydantic AI agent, one tool only: web research, synthesis, briefs, drafts, competitive analysis. Terminal artifact is a Markdown document, never sent or published. This is the only vertical in Phase 1 - code executor is Phase 2.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 dispatch_task writes a commitment row plus a workflow handle and returns immediately
- [ ] #2 Completed artifact link lands in the idea graph regardless of whether it was spoken
<!-- AC:END -->
