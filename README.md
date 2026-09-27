# relay

relay is a voice ideation partner. You say a wake word and think out loud with an agent. When you plainly tell it to do something ("look into X and write it up", "build me a script that..."), it starts right away and says "On it"; the server, not the voice model, judges that your words were an instruction. When you are still exploring or asking for ideas or its opinion, it starts nothing and just answers. Only if that judgement fails, or with `DIRECT_DISPATCH=0`, does it read back what it would do (the goal, what it leaves out, and what it hands back) and wait for an explicit yes. The agreed work then runs in the background, and the result is announced at a later turn. v1 covers research and writing only, and the artifact is a Markdown document. Code work that ends in a draft pull request comes later: `ENABLED_KINDS` defaults to `research`. See [`SPEC.md`](SPEC.md) for the product spec.

## Prerequisites

| Need | Why / notes |
|------|-------------|
| [uv](https://docs.astral.sh/uv/) | Python 3.12+ environment and runner |
| Docker | Runs Postgres 16. If `docker` is not on your `PATH`, the CLI may be in `~/.docker/bin`. |
| [Ollama](https://ollama.com) on `localhost:11434` | All defaults are local: `gemma4:e4b` handles conversation, classifiers and easy tasks; `qwen3.8:latest` handles hard tasks with gemma as fallback. Pull both with `ollama pull gemma4:e4b` and `ollama pull qwen3.8:latest`. |
| ElevenLabs account + API key | Handles STT, VAD, turn-taking and TTS through ElevenLabs Agents. Every voice session uses agent minutes. |
| [ngrok](https://ngrok.com) with a free dev domain | ElevenLabs calls the Delegator from its own cloud, so the Delegator needs a public URL. |
| Headset with a microphone | There is no acoustic echo cancellation (see [live voice testing](docs/live-voice-testing.md#echo-gate)). |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Docker.

One command installs dependencies, starts Postgres, migrates it, and runs the Delegator + executor:
`./scripts/dev-up.sh` (macOS/Linux) or `.\scripts\dev-up.ps1` (Windows).

Manual setup:

```bash
uv sync                           # runtime + dev dependency group
cp .env.example .env              # local Ollama defaults; no paid API required
docker compose up -d postgres     # Postgres 16 on 127.0.0.1:55432
uv run alembic upgrade head       # idea-graph schema (DBOS creates its own `dbos` schema)
```

These `.env` keys matter (every setting is in `src/relay/config.py`):

| Key | Set to |
|-----|--------|
| `DELEGATOR_SHARED_SECRET` | A long random value, e.g. `openssl rand -hex 32`. The Delegator will not start with the placeholder `dev-secret-change-me` unless `RELAY_ALLOW_DEV_SECRET=1` is set, and `apply_agent_config.py --apply` always refuses the placeholder. |
| `DELEGATOR_PUBLIC_URL` | Your ngrok URL, e.g. `https://<your-domain>.ngrok-free.app`. `--apply` refuses localhost URLs. |
| `ELEVENLABS_API_KEY` | Your ElevenLabs API key |
| `ELEVENLABS_AGENT_ID` | Leave empty until `apply_agent_config.py --apply` prints an id, then paste it here |
| `*_MODEL` | Model refs of the form `<provider>:<model>`, where the provider is `ollama`, `openai` or `anthropic`. The defaults are all local Ollama models. |
| `SILENCE_TIMEOUT_S` | Seconds without speech before the client closes a session (default 60) |
| `OPENAI_API_KEY` | Optional only. No paid provider is used by the default configuration. |
| `EXECUTOR_PROJECTS_ROOT` | Where the executor creates one project folder per idea (default `~/relay-projects`). It must be outside the relay repo; the worker refuses a root inside it, `/` or your home directory itself. |

## Running

Start the processes in this order, each in its own terminal.

**1. Delegator** (the custom LLM that ElevenLabs calls), on `127.0.0.1:8000`:

```bash
uv run python -m relay.delegator
```

The Delegator writes a line when the DBOS client warm-up finishes. It also starts any commitment left without a task by an earlier crash. The host and port can be overridden with `DELEGATOR_HOST` and `DELEGATOR_PORT`.

**2. Executor worker** (runs dispatched tasks as durable DBOS workflows), on `127.0.0.1:8001`:

```bash
uv run python -m relay.executor
curl http://127.0.0.1:8001/healthz        # {"ok":true,"kinds":["research"]}
```

**3. Tunnel** to the Delegator's port 8000:

```bash
ngrok http 8000 --url <your-domain>.ngrok-free.app
curl -N https://<your-domain>.ngrok-free.app/healthz
```

Do not use Cloudflare quick tunnels (`cloudflared tunnel --url`): they do not support Server-Sent Events, and the Delegator streams SSE. Other tunnel options are compared in [docs/elevenlabs-localhost-connectivity.md](docs/elevenlabs-localhost-connectivity.md).

**4. Agent config.** This renders `config/elevenlabs/agent.json` and pushes it to ElevenLabs:

```bash
uv run python scripts/apply_agent_config.py            # dry run: prints the payload, secrets redacted
uv run python scripts/apply_agent_config.py --apply    # CHANGES your ElevenLabs account
```

`--apply` stores the shared secret as an ElevenLabs workspace secret. If `ELEVENLABS_AGENT_ID` is set it updates that agent; otherwise it creates a new one and prints the id, which you then put into `ELEVENLABS_AGENT_ID`. The agent is private (`enable_auth`), so only signed sessions can start. Run `--apply` again whenever the tunnel URL, the secret or `SILENCE_TIMEOUT_S` changes.

**5. Client** (wake word, then the voice session):

```bash
uv run python -m relay.client                  # say "hey jarvis"
uv run python -m relay.client --list-devices   # pick a microphone
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--wake-model` | `WAKE_MODEL` (`hey_jarvis`) | Pretrained openWakeWord name or a path to an `.onnx` model |
| `--threshold` | `WAKE_THRESHOLD` (0.5) | Wake-word score that triggers a session |
| `--silence-timeout` | `SILENCE_TIMEOUT_S` (60) | Seconds without speech before auto-close |
| `--refractory` | 2 | Seconds the wake word is ignored after a trigger |
| `--device` | system default | Input device index or name substring |

The client prints its state (`[listening] say 'hey_jarvis'`, `[active]`, and so on) and both sides of the transcript (`you:` / `agent:`). Press Ctrl-C to stop it. The open session's `ended_at` is still recorded.

**Keep gemma warm.** Ollama unloads idle models, so the first turn after a pause pays the model load time. Keep the Delegator model resident, for example:

```bash
curl http://localhost:11434/api/generate -d '{"model": "gemma4:e4b", "keep_alive": -1}'
```

## Testing

```bash
uv run pytest -q          # tests create and drop their own throwaway database
uv run mypy src           # strict type check
uv run ruff check .       # lint (ruff format . to format)
```

Database tests are skipped, not failed, when Postgres is unreachable. To use another server, set `TEST_DATABASE_URL`.

Live tests are opt-in and call real local models:

```bash
RELAY_LLM_TESTS=1 uv run pytest -q tests/delegator/test_live_ollama.py tests/delegator/commitment/test_live_assent.py
RELAY_LLM_TESTS=1 RELAY_LIVE_RESEARCH=1 uv run pytest -q tests/executor/agent/test_live.py
```

To verify a hardware question against this host without ElevenLabs or a paid API:

```bash
RELAY_LLM_TESTS=1 uv run pytest tests/delegator/test_live_ollama.py -k hardware -q -s
```

The check fails if the local model does not select `hardware_capabilities` or if the answer omits the observed chip or RAM. The tool reports the Relay backend host, which can differ from a remote voice client.

The end-to-end voice checks (latency, barge-in, commitments) are manual. They are listed in [docs/live-voice-testing.md](docs/live-voice-testing.md), including the ordered Phase 1 close-out run. After a live session, audit it from the database (read-only; exit code 1 means an unintended dispatch or a session that never closed):

```bash
uv run python scripts/phase1_audit.py --latest
uv run python scripts/phase1_audit.py --session <uuid> --json audit.json
```

## Demo website

`web/` holds a browser demo (Vite + React) that shows the read-back, assent label, dispatch and delivered brief next to the conversation. The zero-cost hackathon path uses its scripted in-browser backend:

```bash
cd web && npm install && npm run dev:mock   # http://localhost:5173, passcode relay-demo
```

For live mode, build without `--mode mock` and serve it alongside the completed `/demo` API. See [web/README.md](web/README.md) for the service setup and [web/CONTRACT.md](web/CONTRACT.md) for the API contract.

## Layout

| Path | Contents |
|------|----------|
| `src/relay/config.py` | `Settings` / `get_settings()`: every environment variable and its default |
| `src/relay/store/` | SQLAlchemy models, async engine, idea repository, Alembic migrations |
| `src/relay/delegator/` | OpenAI-compatible SSE endpoint that ElevenLabs calls (`app.py`, `service.py`) |
| `src/relay/delegator/wiring.py` | Registration of the internal tools and turn hooks |
| `src/relay/delegator/hooks/`, `tools/` | Scope, reports and idea-summary hooks; `get_status`, `recall`, `focus_idea`, `link_ideas` |
| `src/relay/delegator/commitment/` | Commitment protocol: read-back, assent classifier, `propose_commitment` / `dispatch_task` |
| `src/relay/delegator/scope.py`, `prompts/system.md` | v1 scope boundary |
| `src/relay/delegator/llm/`, `adapters/` | Provider-agnostic chat models with fallback, OpenAI wire format |
| `src/relay/executor/` | DBOS worker, dispatch client, `run_task` workflow, runner registry |
| `src/relay/executor/agent/` | General executor: durable Pydantic AI agent with the pydantic-ai-harness Researcher (web search, SSRF-safe fetch) and Coder (files, shell) tools |
| `src/relay/executor/routing.py`, `workspace.py` | Easy/hard task routing; per-idea project folders under `EXECUTOR_PROJECTS_ROOT` |
| `src/relay/client/` | Wake word (`wake.py`), listener state machine (`listener.py`), ElevenLabs voice session |
| `config/elevenlabs/agent.json` | Versioned ElevenLabs agent config (with `${NAME}` placeholders) |
| `scripts/apply_agent_config.py` | Renders the agent config and applies it (dry run by default) |
| `scripts/measure_false_triggers.py` | Wake-word false-trigger measurement (no agent minutes used) |
| `scripts/phase1_audit.py` | Read-only Phase 1 audit of live sessions |
| `web/` | Demo website with zero-cost mock mode and the live TASK-42 API client |
| `tests/` | Unit, contract, database and opt-in live tests |

## Further reading

- [docs/architecture.md](docs/architecture.md): system diagram, request lifecycle, commitment protocol, safety rails
- [docs/live-voice-testing.md](docs/live-voice-testing.md): live acceptance checklist and troubleshooting
- [docs/elevenlabs-localhost-connectivity.md](docs/elevenlabs-localhost-connectivity.md): why a tunnel is needed and which one to use
- [SPEC.md](SPEC.md): product specification
