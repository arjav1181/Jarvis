"""core/crew.py — messaging a bot, and it acting as itself.

THE GAP THIS CLOSES
    `core/agents.py` can hire a bot and `core/skills.py` can run a method, but
    there was no way to *talk to* one. The assistant has its own conversation;
    a bot is a record with a persona string. So "give this to the Ads Manager"
    had nowhere to go, and every bot behaved like a slightly different tone of
    the same assistant.

    A bot now has its own transcript, its own voice, and its own skills. You
    message a bot, it answers as that bot, and if the message is work rather
    than conversation it runs the skill that owns that work.

THE ONE THING THAT MAKES A BOT A BOT
    Its own history. Without a transcript, every message re-derives everything
    from the persona, which is the "helpful assistant with a costume" failure.
    With one, turn nine knows what turn two decided, and the bot can be told
    "actually, do it like last time" and mean something.

    Transcripts are bounded and per-bot. A bot that has been running for a week
    must not blow the context window on its own conversation, so the tail is
    what gets sent and the summary carries the rest.

ROUTING: CONVERSATION OR WORK
    Grok Bot's rule, and the right one: message a bot and it works, unless the
    message is clearly just talking. Guessing wrong in either direction is bad
    — running a coder loop for "hello" burns a model call, and answering a
    real task in prose does nothing. So it asks the model which, cheaply, and
    only proceeds to a skill when it is work.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from core import agents as _ag
from core import gemini
from core import skills as _sk

MAX_TAIL = 24          # messages carried into the prompt
MAX_CHARS = 1_400     # per user message, so a pasted log cannot flood it


def _dir() -> Path:
    from core.data_paths import data_root
    d = data_root() / "bot_chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(name: str) -> Path:
    from core import skills as _s
    return _dir() / f"{_s._slug(name)}.json"


# ── transcript ───────────────────────────────────────────────────────────────

def transcript(name: str, limit: int = 60) -> list[dict]:
    try:
        data = json.loads(_path(name).read_text(encoding="utf-8"))
        rows = data.get("turns") if isinstance(data, dict) else None
        return rows[-limit:] if isinstance(rows, list) else []
    except Exception:
        return []


def _append(name: str, turn: dict) -> None:
    p = _path(name)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    turns = data.get("turns") if isinstance(data.get("turns"), list) else []
    turn["at"] = time.time()
    turns.append(turn)
    # bounded: a bot that never forgets is a bot that stops working
    data["turns"] = turns[-400:]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                     encoding="utf-8")
    except Exception:
        pass


def clear(name: str) -> bool:
    p = _path(name)
    try:
        p.unlink()
        return True
    except Exception:
        return False


# ── what a bot knows about itself ────────────────────────────────────────────

def brief(name: str) -> dict:
    """The bot's identity and its own skills, as one block of prose."""
    rec = _ag.find(name)
    if not rec:
        return {"ok": False, "error": f"no bot named '{name}'"}
    mine = [s for s in _sk.all_skills()
            if (s.get("bot") or "").lower() == str(rec["name"]).lower()]
    lines = [f"You are {rec['name']}, the {rec.get('role') or 'assistant'}."]
    if rec.get("persona"):
        lines.append(rec["persona"].strip())
    if rec.get("tools"):
        lines.append("You can reach: " + ", ".join(rec["tools"]))
    if mine:
        lines.append("")
        lines.append("Your saved methods — use one when it fits, and say which:")
        for s in mine:
            when = str(s.get("when") or "").strip().splitlines()[0][:110]
            lines.append(f"  - {s['name']}: {when}")
    else:
        lines.append("")
        lines.append("You have no saved methods yet. When you do a task well, "
                     "the user can save it as one so you can rerun it.")
    return {"ok": True, "agent": rec, "skills": mine,
            "brief": "\n".join(lines)}


def _history_prompt(name: str) -> str:
    rows = transcript(name, limit=MAX_TAIL)
    if not rows:
        return ""
    lines = []
    for t in rows[-MAX_TAIL:]:
        who = "User" if t.get("role") == "user" else str(t.get("role") or "Bot")
        body = str(t.get("text") or "").strip()
        if not body:
            continue
        lines.append(f"{who}: {body[:MAX_CHARS]}")
    if not lines:
        return ""
    return ("\nYOUR CONVERSATION SO FAR (you remember this; do not ask again "
            "what you were told):\n" + "\n".join(lines))


# ── talking to a bot ─────────────────────────────────────────────────────────

_ROUTE = """Decide what the message is.

Answer with EXACTLY one word:
  WORK  - it asks you to actually do something: fetch, read, check, run,
          research, find, draft a document, change a file, or act on a system.
  TALK  - it is a question, an opinion, a greeting, or a discussion.

Message: {msg}"""


def _is_work(msg: str) -> bool:
    raw = gemini.text([{"role": "user", "parts": [{"text": _ROUTE.format(msg=msg)}]}],
                      tier=gemini.FAST, timeout_ms=20_000, default="TALK")
    return "WORK" in (raw or "").strip().upper()


