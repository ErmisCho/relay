---
id: TASK-49
title: Fix silent hardware requests and bound voice context I/O
status: In Progress
assignee:
  - '@codex'
created_date: '2026-09-26 22:12'
updated_date: '2026-09-26 22:15'
labels: []
dependencies: []
priority: high
type: bug
ordinal: 21000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The user could not complete a basic PC specs request. Reproductions at main 76fa4d8 show empty model streams returning silent success, unbounded pre-model context I/O, and no Darwin sysctl fallback. The actual Mac model and voice session are unavailable here; live validation must remain explicit.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A stalled context write or report hook cannot indefinitely block a spoken response
- [x] #2 Empty model output triggers fallback or an audible error instead of silent success
- [x] #3 Darwin chip and RAM detection works when POSIX memory keys are unavailable
- [ ] #4 An opt-in check exercises the configured real Ollama model selecting the real hardware tool and answering with observed specs
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Reproduce each failure before changes. 2. Bound optional context I/O and handle empty model output. 3. Add bounded Darwin hardware probes and explicit tool instructions. 4. Add a live local model smoke check, run regression tests and code checks, and report runtime validation limits.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verification: five initial regression cases failed against main 76fa4d8 (two blocked context operations, two silent empty streams, one simulated Darwin probe). After fixes, full pytest: 41 passed, 38 skipped; mypy src and ruff check . pass; git diff --check clean. Skips comprise 36 Postgres-dependent cases and two opt-in live Ollama checks. Mocked tests ran with proxy environment variables unset because this workspace uses a SOCKS proxy without the optional socksio dependency. No production proxy settings were changed. AC4 remains pending a real run on the user Mac: RELAY_LLM_TESTS=1 uv run pytest tests/delegator/test_live_ollama.py -k hardware -q -s. The check exists but was not run here. Voice setup and the exact original incident remain unverified. Context timeout tradeoff: a database write or report lookup exceeding 0.5 seconds is cancelled/logged so speech can continue; affected conversation records may be absent. GPU/storage and remote-client hardware are not collected.
<!-- SECTION:NOTES:END -->
