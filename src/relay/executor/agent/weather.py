"""Executor tool ``current_weather``: live conditions via Open-Meteo (free, keyless).

With no location it geolocates this machine's public IP (ipwho.is, keyless): relay runs on
the user's own machine. The answer says the place is an IP-based guess, because IP location
is coarse (services disagreed: Vienna vs Braunau am Inn, 2026-09-27).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

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


#: Swapped in tests; not a tool parameter, so it stays out of the tool schema.
client_factory: Callable[[], Awaitable[httpx.AsyncClient]] = _default_client_factory


async def current_weather(location: str | None = None) -> str:
    """Current weather. Leave ``location`` empty when the user named no place: it is then
    located from this computer's IP address, and the answer says so.

    Args:
        location: A place name such as "Berlin" or "Berlin, DE", only if the user named one.
    """
    location = (location or "").strip()
    by_ip = not location
    try:
        async with await client_factory() as client:
            if by_ip:
                geo = await client.get(_IP_GEO_URL)
                geo.raise_for_status()
                place = geo.json()
                if not place.get("success"):
                    return "This computer's location could not be worked out from its IP address."
            else:
                geo = await client.get(_GEOCODE_URL, params={"name": location, "count": 1})
                geo.raise_for_status()
                results = geo.json().get("results") or []
                if not results:
                    return f"No place called {location!r} was found."
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
        where = "this computer's location" if by_ip else repr(location)
        return f"The weather service could not be reached for {where}."

    if not current:
        return f"No current conditions are available for {label}."

    sky = describe_wmo_code(int(current["weathercode"]))
    temp = current["temperature"]
    wind = current["windspeed"]
    prefix = f"Based on the IP address, this computer seems to be near {label}. " if by_ip else ""
    return f"{prefix}In {label} it's currently {temp:.0f}°C with {sky}, wind {wind:.0f} km/h."