def say(name: str, message: str, *, force: str = "",
        path: str = "", run: bool = True) -> dict:
    """Message a bot. It answers as itself, and does the work if asked to."""
    b = brief(name)
    if not b.get("ok"):
        return b
    msg = str(message or "").strip()
    if not msg:
        return {"ok": False, "error": "say something to it"}
    _append(name, {"role": "user", "text": msg[:MAX_CHARS * 4]})

    mode = str(force or "").lower()
    if mode not in ("work", "talk"):
        mode = "WORK" if _is_work(msg) else "TALK"

    if mode == "work" and run:
        return _do_work(name, msg, b, path)
    return _converse(name, msg, b)


def _converse(name: str, msg: str, b: dict) -> dict:
    prompt = "\n\n".join([b["brief"], _history_prompt(name),
                          f"\nUser: {msg}", "\nAnswer as yourself. Be brief."])
    out = gemini.text([{"role": "user", "parts": [{"text": prompt}]}],
                      tier=gemini.FAST, timeout_ms=90_000,
                      default="I did not get an answer back — try again?")
    _append(name, {"role": "assistant", "text": out, "mode": "talk"})
    return {"ok": bool(out), "bot": name, "mode": "talk", "reply": out}


def _do_work(name: str, msg: str, b: dict, path: str) -> dict:
    """Work, not talk. A named skill runs; otherwise the coder loop does, with
    the bot's brief as the standing instruction — so an unnamed job still gets
    the bot's judgement rather than the generic one."""
    named = next((s for s in b["skills"]
                  if s["name"].lower() in msg.lower()), None)
    if named:
        # A named skill is the method that owns this work. If it is read-only it
        # runs; if it is NOT, that is its stated boundary and the request must
        # stop here.
        #
        # The earlier version fell through to the generic coder loop instead,
        # which meant naming a skill whose approval field said "sending always
        # needs approval" caused the work to be done anyway — by a path that
        # never read the field. The boundary has to be the end of the road, not
        # a suggestion the router can route around.
        if not _sk.readonly(named):
            text = (f"'{named['name']}' needs your approval before it runs: "
                    f"{named['approval']} Say the word and I will run it.")
            _append(name, {"role": "assistant", "text": text, "mode": "skill"})
            return {"ok": False, "bot": name, "mode": "skill",
                    "skill": named["name"], "reply": text,
                    "needs_approval": True}
        r = _sk.run(named["name"], msg, path=path)
        if r.get("ok"):
            _append(name, {"role": "assistant", "text": r.get("summary", ""),
                           "mode": "skill", "skill": named["name"]})
            return {"ok": True, "bot": name, "mode": "skill",
                    "skill": named["name"], "reply": r.get("summary", ""),
                    "changed": r.get("changed", []), "steps": r.get("steps", 0)}
        text = (f"I could not run '{named['name']}' unattended: "
                f"{r.get('error') or r.get('stopped')}")
        _append(name, {"role": "assistant", "text": text, "mode": "skill"})
        return {"ok": False, "bot": name, "mode": "skill",
                "skill": named["name"], "reply": text}

    goal = ("You are acting as work, not conversation.\n\n"
            f"YOUR STANDING BRIEF\n{b['brief']}\n\n"
            f"{_history_prompt(name)}\n"
            f"THE JOB\n{msg}\n\n"
            "Do it in the workspace. Report what you did and how you verified "
            "it. If you cannot verify it, say so rather than claiming success.")
    from core import coder as _coder
    # the bot's name goes with the job, so its clicks and keystrokes are
    # logged as that bot rather than as an anonymous agent
    r = _coder.run(goal, path, bot=name)
    reply = r.get("summary") or "I did not finish — check the steps."
    _append(name, {"role": "assistant", "text": reply, "mode": "work",
                   "changed": r.get("changed", [])})
    return {"ok": bool(r.get("ok")), "bot": name, "mode": "work",
            "reply": reply, "changed": r.get("changed", []),
            "steps": r.get("steps", 0), "stopped": r.get("stopped", "")}


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, bot: str = "", message: str = "",
         path: str = "", force: str = "") -> str:
    """JARVIS delegating to the crew. Prose back — this is what gets read out."""
    a = str(action or "").strip().lower()
    try:
        if a in ("", "list", "roster"):
            rows = _ag.roster()
            if not rows:
                return "There are no bots yet. Hire one first."
            out = []
            for r in rows:
                mine = [s["name"] for s in _sk.all_skills()
                        if (s.get("bot") or "").lower() == r["name"].lower()]
                out.append(f"- {r['name']} ({r.get('role') or 'assistant'})"
                           + (f" · {len(mine)} skill(s): {', '.join(mine)}"
                              if mine else " · no skills")
                           + ("" if r.get("enabled", True) else " · disabled"))
            return "\n".join(out)

        if a in ("brief", "about", "who"):
            b = brief(bot)
            return b.get("brief") if b.get("ok") else b.get("error")

        if a in ("say", "tell", "ask", "delegate", "message"):
            r = say(bot, message, path=path, force=force)
            if r.get("ok") is False and "error" in r:
                return str(r["error"])
            head = f"[{bot}]"
            if r.get("skill"):
                head += f" ran the skill '{r['skill']}'"
            elif r.get("mode") == "work":
                head += " did the work"
            return f"{head}\n{r.get('reply','')}"

        return "Unknown action. Use list / brief / say."
    except Exception as e:
        return f"crew: {type(e).__name__}: {e}"[:200]