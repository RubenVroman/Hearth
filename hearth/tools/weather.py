"""Weather for the house — Open-Meteo (no API key) with fixture fallback."""

from __future__ import annotations

from typing import Any

import httpx

from hearth.config import settings
from hearth.fixtures import MOCK_WEATHER

# WMO Weather interpretation codes → short labels.
_WMO: dict[int, str] = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    80: "Rain showers",
    81: "Rain showers",
    82: "Heavy showers",
    95: "Thunderstorm",
    96: "Thunderstorm",
    99: "Thunderstorm",
}


def _condition(code: Any) -> str:
    try:
        return _WMO.get(int(code), f"Code {code}")
    except (TypeError, ValueError):
        return "Unknown"


async def fetch_weather(*, place: str | None = None) -> dict[str, Any]:
    """Current conditions near the house. Never exposes API keys (none needed)."""
    lat = settings.weather_latitude
    lon = settings.weather_longitude
    label = (place or settings.weather_place or "Home").strip() or "Home"

    if settings.weather_force_mock:
        payload = dict(MOCK_WEATHER)
        payload["place"] = label
        return payload

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            raw = response.json()
    except Exception as exc:  # noqa: BLE001
        if settings.mock_if_unconfigured:
            payload = dict(MOCK_WEATHER)
            payload["place"] = label
            payload["fallback_error"] = str(exc)[:200]
            return payload
        return {"ok": False, "error": f"weather fetch failed: {exc}", "place": label}

    current = raw.get("current") or {}
    return {
        "ok": True,
        "mode": "live",
        "place": label,
        "latitude": lat,
        "longitude": lon,
        "temperature": current.get("temperature_2m"),
        "temperature_unit": "°C",
        "humidity": current.get("relative_humidity_2m"),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_unit": "km/h",
        "weather_code": current.get("weather_code"),
        "condition": _condition(current.get("weather_code")),
        "observed_at": current.get("time"),
    }
