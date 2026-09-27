"""The per-route research models are built from settings (TASK-46); no network."""

from __future__ import annotations

import httpx2
import pytest
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from relay.config import Settings
from relay.executor.agent import agent as agent_module
from relay.executor.agent.agent import (
    RateLimitRetryTransport,
    build_model_from_ref,
    build_route_models,
    primary_system,
    route_model_refs,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = dict(
        research_easy_model="ollama:gemma4:e4b",
        research_fallback_model="ollama:gemma4:e4b",
        research_hard_model="openai:gpt-6-luna",
        research_hard_fallback_model="ollama:qwen3.8:latest",
        openai_api_key="sk-test",
        ollama_base_url="http://ollama.test:11434/v1",
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_easy_route_is_local_gemma_without_duplicate_fallback() -> None:
    s = _settings()
    assert route_model_refs("easy", s) == ("ollama:gemma4:e4b", None)
    easy = build_route_models(s)["easy"]
    assert isinstance(easy, OpenAIChatModel) and easy.model_name == "gemma4:e4b"


def test_easy_route_uses_a_different_research_fallback() -> None:
    s = _settings(research_fallback_model="ollama:qwen3.8:latest")
    assert route_model_refs("easy", s) == ("ollama:gemma4:e4b", "ollama:qwen3.8:latest")
    easy = build_route_models(s)["easy"]
    assert isinstance(easy, FallbackModel)
    assert [m.model_name for m in easy.models] == ["gemma4:e4b", "qwen3.8:latest"]


def test_hard_route_is_luna_on_responses_api_with_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_REASONING_EFFORT", raising=False)
    hard = build_route_models(_settings())["hard"]
    assert isinstance(hard, FallbackModel)
    primary, fallback = hard.models
    # Chat Completions rejects function tools + reasoning effort for gpt-6-luna.
    assert isinstance(primary, OpenAIResponsesModel) and primary.model_name == "gpt-6-luna"
    assert primary.settings == {"openai_reasoning_effort": "medium"}
    assert isinstance(fallback, OpenAIChatModel) and fallback.model_name == "qwen3.8:latest"


@pytest.mark.parametrize(
    ("env", "expected"), [("high", {"openai_reasoning_effort": "high"}), ("default", {})]
)
def test_reasoning_effort_is_configurable(
    monkeypatch: pytest.MonkeyPatch, env: str, expected: dict[str, str]
) -> None:
    monkeypatch.setenv("RESEARCH_REASONING_EFFORT", env)
    hard = build_route_models(_settings())["hard"]
    assert isinstance(hard, FallbackModel)
    assert hard.models[0].settings == expected


def test_hard_route_without_openai_key_runs_on_fallback_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hard = build_route_models(_settings(openai_api_key=None))["hard"]
    assert isinstance(hard, OpenAIChatModel) and hard.model_name == "qwen3.8:latest"
    assert "OPENAI_API_KEY" in caplog.text


@pytest.mark.parametrize("ref", ["ollama:gemma4:e4b", "openai:gpt-6-luna"])
def test_models_fail_fast_so_a_hung_endpoint_hands_over_to_the_fallback(ref: str) -> None:
    # SDK retries or the default 10-minute read timeout would stall the route for minutes.
    model = build_model_from_ref(ref, _settings())
    assert isinstance(model, OpenAIChatModel | OpenAIResponsesModel)
    assert model.client.max_retries == 0
    timeout = model.client.timeout
    assert isinstance(timeout, httpx2.Timeout)
    assert (timeout.connect, timeout.read) == (5.0, 120.0)


def test_ollama_model_talks_to_the_configured_base_url() -> None:
    model = build_model_from_ref("ollama:gemma4:e4b", _settings())
    assert isinstance(model, OpenAIChatModel)
    assert model.base_url == "http://ollama.test:11434/v1/"


@pytest.mark.parametrize(
    ("responses", "expected_status", "expected_calls"),
    [
        # A rate limit is waited out, so the run stays on the frontier model.
        ([(429, {"retry-after-ms": "10"}), (200, {})], 200, 2),
        # Anything else goes straight to the FallbackModel.
        ([(500, {}), (200, {})], 500, 1),
        # A wait longer than RATE_LIMIT_MAX_WAIT_S is not worth blocking the run on.
        ([(429, {"retry-after": "60"}), (200, {})], 429, 1),
    ],
)
async def test_only_short_rate_limits_are_retried(
    responses: list[tuple[int, dict[str, str]]],
    expected_status: int,
    expected_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(agent_module.asyncio, "sleep", no_sleep)
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        status, headers = queue.pop(0)
        return httpx2.Response(status, headers=headers)

    transport = RateLimitRetryTransport(httpx2.MockTransport(handler))
    async with httpx2.AsyncClient(transport=transport) as client:
        response = await client.post("https://api.test/v1/responses", json={"x": 1})
    assert response.status_code == expected_status
    assert len(responses) - len(queue) == expected_calls
    assert waits == ([0.01] if expected_calls == 2 else [])


def test_context_budget_follows_the_primary_model_not_the_fallback() -> None:
    # easy = local gemma alone; hard = gpt-6-luna backed by local qwen. The hard route must
    # get the frontier budget, or gpt-6-luna loses results to clearing and re-fetches them.
    models = build_route_models(_settings())
    assert primary_system(models["easy"]) == "ollama"
    assert primary_system(models["hard"]) == "openai"
