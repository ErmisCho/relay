# relay architecture (Phase 1)

This document describes the code on `feat/phase-1`. For the product rationale, see [`SPEC.md`](../SPEC.md). For how to run it, see the [README](../README.md).

## System diagram

```
 your Mac                                                     ElevenLabs cloud
 ┌──────────────────────────────────────────┐                ┌──────────────────────────────┐
 │ relay.client                             │  websocket     │ ElevenLabs Agent (private,   │
 │  WakeListener ── openWakeWord            │  (signed URL)  │ enable_auth)                 │
 │   "hey jarvis" → hands mic over →        │ ─────────────► │  STT · VAD · turn-taking ·   │
 │  ElevenLabsVoiceSession                  │ ◄───────────── │  barge-in · TTS (Flash v2)   │
 │   sounddevice I/O + echo gate            │   audio        │  custom LLM = relay Delegator│
 │   silence watchdog → sessions.ended_at   │                └──────────────┬───────────────┘
 └──────────────────────────────────────────┘                               │ POST …/chat/completions
                                                                            │ Bearer <shared secret>, SSE
 ┌──────────────────────────────────────────────────────────────┐          ▼
 │ relay.delegator  (FastAPI, 127.0.0.1:8000)  ◄──────── ngrok (https://<domain>.ngrok-free.app)
 │  route aliases: /v1/chat/completions, /chat/completions,      │
 │                 /v1/v1/chat/completions, /v1                  │
 │  bearer auth → DelegatorService                               │
 │   hooks (wiring.build_hooks, in order):                       │
 │     ScopeHook → CommitmentHook → ReportsHook → IdeaSummaryHook│
 │   internal tools (wiring.build_registry):                     │
 │     get_status, recall, focus_idea, link_ideas,               │
 │     propose_commitment, dispatch_task                         │
 │   FallbackChatModel ──────────────► Ollama gemma4:e4b (local) │
 │   dispatch_task → start_task (DBOSClient, enqueue only)       │
 └───────────────┬───────────────────────────────────────────────┘
                 │ enqueue workflow "relay.run_task" on queue "relay-tasks"
                 ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ relay.executor worker (FastAPI + DBOS, 127.0.0.1:8001)       │
 │  run_task (durable workflow)                                 │
 │   └ executor runner (executor/agent), kind "research"        │
 │       route step: easy/hard (router gemma4:e4b)              │
 │       workspace step: ~/relay-projects/<slug>-<id8>          │
 │       child workflow relay.research.run:                     │
 │         Pydantic AI Agent + DBOSDurability                   │
 │         easy: gemma4:e4b · hard: gpt-6-luna → qwen3.8        │
 │         pydantic-ai-harness tools (each call a DBOS step):   │
 │           Researcher: web_search, web_fetch (SSRF-safe)      │
 │           Coder: file tools + shell, in the project folder   │
 │     → render step: artifacts/<idea_id>/<task_id>.md          │
 │   → artifacts row, idea "delivered", pending_reports row     │
 └───────────────┬──────────────────────────────────────────────┘
                 ▼
 ┌──────────────────────────────────────────────────────────────┐
 │ Postgres 16 (docker, localhost:55432, db "relay")            │
 │  public: ideas, sessions, turns, commitments, tasks,         │
 │          artifacts, idea_edges, router_decisions,            │
 │          pending_reports                                     │
 │  dbos:   DBOS system tables (workflow status, queue, steps)  │
 └──────────────────────────────────────────────────────────────┘
        ▲ ReportsHook offers undelivered pending_reports at the next user turn
```

Components and where they live:

