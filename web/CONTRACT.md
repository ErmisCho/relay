# relay demo API contract (TASK-42 ⇄ TASK-43)

The demo website (`web/`, TASK-43) needs these endpoints from the Delegator (TASK-42). Shapes are defined in [`src/api/contract.ts`](src/api/contract.ts); this page explains them for the backend implementer. The in-browser mock (`src/api/mock.ts`) implements the same contract and is a working reference for event order.

## Serving and origin

- All demo endpoints live under `/demo/` on the Delegator (port 8000), next to the existing `/v1/chat/completions` routes.
- Recommended: the Delegator also serves the built site (`web/dist`) at `/` (for example FastAPI `StaticFiles(directory="web/dist", html=True)` mounted last). The site and the API then share one origin, so the ngrok URL serves both, the cookie is first-party and no CORS setup is needed. In development Vite proxies `/demo` to `http://localhost:8000`.
- The client sends `ngrok-skip-browser-warning: 1` on every `fetch`, so ngrok's free-tier interstitial does not replace JSON responses. `EventSource` cannot send headers; it relies on the browser having already loaded the page through ngrok.

## Access control (TASK-42 AC#6)

| | |
|---|---|
| `POST /demo/auth` `{ "passcode": "…" }` | `204` and `Set-Cookie: relay_demo=<opaque, signed>; HttpOnly; SameSite=Lax; Path=/demo; Secure` (Secure when served over https). Wrong passcode: `401 {"error":"unauthorized"}`. Rate-limit guesses: `429 {"error":"rate_limited"}`. |
| `GET /demo/auth` | `204` when the cookie is valid, else `401`. The site calls it on load to skip the gate. |
| every other `/demo/*` | `401 {"error":"unauthorized"}` without a valid cookie. |

The passcode is configuration (for example `RELAY_DEMO_PASSCODE`), never in the repo. A cookie rather than a header is required because `EventSource` cannot set headers.

## Sessions, text chat and voice

| Method and path | Response | Notes |
|---|---|---|
| `POST /demo/sessions` | `201 {session_id, created_at, voice_max_seconds}` | A demo session = a relay `sessions` row. The site creates a new one on load, on "New session" and before each guided scenario. |
| `POST /demo/sessions/{id}/messages` `{text}` | `202 {turn_id}` | 1–2000 chars. Runs through the **same Delegator path** as a voice turn (hooks, scope gate, commitment protocol, tools). The reply arrives on the event stream as `assistant_turn`, not in this response. `422` on a bad body, `404` on an unknown session. |
| `POST /demo/sessions/{id}/voice` | `200 VoiceCredentials` | See below. `409 {"error":"voice_busy"}` while another voice session holds the single slot; `503 {"error":"voice_unavailable","message":"…"}` if ElevenLabs is not configured. |
| `DELETE /demo/sessions/{id}/voice` | `204` | Sent when the viewer ends the call or the client-side timer runs out. Releases the slot. The server must also release it on its own after `max_duration_s` (the tab may simply close). |

`VoiceCredentials` (the ElevenLabs API key never leaves the server):

```json
{ "transport": "webrtc", "conversation_token": "…", "expires_at": "2026-09-26T16:52:04.000Z", "max_duration_s": 600 }
{ "transport": "websocket", "signed_url": "wss://api.elevenlabs.io/…", "expires_at": "…", "max_duration_s": 600 }
```

- `webrtc` comes from ElevenLabs `GET /v1/convai/conversation/token?agent_id=…`; `websocket` from `GET /v1/convai/conversation/get-signed-url?agent_id=…`. WebRTC is preferred for the browser (echo cancellation makes barge-in reliable).
- The browser starts the session with `dynamicVariables: {session_id}` **and** `customLlmExtraBody: {session_id}`, the same two channels the Python client uses, so the Delegator's session-id resolution attributes the voice turns to this demo session and emits their events on its stream.

## Live event stream (TASK-42 AC#2, AC#3)

`GET /demo/sessions/{id}/events` → `text/event-stream`

```
id: 7
data: {"seq":7,"session_id":"…","ts":"2026-09-26T16:42:04.120Z","type":"assent","data":{…}}

: keepalive
```

- One frame per event, no `event:` field (the type is in the JSON).
- `seq` is per-session, strictly increasing, and equals the SSE `id`.
- **Replay:** on connect, first send every stored event of the session with `seq` greater than `Last-Event-ID` (automatic on reconnect) or the `after` query parameter (first connect, the site sends `after=0`), then stream live. The client drops any `seq` it has already applied, so an overlap is harmless.
- Send `: keepalive` every 15 s. Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- `ts` is when the decision happened on the server. The site renders an event as soon as it arrives; TASK-43 AC#3 needs it on screen within 1 s, so emit right at the decision point, not batched at turn end.
- Emitting must not add latency to the voice turn: put events on an in-process queue and never await a slow consumer on the TTFT path.

### Event catalogue

Every event: `{seq, session_id, ts, type, data}`.

