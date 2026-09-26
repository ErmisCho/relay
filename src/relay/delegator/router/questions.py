"""The shared routing questions (SPEC section 2): every backend is asked exactly these.

Rules derived from measurement, kept as permanent constraints because routing is
zero-shot with no labelled corpus:

* ``choice`` questions only. The ``score`` head collapses zero-shot (a constant 1.71-3.05),
  so no other question type exists here (a test pins this).
* binary or near-binary, descriptive criteria (a sentence of positive evidence), <= 10 options.
* never turn-taking (50% = chance); that is ElevenLabs' VAD.

``difficulty`` and ``ready`` are copied verbatim from the validated SPEC schemas. The 5-way
intent question follows the same rules; its wording is new (SPEC only reports its accuracy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from relay.delegator.router.base import Difficulty, Ready, RouterDecision, Turn


@dataclass(frozen=True)
class ChoiceQuestion:
    """One zero-shot ``choice`` question: pick exactly one label."""

    instructions: str
    # label -> descriptive criterion (positive evidence for that label)
    criteria: dict[str, str]
    type: Literal["choice"] = "choice"

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(self.criteria)

    def as_schema(self) -> dict[str, Any]:
        """The Laya question schema (``{"type", "instructions", "criteria"}``)."""
        return {
            "type": self.type,
            "instructions": self.instructions,
            "criteria": dict(self.criteria),
        }


ROUTING_QUESTIONS: dict[str, ChoiceQuestion] = {
    "difficulty": ChoiceQuestion(
        instructions="Pick the smallest model that could carry out this request correctly.",
        criteria={
            "small_local": (
                "mechanical edit, rename, reformat, lookup, or a single unambiguous file change"
            ),
            "frontier": (
                "requires novel design, proofs, architecture decisions, or careful multi-file "
                "reasoning where a mistake is expensive"
            ),
        },
    ),
    "ready": ChoiceQuestion(
        instructions="Decide whether this idea can be handed to a background worker right now.",
        criteria={
            "keep_talking": (
                "still exploratory - the goal or scope is not yet pinned down, or the user is "
                "still weighing options"
            ),
            "ready_to_execute": (
                "the goal and scope are explicit and the user has signalled agreement to proceed"
            ),
        },
    ),
}

INTENT_QUESTION = ChoiceQuestion(
    instructions="Pick what the user is doing with this utterance.",
    criteria={
        "explore_idea": (
            "thinking out loud about a new or existing idea: brainstorming, describing a "
            "problem, or weighing options"
        ),
        "refine_scope": (
            "narrowing or correcting a proposal: changing the goal, adding or removing "
            "what is in scope, or answering a clarifying question"
        ),
        "delegate_task": (
            "asking for work to be carried out, or agreeing to a proposed task, e.g. "
            "'go ahead', 'yes, do that', 'research this and write it up'"
        ),
        "ask_status": (
            "asking about earlier ideas or delegated work: progress, results, or what "
            "was decided before"
        ),
        "other": "small talk, greetings, thanks, or anything unrelated to ideas and tasks",
    },
)

#: Every question a backend answers per turn, in prompt order. Choice questions only.
ALL_QUESTIONS: dict[str, ChoiceQuestion] = {**ROUTING_QUESTIONS, "intent": INTENT_QUESTION}


def render_context(context: list[Turn], max_turns: int, max_chars: int) -> str:
    """The last ``max_turns`` turns, oldest first, dropping older ones past ``max_chars``."""
    lines: list[str] = []
    used = 0
    for turn in reversed(context[-max_turns:] if max_turns > 0 else []):
        line = f"{turn.role}: {' '.join(turn.text.split())}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(reversed(lines))


def answer_schema() -> dict[str, Any]:
    """JSON schema of the answer object: one enum per question, all required."""
    return {
        "type": "object",
        "properties": {
            key: {"type": "string", "enum": list(q.labels)} for key, q in ALL_QUESTIONS.items()
        },
        "required": list(ALL_QUESTIONS),
        "additionalProperties": False,
    }


def system_prompt() -> str:
    """The LLM router's instructions, generated from ``ALL_QUESTIONS``."""
    lines = [
        "You label one user utterance from a voice conversation in which a user develops "
        "ideas with an assistant that can hand finished-scope tasks to background workers. "
        "Use the earlier turns only to understand the latest `utterance`. "
        "Answer every question by picking exactly one label.",
    ]
    for key, question in ALL_QUESTIONS.items():
        lines += ["", f"{key}: {question.instructions}"]
        lines += [f'- "{label}": {desc}' for label, desc in question.criteria.items()]
    example = ", ".join(f'"{key}": "<label>"' for key in ALL_QUESTIONS)
    lines += ["", f"Answer with JSON only, exactly: {{{example}}}."]
    return "\n".join(lines)


def user_prompt(utterance: str, context_text: str) -> str:
    return f"earlier turns:\n{context_text or '(none)'}\n\nutterance:\n{utterance.strip()}"


def decision_from_answers(
    answers: object, *, backend: str, latency_ms: int, confidence: float | None = None
) -> RouterDecision | None:
    """Validate ``{"difficulty", "ready", "intent"}`` labels; ``None`` if any is invalid."""
    if not isinstance(answers, dict):
        return None
    picked: dict[str, str] = {}
    for key, question in ALL_QUESTIONS.items():
        value = answers.get(key)
        value = value.strip().lower() if isinstance(value, str) else None
        if value not in question.labels:
            return None
        picked[key] = value
    difficulty: Difficulty = "small_local" if picked["difficulty"] == "small_local" else "frontier"
    ready: Ready = "ready_to_execute" if picked["ready"] == "ready_to_execute" else "keep_talking"
    return RouterDecision(
        difficulty=difficulty,
        ready=ready,
        intent=picked["intent"],
        confidence=confidence,
        backend=backend,
        latency_ms=latency_ms,
    )
