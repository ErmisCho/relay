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

The site needs the TASK-42 demo endpoints (see [CONTRACT.md](CONTRACT.md)). Start the services from the repository README first:

1. Postgres (`docker compose up -d`)
2. Ollama with the delegator and executor models pulled
3. the Delegator on port 8000, with the demo passcode and the ElevenLabs key/agent id in its environment
4. the executor worker on port 8001

Then:

```bash
npm run dev             # proxies /demo to http://localhost:8000
# or: RELAY_DELEGATOR_URL=http://127.0.0.1:8000 npm run dev
```

Browsers only expose the microphone on `https` or `localhost`.

### Over the ngrok URL

```bash
npm run build           # type-checks, then writes web/dist
```

Serve `web/dist` from the Delegator at `/` (see CONTRACT.md, "Serving and origin") and point ngrok at the Delegator port:

```bash
ngrok http --url=geology-hardiness-cage.ngrok-free.dev 8000
```

Open https://geology-hardiness-cage.ngrok-free.dev, click through ngrok's one-time browser warning, and enter the passcode. ElevenLabs sessions use the server-issued token, so the private agent works from any origin (no localhost allowlist, see elevenlabs-js issue #320).

## Develop

```bash
npm run typecheck       # tsc --noEmit
npm test                # vitest: event reducer, mock scenarios, Markdown sanitising
npm run build
```

Layout: `src/api/contract.ts` (the API contract), `src/api/client.ts` (HTTP + SSE), `src/api/mock.ts`, `src/state/trace.ts` (folds events into transcript, proposal threads and task cards), `src/components/`.