| Component | Code | Notes |
|-----------|------|-------|
| Wake listener | `src/relay/client/listener.py`, `wake.py` | States: `listening → starting → active → closing`. Writes `sessions` (with `wake_trigger`) and sets `ended_at`. Closes the microphone before the voice session opens its own. |
| Voice session | `src/relay/client/elevenlabs_session.py` | The only module that imports the ElevenLabs SDK. Uses the websocket `Conversation` with `requires_auth=True`, and sends the session id as a dynamic variable and in `custom_llm_extra_body`. While the listener waits for the wake word, it prefetches a fresh signed URL (`prefetch()`), so a trigger only has to open the websocket. A failed or stale prefetch falls back to fetching the URL on demand. The session log line says which one was used. |
| Agent config | `config/elevenlabs/agent.json`, `scripts/apply_agent_config.py` | Custom LLM at `${DELEGATOR_PUBLIC_URL}/v1`, `end_call` system tool, `turn_eagerness: patient`, backchannel ignore terms, server silence timeout = `SILENCE_TIMEOUT_S` + 15 s, `enable_auth: true`. |
| Delegator | `src/relay/delegator/app.py`, `service.py` | OpenAI-compatible chat completions, streamed or not. Internal tools run server-side and never reach the stream. ElevenLabs' own tools (for example `end_call`) are passed through as `tool_calls`. |
| Chat models | `src/relay/delegator/llm/` | `DELEGATOR_MODEL` wrapped in a `FallbackChatModel` (`DELEGATOR_FALLBACK_MODEL`). It hands over if the primary fails or sends nothing within 5 s (30 s when both refs are the same model). |
| Dispatch | `src/relay/executor/dispatch.py`, `common.py` | `start_task` is idempotent per commitment (row lock plus the deterministic workflow id `task-<id>` with DBOS "return existing"). The Delegator process only holds a `DBOSClient`. |
| Worker | `src/relay/executor/worker.py`, `workflows.py` | Launches DBOS with its system tables in the `dbos` schema of the same database. It recovers pending workflows and re-enqueues orphaned `queued` tasks on startup. |
| Executor runner | `src/relay/executor/agent/` | One general executor, registered as the durable runner for the `research` kind (the persisted kind name is unchanged; `code` is not enabled yet, TASK-33). A route step classifies the task easy or hard once (TASK-46). Easy runs on `RESEARCH_EASY_MODEL`; hard runs on `RESEARCH_HARD_MODEL` (default `openai:gpt-6-luna`) with `RESEARCH_HARD_FALLBACK_MODEL` behind it. Tools come from pydantic-ai-harness: the `Researcher` (web search and web fetch) and the `Coder` set (file read/write/edit and a shell). All of them form one per-run dynamic toolset, so every tool call is a DBOS step. Up to 30 model requests per run. The wall-clock timeout (`RELAY_RESEARCH_TIMEOUT_S`, default 20 min) is persisted by DBOS. DBOS workflow and step names keep their `relay.research.*` values. |
| Project folders | `src/relay/executor/workspace.py` | Each idea gets its own folder `<EXECUTOR_PROJECTS_ROOT>/<slug>-<first 8 hex of idea id>` (default root `~/relay-projects`). The folder is keyed by idea id, so a renamed idea keeps its folder. The file tools and the shell are rooted there. The root must not be `/`, your home directory itself, or anywhere inside the relay repo. |

## One voice turn

ElevenLabs sends the full conversation history on every request. `DelegatorService` handles one request as follows (`service.py`):

1. **Prepare** (`prepare`)
   - Resolve the session id in this order: `elevenlabs_extra_body.session_id`, then top-level `session_id`, then the `X-Relay-Session-Id` header, then the ElevenLabs conversation id. If none is present, the id is a hash of the conversation's stable opening (a warning is logged). If that is missing too, a fresh uuid is used.
   - Wait up to 1 s for the same session's previous turn to finish finalizing, so hooks see turns in order.
   - Persist the user turn to `turns` and increment `user_turn_index`.
2. **Hooks** (`before_model`, in registration order). Each hook returns short per-turn system notes, which are inserted right before the latest user message.
   - `ScopeHook` adds a refusal note if the utterance is clearly out of scope.
   - `CommitmentHook` checks read-back delivery and classifies assent (see below).
   - `ReportsHook` offers finished-task reports.
   - `IdeaSummaryHook` names the current idea.
3. **Static system prefix.** Hooks that implement `SystemPrefixProvider` (the scope rules and the commitment-protocol rules) contribute fixed text. That text goes right after the conversation's leading system message(s), a position that is identical on every request of a call. The local model can therefore reuse its prompt cache. Per-turn notes stay small and sit next to the user message. Measured on gemma4:e4b with full wiring: TTFT was about 0.32–0.38 s after the first turn (TASK-29 notes). Before this change, when the notes sat before the latest user message, the tail was re-processed every turn at about 1 s (`_with_system_prefix` docstring; about 1.1–1.3 s by coordinator measurement).
4. **Tool loop** (`_run`)
   - Stream from the chat model. Internal tool calls are executed and fed back, up to 5 rounds.
   - External (ElevenLabs) tool calls are emitted to the stream.
   - If the model fails before any text, a short spoken apology is sent instead.
