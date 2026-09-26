# Voice-First Ideation Agent — Technical Specification

**Status:** draft v1 · **Date:** 2026-09-26
**One line:** A wake-word voice agent you think out loud with; when an idea is mutually agreed to be ready, it delegates execution to background agents and hands back a reviewable artifact.

---

## 1. Validation summary

### Why this is a better premise than rabbit.tech

Rabbit sold *speak → instant action*, which requires the action layer to be correct on the first turn. Their Large Action Model "failed to deliver consistent and reliable results… misinterpreted commands, stalled, or produced unpredictable outputs," and shipped with four working app integrations against a universal promise.

This product sells *speak → keep talking → act once we agree*. Conversation becomes the error-correction mechanism **before** any action is taken. That structurally converts rabbit's fatal failure mode into a feature: nothing executes until convergence.

### The two risks that actually matter

| Risk | Why it kills the product | Mitigation in this spec |
|---|---|---|
| **Unbounded action space** | Exactly what killed rabbit. "The agent executes it" is not a scope. | Hard-bounded to two verticals (§3). Anything outside returns "I can't do that yet" — never a degraded attempt. |
| **Gate fires without real agreement** | "Agreed between the agent and me" is a *dialogue policy* problem, not a classification problem. A confidence threshold will fire on things you didn't agree to. One such event destroys trust permanently. | The gate cannot dispatch. It can only *propose*. Dispatch requires explicit spoken assent to a spoken read-back (§6). |

### Verdict

**Build it.** The premise is sound and the MVP path is short because ElevenLabs Agents removes the hardest engineering (turn-taking, barge-in, streaming TTS). The novel work is the commitment protocol and the idea graph — which is also where the defensibility lives. Ship the voice loop with a frontier model only, then add Laya routing as a cost/latency optimization once you have labeled turns.

---

## 2. Laya / Jev evaluation — measured, not estimated

`laya-mlx` was installed and benchmarked on an M4 Pro (64 GB) against 14 hand-labeled utterances representing this product's real decisions. Zero-shot, FP16, `aac6fef/laya-mlx` (421M, ModernBERT-large).

### Results

| Decision | Question type | Accuracy | Latency |
|---|---|---|---|
| Intent (5-way) | `choice` | **86%** (12/14; 6/7 on hard cases) | 58 ms / 4 questions |
| Model routing (small vs frontier) | `choice`, binary, concrete anchors | **100%** (5/5) | 32 ms / 2 questions |
| Ready-to-execute gate | `choice`, binary | **88%** (7/8) | — |
| Ready / difficulty | `score` rubric | **64% / 33%** | — |
| Turn complete | `noul` | **50%** | — |

### Three findings that drive the design

1. **The `score` head collapses zero-shot.** Every input returned 1.71–3.05 regardless of content — effectively a constant. Reframing the *identical* decisions as binary `choice` questions with descriptive criteria took routing 33% → **100%** and the gate 64% → **88%**. → **Use `choice` only. Do not use `score` until fine-tuned.**

2. **Laya must never do turn-taking.** 50% is chance, and this is structural: whether someone is mid-sentence lives in prosody and pause length, not in text. → **Turn-taking belongs to ElevenLabs' VAD. Not a Laya responsibility.**

3. **Confidence is uncalibrated and unusable for abstention.** The checkpoint ships temperature `0.1006` for the `choice:11+` bucket; the runtime clamps it to `[0.5, 5.0]` and emits a `RuntimeWarning` that affected buckets are uncalibrated. Observed confidences ranged 0.01–0.58 *even when correct*. → **Never threshold on `confidence` until calibrated on your own labeled turns (§8.3).**

### Laya over Jev

| | Jev | Laya (local, MLX) |
|---|---|---|
| Latency | 236–276 ms (API) | 7–32 ms |
| Cost | per call | zero marginal |
| License | closed | Apache 2.0 |
| Offline | no | yes |

The router is called on **every utterance** — 50+ times per conversation. Local latency and zero marginal cost win outright. Use Jev only as an offline second opinion while building the labeled set.

