"""get_weather offline: geocoding, the forecast text, coordinates, errors (mock httpx transport)."""
import httpx

from core import weather

ok_all = True
def check(name, cond, info=None):
    global ok_all
    ok_all &= bool(cond)
    print(("OK   " if cond else "FAIL ") + name + ("" if cond or info is None else f"  {info}"))

GEO = {"results": [
    {"name": "Paris", "latitude": 33.66, "longitude": -95.55, "country": "United States", "country_code": "US", "admin1": "Texas"},
    {"name": "Paris", "latitude": 48.85, "longitude": 2.35, "country": "France", "country_code": "FR", "admin1": "Île-de-France"},
]}
FORECAST = {
    "timezone": "Europe/Paris",
    "current": {"time": "2026-11-08T14:00", "temperature_2m": 11.2, "apparent_temperature": 9.0, "relative_humidity_2m": 81,
                "precipitation": 0.0, "weather_code": 3, "wind_speed_10m": 4.1, "wind_direction_10m": 225, "wind_gusts_10m": 9.3},
    "daily": {"time": ["2026-11-08", "2026-11-09"], "weather_code": [3, 61], "temperature_2m_max": [12.0, 10.5],
              "temperature_2m_min": [6.1, 5.0], "precipitation_sum": [0.0, 4.2], "precipitation_probability_max": [10, 80],
              "wind_speed_10m_max": [5.0, 7.5]},
}
MOSCOW = "\u041c\u043e\u0441\u043a\u0432\u0430"  # the city name typed in Russian
MOSCOW_GEO = {"results": [{"name": MOSCOW, "latitude": 55.75, "longitude": 37.62, "country": "Russia"}]}
seen = []
def handler(req):
    seen.append(req)
    if "geocoding" in req.url.host:
        if req.url.params["name"] == MOSCOW:  # the service finds Cyrillic names only in a Russian request
            return httpx.Response(200, json=MOSCOW_GEO if req.url.params["language"] == "ru" else {})
        return httpx.Response(200, json=GEO if req.url.params["name"] == "Paris" else {})
    return httpx.Response(200, json=FORECAST)
T = httpx.MockTransport(handler)

text = weather.get_weather("Paris, France", days=2, transport=T)
check("country hint picks Paris, France", "Paris, Île-de-France, France" in text and "(48.85, 2.35)" in text, text)
fc = seen[-1].url.params
check("forecast request: coordinates, days, m/s, local time zone", fc["latitude"] == "48.85" and fc["forecast_days"] == "2"
      and fc["wind_speed_unit"] == "ms" and fc["timezone"] == "auto")
check("current weather line", "Now (2026-11-08 14:00): Overcast, 11.2 °C (feels like 9.0 °C)" in text
      and "wind 4.1 m/s from SW" in text, text)
check("daily lines", "2026-11-09: Light rain, 5.0..10.5 °C, precipitation 4.2 mm (80% chance), wind up to 7.5 m/s" in text, text)
check("source credited", text.endswith("Source: Open-Meteo.com"))

seen.clear()
weather.get_weather("Paris", transport=T)
check("without a hint: the first match, 3 days by default", "Texas" in weather.get_weather("Paris", transport=T)
      and seen[-1].url.params["forecast_days"] == "3")

seen.clear()
text = weather.get_weather("55.75, 37.62", days=99, transport=T)
check("coordinates skip geocoding, days are capped at 16", len(seen) == 1 and seen[0].url.params["forecast_days"] == "16"
      and "55.75, 37.62" in text)

seen.clear()
text = weather.get_weather(MOSCOW, transport=T)
check("a Cyrillic name is searched in Russian", "(55.75, 37.62)" in text and seen[0].url.params["language"] == "ru", text)
seen.clear()
weather.get_weather("Paris", transport=T)
check("a Latin name is searched in English only", [r.url.params.get("language") for r in seen if "geocoding" in r.url.host] == ["en"])

def err(location, needle, transport=T):
    try:
        weather.get_weather(location, transport=transport)
        return False
    except weather.WeatherError as e:
        return needle in str(e)
check("unknown place", err("Nowhereville", "was not found"))
check("empty place", err(" , France", "No place given"))
check("coordinates out of range", err("123, 500", "out of range"))
check("server error with a reason", err("Paris", "Open-Meteo error 400: bad latitude",
      httpx.MockTransport(lambda r: httpx.Response(400, json={"error": True, "reason": "bad latitude"}))))
def down(_req):
    raise httpx.ConnectError("no network")
check("no network", err("Paris", "unavailable", httpx.MockTransport(down)))
check("wind direction", weather.direction(0) == "N" and weather.direction(350) == "N" and weather.direction(90) == "E")
check("unknown weather code", weather.describe_code(42) == "weather code 42")

print("ALL OK" if ok_all else "SOME FAILED")
