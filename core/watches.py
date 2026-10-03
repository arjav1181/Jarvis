"""Watches: the part that makes this an assistant rather than a chatbot.

A chatbot answers when spoken to. An assistant notices. The difference is not
a bigger model — it is a scheduled job that goes and looks without being asked.

The design rule here is the one that keeps watches from becoming noise: **a
watch only speaks when something has CHANGED.** A red build that has been red
since yesterday is not news, and reporting it every thirty minutes is how you
train a human to ignore the one message that mattered. So each watch keeps the
last state it saw and stays silent unless that state moved.

Everything is read-only. A watch observes and reports; if the user wants it
changed, the model says so and the approval gate decides.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

from core import github as gh
from core import vercel as vc
from core.data_paths import data_root

# how long a still-broken thing stays "already reported"
STALE_S = 6 * 3600


def _state_path(name: str):
    return data_root() / f"watch_{name}.json"


def _load(name: str) -> dict:
    try:
        return json.loads(_state_path(name).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(name: str, st: dict) -> None:
    p = _state_path(name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _changed(watch: str, keys: list[str], *, fetcher: Optional[Callable] = None,
             now: Optional[float] = None) -> tuple[bool, list[str], list[str]]:
    """Compare what is broken now against what was broken last time.

    Returns `(should_report, new_keys, still_broken)`. A key that was already
    reported within the staleness window does not count as new — otherwise a
    build that fails every run would re-announce itself on every tick, which
    is the single fastest way to make a human stop reading the output.
    """
    now = now or time.time()
    st = _load(watch)
    last_keys = set(st.get("keys") or [])
    last_at = float(st.get("at") or 0)
    still = [k for k in keys if k in last_keys]
    fresh = [k for k in keys if k not in last_keys]
    reported_before = (now - last_at) < STALE_S
    should = bool(fresh) or bool(keys) != bool(last_keys) or \
        (not reported_before and bool(keys))
    _save(watch, {"keys": sorted(keys), "at": now})
    return should, fresh, still


# ── the watches ──────────────────────────────────────────────────────────────

def ci_watch(repos: Optional[list] = None, *, limit: int = 20,
             fetcher: Optional[Callable] = None) -> str:
    """Report CI that has just gone red. Silence when it was already red."""
    if not gh.configured() and fetcher is None:
        return ""
    try:
        targets = repos or [r["full_name"]
                            for r in gh.repos(limit=8, fetcher=fetcher)]
    except Exception:
        # the repo list is fetched outside the per-repo guard below, so it needs
        # its own. A watch that raises into the scheduler stops every other job.
        return ""
    lines: list[str] = []
    for repo in targets:
        try:
            bad = gh.failing_ci(repo, limit=limit, fetcher=fetcher)
        except Exception:
            continue
        keys = [f"{repo}#{r['id']}:{r['conclusion']}" for r in bad]
        if not keys:
            _changed(f"ci_{repo.replace('/', '_')}", [])
            continue
        should, fresh, still = _changed(f"ci_{repo.replace('/', '_')}", keys,
                                        fetcher=fetcher)
        if not should:
            continue
        head = (f"CI went red on {repo}."
                if fresh else f"CI is still red on {repo}.")
        detail = "\n".join(
            f"- {r['name']} · {r['conclusion']} · {r['branch']} · "
            f"{(r.get('commit') or '')[:70]}" for r in bad[:4])
        tail = (f"\n({len(still)} of these were already reported.)"
                if still else "")
        lines.append(head + "\n" + detail + tail)
    return "\n\n".join(lines)


def deploy_watch(*, limit: int = 20, fetcher: Optional[Callable] = None) -> str:
    """Report a production deploy that failed. This is the 3am one."""
    if not vc.configured() and fetcher is None:
        return ""
    try:
        bad = vc.broken_deploys(limit=limit, fetcher=fetcher)
    except Exception:
        return ""
    keys = [f"{d['uid']}:{d['state']}" for d in bad]
    if not keys:
        _changed("vercel", [])
        return ""
    should, fresh, still = _changed("vercel", keys, fetcher=fetcher)
    if not should:
        return ""
    head = ("A Vercel deploy failed." if fresh
            else "A Vercel deploy is still failing.")
    detail = "\n".join(f"- {d['created_at']} {d['name']} · {d['state']}"
                       + (f" · {d['commit_msg'][:60]}" if d["commit_msg"] else "")
                       for d in bad[:4])
    tail = f"\n({len(still)} already reported.)" if still else ""
    return head + "\n" + detail + tail


def infra_digest(*, fetcher: Optional[Callable] = None) -> str:
    """One paragraph of state, for the morning briefing. No change detection —
    a briefing is supposed to restate the present, not announce changes."""
    bits = []
    if gh.configured() or fetcher:
        try:
            bad = []
            for r in gh.repos(limit=5, fetcher=fetcher):
                f = gh.failing_ci(r["full_name"], limit=5, fetcher=fetcher)
                if f:
                    bad.append(f"{r['full_name']} ({len(f)} red)")
            bits.append("CI " + ("red on " + ", ".join(bad) if bad else "green everywhere"))
        except Exception:
            pass
    if vc.configured() or fetcher:
        try:
            bad = vc.broken_deploys(limit=10, fetcher=fetcher)
            bits.append("Vercel " + (f"{len(bad)} broken deploy(s)"
                                    if bad else "all READY"))
        except Exception:
            pass
    return " · ".join(bits)
