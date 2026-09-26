---
id: TASK-33
title: Build the code executor that stops at a pull request
status: In Progress
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 19:49'
labels:
  - phase-2
  - executor
  - code
milestone: m-1
dependencies:
  - TASK-32
  - TASK-47
references:
  - SPEC.md#3-scope
  - SPEC.md#8-phasing
documentation:
  - SPEC.md
priority: medium
type: feature
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Phase 2 adds the second vertical: write code, refactor, run tests, open PRs. The terminal artifact is a pull request on a branch — **never merged** — so the executor cannot land unreviewed changes (SPEC §10).

**Technical details**
- DBOS workflow `code_task(task_id)` with steps: `checkout` (clone/fetch into an isolated worktree, branch `relay/<task_id>-<slug>`), `agent_loop` (Pydantic AI `DBOSAgent`), `run_tests`, `push`, `open_pr` (GitHub API / `gh pr create --draft` with the commitment goal, scope exclusions and test results in the body).
- Agent tools: `list_files`, `read_file`, `write_file`/`apply_patch` confined to the worktree, `run_command` with an allowlist (test runner, linters, package manager) plus timeout and output truncation. No merge, push-to-default-branch or network-publish tools.
- Record `artifacts(kind='pull_request', url=<PR url>)`; failed tests are reported in the PR, not hidden.
- Add `code` to ENABLED_KINDS and `pull_request` to the allowed artifact kinds; read-back phrasing "…and I'll leave it as a PR for you to look at".
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 An agreed code commitment produces a draft PR on a new branch with a description containing goal, exclusions and test results
- [x] #2 The executor has no capability to merge or push to the default branch, verified by test
- [x] #3 Killing the worker mid-task resumes from the last completed step
- [x] #4 An `artifacts` row of kind `pull_request` is recorded and announced at the next turn boundary
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 retarget (owner): the code executor is no longer a separate vertical with hand-rolled list_files/read_file/write_file/run_command tools. It is the general executor from TASK-47, using the pydantic-ai-harness Coder capability (filesystem, ripgrep, shell, sub-agents). The work happens in a per-idea project folder independent of the relay repo. This task keeps what is specific to code commitments: branch per task, run_tests step, draft PR as the terminal artifact (never merged), no merge/push-to-default capability (Coder shell is unrestricted, so this needs a guard verified by test), pull_request artifact kind, adding code to ENABLED_KINDS, and the read-back phrasing.

2026-09-26 751ca58: DBOS steps route→workspace→checkout (git init + relay/<id8>-<slug>)→code agent→sandboxed commit→sandboxed run_tests→worker-side push→draft PR (reuses open PR; never merges). No GITHUB_TOKEN → branch artifact (migration 0004). Tests: e2e draft PR with goal/exclusions/test results + pull_request artifact announced via ReportsHook; push guard refuses main/master/origin HEAD/non-relay branches; sigkill resume; no-token local branch. Gate 488 passed/15 skipped, mypy 0, ruff 0. Code stays opt-in: ENABLED_KINDS=research,code + GITHUB_TOKEN. Not yet live-run.
<!-- SECTION:NOTES:END -->
