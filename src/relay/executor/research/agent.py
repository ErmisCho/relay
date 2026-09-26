"""The research agent: a Pydantic AI ``Agent`` made durable with DBOS.

Durability uses the ``DBOSDurability`` capability (pydantic-ai 2.51 deprecates the
``DBOSAgent`` wrapper in its favour): every model request is a DBOS step when the agent runs
inside a DBOS workflow (``relay.research.run`` in ``runner.py``), and the tools checkpoint
themselves as DBOS steps (see ``tools.py``).

Difficulty routing (TASK-46): the one agent carries a model per route, registered with
``DBOSDurability(models=...)`` under the keys ``easy`` and ``hard``, and each run picks one by
key (``agent.run_sync(..., model="hard")``). Step names do not depend on the model, so the
durable history of a run is the same whichever route it took. The agent's own default model
(``research_model`` -> ``research_fallback_model``) serves runs without a route.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping

import httpx2
from pydantic import BaseModel, Field, HttpUrl
from pydantic_ai import Agent
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from relay.config import Settings, get_settings, parse_model_ref
from relay.executor.research.tools import fetch_url, web_search
from relay.executor.routing import LABELS, Difficulty

log = logging.getLogger(__name__)

AGENT_NAME = "relay_research"
# A route is a router difficulty; it is also the model's key in the DBOSDurability registry.
Route = Difficulty
ROUTES: tuple[Route, ...] = LABELS
# Reasoning effort for `openai:` research models. Research quality matters more than latency
# (a run takes minutes; a reasoning pass per request is cheap next to that), so this keeps
# OpenAI's own default for gpt-6-luna, "medium", but pins it so a provider default change is
# not silent. "none" is what the voice path uses; "default" omits the parameter.
DEFAULT_OPENAI_REASONING_EFFORT = "medium"
_PROVIDER_DEFAULT_EFFORTS = frozenset({"", "default"})
# Local generation is slow, so reads get 2 minutes; a dead endpoint fails fast on connect.
MODEL_TIMEOUT = httpx2.Timeout(120.0, connect=5.0)  # openai/anthropic SDKs are built on httpx2
# FallbackModel is the retry: an SDK retry loop would only delay switching to the fallback.
MODEL_MAX_RETRIES = 0

# pydantic-ai prints a multi-line "observability" banner on first run; keep worker logs clean.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


class ResearchBrief(BaseModel):
    """The agent's structured output, rendered to Markdown by the runner."""

    title: str = Field(min_length=1, description="Short document title, no Markdown.")
    summary: str = Field(min_length=1, description="Two or three plain sentences.")
    body_markdown: str = Field(
        min_length=1, description="The document body in Markdown, without a Sources section."
    )
    sources: list[HttpUrl] = Field(description="URLs actually fetched or read and relied on.")


INSTRUCTIONS = """\
You are relay's research and writing executor. You produce ONE written brief for the user to
review later. You never send, post, book, buy or publish anything; you only read the web.

Method:
1. Use web_search to find relevant, reputable pages, then fetch_url to read the best ones.
2. Write the brief from what you actually read. Do not invent facts, figures or URLs.
3. Stay strictly inside the goal. The "Out of scope" text is binding: do not research,
   discuss, recommend or even mention anything it excludes.
4. List in `sources` only URLs you fetched or that search results showed and you relied on.
   Do not put a Sources section inside body_markdown; it is added automatically.
"""


def build_prompt(goal: str, scope_excludes: str) -> str:
    """User prompt carrying the commitment's goal and exclusions verbatim."""
    excludes = scope_excludes if scope_excludes.strip() else "(nothing explicitly excluded)"
    return f"Goal:\n{goal}\n\nOut of scope (must be respected):\n{excludes}\n"


