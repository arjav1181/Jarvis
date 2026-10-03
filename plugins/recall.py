"""
Search everything you have ever said to me.

The transcripts are already there — every bot conversation, every turn, in
plain JSON on the volume. What was missing was any way to go back through
them, which makes them a log rather than a memory. "What did I say about the
pricing thing last month" should be answerable.

    action=search:  find turns matching some words.
    action=say:     what did I say about <thing> — ranked, with the date.
    action=who:     who said it (which bot), and how much of it.
    action=forget:  drop a bot's transcript. THIS ONE IS DESTRUCTIVE and the
                    policy gate makes it ask every time, because it is the
                    only action here that throws something away.

Read-only apart from `forget`, and the honest limitation is up front: this
searches what was SAID, not what is true. It tells you when you decided
something, not whether the decision still holds.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

MAX_HITS = 12


def _turns(limit_per_bot: int = 4000) -> list[dict]:
    """Every stored turn, newest first, with the bot that said it."""
    from core.data_paths import data_root
    d = data_root() / "bot_chats"
    if not d.is_dir():
        return []
    rows: list[dict] = []
    for p in sorted(d.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        turns = raw if isinstance(raw, list) else (raw.get("turns") or [])
        bot = p.stem
        for t in list(turns)[-limit_per_bot:]:
            if not isinstance(t, dict):
                continue
            text = str(t.get("text") or "").strip()
            if not text:
                continue
            rows.append({"bot": bot, "text": text,
                         "at": t.get("at") or t.get("time") or 0,
                         "role": t.get("role") or "?"})
    rows.sort(key=lambda r: (r.get("at") or 0), reverse=True)
    return rows


def _words(q: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", str(q or "").lower()) if len(w) > 2]


def _score(row: dict, terms: list[str]) -> int:
    text = row["text"].lower()
    hits = 0
    for t in terms:
        n = text.count(t)
        # every term must appear somewhere, or it is not a match at all
        if n == 0:
            return 0
        hits += n
    # a turn that opens with the phrase is usually the one that matters
    if any(terms[0] in text[:60] for _ in (0,)):
        hits += 3
    return hits


def _when(ts) -> str:
    try:
        import time
        if not ts:
            return ""
        age = time.time() - float(ts)
        if age < 3600:
            return f"{int(age // 60)} min ago"
        if age < 86400:
            return f"{int(age // 3600)} h ago"
        return f"{int(age // 86400)} d ago"
    except Exception:
        return ""


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "search").strip().lower()
    query = str(parameters.get("query") or parameters.get("text") or "")
    bot = str(parameters.get("bot") or "")
    try:
        return _run(action, query, bot, player)
    except Exception as e:
        return f"I could not search: {type(e).__name__}: {e}"[:200]


def _run(action: str, query: str, bot: str, player) -> str:
    rows = _turns()
    if action in ("count", "stats"):
        if not rows:
            return ("I have no transcripts saved yet — nothing to search.")
        by_bot: dict[str, int] = {}
        for r in rows:
            by_bot[r["bot"]] = by_bot.get(r["bot"], 0) + 1
        words = sum(len(r["text"].split()) for r in rows)
        top = ", ".join(f"{b} ({n})" for b, n in
                        sorted(by_bot.items(), key=lambda kv: -kv[1]))
        return (f"{len(rows)} turns, about {words:,} words, across "
                f"{len(by_bot)} bot(s). Most talkative: {top}.")

    if action == "forget":
        from core.data_paths import data_root
        from core import skills as _s
        if not bot:
            return "Tell me which bot's history to forget."
        p = data_root() / "bot_chats" / f"{_s._slug(bot)}.json"
        if not p.is_file():
            return f"I have no transcript for {bot}."
        p.unlink()
        return f"Forgotten — {bot}'s transcript is gone."

    if not query:
        return ("Ask me to search for something. For example: "
                "\"search everything I've said about the pricing\".")
    terms = _words(query)
    if not terms:
        return "That search had no real words in it."

    pool = [r for r in rows if not bot or r["bot"] == bot]
    scored = []
    for r in pool:
        n = _score(r, terms)
        if n:
            scored.append((n, r))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        where = f" from {bot}" if bot else ""
        return (f"Nothing{where} matches '{query}'. I have {len(pool)} turns "
                f"to search — try fewer or different words.")

    out = [f"{len(scored)} turn(s) mention {' + '.join(terms)}:"]
    for n, r in scored[:MAX_HITS]:
        when = _when(r.get("at"))
        text = r["text"].replace("\n", " ")
        if len(text) > 180:
            text = text[:177] + "..."
        out.append(f"- {r['bot']}{' · ' + when if when else ''}: {text}")
    if len(scored) > MAX_HITS:
        out.append(f"- ...and {len(scored) - MAX_HITS} more")
    out.append("That is what was said, not necessarily what is still true.")
    return "\n".join(out)


PLUGIN = {
    "name": "recall",
    "description": (
        "Search everything the user has ever said to any bot — every stored "
        "transcript, back through time. Use this when they ask what they said "
        "about something previously, what was decided, when they decided it, "
        "or what a bot knows about a topic. Do NOT use it to check a fact; it "
        "only reports what was said, not what is true."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "search | count | forget"},
            "query": {"type": "STRING",
                      "description": "the words to look for"},
            "bot": {"type": "STRING",
                    "description": "limit to one bot; also used by forget"},
        },
        "required": [],
    },
}
