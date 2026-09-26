"""LLMRouter failure handling and transport selection (no network)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
import pytest
from pydantic_ai.models.test import TestModel

from relay.delegator.router import LLMRouter, Turn
from relay.delegator.router.questions import ALL_QUESTIONS

from ..conftest import make_settings

VALID = {"difficulty": "frontier", "ready": "keep_talking", "intent": "explore_idea"}


def _ollama(handler: Any, timeout_ms: int = 200) -> LLMRouter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = make_settings(router_llm_model="ollama:gemma4:e4b", router_llm_timeout_ms=timeout_ms)
    return LLMRouter(settings, http_client=client)


def _reply(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"message": {"content": content}, "prompt_eval_count": 400, "eval_count": 25}
    )


async def _slow(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(5)
    return _reply(json.dumps(VALID))


def _boom(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": "model not found"})


@pytest.mark.parametrize(
    ("handler", "status"),
    [
        (_slow, "timeout"),
        (_boom, "error"),
        (lambda r: _reply("not json at all"), "invalid"),
        (lambda r: _reply(json.dumps({**VALID, "difficulty": "medium"})), "invalid"),
        (
            lambda r: _reply(json.dumps({"difficulty": "frontier", "ready": "keep_talking"})),
            "invalid",
        ),
    ],
    ids=["timeout", "http-error", "not-json", "unknown-label", "missing-question"],
)
async def test_failures_return_no_decision(handler: Any, status: str) -> None:
    """Bug caught: a timeout, transport error or malformed answer surfacing as a (default)
    decision or an exception instead of ``None``, which callers route to the frontier."""
    router = _ollama(handler)
    loop = asyncio.get_running_loop()
    start = loop.time()
    decision, got = await router.decide_with_status("rename foo to bar", [])
    assert decision is None and got == status
    assert await router.decide("rename foo to bar", []) is None
    assert loop.time() - start < 2  # the hard budget (200 ms) held both calls


async def test_ollama_request_uses_shared_questions_and_bounded_context() -> None:
    """Bug caught: the LLM prompt drifting from the shared question definitions, sampling
    at non-zero temperature, or sending an unbounded conversation history."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _reply(json.dumps(VALID))

    router = _ollama(handler)
    history = [Turn("user" if i % 2 else "assistant", f"turn {i} " + "x" * 900) for i in range(40)]
    decision = await router.decide("go ahead", history)
    assert decision is not None
    assert (decision.difficulty, decision.backend, decision.confidence) == ("frontier", "llm", None)
    payload = seen[0]
    assert payload["options"]["temperature"] == 0
    assert payload["format"]["required"] == list(ALL_QUESTIONS)
    system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
    for question in ALL_QUESTIONS.values():
        for label, criterion in question.criteria.items():
            assert f'"{label}": {criterion}' in system
    assert "turn 39" in user and "turn 0 " not in user
    assert len(user) <= make_settings().router_llm_context_chars + 200


async def test_cloud_model_goes_through_pydantic_ai_structured_output() -> None:
    """Bug caught: a cloud ``ROUTER_LLM_MODEL`` falling through to the Ollama transport, or
    its structured output not being validated into a decision."""
    router = LLMRouter(make_settings(router_llm_model="openai:gpt-5-nano", openai_api_key="sk-x"))
    agent = router._build_agent()
    router._agent = agent
    with agent.override(model=TestModel()):
        decision = await router.decide("go ahead", [])
    assert decision is not None and decision.backend == "llm"


@pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1", reason="live LLM test: set RELAY_LLM_TESTS=1"
)
async def test_live_local_gemma_answers_every_question() -> None:
    router = LLMRouter(make_settings(router_llm_timeout_ms=30000))
    decision, status = await router.decide_with_status(
        "Yes, go ahead and research that.",
        [Turn("assistant", "Shall I compare three bike locks and leave you a brief?")],
    )
    assert status == "ok" and decision is not None
    assert decision.ready == "ready_to_execute"
