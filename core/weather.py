"""Weather, in one place, from a source that needs no key.

This exists because there were two answers to "what is the weather", and one of
them was a lie. `actions/weather_report.py` opened a Google search in a browser
and reported "Showing the weather for London" — which is not a weather report,
and on a headless Space cannot happen at all. The ceremony meanwhile used
Open-Meteo properly. Two implementations, one of them fake, is how a feature
ends up looking broken while the code looks fine.

So: one module. The ceremony calls it, the weather tool calls it, and there is
no third opinion available.

Open-Meteo needs no API key and no OAuth, which matters here because the Space
has no secrets to leak and nothing to rotate. Every failure path returns None
rather than raising: weather is a garnish, and a ceremony that dies because a
weather API hiccupped is a worse product than one that skips the weather.
"""

from typing import Optional

#: WMO weather interpretation codes -> (plain-English description, kind).
#: The kind is what the prose branches on — "is it going to rain" is a
#: different sentence from "what is the weather like".
WMO = {
    0: ("clear", "clear"), 1: ("mostly clear", "clear"), 2: ("partly cloudy", "cloud"),
    3: ("overcast", "cloud"), 45: ("fog", "fog"), 48: ("freezing fog", "fog"),
    51: ("light drizzle", "rain"), 53: ("drizzle", "rain"), 55: ("heavy drizzle", "rain"),
    61: ("light rain", "rain"), 63: ("rain", "rain"), 65: ("heavy rain", "rain"),
    66: ("freezing rain", "rain"), 67: ("heavy freezing rain", "rain"),
    71: ("light snow", "snow"), 73: ("snow", "snow"), 75: ("heavy snow", "snow"),
    77: ("snow grains", "snow"), 80: ("light showers", "rain"),
    81: ("showers", "rain"), 82: ("violent showers", "rain"),
    85: ("light snow showers", "snow"), 86: ("snow showers", "snow"),
    95: ("thunderstorm", "rain"), 96: ("thunderstorm with hail", "rain"),
    99: ("thunderstorm with hail", "rain"),
}


def _env(name: str, default: str = "") -> str:
    import os
    return (os.environ.get(name) or default).strip()


def city() -> str:
    """Where the user is. Empty means unset — never guessed.

    A wrong city is worse than no city: the user gets a confident forecast for
    somewhere they are not, and has no way to tell it is wrong.

    Read from the settings store, which is what the dashboard writes. An explicit
    JARVIS_CITY still wins, because an operator setting it in the Space is
    making a statement about the deployment.
    """
    try:
        from core import settings as _set
        return _set.get("city", "")
    except Exception:
        return _env("JARVIS_CITY", "")


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(str(s or ""))


def _get_json(url: str, timeout: float = 6.0) -> Optional[dict]:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _geocode(place: str, timeout: float) -> Optional[dict]:
    geo = _get_json(
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={_q(place)}&count=1&language=en&format=json", timeout)
    results = (geo or {}).get("results") or []
    return results[0] if results else None


def weather(place: str = "", *, timeout: float = 6.0) -> Optional[dict]:
    """Current conditions for `place`. No API key; None if it cannot be had."""
    place = (place or city()).strip()
    if not place:
        return None
    r0 = _geocode(place, timeout)
    if not r0:
        return None
    data = _get_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={r0['latitude']}&longitude={r0['longitude']}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
        "precipitation,weather_code,wind_speed_10m"
        "&daily=temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max&timezone=auto&forecast_days=1",
        timeout)
    if not data:
        return None
    cur = data.get("current") or {}
    day = data.get("daily") or {}
    code = int(cur.get("weather_code") or 0)
    desc, kind = WMO.get(code, ("unsettled", "cloud"))
    return {
        "place": r0.get("name") or place,
        "temp": cur.get("temperature_2m"),
        "feels": cur.get("apparent_temperature"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind": cur.get("wind_speed_10m"),
        "code": code, "desc": desc, "kind": kind,
        "high": (day.get("temperature_2m_max") or [None])[0],
        "low": (day.get("temperature_2m_min") or [None])[0],
        "rain_chance": (day.get("precipitation_probability_max") or [None])[0],
    }


def forecast(place: str = "", days: int = 3, *, timeout: float = 6.0) -> list[dict]:
    """The next few days, so "what's the weather like this week" has an answer.

    Returns [] rather than None so a caller can tell "no forecast" from
    "forecast unavailable" without a second round trip.
    """
    place = (place or city()).strip()
    if not place:
        return []
    days = max(1, min(int(days or 1), 7))
    r0 = _geocode(place, timeout)
    if not r0:
        return []
    data = _get_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={r0['latitude']}&longitude={r0['longitude']}"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max&timezone=auto"
        f"&forecast_days={days}", timeout)
    daily = (data or {}).get("daily") or {}
    dates = daily.get("time") or []
    out: list[dict] = []
    for i, d in enumerate(dates):
        try:
            code = int((daily.get("weather_code") or [0] * len(dates))[i])
        except Exception:
            code = 0
        desc, kind = WMO.get(code, ("unsettled", "cloud"))
        out.append({
            "date": d,
            "desc": desc, "kind": kind,
            "high": _at(daily.get("temperature_2m_max"), i),
            "low": _at(daily.get("temperature_2m_min"), i),
            "rain_chance": _at(daily.get("precipitation_probability_max"), i),
        })
    return out


