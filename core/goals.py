"""core/goals.py — what JARVIS is actually working on.

The prompt already describes the character beautifully. It is wasted, because
a character needs something to have a *position* about. An assistant that
answers "what is the weather" in the film's voice and an assistant that has been
quietly trying to get the user three clients sound identical in any single reply
— but only one of them is JARVIS.

So: standing goals with a held position on each, a metric that moves, and a
next move that is already decided. That is the whole trick:

  * a **goal** is what the user is trying to do, in their words;
  * a **position** is JARVIS's own read on how it is going — written in the
    first person, updated when work happens, not derived on demand;
  * a **move** is what JARVIS intends to do next, so the morning is a plan
    rather than a report about a plan.

Continuity falls out of this for free. JARVIS does not "remember" Tuesday
because a summary mentions it — it remembers because it was *in the middle of
something* and the position still says so.

Two rules keep this honest:

  * **A position is never invented.** `position()` refuses text with no
    substance and the briefing never claims progress that has no evidence
    behind it. An assistant that says "we're making great progress" with nothing
    under it is worse than one that says "this is stuck and here is why".
  * **Goals are few.** Three, maybe four. A wall of goals is a to-do list, and a
    to-do list is what the user already has.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any, Optional

from core.data_paths import data_root

MAX_ACTIVE = 6
MAX_HISTORY = 60


def _file():
    return data_root() / "goals.json"


_lock = threading.RLock()
_cache: Optional[dict] = None


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
        d.setdefault("goals", [])
        d.setdefault("queue", [])       # things waiting on the user, batched
        d.setdefault("standups", [])
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
    global _cache
    with _lock:
        _cache = None


# ── words that make a position worthless ─────────────────────────────────────
EMPTY = re.compile(
    r"^\W*(going (fine|well|great|okay)|fine|good|great|excellent|perfect|"
    r"progress|on track|making progress|moving forward|under control|"
    r"no change|no update|nothing( new)?|n/?a|unknown|not sure|"
    r"i (think|guess|believe)|probably|maybe|soon|later|eventually|"
    r"working on it|we'll see|waiting to see|business as usual)\W*$", re.I)

def _substantive(text: str) -> bool:
    t = str(text or "").strip()
    if len(t) < 12:
        return False
    if EMPTY.match(t):
        return False
    # Length and the empty-phrase list is the whole test. An earlier version
    # also demanded a digit or a work-word, and it rejected real positions like
    # "no notes yet on competitor pricing" — which is information. The thing
    # worth catching is the valueless update, not ordinary English.
    return True


# ── the goals ────────────────────────────────────────────────────────────────

def set_goal(title: str, *, why: str = "", target: str = "",
             metric: str = "", start: float = 0.0, goal: float = 0.0,
             unit: str = "", deadline: str = "", position: str = "",
             move: str = "", next_move: str = "") -> dict:
    """Create or update a standing goal. `position` is optional at creation and
    can be left blank — an honest blank reads better than a fabricated one."""
    title = str(title or "").strip()[:120]
    if not title:
        raise ValueError("a goal needs a title")
    if position and not _substantive(position):
        raise ValueError("that position says nothing — say where it actually "
                         "stands, and why")
    with _lock:
        data = _load()
        row = next((g for g in data["goals"]
                    if g["title"].lower() == title.lower()), None)
        if row is None:
            if len([g for g in data["goals"]
                    if g.get("status") == "active"]) >= MAX_ACTIVE:
                raise ValueError(f"{MAX_ACTIVE} active goals is plenty — finish "
                                 f"or drop one before adding another")
            row = {"id": "g-" + uuid.uuid4().hex[:8], "created": time.time(),
                   "status": "active", "history": []}
            data["goals"].append(row)
        row["title"] = title
        if why:
            row["why"] = str(why)[:300]
        if target:
            row["target"] = str(target)[:300]
        if metric:
            row["metric"] = str(metric)[:60]
        if unit:
            row["unit"] = str(unit)[:20]
        if deadline:
            row["deadline"] = str(deadline)[:40]
        row["start"] = float(start or row.get("start") or 0.0)
        row["goal_value"] = float(goal if goal else row.get("goal_value") or 0.0)
        row.setdefault("start", start)
        row.setdefault("goal_value", goal)
        row.setdefault("history", [])
        row["status"] = "active"
        if position:
            _apply_position(row, position, kind="set")
        if move or next_move:
            row["next_move"] = str(move or next_move)[:200]
        row["updated"] = time.time()
        row["progress"] = _progress(row)
        _save()
        return dict(row)


def _find(goal_id_or_title: str) -> Optional[dict]:
    q = str(goal_id_or_title or "").strip().lower()
    for g in _load()["goals"]:
        if q and (q == g["id"].lower() or q in g["title"].lower()):
            return g
    return None


def get(ref: str) -> Optional[dict]:
    g = _find(ref)
    return dict(g) if g else None


def goals(active_only: bool = True) -> list[dict]:
    with _lock:
        rows = [dict(g) for g in _load()["goals"]]
    if active_only:
        rows = [g for g in rows if g.get("status") == "active"]
    rows.sort(key=lambda g: g.get("created", 0))
    for g in rows:
        g["progress"] = _progress(g)
    return rows


def _progress(g: dict) -> Optional[float]:
    """0.0 → 1.0 toward the goal value, or None when there is nothing to measure.
    A goal without a number is a real kind of goal, and pretending otherwise
    with a fake percentage is how a progress bar starts lying."""
    start, want = float(g.get("start") or 0), float(g.get("goal_value") or 0)
    now = float(g.get("value") or start)
    if want == start:
        return None
    pct = (now - start) / (want - start)
    return max(0.0, min(1.0, round(pct, 3)))


def advance(ref: str, value: float, note: str = "") -> dict:
    """Move the number. The note is the evidence — without it, do not pretend
    the number changed for a reason."""
    g = _find(ref)
    if g is None:
        raise ValueError(f"no goal called '{ref}'")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("that is not a number") from None
    with _lock:
        prev = float(g.get("value") or g.get("start") or 0)
        g["value"] = v
        g["updated"] = time.time()
        if note:
            g.setdefault("history", []).append(
                {"at": time.time(), "kind": "advance", "text": str(note)[:200],
                 "from": prev, "to": v})
        _save()
        return dict(g)


def position(ref: str, text: str, *, move: str = "") -> dict:
    """Set the read. This is the single most important call in the system: it is
    what makes the difference between an assistant and an operator."""
    g = _find(ref)
    if g is None:
        raise ValueError(f"no goal called '{ref}'")
    if not _substantive(text):
        raise ValueError("that position says nothing — say where it actually "
                         "stands, and why")
    with _lock:
        _apply_position(g, text, kind="position")
        if move:
            g["next_move"] = str(move)[:200]
        _save()
        return dict(g)


def _apply_position(g: dict, text: str, *, kind: str) -> None:
    text = str(text or "").strip()
    prev = g.get("position", "")
    if prev and prev == text:
        return
    g["position"] = text
    g["position_at"] = time.time()
    g.setdefault("history", []).append(
        {"at": time.time(), "kind": kind, "text": text, "was": prev})
    g["history"] = g["history"][-MAX_HISTORY:]


def note(ref: str, text: str) -> dict:
    g = _find(ref)
    if g is None:
        raise ValueError(f"no goal called '{ref}'")
    with _lock:
        g.setdefault("history", []).append(
            {"at": time.time(), "kind": "note", "text": str(text)[:300]})
        g["history"] = g["history"][-MAX_HISTORY:]
        g["updated"] = time.time()
        g["progress"] = _progress(g)
        _save()
        return dict(g)


def status(ref: str, value: str) -> dict:
    g = _find(ref)
    if g is None:
        raise ValueError(f"no goal called '{ref}'")
    with _lock:
        g["status"] = str(value or "").lower()
        if g["status"] not in ("active", "paused", "done", "dropped"):
            raise ValueError("status is active, paused, done or dropped")
        g["updated"] = time.time()
        if g["status"] in ("done", "dropped"):
            g["closed_at"] = time.time()
        _save()
        return dict(g)


def history(ref: str, limit: int = 12) -> list[dict]:
    g = _find(ref)
    if g is None:
        return []
    return list(reversed(g.get("history") or []))[:max(1, min(int(limit or 12), 60))]


# ── what the model needs to sound like it remembers ──────────────────────────

def prompt_block(budget: int = 900) -> str:
    """Injected into the system prompt. This is the continuity: the model does
    not have to remember, it is *told where things stand* every single turn."""
    rows = goals()
    if not rows:
        return ""
    out = ["[WHAT YOU ARE WORKING ON]",
           "Standing goals. You hold a position on each — it is yours, and it "
           "is already written down. Do not re-derive it, and do not describe "
           "these as tasks the user gave you to do right now.", ""]
    for g in rows[:4]:
        bits = [f"- {g['title']}"]
        if g.get("why"):
            bits.append(f"  why: {g['why']}")
        if g.get("position"):
            bits.append(f"  where it stands: {g['position']}")
        if g.get("next_move"):
            bits.append(f"  your next move: {g['next_move']}")
        prog = g.get("progress")
        if prog is not None and g.get("metric"):
            now = g.get("value", g.get("start", 0))
            bits.append(f"  {g['metric']}: {now:g}"
                        + (f" of {g['goal_value']:g}" if g.get("goal_value") else "")
                        + f" {g.get('unit', '')}".rstrip())
        if g.get("deadline"):
            bits.append(f"  by: {g['deadline']}")
        out.append("\n".join(bits))
    out += ["",
            "Speak about these the way an operator does: the position, the next "
            "move, what is blocking it. Never recite the list back unless asked "
            "for it."]
    return "\n".join(out)[:budget]


# ── the standup ──────────────────────────────────────────────────────────────

def standup(*, force: bool = False) -> str:
    """Not a data dump. What moved, where each thing stands, what happens next.

    The shape is deliberate: state first, then the move. A briefing that leads
    with metrics is a dashboard. A briefing that leads with a position is an
    operator.
    """
    with _lock:
        last = float((_load().get("standups") or [{}])[0].get("at", 0)) \
            if _load().get("standups") else 0.0
    if not force and time.time() - last < 6 * 3600:
        return ""
    rows = goals()
    if not rows:
        return ("No standing goals yet. Say what you're trying to do and I'll "
                "keep a position on it.")
    lines = []
    for g in rows:
        pos = g.get("position") or "no position yet — I have not looked"
        move = g.get("next_move") or ""
        fresh = [h for h in (g.get("history") or [])
                 if h.get("at", 0) > last and h.get("kind") in ("advance", "note")]
        pos = pos.rstrip(". ")
        line = f"**{g['title']}** — {pos}"
        if fresh:
            line += f" ({' '.join(h['text'] for h in fresh[:2])[:120]})"
        if move:
            line += f" Next: {move.rstrip('. ')}."
        lines.append(line)
    queued = _load().get("queue") or []
    if queued:
        lines.append("Waiting on you: " + ", ".join(
            q.get("summary", q.get("kind", "?"))[:60] for q in queued[:4]) + ".")
    text = "\n\n".join(lines)
    with _lock:
        data = _load()
        data.setdefault("standups", []).insert(0, {"at": time.time(),
                                                   "text": text[:1500]})
        data["standups"] = data["standups"][-30:]
        _save()
    return text


# ── the approval queue: one interruption, not ten ────────────────────────────

def queue(kind: str, summary: str, *, detail: str = "", ref: str = "") -> dict:
    """Something that needs the user — held, and surfaced in one go, so acting
    freely does not turn into thirty interruptions."""
    with _lock:
        data = _load()
        if any(q.get("ref") == ref and q.get("kind") == kind
               for q in data["queue"] if ref):
            raise ValueError("that is already waiting")
        rec = {"id": "q-" + uuid.uuid4().hex[:8], "kind": str(kind)[:40],
               "summary": str(summary)[:200], "detail": str(detail)[:600],
               "ref": str(ref or "")[:120], "at": time.time()}
        data["queue"].append(rec)
        data["queue"] = data["queue"][-40:]
        _save()
        return rec


def pending() -> list[dict]:
    with _lock:
        return list(reversed(_load().get("queue") or []))


def clear(ref: str = "") -> int:
    with _lock:
        data = _load()
        if ref:
            before = len(data["queue"])
            data["queue"] = [q for q in data["queue"] if q.get("ref") != ref]
        else:
            before = len(data["queue"])
            data["queue"] = []
        _save()
        return before - len(data["queue"])


def stats() -> dict:
    with _lock:
        data = _load()
    rows = data["goals"]
    return {
        "goals": len(rows),
        "active": sum(1 for g in rows if g.get("status") == "active"),
        "with_position": sum(1 for g in rows if g.get("position")),
        "done": sum(1 for g in rows if g.get("status") == "done"),
        "waiting_on_you": len(data.get("queue") or []),
        "standups": len(data.get("standups") or []),
    }


def describe() -> str:
    s = stats()
    rows = goals()
    if not rows:
        return "No standing goals. Tell me what you are trying to do."
    return (f"{s['active']} active goal(s): "
            + "; ".join(g["title"] for g in rows[:4])
            + (f". {s['waiting_on_you']} waiting on you."
               if s["waiting_on_you"] else "."))


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", ref: str = "", **kw: Any) -> str:
    """One entry point for the model. Prose back, because the assistant reads it
    out loud or pastes it into the panel — a JSON blob spoken to a human is a
    bug, not a feature."""
    a = str(action or "").strip().lower()
    try:
        if a in ("", "list", "status"):
            rows = goals()
            if not rows:
                return ("No standing goals. Ask the user what they are trying "
                        "to do, then set one.")
            out = [describe()]
            for g in rows:
                out.append(f"- {g['title']}: {g.get('position') or 'no position yet'}"
                           + (f" · next: {g['next_move']}" if g.get("next_move") else "")
                           + (f" · {g.get('progress', 0) * 100:.0f}%"
                              if g.get("progress") is not None else ""))
            q = pending()
            if q:
                out.append("Waiting on the user: "
                           + "; ".join(x["summary"] for x in q[:4]))
            return "\n".join(out)

        if a in ("set", "add", "new", "goal"):
            g = set_goal(str(kw.get("title") or ref or ""),
                         why=str(kw.get("why") or ""),
                         target=str(kw.get("target") or ""),
                         metric=str(kw.get("metric") or ""),
                         start=float(kw.get("start") or 0),
                         goal=float(kw.get("goal") or 0),
                         unit=str(kw.get("unit") or ""),
                         deadline=str(kw.get("deadline") or ""),
                         position=str(kw.get("position") or ""),
                         move=str(kw.get("next_move") or ""))
            return f"{g['title']} is now a standing goal."

        if a in ("position", "update"):
            g = position(ref or str(kw.get("title") or ""),
                         str(kw.get("text") or kw.get("position") or ""),
                         move=str(kw.get("next_move") or ""))
            return f"Position on {g['title']} updated."

        if a in ("advance", "move", "count"):
            g = advance(ref or str(kw.get("title") or ""),
                        kw.get("value", 0), str(kw.get("note") or ""))
            pct = g.get("progress")
            return (f"{g['title']}: {g.get('metric', 'value')} = "
                    f"{g.get('value')}"
                    + (f" ({pct * 100:.0f}% of the way)" if pct is not None else ""))

        if a in ("note", "add_note"):
            note(ref or str(kw.get("title") or ""), str(kw.get("text") or ""))
            return "Noted."

        if a in ("standup", "briefing", "how"):
            return standup(force=True) or standup()

        if a in ("act", "director", "work"):
            from core import director as D
            r = D.tick(dry_run=bool(kw.get("dry_run")))
            if not r.get("did"):
                return r.get("why") or r.get("dry_run") and "Dry run only."
            return f"{r['move']} on {r['goal']}: {r.get('detail', '')}"

        if a in ("close", "done", "drop", "pause"):
            g = status(ref or str(kw.get("title") or ""), a if a in ("done", "drop") else "paused")
            return f"{g['title']} is {g['status']}."

        if a in ("waiting", "queue"):
            pending()
            return f"{len(pending())} thing(s) waiting on the user."

        if a in ("clear", "ack"):
            n = clear(ref)
            return f"Cleared {n}."

        return "Unknown action. Use list / set / position / advance / note / standup / act / close."
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:160]
