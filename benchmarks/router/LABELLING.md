# Labelling criteria (labels.jsonl)

Each line is one task as a dispatched commitment carries it: `goal` plus
`scope_excludes`. The executor turns it into a sourced Markdown research brief
(Relay v1 only does research and writing). Fields: `id`, `goal`,
`scope_excludes`, `label` (`easy` | `hard`), `domain`, `borderline`.

The question behind every label: **would a small local model (gemma4:e4b)
most likely produce a brief that is as good as the frontier model's?** If yes,
`easy`; if a noticeably worse brief is plausible, `hard`.

## hard
Any one of these is enough:
- multi-source synthesis (more than a handful of sources must be read and reconciled)
- comparison across many options or many dimensions (cost, latency, licensing, ...)
- a fast-moving or technical topic where recency and careful reasoning matter
  (latest models, current rates, this year's results, regulation guidance)
- quantitative analysis (estimates, market sizing, TCO, sensitivity ranges)
- conflicting sources that have to be weighed (study quality, effect sizes)

## easy
- a well-known, stable topic summarised from a few sources
- a definition, a simple how-to, or a general overview
- one clear answer; a wrong detail is unlikely and cheap

## Tie-break
When in doubt, label `hard`: sending a hard task to the local model is the
costlier error (worse output); sending an easy task to gpt-6-luna only costs
money and latency.

## Borderline items
`borderline: true` marks items whose wording points one way and substance the
other, e.g. "Summarise the current state of quantum error correction" (easy
verb, hard substance), or "Explain how HTTPS certificates work" (technical
wording, stable textbook topic). 14 of the 60 items are borderline (6 easy, 8
hard). They are the discriminating part of the set; RESULTS.md reports them
separately.

## Composition
60 items: 29 easy, 31 hard, spread over ~20 domains (tech, AI, finance, health,
science, energy, economics, legal, history, food, ...). Item `t01` is the
owner's real example ("Research the latest open-weight LLMs that fit in 64 GB
RAM on an M4 Pro, with quantization options", excluding cloud-only models).

## Known bias
All items and labels were written by one author (an agent), who also wrote
the router prompts. That makes the set cleaner and more separable than real
commitments. Replace or extend it with real dispatched commitments once there
are some (SPEC section 8.3).
