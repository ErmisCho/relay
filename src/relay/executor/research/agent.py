"""The research agent: a Pydantic AI ``Agent`` made durable with DBOS.

Durability uses the ``DBOSDurability`` capability (pydantic-ai 2.51 deprecates the
``DBOSAgent`` wrapper in its favour): every model request is a DBOS step when the agent runs
inside a DBOS workflow (``relay.research.run`` in ``runner.py``), and the tools checkpoint
themselves as DBOS steps (see ``tools.py``).
"""

from __future__ import annotations

import os
import threading

import httpx2
from pydantic import BaseModel, Field, HttpUrl
from pydantic_ai import Agent
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

from relay.config import Settings, get_settings, parse_model_ref
from relay.executor.research.tools import fetch_url, web_search

AGENT_NAME = "relay_research"
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
        from pydantic_ai.providers.openai import OpenAIProvider

        openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=MODEL_TIMEOUT, max_retries=MODEL_MAX_RETRIES
        )
        return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=openai_client))
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


def build_research_agent(model: Model) -> Agent[None, ResearchBrief]:
    return Agent(
        model,
        name=AGENT_NAME,
        output_type=ResearchBrief,
        instructions=INSTRUCTIONS,
        tools=[web_search, fetch_url],
        capabilities=[DBOSDurability()],
        retries=2,
    )


_agent: Agent[None, ResearchBrief] | None = None
_lock = threading.Lock()


def configure_research_agent(model: Model | None = None) -> Agent[None, ResearchBrief]:
    """(Re)build the process-wide agent, e.g. with a test model before DBOS launches."""
    global _agent
    with _lock:
        _agent = build_research_agent(model if model is not None else build_research_model())
        return _agent


def get_research_agent() -> Agent[None, ResearchBrief]:
    """The configured agent, built lazily from settings on first use."""
    with _lock:
        if _agent is not None:
            return _agent
    return configure_research_agent()