| `type` | `data` | Emit when (suggested code point) |
|---|---|---|
| `user_turn` | `{turn_id, text, channel: "voice"\|"text"}` | the user turn is persisted (`DelegatorService.prepare`) |
| `assistant_turn` | `{turn_id, text, interrupted, ttft_ms?}` | the assistant turn is persisted (`_finalize`). For voice, `text` is what ElevenLabs recorded (truncated on barge-in), `interrupted` = `metadata.interrupted` |
| `scope_refusal` | `{turn_id, utterance, category, reason}` | `ScopeHook` flags the turn (`refused: true`) |
| `current_idea` | `{idea_id, title, status, change: "created"\|"switched"\|"recalled", edges: IdeaEdge[]}` | an idea is created, focused (`focus_idea`) or recalled (`recall`) |
| `ready_gate` | `{idea_id, verdict: "keep_talking"\|"ready_to_execute", reason?}` | the ready score is logged (`router_decisions`) |
| `proposal` | `{proposal_id, idea_id, goal, scope_excludes, artifact_kind, readback}` | `propose_commitment` stores the pending proposal. `readback` is the server-built text |
| `assent` | `{proposal_id, turn_id, label, utterance}` | `CommitmentHook` classifies the answer. `label` ∈ `affirmative`, `hedge`, `negative`, `new_information`; `utterance` verbatim |
| `proposal_dropped` | `{proposal_id, reason}` | the proposal is discarded without dispatch. `reason` ∈ `readback_interrupted`, `not_affirmative`, `scope_guard`, `expired` |
| `dispatch` | `{proposal_id, commitment_id, task_id, workflow_id, kind, idea_id}` | the commitment row is written and `start_task` enqueued. `kind` is the executor capability as a plain string (`research`, `code`, …) |
| `task_status` | `{task_id, status: "queued"\|"running"\|"succeeded"\|"failed", error?}` | the task row changes status (the executor worker is a separate process, so it needs a way to reach the session's stream: a Postgres `NOTIFY`, or the Delegator polling `tasks` for sessions with an open stream) |
| `artifact_delivered` | `{artifact_id, task_id, idea_id, title, summary, markdown, sources: [{title, url}]}` | `complete_task_step` writes the artifact |

Ordering for the scripted test in TASK-42 AC#7 (propose, hedge, then assent and dispatch):

```
user_turn → current_idea → ready_gate(ready_to_execute) → proposal → assistant_turn(read-back)
user_turn("sure, I guess") → assent(hedge) → proposal_dropped(not_affirmative) → assistant_turn
… → proposal → assistant_turn(read-back)
user_turn("yes, go ahead") → assent(affirmative) → dispatch → task_status(queued) → assistant_turn
task_status(running) → task_status(succeeded) → artifact_delivered
```

The worker may report `running` before the Delegator emits `dispatch` (the commit runs in a detached task). The client handles that: a task's status never moves backwards and a later `dispatch` fills in the rest of the card.

## Read endpoints (TASK-42 AC#4)

| Method and path | Response |
|---|---|
| `GET /demo/ideas` | `{ideas: [{id, title, summary, status, created_at, updated_at}], edges: [{from_idea, to_idea, relation}]}` |
| `GET /demo/commitments` | `{commitments: [{id, idea_id, goal, scope_excludes, artifact_kind, readback_text, assent_utterance, assented_at, created_at}]}` |
| `GET /demo/tasks[?session_id=]` | `{tasks: [{id, commitment_id, kind, status, dbos_workflow_id, started_at, finished_at, error, artifact_id}]}` |
| `GET /demo/artifacts/{id}` | `{id, task_id, idea_id, kind, title, summary, markdown, sources, created_at}` or `404` |

`markdown` is the stored brief (`artifacts/<idea_id>/<task_id>.md`). The site renders it with raw HTML disabled, images replaced by placeholders and only http(s)/mailto links kept, so the server does not need to sanitize it, but it must not inline secrets or local file paths. `sources` should be the executor's cited URLs; if they only exist inside the Markdown today, extract them when the artifact is written.

## Errors

`{"error": "<code>", "message": "<optional, safe to show>"}` with codes `unauthorized` (401), `not_found` (404), `voice_busy` (409), `invalid_request` (400/422), `rate_limited` (429), `voice_unavailable` (503).

## Open questions for TASK-42

1. `proposal_dropped` is not in TASK-42's AC#2 list but is needed so a read-back interrupted by barge-in does not show as "waiting for a yes" forever. It is cheap to emit from the same place that drops the proposal.
2. Task progress lives in the executor worker (port 8001). Which channel carries `task_status` and `artifact_delivered` back to the session stream: `NOTIFY`/`LISTEN`, or polling from the Delegator?
3. Should a guided scenario reuse one session or start a new one? The site starts a new session per scenario so the trace stays readable; recall across sessions is then the point of the recall scenario.
4. `ready_gate` in phase 1 is scored after 3 s of idle and is only a hint. If it is not scored before a proposal, the site simply shows the proposal; no ordering dependency.
