"""Build the Delegator's upstream model from ``Settings``."""

from __future__ import annotations

from relay.config import Settings, parse_model_ref
from relay.delegator.llm.anthropic import AnthropicChatModel
from relay.delegator.llm.base import ChatModel
from relay.delegator.llm.fallback import FallbackChatModel
from relay.delegator.llm.openai_compat import OpenAICompatChatModel


def build_model(ref: str, settings: Settings) -> ChatModel:
    """Build one model from a ``<provider>:<model>`` ref; ``model_name`` is the full ref."""
    provider, model = parse_model_ref(ref)
    if provider == "ollama":
        # Thinking models (gemma4, glm) otherwise spend the whole token budget on hidden
        # reasoning before the first spoken token; Ollama honours reasoning_effort="none".
        return OpenAICompatChatModel(
            model,
            api_key="ollama",
            base_url=settings.ollama_base_url,
            name=ref,
            reasoning_effort="none",
        )
    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(f"{ref!r} needs OPENAI_API_KEY")
        return OpenAICompatChatModel(model, api_key=settings.openai_api_key, name=ref)
    if not settings.anthropic_api_key:
        raise ValueError(f"{ref!r} needs ANTHROPIC_API_KEY")
    return AnthropicChatModel(model, api_key=settings.anthropic_api_key, name=ref)


def build_chat_model(settings: Settings) -> ChatModel:
    """Primary ``delegator_model`` wrapped with ``delegator_fallback_model``.

    The wrapper is kept even when both refs are equal: a second attempt still
    rescues a transient failure before the first token.
    """
    primary = build_model(settings.delegator_model, settings)
    fallback = build_model(settings.delegator_fallback_model, settings)
    return FallbackChatModel(primary, fallback)
