"""core/journal.py — the episodic half of memory.

`knowledge.py` answers "what did I tell you about X?". This answers "what
happened, and when". They are different problems and they fail differently:

  * knowledge is a bag of documents. It has no opinion about time. "What did we
    decide about the second invoice?" is a knowledge question.
  * the journal is a dated, structured record: decisions, things shipped, money,
    approvals, what an agent did and whether it worked. "What happened last
    Tuesday, and did the ads actually land?" is a journal question.

Why a separate store instead of more tables in the knowledge DB: the journal has
to be readable by a human with `cat`, diffable, and append-only in spirit. A
file per day means losing the database is not losing the memory, and a bad
migration cannot eat a year of history.

The important design decision is `record_event`. Every subsystem that does
something worth remembering calls it, so the journal fills itself in:

    * an agent shift ran, and whether it succeeded
    * an invoice was sent, and whether it got paid
    * a lead was approved and emailed
    * a scheduled job fired
    * the user made a decision, via the `journal` tool

That means the "deeper memory" is not a feature you switch on. It is a
by-product of the system already running.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

KINDS = ("decision", "done", "note", "metric", "lesson", "event", "money", "error")

_lock = threading.RLock()
_cache: dict[str, Any] = {}


def _dir() -> Path:
    return data_root() / "journal"


def _index_path() -> Path:
    return _dir() / "index.json"


def _day_path(day: str) -> Path:
    return _dir() / f"{day}.json"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_index() -> dict:
    with _lock:
        if "index" in _cache:
            return _cache["index"]
        try:
            idx = json.loads(_index_path().read_text(encoding="utf-8"))
            if not isinstance(idx, dict):
                idx = {}
        except Exception:
            idx = {}
        idx.setdefault("days", [])
        idx.setdefault("seq", 0)
        _cache["index"] = idx
        return idx


def _save_index(idx: dict) -> None:
    with _lock:
        p = _index_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        _cache["index"] = idx


def _load_day(day: str) -> dict:
    try:
        d = json.loads(_day_path(day).read_text(encoding="utf-8"))
        if isinstance(d, list):
            return {"day": day, "entries": d}
        if not isinstance(d, dict):
            return {"day": day, "entries": []}
        d.setdefault("entries", [])
        return d
    except Exception:
        return {"day": day, "entries": []}


def _save_day(day: str, data: dict) -> None:
    with _lock:
        p = _day_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        idx = _load_index()
        if day not in idx["days"]:
            idx["days"].append(day)
            idx["days"].sort()
        _save_index(idx)


def reset() -> None:
    global _cache
    with _lock:
        _cache = {}


# ── writing ──────────────────────────────────────────────────────────────────

def entry(kind: str, title: str, body: str = "", *, tags: Any = None,
          day: str = "", refs: Any = None, actor: str = "JARVIS") -> dict:
    """Write one entry. Day is the local date, so 'this morning' means the same
    thing to the user as it does to the file name on disk."""
    kind = str(kind or "note").strip().lower()
    if kind not in KINDS:
        kind = "note"
    title = str(title or "").strip()[:200]
    if not title:
        raise ValueError("a journal entry needs a title")
    day = day or _today()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        raise ValueError(f"'{day}' is not a date — use YYYY-MM-DD")
    with _lock:
        data = _load_day(day)
        idx = _load_index()
        idx["seq"] = int(idx.get("seq") or 0) + 1
        rec = {
            "id": f"j{idx['seq']:05d}",
            "kind": kind,
            "title": title,
            "body": str(body or "")[:4000],
            "tags": sorted({str(t).strip().lower()[:32]
                            for t in (tags or []) if str(t).strip()}),
            "refs": [str(r)[:120] for r in (refs or [])][:8],
            "actor": str(actor or "JARVIS")[:40],
            "at": time.time(),
        }
        data["entries"].append(rec)
        _save_day(day, data)
        return dict(rec)


def record_event(kind: str, title: str, *, body: str = "", tags: Any = None,
                 refs: Any = None, ok: bool = True, actor: str = "JARVIS") -> dict:
    """The hook every subsystem calls. Success lands under the kind; a failure
    is recorded as an error *and* keeps the kind, because 'we tried to invoice
    and it failed' is more useful later than a clean list of what worked."""
    if not ok:
        return entry("error", title, body or "failed", tags=tags, refs=refs,
                     actor=actor)
    return entry(kind, title, body, tags=tags, refs=refs, actor=actor)


# ── reading ──────────────────────────────────────────────────────────────────

def day_entries(day: str = "", *, kind: str = "") -> list[dict]:
    day = day or _today()
    rows = [dict(e) for e in _load_day(day)["entries"]]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    rows.sort(key=lambda r: r.get("at", 0))
    return rows


