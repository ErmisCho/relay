# relay demo website

A browser front end for demoing relay to people who have never seen it (TASK-43). It puts the parts of a voice call you normally cannot see next to the conversation: the scope gate, the ready gate, the spoken read-back, the assent label, the durable dispatch and the delivered brief.

**Stack.** Vite + React + TypeScript. React because the official ElevenLabs browser SDK ships a React binding (`@elevenlabs/react`) that handles the mic, WebRTC/WebSocket transport and barge-in. Vite gives a static build the Delegator can serve plus a dev proxy for `/demo`. The only other runtime dependency is `react-markdown` (+ `remark-gfm`) for rendering briefs safely.

## What is on the page

- **Passcode gate.** Nothing else loads until `/demo/auth` accepts the demo passcode.
- **Voice panel.** Start/stop an ElevenLabs conversation with a server-issued token, a bar visualizer (states modelled on the ElevenLabs UI BarVisualizer), live captions and the transcript. ElevenLabs handles barge-in; an interrupted reply is marked "cut off by barge-in".
- **Type to talk.** Always available, and focused automatically when there is no mic or permission is denied. It goes through the same Delegator path.
- **Decision trace.** Current idea, ready gate, pending read-back, assent label, scope refusals and dispatches, live from the session's event stream. A dispatch opens into a task card (id, executor kind, read-back, verbatim assent words, DBOS workflow id, live status). On delivery, "Read the brief" opens the Markdown with clickable sources. On a phone the trace becomes a bottom sheet.
- **Ideas & audit.** Every idea with its status and links, and every commitment with its read-back and the exact words that said yes.
- **Guided scenarios.** Full flow, a hedge that does not dispatch, an out-of-scope refusal, and recall of an earlier idea. They send scripted lines through the text endpoint, so they work against the mock and the real Delegator.

## Run it

Requires Node 20+.

```bash
cd web
npm install
```

### Mock mode (no Python stack)

```bash
npm run dev:mock        # http://localhost:5173, passcode: relay-demo
```

`VITE_API_MODE=mock` (from `.env.mock`) swaps the HTTP client for a scripted in-browser backend (`src/api/mock.ts`). It emits the same events as the real feed. Voice is not available in mock mode; the site says so and points to typing or the scenarios.

### Against the real Delegator (locally)

The site talks to the TASK-42 demo endpoints (see [CONTRACT.md](CONTRACT.md)). All commands run from the repository root unless noted.

1. Postgres (`docker compose up -d`) with the schema at head: `uv run alembic upgrade head` (the Delegator answers 500 on `/demo` routes when the database is behind the code, e.g. `column sessions.end_reason does not exist`).
2. Ollama running with the models named in `.env` pulled.
3. Build the site so the Delegator can serve it (it mounts `web/dist` at `/` when the demo is on):

   ```bash
   (cd web && npm install && npm run build)
   ```

4. The executor worker (picks dispatched tasks off the DBOS queue in Postgres):

   ```bash
   uv run python -m relay.executor                     # 127.0.0.1:8001, EXECUTOR_PORT to change
   curl http://127.0.0.1:8001/healthz                  # {"ok":true,"kinds":["research"]}
   ```

5. The Delegator with the demo turned on. `DEMO_PASSCODE` is required (empty = no `/demo` routes, 404); keep it out of the repo and out of `.env.example`:

   ```bash
   DEMO_PASSCODE="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')" \
     uv run python -m relay.delegator                  # 127.0.0.1:8000, DELEGATOR_PORT to change
   ```

   Optional: `DEMO_MAX_VOICE_SECONDS` (default 600), `DEMO_WEB_DIST` (default `web/dist`, relative to the working directory), `DEMO_TASK_POLL_S` (default 0.5). Voice also needs `ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID`; without them `POST /demo/sessions/{id}/voice` answers `503 voice_unavailable` and typing still works.

6. Open http://127.0.0.1:8000/ and enter the passcode. Or, to work on the UI with hot reload, run Vite and let it proxy `/demo` to the Delegator:

   ```bash
   cd web
   npm run dev                                         # http://localhost:5173 → /demo on :8000
   RELAY_DELEGATOR_URL=http://127.0.0.1:8100 npm run dev -- --port 5174   # other ports
   ```

With local models a reply takes seconds to a minute, and a research brief several minutes. Task status reaches the page only for tasks dispatched by the Delegator you are looking at (it polls their rows), and the trace history lives in the Delegator's memory: after a Delegator restart a reload shows an empty trace, while "Ideas & audit" still shows everything.

Browsers only expose the microphone on `https` or `localhost`.

### Over the ngrok URL

Run steps 1–5 above, then point ngrok at the Delegator port:

```bash
ngrok http --url=geology-hardiness-cage.ngrok-free.dev 8000
```

Open https://geology-hardiness-cage.ngrok-free.dev, click through ngrok's one-time browser warning, and enter the passcode. The cookie is marked `Secure` automatically behind ngrok (`X-Forwarded-Proto: https`). ElevenLabs sessions use the server-issued token, so the private agent works from any origin (no localhost allowlist, see elevenlabs-js issue #320).

## Develop

```bash
npm run typecheck       # tsc --noEmit
npm test                # vitest: event reducer, mock scenarios, Markdown sanitising
npm run build
```

Layout: `src/api/contract.ts` (the API contract), `src/api/client.ts` (HTTP + SSE), `src/api/mock.ts`, `src/state/trace.ts` (folds events into transcript, proposal threads and task cards), `src/components/`.
