---
id: TASK-27
title: 'Implement the commitment protocol (score, propose, assent, dispatch)'
status: Done
assignee:
  - '@claude'
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 18:03'
labels:
  - phase-1
  - protocol
  - delegator
  - safety
milestone: m-0
dependencies:
  - TASK-22
  - TASK-26
  - TASK-25
references:
  - SPEC.md#6-the-commitment-protocol
  - SPEC.md#2-laya--jev-evaluation--measured-not-estimated
documentation:
  - SPEC.md
priority: high
type: feature
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
This is the product. A gate firing without real agreement destroys trust permanently, so "ready" is a dialogue policy, not a confidence threshold. Invariant: **the gate may only ever propose; dispatch requires explicit spoken assent to a spoken read-back.** A false-positive dispatch is far worse than a false negative; every commitment records the exact assent utterance and read-back so misfires are auditable.

**Technical details**
- **Score:** on every user turn the Delegator runs a structured frontier call answering the validated `ready` choice question (`keep_talking` | `ready_to_execute`, criteria from SPEC §2) and writes it to `router_decisions(backend='frontier', ready=…)`. Treated as a hint only. Laya/LLM routers may log a shadow `ready` in Phase 3, but gate scoring stays on the frontier model in v1.
- **Propose:** internal tool `propose_commitment(goal, scope_excludes, artifact_kind, idea_id)` stores a pending proposal in session state (not in `commitments` — that table requires assent) with a server-generated `readback_text` covering goal, explicit exclusions and terminal artifact ("…and I'll leave it as a document for you to read"). The model must speak exactly that read-back. Borderline `ready` → the model asks a clarifying question instead.
- **Assent:** on the next user turn a separate frontier classification call with structured output `{"label": "affirmative"|"hedge"|"negative"|"new_information"}` given `readback_text` + utterance. Only `affirmative` passes; hedges ("sure, I guess", "maybe", "yeah but what about…") clear the proposal and return to conversation.
- **Dispatch:** `dispatch_task` is authorised server-side, not by the model: it succeeds only if a pending proposal exists for this session, the immediately preceding assistant turn contained its read-back, and the assent classifier returned `affirmative` for the immediately following user turn. It then writes `commitments(assent_utterance, assented_at, readback_text, …)`, sets `ideas.status='committed'`, and calls `start_task`. Any other path returns an error to the model.
- Pending proposals expire on any intervening topic change or after one user turn; a restart drops them (fails safe to no dispatch).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 No code path creates a `commitments` or `tasks` row without a stored read-back and an `affirmative` assent classification on the next user turn
- [x] #2 Hedged responses (at least: 'sure, I guess', 'maybe', 'yeah but what about…') never dispatch
- [x] #3 A model-emitted `dispatch_task` call without a valid pending proposal is rejected server-side
- [x] #4 Every read-back names goal, scope exclusion and terminal artifact
- [x] #5 Each commitment row stores the verbatim assent utterance and read-back text
- [x] #6 Scripted adversarial conversations (20+) produce zero unintended dispatches
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Assent classifier prompt and scripted assent/hedge test utterances checked into the repo as tests
<!-- DOD:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
commitment/ package: ready scorer (logged to router_decisions backend=frontier, off the TTFT path), propose_commitment tool (scope_guard first, server-generated read-back naming goal/exclusion/artifact, pending proposal in session state), read-back delivery check against ElevenLabs-recorded history, assent classifier (affirmative|hedge|negative|new_information, fail-safe), dispatch_task authorised server-side only (pending proposal + spoken read-back + affirmative on the immediately following user turn) writing commitments with verbatim assent + start_task, expiry rules; CommitmentHook + system prefix instructions; 20+ adversarial scripted conversations + checked-in assent/hedge tests + opt-in live classifier eval.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
2026-09-26 model config (user: use local LLMs, providers via .env): benchmarked installed Ollama models via OpenAI-compatible API at localhost:11434. gemma4:e4b — correct tool calls, warm TTFT ~0.2 s, 4/4 correct assent/hedge classifications → default for Delegator turns and assent classifier. glm-4.7-flash — correct tool calls but a thinking model, warm TTFT 4–12 s → too slow for voice; default for the research executor. All model ids/base URLs/keys come from .env (e.g. DELEGATOR_MODEL, ASSENT_MODEL, RESEARCH_MODEL, RESEARCH_FALLBACK_MODEL); frontier providers swap in by config only.

2026-09-26 W4: commitment/ package — server-generated read-back ending 'Sound good?', delivery verified as the suffix of the ElevenLabs-recorded assistant text from a completed stream; assent classifier (pre-check only moves away from affirmative incl. read-back echo; gemma JSON; fail-safe); dispatch_task only reserves: commit at latest user request + 3.5 s grace, reservation held synchronously while any new request is classified, only affirmative keeps it (after the spoken confirmation a hedge/pleasantry keeps it, negative/new_information cancel), commit + start_task detached; replay → 'already started'; reconciler at startup (scope-checked, ≤24 h); drain cancels waiting reservations; ready score batch after 20 s idle. Service: re-send detection, per-turn completion, empty-output retry. Reviews: 3 rounds (+ owner-authorized 3rd fix). Evidence: 141 commitment tests; reviewer probe suite 16/16 (coordinator re-run); live classifier precision/recall 1.0 on 31 utterances; TTFT normal ~0.21 s (5 s gaps, scorer idle), assent turn ~0.74–1.5 s. Known: local gemma often paraphrases instead of calling propose_commitment (TASK-44 moves conversation to gpt-6-luna).
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Commitment protocol enforcing 'propose only; dispatch needs spoken assent to a spoken read-back', hardened against ElevenLabs re-sends, ASR revisions, barge-in, echo and cancellation. Verified by 141 tests, a 16-probe adversarial suite and a live classifier eval with zero false affirmatives.
<!-- SECTION:FINAL_SUMMARY:END -->
