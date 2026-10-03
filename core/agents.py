"""core/agents.py — a roster of agents, and JARVIS as the thing that runs them.

Phase 8. The plan asks for named roles you configure in the dashboard, each with
a persona, a model, allowed tools, a budget and a schedule — plus `delegate`
becoming org-aware, deliverables with version history, and per-agent spend and
success rates.

The design decision that matters: **an agent is a config row, not a class.** A
new role is five fields in the dashboard, not a pull request. That is the whole
difference between a company you can rearrange on a Tuesday and a codebase.

How work actually runs:

  * `hire()` / `add` — create a role (or edit one).
  * `brief()` — hand an agent a job. The agent gets its persona plus its allowed
    tools and nothing else; the result lands in the deliverables store with a
    version, so "the second version of the ad set" is a real thing.
  * An agent with `tools: []` runs on our cheap model. One with `opencode` can
    build things (Phase 2's runner). One with `leads` can go hunting.
  * Budgets are enforced *before* the call, per agent, per day — the same
    fail-closed shape as the policy engine, because a company that cannot
    overspend is a company you can leave running overnight.

Reviewers are real: an agent can be pointed at another's deliverable, and its
verdict is recorded against it.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

_lock = threading.RLock()
_path: Optional[Path] = None
_cache: Optional[dict] = None

# ── store ────────────────────────────────────────────────────────────────────

def _file() -> Path:
    global _path
    if _path is None:
        _path = data_root() / "agents.json"
    return _path


def _blank() -> dict:
    return {"agents": [], "deliverables": [], "runs": [], "spend": {}, "seq": 0}


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            data = json.loads(_file().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = _blank()
        except Exception:
            data = _blank()
        for k, v in _blank().items():
            data.setdefault(k, v)
        _cache = data
        return _cache


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


def seed(force: bool = False) -> int:
    """Seed nothing, and say so.

    There used to be a starter crew here — Coder, Lead Scout, Copywriter, Ads
    Manager, Researcher, Builder, Reporter — copied into the store on first run.
    They were deleted because they were somebody else's idea of what you need,
    and they seeded a workforce aimed at selling services, which is not what
    this is for. An empty roster is the honest default: you and JARVIS build the
    crew, out loud, when you know what it is for.

    `hire()` is untouched and remains the way an agent gets made. `force` is kept
    so the startup call sites do not need changing; it no longer means anything,
    because there is nothing left to force in.
    """
    return 0


# ── the roster ───────────────────────────────────────────────────────────────

def _tools_arg(tools: Any) -> list[str]:
    if tools is None:
        return []
    if isinstance(tools, str):
        return [t.strip() for t in tools.replace(",", " ").split() if t.strip()]
    if isinstance(tools, (list, tuple)):
        return [str(t).strip() for t in tools if str(t).strip()]
    return []


def hire(name: str, *, role: str = "", persona: str = "",
         tools: Any = None, model: str = "default",
         budget_usd_day: float = 0.0, schedule: str = "",
         enabled: bool = True, starter: bool = False) -> dict:
    name = str(name or "").strip()[:60]
    if not name:
        raise ValueError("an agent needs a name")
    with _lock:
        data = _load()
        existing = next((a for a in data["agents"]
                         if a["name"].lower() == name.lower()), None)
        if existing is None:
            existing = {"id": "ag-" + uuid.uuid4().hex[:8],
                        "name": name, "created": time.time(),
                        "runs": 0, "success": 0, "failed": 0,
                        "spent_usd_total": 0.0, "versions": 0}
            data["agents"].append(existing)
        existing["name"] = name
        if role:
            existing["role"] = str(role)[:40]
        if persona:
            existing["persona"] = str(persona)[:1200]
        tl = _tools_arg(tools)
        if tl:
            existing["tools"] = tl[:24]
        existing.setdefault("tools", [])
        existing.setdefault("role", "other")
        existing.setdefault("persona", "")
        existing["model"] = str(model or "default")[:40]
        existing["budget_usd_day"] = max(0.0, float(budget_usd_day or 0))
        existing["schedule"] = str(schedule or "")[:80]
        existing["enabled"] = bool(enabled)
        existing["starter"] = bool(existing.get("starter") or starter)
        existing["updated"] = time.time()
        _save()
        return dict(existing)


def find(name: str) -> Optional[dict]:
    n = str(name or "").strip().lower()
    with _lock:
        for a in _load()["agents"]:
            if n and n in a["name"].lower():
                return dict(a)
    return None


def roster(include_disabled: bool = True) -> list[dict]:
    with _lock:
        rows = [dict(a) for a in _load()["agents"]]
    if not include_disabled:
        rows = [a for a in rows if a.get("enabled")]
    rows.sort(key=lambda a: a["name"].lower())
    for a in rows:
        s = a.get("success", 0)
        r = a.get("runs", 0)
        a["success_rate"] = round(s / r, 3) if r else None
        a["status"] = ("retired" if not a.get("enabled") else
                       "idle" if not a.get("busy") else "working")
    return rows


def retire(name: str, *, delete: bool = False) -> dict:
    with _lock:
        data = _load()
        a = next((x for x in data["agents"]
                  if name.strip().lower() in x["name"].lower()), None)
        if a is None:
            raise ValueError(f"no agent called '{name}'")
        if delete:
            data["agents"].remove(a)
            _save()
            return {"deleted": a["name"]}
        a["enabled"] = False
        a["updated"] = time.time()
        _save()
        return dict(a)


def set_busy(name: str, busy: bool) -> None:
    with _lock:
        a = next((x for x in _load()["agents"]
                  if name.strip().lower() in x["name"].lower()), None)
        if a is not None:
            a["busy"] = bool(busy)
            _save()


# ── budget ───────────────────────────────────────────────────────────────────

def _day() -> str:
    return time.strftime("%Y-%m-%d")


def spent(name: str) -> float:
    with _lock:
        spend = _load().get("spend") or {}
        return round(float(spend.get(f"{name}:{_day()}", 0.0)), 4)


def may_spend(name: str, amount: float, *, cost_hint: float = 0.0) -> tuple[bool, str]:
    """Checked before a run, exactly like the policy engine's daily cap. $0 means
    that agent cannot use a paid model until the user gives it a budget."""
    try:
        amount = max(0.0, float(amount))
    except (TypeError, ValueError):
        return False, "could not read the amount"
    a = find(name)
    if a is None:
        return False, f"no agent called '{name}'"
    if amount <= 0:
        return True, ""
    cap = float(a.get("budget_usd_day") or 0)
    if cap <= 0:
        return False, (f"{a['name']} has no daily budget, so it cannot spend. "
                       f"Give it one in the Agents panel if you want a paid model.")
    done = spent(a["name"])
    if done + amount > cap:
        return False, (f"${amount:.2f} would take {a['name']} to "
                       f"${done + amount:.2f}, over its ${cap:.2f} daily budget")
    return True, ""


def record_spend(name: str, amount: float, *, what: str = "") -> float:
    global _day
    with _lock:
        data = _load()
        key = f"{name}:{_day()}"
        data["spend"][key] = round(float(data["spend"].get(key, 0.0))
                                   + float(amount or 0), 4)
        a = next((x for x in data["agents"]
                  if x["name"].lower() == name.lower()), None)
        if a is not None:
            a["spent_usd_total"] = round(
                float(a.get("spent_usd_total") or 0) + float(amount or 0), 4)
        _save()
        return data["spend"][key]


# ── deliverables ─────────────────────────────────────────────────────────────

def deliver(agent: str, kind: str, title: str, body: str, *,
            summary: str = "", source: str = "", cost: float = 0.0,
            status: str = "new") -> dict:
    """A thing an agent produced. Same title, later run = a new version, and the
    history is kept — version history is the point, not a nicety."""
    title = str(title or "").strip()[:160]
    if not title:
        raise ValueError("a deliverable needs a title")
    with _lock:
        data = _load()
        same = [d for d in data["deliverables"]
                if d["title"].lower() == title.lower() and d["kind"] == kind]
        version = (max(int(d.get("version") or 1) for d in same) + 1) if same else 1
        rec = {
            "id": "del-" + uuid.uuid4().hex[:8],
            "agent": agent, "kind": str(kind or "note")[:40],
            "title": title, "body": str(body or "")[:20000],
            "summary": str(summary or "")[:400],
            "source": str(source or "")[:200],
            "version": version, "status": status,
            "cost": round(float(cost or 0), 4),
            "created": time.time(),
            "reviews": [],
        }
        data["deliverables"].append(rec)
        a = next((x for x in data["agents"]
                  if x["name"].lower() == str(agent).lower()), None)
        if a is not None:
            a["versions"] = int(a.get("versions") or 0) + 1
        data["seq"] = int(data.get("seq") or 0) + 1
        _save()
        return dict(rec)


def deliverables(kind: str = "", agent: str = "", limit: int = 60) -> list[dict]:
    with _lock:
        rows = [dict(d) for d in _load()["deliverables"]]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if agent:
        rows = [r for r in rows if str(r.get("agent", "")).lower() == agent.lower()]
    rows.sort(key=lambda r: r.get("created", 0), reverse=True)
    return rows[:max(1, min(int(limit or 60), 500))]


def get_deliverable(ref: str) -> Optional[dict]:
    r = str(ref or "").strip().lower()
    with _lock:
        for d in _load()["deliverables"]:
            if r in (d["id"].lower(), d["title"].lower()):
                return dict(d)
    return None


def review(ref: str, reviewer: str, verdict: str, note: str = "") -> dict:
    """One agent grading another's work. Recorded against the deliverable so the
    version history carries the review too."""
    with _lock:
        d = next((x for x in _load()["deliverables"]
                  if str(ref).strip().lower() in (x["id"].lower(),
                                                  x["title"].lower())), None)
        if d is None:
            raise ValueError(f"no deliverable matching '{ref}'")
        d.setdefault("reviews", []).append({
            "reviewer": str(reviewer)[:60],
            "verdict": str(verdict or "")[:40],
            "note": str(note or "")[:400], "at": time.time()})
        d["status"] = "reviewed"
        _save()
        return dict(d)


# ── runs ─────────────────────────────────────────────────────────────────────

def log_run(agent: str, *, job: str, ok: bool = True, cost: float = 0.0,
            detail: str = "", deliverable_id: str = "") -> dict:
    with _lock:
        data = _load()
        rec = {"id": "run-" + uuid.uuid4().hex[:8], "agent": agent,
               "job": str(job or "")[:200], "ok": bool(ok),
               "cost": round(float(cost or 0), 4),
               "detail": str(detail or "")[:300],
               "deliverable_id": deliverable_id, "at": time.time()}
        data["runs"].append(rec)
        data["runs"] = data["runs"][-500:]
        a = next((x for x in data["agents"]
                  if x["name"].lower() == str(agent).lower()), None)
        if a is not None:
            a["runs"] = int(a.get("runs") or 0) + 1
            a["success"] = int(a.get("success") or 0) + (1 if ok else 0)
            a["failed"] = int(a.get("failed") or 0) + (0 if ok else 1)
        if cost:
            record_spend(agent, cost, what=job)
        _save()
        return rec


def recent_runs(limit: int = 40) -> list[dict]:
    with _lock:
        return list(reversed(_load()["runs"]))[:max(1, min(int(limit or 40), 300))]


# ── the org view ─────────────────────────────────────────────────────────────

def org() -> dict:
    """What the Agents panel header shows: the company at a glance."""
    rows = roster()
    live = [a for a in rows if a.get("enabled")]
    today = _day()
    spent_today = round(sum(v for k, v in
                            ((_load().get("spend") or {}).items())
                            if k.endswith(":" + today)), 4)
    runs = _load()["runs"]
    recent = [r for r in runs if _day() in time.strftime("%Y-%m-%d",
                                                       time.localtime(r["at"]))]
    return {
        "agents": len(rows), "active": len(live),
        "working": sum(1 for a in live if a.get("busy")),
        "deliverables": len(_load()["deliverables"]),
        "runs_total": len(runs),
        "runs_today": len(recent),
        "ok_today": sum(1 for r in recent if r.get("ok")),
        "spent_usd_today": spent_today,
        "success_rate": (round(sum(1 for r in recent if r.get("ok")) / len(recent), 3)
                         if recent else None),
    }


def brief(agent_name: str, job: str, *, deliverable_kind: str = "note",
          extra: str = "") -> dict:
    """Compose the work order an agent gets: its persona, its tools, its budget
    state and the job. Returned as text so it can be shown, logged, or handed
    to a model verbatim."""
    a = find(agent_name)
    if a is None:
        raise ValueError(f"no agent called '{agent_name}'")
    if not a.get("enabled"):
        raise ValueError(f"{a['name']} is retired — switch it on first")
    lines = [
        f"# {a['name']} — {a.get('role', 'other')}",
        "",
        a.get("persona") or "(no persona set — write like a careful professional)",
        "",
        "## Tools you may use",
        ", ".join(a.get("tools") or []) or "(none: you write prose, nothing else)",
        "",
        f"## Daily budget",
        f"${float(a.get('budget_usd_day') or 0):.2f} — spent ${spent(a['name']):.2f} today",
        "",
        "## The job",
        str(job or "").strip(),
    ]
    if extra:
        lines += ["", "## Context", str(extra)]
    lines += ["", "## Output",
              f"Deliver a {deliverable_kind}. Say plainly what you produced and "
              f"what you are unsure about. Do not invent facts, numbers, "
              f"contacts or results."]
    return {"agent": a["name"], "text": "\n".join(lines),
            "tools": a.get("tools") or [], "model": a.get("model", "default")}


def describe() -> str:
    o = org()
    return (f"{o['active']}/{o['agents']} agents active, "
            f"{o['runs_today']} runs today, "
            f"{o['deliverables']} deliverables, "
            f"${o['spent_usd_today']:.2f} spent")
def delete_deliverable(ref: str) -> str:
    with _lock:
        data = _load()
        d = next((x for x in data["deliverables"]
                  if str(ref).strip().lower() in (x["id"].lower(),
                                                  x["title"].lower())), None)
        if d is None:
            return ""
        data["deliverables"].remove(d)
        _save()
        return d["title"]


# ── a shift ──────────────────────────────────────────────────────────────────

def _snapshot(tools: list[str]) -> list[str]:
    """Cheap, real numbers from whatever that agent is allowed to touch. This is
    the honest part of an agent shift: no invented progress, just the state of
    the tools it can actually reach."""
    out: list[str] = []
    if "task_status" in tools or "code_task" in tools:
        try:
            from core import tasks as T
            jobs = T.list_jobs()
            live = [j for j in jobs if j.get("status") in ("running", "queued")]
            out.append(f"coding tasks: {len(live)} live of {len(jobs)}")
        except Exception:
            pass
    if "knowledge" in tools:
        try:
            from core import knowledge as K
            st = K.stats()
            out.append(f"memory: {st.get('documents', 0)} documents, "
                       f"{st.get('turns', 0)} past turns")
        except Exception:
            pass
    return out


def shift(agent_name: str, job: str = "") -> str:
    """Run an agent's shift: hand them the work order plus the real state of
    their tools, and record that it happened. Returns the order as text — a
    scheduler handler cannot call a model, so it never pretends the work is
    done; it produces the brief and says so."""
    a = find(agent_name)
    if a is None:
        raise ValueError(f"no agent called '{agent_name}'")
    if not a.get("enabled"):
        raise ValueError(f"{a['name']} is retired")
    tools = a.get("tools") or []
    work = job.strip() or _default_shift(a)
    order = brief(a["name"], work, deliverable_kind=_kind_of(tools))
    snaps = _snapshot(tools)
    if snaps:
        order["text"] += "\n\n## What changed since your last shift\n" + \
            "\n".join(f"- {s}" for s in snaps)
    set_busy(a["name"], False)
    log_run(a["name"], job=work[:120], ok=True)
    return order["text"]


def _kind_of(tools: list[str]) -> str:
    if "code_task" in tools:
        return "code"
    if "proposal" in tools:
        return "proposal"
    if "display" in tools:
        return "visual"
    if "leads" in tools or "invoice" in tools:
        return "report"
    return "note"


def _default_shift(a: dict) -> str:
    role = (a.get("role") or "").lower()
    return {
        "sales": "Work the top of the lead board: who is warm, what is the next "
                 "send, and draft it if it is due.",
        "marketing": "Review what the business did last week and produce next "
                     "week's three highest-leverage pieces of copy.",
        "operations": "Summarise the company: revenue collected, leads moved, "
                      "tasks shipped, anything overdue or stuck.",
        "research": "Find the one thing that changed this week that would alter "
                    "our plan, with the source.",
        "engineering": "Pick the highest-value small improvement and describe it "
                       "concretely enough to build.",
    }.get(role, f"Take the next piece of {a.get('role') or 'company'} work that "
                f"matters most this week.")
