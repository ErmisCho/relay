"""``get_weather`` internal tool: live conditions for a named location.

Deliberate scope expansion beyond SPEC.md's two verticals (user decision,
2026-09-26): answers directly in conversation, never through the commitment
protocol. Uses Open-Meteo (free, keyless) so no settings/API key surface is
needed. With no location it geolocates this machine's public IP (ipwho.is,
keyless): the Delegator runs on the user's own machine, the same assumption
``hardware_capabilities`` makes. The request's own client IP would be
ElevenLabs' cloud, not the user.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from relay.delegator.contracts import ToolContext, ToolResult

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_IP_GEO_URL = "https://ipwho.is/"

# WMO weather codes (https://open-meteo.com/en/docs), collapsed to short phrases.
_WMO_CODES: dict[int, str] = {
    0: "clear sky",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    95: "thunderstorm",
}


def describe_wmo_code(code: int) -> str:
    return _WMO_CODES.get(code, "unsettled weather")


async def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=5.0)


class GetWeatherTool:
    """``InternalTool`` reporting current weather for a named location."""

    name = "get_weather"
    direct_response = True
    description = (
        "Look up current weather. If the user names no place, call it WITHOUT a location - "
        "it is then located from the user's IP address. Don't ask the user for a city."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "A place name, e.g. 'Berlin' or 'Berlin, DE'. Omit if not named.",
            }
        },
        "additionalProperties": False,
    }

    def __init__(
        self,
        client_factory: Callable[[], Awaitable[httpx.AsyncClient]] = _default_client_factory,
    ) -> None:
        self._client_factory = client_factory

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        location = str(args.get("location") or "").strip()
        by_ip = not location
        try:
            async with await self._client_factory() as client:
                if by_ip:
                    geo = await client.get(_IP_GEO_URL)
                    geo.raise_for_status()
                    place = geo.json()
                    if not place.get("success"):
                        return ToolResult("I couldn't work out where you are - which city?")
                else:
                    geo = await client.get(_GEOCODE_URL, params={"name": location, "count": 1})
                    geo.raise_for_status()
                    results = geo.json().get("results") or []
                    if not results:
                        return ToolResult(f"I couldn't find a place called {location!r}.")
                    place = results[0]
                lat, lon = place["latitude"], place["longitude"]
                label = place.get("city") or place.get("name") or location

                fc = await client.get(
                    _FORECAST_URL,
                    params={"latitude": lat, "longitude": lon, "current_weather": "true"},
                )
                fc.raise_for_status()
                current = fc.json().get("current_weather")
        except (httpx.HTTPError, KeyError, ValueError):
            where = "your location" if by_ip else repr(location)
            return ToolResult(f"I couldn't reach the weather service for {where} right now.")

        if not current:
            return ToolResult(f"I couldn't get current conditions for {label}.")

        sky = describe_wmo_code(int(current["weathercode"]))
        temp = current["temperature"]
        wind = current["windspeed"]
        # Spoken verbatim (direct_response): say the place is a guess, or a wrong city
        # sounds like a fact.
        prefix = f"Based on your IP address you seem to be near {label}. " if by_ip else ""
        return ToolResult(
            f"{prefix}In {label} it's currently {temp:.0f}°C with {sky}, wind {wind:.0f} km/h."
        )