def _at(seq, i: int):
    try:
        return (seq or [])[i]
    except Exception:
        return None


def weather_line(w: Optional[dict]) -> str:
    """Turn conditions into something a person would say.

    The point is the second clause. "It's 14 degrees" is a weather report;
    "14 degrees and it'll rain by noon, you'll want an umbrella" is somebody
    who was paying attention.
    """
    if not w:
        return ""
    try:
        t = round(float(w.get("temp")))
    except Exception:
        return ""
    kind = w.get("kind") or "cloud"
    rain = w.get("rain_chance")
    parts = [f"{t} degrees and {w.get('desc') or 'unsettled'}"
             f" in {w.get('place') or 'your area'}"]
    # Snow is checked FIRST. It used to be the elif, so snow with a high
    # precipitation probability fell into the rain branch and was told to
    # take an umbrella — advice for the wrong weather, given with total
    # confidence, which is the worst kind of wrong.
    if kind == "snow":
        parts.append("it's snowing, so wrap up warm")
    elif kind == "rain" or (rain is not None and int(rain) >= 55):
        when = f"{int(rain)} percent chance" if rain is not None else "a fair chance"
        parts.append(f"{when} of rain, so take an umbrella")
    elif kind == "storm":
        parts.append("there's weather worth watching")
    try:
        feels = float(w.get("feels"))
        if feels >= 28:
            parts.append(f"it'll feel like {round(feels)}, so keep drinking water")
        elif feels <= 2:
            parts.append(f"it'll feel closer to {round(feels)}, coat weather")
    except Exception:
        pass
    try:
        if float(w.get("wind") or 0) >= 38:
            parts.append("and it's blowing")
    except Exception:
        pass
    if kind == "clear" and 12 <= t <= 24:
        parts.append("which is as good as it gets")
    return ", ".join(parts) + "."

def forecast_line(days: list[dict], place: str = "") -> str:
    """A week, read out. Names the worst day, because that is the useful one."""
    if not days:
        return ""
    place = place or (days[0].get("place") or city() or "your area")
    out = []
    for d in days[:5]:
        try:
            hi, lo = round(float(d.get("high"))), round(float(d.get("low")))
        except Exception:
            continue
        rain = d.get("rain_chance")
        wet = f", {int(rain)}% rain" if rain is not None and int(rain) >= 40 else ""
        out.append(f"{d.get('date')} {d.get('desc')}, {hi}/{lo}{wet}")
    if not out:
        return ""
    return f"Forecast for {place}: " + "; ".join(out) + "."


def report(place: str = "", when: str = "today", *, timeout: float = 6.0) -> str:
    """One answer for a person who asked out loud.

    Never raises and never invents. No city configured, an unknown city, or an
    unreachable API each produce a sentence that says so, because a weather tool
    that guesses is worse than one that admits it does not know.
    """
    place = (place or city()).strip()
    when = (when or "today").strip().lower()
    if not place:
        return ("I do not have a city set, so I cannot give you a real forecast. "
                "Set one in the dashboard and I will.")
    if when in ("week", "forecast", "outlook", "5 day", "five day"):
        days = forecast(place, 5, timeout=timeout)
        if not days:
            return f"I could not reach the weather service for {place}."
        return forecast_line(days, place)
    if when not in ("today", "now", "current"):
        try:
            n = int(when)
        except Exception:
            n = 1
        days = forecast(place, max(1, min(n, 7)), timeout=timeout)
        if not days:
            return f"I could not reach the weather service for {place}."
        return forecast_line(days, place)
    w = weather(place, timeout=timeout)
    if not w:
        return f"I could not get the weather for {place}. The service may be down."
    line = weather_line(w)
    # str.capitalize() lowercases everything after the first character, which
    # turned "in Oslo" into "in oslo". Only the first letter is ours to raise.
    return line[:1].upper() + line[1:] if line else line