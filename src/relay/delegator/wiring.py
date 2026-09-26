"""Composition root for the Delegator's internal tools and turn hooks.

Owned by the coordinator. Each feature module exposes its tools/hooks and is
registered here; the Delegator app only ever calls ``build_registry`` and
``build_hooks``.
"""

from __future__ import annotations

from relay.delegator.contracts import ToolRegistry, TurnHook
from relay.delegator.hooks.reports import ReportsHook
from relay.delegator.tools.status import GetStatusTool


def build_registry() -> ToolRegistry:
    """Return the internal tools available to the upstream model."""
    registry = ToolRegistry()
    registry.register(GetStatusTool())
    return registry


def build_hooks() -> list[TurnHook]:
    """Return the turn hooks, in the order they run."""
    hooks: list[TurnHook] = [ReportsHook()]
    return hooks
