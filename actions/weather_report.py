import webbrowser
from urllib.parse import quote_plus


from core.weather import city as configured_city
from core.weather import report as weather_report


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    """Answer with the weather, not with a search engine.

    This used to open a Google search in a browser and report "Showing the
    weather for London" — which told the user nothing, and could not work at all
    on the Space, where there is no browser to open. A tool that cannot answer
    the question it exists to answer is worse than no tool, because it looks
    like an answer.

    The city is optional here. If the dashboard has one set, "what's the
    weather" needs no argument at all, which is how anyone actually asks.
    """
    params    = parameters or {}
    place     = (params.get("city") or "").strip() or configured_city()
    when      = (params.get("time") or params.get("when") or "today").strip()

    msg = weather_report(place, when)
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=f"weather in {place} {when}",
                                          response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": (
        "Real current weather and short forecast for a city, from Open-Meteo. "
        "Reads like a person, not an API. With no city it uses the one set in "
        "the dashboard. Use this for any weather question."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City name. Optional if one is set in the dashboard."
            },
            "time": {
                "type": "STRING",
                "description": "'today' (default), or 'week' for the next few days."
            }
        },
        "required": []
    },
    "handler": weather_action,
}
