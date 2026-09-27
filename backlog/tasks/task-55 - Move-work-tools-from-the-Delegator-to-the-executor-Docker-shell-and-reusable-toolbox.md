---
id: TASK-55
title: >-
  Move work tools from the Delegator to the executor; Docker shell and reusable
  toolbox
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-27 05:00'
updated_date: '2026-09-27 06:06'
labels:
  - delegator
  - executor
dependencies: []
priority: high
type: feature
ordinal: 27000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
User decision 2026-09-27: the Delegator only converses, creates tasks and delegates; it keeps propose_commitment/dispatch_task/get_status and idea memory (recall/focus_idea/link_ideas) but no work tools. hardware_capabilities and get_weather move into the executor as built-in tools (a container cannot see the host's hardware). Every handoff keeps read-back + explicit yes (no protocol change, user choice). On Windows/Linux the executor gets a shell whose commands run in a throwaway Docker container (--network none, only the task folder mounted) since the macOS sandbox-exec shell is macOS-only and the harness shell is POSIX-only. The executor saves reusable scripts it wrote to a persistent toolbox, copied into each task folder and synced back after the run, so later tasks reuse them.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Delegator registry has no hardware_capabilities/get_weather and no direct_response path
- [x] #2 Executor has built-in hardware and weather tools
- [x] #3 On Windows the executor runs shell commands in a Docker container with no network
- [x] #4 Scripts saved to toolbox/ in one task are available in the next task's folder
- [ ] #5 Live: specs+weather question -> read-back -> yes -> executor answers both
- [x] #6 uv run pytest, uv run mypy src, uv run ruff check . pass
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-27. Delegator: registry = get_status, recall, focus_idea, link_ideas, propose_commitment, dispatch_task; HARDWARE_TOOL_NOTE and the direct_response speak path removed. Executor: machine_hardware + current_weather built-in (moved from delegator/tools, git mv); Windows/Linux shell = docker_shell.run_command (python:3.13-slim, --network none, only the task folder at /work, 2g/2cpu/256 pids, killed by name on timeout; image pre-pulled once, --pull never; fail closed without Docker); macOS keeps sandbox-exec. Toolbox at <projects_root>/.toolbox, copied in/out by DBOS steps relay.executor.toolbox_in/_out around the agent run. Tests: 534 passed/24 skipped; network test fails with --network bridge. Live: Delegator proposed (no work tool), yes -> dispatch_task, commitment recorded. Executor run directly (gpt-6-luna, real tools): task 1 called machine_hardware + current_weather and reported both; task 2 wrote toolbox/disk_space.py + INDEX.md, ran it in Docker, saved to toolbox; task 3 got both in its folder, read INDEX, reused the script. AC5 open: the queued live task was picked up by an older executor process the operator had running on the same Postgres queue (it answered with no shell), so the full voice->queue->new-executor path is unverified. One direct run failed on 3 empty gpt-6-luna responses (upstream), passed on rerun.

2026-09-27 later: operator's voice tests had been served by a collaborator's Mac (shared ElevenLabs agent pointed at geology-hardiness-cage via her apply_agent_config run) - old code, nothing in the local DB. On operator's choice the shared agent was re-pointed with scripts/apply_agent_config.py --apply (capital-rhyme-manmade tunnel, operator's shared secret); whoever applies last wins. Fixes: answer_first on propose_commitment (mixed chat+work: 0/5 -> 5/5), cut-off-reply note in VoiceRulesHook (split speech: 0/5 -> 10/10 kept both questions), quick lookups in 2 executor calls (6/6, 5-10 s). Gates: 538 passed/24 skipped.
<!-- SECTION:NOTES:END -->
