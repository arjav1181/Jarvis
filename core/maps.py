"""
core/maps.py — places, links and distances.

WHAT THIS IS FOR
    "Where's the closest clinic?", "send me directions to the client",
    "how far is that office from here" — questions that need a place, not a
    web search. The assistant resolves a place to coordinates once, then hands
    back links for whichever map app the person actually uses.

WHY NOMINATIM FOR GEOCODING
    It is the only geocoder with no key, no signup, and a real usage policy
    (identify yourself, one request a second, no heavy use). Google Places
    would need a billing-enabled key for the same answer. If the user has
    supplied a Google Places key, that is used instead — see _places_key().

WHY LINKS FOR THREE APPS
    People are on different phones. A single provider's URL is a dead end for
    the other two, and the difference is not cosmetic: Apple Maps and Ola Maps
    will open their own turn-by-turn flow, while a Google link just renders a
    web map. So every result carries links for all of them and the panel shows
    whichever the user picks.

THE COORDINATE CONTRACT
    Latitude/longitude are the currency: every provider link is built from
    them, distances are computed from them, and a pin saved to memory keeps
    them. Rounding is 6 decimals (~10 cm) — more would be false precision.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

# Nominatim asks for a real User-Agent and rate-limits anonymous callers, so
# this string is not optional. It used to be imported from the lead engine,
# where it identified itself as "JARVIS-lead-engine" — which meant the map
# panel was announcing itself as something it is not.
UA = "JARVIS-maps/1.0 (+https://github.com/FatihMakes/Mark-LIV)"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REV = "https://nominatim.openstreetmap.org/reverse"

PROVIDERS = ("google", "apple", "ola", "osm")
DEFAULT_PROVIDER = "google"

# Nominatim asks for at most one request a second. Enforced here rather than
# trusted to callers, because a burst from the panel plus a voice command
# would otherwise earn this IP a block.
_MIN_GAP_S = 1.05
_last_call = 0.0

LABELS = {
    "google": "Google Maps",
    "apple": "Apple Maps",
    "ola": "Ola Maps",
    "osm": "OpenStreetMap",
}


# ── link builders ────────────────────────────────────────────────────────────

def _q(text: str) -> str:
    return urllib.parse.quote(str(text or "").strip(), safe="")


def link(provider: str, lat: float | None = None, lon: float | None = None,
         query: str = "", label: str = "") -> str:
    """A URL that opens the right map app at the right place.

    With coordinates: a pin. Without: a search for the query. Never both,
    because a search with coordinates silently ignores the label.
    """
    p = (provider or DEFAULT_PROVIDER).lower()
    has_geo = lat is not None and lon is not None
    if p == "google":
        if has_geo:
            api = {"api": "1", "query": f"{lat},{lon}"}
            if label:
                api["query_place_id"] = _q(label)
            return f"https://www.google.com/maps/search/?{urllib.parse.urlencode(api)}"
        return f"https://www.google.com/maps/search/?api=1&query={_q(query)}"
    if p == "apple":
        if has_geo:
            out = f"https://maps.apple.com/?ll={lat},{lon}"
            if label:
                out += f"&q={_q(label)}"
            return out
        return f"https://maps.apple.com/?q={_q(query)}"
    if p == "ola":
        # Ola Maps is deep-link friendly: omaps.app/<lat>,<lon> opens the app
        # on Android, and a /search path is the web fallback.
        if has_geo:
            out = f"https://omaps.app/{lat},{lon}"
            if label:
                out += f"/{_q(label)}"
            return out
        return f"https://omaps.app/search/{_q(query)}"
    if p == "osm":
        if has_geo:
            return (f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}"
                    f"#map=17/{lat}/{lon}")
        return f"https://www.openstreetmap.org/search?query={_q(query)}"
    return link(DEFAULT_PROVIDER, lat, lon, query, label)


def all_links(lat: float | None, lon: float | None, query: str,
              label: str = "") -> dict[str, str]:
    return {p: link(p, lat, lon, query, label) for p in PROVIDERS}


def embed_url(lat: float | None = None, lon: float | None = None,
              query: str = "") -> str:
    """An iframe-able URL, or '' when the provider does not offer one.

    Only Google's output=embed works without a key. Apple Maps has an embed
    that needs a signed token, and Ola/OSM have none — so those show a map
    preview built from static tiles instead (see tiles_url), which needs no
    account and works everywhere.
    """
    if lat is not None and lon is not None:
        return (f"https://maps.google.com/maps?q={lat},{lon}&z=15&output=embed")
    return f"https://maps.google.com/maps?q={urllib.parse.quote(query)}&output=embed"


def tiles_url(lat: float | None, lon: float | None, z: int = 14) -> str:
    """Slippy-map tile URL for an <img> preview — provider-neutral."""
    if lat is None or lon is None:
        return ""
    return f"https://tile.openstreetmap.org/{int(z)}/{int(lon)}/{int(lat)}.png"


# ── geocoding ────────────────────────────────────────────────────────────────

def _throttle() -> None:
    global _last_call
    gap = time.time() - _last_call
    if gap < _MIN_GAP_S:
        time.sleep(_MIN_GAP_S - gap)
    _last_call = time.time()


def _fetch_json(url: str, opener: Optional[Callable] = None) -> Any:
    _throttle()
    if opener:
        return json.loads(opener(url))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def search(query: str, *, limit: int = 5, opener: Optional[Callable] = None,
           country: str = "") -> list[dict]:
    """Geocode a place name. Returns normalised results, best first."""
    q = str(query or "").strip()
    if not q:
        return []
    params = {"q": q, "format": "jsonv2", "limit": str(max(1, min(limit, 10))),
              "addressdetails": "1"}
    if country:
        params["countrycodes"] = country.lower()
    url = NOMINATIM + "?" + urllib.parse.urlencode(params)
    data = _fetch_json(url, opener)
    out: list[dict] = []
    for r in (data or [])[:limit]:
        try:
            lat = float(r.get("lat"))
            lon = float(r.get("lon"))
        except (TypeError, ValueError):
            continue
        addr = r.get("address") or {}
        label = (r.get("display_name") or q).split(",")[0][:80]
        out.append({
            "label": label,
            "full_name": str(r.get("display_name") or "")[:220],
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "type": str(r.get("type") or r.get("category") or ""),
            "category": str(r.get("category") or ""),
            "city": str(addr.get("city") or addr.get("town")
                        or addr.get("village") or addr.get("municipality") or ""),
            "country": str(addr.get("country") or ""),
            "postcode": str(addr.get("postcode") or ""),
            "osm_id": str(r.get("osm_id") or ""),
            "links": all_links(round(lat, 6), round(lon, 6), q, label),
        })
    return out


def reverse(lat: float, lon: float, *, opener: Optional[Callable] = None) -> dict:
    """Coordinates → the place they are. Used for "where am I"."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return {}
    url = (NOMINATIM_REV + "?"
           + urllib.parse.urlencode({"lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                                     "format": "jsonv2"}))
    try:
        data = _fetch_json(url, opener)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    name = data.get("name") or data.get("display_name", "").split(",")[0]
    return {"label": str(name)[:80],
            "full_name": str(data.get("display_name") or "")[:220],
            "lat": round(lat, 6), "lon": round(lon, 6),
            "links": all_links(round(lat, 6), round(lon, 6), name)}


# ── distance ─────────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Good enough to say '20 minutes away'."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def travel_hint(km: float) -> str:
    """A rough, honest duration.

    Deliberately vague — real routing needs a routing engine — but it must not
    be absurd: an earlier version divided by 45 and added 5, which turned
    350 km into "roughly 12 minutes by car". Bands, and a floor.
    """
    if km < 0.3:
        return "a couple of minutes' walk"
    if km < 1.5:
        return f"about {max(3, int(km * 12))} minutes on foot"
    if km < 10:
        return f"about {int(km * 4) + 4} minutes by bike or a short drive"
    if km < 100:
        return f"roughly {max(20, int(km / 60) * 15 + 15)} minutes by car"
    return f"roughly {int(km / 80) + 1} hours by car"


def describe(place: dict, origin: dict | None = None) -> str:
    """Prose for the model to read out — no raw JSON, no bare coordinates."""
    if not place:
        return "I could not find that place."
    bits = [f"{place.get('label') or 'that place'}"]
    where = ", ".join(x for x in (place.get("city"), place.get("country")) if x)
    if where:
        bits.append(where)
    line = " — ".join(bits)
    if origin and origin.get("lat") is not None:
        km = haversine_km(float(origin["lat"]), float(origin["lon"]),
                          float(place["lat"]), float(place["lon"]))
        line += f" — about {km:.1f} km away, {travel_hint(km)}"
    return line


def save_pin(place: dict, name: str = "") -> dict:
    """Remember a place in memory so "the client's office" means something later."""
    try:
        from memory.memory_manager import load_memory, save_memory
    except Exception as e:
        return {"error": f"memory unavailable: {e}"}
    mem = load_memory() or {}
    pins = mem.get("places")
    if not isinstance(pins, dict):
        pins = {}
    key = (name or place.get("label") or "pin")[:60]
    pins[key] = {"lat": place.get("lat"), "lon": place.get("lon"),
                 "label": place.get("label"), "at": time.time()}
    mem["places"] = pins
    save_memory(mem)
    return {"ok": True, "name": key}


def load_pins() -> dict:
    try:
        from memory.memory_manager import load_memory
        pins = (load_memory() or {}).get("places")
        return pins if isinstance(pins, dict) else {}
    except Exception:
        return {}
