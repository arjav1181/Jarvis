"""core/home.py — the house, as a set of things you can actually do.

`jarvisd` already pairs phones and boxes to this server over `/ws/agent`. This
module turns that existing channel into a home: named devices, named scenes, and
one place to ask what the house is doing.

Four adapter kinds, in descending order of how much I trust them:

  * **paired** — a `jarvisd` limb. It already has a token and a heartbeat, so
    this is the native path: `{"kind": "paired", "device": "lamp", "op": "on"}`.
  * **mqtt** — anything on a broker (Home Assistant, Zigbee2MQTT, Tasmota).
    Topic templates, so one scene covers a whole room.
  * **hass** — Home Assistant's REST API, for entities the broker does not
    expose as a topic.
  * **webhook** — a plain HTTP call, for a camera, a relay, a doorbell.

Why scenes and not raw commands: "movie night" is four devices and one idea. A
scene is the idea; the device list is the implementation. Scenes also make
schedules legible — a scheduler job that says "run movie night" tells you what
the house will do without a table of topic strings.

The one thing that is always gated: anything that opens a door, unlocks, or
arms a security system. Lights and music are free; letting a language model open
your front door is not, no matter how it phrases the request.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from core.data_paths import data_root

KINDS = ("paired", "mqtt", "hass", "webhook", "virtual")

#: verbs that touch the physical world in a way you cannot undo
GATED_WORDS = ("unlock", "open_door", "open_gate", "arm", "disarm", "panic",
               "valve", "boiler", "heater_off", "kill_power")


def _always_gated(device: str, op: str) -> bool:
    blob = f"{device} {op}".lower().replace(" ", "_").replace("-", "_")
    return any(w in blob for w in GATED_WORDS)


_lock = threading.RLock()
_path = None
_cache: Optional[dict] = None


def _file():
    global _path
    if _path is None:
        _path = data_root() / "home.json"
    return _path


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            d = json.loads(_file().read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d.setdefault("devices", [])
        d.setdefault("scenes", [])
        d.setdefault("runs", [])
        d.setdefault("log", [])
        _cache = d
        return d


def _save() -> None:
    with _lock:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)


def reset() -> None:
    global _cache, _path
    with _lock:
        _cache = None
        _path = None


# ── the registry ─────────────────────────────────────────────────────────────

def device(name: str, kind: str = "virtual", *, op: str = "", args: Any = None,
           where: str = "", note: str = "", enabled: bool = True) -> dict:
    """Declare a device. `virtual` is not a toy: it is how you dry-run a scene
    before it touches the real house, and how the tests stay honest."""
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("a device needs a name")
    kind = str(kind or "virtual").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    with _lock:
        d = _load()
        row = next((x for x in d["devices"] if x["name"].lower() == name.lower()),
                   None)
        if row is None:
            row = {"name": name, "created": time.time(), "runs": 0}
            d["devices"].append(row)
        row.update({"kind": kind, "op": str(op or "toggle")[:40],
                    "args": args or {}, "where": str(where or "")[:80],
                    "note": str(note or "")[:200], "enabled": bool(enabled),
                    "state": row.get("state", "off"), "updated": time.time()})
        _save()
        return dict(row)


def devices() -> list[dict]:
    with _lock:
        rows = [dict(x) for x in _load()["devices"]]
    rows.sort(key=lambda r: (not r.get("enabled"), r["name"].lower()))
    for r in rows:
        r["gated"] = _always_gated(r.get("name", ""), r.get("op", ""))
    return rows


def find(name: str) -> Optional[dict]:
    n = str(name or "").strip().lower()
    with _lock:
        for d in _load()["devices"]:
            if n and (n == d["name"].lower() or n in d["name"].lower()):
                return dict(d)
    return None


# ── scenes ───────────────────────────────────────────────────────────────────

def scene(name: str, steps: list, *, enabled: bool = True) -> dict:
    """A step is `{"device": "lamp", "op": "on"}` or
    `{"device": "lamp", "op": "on", "after": "tv"}` to order it behind another."""
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("a scene needs a name")
    if not isinstance(steps, list) or not steps:
        raise ValueError("a scene needs at least one step")
    clean = []
    for s in steps[:20]:
        if not isinstance(s, dict):
            continue
        dev = str(s.get("device") or "").strip()
        if not dev:
            raise ValueError(f"step {s!r} has no device")
        clean.append({"device": dev, "op": str(s.get("op") or "toggle")[:40],
                      "args": s.get("args") or {},
                      "after": str(s.get("after") or "")[:60]})
    if not clean:
        raise ValueError("none of those steps were usable")
    with _lock:
        d = _load()
        row = next((x for x in d["scenes"] if x["name"].lower() == name.lower()),
                   None)
        if row is None:
            row = {"name": name, "created": time.time(), "runs": 0}
            d["scenes"].append(row)
        row["steps"] = clean
        row["enabled"] = bool(enabled)
        row["gated"] = any(_always_gated(s["device"], s["op"]) for s in clean)
        row["updated"] = time.time()
        _save()
        return dict(row)


def scenes() -> list[dict]:
    with _lock:
        rows = [dict(x) for x in _load()["scenes"]]
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def find_scene(name: str) -> Optional[dict]:
    n = str(name or "").strip().lower()
    with _lock:
        for s in _load()["scenes"]:
            if n and (n == s["name"].lower() or n in s["name"].lower()):
                return dict(s)
    return None


def delete_scene(name: str) -> dict:
    with _lock:
        d = _load()
        row = next((x for x in d["scenes"]
                    if str(name).lower() in x["name"].lower()), None)
        if row is None:
            raise ValueError(f"no scene called '{name}'")
        d["scenes"].remove(row)
        _save()
    return {"deleted": row["name"]}


# ── running ──────────────────────────────────────────────────────────────────

def _apply(d: dict, op: str, args: dict) -> dict:
    """One device, one op. Returns what happened, never raises on a failure —
    a scene with one dead bulb is still a scene that mostly worked."""
    kind = d.get("kind", "virtual")
    if kind == "virtual":
        state = d.get("state", "off")
        if op in ("on", "true", "1", "open"):
            new = "on"
        elif op in ("off", "false", "0", "close"):
            new = "off"
        elif op == "toggle":
            new = "off" if state == "on" else "on"
        else:
            new = str(op)[:40]
        return {"ok": True, "state": new, "detail": "virtual"}
    if kind == "paired":
        return _paired(d, op, args)
    if kind == "mqtt":
        return _mqtt(d, op, args)
    if kind == "hass":
        return _hass(d, op, args)
    if kind == "webhook":
        return _webhook(d, op, args)
    return {"ok": False, "error": f"unknown kind '{kind}'"}


def _paired(d: dict, op: str, args: dict) -> dict:
    """Send over the existing agent websocket. The server object is looked up
    lazily so this module stays importable in a test with no server running."""
    try:
        from dashboard.server import app_state
        srv = app_state.get("jarvis")
        if srv is None or not getattr(srv, "_dashboard", None):
            return {"ok": False, "error": "no agent channel right now"}
        sent = srv._dashboard.broadcast({"type": "home", "device": d["name"],
                                         "op": op, "args": args})
        return {"ok": True, "state": op, "detail": "sent to paired limb",
                "queued": bool(sent)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}


def _mqtt(d: dict, op: str, args: dict) -> dict:
    host = os_env("JARVIS_MQTT_HOST")
    if not host:
        return {"ok": False,
                "error": "no MQTT broker — set JARVIS_MQTT_HOST as a Space secret"}
    topic = str(d.get("args", {}).get("topic") or "").format(
        device=d["name"].replace(" ", "_").lower(), op=op)
    if not topic:
        return {"ok": False, "error": "this device has no MQTT topic template"}
    payload = json.dumps({"state": op, **(args or {})})
    try:
        import paho.mqtt.publish as pub
        pub.single(topic, payload, hostname=host,
                   port=int(os_env("JARVIS_MQTT_PORT") or 1883),
                   auth_username=os_env("JARVIS_MQTT_USER") or None,
                   auth_password=os_env("JARVIS_MQTT_PASS") or None)
        return {"ok": True, "state": op, "detail": f"published {topic}"}
    except Exception as e:
        return {"ok": False, "error": f"paho-mqtt not available: {e}"[:120]}


def _hass(d: dict, op: str, args: dict) -> dict:
    base = (os_env("JARVIS_HASS_URL") or "").rstrip("/")
    token = os_env("JARVIS_HASS_TOKEN") or ""
    if not (base and token):
        return {"ok": False,
                "error": "no Home Assistant — set JARVIS_HASS_URL and "
                         "JARVIS_HASS_TOKEN"}
    ent = str(d.get("args", {}).get("entity") or "")
    if not ent:
        return {"ok": False, "error": "this device has no Home Assistant entity"}
    state = {"on": "ON", "off": "OFF", "toggle": "TOGGLE"}.get(op, op.upper())
    body = json.dumps({"state": state}).encode()
    req = urllib.request.Request(
        f"{base}/api/services/homeassistant/turn_on" if state != "OFF"
        else f"{base}/api/services/homeassistant/turn_off",
        data=body if state != "TOGGLE" else None,
        method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return {"ok": True, "state": op, "detail": f"hass {ent} → {state}"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}


def _webhook(d: dict, op: str, args: dict) -> dict:
    url = str(d.get("args", {}).get("url") or "").replace("{op}", op)
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "a webhook device needs an http(s) url"}
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15):
            return {"ok": True, "state": op, "detail": "webhook called"}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}


def os_env(name: str) -> str:
    import os
    return (os.environ.get(name) or "").strip()


def run_device(name: str, op: str = "", *, args: Any = None,
               dry_run: bool = False) -> dict:
    d = find(name)
    if d is None:
        raise ValueError(f"no device called '{name}' — ask me what the house has")
    if not d.get("enabled"):
        return {"ok": False, "error": f"{d['name']} is switched off"}
    op = str(op or d.get("op") or "toggle")
    gated = _always_gated(d["name"], op)
    if gated and not dry_run:
        return {"ok": False, "needs_approval": True, "device": d["name"],
                "op": op,
                "reason": f"{d['name']} {op} affects security or safety — "
                          f"I will not do that without your say-so"}
    if dry_run:
        return {"ok": True, "dry_run": True, "device": d["name"], "op": op,
                "would_call": d.get("kind"), "gated": gated}
    out = _apply(d, op, args or {})
    with _lock:
        dd = _load()
        rec = next((x for x in dd["devices"]
                    if x["name"].lower() == d["name"].lower()), None)
        if rec is not None:
            rec["runs"] = int(rec.get("runs") or 0) + 1
            rec["updated"] = time.time()
            if out.get("state"):
                rec["state"] = out["state"]
        rec_run = {"target": d["name"], "op": op, "ok": bool(out.get("ok")),
                   "detail": str(out.get("detail") or out.get("error") or "")[:120],
                   "at": time.time()}
        dd["runs"].append(rec_run)
        dd["runs"] = dd["runs"][-200:]
        dd["log"].append(rec_run)
        dd["log"] = dd["log"][-200:]
        _save()
    _note(d["name"], op, out)
    return {"ok": bool(out.get("ok")), "device": d["name"], "op": op, **out}


def _note(name: str, op: str, out: dict) -> None:
    try:
        from core import journal as J
        J.entry("done" if out.get("ok") else "error",
                f"home: {name} {op}"[:180],
                body=str(out.get("detail") or out.get("error") or "")[:200],
                tags=["home", name.lower().replace(" ", "_")],
                ok=bool(out.get("ok")), actor="home")
    except Exception:
        pass


def run_scene(name: str, *, dry_run: bool = False) -> dict:
    s = find_scene(name)
    if s is None:
        raise ValueError(f"no scene called '{name}'")
    if not s.get("enabled"):
        return {"ok": False, "error": f"{s['name']} is switched off"}
    # honour `after`, so 'lights down' happens once the TV is on
    steps = list(s["steps"])
    ordered, guard = [], 0
    while steps and guard < 40:
        guard += 1
        for st in list(steps):
            dep = st.get("after")
            if not dep or any(o["device"].lower() == dep.lower() for o in ordered):
                ordered.append(st)
                steps.remove(st)
    ordered += steps                       # anything with a cycle, run last
    if dry_run or s.get("gated"):
        return {"ok": True, "dry_run": True, "gated": bool(s.get("gated")),
                "scene": s["name"],
                "plan": [{"device": x["device"], "op": x["op"]} for x in ordered],
                "reason": ("this scene touches security or safety — approve it "
                           "and I will run it") if s.get("gated") else "dry run"}
    results = []
    for st in ordered:
        try:
            results.append(run_device(st["device"], st["op"], args=st.get("args")))
        except ValueError as e:
            results.append({"ok": False, "device": st["device"], "error": str(e)})
    good = sum(1 for r in results if r.get("ok"))
    with _lock:
        dd = _load()
        row = next((x for x in dd["scenes"]
                    if x["name"].lower() == s["name"].lower()), None)
        if row is not None:
            row["runs"] = int(row.get("runs") or 0) + 1
        _save()
    _note(s["name"], "scene", {"ok": good == len(results),
                               "detail": f"{good}/{len(results)} steps"})
    return {"ok": good == len(results), "scene": s["name"],
            "worked": good, "steps": len(results), "results": results}


def state() -> dict:
    with _lock:
        d = _load()
        runs = list(d["runs"])[-40:]
    return {"devices": devices(), "scenes": scenes(),
            "configured": bool(d["devices"] or d["scenes"]),
            "recent": runs[::-1],
            "ok_rate": (round(sum(1 for r in runs if r.get("ok")) / len(runs), 3)
                        if runs else None)}


def describe() -> str:
    devs, scs = devices(), scenes()
    if not devs and not scs:
        return ("The house is empty — no devices or scenes yet. I can register "
                "a paired limb, an MQTT device, a Home Assistant entity or a "
                "webhook.")
    bits = []
    if devs:
        bits.append(f"{len(devs)} device(s): "
                    + ", ".join(f"{d['name']}({d['kind']})" for d in devs[:6]))
    if scs:
        bits.append(f"{len(scs)} scene(s): " + ", ".join(s["name"] for s in scs[:6]))
    return "; ".join(bits) + "."


def seed() -> int:
    """A small, honest starter house: all virtual, so running it changes nothing.
    Useful for a dry run, and useful in the tests."""
    with _lock:
        if _load()["devices"]:
            return 0
    for name, kind, where, note in (
        ("living room lamp", "virtual", "living room", "demo device"),
        ("desk lamp", "virtual", "office", "demo device"),
        ("tv", "virtual", "living room", "demo device"),
    ):
        device(name, kind, where=where, note=note)
    scene("movie night", [{"device": "tv", "op": "on"},
                          {"device": "living room lamp", "op": "off",
                           "after": "tv"},
                          {"device": "desk lamp", "op": "on", "after": "tv"}])
    scene("good night", [{"device": "living room lamp", "op": "off"},
                         {"device": "desk lamp", "op": "off"}])
    return 1