class _ResearchLLMEnv(BaseSettings):
    """Research knobs not in ``relay.config.Settings``; read from the env and ``.env``.

    ``RESEARCH_REASONING_EFFORT``: reasoning effort sent to ``openai:`` research models
    (gpt-6-luna accepts none|low|medium|high|xhigh; default ``medium``; ``default`` omits it).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    research_reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT


def research_reasoning_effort() -> str | None:
    """The reasoning effort for ``openai:`` research models; None = provider default."""
    value = _ResearchLLMEnv().research_reasoning_effort.strip().lower()
    return None if value in _PROVIDER_DEFAULT_EFFORTS else value


def build_model_from_ref(ref: str, settings: Settings) -> Model:
    """One concrete model for ``<provider>:<model>``; Ollama via its OpenAI-compatible API."""
    provider, name = parse_model_ref(ref)
    if provider in ("ollama", "openai"):
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel

        if provider == "ollama":
            from pydantic_ai.providers.ollama import OllamaProvider

            # OllamaProvider's profile maps Ollama's `reasoning` field (qwen3/gemma thinking)
            # to ThinkingParts, so thinking never leaks into the structured output.
            ollama_client = AsyncOpenAI(
                base_url=settings.ollama_base_url,
                api_key=os.environ.get("OLLAMA_API_KEY") or "ollama",
                timeout=MODEL_TIMEOUT,
                max_retries=MODEL_MAX_RETRIES,
            )
            return OpenAIChatModel(name, provider=OllamaProvider(openai_client=ollama_client))
        from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
        from pydantic_ai.providers.openai import OpenAIProvider

        if not settings.openai_api_key:
            raise ValueError(f"{ref!r} needs OPENAI_API_KEY")
        openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=MODEL_TIMEOUT, max_retries=MODEL_MAX_RETRIES
        )
        # The Responses API, not Chat Completions: gpt-6-luna rejects function tools together
        # with a reasoning effort on /v1/chat/completions (400, verified live 2026-09-26), and
        # the agent needs both. Responses also carries reasoning across tool calls.
        # pydantic-ai's OpenAI profile knows gpt-6-luna as a reasoning model (drops sampling
        # params while reasoning is on).
        model_settings = OpenAIResponsesModelSettings()
        if (effort := research_reasoning_effort()) is not None:
            model_settings["openai_reasoning_effort"] = effort  # type: ignore[typeddict-item]
        return OpenAIResponsesModel(
            name, provider=OpenAIProvider(openai_client=openai_client), settings=model_settings
        )
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    anthropic_client = AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=MODEL_TIMEOUT, max_retries=MODEL_MAX_RETRIES
    )
    return AnthropicModel(name, provider=AnthropicProvider(anthropic_client=anthropic_client))


def build_research_model(settings: Settings | None = None) -> FallbackModel:
    """``research_model`` with ``research_fallback_model`` behind it (on any ModelAPIError)."""
    s = settings or get_settings()
    return FallbackModel(
        build_model_from_ref(s.research_model, s),
        build_model_from_ref(s.research_fallback_model, s),
    )


def _chain(primary: str, fallback: str | None, s: Settings) -> Model:
    """``primary`` alone, or wrapped with ``fallback`` when that is a different model."""
    first = build_model_from_ref(primary, s)
    if fallback is None or fallback == primary:
        return first
    return FallbackModel(first, build_model_from_ref(fallback, s))


def route_model_refs(route: Route, settings: Settings | None = None) -> tuple[str, str | None]:
    """(primary, fallback) model refs for a route.

    easy: ``research_easy_model``; ``research_fallback_model`` backs it only when it differs
    (both default to local gemma4, and a second attempt on the same dead Ollama would only
    double the wait). hard: ``research_hard_model`` -> ``research_hard_fallback_model``.
    """
    s = settings or get_settings()
    if route == "easy":
        fallback = s.research_fallback_model
        return s.research_easy_model, None if fallback == s.research_easy_model else fallback
    return s.research_hard_model, s.research_hard_fallback_model


def build_route_models(settings: Settings | None = None) -> dict[Route, Model]:
    """The model (chain) per route, from settings.

    If the hard primary cannot be built (e.g. no OPENAI_API_KEY) hard tasks run on the hard
    fallback alone, with a warning: the served model is recorded per task, so it is visible.
    """
    s = settings or get_settings()
    models: dict[Route, Model] = {}
    for route in ROUTES:
        primary, fallback = route_model_refs(route, s)
        try:
            models[route] = _chain(primary, fallback, s)
        except ValueError as exc:
            if fallback is None:
                raise
            log.warning(
                "research %s model %s unavailable (%s); using %s", route, primary, exc, fallback
            )
            models[route] = build_model_from_ref(fallback, s)
    return models


def build_research_agent(
    model: Model, routes: Mapping[Route, Model] | None = None
) -> Agent[None, ResearchBrief]:
    return Agent(
        model,
        name=AGENT_NAME,
        output_type=ResearchBrief,
        instructions=INSTRUCTIONS,
        tools=[web_search, fetch_url],
        capabilities=[DBOSDurability(models={str(k): m for k, m in (routes or {}).items()})],
        retries=2,
    )


_agent: Agent[None, ResearchBrief] | None = None
_lock = threading.Lock()


def configure_research_agent(
    model: Model | None = None, routes: Mapping[Route, Model] | None = None
) -> Agent[None, ResearchBrief]:
    """(Re)build the process-wide agent, e.g. with test models before DBOS launches.

    ``model`` serves runs without a route; ``routes`` maps ``easy``/``hard`` to their models.
    Each defaults to the models built from settings.
    """
    global _agent
    with _lock:
        _agent = build_research_agent(
            model if model is not None else build_research_model(),
            routes if routes is not None else build_route_models(),
        )
        return _agent


def get_research_agent() -> Agent[None, ResearchBrief]:
    """The configured agent, built lazily from settings on first use."""
    with _lock:
        if _agent is not None:
            return _agent
    return configure_research_agent()
