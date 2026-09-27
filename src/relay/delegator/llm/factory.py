"""Build the Delegator's upstream model from ``Settings``."""

from __future__ import annotations

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict

from relay.config import Settings, parse_model_ref
from relay.delegator.llm.anthropic import AnthropicChatModel
from relay.delegator.llm.base import ChatModel
from relay.delegator.llm.fallback import FallbackChatModel
from relay.delegator.llm.openai_compat import OpenAICompatChatModel

log = logging.getLogger(__name__)

# Voice needs a fast first token: with OpenAI's default reasoning gpt-6-luna took ~3.7 s
# to its first token on a short prompt, ~1.2 s with reasoning_effort="none".
DEFAULT_OPENAI_REASONING_EFFORT = "none"
# Spellings that mean "don't send reasoning_effort; use the provider's default".
_PROVIDER_DEFAULT_EFFORTS = frozenset({"", "default"})
# ``model_used`` prefix of a turn served by the client device's own Ollama (TASK-37), so it
# never reads like the server-side ``ollama:`` model even when both are gemma4.
LOCAL_MODEL_PREFIX = "local:"
# A small_local turn hands over to the normal model if the device's Ollama has not produced
# its first delta by then (warm gemma4 answers in well under 1 s; a cold load takes longer).
LOCAL_FIRST_DELTA_TIMEOUT_S = 2.0


class _DelegatorLLMEnv(BaseSettings):
    """LLM knobs not (yet) in ``relay.config.Settings``; read from the env and ``.env``.

    ``DELEGATOR_REASONING_EFFORT``: reasoning effort sent to an ``openai:`` delegator
    model (gpt-6-luna accepts none|low|medium|high|xhigh; default ``none``).
    ``default`` omits the parameter.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    delegator_reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT


def delegator_reasoning_effort(settings: Settings) -> str | None:
    """The reasoning effort for an ``openai:`` delegator model; None = provider default.

    A ``delegator_reasoning_effort`` field on ``Settings`` wins if one is ever added;
    otherwise ``DELEGATOR_REASONING_EFFORT`` from the environment or ``.env``.
    """
    raw = getattr(settings, "delegator_reasoning_effort", None)
    if raw is None:
        raw = _DelegatorLLMEnv().delegator_reasoning_effort
    value = str(raw).strip().lower()
    return None if value in _PROVIDER_DEFAULT_EFFORTS else value


def build_model(
    ref: str,
    settings: Settings,
    *,
    reasoning_effort: str | None = DEFAULT_OPENAI_REASONING_EFFORT,
) -> ChatModel:
    """Build one model from a ``<provider>:<model>`` ref; ``model_name`` is the full ref.

    ``reasoning_effort`` applies to ``openai:`` refs only (None = OpenAI's default); the
    assent/ready/summary callers keep the fast ``none`` default. Ollama refs always send
    ``none`` and Anthropic refs send nothing.
    """
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
        # OpenAI reasoning models (gpt-6-luna, verified live 2026-09-26) reject
        # `max_tokens` (want `max_completion_tokens`) and accept a non-default
        # temperature only when reasoning_effort is "none".
        return OpenAICompatChatModel(
            model,
            api_key=settings.openai_api_key,
            name=ref,
            reasoning_effort=reasoning_effort,
            max_tokens_param="max_completion_tokens",
            send_temperature=reasoning_effort == "none",
        )
    if not settings.anthropic_api_key:
        raise ValueError(f"{ref!r} needs ANTHROPIC_API_KEY")
    return AnthropicChatModel(model, api_key=settings.anthropic_api_key, name=ref)


def _describe(model: ChatModel) -> str:
    describe = getattr(model, "describe", None)
    return describe() if callable(describe) else model.model_name


def build_chat_model(settings: Settings) -> ChatModel:
    """Primary ``delegator_model`` wrapped with ``delegator_fallback_model``.

    The wrapper is kept even when both refs are equal: a second attempt still
    rescues a transient failure before the first token. Only the conversational model
    is built here; the assent/ready/summary models come from their own settings via
    ``build_model`` and never from ``delegator_model``.
    """
    effort = delegator_reasoning_effort(settings)
    primary = build_model(settings.delegator_model, settings, reasoning_effort=effort)
    fallback = build_model(settings.delegator_fallback_model, settings, reasoning_effort=effort)
    model = FallbackChatModel(primary, fallback)
    log.info(
        "delegator conversational model: %s; fallback: %s; first-delta deadline %.0f s",
        _describe(primary),
        _describe(fallback),
        model.first_delta_timeout,
    )
    return model


def build_local_model(base_url: str, model: str) -> ChatModel:
    """The client device's Ollama (``CapabilityProfile.local_base_url``/``local_model``).

    ``base_url`` is the OpenAI-compatible base; a bare host (no ``/v1``) is accepted.
    ``model_name`` is ``local:<model>``.
    """
    root = base_url.rstrip("/").removesuffix("/v1")
    return OpenAICompatChatModel(
        model,
        api_key="ollama",
        base_url=f"{root}/v1",
        name=f"{LOCAL_MODEL_PREFIX}{model}",
        reasoning_effort="none",
    )


def with_normal_fallback(
    local: ChatModel,
    normal: ChatModel,
    *,
    first_delta_timeout: float | None = None,
) -> FallbackChatModel:
    """``local`` first; on an error or a missed first-delta deadline (default
    ``LOCAL_FIRST_DELTA_TIMEOUT_S``), the whole turn restarts on ``normal`` (the Delegator's
    usual model, itself with its own fallback)."""
    if first_delta_timeout is None:
        first_delta_timeout = LOCAL_FIRST_DELTA_TIMEOUT_S
    return FallbackChatModel(local, normal, first_delta_timeout=first_delta_timeout)
