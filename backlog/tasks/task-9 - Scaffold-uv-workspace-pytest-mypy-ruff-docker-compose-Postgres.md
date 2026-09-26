---
id: TASK-9
title: 'Scaffold uv workspace, pytest/mypy/ruff, docker-compose Postgres'
status: To Do
assignee: []
created_date: '2026-09-26 13:17'
labels:
  - phase-1
  - tooling
  - scaffolding
milestone: m-0
dependencies: []
references:
  - docs/spec-v1-draft.md#9-cost-model
priority: high
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Creates the two-member uv workspace (server/, client/), shared lint/type/test config, and a docker-compose Postgres service. Nothing else in Phase 1 can be built or verified without this.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 docker compose up -d --wait postgres succeeds
- [ ] #2 uv sync succeeds for both workspace members
- [ ] #3 test-command, typecheck-command, lint-command all exit 0 against placeholder packages
- [ ] #4 Alembic baseline migration runs against the compose Postgres
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1) root pyproject.toml with [tool.uv.workspace] members=[server,client] plus shared [tool.ruff]/[tool.mypy]/[tool.pytest.ini_options] (asyncio_mode=auto, markers=[integration])  2) docker-compose.yml: postgres:16, named volume, pg_isready healthcheck  3) .env.example with DATABASE_URL  4) server/pyproject.toml (fastapi, sqlalchemy[asyncpg], dbos, pydantic-ai, anthropic) plus placeholder relay_server package and one passing test  5) client/pyproject.toml (openwakeword, elevenlabs) plus placeholder relay_client package and one passing test  6) uv sync at root, confirm both members resolve  7) alembic init under server/src/relay_server/idea_graph/migrations, empty baseline revision runnable against compose postgres  8) confirm Docker Desktop/WSL2 present and docker compose up -d --wait postgres succeeds  9) add the three Session Config command strings to CLAUDE.md
<!-- SECTION:PLAN:END -->
