"""Composition root for the Delegator's internal tools and turn hooks.

Owned by the coordinator. Each feature module exposes its tools/hooks and is
registered here; the Delegator app only ever calls ``build_registry`` and
``build_hooks``.
"""

from __future__ import annotations

from relay.delegator.contracts import ToolRegistry, TurnHook
from relay.delegator.hooks.ideas import IdeaSummaryHook
from relay.delegator.hooks.reports import ReportsHook
from relay.delegator.hooks.scope import ScopeHook
from relay.delegator.tools.ideas import FocusIdeaTool, LinkIdeasTool, RecallTool
from relay.delegator.tools.status import GetStatusTool


def build_registry() -> ToolRegistry:
    """Return the internal tools available to the upstream model."""
    registry = ToolRegistry()
    registry.register(GetStatusTool())
    registry.register(RecallTool())
    registry.register(FocusIdeaTool())
    registry.register(LinkIdeasTool())
    return registry


def build_hooks() -> list[TurnHook]:
    """Return the turn hooks, in the order they run.

    ScopeHook goes first so the scope rules and any refusal note lead the
    per-turn system notes.
    """
    hooks: list[TurnHook] = [ScopeHook(), ReportsHook(), IdeaSummaryHook()]
    return hooks
