
<!-- BACKLOG.MD GUIDELINES START -->
<CRITICAL_INSTRUCTION>

## Backlog.md Workflow

This project uses Backlog.md for task and project management.

**For every user request in this project, run `backlog instructions overview` before answering or taking action.**

Use the overview to decide whether to search, read, create, or update Backlog tasks.

Use the detailed guides when needed:
- `backlog instructions task-creation` for creating or splitting tasks
- `backlog instructions task-execution` for planning and implementation workflow
- `backlog instructions task-finalization` for completion and handoff

Use `backlog <command> --help` before running unfamiliar commands. Help shows options, fields, and examples.

Do not edit Backlog task, draft, document, decision, or milestone markdown files directly. Use the `backlog` CLI so metadata, relationships, and history stay consistent.

</CRITICAL_INSTRUCTION>
<!-- BACKLOG.MD GUIDELINES END -->
<!-- source: session-orchestrator plugin (canonical: templates/_shared/harte-regeln.md) -->
## Harte Regeln

Nicht verhandelbar — gilt unabhängig vom Wachstum dieses Repos, auch unterhalb
der `.claude/rules/`-Graduationsschwelle (#730/H6):

1. Keine Secrets/API-Keys committen (auch nicht in `.env.example`-Kommentaren).
2. Kein `git push --force` auf den Default-Branch ohne explizite Rückfrage.
3. Kein `git add -A` / `git add .` — Dateien einzeln stagen.
4. Destruktive Kommandos (`rm -rf`, `git reset --hard`, `git checkout -- <file>`)
   nur nach expliziter Bestätigung.
5. Status-Dokumente (STATE.md, README-Status-Sections, Dashboards) sind INDEX,
   nie HISTORIE — CCU-009 (see `skills/_shared/state-ownership.md` in the plugin).
6. Test-/Typecheck-/Lint-Commands in Session Config müssen echte Checks sein,
   keine Stubs (`echo`/`noop`).
7. Kein `console.log`/`debugger` in committeten Production-Files.

## Session Config

project-name: relay
vcs: github
persistence: true
enforcement: warn   # strict | warn | off
waves: 5
agents-per-wave: 6
test-command: uv run pytest
typecheck-command: uv run mypy src
lint-command: uv run ruff check .
recent-commits: 20
stale-branch-days: 7
skill-evolution:
  autonomy: off            # off | advisory | autonomous-gated — opt-in self-evolution (default off)

## Dispatcher Autonomy

> **Parity-exempt section.** This H2 is intentionally placed outside the `## Session Config` block so that the `claude-md-drift-check` Check-6 parity scanner (which extracts only column-0 keys inside the `## Session Config` block) does not flag repos that have not yet adopted this feature. Issue #679 / #681.

Opt-in configuration for the cross-repo free-repo dispatcher autonomy gate (Epic #673). The default is `off` — fail-closed. The effective `autonomy` resolves with host-local precedence `SO_DISPATCHER_AUTONOMY` env > `owner.yaml` `dispatcher.autonomy` > committed > `off` (#653 pattern).

```yaml
dispatcher-autonomy:
  autonomy: off            # off | advisory | autonomous-gated — default off (fail-closed)
  confidence-floor: 0.5    # float 0.0..1.0
```

Read by: `scripts/lib/config/dispatcher-autonomy.mjs` (parser + resolver), `skills/dispatcher/SKILL.md` (cross-repo dispatch flow). Issue: #681.
