"""core/director.py — the thing that makes JARVIS do something while you sleep.

This is the initiative gap, and it is a scheduling problem more than an
intelligence one. JARVIS already has agents, a lead engine, a display surface, a
calendar and a journal. What it lacks is a reason to *pick* any of them at 6am
without being asked.

So the director is deliberately small and deliberately boring:

  * read the goals;
  * pick the one furthest from done that has a next move;
  * do **one** concrete thing about it;
  * write down what happened, and update the position.

One thing, not a campaign. A background agent that tries to be useful for nine
hours is a machine that emails people. One move per run, with a position
written afterwards, is how you get an assistant that is *ahead of you* rather
than merely fast.

The moves are all reversible and internal by construction — research, building,
drafting, agent shifts, reminders. Nothing here can send, pay, delete or
unlock. Anything outward-facing goes on `goals.queue()` and waits for the user
in one batch, which is the whole "act freely, report later" bargain.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from core import goals as G

#: cooldown per goal, so one stubborn goal cannot monopolise the morning
COOLDOWN = 45 * 60

MOVES = ("research", "build", "draft", "shift", "remind", "review")


def _last_touched(g: dict) -> float:
    return max(float(g.get("updated") or g.get("created") or 0),
               float(g.get("position_at") or 0))


def pick(goals: Optional[list] = None) -> Optional[dict]:
    """Which goal deserves attention right now.

    Ranked by distance to done, with a cooldown so the same goal is not worked
    twice in a row, and a nudge toward goals that have never been touched —
    an untouched goal is the one that quietly dies.
    """
    rows = goals if goals is not None else G.goals()
    if not rows:
        return None
    now = time.time()
    best, best_score = None, -1e9
    for g in rows:
        cool = now - _last_touched(g)
        if cool < COOLDOWN:
            continue
        prog = g.get("progress")
        # no metric means we cannot judge distance, so treat it as mid-way
        dist = 1.0 - float(prog if prog is not None else 0.5)
        never = 0.0 if g.get("position") else 0.35
        deadline_soon = 0.0
        if g.get("deadline"):
            deadline_soon = 0.25 if _near_deadline(g["deadline"]) else 0.0
        score = dist + never + deadline_soon
        if score > best_score:
            best, best_score = g, score
    if best is None:                      # everything is on cooldown
        cooled = [(now - _last_touched(g), g) for g in rows]
        cooled.sort(reverse=True)
        return cooled[0][1] if cooled else None
    return best


def _near_deadline(spec: str, within_days: int = 14) -> bool:
    from datetime import datetime, timedelta
    t = str(spec or "").strip()
    for fmt in ("%Y-%m-%d", "%d %B %Y", "%B %d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(t, fmt).date()
            return (d - datetime.now().date()) <= timedelta(days=within_days)
        except ValueError:
            continue
    return False


def _next_action(g: dict) -> tuple[str, str]:
    """Which move, and the argument for it. Chosen from the goal's own words
    where possible, so the reason it acted is legible afterwards."""
    title = g["title"]
    why = g.get("why") or ""
    if any(w in (title + " " + why).lower()
           for w in ("client", "lead", "sales", "pipeline", "revenue", "business")):
        return "shift", "pipeline"
    if any(w in (title + " " + why).lower()
           for w in ("site", "web", "app", "build", "ship", "code", "product")):
        return "build", "build"
    if any(w in (title + " " + why).lower()
           for w in ("write", "content", "ads", "copy", "blog", "brand")):
        return "draft", "copy"
    if any(w in (title + " " + why).lower()
           for w in ("learn", "research", "read", "study", "understand")):
        return "research", "research"
    return "review", "review"


def tick(*, dry_run: bool = False, now: float | None = None) -> dict:
    """One move. Returns what it did, so a scheduler can report it and a test
    can assert on it without a model."""
    now = now or time.time()
    goal = pick()
    if goal is None:
        return {"did": False, "why": "no standing goals to work on"}
    move, kind = _next_action(goal)
    plan = {"goal": goal["title"], "goal_id": goal["id"], "move": move,
            "next_move": goal.get("next_move", ""), "at": now}
    if dry_run:
        return {"did": False, "dry_run": True, **plan}

    result: dict[str, Any] = {"ok": True, "detail": ""}
    try:
        result = _run(move, goal)
    except Exception as e:                      # never take the scheduler down
        result = {"ok": False, "detail": f"{type(e).__name__}: {e}"[:160]}

    G.note(goal["id"], f"Director: {move} — {result.get('detail', '')[:120]}")
    G.position(
        goal["id"],
        f"{goal.get('position', 'No position yet')} "
        f"Latest: {move} — {result.get('detail', 'no change')[:110]}".strip(),
        move=goal.get("next_move", ""))
    if result.get("waited_on_user"):
        G.queue("review", result.get("detail", "")[:120], ref=goal["id"])
    return {"did": True, "move": move, "goal": goal["title"],
            "ok": result.get("ok", True), "detail": result.get("detail", "")}


def _run(move: str, goal: dict) -> dict:
    """Each move is a real call into a subsystem that already exists. None of
    them can reach a human — that is enforced here, not hoped for."""
    title = goal["title"]
    if move == "shift":
        from core import agents as A
        A.seed()
        pick_agent = _agent_for(goal)
        if not pick_agent:
            return {"ok": True, "detail": "no agent suits that goal yet"}
        order = A.shift(pick_agent)
        return {"ok": True, "detail": f"{pick_agent} shift: {len(order)} chars of work order"}

    if move == "build":
        from core import display as D
        html = (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>{title}</title></head><body style=\"background:#00060a;"
                f"color:#e6f7ff;font:16px system-ui;padding:40px\">"
                f"<h1 style=\"color:#00d4ff\">{title}</h1>"
                f"<p>Drafted by JARVIS at {time.strftime('%H:%M')}.</p>"
                f"</body></html>")
        art = D.show(html=html, title=f"{title} — draft", kind="html",
                     pin=False)
        return {"ok": True,
                "detail": f"draft page on the display ({art.get('id', '?')})"}

    if move == "draft":
        # Draft into memory rather than out to a person. Writing is free;
        # sending is a decision, and that decision is not the director's.
        from core import knowledge as K
        K.ingest(f"draft — {goal['title']}",
                 f"Opening note for: {goal['title']}.\n"
                 f"Why it matters: {goal.get('why', 'not recorded')}\n"
                 f"Where it stands: {goal.get('position', 'not recorded')}",
                 source="draft")
        return {"ok": True, "detail": "opened a draft in memory, nothing sent"}

    if move == "research":
        from core import knowledge as K
        hits = K.search(title, k=3)
        return {"ok": True,
                "detail": f"{len(hits)} related note(s) already in memory"}

    if move == "review":
        from core import journal as J
        recent = J.recent(2, kind="error", limit=3)
        if recent:
            return {"ok": True,
                    "detail": f"{len(recent)} thing(s) went wrong recently: "
                              + recent[0]["title"][:70],
                    "waited_on_user": False}
        return {"ok": True, "detail": "nothing broken in the last two days"}

    return {"ok": True, "detail": "no move for that"}


#: goal words -> the agent that actually does that job. Matching on the agent's
#: own name was wrong (a "clients" goal matched nobody) and matching on role
#: alone was worse (every "pipeline" goal became the same pair of marketers).
_ROLES = (
    (("client", "lead", "sales", "pipeline", "revenue", "business", "invoice"),
     ("Lead Scout", "Sales")),
    (("site", "web", "app", "build", "ship", "code", "product", "sample"),
     ("Builder", "Coder")),
    (("write", "content", "ads", "copy", "blog", "brand", "proposal"),
     ("Copywriter", "Marketing")),
    (("learn", "research", "read", "study", "understand", "market"),
     ("Researcher", "Research")),
    (("report", "brief", "ops", "standup", "status", "summary"),
     ("Reporter", "Operations")),
)


def _agent_for(goal: dict) -> str:
    """The right pair of hands for this goal, or nobody."""
    try:
        from core import agents as A
        A.seed()
        title = goal["title"].lower()
        why = (goal.get("why") or "").lower()
        # Score, do not first-match. "Ship the India client site" contains
        # "client" as well as "site", and a first-match loop sent every goal to
        # the same pair of hands. The title counts double: that is where the
        # actual subject is.
        best, best_score = "", 0.0
        for words, names in _ROLES:
            score = sum(3.0 for w in words if w in title) \
                  + sum(1.0 for w in words if w in why)
            if score <= best_score:
                continue
            for want in names:
                hit = A.find(want)
                if hit and hit.get("enabled"):
                    best, best_score = hit["name"], score
                    break
        if best:
            return best
        for a in A.roster():
            if a.get("enabled") and a.get("tools"):
                return a["name"]
    except Exception:
        pass
    return ""


def report() -> str:
    """What the director has been doing, for the standup. Reads the goal
    history, which is where it actually records itself — the journal is for
    things that happened, not for what JARVIS decided to do about them."""
    rows = []
    for g in G.goals():
        for h in g.get("history") or []:
            if str(h.get("text", "")).startswith("Director:"):
                rows.append((float(h.get("at") or 0),
                             str(h["text"]).replace("Director: ", "")[:90]))
    if not rows:
        return "The director has not moved yet."
    rows.sort(reverse=True)
    return " · ".join(t for _, t in rows[:3])
