# actions/play_music.py
"""Alexa-style music for the dashboard browser.

Sources — official APIs / sanctioned embeds only, no scraping:
  YouTube     Data API v3 (search + mostPopular music chart), IFrame Player
              API playback. Key: env YOUTUBE_API_KEY, else api_keys.json
              "youtube_api_key". No key → YouTube search skipped, direct
              links still play.
  Deezer      Official public search/chart API (no key), official embed
              player for playback. Also the YouTube-less fallback.
  Radio       SomaFM official channels API; direct MP3 streams in <audio>.
  SoundCloud  Official embed widget for pasted links only (search needs the
              paid Artist Pro API plan).

Playback state (queue / index / volume) lives here at module level. Voice
commands broadcast {"type": "music", ...} over the dashboard's main /ws via
HeadlessUI._bcast; on-screen buttons and track-end advance come back through
POST /api/music/control and /api/music/advance in dashboard/server.py, which
call client_op() / client_advance() and broadcast the result. Only the tab
that owns the TTS pipe (/ws/audio-out) executes playback, so two tabs never
double up.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:  # server image should have it; degrade to link-only
    requests = None

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_TIMEOUT = 8
_YT_API = "https://www.googleapis.com/youtube/v3"
_DZ_API = "https://api.deezer.com"

# SomaFM official stream endpoints — used if api.somafm.com is unreachable.
_FALLBACK_STATIONS = [
    {"id": "groovesalad",  "title": "Groove Salad",
     "stream": "https://ice1.somafm.com/groovesalad-128-mp3",
     "tags": "chill ambient downtempo relaxing calm lofi"},
    {"id": "beatblender",  "title": "Beat Blender",
     "stream": "https://ice1.somafm.com/beatblender-128-mp3",
     "tags": "deep house electronic chillout dance"},
    {"id": "dronezone",    "title": "Drone Zone",
     "stream": "https://ice1.somafm.com/dronezone-128-mp3",
     "tags": "ambient space drone atmospheric"},
    {"id": "secretagent",  "title": "Secret Agent",
     "stream": "https://ice1.somafm.com/secretagent-128-mp3",
     "tags": "spy lounge retro noir"},
    {"id": "lush",         "title": "Lush",
     "stream": "https://ice1.somafm.com/lush-128-mp3",
     "tags": "sensual vocals female chillout"},
    {"id": "indiepop",     "title": "Indie Pop Rocks!",
     "stream": "https://ice1.somafm.com/indiepop-128-mp3",
     "tags": "indie pop rock alternative"},
    {"id": "metal",        "title": "Metal Detector",
     "stream": "https://ice1.somafm.com/metal-128-mp3",
     "tags": "metal heavy rock"},
    {"id": "defcon",       "title": "DEF CON Radio",
     "stream": "https://ice1.somafm.com/defcon-128-mp3",
     "tags": "hacker techno coding cyber"},
    {"id": "deepspaceone", "title": "Deep Space One",
     "stream": "https://ice1.somafm.com/deepspaceone-128-mp3",
     "tags": "space ambient sci-fi stars"},
    {"id": "seventies",    "title": "Left Coast 70s",
     "stream": "https://ice1.somafm.com/seventies-128-mp3",
     "tags": "70s classic rock retro oldies"},
    {"id": "fluid",        "title": "Fluid",
     "stream": "https://ice1.somafm.com/fluid-128-mp3",
     "tags": "instrumental hip-hop jazz beats"},
    {"id": "bootliquor",   "title": "Boot Liquor",
     "stream": "https://ice1.somafm.com/bootliquor-128-mp3",
     "tags": "americana country roots folk"},
]

_lock = threading.Lock()
_STATE: dict = {"queue": [], "index": 0, "volume": 80}
_station_cache: dict = {"at": 0.0, "list": None}


# ── keys / http ──────────────────────────────────────────────────────────────

def _yt_key() -> str:
    k = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if k:
        return k
    try:
        from core.data_paths import config_dir
        path = Path(config_dir()) / "api_keys.json"
    except Exception:
        path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
    try:
        if path.is_file():
            k = (json.loads(path.read_text(encoding="utf-8"))
                 .get("youtube_api_key") or "").strip()
            if k:
                return k
    except Exception:
        pass
    return ""


def _get_json(url: str, params: dict | None = None, timeout: int = _TIMEOUT):
    if requests is None:
        raise RuntimeError("requests not installed")
    r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── source searches (official APIs) ─────────────────────────────────────────

def _search_youtube(query: str) -> list[dict]:
    key = _yt_key()
    if not key:
        raise RuntimeError("no YouTube API key")
    data = _get_json(f"{_YT_API}/search", {
        "part": "snippet", "type": "video", "videoCategoryId": "10",
        "videoEmbeddable": "true", "maxResults": "8",
        "q": query, "key": key,
    }, timeout=10)
    out = []
    for it in data.get("items", []):
        vid = (it.get("id") or {}).get("videoId")
        if not vid:
            continue
        sn = it.get("snippet") or {}
        out.append({
            "source": "youtube", "id": vid,
            "title": sn.get("title") or query,
            "artist": sn.get("channelTitle") or "",
            "cover": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    if not out:
        raise RuntimeError("no results")
    return out


def _trending_youtube() -> list[dict]:
    """Generic 'play music' → official mostPopular music chart (1 quota unit)."""
    key = _yt_key()
    if not key:
        raise RuntimeError("no YouTube API key")
    data = _get_json(f"{_YT_API}/videos", {
        "part": "snippet", "chart": "mostPopular",
        "videoCategoryId": "10", "maxResults": "10", "key": key,
    }, timeout=10)
    out = []
    for it in data.get("items", []):
        vid = it.get("id")
        if not vid:
            continue
        sn = it.get("snippet") or {}
        out.append({
            "source": "youtube", "id": vid,
            "title": sn.get("title") or "Trending music",
            "artist": sn.get("channelTitle") or "",
            "cover": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    if not out:
        raise RuntimeError("empty chart")
    return out


def _search_deezer(query: str) -> list[dict]:
    data = _get_json(f"{_DZ_API}/search", {"q": query, "limit": 10})
    out = []
    for it in data.get("data", []):
        artist = (it.get("artist") or {}).get("name") or ""
        out.append({
            "source": "deezer", "id": str(it.get("id")),
            "title": it.get("title") or query,
            "artist": artist,
            "cover": (it.get("album") or {}).get("cover_medium") or "",
        })
    if not out:
        raise RuntimeError("no results")
    return out


def _charts_deezer() -> list[dict]:
    data = _get_json(f"{_DZ_API}/chart/0/tracks", {"limit": 10})
    out = []
    for it in data.get("data", []):
        artist = (it.get("artist") or {}).get("name") or ""
        out.append({
            "source": "deezer", "id": str(it.get("id")),
            "title": it.get("title") or "Top track",
            "artist": artist,
            "cover": (it.get("album") or {}).get("cover_medium") or "",
        })
    if not out:
        raise RuntimeError("empty chart")
    return out


# ── radio (SomaFM official channels API) ────────────────────────────────────

def _stations() -> list[dict]:
    now = __import__("time").monotonic()
    if _station_cache["list"] and now - _station_cache["at"] < 3600:
        return _station_cache["list"]
    stations: list[dict] = []
    try:
        data = _get_json("https://api.somafm.com/channels.json")
        for ch in data.get("channels", []):
            stream = ""
            playlists = ch.get("playlists") or []
            mp3s = [p for p in playlists if p.get("format") == "mp3"]
            if mp3s:
                stream = mp3s[0].get("url") or ""
            if not stream:
                continue
            img = ch.get("image") or ch.get("cover") or ""
            if img.startswith("/"):
                img = "https://somafm.com" + img
            stations.append({
                "id": ch.get("id") or "",
                "title": ch.get("title") or ch.get("id") or "",
                "stream": stream,
                "tags": (ch.get("genre") or "") + " " + (ch.get("description") or ""),
                "image": img,
            })
    except Exception as e:
        print(f"[Music] SomaFM API unavailable ({e}) — static station list")
    if not stations:
        stations = [dict(s) for s in _FALLBACK_STATIONS]
    _station_cache["list"] = stations
    _station_cache["at"] = now
    return stations


def _match_stations(query: str) -> list[dict]:
    """Match SomaFM stations from a spoken query.

    A radio-ish word ('radio', 'station', …) opens tag matching (so 'jazz
    radio' can land on Fluid). Without one, only distinctive station *names*
    match ('play groove salad') — so plain genre searches ('play metal')
    still go to the music APIs.
    """
    q = (query or "").lower()
    if not q:
        return []
    words = set(re.findall(r"[a-z0-9]+", q))
    _RADIO_WORDS = {"radio", "station", "stations", "somafm", "stream",
                    "streams", "fm"}
    radioish = bool(words & _RADIO_WORDS)
    core = words - _RADIO_WORDS   # 'jazz radio' must not match titles containing 'radio'
    hits = []
    for st in _stations():
        title_words = set(re.findall(r"[a-z0-9]+", st["title"].lower()))
        if radioish:
            hay = set(re.findall(
                r"[a-z0-9]+",
                f"{st['id']} {st['title']} {st.get('tags','')}".lower()))
            if core & hay:
                hits.append(st)
            continue
        name_hit = bool(title_words) and title_words <= words
        distinctive = (len(st["id"]) >= 7 and st["id"] in words)
        if name_hit or distinctive:
            hits.append(st)
    return hits


def _radio_item(st: dict) -> dict:
    return {
        "source": "radio", "id": st["id"],
        "title": st["title"], "artist": "SomaFM Radio",
        "stream": st["stream"], "cover": st.get("image") or "",
    }


def _rotation() -> list[dict]:
    """All stations, default chill station (Groove Salad) first."""
    return sorted(_stations(), key=lambda s: s["id"] != "groovesalad")


# ── direct links (no search — official oEmbed / parse) ──────────────────────

def _yt_id(url: str) -> str | None:
    m = re.search(
        r"(?:v=|/v/|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})",
        url,
    )
    return m.group(1) if m else None


def _oembed_title(url: str, provider: str) -> tuple[str, str]:
    """(title, author) via the provider's official oEmbed endpoint."""
    try:
        if provider == "youtube":
            d = _get_json("https://www.youtube.com/oembed",
                          {"url": url, "format": "json"})
        elif provider == "soundcloud":
            d = _get_json("https://soundcloud.com/oembed",
                          {"format": "json", "url": url})
        else:
            d = _get_json(f"{_DZ_API}/oembed", {"url": url})
        return (d.get("title") or "", d.get("author_name") or "")
    except Exception:
        return ("", "")


def _item_from_url(url: str) -> dict | None:
    u = urlparse(url)
    host = (u.netloc or "").lower().removeprefix("www.")
    # YouTube
    if "youtu" in host:
        if "playlist" in u.path or parse_qs(u.query).get("list"):
            lid = (parse_qs(u.query).get("list") or [""])[0]
            if lid:
                title, author = _oembed_title(url, "youtube")
                return {"source": "youtube", "list": lid, "id": lid,
                        "title": title or "YouTube playlist", "artist": author}
        vid = _yt_id(url)
        if vid:
            title, author = _oembed_title(url, "youtube")
            return {"source": "youtube", "id": vid,
                    "title": title or "YouTube video", "artist": author}
    # Deezer
    if "deezer" in host:
        m = re.search(r"/track/(\d+)", u.path)
        if m:
            title, author = _oembed_title(url, "deezer")
            if not title:
                try:
                    d = _get_json(f"{_DZ_API}/track/{m.group(1)}")
                    title = d.get("title") or "Deezer track"
                    author = (d.get("artist") or {}).get("name") or ""
                except Exception:
                    title, author = "Deezer track", ""
            return {"source": "deezer", "id": m.group(1),
                    "title": title, "artist": author}
    # SoundCloud
    if "soundcloud" in host:
        title, author = _oembed_title(url, "soundcloud")
        if not title:
            slug = u.path.rstrip("/").split("/") or ["", ""]
            title = slug[-1].replace("-", " ") or "SoundCloud track"
        return {"source": "soundcloud", "url": url, "id": url,
                "title": title, "artist": author}
    return None


# ── play construction ───────────────────────────────────────────────────────

_GENERIC = {"", "music", "some music", "a song", "song", "songs", "something",
            "anything", "whatever", "play music", "some songs"}


def build_play(query: str = "", source: str = "auto", url: str = "") -> dict:
    """Return a {'type':'music','op':'play',…} broadcast message."""
    query = (query or "").strip()
    source = (source or "auto").strip().lower()
    url = (url or "").strip()
    errors: list[str] = []

    # 1. Direct link always wins.
    if url:
        item = _item_from_url(url)
        if not item:
            raise RuntimeError(
                "that link is not a supported source — YouTube, Deezer and "
                "SoundCloud links only")
        with _lock:
            _STATE.update(queue=[item], index=0)
        return {"type": "music", "op": "play", "queue": [item], "index": 0,
                "volume": _STATE["volume"]}

    # 2. Radio: a known station name always wins; a radio-ish query with no
    #    named station plays the full SomaFM rotation starting at the default
    #    chill station (Groove Salad), so 'next' cycles stations. Genres that
    #    aren't stations ('jazz', 'metal') fall through to the music APIs —
    #    unless tagged on a station ('jazz radio' → Fluid).
    ql = query.lower()
    want_radio = source == "radio"
    _RADIO_WORDS = {"radio", "station", "stations", "somafm", "stream",
                    "streams", "fm"}
    radioish = bool(set(re.findall(r"[a-z0-9]+", ql)) & _RADIO_WORDS)
    matched = _match_stations(query) if not (source in ("youtube", "deezer")) else []
    if matched or want_radio or (radioish and source not in ("youtube", "deezer")):
        pool = matched or _rotation()
        queue = [_radio_item(s) for s in pool]
        with _lock:
            _STATE.update(queue=queue, index=0)
        return {"type": "music", "op": "play", "queue": queue, "index": 0,
                "volume": _STATE["volume"]}

    # 3. Generic request → trending charts (no search quota on YouTube).
    generic = ql in _GENERIC
    if source != "deezer":
        try:
            queue = (_trending_youtube() if generic else _search_youtube(query))
            with _lock:
                _STATE.update(queue=queue, index=0)
            return {"type": "music", "op": "play", "queue": queue, "index": 0,
                    "volume": _STATE["volume"]}
        except Exception as e:
            errors.append(f"youtube: {e}")

    # 4. Deezer fallback / forced source.
    if source != "youtube":
        try:
            queue = _charts_deezer() if generic else _search_deezer(query)
            with _lock:
                _STATE.update(queue=queue, index=0)
            return {"type": "music", "op": "play", "queue": queue, "index": 0,
                    "volume": _STATE["volume"]}
        except Exception as e:
            errors.append(f"deezer: {e}")

    raise RuntimeError("; ".join(errors) or "no music source available")


# ── client-driven ops (buttons, track end) ──────────────────────────────────

def _play_msg_locked() -> dict:
    return {"type": "music", "op": "play",
            "queue": list(_STATE["queue"]), "index": _STATE["index"],
            "volume": _STATE["volume"]}


def client_op(action: str, value=None) -> dict | None:
    """Called from dashboard endpoints (button clicks). Broadcasts state."""
    action = (action or "").strip().lower()
    with _lock:
        if action == "volume":
            try:
                v = max(0, min(100, int(value)))
            except (TypeError, ValueError):
                return None
            _STATE["volume"] = v
            return {"type": "music", "op": "volume", "value": v}
        if action == "pause" or action == "resume":
            if not _STATE["queue"]:
                return None
            return {"type": "music", "op": action}
        if action == "stop":
            _STATE.update(queue=[], index=0)
            return {"type": "music", "op": "stop"}
        if action == "next":
            return _advance_locked()
        if action == "prev":
            if not _STATE["queue"]:
                return None
            if _STATE["index"] > 0:
                _STATE["index"] -= 1
            return _play_msg_locked()
    return None


def _advance_locked() -> dict:
    if not _STATE["queue"]:
        return {"type": "music", "op": "end"}
    if _STATE["index"] + 1 < len(_STATE["queue"]):
        _STATE["index"] += 1
        return _play_msg_locked()
    _STATE.update(queue=[], index=0)
    return {"type": "music", "op": "end"}


def client_advance() -> dict:
    """Track ended in the browser → move the queue forward."""
    with _lock:
        return _advance_locked()


# ── music library (dashboard: browse / search / play manually) ──────────────

def search(query: str, source: str = "auto") -> dict:
    """Aggregated search for the music panel: YouTube + Deezer, no scraping."""
    q = (query or "").strip()
    if not q:
        return {"results": [], "errors": []}
    source = (source or "auto").strip().lower()
    results: list[dict] = []
    errors: list[str] = []
    if source in ("auto", "youtube"):
        try:
            results += _search_youtube(q)
        except Exception as e:
            errors.append(f"youtube: {e}")
    if source in ("auto", "deezer"):
        try:
            results += _search_deezer(q)
        except Exception as e:
            errors.append(f"deezer: {e}")
    return {"results": results, "errors": errors}


def browse(kind: str = "trending") -> dict:
    """Music panel sections: trending (YT chart), charts (Deezer), radio."""
    kind = (kind or "").strip().lower()
    results: list[dict] = []
    errors: list[str] = []
    if kind == "radio":
        results = [_radio_item(s) for s in _stations()]
    elif kind == "charts":
        try:
            results = _charts_deezer()
        except Exception as e:
            errors.append(f"deezer: {e}")
    else:
        try:
            results = _trending_youtube()
        except Exception as e:
            errors.append(f"youtube: {e}")
            try:
                results = _charts_deezer()
            except Exception as e2:
                errors.append(f"deezer: {e2}")
    return {"results": results, "errors": errors}


def _valid_item(it) -> dict | None:
    """Sanitize a client-supplied queue item before it hits state."""
    if not isinstance(it, dict):
        return None
    src = str(it.get("source") or "")
    if src not in ("youtube", "deezer", "soundcloud", "radio"):
        return None
    if src == "radio":
        stream = str(it.get("stream") or "")
        if not stream.startswith("https://"):
            return None
        return {
            "source": "radio", "id": str(it.get("id") or "")[:64],
            "title": str(it.get("title") or "Radio")[:200],
            "artist": "SomaFM Radio", "stream": stream,
            "cover": str(it.get("cover") or "")[:300],
        }
    _id = str(it.get("id") or "").strip()
    if not _id or len(_id) > 64:
        return None
    item = {
        "source": src, "id": _id,
        "title": str(it.get("title") or "")[:300],
        "artist": str(it.get("artist") or "")[:200],
        "cover": str(it.get("cover") or "")[:300],
    }
    if src == "soundcloud" and it.get("url"):
        item["url"] = str(it["url"])[:500]
    if it.get("list"):
        item["list"] = str(it["list"])[:64]
    return item


def set_queue(items, index: int = 0) -> dict:
    """Play an explicit queue from the music panel (manual click)."""
    if not isinstance(items, list):
        raise RuntimeError("items must be a list")
    clean: list[dict] = []
    for it in items[:50]:
        c = _valid_item(it)
        if c:
            clean.append(c)
    if not clean:
        raise RuntimeError("no valid tracks")
    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0
    index = min(max(0, index), len(clean) - 1)
    with _lock:
        _STATE.update(queue=clean, index=index)
        msg = _play_msg_locked()
    cur = clean[index]
    print(f"[Music] \u25b6 manual {cur['source']}: {cur['title'][:70]}",
          flush=True)
    return msg


def client_jump(index) -> dict | None:
    """Jump to a queue position from the bar / panel queue list."""
    with _lock:
        if not _STATE["queue"]:
            return None
        try:
            i = int(index)
        except (TypeError, ValueError):
            return None
        if not 0 <= i < len(_STATE["queue"]):
            return None
        _STATE["index"] = i
        msg = _play_msg_locked()
    print(f"[Music] \u23ed jump \u2192 {msg['queue'][i].get('title', '')[:70]}",
          flush=True)
    return msg


# ── broadcast helper ────────────────────────────────────────────────────────

def _broadcast(player, msg: dict) -> bool:
    fn = getattr(player, "_bcast", None) or getattr(player, "broadcast", None)
    if callable(fn):
        try:
            fn(msg)
            return True
        except Exception as e:
            print(f"[Music] broadcast failed: {e}")
    return False


def _open_desktop_fallback(queue: list[dict]) -> None:
    """Desktop Qt mode has no dashboard broadcast — open a playable URL."""
    if not queue:
        return
    it = queue[0]
    try:
        import webbrowser
        if it["source"] == "youtube":
            webbrowser.open(f"https://www.youtube.com/watch?v={it['id']}")
        elif it["source"] == "deezer":
            webbrowser.open(f"https://www.deezer.com/track/{it['id']}")
        elif it["source"] == "soundcloud":
            webbrowser.open(it.get("url") or "https://soundcloud.com")
        else:
            webbrowser.open(it.get("stream") or "https://somafm.com")
    except Exception as e:
        print(f"[Music] desktop fallback failed: {e}")


# ── tool entry ──────────────────────────────────────────────────────────────

_OPS = ("pause", "resume", "stop", "next", "previous", "prev", "volume")


def play_music(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = (params.get("action") or "play").lower().strip()
    if action == "previous":
        action = "prev"
    query = (params.get("query") or "").strip()
    url = (params.get("url") or "").strip()
    source = (params.get("source") or "auto").strip()

    if action not in ("play", *_OPS):
        return (f"Unknown music action '{action}'. "
                "Use: play, pause, resume, stop, next, prev, volume.")

    try:
        if action == "play":
            msg = build_play(query, source, url)
            _broadcast(player, msg)
            if not getattr(player, "_bcast", None):
                _open_desktop_fallback(msg.get("queue") or [])
            it = msg["queue"][msg["index"]]
            where = {"youtube": "YouTube", "deezer": "Deezer",
                     "soundcloud": "SoundCloud", "radio": "radio"}.get(
                         it["source"], it["source"])
            title = it.get("title") or "music"
            artist = it.get("artist") or ""
            print(f"[Music] ▶ {it['source']}: {title} ({artist})")
            if it["source"] == "radio":
                return f"Playing {title} — {artist}."
            return f"Playing “{title}”" + (f" by {artist}" if artist else "") + f" on {where}."

        if action == "volume":
            raw = params.get("volume")
            msg = client_op("volume", raw)
            if msg is None:
                return "I couldn't read that volume level, sir."
            _broadcast(player, msg)
            return f"Volume set to {msg['value']} percent."

        if action == "prev":
            msg = client_op("prev")
        else:
            msg = client_op("next" if action == "next" else action)
        if msg is None:
            return "Nothing is playing right now, sir."
        _broadcast(player, msg)
        if action == "next":
            if msg.get("op") == "end":
                return "That was the end of the queue, sir."
            it = msg["queue"][msg["index"]]
            return f"Skipped to “{it.get('title', '')}”."
        if action == "stop":
            return "Stopped."
        if action == "pause":
            return "Paused."
        if action == "resume":
            return "Resumed."
        return "Done."

    except Exception as e:
        print(f"[Music] ❌ {action} failed: {e}")
        return f"Music failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "play_music",
    "description": (
        "Plays music, songs and radio in the user's browser (Alexa-style) — "
        "use for ANY request to play music, a song, an artist, a genre, a "
        "playlist, a music link, or internet radio. Sources: YouTube, Deezer, "
        "SomaFM radio, plus SoundCloud links. Actions: play (search by query "
        "or pass url for a direct link; empty query = trending hits), pause, "
        "resume, stop, next, prev, volume. Music keeps playing while you talk "
        "— the microphone stays open, so the user can say 'stop the music' "
        "or keep conversing. For watching/analyzing videos (summaries, info) "
        "use youtube_video instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | pause | resume | stop | next | prev | volume (default: play)"
            },
            "query": {
                "type": "STRING",
                "description": "What to play: song, artist, genre, station, or 'music' for trending hits (play only)"
            },
            "source": {
                "type": "STRING",
                "description": "auto (default) | youtube | deezer | radio — force a source; radio plays SomaFM stations"
            },
            "url": {
                "type": "STRING",
                "description": "Direct YouTube / Deezer / SoundCloud link to play instead of searching (play only)"
            },
            "volume": {
                "type": "INTEGER",
                "description": "0–100 (volume action only)"
            }
        },
        "required": []
    },
    "handler": play_music,
}
