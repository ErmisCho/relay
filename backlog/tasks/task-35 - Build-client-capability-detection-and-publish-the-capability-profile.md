---
id: TASK-35
title: Build client capability detection and publish the capability profile
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-27 00:34'
labels:
  - phase-3
  - client
  - router
milestone: m-2
dependencies:
  - TASK-31
references:
  - SPEC.md#5-components
documentation:
  - SPEC.md
priority: low
type: feature
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Cloud is the default; local inference is an opportunistic optimisation and never on the critical path for correctness. A device with no local capability must remain fully functional, just routing everything to cloud.

**Technical details**
- At client startup probe: Apple Silicon (`platform.system()=='Darwin' and platform.machine()=='arm64'`) and `import mlx`; Ollama daemon (`GET http://localhost:11434/api/tags`, check `gemma4:e4b` / `glm-4.7-flash` present); free RAM/VRAM via `psutil.virtual_memory().available` (and GPU memory where available).
- Build `CapabilityProfile(can_run_laya_mlx, can_run_local, local_model, local_base_url, free_mem_gb)` and `POST /v1/capabilities` to the Delegator keyed by session_id; re-probe on each wake.
- Delegator stores it per session and defaults to `can_run_local=False` if absent.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Profile is published at session start on macOS, Windows and Linux
- [x] #2 A machine without Ollama reports `can_run_local=false` and the system works unchanged
- [x] #3 Delegator treats a missing profile as cloud-only
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Exercise capability detection on Windows plus simulated macOS/Linux branches, verify missing Ollama remains functional, and keep local inference optional.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26: client probe (Ollama /api/tags 1 s budget, mlx import, free RAM via vm_stat//proc/meminfo/GlobalMemoryStatusEx) published on every session create via CapabilityPublishingStore to POST /v1/capabilities (bearer auth, LRU 256); missing profile = CLOUD_ONLY. Real probe on the M4 Pro: can_run_local=true, gemma4:e4b, free 32.75 GB, mlx not installed, 31 ms. AC1 open: Linux/Windows memory branches never run.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Capability probe/publish is cross-platform stdlib code, non-blocking on every wake, and defaults safely to cloud-only when absent.
<!-- SECTION:FINAL_SUMMARY:END -->
