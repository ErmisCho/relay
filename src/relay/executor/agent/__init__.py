"""The general executor: one agent that researches and codes (pydantic-ai-harness tools).

Importing this package registers the durable runner for the ``research`` task kind. The
terminal artifact is a Markdown document on disk; nothing is ever sent, posted, published,
pushed or merged.
"""

from __future__ import annotations

from relay.executor.agent.agent import (
    AGENT_NAME,
    ExecutorDeps,
    ResearchBrief,
    build_executor_model,
    build_prompt,
    configure_executor_agent,
    executor_capability,
    get_executor_agent,
    workspace_capabilities,
)
from relay.executor.agent.runner import executor_workflow, render_markdown, run_executor_task

__all__ = [
    "AGENT_NAME",
    "ExecutorDeps",
    "ResearchBrief",
    "build_executor_model",
    "build_prompt",
    "configure_executor_agent",
    "executor_capability",
    "executor_workflow",
    "get_executor_agent",
    "render_markdown",
    "run_executor_task",
    "workspace_capabilities",
]
