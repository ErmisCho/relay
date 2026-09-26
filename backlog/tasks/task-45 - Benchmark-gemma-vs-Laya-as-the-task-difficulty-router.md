---
id: TASK-45
title: Benchmark gemma vs Laya as the task-difficulty router
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 17:38'
updated_date: '2026-09-26 17:54'
labels:
  - phase-1
  - router
  - spike
milestone: m-0
dependencies: []
references:
  - SPEC.md
priority: high
type: spike
ordinal: 22000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Owner decision (2026-09-26): delegated tasks are routed by difficulty — easy to local gemma4:e4b, hard to gpt-6-luna. The router must be chosen by measurement: run gemma4:e4b (zero-shot structured choice) and Laya (laya-mlx on Apple Silicon, binary choice questions only, never score/confidence thresholds — see SPEC §2) side by side as an INDEPENDENT script on the same labelled set, and pick the better one on accuracy and latency. Laya is not installed yet. This is the per-task counterpart of the Phase 3 router tasks (TASK-36/41), done now at the owner's request.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A standalone script (runnable without the Delegator) evaluates both routers on the same checked-in labelled set of at least 40 task descriptions (commitment goal + exclusions) labelled easy/hard, with the labelling criteria written down
- [x] #2 The script reports per router: accuracy, precision/recall on 'hard', confusion matrix, and p50/p95 latency on this Mac, with warm and cold runs distinguished
- [x] #3 Laya is installed and runs locally, using only binary choice questions (no score head, no confidence thresholds), or the reason it cannot is documented
- [x] #4 The result and the chosen router are recorded as a Backlog decision with the numbers; misrouting a hard task to the local model is treated as the costlier error
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Standalone benchmarks/router/: labelled set (≥40 goals+exclusions, easy/hard, written criteria), gemma4 zero-shot structured choice router, Laya (laya-mlx via uv run --with / isolated env, binary choice questions only), eval script with accuracy/precision/recall on hard/confusion/p50-p95 latency warm vs cold; record decision.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26: benchmarks/router/ — 60 labelled tasks (29 easy/31 hard, 14 borderline, labels by the agent: optimistic), shared question wording, 3 reps, stable predictions. gemma4:e4b v2: acc 100%, hard recall 100% (0 hard→local), warm p50/p95 557/591 ms, cold ~2.1 s. gemma v1: 98.3%, 1 hard→local, 496/508 ms. Laya (laya-mlx 0.2.0, aac6fef/laya-mlx, binary choice only, no score/confidence; run via uv run --no-project --with laya-mlx==0.2.0): 91.7%, hard recall 87.1% (4 hard→local on easy-worded/hard-content tasks), borderline hard recall 50%, warm p50 29–31 ms. Decision-5: gemma v2. Follow-ups: re-run on ~50 real dispatched tasks; TASK-46 router timeout budget (warm ~0.5 s, cold 2–5 s).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Measured gemma4:e4b vs Laya as the easy/hard task router on a 60-item labelled set: gemma (prompt v2) had perfect hard recall at ~0.56 s warm, Laya was 17x faster but misrouted 4/31 hard tasks. Chose gemma v2 (decision-5).
<!-- SECTION:FINAL_SUMMARY:END -->
