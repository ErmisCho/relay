---
id: TASK-52
title: Prevent silent hardware turns and stalled context I/O
status: Done
assignee: []
created_date: '2026-09-27 00:30'
updated_date: '2026-09-27 00:30'
labels:
  - delegator
  - reliability
dependencies: []
modified_files:
  - src/relay/delegator/llm/fallback.py
  - src/relay/delegator/service.py
  - src/relay/delegator/tools/hardware.py
  - src/relay/delegator/tools/weather.py
  - tests/delegator/test_request_failures.py
  - tests/delegator/test_live_ollama.py
priority: high
type: bug
ordinal: 26000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A stalled optional context write or report hook cannot indefinitely block speech
- [x] #2 Empty model output triggers fallback or an audible error instead of silent success
- [x] #3 Safety-critical commitment and scope hooks are never cancelled by the optional-context timeout
- [x] #4 Darwin chip and RAM detection works when POSIX memory keys are unavailable
- [x] #5 A real local Ollama hardware question selects the real tool and speaks observed specs
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Integrated and strengthened the unmerged reliability fix: bounded optional context I/O, preserved unbounded safety gates, rejected empty model streams, direct read-only hardware/weather answers, Darwin sysctl fallback, GPU reporting, and a real zero-cost Ollama smoke test. Final gate: 524 passed, 25 skipped; mypy and Ruff clean.
<!-- SECTION:FINAL_SUMMARY:END -->
