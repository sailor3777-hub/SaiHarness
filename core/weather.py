"""Weather from Open-Meteo (free, no key), no Qt: a place name or "lat, lon" -> current weather and a daily forecast.

Geocoding: geocoding-api.open-meteo.com ("City, Country" picks the country among the matches). The service matches
names in the language of the request, so a Cyrillic name is searched in Russian first (the user writes in Russian). Forecast: api.open-meteo.com, metric units (°C, m/s, mm), the place's own time zone.
"""
import re

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 15.0
MAX_DAYS = 16

# WMO weather interpretation codes
CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Light freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Rain showers", 82: "Violent rain showers", 85: "Light snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with light hail", 99: "Thunderstorm with hail",
}
_DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
_CYRILLIC = re.compile("[\u0400-\u04ff]")
_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,; ]\s*(-?\d+(?:\.\d+)?)\s*$")


class WeatherError(Exception):
    """A clear reason for the model and the user."""


def describe_code(code) -> str:
    return CODES.get(code, f"weather code {code}") if code is not None else "unknown"


def direction(degrees) -> str:
    return _DIRECTIONS[round(float(degrees) / 45) % 8] if degrees is not None else "?"


def _get(client: httpx.Client, url: str, params: dict) -> dict:
    try:
        r = client.get(url, params=params)
    except httpx.TimeoutException:
        raise WeatherError(f"Open-Meteo did not answer within {TIMEOUT:g} s.") from None
    except httpx.HTTPError as e:
        raise WeatherError(f"Open-Meteo is unavailable: {e.__class__.__name__}.") from None
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400 or not isinstance(data, dict):
        reason = data.get("reason") if isinstance(data, dict) else None
        raise WeatherError(f"Open-Meteo error {r.status_code}: {reason or 'invalid reply'}.")
    return data


def geocode(client: httpx.Client, location: str) -> dict:
    """{name, latitude, longitude, label}: the best match; "City, Country" prefers that country."""
    m = _COORDS.match(location)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise WeatherError(f"Coordinates out of range: {location}.")
        return {"latitude": lat, "longitude": lon, "label": f"{lat:.4g}, {lon:.4g}"}
    name, _, hint = location.partition(",")
    name, hint = name.strip(), hint.strip().lower()
    if not name:
        raise WeatherError("No place given: pass a city name or \"latitude, longitude\".")
    results = []
    for language in (("ru", "en") if _CYRILLIC.search(name) else ("en",)):
        data = _get(client, GEOCODE_URL, {"name": name, "count": 10, "language": language, "format": "json"})
        results = [r for r in data.get("results") or [] if isinstance(r, dict) and "latitude" in r and "longitude" in r]
        if results:
            break
    if not results:
        raise WeatherError(f"Place \"{name}\" was not found. Try another spelling, the English name or coordinates.")
    if hint:
        matching = [r for r in results if any(hint in str(r.get(k) or "").lower()
                                              for k in ("country", "country_code", "admin1", "admin2"))]
        results = matching or results
    best = results[0]
    parts = [best.get("name"), best.get("admin1"), best.get("country")]
    label = ", ".join(dict.fromkeys(p for p in parts if p))  # without repeats like "Moscow, Moscow, Russia"
    return {"latitude": best["latitude"], "longitude": best["longitude"], "label": label}


def get_weather(location: str, days: int = 3, transport=None, timeout: float = TIMEOUT) -> str:
    """Current weather and a forecast for `days` days (1-16) as text."""
    days = max(1, min(int(days), MAX_DAYS))
    with httpx.Client(transport=transport, timeout=timeout, headers={"User-Agent": "SaiHarness/1.0"}) as client:
        place = geocode(client, location)
        data = _get(client, FORECAST_URL, {
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
            "forecast_days": days, "wind_speed_unit": "ms",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,"
                       "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max,wind_speed_10m_max",
        })
    cur = data.get("current") or {}
    lines = [f"Weather for {place['label']} ({place['latitude']:.2f}, {place['longitude']:.2f}), "
             f"time zone {data.get('timezone') or '?'}"]
    if cur:
        lines.append(
            f"Now ({str(cur.get('time') or '').replace('T', ' ')}): {describe_code(cur.get('weather_code'))}, "
            f"{cur.get('temperature_2m')} °C (feels like {cur.get('apparent_temperature')} °C), "
            f"humidity {cur.get('relative_humidity_2m')}%, wind {cur.get('wind_speed_10m')} m/s from "
            f"{direction(cur.get('wind_direction_10m'))}, gusts {cur.get('wind_gusts_10m')} m/s, "
            f"precipitation {cur.get('precipitation')} mm")
    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    if dates:
        lines.append("Forecast by day:")
        col = lambda key, i: (daily.get(key) or [None] * len(dates))[i]
        for i, day in enumerate(dates):
            chance = col("precipitation_probability_max", i)
            lines.append(
                f"{day}: {describe_code(col('weather_code', i))}, {col('temperature_2m_min', i)}..{col('temperature_2m_max', i)} °C, "
                f"precipitation {col('precipitation_sum', i)} mm" + (f" ({chance}% chance)" if chance is not None else "")
                + f", wind up to {col('wind_speed_10m_max', i)} m/s")
    lines.append("Source: Open-Meteo.com")
    return "\n".join(lines)