### Working question schemas (validated)

```python
ROUTING_QUESTIONS = {
    "difficulty": {
        "type": "choice",
        "instructions": "Pick the smallest model that could carry out this request correctly.",
        "criteria": {
            "small_local": "mechanical edit, rename, reformat, lookup, or a single unambiguous file change",
            "frontier": "requires novel design, proofs, architecture decisions, or careful multi-file reasoning where a mistake is expensive",
        },
    },
    "ready": {
        "type": "choice",
        "instructions": "Decide whether this idea can be handed to a background worker right now.",
        "criteria": {
            "keep_talking": "still exploratory - the goal or scope is not yet pinned down, or the user is still weighing options",
            "ready_to_execute": "the goal and scope are explicit and the user has signalled agreement to proceed",
        },
    },
}
```

Rules derived from measurement: **binary or near-binary** choices; **descriptive** criteria (a sentence of positive evidence), never bare labels; ≤10 options (upstream documents degraded performance above 20); 512-token context on the English checkpoint.

---

## 3. Scope

### In scope (v1)

| Vertical | Dispatched work | Terminal artifact |
|---|---|---|
| **Code & repos** | Write code, refactor, run tests, open PRs | Pull request on a branch — **never merged** |
| **Research & writing** | Web research, synthesis, briefs, drafts, competitive analysis | Markdown document — **never sent or published** |

### Explicitly out of scope (v1)

Comms & scheduling (email, calendar, Slack) · computer/browser use and GUI automation · anything outward-facing or irreversible · dedicated hardware.

**Refusal is a feature.** Out-of-scope requests get a clear "I can't do that yet" — never a partial attempt. This is the single most important lesson from rabbit.

---

## 4. Architecture

```
┌────────────────────────────────────────────────────────────────┐
│ CLIENT (any OS — Mac / Windows / Linux)                        │
│                                                                │
│  openWakeWord (local, CPU) ──► opens session                   │
│         │                                                      │
│         ▼                                                      │
│  ElevenLabs Agents SDK (WebRTC)                                │
│  audio in/out · VAD · turn-taking · barge-in · streaming TTS    │
└───────────────────────────┬────────────────────────────────────┘
                            │ ElevenLabs calls YOUR endpoint
                            │ (OpenAI-compatible, SSE, function calling)
                            ▼
┌────────────────────────────────────────────────────────────────┐
│ DELEGATOR — custom LLM endpoint (FastAPI)          ⟵ the seam   │
│                                                                │
│  1. Laya router (phase 2)  — 32 ms, local or cloud            │
│  2. Model selection: small_local → Ollama | frontier → API     │
│  3. Tools: propose_commitment · dispatch · status · recall     │
│  4. Idea-graph read/write                                      │
└───────────────┬────────────────────────────────┬───────────────┘
                │ dispatch (async, non-blocking) │ read/write
                ▼                                ▼
┌───────────────────────────────┐   ┌────────────────────────────┐
│ EXECUTOR — Pydantic AI + DBOS │   │ IDEA GRAPH (Postgres)      │
│ durable · typed · resumable   │   │ ideas · turns · commitments│
│ code agent  │ research agent  │   │ tasks · artifacts          │
└───────────────────────────────┘   └────────────────────────────┘
```

### The key architectural decision

ElevenLabs' custom-LLM hook requires an **OpenAI-compatible endpoint** (`/v1/chat/completions` or `/v1/responses`), **SSE streaming** (`Content-Type: text/event-stream`, chunks as `data: {json}\n\n`, terminated by `data: [DONE]\n\n`), and **function-calling support** in OpenAI format.

Therefore: the Delegator *is* the LLM from ElevenLabs' point of view. Routing, gating and memory all happen inside one process behind a single seam — no extra network hop, no ElevenLabs-side tool round-trip for routing decisions. This is why Laya can be added in phase 2 without touching the voice layer at all.

---

## 5. Components

