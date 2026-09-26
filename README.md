# relay

Voice-first ideation agent: wake word, ElevenLabs voice loop, Delegator, durable executors and a Postgres idea graph. See `SPEC.md`.

## Installation

```bash
uv sync --extra dev
```

## Local setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

One command that does all of the below and then runs the Delegator + executor:
`./scripts/dev-up.sh` (macOS/Linux) or `.\scripts\dev-up.ps1` (Windows).

Manual steps, if you'd rather run them yourself:

```bash
uv sync --extra dev               # install runtime + dev dependencies
cp .env.example .env              # settings (local Ollama models by default)
docker compose up -d postgres     # Postgres 16 on localhost:55432 (user/pass/db: relay)
uv run alembic upgrade head       # create the idea-graph schema
uv run pytest                     # tests create and drop their own throwaway database
```

Settings live in `src/relay/config.py` and are read from the environment and `.env`.
Database tests are skipped (not failed) when Postgres is unreachable; point them at another
server with `TEST_DATABASE_URL`. `uv run alembic downgrade base` removes the schema.

## Layout

| Path | Contents |
|------|----------|
| `src/relay/config.py` | `Settings` / `get_settings()` |
| `src/relay/store/` | SQLAlchemy models, async engine, Alembic migrations |
| `src/relay/delegator/` | Custom-LLM endpoint for ElevenLabs |
| `src/relay/executor/` | Durable task executors |
| `src/relay/client/` | Wake word + voice session client |

## Development

| Command | Description |
|---------|-------------|
| `uv sync --extra dev` | Install dependencies |
| `uv run ruff check .` | Lint with Ruff |
| `uv run ruff format .` | Format with Ruff |
| `uv run mypy src` | Type-check |
| `uv run pytest` | Run tests with pytest |
