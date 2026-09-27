# Asian Dad Final Evaluation

Evaluated: 2026-09-27
Rubrics: `.claude/.asian-dad/*-rubric.json`
Rule: every criterion is binary; one failed criterion fails its rubric.

## Overall hackathon-completion rubric — **FAILURE**

- C1 **PASS** — `backlog task list --plain` lists every task under Done and no open section.
- C2 **PASS** — `uv run pytest -q`: 524 passed, 25 skipped; `uv run mypy src`: success; `uv run ruff check .`: success; web: 14 passed and mock production build succeeded.
- C3 **PASS** — visible Chrome runs at desktop and phone widths completed full delivery, hedge, scope-refusal, recall and artifact-reader flows without console errors or horizontal overflow.
- C4 **PASS** — validation used local Ollama/Postgres, deterministic provider transports, browser mock mode and keyless Open-Meteo. No paid provider was called.
- C5 **FAIL** — four sealed task rubrics retain unmet external/long-window acceptance criteria: TASK-24 C5, TASK-34 C1, TASK-38 C1/C3 and TASK-43 C7. TASK-49 is a documented specification gap, not a fabricated implementation.
- C6 **PASS** — verdict-driving gates, latency, local-share sample, browser behavior and real local tools have command/browser evidence.

Why overall is not PASS: backlog state is fully reconciled and the MVP gates are green, but strict evaluation cannot convert missing one-hour, human-merge, same-session router comparison, blind-quality and first-time-human evidence into passes.

## TASK-22 — **PASS**

- C1 **PASS** — SSE contract tests verify OpenAI chunks and `[DONE]`.
- C2 **PASS** — system-tool/internal-tool stream separation is contract tested.
- C3 **PASS** — database-backed request tests verify persisted turns, model and latency.
- C4 **PASS** — valid pre-output supersession replaces paid live ElevenLabs with the fake transport plus local model path; it passes.
- C5 **PASS** — unauthorized request tests return 401.
- C6 **PASS** — recorded-request byte-format contract tests pass.
- C7 **PASS** — ten real local turns measured p50 TTFT at 816 ms.
- C8 **PASS** — no paid API called.
- C9 **PASS** — claims above cite measured outputs.

## TASK-23 — **PASS**

- C1 **PASS** — superseded fake-SDK/Delegator round trip passes.
- C2 **PASS** — superseded interruption path cancels playback in tests.
- C3 **PASS** — superseded session identity path is verified through callbacks and persistence.
- C4 **PASS** — SDK import confinement test passes.
- C5 **PASS** — versioned agent configuration exists in the repository.
- C6 **PASS** — no paid API called.
- C7 **PASS** — evidence is executable, not asserted.

## TASK-24 — **FAILURE**

- C1 **PASS** — deterministic listener test measures 150–500 ms trigger-to-connect behavior.
- C2 **PASS** — silence closes and records the end reason.
- C3 **PASS** — listener resume/reopen path passes.
- C4 **PASS** — microphone handoff test passes.
- C5 **FAIL** — no one-hour physical background-audio false-trigger run was performed or recorded.
- C6 **PASS** — no paid API called.
- C7 **PASS** — the missing physical measurement is reported rather than invented.

## TASK-31 — **PASS**

- C1 **PASS** — superseded zero-cost single-session web test records exactly 3 affirmative assents, 3 dispatches and 3 non-empty delivered documents.
- C2 **PASS** — audit events and hedge/scope scenarios show no unintended dispatch.
- C3 **PASS** — automatic and explicit close paths pass client tests.
- C4 **PASS** — superseded local baseline records $0 paid cost and 816 ms p50 TTFT.
- C5 **PASS** — no paid API called.
- C6 **PASS** — counts and latency are measured.

## TASK-32 — **PASS**

- C1 **PASS** — accepted decision 6 covers secrets, durability and sandboxing.
- C2 **PASS** — TASK-33 records and implements the chosen model.
- C3 **PASS** — no paid API called.
- C4 **PASS** — decision and executor tests are inspectable.

## TASK-33 — **PASS**

