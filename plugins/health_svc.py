"""
Is any of it on fire?

One read-only look at the whole machine and the things around it: the Space's
own health, whether the latest deploy went red, what the scheduler is doing,
how many plugins are loaded, and what is on disk. Everything here is a read —
it never restarts anything, clears anything, or changes a setting.

    action=status:  the short answer. Is anything broken, yes or no.
    action=all:     everything, including the boring parts.
    action=jobs:    what the scheduler is running and when it next runs.
    action=deploy:  the deploy history and whether the last one was green.
    action=disk:    what is using space, and the biggest files.
    action=plugins: what is loaded, and what was rejected and why.

The "all" answer is deliberately unsatisfying when things are fine. A health
check that produces a wall of red because a metric has no history is a health
check people learn to ignore, which is the same failure as a welcome that
always says everything is operational.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

HUMAN = {"OK": "fine", "WARN": "watch", "BAD": "broken", "NONE": "no data"}


def _space() -> dict:
    """How this Space has been doing. Runtime facts, no guesses."""
    out = {}
    try:
        import psutil
        up = time.time() - psutil.boot_time()
        vm = psutil.virtual_memory()
        out["uptime"] = f"{int(up // 3600)}h {int(up % 3600 // 60)}m"
        out["cpu"] = f"{psutil.cpu_percent(interval=0.2):.0f}%"
        out["memory"] = f"{vm.percent:.0f}% of {vm.total / 2**30:.1f} GB"
    except Exception:
        pass
    try:
        d = _root()
        t, u, f = shutil.disk_usage(d)
        out["disk"] = f"{f / 2**30:.1f} GB free of {t / 2**30:.0f} GB"
        out["disk_pct_used"] = int((t - f) * 100 / t)
    except Exception:
        pass
    return out


def _root() -> Path:
    try:
        from core.data_paths import data_root
        return Path(data_root())
    except Exception:
        return Path(".")


def _load() -> float:
    """1 - 5. Only counts things that are actually wrong."""
    bad = 0
    sp = _space()
    pct = sp.get("disk_pct_used")
    if isinstance(pct, int) and pct > 92:
        bad += 2
    try:
        import psutil
        if psutil.virtual_memory().percent > 93:
            bad += 1
        if psutil.cpu_percent(interval=0.2) > 95:
            bad += 1
    except Exception:
        pass
    try:
        from core import computer as C
        st = C.status()
        if st.get("error"):
            bad += 1
    except Exception:
        bad += 1
    return min(5.0, 1.0 + bad)


def _deploy() -> tuple[str, str]:
    try:
        from core import watches as _w
        line = _w.deploy_watch() or ""
        if not line:
            return "NONE", "no deploy history yet"
        bad = any(w in line.lower() for w in ("fail", "red", "error", "broken"))
        return ("BAD" if bad else "OK"), line[:300]
    except Exception as e:
        return "NONE", f"could not check: {type(e).__name__}"


def _jobs() -> list[dict]:
    try:
        from core import scheduler as S
        store = getattr(S, "list_jobs", None)
        rows = store() if callable(store) else []
        return rows[:20] if isinstance(rows, list) else []
    except Exception:
        return []


def _plugins() -> tuple[list, list]:
    try:
        from core.plugin_loader import discover_plugins
        from core import plugins as P
        reg = discover_plugins(plugins_dir=P.repo_dir(), core_tool_names=set(),
                               logger=lambda m: None,
                               extra_dirs=[d for d in [P.user_dir()]
                                           if d is not None])
        good = [d for d in reg.list_for_ui() if not d.get("error")]
        bad = [d for d in reg.list_for_ui() if d.get("error")]
        return good, bad
    except Exception:
        return [], []


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "status").strip().lower()
    try:
        return _run(action)
    except Exception as e:
        return f"The health check broke: {type(e).__name__}: {e}"[:200]


def _run(action: str) -> str:
    load = _load()
    stage, deploy_line = _deploy()

    if action in ("status", "", "up", "ok"):
        verdict = ("Nothing is on fire." if load <= 1.5 else
                   "Some of it is unhappy." if load < 3.5 else
                   "Something is properly broken.")
        parts = [verdict]
        if stage == "BAD":
            parts.append(f"The last deploy is not green: {deploy_line}")
        good, bad = _plugins()
        if bad:
            parts.append(f"{len(bad)} plugin(s) refused to load.")
        return " ".join(parts)

    if action in ("all", "full", "everything"):
        sp = _space()
        good, bad = _plugins()
        out = [f"Health {load:.1f}/5 — {HUMAN.get('OK' if load <= 1.5 else 'WARN')}."]
        if sp:
            out.append("Space: " + ", ".join(f"{k} {v}" for k, v in sp.items()
                                             if k != "disk_pct_used") + ".")
        out.append(f"Deploy: {HUMAN[stage]} — {deploy_line}")
        out.append(f"Plugins: {len(good)} loaded"
                   + (f", {len(bad)} refused" if bad else "") + ".")
        jobs = _jobs()
        out.append(f"Scheduled jobs: {len(jobs)}.")
        if not good and not bad:
            out.append("No plugins found at all, which is itself worth "
                       "knowing — a broken import would look exactly like this.")
        return " ".join(out)

    if action in ("deploy", "deploys", "shipped"):
        return f"Deploy watch: {HUMAN[stage]} — {deploy_line}"

    if action in ("jobs", "schedule", "cron"):
        jobs = _jobs()
        if not jobs:
            return ("Nothing is scheduled. If that surprises you, the "
                    "scheduler may not have started.")
        lines = []
        for j in jobs[:12]:
            name = j.get("name") or j.get("id") or "?"
            when = j.get("next") or j.get("spec") or j.get("kind") or "?"
            lines.append(f"- {name}: {when}")
        return f"{len(jobs)} scheduled:\n" + "\n".join(lines)

    if action in ("disk", "space", "storage"):
        sp = _space()
        big = []
        try:
            for p in sorted(_root().rglob("*"), key=lambda x: -x.stat().st_size
                            if x.is_file() else 0)[:400]:
                if p.is_file() and p.stat().st_size > 20 * 2**20:
                    big.append(f"{p.name} {p.stat().st_size / 2**20:.0f} MB")
                if len(big) >= 6:
                    break
        except Exception:
            pass
        out = [f"Disk: {sp.get('disk', 'unknown')}",
               f"used: {sp.get('disk_pct_used', '?')}%"]
        if big:
            out.append("largest: " + ", ".join(big))
        return " ".join(out)

    if action in ("plugins", "extensions"):
        good, bad = _plugins()
        if not good and not bad:
            return "No plugins found."
        out = [f"{len(good)} loaded: " + ", ".join(
            sorted(str(d.get("name")) for d in good))]
        for d in bad[:6]:
            out.append(f"refused {d.get('name')}: "
                       f"{str(d.get('error'))[:90]}")
        return " ".join(out)

    if action in ("agent", "agents", "delegate"):
        # The agent runtime is the one capability that is optional by design, so
        # it is worth stating plainly whether delegation can happen at all. A
        # health check that stays quiet about it is how you find out from the
        # user instead of from the panel.
        try:
            from core import agent_runtime as _ar
            ok, why = _ar.available()
        except Exception as e:
            return f"agent runtime: could not even be inspected ({e})"
        return ("agent runtime: ready - I can delegate a task to a real agent."
                if ok else f"agent runtime: NOT ready - {why}")

    return ("Actions: status | all | jobs | deploy | disk | plugins | agent.")


PLUGIN = {
    "name": "health_svc",
    "description": (
        "Read-only health of the Space and the services around it — uptime, "
        "CPU, memory, disk, whether the last deploy was green, what the "
        "scheduler is running, and which plugins loaded or were refused. Use "
        "this for 'is anything broken', 'how is the space doing', 'why is it "
        "slow', or 'what is scheduled'. It changes nothing."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status | all | jobs | deploy | disk | plugins"},
        },
        "required": [],
    },
}
