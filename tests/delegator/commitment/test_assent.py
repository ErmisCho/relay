"""Assent classifier prompt, deterministic pre-check and read-back contents (no database)."""

from __future__ import annotations

import pytest

from relay.delegator.commitment import (
    ASSENT_SYSTEM_PROMPT,
    AssentLabel,
    build_readback,
    classify_assent,
    precheck,
    readback_delivered,
)
from relay.delegator.commitment.readback import ARTIFACT_PHRASE
from relay.delegator.scope import ArtifactKind

from .harness import FakeLabelModel
from .utterances import AFFIRMATIVE, HEDGES, NEGATIVES, READBACK


@pytest.mark.parametrize("artifact", list(ArtifactKind))
@pytest.mark.parametrize(
    ("goal", "excludes"),
    [
        ("research proximity-unlock bike locks", "pricing"),
        ("Compare Rust web frameworks", "benchmarks older than 2024"),
        ("I'll write a brief on heat pumps", "installer quotes"),
    ],
)
def test_every_readback_names_goal_exclusion_and_artifact_and_asks(
    goal: str, excludes: str, artifact: ArtifactKind
) -> None:
    rb = build_readback(goal, excludes, artifact)
    core = goal.removeprefix("I'll ").split(" ", 1)[1]
    assert core in rb
    assert f"leaving out {excludes}" in rb
    assert ARTIFACT_PHRASE[artifact] in rb
    assert rb.endswith("?")
    assert "I'll I'll" not in rb


def test_the_checked_in_prompt_states_the_fail_safe_rules() -> None:
    for phrase in ("sure, I guess", "maybe", "yeah but what about", "NOT affirmative"):
        assert phrase in ASSENT_SYSTEM_PROMPT


@pytest.mark.parametrize("utterance", HEDGES + NEGATIVES)
async def test_listed_hedges_and_negatives_never_classify_affirmative(utterance: str) -> None:
    # A gullible model answering "affirmative" must not matter for the scripted set.
    result = await classify_assent(FakeLabelModel("affirmative"), READBACK, utterance)
    assert result.label is not AssentLabel.AFFIRMATIVE
    assert result.source == "precheck"


@pytest.mark.parametrize("utterance", AFFIRMATIVE)
async def test_plain_yes_still_needs_the_model_to_agree(utterance: str) -> None:
    assert precheck(utterance) is None
    no = await classify_assent(FakeLabelModel("hedge"), READBACK, utterance)
    assert no.label is AssentLabel.HEDGE and no.source == "model"
    yes = await classify_assent(FakeLabelModel("affirmative"), READBACK, utterance)
    assert yes.label is AssentLabel.AFFIRMATIVE


async def test_no_model_means_no_affirmative() -> None:
    result = await classify_assent(None, READBACK, "yes")
    assert (result.label, result.source) == (AssentLabel.HEDGE, "error")


def test_delivery_requires_the_full_readback_in_the_last_assistant_message() -> None:
    history = [{"role": "assistant", "content": READBACK}, {"role": "user", "content": "yes"}]
    assert readback_delivered(history, READBACK)
    older = [
        history[0],
        {"role": "user", "content": "hm"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "yes"},
    ]
    assert not readback_delivered(older, READBACK)
    cut = [{"role": "assistant", "content": READBACK[:-20]}, {"role": "user", "content": "yes"}]
    assert not readback_delivered(cut, READBACK)


async def test_label_in_a_tool_call_is_accepted_too() -> None:
    result = await classify_assent(FakeLabelModel("affirmative", as_tool=True), READBACK, "yes")
    assert (result.label, result.source) == (AssentLabel.AFFIRMATIVE, "model")


def _tails(readback: str) -> list[str]:
    words = readback.split()
    return [" ".join(words[-n:]) for n in range(1, len(words) + 1)]


@pytest.mark.parametrize(
    "echo",
    [*_tails(READBACK), "sound good", "Sound good?", "Sound good.", "a document for you to read"],
)
async def test_an_echo_of_the_readback_is_never_affirmative(echo: str) -> None:
    # Headset echo of our own read-back gets transcribed as a user turn.
    result = await classify_assent(FakeLabelModel("affirmative"), READBACK, echo)
    assert result.label is not AssentLabel.AFFIRMATIVE


@pytest.mark.parametrize("answer", ["yes", "go ahead", "do it", "sounds good", "start it", "Yeah."])
def test_natural_answers_to_the_closing_question_are_not_echoes(answer: str) -> None:
    assert precheck(answer, READBACK) is None


def test_delivery_requires_the_readback_to_end_the_assistant_message() -> None:
    def hist(spoken: str) -> list[dict[str, str]]:
        return [{"role": "assistant", "content": spoken}, {"role": "user", "content": "ok"}]

    assert readback_delivered(hist("Sure. " + READBACK + " "), READBACK)
    assert not readback_delivered(hist(READBACK + " Great, I've started on it already."), READBACK)
