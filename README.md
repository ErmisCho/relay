# relay

Voice-first ideation agent: wake word, ElevenLabs voice loop, Delegator, durable executors and a Postgres idea graph. See `SPEC.md`.

## Installation

```bash
uv sync --extra dev
```

## Development

| Command | Description |
|---------|-------------|
| `uv sync --extra dev` | Install dependencies |
| `uv run ruff check .` | Lint with Ruff |
| `uv run ruff format .` | Format with Ruff |
| `uv run mypy src` | Type-check |
| `uv run pytest` | Run tests with pytest |
