# Live voice testing

This is the checklist for the acceptance checks that need a real microphone, a real ElevenLabs agent and the running stack. Unit, contract and database tests cover everything else (`uv run pytest -q`).

Every voice session uses ElevenLabs agent minutes. The wake-word false-trigger run (TASK-24 DoD) does not.

## Before you start

1. Complete the README setup, then start the Delegator, the executor worker, ngrok on port **8000**, and the client. Run `apply_agent_config.py --apply` once so that `ELEVENLABS_AGENT_ID` is set.
2. Keep gemma warm: `curl http://localhost:11434/api/generate -d '{"model": "gemma4:e4b", "keep_alive": -1}'`.
3. Open a SQL shell for the checks below:

   ```bash
   docker compose exec postgres psql -U relay -d relay
   ```

4. Keep the ElevenLabs dashboard's conversation history for the agent open. Each agent message in its transcript shows whether it was `interrupted`.

### Where to look

| Source | What it shows |
|--------|---------------|
| Delegator log | `delegator: chat request on <path>` (which route alias ElevenLabs uses), `delegator turn session=<id> ttft_ms=<n> model=<m>`, `commitment: …`, `scope: refusing out-of-scope request (…)` |
| Client log/stdout | `[listening] say 'hey_jarvis'`, `wake word 'hey_jarvis' detected`, `session <id>: trigger -> start() N ms, trigger -> connected M ms`, `session <id>: N s of silence, auto-closing`, `you:` / `agent:` transcript lines |
| Executor log | DBOS workflow activity, `research <workflow-id>: model requests served by [...]` |
| Postgres | `sessions`, `turns` (`latency_ms` = TTFT, `metadata`), `commitments`, `tasks`, `artifacts`, `pending_reports`, `router_decisions` |
| ElevenLabs transcript | What was actually spoken, where replies were `interrupted`, the user transcript as STT heard it |

Useful queries:

```sql
-- latest sessions
SELECT id, started_at, ended_at, wake_trigger FROM sessions ORDER BY started_at DESC LIMIT 5;

-- one session's turns
SELECT ts, role, left(text, 80) AS text, latency_ms, model_used, metadata
FROM turns WHERE session_id = '<session-id>' ORDER BY ts;

-- TTFT p50 / p90 over one session's assistant turns
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
       percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90, count(*)
FROM turns WHERE role = 'assistant' AND latency_ms IS NOT NULL AND session_id = '<session-id>';

-- commitment audit trail
SELECT c.created_at, c.goal, c.scope_excludes, c.readback_text, c.assent_utterance,
       t.status, t.error, t.started_at, t.finished_at, a.url
FROM commitments c
LEFT JOIN tasks t ON t.commitment_id = c.id
LEFT JOIN artifacts a ON a.task_id = t.id
ORDER BY c.created_at DESC;

-- completion reports
SELECT created_at, summary, offered_at, delivered_at FROM pending_reports ORDER BY created_at DESC;
```

## Phase 1 close-out run

Do these steps in this order, in one sitting. Each step closes the ACs it names. The sections after this one explain each check in more detail.

Save the logs to files so you can grep them afterwards. Start each process with `2>&1 | tee <name>.log` (for example `delegator.log`, `client.log`, `executor.log`).

1. **Wake-word false-trigger run (TASK-24 DoD).** Do this first, before any other process holds the mic.
   - Start: nothing else. Run `uv run python scripts/measure_false_triggers.py --minutes 60`.
   - Do: live normally near the mic for an hour (talk, music, calls). Do not say the wake phrase.
   - Observe: each `TRIGGER at … s` line, then the final report.
   - Evidence: copy the report (triggers, triggers/h, max score per minute) into the TASK-24 notes.