- C1 **PASS** — offline e2e produces a new branch, safe commit, draft-PR contract and test-bearing description.
- C2 **PASS** — default-branch push/merge guards pass.
- C3 **PASS** — worker-kill resume test passes.
- C4 **PASS** — pull-request artifact and next-boundary report delivery pass.
- C5 **PASS** — GitHub/provider interactions use fake transports and local repositories.
- C6 **PASS** — full test evidence exists.

## TASK-34 — **FAILURE**

- C1 **FAIL** — no real voice-dispatched PR was merged by a human user without rewriting.
- C2 **PASS** — tests prove the executor cannot merge or push the default branch.
- C3 **PASS** — no paid API called.
- C4 **PASS** — the absent human merge is explicitly reported.

## TASK-35 — **PASS**

- C1 **PASS** — session-start publishing is platform-neutral; Linux, Darwin and Windows probe branches exist, and every-wake publishing is contract tested.
- C2 **PASS** — refused and stalled Ollama tests return cloud-only within budget.
- C3 **PASS** — absent-profile active-routing test uses frontier behavior.
- C4 **PASS** — no paid API called.
- C5 **PASS** — executable tests support the claims.

## TASK-36 — **PASS**

- C1 **PASS** — shadow router runs after response and database tests verify inactive rows without TTFT delay.
- C2 **PASS** — shared question tests pin choice-only schemas.
- C3 **PASS** — code/tests show confidence is audit-only.
- C4 **PASS** — registry test adds a backend without request-path edits.
- C5 **PASS** — router-report tests cover agreement and latency percentiles.
- C6 **PASS** — no paid API called.
- C7 **PASS** — all claims have runnable tests.

## TASK-37 — **PASS**

- C1 **PASS** — active database-backed tests verify Ollama service and `model_used`.
- C2 **PASS** — parameterized configuration-only switching passes for laya/llm/none.
- C3 **PASS** — timeout/error/local failure cases silently fall back.
- C4 **PASS** — assent bypass test proves safety-critical isolation.
- C5 **PASS** — no paid API called.
- C6 **PASS** — all paths are measured by tests.

## TASK-38 — **FAILURE**

- C1 **FAIL** — decision 7 exists, but no measured Laya-vs-LLM comparison over the same conversation sessions was produced in this completion pass.
- C2 **PASS** — the recorded live sample served 2 of 3 turns locally (66.7%).
- C3 **FAIL** — automated safety/regression tests pass, but the specified Phase-1 comparison with a recorded blind side-by-side quality method was not run.
- C4 **PASS** — commitment and scenario tests record zero unintended dispatches in the measured window.
- C5 **PASS** — no paid API called.
- C6 **PASS** — the small window and missing week/blind review are disclosed.

## TASK-41 — **PASS**

- C1 **PASS** — LLMRouter uses the Router protocol and shared questions.
- C2 **PASS** — valid supersession: real local Ollama plus fake cloud transport both pass under configuration.
- C3 **PASS** — timeout/error/malformed cases return no decision and fail safe.
- C4 **PASS** — migration 0005 persists backend decision, latency, input/output tokens and `cost_usd`; targeted router/schema gate passed 47 tests with one live opt-in skip.
- C5 **PASS** — no paid API called.
- C6 **PASS** — persistence and transport claims are tested.

## TASK-42 — **PASS**

- C1 **PASS** — authenticated credential endpoint and key non-leakage are contract tested.
- C2 **PASS** — ordered, replayable SSE event coverage is database tested.
- C3 **PASS** — detached feed/router work remains outside the TTFT path and timing tests pass.
- C4 **PASS** — idea, commitment, task and rendered-artifact reads pass.
- C5 **PASS** — text chat uses the real Delegator service path.
- C6 **PASS** — passcode, one-live-voice slot and duration limits are tested.
- C7 **PASS** — scripted ordering, token leakage and access-control tests pass.
- C8 **PASS** — no paid API called.
- C9 **PASS** — evidence is executable.

## TASK-43 — **FAILURE**

- C1 **PASS** — valid supersession: voice panel contract is implemented/tested and zero-cost demo visibly falls back to type-to-talk.
- C2 **PASS** — type-to-talk full flow passes in visible Chrome.
- C3 **PASS** — thinking/audit panel updates through the tested ordered event path.
- C4 **PASS** — task delivery and Markdown reader work visibly.
- C5 **PASS** — ideas and commitment audit views work.
- C6 **PASS** — all four guided scenarios pass browser and unit validation.
- C7 **FAIL** — Chrome desktop/phone and prior WebKit automation pass, but no first-time human viewer completion was performed; that conjunct cannot be inferred from automation.
- C8 **PASS** — README documents local/live startup and required services.
- C9 **PASS** — no paid API called.
- C10 **PASS** — browser/test/build evidence is recorded.

