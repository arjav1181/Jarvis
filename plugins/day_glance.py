"""
Your day, in one breath.

Four things, in the order a person actually wants them: what time it is and
whether to go outside, what is on the calendar next, what you said you would
do, and how long is left on whatever you are timing.

    action=glance:  all of it. The default.
    action=next:    just the next thing, for when you are already late.
    action=add:     put something on the list. (Asks — it writes.)
    action=done:    tick something off.
    action=list:    the list on its own.
    action=focus:   start, stop or check a timer.

Calendar comes from an iCal feed if one is configured — no OAuth, no Google
login, just a secret URL. Without one it says so rather than pretending your
day is empty, because "nothing scheduled" and "I cannot see your calendar" are
very different sentences.

The todo list is a plain text file in the data directory, one item a line,
`- [ ]` for open and `- [x]` for done. Deliberately dull: it is a file you can
read, edit, and delete without this project existing.

Read-only except add/done, which ask.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_ITEMS = 25


def _todos_path() -> Path:
    from core.data_paths import data_root
    d = data_root() / "day"
    d.mkdir(parents=True, exist_ok=True)
    return d / "todos.txt"


def _state_path() -> Path:
    from core.data_paths import data_root
    d = data_root() / "day"
    d.mkdir(parents=True, exist_ok=True)
    return d / "focus.json"


def _ics_url() -> str:
    import os
    return str(os.environ.get("JARVIS_CALENDAR_ICS")
               or os.environ.get("JARVIS_ICAL_URL") or "").strip()


# ── todos ────────────────────────────────────────────────────────────────────

def _read_todos() -> tuple[list[str], list[str]]:
    p = _todos_path()
    if not p.is_file():
        return [], []
    open_, done = [], []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if re.match(r"^-\s*\[[xX]\]", s):
            done.append(re.sub(r"^-\s*\[[xX]\]\s*", "", s))
        elif re.match(r"^-\s*\[ \]", s):
            open_.append(re.sub(r"^-\s*\[ \]\s*", "", s))
        else:
            open_.append(s.lstrip("- ").strip())
    return open_, done


def _write_todos(open_: list[str], done: list[str]) -> None:
    lines = [f"# JARVIS todos — plain text, edit freely", ""]
    lines += [f"- [ ] {t}" for t in open_]
    if open_ and done:
        lines.append("")
    lines += [f"- [x] {t}" for t in done]
    _todos_path().write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── calendar ─────────────────────────────────────────────────────────────────

def _calendar_events(hours: int = 24) -> tuple[list[dict], str]:
    """(events, note). `note` is set when the calendar could not be read, so
    the caller can say so rather than reporting an empty day."""
    url = _ics_url()
    if not url:
        return [], "no calendar feed configured (set JARVIS_CALENDAR_ICS)"
    try:
        import httpx
        r = httpx.get(url, timeout=12.0, follow_redirects=True)
        if r.status_code != 200:
            return [], f"the calendar feed said {r.status_code}"
        text = r.text
    except Exception as e:
        return [], f"could not reach the calendar: {type(e).__name__}"

    # Unfold RFC 5545 line continuations, then pull the fields we need. A full
    # iCal parser is a dependency; this reads the 90% that is useful.
    text = re.sub(r"\r?\n[ \t]", "", text)
    now = datetime.now(timezone.utc)
    until = now + timedelta(hours=hours)
    out: list[dict] = []
    for block in re.split(r"BEGIN:VEVENT", text)[1:]:
        block = block.split("END:VEVENT")[0]

        def field(name: str) -> str:
            m = re.search(rf"^{name}[^:]*:(.*)$", block, re.M)
            return (m.group(1).strip() if m else "")

        summary = field("SUMMARY") or "(no title)"
        start = field("DTSTART")
        stamp = None
        if start:
            raw = re.sub(r"[^0-9TZ]", "", start)[:15]
            for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
                try:
                    naive = datetime.strptime(raw[:8] + (raw[9:15] or ""),
                                              fmt if fmt != "%Y%m%d" else "%Y%m%d")
                    stamp = naive.replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    continue
        if stamp is None:
            continue
        if not (now - timedelta(hours=2) <= stamp <= until):
            continue
        out.append({"when": stamp, "what": summary,
                    "where": field("LOCATION")})
    out.sort(key=lambda e: e["when"])
    return out, ""


# ── focus timer ─────────────────────────────────────────────────────────────

def _focus() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmt_delta(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "glance").strip().lower()
    item = str(parameters.get("item") or parameters.get("what") or "").strip()
    minutes = parameters.get("minutes")
    try:
        return _run(action, item, minutes)
    except Exception as e:
        return f"My day glance failed: {type(e).__name__}: {e}"[:200]


def _run(action: str, item: str, minutes) -> str:
    open_, done = _read_todos()

    if action in ("add", "todo", "new"):
        if not item:
            return "What should I add?"
        if any(t.lower() == item.lower() for t in open_):
            return f"'{item}' is already on the list."
        open_.append(item)
        _write_todos(open_, done)
        return f"Added. That is {len(open_)} thing(s) open."

    if action in ("done", "tick", "finish"):
        if not item:
            return "Which one did you finish? Give me the item."
        hit = next((t for t in open_ if t.lower() == item.lower()), None) \
            or next((t for t in open_ if item.lower() in t.lower()), None)
        if hit is None:
            return f"I cannot find '{item}' on the list. Open: " + \
                   (", ".join(open_[:6]) or "nothing")
        open_.remove(hit)
        done.append(hit)
        _write_todos(open_, done)
        return f"Done: {hit}. {len(open_)} still open."

    if action == "list":
        if not open_:
            return f"Nothing open. {len(done)} finished so far."
        lines = "\n".join(f"- {t}" for t in open_[:MAX_ITEMS])
        more = f"\n- ...and {len(open_) - MAX_ITEMS} more" if len(open_) > MAX_ITEMS else ""
        return f"{len(open_)} open:\n{lines}{more}"

    if action == "focus":
        st = _focus()
        if item and str(item).lower() in ("stop", "off", "cancel"):
            _state_path().unlink(missing_ok=True)
            return "Timer stopped."
        if minutes:
            mins = int(float(minutes))
            st = {"what": item or "focus", "until": time.time() + mins * 60,
                  "started": time.time()}
            _state_path().write_text(json.dumps(st), encoding="utf-8")
            return f"Timing {mins} minute(s) on {st['what']}."
        if st.get("until"):
            left = float(st["until"]) - time.time()
            if left > 0:
                return (f"{_fmt_delta(left)} left on {st.get('what', 'it')}. "
                        f"{len(open_)} thing(s) open.")
            _state_path().unlink(missing_ok=True)
            return (f"{st.get('what', 'That')} is done — {_fmt_delta(0)} over. "
                    f"{len(open_)} thing(s) open.")
        return f"No timer running. {len(open_)} thing(s) open."

    # the default: the whole picture, in the order it is wanted
    now = datetime.now().astimezone()
    part = ("early morning" if now.hour < 8 else
            "morning" if now.hour < 12 else
            "afternoon" if now.hour < 17 else
            "evening" if now.hour < 22 else "late")
    lines = [f"{now.strftime('%A %d %B, %H:%M')} — {part}."]

    events, note = _calendar_events()
    if events:
        lines.append("Next up:")
        for e in events[:4]:
            lines.append(f"- {e['when'].astimezone().strftime('%H:%M')} "
                         f"{e['what']}" + (f" ({e['where']})" if e["where"] else ""))
    else:
        # The distinction that matters: empty, versus cannot see.
        lines.append("Calendar: " + ("nothing in the next 24 hours."
                                     if note.startswith("no calendar")
                                     else f"could not be read ({note})."))

    if open_:
        lines.append(f"On the list ({len(open_)}): " +
                     "; ".join(open_[:3]) +
                     (f"; and {len(open_) - 3} more" if len(open_) > 3 else ""))
    else:
        lines.append("Your list is empty.")

    st = _focus()
    if st.get("until"):
        left = float(st["until"]) - time.time()
        if left > 0:
            lines.append(f"Timer: {_fmt_delta(left)} left on "
                         f"{st.get('what', 'it')}.")
    lines.append("Say add plus something to put it on the list, or start a timer.")
    return "\n".join(lines)


PLUGIN = {
    "name": "day_glance",
    "description": (
        "The user's day in one answer: time, calendar next up, the todo list, "
        "and any running focus timer. Use this whenever they ask what is on "
        "today, what should I be doing, what is next, or how long is left. "
        "Also the only tool that manages the todo list and the timer."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "glance | next | list | add | done | focus"},
            "item": {"type": "STRING",
                     "description": "what to add, or tick off, or focus on"},
            "minutes": {"type": "INTEGER",
                         "description": "for focus: how long to run"},
        },
        "required": [],
    },
}
