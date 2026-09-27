"""Scripted, labelled replies to a read-back: the checked-in assent/hedge test set (TASK-27 DoD).

Used by the deterministic tests (hedges / negatives must never dispatch) and by the opt-in live
classifier eval (``test_live_assent.py``). Only ``affirmative`` may ever lead to dispatch.
"""

from __future__ import annotations

READBACK = (
    "Just to confirm: I'll research proximity-unlock bike locks, leaving out pricing, "
    "and I'll leave it as a document for you to read. Sound good?"
)

AFFIRMATIVE = [
    "yes",
    "Yes please.",
    "Go ahead.",
    "Yep, do it.",
    "Yes, go ahead!",
    "Sounds good, go for it.",
    "Absolutely, start it.",
    "Yeah, that's exactly it, go.",
]

HEDGES = [
    "sure, I guess",
    "maybe",
    "yeah but what about the battery?",
    "yeah but what about…",
    "I think so?",
    "probably",
    "hmm ok",
    "I suppose so",
    "kind of, yeah",
    "if you want",
    "let me think about it",
    "uh, sure",
    "whatever works",
]

NEGATIVES = [
    "no",
    "not yet",
    "wait, hold on",
    "nope, let's not",
    "cancel that",
    "don't do it yet",
]

NEW_INFORMATION = [
    "also include the prices",
    "actually make it about electric scooters instead",
    "what's the weather like in Paris tomorrow",
    "include European brands as well",
]

LABELLED: list[tuple[str, str]] = (
    [(u, "affirmative") for u in AFFIRMATIVE]
    + [(u, "hedge") for u in HEDGES]
    + [(u, "negative") for u in NEGATIVES]
    + [(u, "new_information") for u in NEW_INFORMATION]
)
