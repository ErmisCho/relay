"""hardware_capabilities and get_weather: direct tool tests, not the tool loop.

Deliberate scope expansion beyond SPEC.md's two verticals (TASK-48).
"""

from __future__ import annotations

import subprocess
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.contracts import SessionState, ToolContext
from relay.delegator.tools import hardware as hardware_module
from relay.delegator.tools.hardware import HardwareCapabilitiesTool, recommend_models
from relay.delegator.tools.weather import GetWeatherTool

from .conftest import make_settings


def _ctx(db: async_sessionmaker[AsyncSession]) -> ToolContext:
    return ToolContext(state=SessionState(session_id=uuid.uuid4()), db=db, settings=make_settings())


# ---- hardware_capabilities -------------------------------------------------


@pytest.mark.parametrize(
    ("total_gb", "expected_fragment"),
    [
        (64, "qwen2.5:7b-instruct"),
        (16, "qwen2.5:3b-instruct"),
        (8, "llama3.2:1b"),
    ],
)
def test_recommend_models_tiers(total_gb: float, expected_fragment: str) -> None:
    """Each memory tier names a model that actually fits it, not a neighbouring tier's pick."""
    assert expected_fragment in recommend_models(total_gb)


async def test_hardware_tool_reports_memory_and_recommendation(
    monkeypatch: pytest.MonkeyPatch, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    monkeypatch.setattr(hardware_module, "total_memory_bytes", lambda: 64 * 1024**3)
    monkeypatch.setattr(hardware_module, "gpu_description", lambda: "Apple M4 Pro, 20 cores")
    result = await HardwareCapabilitiesTool()({}, _ctx(dead_db))
    assert "64 GB" in result.content
    assert "GPU: Apple M4 Pro, 20 cores" in result.content
    assert "qwen2.5:7b-instruct" in result.content
    assert not result.rejected


async def test_hardware_tool_degrades_when_memory_unreadable(
    monkeypatch: pytest.MonkeyPatch, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    """A platform where memory can't be read reports the chip, not a crash or a fake number."""
    monkeypatch.setattr(hardware_module, "total_memory_bytes", lambda: None)
    monkeypatch.setattr(hardware_module, "gpu_description", lambda: "not reported")
    result = await HardwareCapabilitiesTool()({}, _ctx(dead_db))
    assert "GPU: not reported" in result.content
    assert "can't read its memory" in result.content


async def test_macos_hardware_uses_sysctl_when_posix_memory_keys_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, dead_db: async_sessionmaker[AsyncSession]
) -> None:
    monkeypatch.setattr(hardware_module.sys, "platform", "darwin")
    monkeypatch.setattr(hardware_module, "gpu_description", lambda: "Apple M4 Pro, 20 cores")

    def unsupported_sysconf(key: str) -> int:
        raise ValueError("unrecognized configuration name")

    def sysctl(command: list[str], **kwargs: object) -> str:
        assert command[:2] == ["/usr/sbin/sysctl", "-n"]
        assert kwargs.get("timeout") is not None
        return {
            "hw.memsize": str(64 * 1024**3),
            "machdep.cpu.brand_string": "Apple M4 Pro",
        }[command[2]]

    monkeypatch.setattr(hardware_module.os, "sysconf", unsupported_sysconf, raising=False)
    monkeypatch.setattr(subprocess, "check_output", sysctl)
    result = await HardwareCapabilitiesTool()({}, _ctx(dead_db))
    assert "Apple M4 Pro" in result.content
    assert "64 GB" in result.content


# ---- get_weather ------------------------------------------------------------


def _client_factory(handler):  # type: ignore[no-untyped-def]
    async def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return factory


async def test_weather_tool_reports_conditions(dead_db: async_sessionmaker[AsyncSession]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            return httpx.Response(
                200, json={"results": [{"name": "Berlin", "latitude": 52.5, "longitude": 13.4}]}
            )
        return httpx.Response(
            200,
            json={"current_weather": {"temperature": 9.4, "windspeed": 12.0, "weathercode": 3}},
        )

    tool = GetWeatherTool(client_factory=_client_factory(handler))
    result = await tool({"location": "Berlin"}, _ctx(dead_db))
    assert result.content == "In Berlin it's currently 9°C with overcast, wind 12 km/h."
    assert not result.rejected


async def test_weather_tool_reports_unknown_location(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    tool = GetWeatherTool(client_factory=_client_factory(handler))
    result = await tool({"location": "Nowheresville"}, _ctx(dead_db))
    assert "couldn't find" in result.content


async def test_weather_tool_degrades_on_upstream_failure(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    """An upstream outage returns a speakable ToolResult, never an unhandled exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    tool = GetWeatherTool(client_factory=_client_factory(handler))
    result = await tool({"location": "Berlin"}, _ctx(dead_db))
    assert "couldn't reach the weather service" in result.content


async def test_weather_tool_rejects_empty_location(
    dead_db: async_sessionmaker[AsyncSession],
) -> None:
    tool = GetWeatherTool()
    result = await tool({"location": "  "}, _ctx(dead_db))
    assert result.rejected