def recent(days: int = 7, *, kind: str = "", limit: int = 200) -> list[dict]:
    idx = _load_index()
    since = (datetime.now() - timedelta(days=max(1, int(days or 1)) - 1)).strftime("%Y-%m-%d")
    have = [d for d in idx["days"] if d >= since]
    out: list[dict] = []
    for d in sorted(have, reverse=True):
        for e in day_entries(d, kind=kind):
            out.append(e)
    out.sort(key=lambda r: r.get("at", 0), reverse=True)
    return out[:max(1, min(int(limit or 200), 1000))]


def search(query: str, *, kinds: Any = None, days: int = 60,
           limit: int = 20) -> list[dict]:
    """Lexical, because a journal is small and the user is typing words they
    remember. Tag and title hits outrank body hits."""
    q = str(query or "").strip().lower()
    if not q:
        return []
    want = {str(k).lower() for k in (kinds or [])}
    terms = [t for t in re.split(r"\W+", q) if len(t) > 2]
    scored = []
    for e in recent(days, limit=1000):
        if want and e.get("kind") not in want:
            continue
        title = str(e.get("title", "")).lower()
        body = str(e.get("body", "")).lower()
        tags = " ".join(e.get("tags") or [])
        score = 0
        if q in title:
            score += 6
        if q in tags:
            score += 4
        for t in terms:
            if t in title:
                score += 3
            if t in tags:
                score += 2
            if t in body:
                score += 1
        if score:
            scored.append((score, e))
    scored.sort(key=lambda x: (x[0], x[1].get("at", 0)), reverse=True)
    return [e for _, e in scored[:max(1, min(int(limit or 20), 100))]]


def decisions(limit: int = 20) -> list[dict]:
    """Decisions are the entries worth re-reading in six months."""
    rows = recent(3650, kind="decision", limit=500)
    return rows[:max(1, min(int(limit or 20), 200))]


def timeline(days: int = 14) -> list[dict]:
    """Day buckets, newest first, each with its counts. This is what the panel
    and the digest both read, so they can never disagree."""
    want = max(1, min(int(days or 14), 365))
    idx = _load_index()
    out = []
    for d in sorted(idx["days"], reverse=True)[:want]:
        es = day_entries(d)
        counts: dict[str, int] = {}
        for e in es:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        out.append({"day": d, "count": len(es), "kinds": counts,
                    "headline": es[-1]["title"][:90] if es else ""})
    return out


def stats() -> dict:
    idx = _load_index()
    all_rows = recent(3650, limit=5000)
    kinds: dict[str, int] = {}
    for e in all_rows:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    return {"days": len(idx["days"]), "entries": len(all_rows), "kinds": kinds,
            "first_day": (idx["days"][0] if idx["days"] else ""),
            "last_day": (idx["days"][-1] if idx["days"] else "")}


# ── the part the briefing eats ───────────────────────────────────────────────

def digest(day: str = "", *, max_chars: int = 1400) -> str:
    """One plain paragraph a model can narrate, or a human can read at a glance.
    Deliberately boring: counts, money, decisions, and what broke. If the day was
    empty, say so in six words instead of padding."""
    day = day or _today()
    es = day_entries(day)
    if not es:
        return f"Nothing recorded for {day}."
    by: dict[str, list[dict]] = {}
    for e in es:
        by.setdefault(e["kind"], []).append(e)
    parts = [f"{day}:"]
    order = ("money", "decision", "done", "lesson", "error", "metric",
             "event", "note")
    names = {"money": "money", "decision": "decided", "done": "shipped",
             "lesson": "learned", "error": "went wrong", "metric": "numbers",
             "event": "happened", "note": "noted"}
    for k in order:
        rows = by.get(k)
        if not rows:
            continue
        bits = [r["title"] for r in rows[:4]]
        extra = f" (+{len(rows) - 4} more)" if len(rows) > 4 else ""
        parts.append(f"{len(rows)} {names.get(k, k)}{extra}: "
                     + "; ".join(bits))
    out = ". ".join(parts) + "."
    return out[:max_chars]


def recall(query: str, *, k: int = 5, days: int = 90) -> str:
    """A block for the system prompt: the journal side of memory. Kept separate
    from `knowledge.recall_block` so the prompt can weight them differently —
    a decision from March is worth more than a passing mention in a document."""
    rows = search(query, days=days, limit=max(1, k))
    if not rows:
        return ""
    lines = []
    for e in rows:
        stamp = datetime.fromtimestamp(e["at"]).strftime("%Y-%m-%d")
        line = f"- [{stamp} · {e['kind']}] {e['title']}"
        if e.get("body"):
            line += f" — {str(e['body'])[:180]}"
        lines.append(line)
    return "From your journal:\n" + "\n".join(lines)


# ── hooking the rest of the system in ───────────────────────────────────────

def wire() -> int:
    """Call once at startup. Subscribes the journal to the things that already
    happen, so the memory fills itself in without anyone remembering to."""
    from core import journal_hooks
    return journal_hooks.wire()
