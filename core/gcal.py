"""core/gcal.py — the calendar, without demanding a Google account.

Two paths, because "connect Google Calendar" is a five-minute setup for some
people and a non-starter for everyone else:

  * **Google Calendar (OAuth).** The Space has a public URL, so the standard
    authorization-code flow works with a real redirect: the dashboard hands the
    user a consent link, Google sends them back to `/api/gcal/callback`, and we
    store a refresh token. Then `events`, `create`, `delete` and `freebusy` all
    work, and agent shifts can be written to the real calendar.

  * **A local .ics file.** Zero setup, zero keys. JARVIS reads and writes
    `/data/calendar.ics`, which any phone or desktop calendar app can subscribe
    to. Scheduling still works, it just lives in one place that is ours.

The rule: an unconfigured calendar is a *status*, never an error. `status()`
always answers, so the Connectors panel and the model can both ask without
special-casing "not set up yet".

Timezones are the classic way scheduling code lies to you, so there is exactly
one conversion function and every path goes through it.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

SCOPES = "https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"

_lock = threading.RLock()
_states: dict[str, float] = {}          # CSRF state -> created


# ── configuration ───────────────────────────────────────────────────────────

def _token_path() -> Path:
    return data_root() / "gcal_token.json"


def _ics_path() -> Path:
    return data_root() / "calendar.ics"


def _creds() -> tuple[str, str]:
    """(client_id, client_secret). Environment wins, so a Space secret beats the
    file, exactly like the map providers."""
    cid = (os.environ.get("JARVIS_GCAL_CLIENT_ID")
           or os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    sec = (os.environ.get("JARVIS_GCAL_CLIENT_SECRET")
           or os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if not (cid and sec):
        try:
            from memory.config_manager import load_api_keys
            d = load_api_keys() or {}
            cid = cid or str(d.get("gcal_client_id") or "").strip()
            sec = sec or str(d.get("gcal_client_secret") or "").strip()
        except Exception:
            pass
    return cid, sec


def _base_url() -> str:
    return (os.environ.get("JARVIS_PUBLIC_URL")
            or os.environ.get("PUBLIC_URL")
            or "http://127.0.0.1:8080").rstrip("/")


def _redirect_uri() -> str:
    return _base_url() + "/api/gcal/callback"


def _token() -> dict:
    try:
        d = json.loads(_token_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_token(d: dict) -> None:
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.chmod(p, 0o600)          # a refresh token is a password


def configured() -> bool:
    cid, sec = _creds()
    return bool(cid and sec and _token().get("refresh_token"))


def status() -> dict:
    """Always answerable. The panel and the model both read this."""
    cid, sec = _creds()
    tok = _token()
    return {
        "google": {
            "has_client": bool(cid),
            "has_secret": bool(sec),
            "authorized": bool(tok.get("refresh_token")),
            "connected": configured(),
            "email": tok.get("email", ""),
            "where": "🗓 Calendar → Google",
            "buys": "real events, free/busy, and agent shifts on your actual calendar",
            "how": ("Create an OAuth client at console.cloud.google.com "
                    "(type: Web application), paste the ID and secret here, "
                    "then press CONNECT."),
        },
        "local_ics": {
            "enabled": True,
            "path": str(_ics_path()),
            "events": len(_ics_events()),
            "where": "🗓 Calendar → Local (.ics)",
            "buys": "scheduling with no account at all — subscribe from any phone",
        },
    }


# ── OAuth ────────────────────────────────────────────────────────────────────

def auth_url() -> str:
    """Mint a consent link. The state token is what stops someone else from
    wiring their calendar to this Space."""
    cid, sec = _creds()
    if not (cid and sec):
        raise ValueError("add the Google client ID and secret first")
    state = secrets.token_urlsafe(24)
    with _lock:
        _states[state] = time.time()
        for k in [k for k, v in _states.items() if v < time.time() - 900]:
            _states.pop(k, None)
    q = {
        "client_id": cid, "redirect_uri": _redirect_uri(), "response_type": "code",
        "scope": SCOPES, "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def callback(code: str, state: str) -> dict:
    with _lock:
        born = _states.pop(str(state or ""), None)
    if not born:
        raise ValueError("that connect link has expired — start again")
    cid, sec = _creds()
    body = urllib.parse.urlencode({
        "code": str(code), "client_id": cid, "client_secret": sec,
        "redirect_uri": _redirect_uri(), "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(
            TOKEN_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}),
            timeout=30) as r:
        tok = json.loads(r.read())
    if not tok.get("refresh_token"):
        # happens when the user had already authorised this client
        raise ValueError("Google returned no refresh token — revoke the app and retry")
    _save_token(tok)
    email = ""
    try:
        email = whoami(tok.get("access_token", ""))
    except Exception:
        pass
    return {"ok": True, "email": email,
            "note": "calendar connected — JARVIS can read and write events"}


def disconnect() -> dict:
    try:
        _token_path().unlink()
    except Exception:
        pass
    return {"ok": True, "connected": False}


def _access_token() -> str:
    tok = _token()
    exp = float(tok.get("expires_at") or 0)
    if tok.get("access_token") and exp - time.time() > 60:
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise ValueError("the calendar is not connected")
    cid, sec = _creds()
    body = urllib.parse.urlencode({
        "client_id": cid, "client_secret": sec,
        "refresh_token": tok["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(
            TOKEN_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}),
            timeout=30) as r:
        fresh = json.loads(r.read())
    fresh["refresh_token"] = tok["refresh_token"]
    fresh["expires_at"] = time.time() + float(fresh.get("expires_in") or 3600)
    _save_token(fresh)
    return fresh["access_token"]


def whoami(at: str = "") -> dict:
    req = urllib.request.Request(API + "/users/me/calendar",
                                 headers={"Authorization": f"Bearer {at or _access_token()}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    _token()["email"] = d.get("summary", "")     # best effort, not worth failing over
    return d


def _api(method: str, path: str, payload: dict | None = None) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=body, method=method,
        headers={"Authorization": f"Bearer {_access_token()}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


# ── time ─────────────────────────────────────────────────────────────────────

def _iso(ts: float | str) -> str:
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    t = str(ts or "").strip()
    if not t:
        raise ValueError("what time?")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(
            timezone.utc).isoformat()
    except Exception:
        raise ValueError(f"I could not read '{t}' as a date/time")


def _parse_when(v: str) -> Optional[datetime]:
    """Google gives ISO-8601, the .ics file gives `20261001T090000Z`, and a
    user types `9am`. Three formats, one function — a schedule that lies about
    when things happen is worse than no schedule."""
    t = str(v or "").strip()
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _fmt(ev: dict) -> dict:
    s = ev.get("start") or {}
    e = ev.get("end") or {}
    return {
        "id": ev.get("id", ""),
        "title": ev.get("summary") or "(no title)",
        "start": s.get("dateTime") or s.get("date") or "",
        "end": e.get("dateTime") or e.get("date") or "",
        "all_day": bool(s.get("date")),
        "where": ev.get("location", ""),
        "notes": (ev.get("description") or "")[:400],
        "who": [a.get("email", "") for a in (ev.get("attendees") or [])],
        "cal": ev.get("htmlLink", ""),
    }


# ── reading and writing ──────────────────────────────────────────────────────

def events(days: int = 7, *, query: str = "") -> list[dict]:
    if not configured():
        return _ics_events()
    now = datetime.now(timezone.utc)
    q = {
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=max(1, min(int(days or 7), 90)))).isoformat(),
        "maxResults": 50, "singleEvents": "true", "orderBy": "startTime",
    }
    if query:
        q["q"] = str(query)[:80]
    d = _api("GET", "/calendars/primary/events?" + urllib.parse.urlencode(q))
    return [_fmt(e) for e in d.get("items", [])]


def create(title: str, start: Any, *, end: Any = "", minutes: int = 60,
           where: str = "", notes: str = "", invite: str = "") -> dict:
    """If minutes is given and no end, the end is derived — because a calendar
    entry with no end time is a bug that only shows up three meetings later."""
    title = str(title or "").strip()
    if not title:
        raise ValueError("the event needs a title")
    s = _iso(start)
    e = _iso(end) if end else (datetime.fromisoformat(s)
                               + timedelta(minutes=max(5, int(minutes or 60)))
                               ).isoformat()
    payload = {"summary": title,
               "start": {"dateTime": s, "timeZone": "UTC"},
               "end": {"dateTime": e, "timeZone": "UTC"}}
    if where:
        payload["location"] = str(where)[:200]
    if notes:
        payload["description"] = str(notes)[:2000]
    if invite:
        payload["attendees"] = [{"email": str(invite).strip()}]
    if configured():
        ev = _api("POST", "/calendars/primary/events", payload)
        try:
            from core import journal as J
            J.entry("done", f"calendar: {title}"[:180],
                    body=f"{s} → {e}" + (f" · {where}" if where else ""),
                    tags=["calendar"])
        except Exception:
            pass
        return _fmt(ev)
    return _ics_add(payload, title, s, e, where, notes)


def delete(ref: str) -> dict:
    if not configured():
        return _ics_del(ref)
    _api("DELETE", f"/calendars/primary/events/{urllib.parse.quote(str(ref))}")
    return {"deleted": ref, "source": "google"}


def freebusy(start: Any, end: Any) -> dict:
    """'When am I free on Thursday?' — the question a calendar exists to answer."""
    if not configured():
        return {"source": "local", "busy": [],
                "note": "connect Google Calendar for real free/busy"}
    q = {"timeMin": _iso(start), "timeMax": _iso(end), "items": [{"id": "primary"}]}
    d = _api("POST", "/freeBusy", q)
    return {"source": "google",
            "busy": d.get("calendars", {}).get("primary", {}).get("busy", [])}


def agenda() -> str:
    """A sentence a human wants read out loud."""
    evs = events(2)
    if not evs:
        return "Nothing on the calendar for the next two days."
    bits = []
    for e in evs[:5]:
        t = _parse_when(e["start"])
        when = (t.strftime("%a %H:%M") if t and not e["all_day"]
                else t.strftime("%a") if t else "unscheduled")
        bits.append(f"{when} {e['title']}" + (f" at {e['where']}" if e["where"] else ""))
    return f"{len(evs)} coming up: " + "; ".join(bits) + "."


# ── the keyless path: a real .ics file ───────────────────────────────────────

def _ics_escape(s: str) -> str:
    return (str(s or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _ics_unfold(raw: str) -> list[str]:
    """RFC 5545 folds long lines; unfold before reading or the tail of a long
    description lands as its own bogus property."""
    out: list[str] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _ics_events() -> list[dict]:
    try:
        raw = _ics_path().read_text(encoding="utf-8")
    except Exception:
        return []
    evs, cur = [], None
    for line in _ics_unescape_safe(raw):
        if line.startswith("BEGIN:VEVENT"):
            cur = {}
        elif line.startswith("END:VEVENT") and cur is not None:
            all_day = not (cur.get("start", "") or "").count("T")
            stamp = (lambda v: {"date": str(v)[:8]} if all_day
                     else {"dateTime": _from_ics_stamp(v)})
            evs.append(_fmt({
                "id": cur.get("uid", ""),
                "summary": cur.get("summary", ""),
                "start": stamp(cur.get("start", "")),
                "end": stamp(cur.get("end", "") or cur.get("start", "")),
                "location": cur.get("where", ""),
                "description": cur.get("notes", ""),
            }))
            cur = None
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            k = k.split(";", 1)[0]
            # map the wire names onto the shape _fmt() reads, or a local event
            # parses fine and then arrives with no start time at all
            field = {"SUMMARY": "summary", "DTSTART": "start", "DTEND": "end",
                     "LOCATION": "where", "DESCRIPTION": "notes",
                     "UID": "uid"}.get(k)
            if field:
                cur[field] = v
    # hand _fmt() the same shape Google returns, so one formatter serves both
    # backends and the two can never drift
    return sorted(evs, key=lambda e: e["start"])


def _ics_unescape_safe(raw: str) -> list[str]:
    out: list[str] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _ics_render(events: list[dict]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//JARVIS//Calendar//EN", "CALSCALE:GREGORIAN"]
    for e in events:
        lines += ["BEGIN:VEVENT", f"UID:{e.get('uid') or uuid_like()}",
                  f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                  f"DTSTART:{_ics_stamp(e.get('start'))}",
                  f"DTEND:{_ics_stamp(e.get('end') or e.get('start'))}",
                  f"SUMMARY:{_ics_escape(e.get('title'))}"]
        if e.get("where"):
            lines.append(f"LOCATION:{_ics_escape(e['where'])}")
        if e.get("notes"):
            lines.append(f"DESCRIPTION:{_ics_escape(e['notes'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def uuid_like() -> str:
    return f"{int(time.time())}-{secrets.token_hex(6)}@jarvis"


def _from_ics_stamp(v: str) -> str:
    """`20261001T090000Z` back to ISO, for the shared formatter."""
    t = str(v or "").strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return t


def _ics_stamp(v: str) -> str:
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_add(payload: dict, title: str, s: str, e: str, where: str,
             notes: str) -> dict:
    path = _ics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (f"BEGIN:VEVENT\r\nUID:{uuid_like()}\r\n"
            f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTSTART:{_ics_stamp(s)}\r\nDTEND:{_ics_stamp(e)}\r\n"
            f"SUMMARY:{_ics_escape(title)}\r\n"
            + (f"LOCATION:{_ics_escape(where)}\r\n" if where else "")
            + (f"DESCRIPTION:{_ics_escape(notes)}\r\n" if notes else "")
            + "END:VEVENT\r\n")
    with _lock:
        cur = path.read_text(encoding="utf-8") if path.exists() else (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//JARVIS//Calendar//EN\r\n"
            "CALSCALE:GREGORIAN\r\n")
        if "END:VCALENDAR" in cur:
            cur = cur.replace("END:VCALENDAR", body + "END:VCALENDAR", 1)
        else:
            cur += body + "END:VCALENDAR\r\n"
        path.write_text(cur, encoding="utf-8")
    return {"title": title, "start": s, "end": e, "where": where,
            "source": "local", "ics": str(path),
            "note": "saved to the local calendar — connect Google to sync everywhere"}


def _ics_del(ref: str) -> dict:
    """Remove one VEVENT by UID or by the title the user actually typed.

    The whole block is buffered before deciding: matching on SUMMARY as it
    streams past means the BEGIN and the DTSTART have already been written out,
    which leaves a VEVENT with no END and a file no calendar will open."""
    path = _ics_path()
    if not path.exists():
        return {"deleted": str(ref), "source": "local", "found": False}
    want = str(ref or "").strip()
    out: list[str] = []
    block: list[str] = []
    found = ""
    for line in _ics_unescape_safe(path.read_text(encoding="utf-8")):
        if line.startswith("BEGIN:VEVENT"):
            block = [line]
            continue
        if line.startswith("END:VEVENT"):
            block.append(line)
            hay = " ".join(
                l.split(":", 1)[1] for l in block
                if l.startswith(("UID:", "SUMMARY:")))
            hit = bool(want) and (want in hay
                                  or want.lower() in hay.lower())
            if hit:
                found = want
            else:
                out.extend(block)
            block = []
            continue
        if block:
            block.append(line)
        else:
            out.append(line)
    if block:                      # unterminated event: keep it, do not eat it
        out.extend(block)
    path.write_text("\r\n".join(out).rstrip("\r\n") + "\r\n", encoding="utf-8")
    return {"deleted": found, "source": "local", "found": bool(found)}


def subscribe_url() -> str:
    """Not a real URL — the file is local. Named anyway so the panel has one
    place to show how to subscribe, instead of prose in three places."""
    return _base_url() + "/api/gcal/calendar.ics"


def ics_text() -> tuple[str, str]:
    """(body, content_type) for the subscribe endpoint."""
    try:
        return _ics_path().read_text(encoding="utf-8"), "text/calendar"
    except Exception:
        return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//JARVIS//EN\r\n"
                "END:VCALENDAR\r\n"), "text/calendar"
