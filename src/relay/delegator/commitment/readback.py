"""Server-generated read-backs and the "was it actually heard?" check (SPEC §6 step 2).

The read-back is built here, never by the model, so it always names the three things the user
is agreeing to: the goal, the explicit exclusion and the terminal artifact. Whether it was
delivered is judged from the assistant message as ElevenLabs recorded it: an interrupted
(barge-in) message is truncated there, so a cut-off read-back is simply not contained in it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from relay.config import get_settings
from relay.delegator.adapters.openai_compat import content_text
from relay.delegator.scope import ArtifactKind

MAX_FIELD_CHARS = 240

#: A code result without a GitHub token stays a local branch (TASK-33).
LOCAL_BRANCH_PHRASE = "a branch in the project folder"
_PHRASES: dict[ArtifactKind, str] = {
    ArtifactKind.DOCUMENT: "a document for you to read",
    ArtifactKind.PULL_REQUEST: "a PR for you to look at",
}


class _ArtifactPhrases(Mapping[ArtifactKind, str]):
    """Read at lookup time: a pull request is promised only when a GitHub token is configured,
    otherwise the read-back promises what the executor will actually leave, the local branch."""

    def __getitem__(self, kind: ArtifactKind) -> str:
        if kind is ArtifactKind.PULL_REQUEST and not get_settings().github_token:
            return LOCAL_BRANCH_PHRASE
        return _PHRASES[kind]

    def __iter__(self) -> Iterator[ArtifactKind]:
        return iter(_PHRASES)

    def __len__(self) -> int:
        return len(_PHRASES)


#: How each terminal artifact is named in the read-back ("…and I'll leave it as <phrase>").
ARTIFACT_PHRASE: Mapping[ArtifactKind, str] = _ArtifactPhrases()
#: Short noun for the spoken dispatch confirmation ("…when the <noun> is ready").
ARTIFACT_NOUN: dict[ArtifactKind, str] = {
    ArtifactKind.DOCUMENT: "document",
    ArtifactKind.PULL_REQUEST: "pull request",
}

#: Deliberately free of the words users answer with ("yes", "go ahead", "do it", "sounds good",
#: "start it"), so an echo of the read-back (headset bleed transcribed as a user turn) can never
#: look like assent while natural answers are not mistaken for an echo.
CLOSING_QUESTION = "Sound good?"

_LEADING_I_WILL = re.compile(r"^(?:i'?ll|i will|i am going to|i'm going to)\s+", re.IGNORECASE)
_NON_WORD = re.compile(r"[^0-9a-z]+")


def clean_field(value: object) -> str:
    """One line of speakable text: whitespace collapsed, trailing punctuation dropped."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("’", "'").split())
    return text.rstrip(" .,;:!?")[:MAX_FIELD_CHARS].strip()


def build_readback(goal: str, scope_excludes: str, artifact_kind: ArtifactKind) -> str:
    """The exact sentence the model must speak; ends with a yes/no question.

    ``goal`` is an imperative phrase ("research X"), ``scope_excludes`` a noun phrase of what is
    left out ("pricing and vendor contacts"). Both are assumed cleaned by :func:`clean_field`.
    """
    goal = _LEADING_I_WILL.sub("", goal)
    # "Research X" -> "research X", but keep acronyms / proper nouns ("AWS", "Rust ...").
    if len(goal) > 1 and goal[0].isupper() and goal[1].islower() and " " in goal:
        first, rest = goal.split(" ", 1)
        if first.lower() in _COMMON_VERBS:
            goal = f"{first.lower()} {rest}"
    return (
        f"Just to confirm: I'll {goal}, leaving out {scope_excludes}, "
        f"and I'll leave it as {ARTIFACT_PHRASE[artifact_kind]}. {CLOSING_QUESTION}"
    )


_COMMON_VERBS = frozenset(
    "research write draft compare summarize summarise investigate analyze analyse find look "
    "survey review collect gather outline build refactor fix add implement create update "
    "explore map list evaluate assess study prepare put make".split()
)


def normalise(text: str) -> str:
    """Case-, punctuation- and whitespace-insensitive form used for delivery checks."""
    return " ".join(_NON_WORD.sub(" ", text.replace("’", "'").replace("'", "").lower()).split())


def previous_assistant_text(messages: list[dict[str, Any]]) -> str | None:
    """Text of the assistant message(s) immediately before the latest user message.

    ``None`` when the latest message is not a user message or nothing precedes it.
    Consecutive assistant messages (one spoken turn split in two) are joined.
    """
    if not messages or messages[-1].get("role") != "user":
        return None
    parts: list[str] = []
    i = len(messages) - 2
    while i >= 0 and messages[i].get("role") == "assistant":
        parts.append(content_text(messages[i].get("content")))
        i -= 1
    if not parts:
        return None
    return " ".join(reversed(parts))


def readback_delivered(messages: list[dict[str, Any]], readback_text: str) -> bool:
    """True only if the immediately preceding assistant message ENDS with the full read-back.

    Only whitespace or punctuation may follow it: anything spoken after the question (e.g. "I've
    started on it already") changes what the user is answering.
    """
    spoken = previous_assistant_text(messages)
    target = normalise(readback_text)
    if spoken is None or not target:
        return False
    return f" {normalise(spoken)}".endswith(f" {target}")
