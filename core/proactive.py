"""core/proactive.py — what JARVIS is allowed to do when you are not talking.

You picked bounded proactive. This module is the boundary, written down in one
table so it can be read, tested, and argued with — because "let the AI act on its
own" is exactly the request that turns a helpful assistant into an incident.

The rule is not "safe" and "unsafe". It is *reversible* and *not reversible*,
with a third bucket for the awkward middle:

  **MAY, unattended** — reading and gathering. Drafting. Scheduling. Telling you.
  Nothing here costs money, contacts a human, or cannot be undone. Morning
  briefings, watching for a price to drop, noticing a lead went quiet, drafting
  the reply you were going to write anyway.

  **MUST ASK, always** — anything that spends, sends, deletes, unlocks, commits
  to a person, or creates an obligation. This includes the subtle ones: not
  "sending email" but "sending an email that promises a price". A draft is free.
  A promise is not.

  **NEVER, not even with approval in the moment** — a small set of things where
  the user's "yes" is not informed enough to be meaningful. Emptying a bank
  account, changing the 2FA on the Space, disabling the approval gate itself. The
  gate that lets JARVIS ask for approval must not be something JARVIS can ask to
  have turned off. That is the whole reason this list exists separately from the
  approval flow.

Every unattended action is written to a ledger, so "what did it do while I was
away?" has an honest answer with timestamps — not a vibe.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from core.data_paths import data_root

MAY = "may"            # fine on its own
ASK = "ask"            # needs the human, every time
NEVER = "never"        # not offered at all, even if asked

#: keywords that move a request into NEVER, whatever it is wrapped in
#: keyword groups, matched on intent rather than exact phrases. The point is to
#: catch the request however it is worded — "disable the approval gate", "turn
#: off approvals" and "switch off the safety check" are the same ask, and each
#: one is a request to remove the thing that asks.
_GATE_VERBS = ("disable", "turn off", "switch off", "remove", "bypass", "skip",
               "get rid of", "lift", "ignore", "stop using", "change",
               "reset", "replace", "overwrite", "share")
_GATE_NOUNS = ("approval", "approvals", "the gate", "safety check", "check",
               "confirmation", "confirmations", "guardrail", "permission",
               "permissions", "two factor", "2fa", "totp", "the pin", "pin",
               "my api key", "the api key", "api key", "my key", "key",
               "hardening", "the password", "password")
_MONEY_VERBS = ("empty", "drain", "wire", "transfer", "move")
_MONEY_NOUNS = ("account", "bank", "wallet", "balance", "all the money",
                "everything", "all of it")
_WIPE_VERBS = ("delete everything", "delete it all", "wipe", "nuke",
               "factory reset", "delete all")
_NEVER_WORDS = ("give away the key", "publish the secret",
                "print the private key", "exfiltrate", "sell the api key")


#: Only these ask. Everything else is MAY — reversible, internal, and cheap to
#: be wrong about. The test is not "is this consequential" (building a website
#: is consequential) but "does this leave the machine and cannot be taken back".
#: Asking about all of those is what made the assistant feel like a permission
#: dialog instead of a colleague.
_ASK_WORDS = (
    "send", "email to", "mail", "reply to", "invoice", "quote", "pay",
    "buy", "spend", "charge", "refund", "delete", "erase", "unlock",
    "open the door", "disarm", "commit to", "push to", "merge", "deploy to",
    "sign on behalf", "post to", "publish", "transfer", "wire",
    # committing and pushing, and hiring against a budget, are decisions with
    # consequences outside the machine. "Build a site" is not — that stays free.
    "commit and push", "push the code", "push to production", "deploy to prod",
    "hire an agent", "give it a budget", "set a budget",
)


def _is_never(text: str) -> str:
    """The reason this is refused, or '' if it is not a never."""
    for w in _NEVER_WORDS:
        if w in text:
            return w
    for verbs, nouns, label in ((_GATE_VERBS, _GATE_NOUNS, "the safety gate"),
                                (_MONEY_VERBS, _MONEY_NOUNS, "the money"),
                                (_WIPE_VERBS, (), "everything")):
        if any(v in text for v in verbs) and (
                not nouns or any(n in text for n in nouns)):
            if verbs is _WIPE_VERBS:
                return "wipe everything"
            return f"{verbs[0]} {nouns[0]}" if nouns else label
    return ""

def classify(request: str) -> dict:
    """The boundary, applied. Returns the verdict and *why*, because a refusal
    that cannot explain itself gets argued with, and a system that changes its
    mind under pressure is a system without a boundary."""
    text = str(request or "").strip().lower()
    if not text:
        return {"verdict": MAY, "tier": "empty", "why": "nothing to do"}
    hit = _is_never(text)
    if hit:
        return {"verdict": NEVER, "tier": "never",
                "why": f"'{hit}' is off the table for me, even if you ask twice"}
    # Drafting is the whole point of the MAY bucket. "draft a reply" and "email
    # a reply" differ by one word and by the entire cost of being wrong, so
    # strip the drafting language and only escalate if something survives.
    scrubbed = text
    drafted = bool(re.search(r"\b(draft|write|prepare|summari[sz]e|outline|"
                             r"compose|sketch)\b", scrubbed))
    if drafted:
        scrubbed = re.sub(
            r"\b(draft|drafts|drafted|write|writes|wrote|writing|prepare|"
            r"prepares|prepared|preparing|summari[sz]e|summari[sz]ed|outline|"
            r"outlines|compose|composes|composed|sketch|sketches|sketched|"
            r"a|an|the|to|for|me|of|and|reply|replies|response|email|mail|"
            r"invoice|proposal|message)\b", " ", scrubbed)
    hits = [w for w in _ASK_WORDS
            if re.search(rf"\b{re.escape(w)}", scrubbed)]
    if hits:
        return {"verdict": ASK, "tier": "ask", "why": hits[0],
                "words": hits[:6]}
    if drafted:
        return {"verdict": MAY, "tier": "may",
                "why": "drafting only — I will not send it"}
    return {"verdict": MAY, "tier": "may", "why": "reads, drafts, or tells you"}


# ── the ledger ───────────────────────────────────────────────────────────────

_lock = threading.RLock()
_path = None
_cache: Optional[dict] = None


def _file():
    global _path
    if _path is None:
        _path = data_root() / "proactive.json"
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
        d.setdefault("ledger", [])
        d.setdefault("watches", [])
        d.setdefault("last_briefing", 0.0)
        d.setdefault("settings", {"briefing": "daily 08:00",
                                  "watches": True, "notify": True})
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


def log(action: str, *, verdict: str = MAY, detail: str = "",
        asked: bool = False) -> dict:
    with _lock:
        d = _load()
        rec = {"action": str(action)[:200], "verdict": verdict,
               "detail": str(detail or "")[:300], "asked": bool(asked),
               "at": time.time()}
        d["ledger"].append(rec)
        d["ledger"] = d["ledger"][-400:]
        _save()
    return rec


def ledger(limit: int = 40) -> list[dict]:
    with _lock:
        return list(reversed(_load()["ledger"]))[:max(1, min(int(limit or 40), 300))]


# ── watches: "if X, then tell me" ───────────────────────────────────────────

def watch(name: str, prompt: str, *, every: str = "6h",
          kind: str = "check") -> dict:
    """A standing question JARVIS answers on a cadence. The rule: a watch may
    look and may report. It may not act — `kind=act` is refused on purpose,
    because a watch that fixes things on its own is a subscription to surprises."""
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("a watch needs a name")
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("a watch needs something to look for")
    if kind not in ("check", "summarise", "draft"):
        raise ValueError("a watch can check, summarise or draft — not act")
    v = classify(prompt)
    if v["verdict"] == NEVER:
        raise ValueError(f"I will not run that: {v['why']}")
    with _lock:
        d = _load()
        row = next((x for x in d["watches"]
                    if x["name"].lower() == name.lower()), None)
        if row is None:
            row = {"name": name, "created": time.time(), "runs": 0}
            d["watches"].append(row)
        row.update({"prompt": prompt[:400], "every": str(every or "6h")[:20],
                    "kind": kind, "verdict": v["verdict"],
                    "paused": False, "next_run": time.time() + 3600})
        _save()
        return dict(row)


def watches() -> list[dict]:
    with _lock:
        return sorted([dict(x) for x in _load()["watches"]],
                      key=lambda r: r["name"].lower())


def unwatch(name: str) -> dict:
    with _lock:
        d = _load()
        row = next((x for x in d["watches"]
                    if str(name).lower() in x["name"].lower()), None)
        if row is None:
            raise ValueError(f"no watch called '{name}'")
        d["watches"].remove(row)
        _save()
    return {"deleted": row["name"]}


def due_watches(now: float | None = None) -> list[dict]:
    now = now or time.time()
    out = []
    with _lock:
        rows = [dict(x) for x in _load()["watches"]]
    for r in rows:
        if r.get("paused"):
            continue
        if now >= float(r.get("next_run") or 0):
            out.append(r)
    return out


def watch_done(name: str, *, note: str = "", found: bool = False) -> dict:
    with _lock:
        d = _load()
        row = next((x for x in d["watches"]
                    if str(name).lower() in x["name"].lower()), None)
        if row is None:
            raise ValueError(f"no watch called '{name}'")
        row["runs"] = int(row.get("runs") or 0) + 1
        row["last_run"] = time.time()
        row["next_run"] = time.time() + _every_secs(row.get("every", "6h"))
        row["last_note"] = str(note or "")[:300]
        row["last_found"] = bool(found)
        _save()
        return dict(row)


def _every_secs(spec: str) -> float:
    s = str(spec or "").strip().lower()
    m = re.match(r"^every\s+(\d+)\s*([hdm])$", s)
    if m:
        n = int(m.group(1))
        return n * {"h": 3600, "m": 60, "d": 86400}[m.group(2)]
    if s in ("hourly", "hour"):
        return 3600
    if s in ("daily", "day", "nightly"):
        return 86400
    if s in ("weekly", "week"):
        return 604800
    return 21600


# ── the morning briefing ─────────────────────────────────────────────────────

def briefing(now: float | None = None, *, force: bool = False) -> str:
    """One screen of everything that matters, assembled from parts that already
    exist. No model required for the facts — only the wording is optional.

    Order is deliberate: what is on fire, what is money, what is moving, what
    the company did, and what is next. A briefing that starts with the weather
    is a briefing nobody reads twice."""
    now = now or time.time()
    with _lock:
        d = _load()
        last = float(d.get("last_briefing") or 0)
    if not force and now - last < 6 * 3600:
        return "You had this briefing less than six hours ago — say 'again' if "\
               "you want a fresh one."
    parts, headline = [], []

    # 1. anything broken
    try:
        from core import journal as J
        errs = J.recent(2, kind="error", limit=5)
        if errs:
            headline.append(f"{len(errs)} thing(s) went wrong: "
                            + "; ".join(e["title"] for e in errs[:2]))
    except Exception:
        pass


    # 3. the calendar
    try:
        from core import gcal as G
        evs = G.events(1)
        if evs:
            bits = []
            for e in evs[:3]:
                t = G._parse_when(e.get("start"))
                bits.append(f"{t.strftime('%H:%M')} {e['title']}" if t
                            else e["title"])
            parts.append("today: " + "; ".join(bits))
    except Exception:
        pass

    # 5. what the company did
    try:
        from core import agents as A
        o = A.org()
        if o.get("runs_today"):
            parts.append(f"company: {o['runs_today']} run(s) today, "
                         f"{o['deliverables']} deliverables")
    except Exception:
        pass

    # 6. what it noticed
    try:
        from core import journal as J
        y = J.digest(datetime.fromtimestamp(now - 86400).strftime("%Y-%m-%d"))
        if y and not y.startswith("Nothing recorded"):
            parts.append("yesterday: " + y[:160])
    except Exception:
        pass

    # 7. watches that fired
    due = due_watches(now)
    if due:
        parts.append(f"{len(due)} watch(es) due: "
                     + ", ".join(w["name"] for w in due[:3]))

    with _lock:
        d = _load()
        d["last_briefing"] = now
        _save()
    log("morning briefing", verdict=MAY,
        detail="; ".join(p[:60] for p in parts)[:280])
    if not parts:
        return ("Nothing is on fire, nothing is owed, and the calendar is "
                "empty. A good day to start something.")
    out = "Good morning. " + ". ".join(parts) + "."
    if headline:
        out += " First: " + headline[0] + "."
    return out


# ── the surface ──────────────────────────────────────────────────────────────

def status() -> dict:
    with _lock:
        d = _load()
    led = d.get("ledger") or []
    return {
        "boundary": {"may": len(_ASK_WORDS), "watch_kinds": ["check", "summarise", "draft"]},
        "settings": d.get("settings", {}),
        "watches": len([w for w in d.get("watches", []) if not w.get("paused")]),
        "ledger": len(led),
        "unattended_today": sum(1 for r in led
                                if datetime.fromtimestamp(r["at"]).strftime("%Y-%m-%d")
                                == datetime.now().strftime("%Y-%m-%d")),
        "last_briefing": d.get("last_briefing", 0),
    }


def describe() -> str:
    st = status()
    return (f"Proactive: {st['watches']} watch(es), "
            f"{st['unattended_today']} unattended action(s) today, "
            f"last briefing "
            f"{'never' if not st['last_briefing'] else 'given'}. "
            f"I read, draft and schedule on my own; anything that spends, sends "
            f"or deletes, I ask first.")


def boundary_rows() -> list[dict]:
    """The table, as data, so the Control Center can show it instead of me
    describing it from memory."""
    return [
        {"bucket": "MAY", "tier": "may",
         "what": "read, gather, draft, schedule, summarise, tell you",
         "examples": "morning briefing · watch a price · draft the reply · "
                     "note what the agents did"},
        {"bucket": "ASK", "tier": "ask",
         "what": "anything that spends, sends, deletes, unlocks or promises",
         "examples": "send an email · issue an invoice · buy something · "
                     "delete a file · hire an agent with a budget · unlock"},
        {"bucket": "NEVER", "tier": "never",
         "what": "changes to the gate itself, or moves all the money",
         "examples": "disable approvals · disable 2FA · empty the account · "
                     "delete everything"},
    ]
