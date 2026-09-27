"""The demo warns when the ElevenLabs agent calls another Delegator.

Bug caught (2026-09-27): the agent's custom LLM URL still named an old tunnel, so voice turns
were answered by a different Delegator; the demo's debug feed stayed empty and nothing was
dispatched, while typed text worked. Nothing said why.
"""

from __future__ import annotations

from typing import Any

import pytest

from relay.delegator.demo.api import agent_url_mismatch

PUBLIC = "https://mine.ngrok-free.dev"


def agent(url: str | None, llm: str = "custom-llm") -> dict[str, Any]:
    prompt: dict[str, Any] = {"llm": llm}
    if url is not None:
        prompt["custom_llm"] = {"url": url}
    return {"conversation_config": {"agent": {"prompt": prompt}}}


@pytest.mark.parametrize("url", [f"{PUBLIC}/v1", f"{PUBLIC}/v1/", PUBLIC, f"{PUBLIC}/"])
def test_agent_calling_this_delegator_is_fine(url: str) -> None:
    assert agent_url_mismatch(agent(url), PUBLIC) is None
    assert agent_url_mismatch(agent(url), f"{PUBLIC}/") is None


@pytest.mark.parametrize(
    ("config", "expect"),
    [
        (agent("https://old-tunnel.ngrok-free.dev/v1"), "old-tunnel"),
        (agent(None), "does not use a custom LLM"),
        (agent(f"{PUBLIC}/v1", llm="gpt-4o"), "does not use a custom LLM"),
        ({}, "does not use a custom LLM"),
    ],
)
def test_agent_calling_elsewhere_is_reported(config: dict[str, Any], expect: str) -> None:
    problem = agent_url_mismatch(config, PUBLIC)
    assert problem is not None and expect in problem
