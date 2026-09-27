---
id: TASK-48
title: Add hardware-capability and live-weather internal tools
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 20:37'
updated_date: '2026-09-27 00:34'
labels:
  - phase-1
  - delegator
  - tools
milestone: m-0
dependencies: []
references:
  - docs/spec-v1-draft.md#3-scope
priority: medium
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Deliberate scope expansion (user decision, 2026-09-26) beyond SPEC.md's two verticals: two new internal tools alongside propose_commitment/dispatch_task/get_status/recall. (a) hardware_capabilities: reports the client's local hardware profile (chip, unified memory, GPU cores) and recommends which local LLMs fit, reusing the client capability-detection shape TASK-35 defines. (b) get_weather: live weather lookup via a free, keyless API (Open-Meteo) for a named location. Both answer directly in conversation; neither goes through the commitment protocol (no artifact, nothing to dispatch).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 hardware_capabilities tool returns chip/memory/GPU info and a local-LLM recommendation
- [x] #2 get_weather tool returns current conditions for a named location via a real API call
- [x] #3 Both tools registered in wiring.build_registry() and covered by a contract test in tests/delegator/test_tools.py
- [x] #4 uv run pytest, uv run mypy src, uv run ruff check . all pass
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
AC#1 partial: hardware_capabilities reports chip + total memory only, no GPU core count - named ceiling, not an oversight. Portable GPU-core detection needs macOS-specific system_profiler parsing (fragile, extra scope); memory is the actual binding constraint for local-LLM sizing on Apple Silicon's unified memory, so it drives the recommendation. Revisit if GPU core count becomes load-bearing for a routing decision. Verification: uv run pytest -q -> 32 passed, 37 skipped, 0 failed. uv run mypy src -> Success: no issues found in 39 source files. uv run ruff check . -> All checks passed.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Hardware and weather answer directly. Hardware now reports CPU/chip, RAM, GPU and a model recommendation with Darwin/Windows/Linux probes; real local Ollama selected the tool and spoke the observed 95 GB host profile. Weather remains keyless and fake-transport tested.
<!-- SECTION:FINAL_SUMMARY:END -->
