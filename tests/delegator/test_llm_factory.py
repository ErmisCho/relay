"""Model construction from settings."""

from __future__ import annotations

from typing import Any

import pytest

from relay.delegator.llm import FallbackChatModel, OpenAICompatChatModel, build_chat_model

from .conftest import make_settings


async def test_ollama_models_disable_hidden_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    # gemma4 via Ollama otherwise streams only `reasoning` and hits max_tokens with no speech.
    model = build_chat_model(make_settings(delegator_model="ollama:gemma4:e4b"))
    assert isinstance(model, FallbackChatModel)
    primary = model.primary
    assert isinstance(primary, OpenAICompatChatModel)
    assert primary.model_name == "ollama:gemma4:e4b"
    sent: dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Any:
        sent.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(primary._client.chat.completions, "create", fake_create)
    with pytest.raises(RuntimeError):
        async for _ in primary.stream([{"role": "user", "content": "hi"}], None):
            pass
    assert sent["model"] == "gemma4:e4b"
    assert sent["reasoning_effort"] == "none"
    assert sent["stream"] is True and "tools" not in sent
