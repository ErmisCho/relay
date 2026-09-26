#!/usr/bin/env bash
# One-command local dev environment: Postgres, migrations, Delegator, executor.
#
# Usage: ./scripts/dev-up.sh
# Ctrl-C stops the Delegator and executor and leaves Postgres running
# (docker compose down -v if you want to wipe it too).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

log_dir="$(mktemp -d)"
delegator_log="$log_dir/delegator.log"
executor_log="$log_dir/executor.log"
delegator_pid=""
executor_pid=""

cleanup() {
  echo
  echo "Stopping delegator/executor..."
  [[ -n "$delegator_pid" ]] && kill "$delegator_pid" 2>/dev/null || true
  [[ -n "$executor_pid" ]] && kill "$executor_pid" 2>/dev/null || true
  wait "$delegator_pid" "$executor_pid" 2>/dev/null || true
  echo "Postgres left running (docker compose down -v to wipe it)."
}
trap cleanup EXIT INT TERM

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found - install Docker Desktop or Colima first." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example (edit it to add real API keys if you're not using Ollama)."
fi

echo "Installing dependencies (uv sync --extra dev)..."
uv sync --extra dev

echo "Starting Postgres..."
docker compose up -d --wait postgres

echo "Applying migrations..."
uv run alembic upgrade head

if command -v ollama >/dev/null 2>&1 && ! curl -sf http://localhost:11434 >/dev/null 2>&1; then
  echo "Warning: Ollama is installed but not responding on :11434 - start it, or edit .env" \
    "to point DELEGATOR_MODEL/etc. at Anthropic/OpenAI instead." >&2
fi

export RELAY_ALLOW_DEV_SECRET=1

echo "Starting Delegator (:8000) and executor (:8001)... logs: $log_dir"
uv run python -m relay.delegator >"$delegator_log" 2>&1 &
delegator_pid=$!
uv run python -m relay.executor >"$executor_log" 2>&1 &
executor_pid=$!

sleep 1
if ! kill -0 "$delegator_pid" 2>/dev/null; then
  echo "Delegator failed to start - see $delegator_log" >&2
  cat "$delegator_log" >&2
  exit 1
fi
if ! kill -0 "$executor_pid" 2>/dev/null; then
  echo "Executor failed to start - see $executor_log" >&2
  cat "$executor_log" >&2
  exit 1
fi

cat <<EOF

Everything is up.
  Delegator: http://127.0.0.1:8000  (log: $delegator_log)
  Executor:  http://127.0.0.1:8001  (log: $executor_log)

Try it:
  curl -N http://127.0.0.1:8000/v1/chat/completions \\
    -H "Authorization: Bearer dev-secret-change-me" \\
    -H "Content-Type: application/json" \\
    -d @tests/delegator/fixtures/elevenlabs_custom_llm_request.json

Ctrl-C to stop.
EOF

tail -f "$delegator_log" "$executor_log" &
tail_pid=$!
wait "$delegator_pid" "$executor_pid"
kill "$tail_pid" 2>/dev/null || true
