---
id: TASK-27
title: 'Implement the commitment protocol (score, propose, assent, dispatch)'
status: To Do
assignee: []
created_date: '2026-09-26 13:09'
updated_date: '2026-09-26 13:14'
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
- [ ] #1 No code path creates a `commitments` or `tasks` row without a stored read-back and an `affirmative` assent classification on the next user turn
- [ ] #2 Hedged responses (at least: 'sure, I guess', 'maybe', 'yeah but what about…') never dispatch
- [ ] #3 A model-emitted `dispatch_task` call without a valid pending proposal is rejected server-side
- [ ] #4 Every read-back names goal, scope exclusion and terminal artifact
- [ ] #5 Each commitment row stores the verbatim assent utterance and read-back text
- [ ] #6 Scripted adversarial conversations (20+) produce zero unintended dispatches
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 Assent classifier prompt and scripted assent/hedge test utterances checked into the repo as tests
<!-- DOD:END -->
