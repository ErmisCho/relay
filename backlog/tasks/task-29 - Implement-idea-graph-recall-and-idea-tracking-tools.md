---
id: TASK-29
title: Implement idea-graph recall and idea tracking tools
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
labels:
  - phase-1
  - delegator
  - data
milestone: m-0
dependencies:
  - TASK-22
references:
  - SPEC.md#7-data-model-idea-graph
documentation:
  - SPEC.md
priority: medium
type: feature
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Ideas are durable and revisited for weeks; the product must answer questions like "where did we land on the routing thing?". Without idea tracking, turns cannot be attributed and commitments cannot be linked to the right idea.

**Technical details**
- Internal Delegator tools: `recall(query)` — full-text search over `ideas` (GIN tsvector) returning top ideas with latest summary, commitments, task statuses, artifact links and `idea_edges`; `focus_idea(idea_id | new_title)` — sets the session's current idea (creating one with `status='exploring'` if new) so subsequent `turns.idea_id` are set; `link_ideas(from, to, relation)` for refines/supersedes/blocks/spun_off_from.
- Periodically (e.g. every N turns or on idea switch) regenerate `ideas.summary` with a cheap frontier call from recent turns.
- Recall results are compact text (≤ ~1k tokens) injected as tool results so spoken answers stay short.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Asking about a previously discussed idea in a new session returns its summary, commitments and artifact links
- [ ] #2 Turns are attributed to the current idea via `turns.idea_id`
- [ ] #3 Idea edges can be created and are included in recall results
<!-- AC:END -->
