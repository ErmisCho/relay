---
id: TASK-8
title: Install session-orchestrator plugin
status: To Do
assignee: []
created_date: '2026-09-26 13:17'
labels:
  - tooling
  - agent-plugin
dependencies: []
references:
  - 'https://github.com/Kanevry/session-orchestrator'
priority: medium
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Run in Claude Code: /plugin marketplace add Kanevry/session-orchestrator, then /plugin install session-orchestrator@kanevry. Then one-time: claude plugin list --json, find session-orchestrator@kanevry installPath, cd into it and run npm install, restart Claude Code. Then add the Session Config block to CLAUDE.md and run /bootstrap once in this repo.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Plugin installed and enabled (visible in claude plugin list)
- [ ] #2 Node deps installed once in the plugin installPath
- [ ] #3 Session Config block present in CLAUDE.md with real test/typecheck/lint commands
- [ ] #4 C:/Program Files/Git/bootstrap run successfully, .orchestrator/bootstrap.lock exists
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) /plugin marketplace add Kanevry/session-orchestrator  2) /plugin install session-orchestrator@kanevry  3) claude plugin list --json, copy installPath for session-orchestrator@kanevry  4) cd <installPath> && npm install  5) restart Claude Code  6) add Session Config block to CLAUDE.md (needs the scaffolding task's command strings to exist first)  7) /bootstrap in ~/relay
<!-- SECTION:PLAN:END -->
