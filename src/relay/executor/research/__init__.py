"""Research & writing executor (the only Phase-1 vertical).

Importing this package registers the durable ``research`` runner. The terminal artifact is a
Markdown document on disk; nothing is ever sent, posted or published.
"""

from __future__ import annotations

from relay.executor.research.agent import (
    AGENT_NAME,
    ResearchBrief,
    build_prompt,
    build_research_model,
    configure_research_agent,
    get_research_agent,
)
from relay.executor.research.runner import render_markdown, research_workflow, run_research
from relay.executor.research.tools import FetchError, fetch_url, fetch_url_text, web_search

__all__ = [
    "AGENT_NAME",
    "FetchError",
    "ResearchBrief",
    "build_prompt",
    "build_research_model",
    "configure_research_agent",
    "fetch_url",
    "fetch_url_text",
    "get_research_agent",
    "render_markdown",
    "research_workflow",
    "run_research",
    "web_search",
]
