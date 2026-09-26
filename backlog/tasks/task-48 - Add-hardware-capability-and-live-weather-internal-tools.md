---
id: TASK-48
title: Add hardware-capability and live-weather internal tools
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-26 20:37'
updated_date: '2026-09-26 20:58'
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
- [ ] #1 hardware_capabilities tool returns chip/memory/GPU info and a local-LLM recommendation
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
Added hardware_capabilities and get_weather internal tools, registered in wiring.build_registry(), both answering directly in conversation (no commitment-protocol dispatch). get_weather calls Open-Meteo (geocoding + forecast, keyless) and degrades to a speakable message on any upstream failure. hardware_capabilities reports chip + total memory (stdlib os.sysconf on POSIX, ctypes GlobalMemoryStatusEx on Windows) and a 3-tier local-LLM recommendation - deliberately memory-only, no GPU core count (see notes). 9 new direct-tool tests plus 1 new end-to-end wiring test through the real registry/tool-loop; full suite 32 passed/0 failed, mypy clean, ruff clean. AC#1 left unchecked: it promised GPU info that was not built.
<!-- SECTION:FINAL_SUMMARY:END -->