## TASK-44 — **PASS**

- C1 **PASS** — superseded local Ollama default works end to end.
- C2 **PASS** — superseded ten-turn local TTFT measurement has 816 ms median.
- C3 **PASS** — first-delta/error fallback tests pass.
- C4 **PASS** — assent, ready and scope remain local.
- C5 **PASS** — superseded local live scope behavior passes.
- C6 **PASS** — no paid API called.
- C7 **PASS** — live local timings and tests support the claims.

## TASK-46 — **PASS**

- C1 **PASS** — one durable task routing decision with chosen model is database tested.
- C2 **PASS** — valid supersession uses configurable local easy/hard/fallback models and stub/local tests.
- C3 **PASS** — router failure/timeout/invalid routes hard.
- C4 **PASS** — served model is recorded in task/artifact metadata.
- C5 **PASS** — environment settings select every model.
- C6 **PASS** — no paid API called.
- C7 **PASS** — database/e2e tests support the claims.

## TASK-47 — **PASS**

- C1 **PASS** — production runner is general executor code; persisted research task values remain compatible.
- C2 **PASS** — harness Researcher/Coder capabilities run inside resumable DBOS workflow tests.
- C3 **PASS** — per-idea roots and path-confinement/symlink tests pass.
- C4 **PASS** — sourced research Markdown e2e remains green.
- C5 **PASS** — TASK-46 routing e2e still covers easy/hard configured models and fallback.
- C6 **PASS** — dependency is locked and the quality triad passes.
- C7 **PASS** — no paid API called.
- C8 **PASS** — claims cite tests and gates.

## TASK-48 — **PASS**

- C1 **PASS** — real local Ollama selected the hardware tool and spoke observed AMD64, RTX 5070/Radeon and ~95 GB RAM data plus recommendation in 2.11 s.
- C2 **PASS** — keyless Open-Meteo returned Vienna temperature 11.7 °C, wind 2.4 and weather code 0; fake transport covers deterministic formatting/failure.
- C3 **PASS** — registry and direct-response contracts pass.
- C4 **PASS** — pytest, mypy and Ruff pass.
- C5 **PASS** — real local hardware and keyless weather outputs were measured.

## TASK-49 — **SPEC-GAP**

- C1 **SPEC-GAP** — the requested partial mlx-whisper replacement has no supported transcript-injection point in the retained ElevenLabs Agents conversation runtime.
- C2 **SPEC-GAP** — replacing VAD requires ownership of the complete STT/VAD/TTS session loop, which the task simultaneously says to retain unchanged.
- C3 **SPEC-GAP** — the target is M4 Pro/mlx-whisper but the available host is Windows; no valid ≤300 ms target-hardware measurement can be produced here.
- C4 **PASS** — decision 8 records the architecture contradiction and rejects a fake partial integration.

Missing information to make the task gradeable: choose a fully self-hosted voice transport/TTS runtime, define its transcript/audio interfaces, and provide an M4 Pro benchmark host. Current deferred behavior is appropriate, but it is not the requested implementation.

## TASK-51 — **PASS**

- C1 **PASS** — barrier regression proves same-round tool overlap.
- C2 **PASS** — `gather` plus strict ordered zipping preserves call order.
- C3 **PASS** — failure isolation preserves sibling results.
- C4 **PASS** — targeted and full tests support the claims.

## TASK-52 — **PASS**

- C1 **PASS** — stalled optional context/persistence regressions prove bounded speech latency.
- C2 **PASS** — empty primary/fallback streams produce audible fallback behavior.
- C3 **PASS** — delayed assent and scope safety gates are exempt and pass race tests.
- C4 **PASS** — Darwin chip/RAM fallback path is covered.
- C5 **PASS** — real local Ollama selected hardware and spoke observed specs in 2.11 s.
- C6 **PASS** — all verdict-driving behavior has test/live evidence.
