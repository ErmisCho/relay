# relay demo API contract (TASK-42 ⇄ TASK-43)

The demo website (`web/`, TASK-43) talks to these endpoints on the Delegator (TASK-42, `src/relay/delegator/demo/`). Shapes are defined in [`src/api/contract.ts`](src/api/contract.ts); this page describes what the Delegator actually does. The in-browser mock (`src/api/mock.ts`) implements the same contract. Backend tests: `tests/delegator/demo/`.

## Serving and origin

- All demo endpoints live under `/demo/` on the Delegator, next to the `/v1/chat/completions` routes. They exist only when `DEMO_PASSCODE` is set; with it empty every `/demo` route is `404` and the event feed is off.
- When the demo is enabled and `DEMO_WEB_DIST` (default `web/dist`, relative to the Delegator's working directory) is a directory, the Delegator serves the built site at `/` (`StaticFiles(html=True)`, mounted after every API route). Site and API then share one origin, so the ngrok URL serves both, the cookie is first-party and no CORS setup is needed.
- In development Vite proxies `/demo` to `RELAY_DELEGATOR_URL` (default `http://localhost:8000`).
- The client sends `ngrok-skip-browser-warning: 1` on every `fetch`, so ngrok's free-tier interstitial does not replace JSON responses. `EventSource` cannot send headers; it relies on the browser having already loaded the page through ngrok.

## Access control (TASK-42 AC#6)

| | |
|---|---|
| `POST /demo/auth` `{ "passcode": "…" }` | `204` and `Set-Cookie: relay_demo=<issued-at>.<HMAC>; HttpOnly; SameSite=Lax; Path=/demo; Max-Age=43200`, plus `Secure` when the request came over https (directly or `X-Forwarded-Proto: https`, as behind ngrok). Wrong passcode: `401 {"error":"unauthorized"}`. After 10 wrong guesses from one address within 60 s: `429 {"error":"rate_limited"}` (even for the right passcode) until the window passes. |
| `GET /demo/auth` | `204` when the cookie is valid, else `401`. The site calls it on load to skip the gate. |
| every other `/demo/*` | `401 {"error":"unauthorized"}` without a valid cookie. |

The passcode is the Delegator's `DEMO_PASSCODE` environment variable, never in the repo. Changing it (or `DELEGATOR_SHARED_SECRET`) invalidates every issued cookie. A cookie rather than a header is required because `EventSource` cannot set headers.

## Sessions, text chat and voice

| Method and path | Response | Notes |
|---|---|---|
| `POST /demo/sessions` | `201 {session_id, created_at, voice_max_seconds}` | A demo session = a relay `sessions` row. The site creates a new one on load, on "New session" and before each guided scenario. |
| `POST /demo/sessions/{id}/messages` `{text}` | `202 {turn_id}` | 1–2000 chars. Runs through `DelegatorService.prepare` + `stream_sse`, the **same path** as a voice turn (hooks, scope gate, commitment protocol, tools). The one difference is the system prompt: typed chat uses its own short prompt ("typed in the demo website instead of spoken") because there is no ElevenLabs agent prompt. The reply arrives on the event stream as `assistant_turn`, not in this response. One text turn runs at a time per session: a second POST waits until the previous reply is finished. `turn_id` is `null` only if the store was unreachable. `422` on a bad body, `404` on an unknown session. |
| `POST /demo/sessions/{id}/voice` | `200 VoiceCredentials` | See below. `409 {"error":"voice_busy"}` while another session holds the single slot (the holder may call again to refresh); `503 {"error":"voice_unavailable","message":"…"}` if ElevenLabs is not configured or token issuance failed. |
| `DELETE /demo/sessions/{id}/voice` | `204` | Sent when the viewer ends the call or the client-side timer runs out. Releases the slot. The slot also frees itself `max_duration_s` after the last acquire. |

`VoiceCredentials` (the ElevenLabs API key never leaves the server):

```json
{ "transport": "webrtc", "conversation_token": "…", "expires_at": "2026-09-26T16:52:04.000Z", "max_duration_s": 600 }
{ "transport": "websocket", "signed_url": "wss://api.elevenlabs.io/…", "expires_at": "…", "max_duration_s": 600 }
```

- `webrtc` comes from ElevenLabs `GET /v1/convai/conversation/token?agent_id=…`; `websocket` (fallback when the token call fails) from `GET /v1/convai/conversation/get-signed-url?agent_id=…`.
- `expires_at` is the server's conservative estimate, now + 600 s; ElevenLabs does not document the real lifetime.
- `max_duration_s` is `DEMO_MAX_VOICE_SECONDS` (default 600). Past it, the Delegator tells the agent on each further turn to say goodbye and call `end_call` (best effort, for a tab that keeps the call open).
- The browser starts the session with `dynamicVariables: {session_id}` **and** `customLlmExtraBody: {session_id}`, the same two channels the Python client uses, so the Delegator attributes the voice turns to this demo session and emits their events on its stream.

## Live event stream (TASK-42 AC#2, AC#3)

`GET /demo/sessions/{id}/events` → `text/event-stream`

```
id: 7
data: {"seq":7,"session_id":"…","ts":"2026-09-26T16:42:04.120Z","type":"assent","data":{…}}

: keepalive
```

- One frame per event, no `event:` field (the type is in the JSON).
- `seq` is per-session, strictly increasing from 1, and equals the SSE `id`.
- **Replay:** on connect the server first sends every stored event of the session with `seq` greater than `Last-Event-ID` (automatic on reconnect) or the `after` query parameter (first connect, the site sends `after=0`), then streams live. The client drops any `seq` it has already applied, so an overlap is harmless.
- **Replay is in memory only**: up to 2000 events per session for the 64 most recently active sessions. A Delegator restart loses the history (a reload then shows an empty trace; the read endpoints below still show everything from the database).
- `: keepalive` every 15 s. Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`.
- `ts` is when the decision happened on the server. Emitting is synchronous and O(1) (a bounded per-subscriber queue that drops its oldest event for a slow consumer), so nothing on the voice turn waits for a browser; measured TTFT overhead is under 1% (TASK-42 AC#3).

### Event catalogue

Every event: `{seq, session_id, ts, type, data}`. Ids are UUID strings.

| `type` | `data` | Emitted when |
|---|---|---|
| `user_turn` | `{turn_id, text, channel: "voice"\|"text"}` | the user turn is persisted (`DelegatorService.prepare`); not again for an ElevenLabs re-send of the same turn |
| `assistant_turn` | `{turn_id\|null, text, interrupted, ttft_ms}` | the assistant turn is persisted (`_finalize`). `interrupted` = the response stream did not complete (barge-in) |
| `scope_refusal` | `{turn_id\|null, utterance, category, reason}` | `ScopeHook` flags the turn. `reason` is one sentence naming the category |
| `current_idea` | `{idea_id, title, status, change: "created"\|"switched", edges: IdeaEdge[]}` | the `focus_idea` tool creates or focuses an idea. **Only `focus_idea` emits it**: `recall` does not, so `change: "recalled"` is never sent today |
| `ready_gate` | `{idea_id\|null, verdict: "keep_talking"\|"ready_to_execute", reason: null}` | the ready score is written to `router_decisions`. It is batch-scored once the session has been idle `READY_IDLE_S` (20 s), so it arrives after the `assistant_turn` it belongs to (a new user turn postpones the scoring). `reason` is always `null` in phase 1 |
| `proposal` | `{proposal_id, idea_id\|null, goal, scope_excludes, artifact_kind, readback}` | `propose_commitment` stores the pending proposal. `readback` is the server-built text. Every proposal, including a re-proposal after a hedge, gets a new `proposal_id` |
| `assent` | `{proposal_id, turn_id\|null, label, utterance}` | `CommitmentHook` classifies the answer. `label` ∈ `affirmative`, `hedge`, `negative`, `new_information`; `utterance` verbatim. Also emitted when a later turn is judged against a dispatch still inside its grace window |
| `proposal_dropped` | `{proposal_id, reason}` | the proposal is discarded without dispatch. `reason` ∈ `readback_interrupted`, `not_affirmative`, `scope_guard`, `expired` |
| `dispatch` | `{proposal_id, commitment_id, task_id, workflow_id, kind, idea_id\|null}` | the commitment row is committed and the task enqueued. This happens after a grace period (`DISPATCH_GRACE_S`, 3.5 s) that lets a quick "wait, no" cancel it, so it comes **after** the `assistant_turn` that confirms the dispatch. `kind` is the executor capability (`research`, `code`, …) |
| `task_status` | `{task_id, status: "queued"\|"running"\|"succeeded"\|"failed", error}` | `queued` right after `dispatch`; later changes come from the Delegator polling the `tasks` rows (every `DEMO_TASK_POLL_S`, 0.5 s) of the tasks **this Delegator process** dispatched. `succeeded` and `failed` are final |
| `artifact_delivered` | `{artifact_id, task_id, idea_id\|null, title, summary, markdown, sources: [{title, url}]}` | the poller sees the task `succeeded` **and** its `artifacts` row (it waits up to 40 polls for the row). `title` is the brief's first `# ` heading, else the artifact summary. `sources` are the `[title](http…)` links found in the Markdown, de-duplicated by URL, in order |

Order for a scripted conversation (TASK-42 AC#7, `tests/delegator/demo/test_demo.py`) that proposes, gets a hedge, then assent and dispatch:

```
user_turn → [current_idea] → proposal(P1) → assistant_turn(read-back)
user_turn("hmm, maybe") → assent(P1, hedge) → proposal_dropped(P1, not_affirmative) → assistant_turn
user_turn → proposal(P2) → assistant_turn(read-back)
user_turn("yes, go ahead") → assent(P2, affirmative) → assistant_turn("On it.") → dispatch(P2) → task_status(queued)
… task_status(running) → task_status(succeeded) → artifact_delivered
[ready_gate] whenever the session has been idle ~20 s
```

The client tolerates other orders too: a worker `running` that arrives before `dispatch` is kept (status never moves backwards, a finished task never changes), and a later `dispatch` fills in the rest of the card. A re-proposal that reuses a known `proposal_id` puts that proposal back to "waiting for a yes".

## Read endpoints (TASK-42 AC#4)

| Method and path | Response |
|---|---|
| `GET /demo/ideas` | `{ideas: [{id, title, summary, status, created_at, updated_at}], edges: [{from_idea, to_idea, relation}]}`, newest update first, every idea and edge in the store |
| `GET /demo/commitments` | `{commitments: [{id, idea_id, goal, scope_excludes, artifact_kind, readback_text, assent_utterance, assented_at, created_at}]}`, newest first |
| `GET /demo/tasks[?session_id=]` | `{tasks: [{id, commitment_id, kind, status, dbos_workflow_id, started_at, finished_at, error, artifact_id}]}`, newest first. Tasks carry no session id: `session_id` filters to the tasks this Delegator dispatched for the session (in-memory) plus those with a `pending_reports` row for it |
| `GET /demo/artifacts/{id}` | `{id, task_id, idea_id, kind, title, summary, markdown, sources, created_at}`, `404 {"error":"not_found"}` for an unknown or malformed id |

`markdown` is read from the artifact's `file://` URL only if that file lies inside `ARTIFACTS_DIR`; anything else yields `""` (and no sources). The site renders the Markdown with raw HTML disabled, images replaced by placeholders and only http(s)/mailto links kept.

## Errors

`{"error": "<code>", "message": "<optional, safe to show>"}` with codes `unauthorized` (401), `not_found` (404), `voice_busy` (409), `invalid_request` (422), `rate_limited` (429), `voice_unavailable` (503).

## Known gaps (not blocking the demo)

1. `recall` does not emit `current_idea` (`change: "recalled"`); the recall scenario shows the recall only in the transcript. Emitting it belongs in `src/relay/delegator/tools/ideas.py`.
2. `ready_gate.reason` is always `null`; the ready scorer returns only a label.
3. Event history does not survive a Delegator restart (in memory by design).
4. Task progress is polled, not pushed (`LISTEN/NOTIFY` would be the next step if many sessions run at once).
