"""core/godseye.py — run the real God's Eye View, and speak to it.

Phase 4f, part two. `core/globe.py` is our own small globe; this is the actual
project (github.com/bilawalsidhu/gods-eye-view, MIT), vendored at a pinned commit
by scripts/build_godseye.py.

Three jobs:

1. **Supervise it.** The Space runs their Node server (`vite preview`), which
   serves both the static app and their /api/* routes. We start it, wait for it
   to answer, and keep it alive; the dashboard proxies /godseye/* and
   /api/gev/* through to it. If it is down, the dashboard degrades to serving
   the vendored static build on its own — the globe still opens, it just loses
   the live API layers.

2. **Speak its language.** The app exposes `window.__gevVoiceCommands`, whose
   `actionExecutor(name, args)` is what their OpenAI-Realtime voice calls —
   and which throws "Voice action cancelled" for everything when no realtime
   session is live. Directly underneath it, `runner(name, args)` is the same
   action vocabulary without that guard. So JARVIS calls `runner`: their voice
   is replaced rather than competed with, no OpenAI key, no second brain, and
   every feature of the project becomes a JARVIS skill. The action and layer
   vocabularies are read from godseye/MANIFEST.json at runtime, so the build
   script is the single source of truth and the two cannot drift.

3. **Stay honest.** `describe()` reports what is actually on screen instead of
   claiming a layer is live when the API never answered.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core import maps as _maps

# ── where things live ────────────────────────────────────────────────────────
#: In the Space image the vendored tree is installed at /opt/godseye, not next
#: to the app, so honour GODSEYE_HOME and fall back to the repo layout for
#: local runs. Getting this wrong 404s every asset while the API still answers.
GODSEYE_ROOT = (Path(os.environ["GODSEYE_HOME"]) if os.environ.get("GODSEYE_HOME")
                else Path(__file__).resolve().parent.parent / "godseye")
SERVER_DIR = GODSEYE_ROOT / "_server"
#: The built browser app. It has to sit at _server/dist because that is where
#: `vite preview` serves from — anywhere else and their server answers /api/*
#: perfectly while 404ing every asset.
GODSEYE_DIR = SERVER_DIR / "dist"
MANIFEST = GODSEYE_ROOT / "MANIFEST.json"

#: Their vite preview listens here; the dashboard proxies to it. Loopback only.
GODSEYE_PORT = int(os.environ.get("JARVIS_GODSEYE_PORT") or "7861")
GODSEYE_HOST = "127.0.0.1"
BASE = f"http://{GODSEYE_HOST}:{GODSEYE_PORT}"
#: The path prefix the app is served under. index.html references /godseye/…
#: for its own assets, so this is load-bearing, not cosmetic.
MOUNT = "/godseye"
#: Their /api/* calls are rewritten to /api/gev/* at build time so they cannot
#: collide with this project's dashboard API on the same origin.
API_PREFIX = "/api/gev"

STYLES = ("normal", "nvg", "flir", "crt", "anime", "god")

_manifest_cache: Optional[dict] = None
_proc: Optional[subprocess.Popen] = None
_lock = threading.Lock()
_state = {"started": 0.0, "last_ok": 0.0, "errors": 0, "note": "not started"}


# ── vocabulary ───────────────────────────────────────────────────────────────

def manifest() -> dict:
    """The vendored app's own inventory. Single source of truth: the build
    script writes it, we only read it."""
    global _manifest_cache
    if _manifest_cache is None:
        try:
            _manifest_cache = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            _manifest_cache = {"actions": [], "layers": [], "commit": "?"}
    return _manifest_cache


def actions() -> list[str]:
    return list(manifest().get("actions") or [])


def layers() -> list[str]:
    return list(manifest().get("layers") or [])


def is_action(name: str) -> bool:
    return str(name) in actions()


def is_layer(name: str) -> bool:
    return str(name) in layers()


# ── supervision ──────────────────────────────────────────────────────────────

def available() -> bool:
    return GODSEYE_DIR.is_dir() and (GODSEYE_DIR / "index.html").exists()


def providers() -> dict:
    """The map/space keys, resolved (env wins over the file).

    Kept here rather than in the server because the Node process needs the same
    values in its environment: their server brokers place search and the
    map_key route, and without them those half-work.
    """
    try:
        from memory.config_manager import get_provider_settings
        return get_provider_settings()["values"]
    except Exception:
        return {"google_maps_key": "", "cesium_ion_token": ""}


def provider_state() -> dict:
    """Booleans and masks for the dashboard — never the key itself."""
    try:
        from memory.config_manager import get_provider_settings, mask_secret
        s = get_provider_settings()
        v = s["values"]
        return {"has_google": bool((v.get("google_maps_key") or "").strip()),
                "has_ion": bool((v.get("cesium_ion_token") or "").strip()),
                "google_masked": mask_secret(v.get("google_maps_key", "")),
                "ion_masked": mask_secret(v.get("cesium_ion_token", "")),
                "env_locked": s["env_locked"]}
    except Exception as e:
        return {"error": str(e)[:120]}


def save_providers(**fields) -> dict:
    """Persist provider keys and restart their server so it picks them up."""
    from memory.config_manager import save_provider_settings
    save_provider_settings(**fields)
    restart()
    return provider_state()


def restart() -> dict:
    """Bounce the Node process so new keys reach their server-side routes."""
    global _proc
    try:
        stop()
    except Exception:
        pass
    return start(wait=20.0)


def api_alive(timeout: float = 1.5) -> bool:
    """Is their Node server answering? Cheap: one TCP connect, not a request."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((GODSEYE_HOST, GODSEYE_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def node_installed() -> bool:
    return shutil_which("node") is not None