5. **Stream.** SSE `chat.completion.chunk` frames, a finish chunk and `data: [DONE]` (headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`).
6. **Detached finalize** (`_finalize`). This runs in a task that is not tied to the request, so a barge-in (client disconnect) cannot cancel it.
   - Log `delegator turn session=… ttft_ms=… model=…`.
   - Persist the assistant turn. The row stores `latency_ms` = TTFT and `metadata.interrupted=true` if streaming did not complete.
   - Run each hook's `after_response`.

Background work that needs the model waits until the session is idle, because local Ollama serves one request at a time:

- The `ready` score (a hint only, logged to `router_decisions` with `backend='frontier'`) runs after 3 s of idle.
- Idea summaries run after 20 s of idle, every 6 user turns or when the idea switches.

Both are cancelled as soon as a new request arrives.

## Commitment protocol

The invariant (SPEC §6): the gate may only ever *propose*. Dispatch requires explicit spoken assent to a spoken read-back. The code is in `src/relay/delegator/commitment/`.

1. **Propose.** The model calls `propose_commitment(goal, scope_excludes, artifact_kind)`.
   - The scope guard runs first.
   - The server builds the read-back; the model does not write it:
     `Just to confirm: I'll <goal>, leaving out <exclusion>, and I'll leave it as a document for you to read. Should I start on it?`
     It always names the goal, the exclusion and the artifact. The closing question avoids the words people answer with, so an echo of it cannot read as assent.
   - The proposal is kept only in in-memory session state. The model is told to speak the read-back word for word.
2. **Delivery check.** On the next user turn, `CommitmentHook.before_model` requires two things:
   - the proposing request streamed to completion, and
   - the assistant message *as ElevenLabs recorded it* ends with the full read-back (`readback_delivered`). A barge-in truncates the recorded message, so an interrupted read-back fails this check. The proposal is dropped and the model is told not to dispatch.
3. **Assent classifier** (`classify.py`). Labels: `affirmative | hedge | negative | new_information`.
   - A deterministic pre-check runs first. It can only move the result *away* from affirmative: negatives, hedges, questions, ellipses and echoes of the read-back return immediately without a model call.
   - Otherwise `ASSENT_MODEL` answers with strict JSON within 4 s.
   - Timeouts, errors and unparseable output all become `hedge`. Anything other than `affirmative` cancels the proposal.
4. **Dispatch** (server-side authorisation only). `dispatch_task` takes no arguments. `authorise()` checks all of the following:
   - a pending proposal exists,
   - it was not already dispatched,
   - an assent record exists for it from the user turn *immediately after* the proposal,
   - the read-back was delivered,
   - the label is `affirmative`,
   - the scope guard still passes for the stored proposal.

   Anything else is rejected, and the model is told not to claim that work started.
5. **Record.**
   - The `commitments` row stores `goal`, `scope_excludes`, `artifact_kind`, `readback_text` and the verbatim `assent_utterance` (NOT NULL), plus `assented_at`.
   - The idea is marked `committed`, then `start_task` enqueues the workflow.
   - On startup the Delegator starts any commitment that has no task (`reconcile.py`).

**Re-sent turns and the dispatch grace window.** This part is still being finalized. <!-- REVIEW: commitment fixes in progress --> After a false barge-in, ElevenLabs can send the same user turn again, sometimes extended ("Yes" → "Yes, but what about Europe?"). The intended design:

- The Delegator treats a request as a re-send when the history before the user message is identical and the new utterance equals or extends the previous one. It then keeps the same user-turn index and updates the same `turns` row (`metadata.resent_from`).
- `dispatch_task` only *reserves* the dispatch. Nothing is written until the turn has settled: either its answer finished streaming, or a grace window (`DISPATCH_GRACE_S`, currently 2 s) passed with no re-send.
- A re-send is classified again. An affirmative re-send restarts the window. Any other label cancels the reservation, and the model tells the user the work did not start.
- The commit and `start_task` run in a detached task, which is flushed on shutdown.

## Completion reports

`complete_task_step` writes the artifact, marks the task `succeeded` and the idea `delivered`, and inserts a `pending_reports` row with a one-sentence summary. This happens in one transaction and at most once.

`ReportsHook` offers undelivered reports as a system note, but only at the start of a new user turn. An announcement can therefore never interrupt the agent mid-answer.

A report counts as delivered only if the summary sentence appears, word for word, in the generated text *and* in the assistant message that ElevenLabs recorded as spoken. An interrupted announcement is offered again at the next turn boundary (decision-3), at most twice. After that it is marked delivered and logged; the link stays in the idea graph and in `get_status`.

## Safety rails

| Rail | Where | What it does |
|------|-------|--------------|
| Scope boundary, layer 1 | `prompts/system.md`, rendered by `scope.render_scope_prompt` | Lists the enabled verticals (research and writing only by default) and the out-of-scope categories (email, calendars, messaging, browser/GUI, purchases, anything irreversible). Refusals start with exactly "I can't do that yet" and say the feature might come in a future version. Drafting an email for the user to send is refused, not turned into a document (decision-4). |
| Scope boundary, layer 2 | `scope.validate_commitment_args` / `scope_guard` | Server-side. Rejects any commitment whose kind or artifact is outside the enums, mismatched (for example research + pull_request), or not in `ENABLED_KINDS`, whatever the model emitted. |
| Scope boundary, layer 3 | `scope.detect_out_of_scope`, `hooks/scope.py` | A deterministic detector tuned for precision. It adds a pointed refusal note and records `{"refused": true, "refusal_reason": …}` in the user turn's `metadata`. |
| Private agent | `agent.json` `platform_settings.auth.enable_auth: true` | Only signed-URL sessions can start. The client always connects with `requires_auth=True`. |
| Shared secret | `delegator/auth.py` | Every chat route requires `Authorization: Bearer <DELEGATOR_SHARED_SECRET>`, compared in constant time; otherwise 401. The Delegator refuses to start with an empty secret, or with the dev placeholder unless `RELAY_ALLOW_DEV_SECRET=1`. `apply_agent_config.py --apply` refuses the placeholder and localhost URLs, with no opt-in. |
| SSRF-safe web fetch | harness `WebFetch(local=True)` via `Researcher`, `executor/agent/agent.py` | The hand-rolled `fetch_url` is gone. When the model has no native fetch, the local fallback uses pydantic-ai's `safe_download`, which resolves the host and refuses private and internal addresses, including cloud metadata. Redirects are followed by hand and checked each time. `tests/executor/agent/test_tools.py` checks that a private address is refused. The agent has no send, post or publish tools. |
| Shell environment | `executor/agent/agent.py` (`shell_env`, `SHELL_DENIED_ENV_PATTERNS`) | The shell does not inherit the worker's environment. It gets only `PATH`, `HOME`, `LANG` and `TMPDIR`. Patterns such as `DATABASE_URL`, `DELEGATOR_*`, `ELEVENLABS_*`, `*_API_KEY`, `*_SECRET` and `*_TOKEN` are stripped as well. `gh` and `glab` are denied. This is not a sandbox: other commands, including `git push` through a credential helper, still work. |
| Deliberately off | `executor/agent/agent.py` (`workspace_capabilities`) | Sub-agents are off: local models are slow and each delegation multiplies model calls. Repo context is off: it turns every model request into a streamed one, which changes the durable step names, and a fresh project folder has nothing to load. Both are to be revisited (a frontier default executor model; TASK-33 working in existing repos). |
| Executor timeout | `executor/agent/runner.py` | `RELAY_RESEARCH_TIMEOUT_S` (default 1200 s) is set as a DBOS workflow timeout, so it holds across a crash and recovery. A timed-out task is marked `failed`. |
| Fail-safe state | `delegator/contracts.py` | Pending proposals live only in memory. A restart or an LRU eviction drops them, which means no dispatch. |

## Key decisions

| Decision | Summary |
|----------|---------|
| [decision-1](../backlog/decisions/) | Wake phrase: pretrained openWakeWord model (`hey_jarvis`), not a custom Porcupine phrase |
| [decision-2](../backlog/decisions/) | Ideas resume across devices only on explicit request (`recall`), not automatically |
| [decision-3](../backlog/decisions/) | A barge-in during a completion report re-queues the report for the next turn boundary |
| [decision-4](../backlog/decisions/) | "Draft an email" requests are refused outright; the refusal says it might be supported in a future version |

To list them: `backlog decision list --plain`.

## Known limitations

- **No WebRTC.** The installed ElevenLabs Python SDK only has the websocket transport. The transport is hidden behind `VoiceSession`, so it can be swapped later.
- **No acoustic echo cancellation.** On a headset whose mic hears its own speaker, the agent's voice reads as a user barge-in and replies get cut into roughly 1 s chunks. The echo gate makes the mic half-duplex while agent audio plays (`adaptive` by default, `on` or `off` also available). In `on` mode you cannot interrupt the agent by voice. In `adaptive` mode an interruption has to be louder than the playback by the margin.
- **Single-slot Ollama.** One local model serves one request at a time. Background model work (ready score, idea summaries) is deferred to idle periods, but a research run and a voice turn on the same Ollama instance still compete. Assent classification is on the TTFT path and is bounded to 4 s.
- **Shell is not sandboxed.** The executor shell has a scrubbed environment and a small command denylist, but no OS sandbox. The code comment says to revisit this when the code runner (TASK-33) lands.
- **Native fetch and search.** The harness uses the provider's own web search and fetch when the model supports them, and the worker's local tools otherwise (local Ollama models always take the local path). A native fetch runs on the provider's servers, which cannot reach services on the owner's machine, so relay's SSRF check is not needed there. The trade-off is that the search queries and fetched pages pass through that provider.
- **Undocumented custom-LLM URL form.** ElevenLabs does not document whether the Server URL is a base URL or the full endpoint. The Delegator therefore serves all four route aliases and logs `delegator: chat request on <path>` so you can see which one is used. See [elevenlabs-localhost-connectivity.md](elevenlabs-localhost-connectivity.md).
- **Tunnel required.** ElevenLabs Agents calls the LLM from its own cloud, so the Delegator has to be publicly reachable (ngrok). Cloudflare quick tunnels do not carry SSE.
