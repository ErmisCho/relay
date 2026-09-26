"""The per-route research models are built from settings (TASK-46); no network."""

from __future__ import annotations

import pytest
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from relay.config import Settings
from relay.executor.agent.agent import build_route_models, route_model_refs


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
    assert primary.client.max_retries == 0
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