def shutil_which(cmd: str) -> Optional[str]:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = Path(d) / cmd
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


def start(*, wait: float = 25.0) -> dict:
    """Start `vite preview` if it is not already up. Idempotent."""
    global _proc
    with _lock:
        if api_alive():
            _state.update(note="already running")
            return state()
        if not available():
            _state.update(note="not vendored")
            return state()
        if not node_installed():
            _state.update(note="node not installed")
            return state()
        if not SERVER_DIR.is_dir():
            _state.update(note="server sources missing")
            return state()
        env = dict(os.environ)
        # They bind to localhost by design and broker credentials server-side.
        # Loopback is the only place this is reachable from.
        env.update({"HOST": GODSEYE_HOST, "PORT": str(GODSEYE_PORT),
                    "NODE_ENV": "production", "CI": "1"})
        # their server-side routes (place search, /api/map_key) read these
        prov = providers()
        if prov.get("google_maps_key"):
            env["GOOGLE_MAPS_API_KEY"] = prov["google_maps_key"]
        if prov.get("cesium_ion_token"):
            env["CESIUM_ION_TOKEN"] = prov["cesium_ion_token"]
        log = Path(os.environ.get("JARVIS_LOG_DIR") or "/tmp") / "godseye.log"
        try:
            fh = log.open("a")
        except Exception:
            fh = subprocess.DEVNULL
        try:
            _proc = subprocess.Popen(
                ["npx", "vite", "preview", "--host", GODSEYE_HOST,
                 "--port", str(GODSEYE_PORT)],
                cwd=str(SERVER_DIR), env=env, stdout=fh, stderr=fh,
                stdin=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            _state.update(note=f"spawn failed: {e}"[:120])
            return state()
        _state.update(started=time.time(), note="starting")
    end = time.time() + wait
    while time.time() < end:
        if api_alive():
            _state.update(last_ok=time.time(), note="running")
            return state()
        time.sleep(0.5)
    _state.update(note="did not answer in time")
    return state()


def stop() -> None:
    global _proc
    with _lock:
        if _proc and _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except Exception:
                _proc.kill()
        _proc = None
        _state.update(note="stopped")


def state() -> dict:
    alive = api_alive() if available() else False
    return {"vendored": available(), "node": node_installed(), "api_alive": alive,
            **provider_state(),
            "port": GODSEYE_PORT, "mount": MOUNT, "note": _state["note"],
            "actions": len(actions()), "layers": len(layers()),
            "commit": manifest().get("commit", "?")[:9]}


# ── the bridge ───────────────────────────────────────────────────────────────
# Each command is (action, args). The client calls actionExecutor on the app's
# own global, so these names must exist upstream — core/godseye.py refuses
# anything not in the vendored MANIFEST rather than silently doing nothing.

def cmd(action: str, **args: Any) -> dict:
    if not is_action(action):
        raise ValueError(
            f"God's Eye has no action '{action}'. Known: {', '.join(actions()[:8])}…")
    return {"action": action, "args": {k: v for k, v in args.items() if v is not None}}


def build_commands(*, place: str = "", lat: Optional[float] = None,
                   lon: Optional[float] = None, show: Optional[list[str]] = None,
                   hide: Optional[list[str]] = None, style: str = "",
                   zoom: str = "", height: Optional[int] = None,
                   iss: bool = False, nearest_aircraft: bool = False,
                   clear: bool = False) -> list[dict]:
    """Turn what the user asked for into a batch of app actions."""
    out: list[dict] = []
    if clear:
        out.append(cmd("clear_annotations"))
    if style:
        st = str(style).lower()
        if st not in STYLES:
            raise ValueError(f"style must be one of {', '.join(STYLES)}")
        out.append(cmd("set_visual_style", style=st))
    if zoom in ("in", "out"):
        out.append(cmd("adjust_camera_zoom", direction=zoom, amount="medium"))
    elif zoom in ("globe", "orbit", "out-all"):
        out.append(cmd("zoom_to_globe"))
    if height:
        out.append(cmd("move_camera", altitude=int(height)))
    if place:
        # their own geocoder, via the app: it resolves the label the same way
        # it does when you click on the globe, so we never disagree with it
        out.append(cmd("fly_to_location", query=place))
    elif lat is not None and lon is not None:
        out.append(cmd("fly_to_location", latitude=float(lat), longitude=float(lon)))
    for lid in (show or []):
        if is_layer(lid):
            out.append(cmd("set_layer_visibility", layerId=lid, visible=True))
    for lid in (hide or []):
        if is_layer(lid):
            out.append(cmd("set_layer_visibility", layerId=lid, visible=False))
    if iss:
        out.append(cmd("next_iss_pass"))
    if nearest_aircraft:
        out.append(cmd("select_nearest_aircraft"))
    return out


def geocode(query: str) -> Optional[dict]:
    from core import globe as _globe
    return _globe.place(query)


def route(origin: str, destination: str) -> Optional[dict]:
    """Hand two places to their OSRM routing and let the app fly it.

    Their /api/route endpoint does the geocoding and the street-following
    geometry; we only translate, so the route is theirs, not a parallel
    implementation that would drift.
    """
    cmds: list[dict] = []
    a, b = geocode(origin), geocode(destination)
    if a is None or b is None:
        missing = origin if a is None else destination
        raise ValueError(f"I could not find {missing}.")
    cmds.append(cmd("set_layer_visibility", layerId="directions", visible=True))
    cmds.append(cmd("fly_route", origin={"lat": a["lat"], "lon": a["lon"],
                                         "label": a["name"]},
                    destination={"lat": b["lat"], "lon": b["lon"],
                                 "label": b["name"]},
                    mode="drive", follow=True))
    return {"commands": cmds,
            "summary": f"{a['name']} to {b['name']}"}


# ── talking about it ─────────────────────────────────────────────────────────

def url() -> str:
    """The app's own URL as the browser should load it."""
    return f"{MOUNT}/"


def describe(cmds: list[dict], *, live: Optional[bool] = None) -> str:
    """One honest sentence for the model to say out loud."""
    acts = {c["action"] for c in cmds}
    bits = []
    if "fly_to_location" in acts:
        bits.append("flying to it")
    if "fly_route" in acts:
        bits.append("drawing the route")
    if "zoom_to_globe" in acts:
        bits.append("pulling back to the globe")
    if "adjust_camera_zoom" in acts:
        bits.append("zooming")
    if "set_visual_style" in acts:
        bits.append("switching the look")
    turned = [c["args"].get("layerId") for c in cmds
              if c["action"] == "set_layer_visibility"]
    if turned:
        bits.append("turning on " + ", ".join(t for t in turned if t))
    out = "On the globe: " + (", ".join(bits) if bits else "it is up") + "."
    if live is False:
        out += (" Note: its live data server is not answering, so the live layers "
                "are empty right now — imagery and the map still work.")
    return out
