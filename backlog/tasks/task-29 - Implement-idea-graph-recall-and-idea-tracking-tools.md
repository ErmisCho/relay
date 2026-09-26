---
id: TASK-29
title: Implement idea-graph recall and idea tracking tools
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 16:19'
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
- [x] #1 Asking about a previously discussed idea in a new session returns its summary, commitments and artifact links
- [x] #2 Turns are attributed to the current idea via `turns.idea_id`
- [x] #3 Idea edges can be created and are included in recall results
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
ideas_repo (FTS recall over ideas GIN index, focus/create, link edges, summary regeneration); internal tools recall/focus_idea/link_ideas; IdeasHook regenerating summaries every N turns / on switch; tests: cross-session recall with commitments+artifacts, turns.idea_id attribution, edges in recall, compact output.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 W3: ideas_repo (FTS using IDEAS_FTS_EXPR: all-terms → any-term → ILIKE), recall/focus_idea/link_ideas tools, IdeaSummaryHook (due every 6 user turns or on switch, runs only after 20 s conversational idle so it never competes with a live turn; drained on shutdown). Review fixes: focus_idea no-op on same idea / near-identical title and reuse by title (no duplicate ideas); spin-off titles stay separate; switching turn re-attributed. Latency (coordinator, real socket, full wiring, gemma4): TTFT 2.5 s first turn, ~0.32–0.38 s afterwards after moving static scope rules into a cached system prefix. Reuse-by-exact-title across sessions accepted under decision-2 (explicit naming).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Idea-graph recall and tracking: full-text recall with commitments/tasks/artifacts/edges, focus and link tools that attribute turns without duplicating ideas, and idle-time summary regeneration. Verified by DB-backed tests and live gemma runs (one idea row per conversation).
<!-- SECTION:FINAL_SUMMARY:END -->
