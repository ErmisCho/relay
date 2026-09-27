"""Spoken artifact wording must match what the executor actually leaves."""

from __future__ import annotations

import pytest

from relay.delegator.commitment import readback
from relay.delegator.hooks.reports import full_note
from relay.delegator.scope import ArtifactKind

from .conftest import make_settings


def test_full_note_does_not_call_every_artifact_a_document() -> None:
    """The completion note said "The document link is stored in the idea." for PR and branch
    artifacts too; the note serves every artifact kind, so it must not name one."""
    note = full_note("Your pull request for the CLI is ready.")
    assert "document" not in note
    assert note.endswith("The link is stored in the idea.")


@pytest.mark.parametrize(("token", "noun"), [("", "branch"), ("ghp_x", "pull request")])
def test_dispatch_noun_for_code_follows_the_github_token(
    monkeypatch: pytest.MonkeyPatch, token: str, noun: str
) -> None:
    """Without a GitHub token the dispatch line promised "the pull request" while the executor
    leaves a local branch."""
    monkeypatch.setattr(readback, "get_settings", lambda: make_settings(github_token=token))
    assert readback.ARTIFACT_NOUN[ArtifactKind.PULL_REQUEST] == noun
    assert readback.ARTIFACT_NOUN[ArtifactKind.DOCUMENT] == "document"
