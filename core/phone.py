"""core/phone.py — the assistant's actuators on the user's phone.

WHY A PWA AND NOT A NATIVE APP
    The dashboard already ships a manifest and a service worker, so the Space is
    already installable on Android with no APK, no sideloading, no store, and no
    `adb pair` ceremony. Adding a native client would mean a release process,
    a signing key, and a per-device update path, for a capability that a
    web push subscription plus a few device APIs already covers.

    So: the phone runs JARVIS as an installed web app, and this module is what
    the assistant uses to act on it.

WHAT A PWA CAN AND CANNOT DO
    Being straight about this matters more than the feature list. A web app CAN:

      * raise a notification, with buttons, that wakes the phone
      * vibrate it, in patterns
      * open a URL, which is how "open Spotify" and "call this person" work
      * report its location, its battery, whether it is charging and online
      * stream its camera and microphone to the assistant
      * hold a wake lock, so the screen stays on while JARVIS is listening

    And it CANNOT:

      * tap or swipe anything outside its own window. There is no accessibility
        API in a browser. "Tap the 4th icon on my home screen" is not possible
        from here and pretending otherwise would waste an evening.

    That limit is why every action below either reaches the user (a
    notification, a vibration) or uses a deep link, and why there is no "tap"
    verb. If you want literal screen control, that is ADB or an accessibility
    service, and it is a different piece of work.

WHY ALMOST EVERYTHING ASKS
    Reading the battery is harmless. Making a noise at 3am, or opening a link on
    someone's phone, is a thing a user should agree to the first time. So the
    gate is per-action: `status` and `locate` are free, and everything that
    makes noise or moves the screen asks.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from core import push


# ── the device's own state, which the PWA posts back ─────────────────────────

def report(device: str, kind: str, data: Optional[dict] = None) -> dict:
    """The phone telling us something: its battery, where it is, a camera frame.

    Stored, not just logged. The last reading is what `status` reports and what
    the model reasons about, so "where is my phone" is a question with an answer
    rather than a guess.
    """
    from core.data_paths import data_root
    name = str(device or "phone").strip() or "phone"
    kind = str(kind or "unknown").strip() or "unknown"
    safe = str(kind)[:40]
    # a camera frame is large and useless to keep; note it and drop it
    body = dict(data or {})
    if safe in ("camera", "screen", "microphone") and body.get("data"):
        body = {"has_frame": True,
                "bytes": len(str(body.pop("data")))}
    p = data_root() / "device_state.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(cur, dict):
            cur = {}
    except Exception:
        cur = {}
    # Per device PER KIND. The first version stored one record per device, so a
    # camera frame arriving wiped the battery and location readings, and
    # "where is my phone" answered "nothing reported" seconds after the phone
    # had told us. A device has several facts, not one.
    dev = cur.get(name) if isinstance(cur.get(name), dict) else {}
    dev[safe] = {"data": body}
    cur[name] = dev
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        os_mode = True
    except Exception:
        os_mode = False
    try:
        import os
        os.chmod(p, 0o600)
    except Exception:
        pass
    return {"ok": os_mode, "device": name, "kind": safe}


def devices() -> dict:
    from core.data_paths import data_root
    try:
        return json.loads((data_root() / "device_state.json").read_text(
            encoding="utf-8"))
    except Exception:
        return {}


def _device(name: str = "") -> dict:
    rows = devices()
    if name:
        return rows.get(name) or {}
    if len(rows) == 1:
        return next(iter(rows.values()))
    return {}


# ── actuators ────────────────────────────────────────────────────────────────

def _subs() -> int:
    try:
        return push.sub_count()
    except Exception:
        return 0


def buzz(pattern: str = "200,100,200", title: str = "", body: str = "") -> dict:
    """A notification. The vibrate pattern is carried in the payload and the
    installed PWA applies it, because a web notification cannot vibrate
    without one."""
    pat = str(pattern or "").strip()
    data = {"vibrate": [int(x) for x in pat.split(",") if x.strip().isdigit()]} \
        if pat else {}
    return push.notify(title or "JARVIS", body or "", data=data,
                       tag="jarvis-buzz", require_interaction=False)


def open_url(url: str, title: str = "", body: str = "") -> dict:
    """Ask the phone to open a link. This is how "open Spotify" works: the app
    handles its own scheme, and a notification is the only honest way to get the
    user to tap through."""
    u = str(url or "").strip()
    if not u:
        return {"ok": False, "sent": 0, "error": "no url"}
    r = push.notify(title or "Open this", body or u, url=u, tag="jarvis-open")
    r["url"] = u
    return r


def wake(text: str = "") -> dict:
    """A notification with `require_interaction`, which is the strongest a PWA
    can do to keep something on screen and demand a tap."""
    r = push.notify("JARVIS", str(text or ""), tag="jarvis-wake",
                    require_interaction=True)
    r["require_interaction"] = True
    return r


def read(kind: str = "", device: str = "") -> dict:
    """What the phone last reported. Reading, so it is free."""
    rows = devices()
    if kind:
        for name, kinds in rows.items():
            rec = kinds.get(kind) if isinstance(kinds, dict) else None
            if rec:
                return {"ok": True, "device": name, "kind": kind,
                        "data": rec.get("data") or {}}
        return {"ok": False, "error": f"no {kind} reading from any device",
                "devices": sorted(rows)}
    d = _device(device)
    if not d:
        return {"ok": False, "error": "the phone has not reported anything yet",
                "subs": _subs()}
    first = next(iter(d), "unknown")
    return {"ok": True, "device": device or "phone", "kind": first,
            "data": (d.get(first) or {}).get("data") or {}}


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, device: str = "", title: str = "",
         body: str = "", url: str = "", pattern: str = "", kind: str = "",
         data: Optional[dict] = None) -> str:
    """Prose back. This is spoken aloud, often at volume, so it says plainly
    whether the phone actually got it — a notification that silently failed to
    send is worse than one that was never attempted."""
    a = str(action or "").strip().lower()
    try:
        if a in ("", "status"):
            n = _subs()
            rows = devices()
            if not n and not rows:
                return ("The phone app is not installed yet. Open the JARVIS "
                        "address on the phone and choose Add to Home screen — "
                        "it installs like an app, with nothing to download.")
            st = push.status()
            out = [f"{n} phone subscription(s)."]
            for name, kinds in rows.items():
                for kind, rec in (kinds or {}).items():
                    d = (rec or {}).get("data") or {}
                    if kind == "battery":
                        out.append(f"  {name}: battery {d.get('level')}% "
                                   f"{'charging' if d.get('charging') else 'not charging'}")
                    elif kind == "location":
                        out.append(f"  {name}: last at {d.get('lat')}, {d.get('lon')}")
                    elif kind == "camera":
                        out.append(f"  {name}: camera frame received")
                    else:
                        out.append(f"  {name}: {kind}")
            if rows and not n:
                out.append("Note: the app reported in, but has not subscribed "
                           "to notifications, so I can read it and cannot "
                           "reach it. Re-open the app on the phone and allow "
                           "notifications.")
            if st.get("last_error"):
                out.append(f"last error: {st['last_error']}")
            return "\n".join(out)

        if a in ("read", "get", "where", "locate", "battery"):
            r = read(kind or ("location" if a in ("where", "locate")
                              else "battery" if a == "battery" else ""), device)
            if not r.get("ok"):
                return str(r.get("error") or "nothing reported")
            d = r["data"]
            if r["kind"] == "battery":
                return (f"{r['device']} is at {d.get('level')}% and "
                        f"{'charging' if d.get('charging') else 'not charging'}.")
            if r["kind"] == "location":
                return (f"{r['device']} last reported at {d.get('lat')}, "
                        f"{d.get('lon')}.")
            return f"{r['device']} last reported {r['kind']}: {json.dumps(d)[:200]}"

        if a in ("buzz", "notify", "alert", "ping"):
            r = buzz(pattern, title, body or " ")
            if not r.get("sent"):
                return ("That did not reach the phone. It may not be installed "
                        "— tell the user to open the JARVIS address on the "
                        "phone and Add to Home screen.")
            return f"Notified {r['sent']} device(s)."

        if a in ("open", "launch", "deeplink"):
            if not url:
                return "Give me a URL to open on the phone."
            r = open_url(url, title, body)
            if not r.get("sent"):
                return "That did not reach the phone — it may not be installed."
            return (f"Sent {r['sent']} notification(s) that open {url}. It is "
                    f"a tap away — a web app cannot open a screen by itself.")

        if a in ("wake", "attention", "important"):
            r = wake(body or title or "You are wanted.")
            if not r.get("sent"):
                return "That did not reach the phone."
            return f"Sent {r['sent']} notification(s) that stay until tapped."

        if a in ("report", "ingest"):
            r = report(device, kind, data or {})
            return (f"Recorded {r['kind']} from {r['device']}."
                    if r.get("ok") else "Could not record that.")

        return "Unknown action. Use status / battery / locate / buzz / open / wake."
    except Exception as e:
        return f"phone: {type(e).__name__}: {e}"[:200]