| Component | Choice | Rationale |
|---|---|---|
| Wake word | **openWakeWord** (local, CPU) | Not native to ElevenLabs — must be client-side. No API key, no metering; a single Pi 3 core runs 15–20 models real-time. Porcupine if you need a trained custom phrase. |
| Voice loop | **ElevenLabs Agents** | Owns STT, VAD, turn-taking, barge-in, TTS. Flash v2.5 is sub-300 ms across 32 languages. Removes the hardest problem in the stack. |
| Delegator | **FastAPI**, OpenAI-compatible SSE | Required shape for the custom-LLM hook. |
| Router | **Laya** via `laya-mlx` (Apple Silicon) / `laya` + ONNX elsewhere | Phase 2. Binary `choice` only. |
| Easy-turn LLM | **Ollama** — `gemma4:e4b`, `glm-4.7-flash` | Already present on the dev machine. Opportunistic: used only when the device can handle it. |
| Hard-turn LLM | **Frontier API** (`claude-opus-5` / `claude-sonnet-5`) | Default path. Cloud-first per requirement. |
| Executor | **Pydantic AI** + **DBOS** durable execution | Typed end-to-end; model-agnostic so it inherits the same routing. DBOS is database-backed with no external workflow engine — right weight for an MVP. Temporal is available if you outgrow it. |
| Store | **Postgres** | DBOS already needs it; the idea graph shares it. |

### Capability detection (modularity requirement)

Cloud is the default; local is an opportunistic optimization. At startup the client probes for Apple Silicon + MLX, an Ollama daemon and free VRAM/RAM, then publishes a capability profile. The Delegator consults it:

```
route = laya.decide(utterance)          # small_local | frontier
if route == "small_local" and caps.can_run_local:
    model = caps.local_model            # Ollama
else:
    model = FRONTIER_MODEL              # always available
```

A device with no local capability is fully functional — it just routes everything to cloud. **Local inference is never on the critical path for correctness.**

---

## 6. The commitment protocol

This is the product. Implement it exactly; do not shortcut it to a threshold.

### Invariant

> **The gate may only ever *propose*. Dispatch requires explicit spoken assent to a spoken read-back.**

### Flow

1. **Score** — every user turn, Laya (or the frontier model in phase 1) answers `ready`: `keep_talking` | `ready_to_execute`. Measured 88% zero-shot; treated as a *hint*, never a trigger.
2. **Propose** — on `ready_to_execute`, the agent calls `propose_commitment` and speaks a read-back naming, in one or two sentences: the **goal**, the **scope boundary** (what is explicitly excluded), and the **terminal artifact** ("…and I'll leave it as a PR for you to look at").
3. **Assent** — dispatch only on unambiguous affirmative assent to *that read-back*. Hedges ("sure, I guess", "maybe", "yeah but what about…") are **not** assent and return to conversation. `laya-multilingual` is a poor arbiter here; use the frontier model for assent classification in v1.
4. **Dispatch** — `dispatch_task` writes a `commitment` row plus a DBOS workflow handle and returns immediately. The conversation continues uninterrupted.
5. **Report** — completion is announced at the next natural turn boundary, never mid-sentence. The artifact link lands in the idea graph regardless of whether it was spoken.

### Failure handling

A false-positive dispatch is far more damaging than a false negative. When `ready` is borderline, the correct behavior is a clarifying question, not a proposal. Every commitment records the exact utterance that constituted assent, so misfires are auditable and become training data (§8.3).

---

## 7. Data model (idea graph)

```sql
-- An idea is durable and long-lived; conversations revisit it for weeks.
ideas        (id, title, summary, status, maturity, created_at, updated_at)
             -- status: exploring | committed | executing | delivered | abandoned

sessions     (id, started_at, ended_at, wake_trigger)
turns        (id, session_id, idea_id, role, text, ts,
              route, model_used, latency_ms,
              laya_intent, laya_ready, laya_confidence)

-- The audit trail for every dispatch. assent_utterance is non-null by design.
commitments  (id, idea_id, goal, scope_excludes, artifact_kind,
              readback_text, assent_utterance, assented_at)

tasks        (id, commitment_id, kind, dbos_workflow_id, status,
              started_at, finished_at, error)
             -- kind: code | research

artifacts    (id, task_id, kind, url, summary, reviewed_at)
             -- kind: pull_request | document

-- Edges make "where did we land on the routing thing?" answerable.
idea_edges   (from_idea, to_idea, relation)
             -- relation: refines | supersedes | blocks | spun_off_from
```

