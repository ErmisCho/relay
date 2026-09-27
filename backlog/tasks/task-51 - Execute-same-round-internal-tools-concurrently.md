---
id: TASK-51
title: Execute same-round internal tools concurrently
status: Done
assignee: []
created_date: '2026-09-27 00:16'
updated_date: '2026-09-27 00:17'
labels:
  - delegator
  - performance
dependencies: []
modified_files:
  - src/relay/delegator/service.py
  - tests/delegator/test_tools.py
priority: medium
type: enhancement
ordinal: 25000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Independent internal tools emitted in one model round execute concurrently
- [x] #2 Tool result messages preserve model call order
- [x] #3 One tool failure does not discard sibling results
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Implemented asyncio.gather in the shared Delegator tool loop. A barrier-based regression test proves concurrency, stable result ordering, and per-tool failure isolation. Verified in the full offline gate: 515 Python tests passed.
<!-- SECTION:FINAL_SUMMARY:END -->
