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

## Check a local hardware question

On the machine running Relay and Ollama, after setting the local model names in `.env`:

```bash
RELAY_LLM_TESTS=1 uv run pytest tests/delegator/test_live_ollama.py -k hardware -q -s
```

This check sends a typed specs question through the real configured local model,
the production tool registry and HTTP response code. It fails if the model does not
call `hardware_capabilities` or does not report the observed chip and RAM. It does
not require ElevenLabs or Postgres, and does not test the microphone/TTS path.
The probe describes the **backend host**, not a separate voice-client device.
GPU and storage details are not currently collected.

When testing the running service, look in `delegator.log` for
`executing internal tool hardware_capabilities` and its completion timing. A
`timed out` context message means optional persistence/report retrieval was skipped
to keep the response moving. `no text or tool calls` means the model returned an
empty answer; Relay now retries through its fallback and speaks an error if that
also fails. Neither case proves a successful hardware answer: run the check above.
