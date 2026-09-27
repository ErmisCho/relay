"""Composition root for the Delegator's internal tools and turn hooks."""

from __future__ import annotations

from relay.delegator.commitment import CommitmentHook, DispatchTaskTool, ProposeCommitmentTool
from relay.delegator.contracts import ToolRegistry, TurnHook
from relay.delegator.hooks.ideas import IdeaSummaryHook
from relay.delegator.hooks.reports import ReportsHook
from relay.delegator.hooks.router import RouterHook
from relay.delegator.hooks.scope import ScopeHook
from relay.delegator.hooks.voice import VoiceRulesHook
from relay.delegator.tools.hardware import HardwareCapabilitiesTool
from relay.delegator.tools.ideas import FocusIdeaTool, LinkIdeasTool, RecallTool
from relay.delegator.tools.status import GetStatusTool
from relay.delegator.tools.weather import GetWeatherTool


def build_registry() -> ToolRegistry:
    """Return the internal tools available to the upstream model."""
    registry = ToolRegistry()
    for tool in (
        GetStatusTool(),
        HardwareCapabilitiesTool(),
        GetWeatherTool(),
        RecallTool(),
        FocusIdeaTool(),
        LinkIdeasTool(),
        ProposeCommitmentTool(),
        DispatchTaskTool(),
    ):
        registry.register(tool)
    return registry


def build_hooks() -> list[TurnHook]:
    """Return turn hooks in safety-first order."""
    return [
        ScopeHook(),
        CommitmentHook(),
        ReportsHook(),
        IdeaSummaryHook(),
        RouterHook(),
        VoiceRulesHook(),
    ]