`turns` doubles as the labeling corpus for §8.3 — every routing and gate decision is logged with its outcome from day one.

---

## 8. Phasing

### Phase 1 — Voice loop, frontier only (target: 2 weeks)

openWakeWord → ElevenLabs Agent → Delegator (FastAPI, SSE, frontier model only) → commitment protocol → Pydantic AI executor with **one** tool: research & writing. Postgres idea graph. **No Laya.**

*Exit criterion:* you hold a 10-minute conversation, agree to three things, and get three documents you'd actually read — with zero unintended dispatches.

### Phase 2 — Code executor

Pydantic AI code agent under DBOS, repo checkout, branch, test run, PR. Stops at PR, never merges.

*Exit criterion:* a PR you'd merge without rewriting it.

### Phase 3 — Laya routing + cost reduction

Drop Laya into the Delegator using the validated `choice` schemas. Route `small_local` → Ollama. Shadow-mode first: log Laya's decision alongside the frontier model's for ~500 turns, compare, then promote.

*Exit criterion:* ≥60% of turns served locally with no measurable quality regression.

### Phase 4 — Calibrate and fine-tune

Using the `turns` corpus: fit per-question temperatures on your own data (this is what makes `confidence` usable, and what unlocks abstention), then fine-tune the gate with RLCD. Revisit `score`-type questions only here.

---

## 9. Cost model

| Item | Rate | 2 h/day | Note |
|---|---|---|---|
| ElevenLabs agent minutes | **$0.08/min** ($0.16 burst) | ~$290/mo | Billed on wall-clock conversation, **not** tokens |
| LLM tokens | separate, per model | varies | Billed on top of agent minutes |
| Laya | $0 | $0 | Local, Apache 2.0 |
| Ollama turns | $0 | $0 | Local |

**This is the dominant cost and it is time-based, which is in direct tension with "just keep talking."** Two consequences for the design:

- The wake-word session model (already chosen) matters financially, not just for privacy. Sessions must **auto-close on silence** — a forgotten open session bills at $4.80/hour.
- Laya routing reduces *token* spend but **not** agent minutes. If per-minute cost becomes the binding constraint, the lever is a self-hosted voice pipeline (local STT/TTS), not better routing. Keep the voice layer behind an interface so it stays swappable.

---

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Gate fires without agreement | **Critical** | Read-back + explicit assent (§6). Gate cannot dispatch. |
| Scope creep into browser/comms | **Critical** | Hard refusal outside §3. Rabbit's exact failure. |
| Per-minute cost at conversational volume | High | Auto-close sessions; keep voice layer swappable (§9). |
| Laya `confidence` trusted prematurely | High | Measured uncalibrated. Phase 3 shadow mode, no thresholding before Phase 4. |
| Laya `score` head used for maturity | Medium | Measured 64%/collapsed. `choice` only. |
| Executor lands unreviewed changes | Medium | Terminal artifact is PR/draft. No merge, no send, ever. |
| Long tasks lost on crash/restart | Medium | DBOS durable execution; workflows resume. |
| Frontier API outage | Low | Pydantic AI `FallbackModel` → alternate provider, then local. |

---

## 11. Open questions

1. **Repo access for the code executor** — cloud sandbox cloning from a git host, or an agent on a machine with the checkout? Affects secrets handling and whether tasks survive a closed laptop.
2. **Wake phrase** — an openWakeWord pretrained phrase (free, immediate) or a custom-trained one (Porcupine, on-brand)?
3. **Multi-device sessions** — does an idea started on the desktop resume on a laptop mid-thought, or is resumption always explicit?
4. **Barge-in during a report** — when the agent is announcing a completed task and you interrupt, does it abandon the announcement or queue it?