2. **Start the stack on gpt-6-luna (TASK-44).**
   - Set `DELEGATOR_MODEL=openai:gpt-6-luna` and `DELEGATOR_FALLBACK_MODEL=ollama:gemma4:e4b` in `.env`.
   - Start the Delegator, the executor worker, ngrok on port 8000 and the client (see [Before you start](#before-you-start)).
3. **Live scope tests with gpt-6-luna (TASK-44 AC#5).** No voice needed.
   - Run: `RELAY_LLM_TESTS=1 uv run pytest -q tests/delegator/test_scope_overrefusal.py tests/delegator/test_luna.py`.
   - Evidence: the pytest summary line (passed, 0 failed, and not all skipped).
4. **Wake to connected (TASK-24 AC#1).** Say "hey jarvis" five times across the next steps.
   - Observe: the client logs `trigger -> connected M ms` each time. `M` must be under 500.
   - Evidence: `grep "trigger -> connected" client.log`
5. **Spoken round trip and session id (TASK-22 AC#4, TASK-23 AC#1, AC#3, TASK-44 AC#1).**
   - Do: say "hey jarvis", then hold a short conversation of a few turns.
   - Observe: the agent answers in speech; the client prints `you:` and `agent:` lines; the Delegator logs `delegator: chat request on <path>` for each turn.
   - Evidence, logs: `grep -E "chat request on|delegator turn session=" delegator.log`. The `model=` field must show gpt-6-luna. There must be no `derived session` warning: `grep -c "carries no session id" delegator.log` must print 0.
   - Evidence, database: the latest session must have a `wake_trigger`, and all its turns must use the same id. Run `uv run python scripts/phase1_audit.py --latest` and read the `session id consistency` line. `client_created` must be `True` and `derived_sessions_during_call` must be 0.
6. **Barge-in (TASK-23 AC#2).** In the same call, start talking while the agent is mid-sentence.
   - Observe: playback stops at once.
   - Evidence: the ElevenLabs transcript marks the agent message `interrupted`, and this query returns the row:

     ```sql
     SELECT ts, left(text, 60) FROM turns
     WHERE session_id = '<session-id>' AND (metadata->>'interrupted')::boolean;
     ```

7. **The TASK-31 session (TASK-31 AC#1–#4).** Say "hey jarvis" and talk for at least 10 minutes.
   - Do: talk through three separate ideas. Agree to exactly three read-backs with "yes". Add at least one hedge ("sure, I guess") and one borderline moment; these must not dispatch. End the call by saying goodbye or by staying silent until it closes.
   - Observe: three `commitment: dispatched` lines in the Delegator log, and three `tasks` rows that reach `succeeded`.
   - Wait until all three tasks have finished (the research timeout is 20 min).
   - Evidence: run the audit for that session and keep the JSON:

     ```bash
     uv run python scripts/phase1_audit.py --session <session-id> --json phase1-audit.json
     echo "exit=$?"
     ```

   - Exit 0 means no unintended dispatch (AC#2) and the session closed (AC#3). Exit 1 prints a `VIOLATION` line for each problem. Exit 2 means a usage or database error.
   - AC#1: the report must list exactly three commitments, each with `tasks=1`, and three `document` artifacts. Read the three documents and decide whether they are worth reading.
   - AC#4: copy the `cost:` line (agent minutes; tokens show `not recorded` unless the turns store them) and the `TTFT` lines (p50/p90 per model) into the TASK-31 notes.
   - The audit only reads the database. It never changes it.

## TASK-22: Delegator live round trip

- [ ] **AC#4: live round trip.** Say "hey jarvis" and hold a short spoken conversation.
  - Observe: the Delegator logs `delegator: chat request on <path>` for every agent turn. Write down the path, because it shows which URL form ElevenLabs uses. The agent answers in speech.
  - Check: `turns` has alternating `user` / `assistant` rows for the session, and the assistant rows have `model_used` set.
- [ ] **DoD#2: TTFT < 1 s p50.** Hold at least 10 turns. Use `ttft_ms` from the `delegator turn …` log lines, or run the p50 query above.
  - Expect the first turn to be slower (model load and an empty prompt cache).
  - After that, gemma4:e4b with full wiring measured about 0.32–0.38 s (TASK-29 notes).

## TASK-23: voice loop

- [ ] **AC#1: round trip through the Delegator.** This is the same call as TASK-22 AC#4. The client prints `you:` and `agent:` lines, and the Delegator logs a request for each turn.
- [ ] **AC#2: barge-in.** Start talking while the agent is mid-sentence.
  - Observe: playback stops within about one output block (62.5 ms after the SDK's interrupt).
  - Check: the ElevenLabs transcript marks that agent message `interrupted`, and the matching `turns` row has `metadata.interrupted = true`.
- [ ] **AC#3: session id on rows.** The client creates the `sessions` row with a `wake_trigger`. Every `turns` row of the call must carry that same `session_id`:

  ```sql
  SELECT s.id, s.wake_trigger, count(t.id) AS turns
  FROM sessions s LEFT JOIN turns t ON t.session_id = s.id
  GROUP BY s.id ORDER BY s.started_at DESC LIMIT 3;
  ```

  If the Delegator logs `chat request carries no session id; derived session … from the history prefix`, the extra body is not reaching it. Check that `platform_settings.overrides.custom_llm_extra_body` is `true` on the agent, then re-run `--apply`.

### Echo gate

Without acoustic echo cancellation, a headset mic that hears its own speaker makes the agent's voice look like the user barging in. You can configure the gate in `.env` or the environment:

| Setting | Values | Effect |
|---------|--------|--------|
| `RELAY_ECHO_GATE` | `adaptive` (default) | During playback, a mic chunk passes only if it is louder than the recent playback level by the margin. Deliberate loud interruptions still work. |
| | `on` | Strict half-duplex. No echo, but also no barge-in by voice while the agent speaks. |
| | `off` | Mic is streamed unchanged. Use this only with hardware echo cancellation. |
| `RELAY_ECHO_GATE_MARGIN_DB` | default `10` | Too low lets echo through. Too high means you have to shout to barge in. |

**Diagnosing chunked replies.** The first live call showed this pattern:

- In the ElevenLabs transcript, agent replies are marked `interrupted` about 1 s in, although you did not speak.
- The same user text arrives at the Delegator again. It shows up as several near-identical user rows, or as the `delegator: re-sent user turn …` log line once re-send handling lands. <!-- REVIEW: commitment fixes in progress -->

That pattern is echo. Try the following in order:

1. Keep `adaptive` and raise `RELAY_ECHO_GATE_MARGIN_DB`.
2. Switch to `RELAY_ECHO_GATE=on`.
3. Use wired headphones (see Troubleshooting).

To look at the affected rows:

```sql
SELECT ts, role, left(text, 60), metadata FROM turns
WHERE session_id = '<session-id>' AND (metadata ? 'interrupted' OR metadata ? 'resent')
ORDER BY ts;
```

## TASK-24: wake word and listener

- [ ] **AC#1: connected within 500 ms.** After each trigger the client logs `session <id>: trigger -> start() N ms, trigger -> connected M ms`. "Connected" means the voice session holds the microphone. Record `M` over several triggers.
- [ ] **AC#2: silence auto-close.** Say nothing after the greeting. After `SILENCE_TIMEOUT_S` (default 60 s) the client logs `session <id>: N s of silence, auto-closing` and returns to `[listening]`. Check that `ended_at` is set:

  ```sql
  SELECT id, started_at, ended_at, ended_at - started_at AS duration FROM sessions ORDER BY started_at DESC LIMIT 1;
  ```

  The server-side ElevenLabs timeout is `SILENCE_TIMEOUT_S + 15` s, so the client should always close first.
- [ ] **AC#3: resume.** After a close (silence, saying goodbye so the agent calls `end_call`, or the watchdog), the client prints `[listening] say 'hey_jarvis'` again. A second "hey jarvis" opens a new session with a new `sessions` row.
- [ ] **AC#4: mic hand-off.** The listener's stream is closed before the voice session starts, and it reopens only after the session releases the mic. There should be no audio-device errors. You should never see `voice session did not release the microphone; resuming anyway`. The line `voice session still holds the microphone; stopping it again` means the hand-off needed a retry.
- [ ] **DoD: 1-hour false-trigger run.** This uses no voice session and no agent minutes. Leave the mic in normal background conditions (talk, music, calls in the room) and run:

  ```bash
  uv run python scripts/measure_false_triggers.py --minutes 60
  # or on a recording: --wav background.wav  (16 kHz mono int16)
  ```

  The run prints each `TRIGGER at … s (score …)` and ends with a report: triggers, triggers/h and the max score per minute. Record the report in the TASK-24 notes. To test other thresholds, use `--threshold`.

## TASK-27 / TASK-31: commitment protocol end to end

<!-- REVIEW: commitment fixes in progress -->

- [ ] **Happy path.**
  1. Talk an idea through until it is concrete. Say what to leave out.
  2. The agent speaks the server-generated read-back: *"Just to confirm: I'll <goal>, leaving out <exclusion>, and I'll leave it as a document for you to read. Should I start on it?"*
  3. Say **"yes"**. The agent confirms that it is starting.
  4. The Delegator logs these lines, in order:
     - `commitment: proposal … session=… turn=N`
     - `commitment: assent label=affirmative source=model …`
     - `commitment: dispatched commitment=… task=… session=… assent='yes'`
  5. The executor picks up the task. `tasks.status` goes `queued → running → succeeded`.
- [ ] **Artifact and report.**
  - Minutes later (the research timeout is 20 min by default) a Markdown file appears at `artifacts/<idea_id>/<task_id>.md`, and `artifacts.url` holds it as a `file://` URL.
  - A `pending_reports` row appears.
  - The summary is announced at your **next** turn, never in the middle of an answer. `pending_reports.delivered_at` is set once the announcement is confirmed as spoken.
- [ ] **Hedge must not dispatch.** Answer a read-back with "sure, I guess" (also try "maybe" and "yeah but what about…").
  - Expect: `commitment: assent label=hedge source=precheck …`, no `dispatched` line, and no new `commitments` row. The agent keeps talking.
- [ ] **Interrupted read-back must not dispatch.** Talk over the read-back before it finishes, then say "yes".
  - Expect: `commitment: proposal … read-back not delivered`, no work is started (the model is told the plan was cancelled and must not say it started), and no new `commitments` row.
- [ ] **Out-of-scope refusal.** Ask it to "draft an email to my team".
  - Expect: a spoken refusal starting with "I can't do that yet" that says the feature might come in a future version (decision-4).
  - Also expect the log line `scope: refusing out-of-scope request (email: …)` and `metadata.refused = true` on that user turn.
- [ ] **Audit trail.** Run the commitment query above. Every row must have `readback_text` and a verbatim `assent_utterance`, and neither may be empty. For TASK-31, the 10-minute session should show exactly the three commitments you agreed to and nothing else. `scripts/phase1_audit.py` runs these checks for one session (see [Phase 1 close-out run](#phase-1-close-out-run)).

## Privacy check: unsigned connections are rejected

The agent has `enable_auth: true`, and the client always uses signed URLs (`requires_auth=True`). To confirm that an unsigned connection cannot start a session, run this text-only attempt, which opens no audio:

```bash
uv run python - <<'EOF'
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation
from relay.config import get_settings
s = get_settings()
c = Conversation(ElevenLabs(api_key=s.elevenlabs_api_key), s.elevenlabs_agent_id,
                 requires_auth=False,
                 callback_agent_response=lambda t: print("AGENT SPOKE:", t),
                 callback_end_session=lambda: print("session ended"))
c.start_session()
print("conversation id:", c.wait_for_session_end())
EOF
```

- Pass: no `AGENT SPOKE:` line (no first message).
- Fail: the agent's greeting prints. In that case `enable_auth` is not active on the agent, so re-run `apply_agent_config.py --apply`.

<!-- REVIEW: source needed --> How ElevenLabs reports the rejection (a websocket close code or an error event) has not been verified live. Record what you see.

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| The agent never answers, and the Delegator logs no `chat request` | The tunnel points at the wrong port. It must forward to the **Delegator on 8000**, not the executor on 8001. Check with `curl -N https://<domain>/healthz` (should return `{"status":"ok"}`). Also check that `DELEGATOR_PUBLIC_URL` matches the tunnel and that `--apply` was re-run after it changed. |
| Requests reach the Delegator but replies never stream, or arrive all at once | A Cloudflare quick tunnel (`cloudflared tunnel --url`) is in use. Quick tunnels do not support SSE; use ngrok. |
| 401 on every request | `DELEGATOR_SHARED_SECRET` changed after `--apply`. Re-run `--apply` so the ElevenLabs workspace secret matches. |
| Delegator exits at startup: "still the dev placeholder" | Set a real `DELEGATOR_SHARED_SECRET` (`openssl rand -hex 32`). |
| First turn is slow, later turns fast | gemma is loading cold and the prompt cache is empty. Keep the model resident with `keep_alive` (see above). The Delegator waits up to 30 s for the first token when `DELEGATOR_FALLBACK_MODEL` equals `DELEGATOR_MODEL` (5 s otherwise) before retrying on the fallback. If both fail before any text, it speaks a short apology. |
| Replies cut into ~1 s pieces, `interrupted` in the ElevenLabs transcript | Echo. See [Echo gate](#echo-gate). |
| Muffled or narrowband audio on a Bluetooth headset | With the mic open, macOS switches Bluetooth headsets to the call (hands-free) profile, which has much lower audio quality. Bluetooth output latency also lets echo arrive after playback (the gate keeps a 0.35 s hangover for this). Use a wired headset, or a separate mic with the Bluetooth device for output only (`--device` selects the input). |
| `ELEVENLABS_AGENT_ID is not set` | Run `scripts/apply_agent_config.py --apply` and put the printed id in `.env`. |
| `unknown wake model` / `wake model file … is missing` | Run `uv sync --reinstall-package openwakeword`. |
