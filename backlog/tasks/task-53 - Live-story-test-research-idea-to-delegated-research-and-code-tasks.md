---
id: TASK-53
title: 'Live story test: research idea to delegated research and code tasks'
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-27 04:44'
updated_date: '2026-09-27 04:58'
labels:
  - phase-1
  - validation
  - executor
milestone: m-0
dependencies: []
references:
  - transcript.txt
priority: high
type: task
ordinal: 25000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The owner wants proof that a free-form spoken brainstorm (in the spirit of transcript.txt) converges on well-defined tasks that the voice agent delegates correctly, and that the executor then creates a per-idea project folder and fills it with research Markdown and working code. Existing live tests cover single turns or seeded commitments only; nothing drives a whole conversation through the real Delegator into the real worker.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 An opt-in live test plays a multi-turn user story through the real Delegator app (real models, real hooks and tools) and assents to each read-back
- [x] #2 The story yields exactly the intended commitments (one research document, one code task) with verbatim assent and no unintended dispatch
- [x] #3 The real executor worker runs both tasks to success in one project folder created under EXECUTOR_PROJECTS_ROOT
- [x] #4 The project folder contains the research Markdown and code files, and the code task's tests pass
- [x] #5 pytest, mypy and ruff pass
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Branch test/lofi-story off origin/feat/merge-dev (main + dev merged). 1) tests/story/test_lofi_story.py: opt-in live story (RELAY_LLM_TESTS=1 RELAY_LIVE_STORY=1) through the real Delegator over a socket and a real worker on a fresh DB + temp projects root. 2) Fix whatever the run exposes.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-27 run 1 (old worker code): delegation correct (2 commitments document + pull_request, verbatim assent, same idea), both tasks succeeded on gpt-6-luna, one folder local-lo-fi-beat-sketching-tool-<id8> created. Two gaps: (a) research brief only in ARTIFACTS_DIR, never in the project folder -> new DBOS step relay.executor.save_to_project copies it to <project>/research/<slug>-<task8>.md; (b) code verdict 'no tests were found to run' for a stdlib script + tests/ without pyproject, and the sandbox python3 (Homebrew) has no pytest -> detect_test_command now finds tests/ or test*.py and runs pytest if importable else unittest discover. Tests: research e2e asserts the project copy; tests/executor/code/test_detect_tests.py runs the command outside the venv. Gate: 546 passed/16 skipped, mypy 0, ruff 0.

2026-09-27 run 2 (with fixes): 1 passed in 236 s. 7 turns, 0 early read-backs; research + code succeeded on gpt-6-luna; folder has lo_fi_beat.py, test_lo_fi_beat.py, research/a-practical-guide-to-building-lo-fi-hip-76db8509.md; code verdict 'tests passed'.
<!-- SECTION:NOTES:END -->
