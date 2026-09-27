"""The executor's built-in machine_hardware and current_weather tools."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

import httpx
import pytest

from relay.executor.agent import hardware as hardware_module
from relay.executor.agent import weather as weather_module
from relay.executor.agent.hardware import machine_hardware, recommend_models
from relay.executor.agent.weather import current_weather

# ---- machine_hardware -------------------------------------------------------


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


async def test_hardware_reports_memory_gpu_and_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hardware_module, "total_memory_bytes", lambda: 64 * 1024**3)
    monkeypatch.setattr(hardware_module, "gpu_description", lambda: "Apple M4 Pro, 20 cores")
    result = await machine_hardware()
    assert "64 GB" in result
    assert "GPU: Apple M4 Pro, 20 cores" in result
    assert "qwen2.5:7b-instruct" in result


async def test_hardware_degrades_when_memory_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A platform where memory can't be read reports the chip, not a crash or a fake number."""
    monkeypatch.setattr(hardware_module, "total_memory_bytes", lambda: None)
    monkeypatch.setattr(hardware_module, "gpu_description", lambda: "not reported")
    result = await machine_hardware()
    assert "GPU: not reported" in result
    assert "memory size is unreadable" in result


async def test_macos_hardware_uses_sysctl_when_posix_memory_keys_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
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
    result = await machine_hardware()
    assert "Apple M4 Pro" in result
    assert "64 GB" in result


# ---- current_weather ----------------------------------------------------------


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[..., httpx.Response]], None]:
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        async def factory() -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(weather_module, "client_factory", factory)

    return install


def _forecast(temp: float, wind: float) -> httpx.Response:
    return httpx.Response(
        200, json={"current_weather": {"temperature": temp, "windspeed": wind, "weathercode": 3}}
    )


async def test_weather_reports_conditions(serve: Callable[..., None]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            return httpx.Response(
                200, json={"results": [{"name": "Berlin", "latitude": 52.5, "longitude": 13.4}]}
            )
        return _forecast(9.4, 12.0)

    serve(handler)
    assert await current_weather("Berlin") == (
        "In Berlin it's currently 9°C with overcast, wind 12 km/h."
    )


async def test_weather_reports_unknown_location(serve: Callable[..., None]) -> None:
    serve(lambda request: httpx.Response(200, json={"results": []}))
    assert "No place called 'Nowheresville'" in await current_weather("Nowheresville")


async def test_weather_degrades_on_upstream_failure(serve: Callable[..., None]) -> None:
    """An upstream outage returns a readable message, never an unhandled exception."""
    serve(lambda request: httpx.Response(503))
    assert "could not be reached" in await current_weather("Berlin")


async def test_weather_without_location_uses_ip_and_says_so(serve: Callable[..., None]) -> None:
    """No place named -> geolocate by IP (never the geocoder), and say it is a guess."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "geocoding-api" not in str(request.url)
        if "ipwho.is" in str(request.url):
            return httpx.Response(
                200, json={"success": True, "city": "Vienna", "latitude": 48.2, "longitude": 16.4}
            )
        return _forecast(14.2, 8.0)

    serve(handler)
    assert await current_weather() == (
        "Based on the IP address, this computer seems to be near Vienna. "
        "In Vienna it's currently 14°C with overcast, wind 8 km/h."
    )


async def test_weather_says_so_when_ip_lookup_fails(serve: Callable[..., None]) -> None:
    serve(lambda request: httpx.Response(200, json={"success": False, "message": "reserved"}))
    assert "could not be worked out" in await current_weather("  ")
