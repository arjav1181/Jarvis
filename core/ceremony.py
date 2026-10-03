"""The welcome ceremony: what happens between the clap and the work.

A welcome is easy to make and hard to make *good*. The bad version is a
recording — same words, same order, every single time, which teaches you in
three days to tune it out. The good version varies, is briefly specific to the
moment, and above all is honest.

That last part is the one that matters most here. "All systems are fully
operational" is a wonderful line and a terrible thing to say when something is
broken: a welcome that lies is worse than no welcome, because you trust the
volume of the voice rather than the content. So the status beat is computed,
not decorative — if the computer is asleep or a connector is unconfigured, the
ceremony says which, out loud, and then asks what you want to work on anyway.
An assistant that reports its own damage and keeps going is the whole point.

The sequence, in order:

    music  →  greeting  →  status  →  weather  →  the ask

Music is a file you supply — a wav or an mp3 dropped in, or uploaded from the
panel onto the persistent volume so it survives restarts and never needs a
redeploy. No Spotify, no key, no account: this is a Space, and the ceremony is
a few seconds long.

Weather is open-meteo, which needs no API key and no OAuth dance. Without a
city configured the line is simply omitted rather than guessed — inventing
weather for a place nobody named would be worse than saying nothing.
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Weather lives in core/weather.py now. There were two implementations of
# this question and one of them opened a Google search and called it a
# forecast. Re-exported so nothing downstream has to know it moved.
from core.weather import WMO, weather, weather_line  # noqa: F401

#: en-GB-Ryan is the closest free voice to the real thing. en-US-Guy, which the
#: assistant uses day to day, reads as a different character entirely — and the
#: ceremony is the one moment where the character has to land.
VOICE = "en-GB-RyanNeural"

#: Attribution for the shipped default track. Kevin MacLeod's work is CC BY
#: 4.0, which is a legal obligation and not a courtesy — so it is carried in
#: the data the server sends, shown in the panel, and written in CREDITS.md.
#: A track the user uploads carries no credit and needs none.
CREDIT = ('"Dreams Become Real" by Kevin MacLeod — incompetech.com — '
          'CC BY 4.0. One 22s swell, taken from 6:50 and rebuilt by '
          'tools/prepare_welcome_music.py.')

#: Four stings, shipped in the repository, chosen by ear rather than by
#: statistics. Two earlier picks were rejected as "trash", which is the correct
#: verdict on a waveform and a swell ratio: neither of those is taste. So the
#: right move is to stop picking and let the user listen, which is what
#: candidates() and choose() are for.
#:
#: They differ in the one thing that matters for a welcome — how much the
#: music moves. Measured as level variation inside the 22s window, the four
#: span 0.25 to 0.86, which is the difference between something calm and
#: something that drifts.
#:
#: All four are CC BY 4.0 from incompetech, and every one of them carries a
#: credit. A track the user picks out of these is still somebody's work.
CANDIDATES = (
    {"id": "ethereal", "file": "welcome-ethereal.mp3",
     "title": "Ethereal Relaxation", "mood": "drifting, wide, the most movement",
     "credit": '"Ethereal Relaxation" by Kevin MacLeod — incompetech.com '
               '— CC BY 4.0.'},
    {"id": "dreams", "file": "welcome-dreams.mp3",
     "title": "Dreams Become Real", "mood": "dreamy synths, a little sad",
     "credit": '"Dreams Become Real" by Kevin MacLeod — incompetech.com '
               '— CC BY 4.0.'},
    {"id": "thunderbird", "file": "welcome-thunderbird.mp3",
     "title": "Thunderbird", "mood": "steady and warm, settles early",
     "credit": '"Thunderbird" by Kevin MacLeod — incompetech.com '
               '— CC BY 4.0.'},
    {"id": "vanishing", "file": "welcome-vanishing.mp3",
     "title": "Vanishing", "mood": "the calmest — almost still",
     "credit": '"Vanishing" by Kevin MacLeod — incompetech.com — CC BY 4.0.'},
)

#: Which one plays until the user says otherwise. Changing this writes a
#: pointer on the volume rather than editing the repository, so trying all four
#: is a click and not a deploy.
DEFAULT_CANDIDATE = "ethereal"


def _music_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "dashboard" / "static" / "music"


def _choice_path() -> Path:
    from core.data_paths import data_root
    d = data_root() / "ceremony"
    d.mkdir(parents=True, exist_ok=True)
    return d / "track.txt"


def chosen_id() -> str:
    try:
        want = _choice_path().read_text(encoding="utf-8").strip()
    except Exception:
        want = ""
    ids = {c["id"] for c in CANDIDATES}
    return want if want in ids else DEFAULT_CANDIDATE


def choose(candidate_id: str) -> dict:
    """Pick which of the shipped stings the ceremony plays."""
    cid = str(candidate_id or "").strip().lower()
    row = next((c for c in CANDIDATES if c["id"] == cid), None)
    if row is None:
        return {"ok": False,
                "error": f"no candidate called '{cid}'",
                "choices": [c["id"] for c in CANDIDATES]}
    try:
        _choice_path().write_text(cid, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    return {"ok": True, "id": cid, "title": row["title"],
            "file": row["file"], "music": music()}


def candidates() -> list[dict]:
    """The audition set, with the active one marked."""
    active = chosen_id()
    out = []
    for c in CANDIDATES:
        p = _music_dir() / c["file"]
        out.append({**c, "active": c["id"] == active,
                    "url": f"/static/music/{c['file']}" if p.is_file() else "",
                    "bytes": p.stat().st_size if p.is_file() else 0})
    return out


def chosen_path() -> Optional[Path]:
    row = next((c for c in CANDIDATES if c["id"] == chosen_id()), None)
    if row is None:
        return None
    p = _music_dir() / row["file"]
    return p if p.is_file() else None


#: A user's own track, in a data directory, is found under any of these names.
MUSIC_NAMES = ("welcome.mp3", "welcome.wav", "welcome.ogg", "welcome.m4a",
               "jarvis.mp3", "jarvis.wav", "theme.mp3", "theme.wav")

#: A welcome is a few seconds long, so the music is stopped there even if the
#: file is a full four-minute track. Without this, dropping in any normal song
#: means either trimming it by hand or sitting through the whole thing while
#: the greeting is long over.
def _music_cap() -> int:
    try:
        return max(5, int(str(os.environ.get(
            "JARVIS_WELCOME_MUSIC_SECONDS") or 25).strip()))
    except Exception:
        return 25


MAX_MUSIC_SECONDS = _music_cap()

#: Nudges for a "how are things" reading. Not filler — the pauses are where a
#: butler in a film gets to be a character instead of a function.
OPENERS = (
    "Good {part}, sir.",
    "{Part}, sir. All quiet on my end.",
    "Sir. {Clock}. I'm here and listening.",
    "Good {part} to you, sir.",
    "Sir, I'm awake. What are we doing?",
    "Evening, sir. Or morning. Depends how you count.",
)
STATUS_OK = (
    "All systems are fully operational.",
    "Everything's green, sir.",
    "All systems nominal. Nothing wants your attention yet.",
    "No faults, sir. Quiet house.",
)
SIGNOFFS = (
    "What would you like to work on?",
    "What shall we build today, sir?",
    "Where would you like to start, sir?",
    "What's the first thing, sir?",
)

PART_LABELS = {"dawn": "morning", "morning": "morning",
               "afternoon": "afternoon", "evening": "evening",
               "late": "evening"}

#: WMO weather codes, grouped by what a person would actually do about them.
def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def address() -> str:
    return _env("JARVIS_ADDRESS", "sir") or "sir"


def city() -> str:
    """Delegates, so the dashboard and the greeting cannot disagree about where
    the user is. Two city() functions that answer differently is a bug waiting
    for someone to set one of them."""
    from core import weather as _wx
    return _wx.city()


def enabled() -> bool:
    return _env("JARVIS_WELCOME_ENABLED", "1").lower() not in ("0", "false", "no")


# ── rotation ─────────────────────────────────────────────────────────────────
#
# Deterministic on purpose. `random.choice` would make the ceremony untestable
# and unrepeatable when something goes wrong; a counter seeded by the day gives
# variety within a session and reproducibility after a bug report.

def _state_path() -> Path:
    from core.data_paths import data_root
    d = data_root() / "ceremony"
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def _bump() -> int:
    """How many ceremonies have run. Survives restarts."""
    p = _state_path()
    n = 0
    try:
        n = int(json.loads(p.read_text(encoding="utf-8")).get("runs") or 0)
    except Exception:
        n = 0
    try:
        p.write_text(json.dumps({"runs": n + 1,
                                 "last": round(time.time())}), encoding="utf-8")
    except Exception:
        pass
    return n + 1


def part_of_day(dt: Optional[datetime] = None) -> str:
    dt = dt or datetime.now(timezone.utc).astimezone()
    h = dt.hour
    if h < 6:
        return "dawn"
    if h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    if h < 22:
        return "evening"
    return "late"


def _pick(options: tuple, n: int) -> str:
    return options[n % len(options)]


# ── weather ──────────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: float = 6.0) -> Optional[dict]:
    try:
        import httpx
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        # Weather is a garnish. A ceremony that fails because a weather API
        # hiccupped is a worse product than one that skips the weather.
        return None


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(str(s or ""))


# ── status: computed, never decorative ───────────────────────────────────────

def faults() -> list[str]:
    """What is genuinely wrong right now.

    The distinction that matters: a computer that is ASLEEP is not a fault, it
    is the designed idle state — it is meant to stay off until something asks
    for it. Reporting that as damage would make the ceremony cry wolf on every
    single run, and a status line that is always negative trains you to ignore
    it, which is the exact failure this function exists to prevent.

    So this only reports things that are actually broken: a computer that is
    up but not answering, and declared MCP servers that failed to come up.
    """
    out: list[str] = []
    try:
        from core import computer as C
        st = C.status()
        if st.get("error"):
            out.append(f"the computer is up but not answering: "
                       f"{str(st['error'])[:60]}")
    except Exception:
        out.append("the computer is not answering")
    try:
        from core import mcp as M
        getter = getattr(M, "status", None) or getattr(M, "list_servers", None)
        if callable(getter):
            rows = getter()
            if isinstance(rows, list):
                bad = [str(r.get("name") or r.get("id") or "?") for r in rows
                       if isinstance(r, dict) and str(
                           r.get("state") or r.get("status") or "").lower()
                       in ("error", "failed", "broken")]
                if bad:
                    out.append("MCP servers down: " + ", ".join(bad[:3]))
    except Exception:
        pass
    return out


def notes() -> list[str]:
    """Context that is worth knowing and is NOT damage."""
    out: list[str] = []
    try:
        from core import computer as C
        st = C.status()
        if not st.get("up"):
            out.append("the computer is asleep")
        elif st.get("handover") == "user":
            out.append("you have control of the computer")
    except Exception:
        pass
    return out


def status_line(n: int = 0) -> str:
    """Only ever claims everything works when nothing is wrong.

    The good line is a *claim about the world*, so it may only be spent when
    there is something true to spend it on.
    """
    bad = faults()
    if not bad:
        return _pick(STATUS_OK, n)
    listed = bad[0] if len(bad) == 1 else f"{bad[0]}, and {bad[1]}"
    if len(bad) > 2:
        listed = f"{bad[0]}, {bad[1]}, and {len(bad) - 2} more"
    return f"One thing to report, sir: {listed}."


# ── music ────────────────────────────────────────────────────────────────────

def music_file() -> Optional[Path]:
    """Which file the ceremony plays, and in what order that is decided.

        1. an explicit JARVIS_WELCOME_MUSIC name, if one is set
        2. a track the user uploaded, in the data directory
        3. the candidate they chose out of the shipped four
        4. any other track that happens to sit in the static directory
        5. the default candidate, so a missing choice is never silence

    The order is not arbitrary and it was wrong once. Candidates sat BELOW a
    generic "any file called welcome.*" scan, so choosing a different track
    changed nothing at all while appearing to succeed — the most confusing
    possible failure, because the confirmation said it worked. A choice the
    user made has to outrank a file nobody picked.
    """
    from core.data_paths import data_root
    candidates: list[Path] = []
    named = _env("JARVIS_WELCOME_MUSIC", "")
    upload_dir = data_root() / "ceremony"
    static_dir = Path(__file__).resolve().parents[1] / "dashboard" / "static"
    if named:
        for root in (upload_dir, static_dir):
            candidates.append(root / named)
        candidates.append(Path(named))
    # 2. the user's own track
    for name in MUSIC_NAMES:
        candidates.append(upload_dir / name)
    # 3. the one they chose
    for cid in (chosen_id(), DEFAULT_CANDIDATE):
        row = next((c for c in CANDIDATES if c["id"] == cid), None)
        if row is not None:
            candidates.append(_music_dir() / row["file"])
    # 4. anything else that was simply dropped there
    for name in MUSIC_NAMES:
        candidates.append(static_dir / name)
    for c in candidates:
        try:
            if c.is_file() and c.stat().st_size > 512:
                return c
        except Exception:
            continue
    return None


def _credit_for(f: Path) -> str:
    """The credit belonging to a specific shipped file, not a guess."""
    for c in CANDIDATES:
        if f.name == c["file"]:
            return c["credit"]
    return ""


def music() -> dict:
    f = music_file()
    if f is None:
        # No file yet. Say so plainly rather than pretending there is music,
        # and let the panel fall back to a synthesised riser.
        return {"url": "", "name": "", "kind": "synth",
                "max_seconds": _music_cap(),
                "note": "no welcome.mp3 yet — using the synthesised riser"}
    return {"url": "/api/ceremony/audio", "name": f.name, "kind": "file",
            "bytes": f.stat().st_size, "max_seconds": _music_cap(),
            # Every shipped track is somebody else's work, so every one of
            # them carries its credit. An upload is the user's own and needs
            # nothing. Getting this wrong in the other direction — showing a
            # CC BY track with no credit — is the failure that matters.
            "credit": (_credit_for(f) or
                       (CREDIT if "static" in str(f.parent) else "")),
            "candidate": next((c["id"] for c in CANDIDATES
                               if f.name == c["file"]), "")}


# ── the ceremony ─────────────────────────────────────────────────────────────

def script(*, now: Optional[datetime] = None, place: str = "",
           w: Optional[dict] = None, with_weather: bool = True) -> list[str]:
    """The lines, in order, as a list. Pure apart from reading the clock."""
    dt = now or datetime.now(timezone.utc).astimezone()
    n = _bump()
    part = part_of_day(dt)
    who = address()
    clock = dt.strftime("%H:%M").lstrip("0") or "0"

    out: list[str] = []
    if part == "late":
        # The line every good butler earns exactly once a night.
        out.append(f"Burning the midnight oil, {who}. I'm here.")
    else:
        label = PART_LABELS.get(part, "day")
        opener = _pick(OPENERS, n).format(part=label, Part=label.capitalize(),
                                          Clock=clock)
        out.append(opener.replace("sir", who).replace("Sir", who.capitalize()
                                                      if who.islower() else who))
    out.append(status_line(n))
    if with_weather:
        line = weather_line(w if w is not None else weather(place))
        if line:
            out.append(line)
    ask = _pick(SIGNOFFS, n).replace("sir", who)
    out.append(ask)
    return out


def bundle(*, place: str = "") -> dict:
    """Everything the ceremony needs to be SAID and HEARD, and nothing else.

    Split from opening the panels on purpose. A cold Chromium takes seconds to
    come up, and a greeting that arrives after the browser has finished booting
    is a greeting you have stopped waiting for. So this returns immediately and
    `open_panels` catches up afterwards.
    """
    if not enabled():
        return {"ok": False, "skipped": True,
                "reason": "the welcome ceremony is switched off"}
    lines = script(place=place)
    return {"ok": True, "lines": lines, "speech": " ".join(lines),
            "music": music(), "voice": VOICE, "faults": faults(),
            "at": time.time()}


def open_panels(panels: Optional[list] = None) -> dict:
    """Put the panels up. Separate, because it is slow and not essential."""
    opened: list[str] = []
    wake = {"ok": False, "skipped": "no panels requested"}
    if panels:
        try:
            from core import computer as C
            C.tool("start", bot="jarvis")
            for p in panels[:4]:
                r = C.goto(str(p), bot="jarvis")
                if r.get("ok"):
                    opened.append(str(p))
            wake = {"ok": bool(opened), "opened": opened}
        except Exception as e:
            wake = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    return {"ok": True, "opened": opened, "wake": wake}


def run(*, place: str = "", panels: Optional[list] = None) -> dict:
    """The whole ceremony: what to say, then what to open."""
    b = bundle(place=place)
    if not b.get("ok"):
        return b
    b.update(open_panels(panels))
    return b


def tool(action: str = "", *, panel: str = "") -> str:
    """The model-facing surface, matching every other tool here: prose back."""
    a = str(action or "").strip().lower()
    try:
        if a in ("", "status"):
            m = music()
            return (f"The welcome ceremony is {('on' if enabled() else 'off')}. "
                    f"Voice {VOICE}. Music: {m.get('name') or 'none (synth riser)'}"
                    f". City: {city() or 'not set'}. Addressed as "
                    f"'{address()}'. Clap to run it.")
        if a in ("run", "welcome", "ceremony"):
            panels = [p for p in (panel or "").split() if p.strip()]
            r = run(panels=panels)
            if not r.get("ok"):
                return "I did not run the ceremony: " + str(r.get("reason"))
            spoken = " ".join(r["lines"])
            opened = (f" I opened {len(r['opened'])} panel(s) on the computer."
                      if r.get("opened") else "")
            return spoken + opened
        if a in ("lines", "script"):
            return " | ".join(script())
        if a in ("weather", "forecast"):
            return weather_line(weather()) or "I have no city configured, sir."
        if a in ("faults", "diagnose"):
            bad = faults()
            return ("Nothing is wrong: " if not bad else "Not right: ") + \
                   (", ".join(bad) if bad else "every system answered")
        return ("Unknown action. Use status / run / lines / weather / faults.")
    except Exception as e:
        return f"ceremony: {type(e).__name__}: {e}"[:200]