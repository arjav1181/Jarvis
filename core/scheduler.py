"""
core/scheduler.py — one persistent clock for everything JARVIS repeats.

THE PROBLEM WITH THE OLD LOOPS
    Four `while True: await asyncio.sleep(N)` blocks were scattered through
    main.py: the system monitor, the background topic monitor, the proactive
    check-in, the morning briefing. Each one:

      * disappears when the process restarts (a Space that rebuilds at 3am
        silently stops checking news until someone opens the dashboard),
      * cannot be seen, listed or changed by the user,
      * is not model-callable — "check the news at 8am" is impossible because
        the interval is a constant baked into a source file,
      * and its cadence is a bare integer, so nobody knows when it last ran.

THE DESIGN HERE
    A job is a record: {id, name, kind, spec, prompt, handler, enabled, ...}.
    Four kinds, chosen because they are the four things people actually say:

      interval   "every 30 minutes"       → every: 30
      daily      "every morning at 08:00" → at: 08:00
      cron       "weekdays at 09:30"      → m h dom mon dow
      once       "in 2 hours" / ISO stamp → fires once, then disables itself

    A job fires in exactly one of two ways:

      prompt:   the text is injected into the conversation as a normal turn,
                exactly as if the user had typed it. This is what makes
                scheduling model-callable: the model keeps every tool it
                already has, so "at 8am brief me" is just a turn it writes
                itself a week in advance.
      handler:  a registered python callable runs instead, for work that must
                not spend a model call (polling monitors, compaction).

    Everything is persisted to data_root()/scheduler.json, so a restarted
    Space or a rebooted PC resumes the same jobs with the same next_run.

CATCH-UP
    A job whose next_run is minutes in the past (the Space was asleep) does
    not storm the model on boot: it fires once, then reschedules from *now*.
    A catch-up limit keeps one restart from replaying a week of jobs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from core.data_paths import data_root

# ── Store location ───────────────────────────────────────────────────────────

def _store_path() -> Path:
    return data_root() / "scheduler.json"


def _log_path(jid: str) -> Path:
    d = data_root() / "scheduler_logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{jid}.log"


KINDS = ("interval", "daily", "cron", "once")

# Jobs fire no faster than this. Ten seconds is the old system-monitor cadence;
# anything below it belongs in a poll loop, not in a persisted schedule.
MIN_INTERVAL_S = 60.0

# How long a job may sit overdue before a restart stops replaying it. A Space
# that slept for a day should run the morning briefing once, not 1440 times.
CATCHUP_LIMIT_S = 6 * 3600.0

TICK_S = 15.0

_LOCK = threading.RLock()


# ── Cron ─────────────────────────────────────────────────────────────────────

_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}
_DOWS = {d: i for i, d in enumerate(
    ["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}


def _cron_field(spec: str, idx: int) -> set[int] | None:
    """Parse one cron field into the set of values it matches.

    Supports: 5  */3  1-5  1,3,5  mon-fri  jan  and combinations thereof.
    Returns None when the field is unparseable, which fails the whole spec —
    a job that silently never fires is worse than one rejected at creation.
    """
    lo, hi = _FIELD_RANGES[idx]
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            return None
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                return None
            step = int(step_s)
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = _cron_value(a, idx, lo), _cron_value(b, idx, lo)
            if start is None or end is None:
                return None
        else:
            start = end = _cron_value(part, idx, lo)
            if start is None:
                return None
        if start is None or end is None or start > end:
            return None
        out.update(range(start, end + 1, step))
    return out or None


def _cron_value(tok: str, idx: int, lo: int) -> Optional[int]:
    tok = tok.strip()
    if idx == 3 and tok[:3] in _MONTHS:
        return _MONTHS[tok[:3]]
    if idx == 4:
        if tok[:3] in _DOWS:
            return _DOWS[tok[:3]]
        if tok == "7":
            return 0
    if tok.isdigit():
        v = int(tok)
        return v if lo <= v <= _FIELD_RANGES[idx][1] else None
    return None


def cron_matches(spec: str, when: datetime) -> bool:
    """True when `when` (to the minute) satisfies a 5-field cron spec."""
    fields = spec.split()
    if len(fields) != 5:
        return False
    dom_star, dow_star = fields[2] == "*", fields[4] == "*"
    m, h, dom, mon, dow = (
        _cron_field(fields[i], i) for i in range(5))
    if any(f is None for f in (m, h, dom, mon, dow)):
        return False
    assert m and h and dom and mon and dow
    if when.minute not in m or when.hour not in h:
        return False
    if when.month not in mon:
        return False
    # Standard cron rule: when both day-of-month and day-of-week are
    # restricted, either matching is enough.
    dom_ok = when.day in dom
    dow_ok = (when.weekday() + 1) % 7 in dow
    if dom_star and dow_star:
        return True
    if dom_star:
        return dow_ok
    if dow_star:
        return dom_ok
    return dom_ok or dow_ok


def cron_next(spec: str, after: datetime) -> Optional[datetime]:
    """Next minute at or after `after` that matches. None = never."""
    cur = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366)
    step = timedelta(minutes=1)
    while cur <= limit:
        if cron_matches(spec, cur):
            return cur
        cur += step
    return None


# ── Spec parsing → next_run ──────────────────────────────────────────────────

def _parse_hhmm(spec: str) -> Optional[tuple[int, int]]:
    m = re.match(r"^(\d{1,2}):(\d{2})$", spec.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return hh, mm


def _parse_duration_min(spec: str) -> Optional[float]:
    """'30' | '90m' | '2h' | '1d' | '1.5h' → minutes. A bare number is
    minutes, which is how people say it out loud ('check every 30')."""
    s = str(spec).strip().lower()
    if not s:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(m|min|mins|minutes|h|hr|hours|d|day|days)?$", s)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2) or "m"
    mult = {"m": 1, "min": 1, "mins": 1, "minutes": 1,
            "h": 60, "hr": 60, "hours": 60,
            "d": 1440, "day": 1440, "days": 1440}[unit]
    return val * mult


def _parse_duration_s(spec: str) -> Optional[float]:
    """Seconds or minutes — for jobs that genuinely need sub-minute cadence.

    The vocabulary matches the minutes parser (30m, 2h, 1d) plus seconds.
    A bare number is deliberately rejected: 'every 5' is ambiguous, and
    guessing wrong means a job firing twelve times more often than the user
    meant. Intervals want a unit.
    """
    s = str(spec).strip().lower()
    if re.match(r"^\d+(?:\.\d+)?$", s):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds)$", s)
    if m:
        return float(m.group(1))
    mins = _parse_duration_min(s)
    return mins * 60.0 if mins is not None else None


def compute_next(job: dict, after: float | None = None) -> Optional[float]:
    """Epoch seconds of the job's next fire, or None for 'never'."""
    now = after if after is not None else time.time()
    kind = job.get("kind")
    spec = str(job.get("spec") or "").strip()
    if kind == "interval":
        secs = _parse_duration_s(spec)
        if not secs:
            return None
        return now + max(secs, MIN_INTERVAL_S)
    if kind == "daily":
        hhmm = _parse_hhmm(spec)
        if not hhmm:
            return None
        hh, mm = hhmm
        when = datetime.fromtimestamp(now)
        target = when.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += timedelta(days=1)
        return target.timestamp()
    if kind == "cron":
        nxt = cron_next(spec, datetime.fromtimestamp(now))
        return nxt.timestamp() if nxt else None
    if kind == "once":
        mins = _parse_duration_min(spec)
        if mins is not None:
            return now + max(mins * 60.0, 5.0)
        try:
            dt = datetime.fromisoformat(spec)
        except ValueError:
            return None
        return dt.timestamp() if dt.timestamp() > now else None
    return None


def describe(job: dict) -> str:
    """Human phrase for the cadence — used by the model and the panel."""
    kind, spec = job.get("kind"), str(job.get("spec") or "")
    if kind == "interval":
        return f"every {_human_duration(_parse_duration_s(spec) or 0)}"
    if kind == "daily":
        return f"daily at {spec}"
    if kind == "cron":
        return f"cron {spec}"
    if kind == "once":
        return f"once ({spec})"
    return str(spec or kind or "?")


def _human_duration(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        h = secs / 3600
        return f"{h:g}h"
    return f"{secs / 86400:g}d"


def validate(name: str, kind: str, spec: str) -> str:
    """'' when the spec is good, else a message explaining what is wrong."""
    if not (name or "").strip():
        return "a job needs a name"
    if kind not in KINDS:
        return f"kind must be one of {', '.join(KINDS)}"
    spec = (spec or "").strip()
    if not spec:
        return "a job needs a schedule spec"
    probe = {"kind": kind, "spec": spec}
    nxt = compute_next(probe, after=time.time())
    if nxt is None:
        if kind == "cron":
            return (f"'{spec}' is not a valid 5-field cron "
                    "(minute hour day-of-month month day-of-week)")
        if kind == "daily":
            return f"'{spec}' is not a time — use HH:MM, e.g. 08:00"
        if kind == "once":
            return f"'{spec}' is neither a duration (30m, 2h) nor an ISO timestamp"
        return f"'{spec}' is not a duration — use 30m, 2h, 1d"
    return ""


# ── Handlers ─────────────────────────────────────────────────────────────────
# Non-model work that must still run on a schedule. Each returns a string that
# is injected as a turn when non-empty, so the assistant narrates the result in
# the user's language instead of printing a log line nobody reads.

HANDLERS: dict[str, Callable[[dict], str]] = {}


def handler(name: str) -> Callable[[Callable[[dict], str]], Callable[[dict], str]]:
    def deco(fn: Callable[[dict], str]) -> Callable[[dict], str]:
        HANDLERS[name] = fn
        return fn
    return deco


@handler("agent_shift")
def _h_agent_shift(job: dict) -> str:
    """An agent coming on shift.

    Deliberately does not try to be the agent: a scheduler tick has no model in
    it, so pretending to have done the work would be a lie the user finds out
    about later. It composes the work order, attaches the real state of that
    agent's tools, and hands it over — logged, and notified to the phone.
    """
    from core import agents as _ag
    who = str(job.get("agent") or "").strip()
    if not who:
        # prompt reads "Run Reporter's shift"
        txt = str(job.get("prompt") or "")
        who = txt.split("Run ", 1)[-1].split("'s shift", 1)[0].strip()
    if not who:
        return ""
    try:
        return _ag.shift(who)
    except ValueError as e:
        return f"Agent shift skipped: {e}"


@handler("welcome_ceremony")
def _h_welcome(job: dict) -> str:
    """The welcome, on demand.

    Registered as a handler so it is reachable three ways without any of them
    needing special support: a clap from either mic, a routine that says so,
    or the panel's button.
    """
    from core import ceremony as _ce
    try:
        r = _ce.run(panels=[p for p in
                            str(job.get("panels") or "").split() if p.strip()])
        if not r.get("ok"):
            return ""
        return " ".join(r.get("lines") or [])
    except Exception:
        return ""


@handler("ci_watch")
def _h_ci_watch(job: dict) -> str:
    """Report CI that has just gone red, and stay quiet while it stays red.

    The quiet part is the whole point. An assistant that repeats the same red
    build every thirty minutes is worse than one that never looks, because the
    user learns to skip the notifications — and then misses the real one.
    """
    from core import watches as _w
    try:
        return _w.ci_watch() or ""
    except Exception:
        return ""


@handler("deploy_watch")
def _h_deploy_watch(job: dict) -> str:
    """The 3am production deploy. Reported once, when it breaks."""
    from core import watches as _w
    try:
        return _w.deploy_watch() or ""
    except Exception:
        return ""


@handler("infra_digest")
def _h_infra_digest(job: dict) -> str:
    """State of the estate, for the morning briefing."""
    from core import watches as _w
    try:
        return _w.infra_digest() or ""
    except Exception:
        return ""


@handler("director_tick")
def _h_director_tick(job: dict) -> str:
    """Do one thing toward the goal furthest from done, then say what moved.

    One move per run, not a campaign. A background agent that tries to be useful
    for nine hours is a machine that emails people; one move, with the position
    written afterwards, is how an assistant ends up *ahead of you* rather than
    merely fast. Every move is reversible and internal by construction — nothing
    here can send, pay, delete or unlock.
    """
    from core import director as _dir
    try:
        r = _dir.tick()
    except Exception as e:
        return ""
    if not r.get("did"):
        return ""
    if not r.get("ok"):
        return f"Director: {r.get('move')} on {r.get('goal')} failed."
    return f"Director: {r.get('move')} on {r.get('goal')} \u2014 {r.get('detail')}"


@handler("standup")
def _h_standup(job: dict) -> str:
    """The standup, once a day. A position and a next move, never a data dump."""
    from core import goals as _gl
    from core import director as _dir
    text = _gl.standup(force=True)
    if not text:
        return ""
    did = _dir.report()
    return text + (f"\n\nMeanwhile: {did}" if did else "")


@handler("monitor_check")
def _h_monitor_check(job: dict) -> str:
    """Daily headline check for user-configured topics (background_monitor)."""
    from actions.background_monitor import check_all
    alerts = check_all()
    if not alerts:
        return ""
    return ("New headlines on a topic you asked me to watch:\n"
            + "\n".join(f"- {a}" for a in alerts))


@handler("scheduled_check")
def _h_scheduled_check(job: dict) -> str:
    """Deliver items the assistant was told to hold onto.

    Things get parked here (see core/briefing.py push_pending) by sessions
    that end before the next morning briefing — "remind me about this" typed
    at 23:50 used to wait until the next boot. This job drains the list and
    speaks it; an empty list returns '' so the job is silent, and the
    scheduler records the tick without a turn.
    """
    try:
        from core.briefing import take_pending
    except Exception:
        return ""
    try:
        items = take_pending() or []
    except Exception:
        return ""
    if not items:
        return ""
    return ("You asked me to hold onto these earlier:\n"
            + "\n".join(f"- {str(t)[:200]}" for t in items[:10]))


# The jobs that used to be hardcoded loops, declared once. Module level so the
# prune step, the panel and the tests cannot drift apart.
BUILTIN_JOBS: dict[str, dict] = {
    "topic monitor check": dict(
        kind="interval", spec="30m", handler_name="monitor_check",
        requires_awake=True, notify=False),
    "held-items delivery": dict(
        kind="interval", spec="15m", handler_name="scheduled_check",
        requires_awake=True, notify=False),
    # Phase 4: the lead engine looks for opportunities on its own. Every six
    # hours is deliberate — these feeds are quiet, and a tighter loop would
    # just re-fetch the same posts.
    "lead discovery": dict(
        kind="interval", spec="6h", handler_name="lead_discovery",
        requires_awake=False, notify=True),
    # Phase 4d. Sending is paced by core/mail.py (one message, 45s apart, one
    # per domain, warmup ceiling) — the cadence here only decides how often JARVIS
    # looks for something worth sending.
    "lead sending": dict(
        kind="interval", spec="20m", handler_name="lead_send",
        requires_awake=False, notify=True),
    "lead reply watch": dict(
        kind="interval", spec="15m", handler_name="lead_replies",
        requires_awake=False, notify=False),
}


# ── The scheduler ────────────────────────────────────────────────────────────

class Scheduler:
    """Persistent job store + one asyncio loop that fires what's due."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._on_fire: Optional[Callable[[dict, str], Any]] = None
        self._broadcast: Optional[Callable[[dict], Any]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._dirty = False

    # ── wiring ──────────────────────────────────────────────────────────────

    def bind(self, on_fire=None, broadcast=None) -> None:
        self._on_fire = on_fire
        self._broadcast = broadcast

    # ── persistence ─────────────────────────────────────────────────────────

    def load(self) -> None:
        with _LOCK:
            try:
                raw = json.loads(_store_path().read_text(encoding="utf-8"))
                jobs = raw.get("jobs") if isinstance(raw, dict) else None
                self._jobs = {j["id"]: j for j in (jobs or [])
                              if isinstance(j, dict) and j.get("id")}
            except FileNotFoundError:
                self._jobs = {}
            except Exception:
                self._jobs = {}

    def _save(self) -> None:
        p = _store_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"jobs": list(self._jobs.values()),
                                   "saved": time.time()},
                                  indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, p)

    def _touch(self) -> None:
        """Mark dirty; the loop writes at most once a minute."""
        self._dirty = True

    def _flush(self) -> None:
        if not self._dirty:
            return
        with _LOCK:
            try:
                self._save()
                self._dirty = False
            except Exception:
                pass

    # ── CRUD ────────────────────────────────────────────────────────────────

    def add(self, name: str, kind: str, spec: str, *, prompt: str = "",
            handler_name: str = "", enabled: bool = True,
            source: str = "model", notify: bool = False,
            requires_awake: bool = False) -> dict:
        err = validate(name, kind, spec)
        if err:
            raise ValueError(err)
        if handler_name and handler_name not in HANDLERS:
            raise ValueError(
                f"unknown handler '{handler_name}' "
                f"(have: {', '.join(sorted(HANDLERS)) or 'none'})")
        if not prompt and not handler_name:
            raise ValueError("a job needs something to do: a prompt or a handler")
        with _LOCK:
            jid = "j-" + uuid.uuid4().hex[:8]
            now = time.time()
            job = {
                "id": jid,
                "name": name.strip()[:80],
                "kind": kind,
                "spec": str(spec).strip()[:80],
                "prompt": str(prompt or "").strip()[:4000],
                "handler": handler_name,
                "enabled": bool(enabled),
                "notify": bool(notify),
                "requires_awake": bool(requires_awake),
                "source": source,
                "created": now,
                "next_run": compute_next({"kind": kind, "spec": spec}, now),
                "last_run": 0.0,
                "last_fired": 0.0,
                "last_result": "",
                "runs": 0,
                "failures": 0,
            }
            self._jobs[jid] = job
            self._save()
        self._emit(job)
        return dict(job)

    def get(self, jid: str) -> Optional[dict]:
        with _LOCK:
            job = self._jobs.get(str(jid))
            return dict(job) if job else None

    def list(self, include_disabled: bool = True) -> list[dict]:
        with _LOCK:
            jobs = [dict(j) for j in self._jobs.values()]
        if not include_disabled:
            jobs = [j for j in jobs if j.get("enabled")]
        jobs.sort(key=lambda j: (j.get("next_run") or 1e18, j.get("name", "")))
        return jobs

    def remove(self, jid: str) -> bool:
        with _LOCK:
            job = self._jobs.pop(str(jid), None)
            if job:
                self._save()
        if job:
            self._emit(None, removed=str(jid))
        return job is not None

    def set_enabled(self, jid: str, enabled: bool) -> Optional[dict]:
        with _LOCK:
            job = self._jobs.get(str(jid))
            if not job:
                return None
            job["enabled"] = bool(enabled)
            if enabled and not job.get("next_run"):
                job["next_run"] = compute_next(job)
            self._save()
            out = dict(job)
        self._emit(out)
        return out

    def ensure(self, name: str, kind: str, spec: str, **kw) -> dict:
        """Get-or-create by name — used to migrate the old hardcoded loops."""
        with _LOCK:
            for j in self._jobs.values():
                if j.get("name") == name:
                    return dict(j)
        return self.add(name, kind, spec, **kw)

    def update(self, jid: str, **fields) -> Optional[dict]:
        kind, spec = fields.get("kind"), fields.get("spec")
        with _LOCK:
            job = self._jobs.get(str(jid))
            if not job:
                return None
            if kind or spec:
                merged = dict(job)
                if kind:
                    merged["kind"] = kind
                if spec:
                    merged["spec"] = spec
                err = validate(merged["name"], merged["kind"], merged["spec"])
                if err:
                    raise ValueError(err)
                job.update(kind=merged["kind"], spec=merged["spec"])
                job["next_run"] = compute_next(job)
            for k in ("name", "prompt", "handler", "notify", "requires_awake"):
                if k in fields and fields[k] is not None:
                    job[k] = fields[k]
            self._save()
            out = dict(job)
        self._emit(out)
        return out

    # ── firing ──────────────────────────────────────────────────────────────

    def due(self, now: float | None = None) -> list[dict]:
        t = now if now is not None else time.time()
        with _LOCK:
            return [dict(j) for j in self._jobs.values()
                    if j.get("enabled") and j.get("next_run")
                    and j["next_run"] <= t]

    def run_now(self, jid: str) -> dict:
        job = self.get(jid)
        if not job:
            return {"error": "unknown job"}
        if job.get("status") == "running":
            return {"error": "already running"}
        self._fire(job, "manual")
        return {"ok": True, "id": jid}

    def _reschedule(self, job: dict, now: float) -> None:
        if job.get("kind") == "once":
            job["enabled"] = False
            job["next_run"] = None
        else:
            job["next_run"] = compute_next(job, now)
            # A cron that no longer matches (impossible after validation, but
            # cheap to survive) must not spin the tick loop.
            if job["next_run"] is None:
                job["enabled"] = False

    def _fire(self, job: dict, reason: str) -> None:
        jid = job.get("id", "")
        text = ""
        ok = True
        err = ""
        hname = job.get("handler") or ""
        if hname:
            fn = HANDLERS.get(hname)
            if fn is None:
                ok, err = False, f"unknown handler {hname}"
            else:
                try:
                    text = fn(dict(job)) or ""
                except Exception as e:
                    ok, err = False, f"{type(e).__name__}: {e}"
        elif job.get("prompt"):
            text = job["prompt"]
        with _LOCK:
            live = self._jobs.get(jid)
            if live is None:
                return
            now = time.time()
            live["last_run"] = now
            live["last_fired"] = now
            live["runs"] = int(live.get("runs", 0)) + 1
            if not ok:
                live["failures"] = int(live.get("failures", 0)) + 1
                live["last_result"] = err[:300]
            elif text:
                live["last_result"] = text[:300]
            else:
                live["last_result"] = "done (no output)"
            self._reschedule(live, now)
            self._save()
            out = dict(live)
        self._log(jid, f"[{datetime.fromtimestamp(now):%H:%M:%S}] "
                       f"{reason} · {'ok' if ok else 'FAILED: ' + err}"
                       + (f" · {text[:200]}" if text else ""))
        if self._broadcast:
            try:
                self._broadcast({"type": "job", "job": out,
                                 "reason": reason,
                                 "ok": ok, "error": err, "text": text[:400]})
            except Exception:
                pass
        if ok and text and self._on_fire:
            try:
                self._on_fire(out, text)
            except Exception as e:
                try:
                    self._log(jid, f"on_fire raised: {type(e).__name__}: {e}")
                except Exception:
                    pass

    def _log(self, jid: str, line: str) -> None:
        try:
            p = _log_path(jid)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(line.rstrip() + "\n")
        except Exception:
            pass

    def read_log(self, jid: str, tail: int = 200) -> str:
        try:
            p = _log_path(jid)
            if not p.exists():
                return ""
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-max(1, int(tail)):])
        except Exception:
            return ""

    def _emit(self, job: Optional[dict], removed: str = "") -> None:
        if not self._broadcast:
            return
        try:
            msg: dict[str, Any] = {"type": "job", "removed": removed}
            if job:
                msg["job"] = dict(job)
                msg["when"] = describe(job)
            self._broadcast(msg)
        except Exception:
            pass

    # ── the loop ────────────────────────────────────────────────────────────

    async def run_loop(self) -> None:
        """Tick forever. Fired jobs are dispatched, not awaited inline, so a
        slow handler can never delay the next job's turn."""
        self._loop = asyncio.get_running_loop()
        self.load()
        self.ensure_defaults()
        # Catch-up: a job that was due while we were down runs once, now.
        now = time.time()
        for job in self.due(now):
            if now - float(job.get("next_run") or now) > CATCHUP_LIMIT_S:
                with _LOCK:
                    live = self._jobs.get(job["id"])
                    if live:
                        self._reschedule(live, now)
                self._log(job["id"], "skipped (overdue past catch-up limit)")
        self._flush()
        try:
            while True:
                try:
                    for job in self.due():
                        self._fire(job, "scheduled")
                except Exception as e:
                    print(f"[Scheduler] tick error: {e}")
                self._flush()
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # ── migration of the old hardcoded loops ────────────────────────────────

    def prune_builtins(self, keep_names: Iterable[str]) -> list[str]:
        """Delete builtin jobs whose name is no longer registered.

        Renaming or retiring a built-in must not leave its old record behind
        ticking forever — the store outlives the code that made it.
        """
        keep = set(keep_names)
        with _LOCK:
            gone = [j["id"] for j in self._jobs.values()
                    if j.get("source") == "builtin" and j.get("name") not in keep]
            for jid in gone:
                self._jobs.pop(jid, None)
            if gone:
                self._save()
                for jid in gone:
                    self._emit(None, removed=jid)
        return gone

    def ensure_defaults(self) -> None:
        """Re-register the loops that used to live in main.py.

        The background topic monitor was a `while True` with a hardcoded
        30-minute sleep; it is now a job that survives restarts, can be
        paused from the panel, and shows up in `manage_schedule` output.
        """
        builtins = BUILTIN_JOBS
        self.prune_builtins(builtins)
        for name, kw in builtins.items():
            self.ensure(name, kw.pop("kind"), kw.pop("spec"),
                        source="builtin", **kw)


# ── Process-wide instance ────────────────────────────────────────────────────

_SCHEDULER: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = Scheduler()
    return _SCHEDULER


def manage(action: str, *, name: str = "", kind: str = "", spec: str = "",
           prompt: str = "", job_id: str = "", enabled: bool = True,
           source: str = "model", notify: bool = False) -> str:
    """Model-facing implementation of the manage_schedule tool.

    Returns prose, because the assistant reads it out loud or pastes it into
    the panel — a JSON blob spoken to a human is a bug, not a feature.
    """
    sched = get_scheduler()
    act = (action or "").strip().lower()
    if act in ("add", "create", "schedule", "new"):
        if not prompt.strip():
            return ("Tell me what the job should DO (the prompt), e.g. "
                    "'brief me on the morning headlines'.")
        try:
            job = sched.add(name or (prompt[:40] + "…"), kind or "interval",
                            spec or "60m", prompt=prompt, source=source,
                            notify=notify)
        except ValueError as e:
            return f"I could not schedule that: {e}"
        return (f"Scheduled '{job['name']}' — {describe(job)}. "
                f"Job id {job['id']}.")
    if act in ("list", "ls", "show"):
        jobs = sched.list()
        if not jobs:
            return "No scheduled jobs yet."
        lines = []
        for j in jobs:
            nxt = (datetime.fromtimestamp(j["next_run"]).strftime("%a %H:%M")
                   if j.get("next_run") else "paused")
            state = "" if j.get("enabled") else " (paused)"
            lines.append(f"- {j['name']}{state}: {describe(j)} · next {nxt}")
        return f"{len(jobs)} scheduled job(s):\n" + "\n".join(lines)
    if act in ("remove", "delete", "cancel"):
        target = job_id or name
        hits = [j for j in sched.list() if j["id"] == target or j["name"] == target]
        if not hits:
            return f"No scheduled job named '{target}'."
        if len(hits) > 1 and not job_id:
            return ("More than one job matches — give the exact job id: "
                    + ", ".join(j["id"] for j in hits))
        sched.remove(hits[0]["id"])
        return f"Removed '{hits[0]['name']}'."
    if act in ("enable", "resume", "pause", "disable", "stop"):
        want = act in ("enable", "resume")
        target = job_id or name
        hits = [j for j in sched.list() if j["id"] == target or j["name"] == target]
        if not hits:
            return f"No scheduled job named '{target}'."
        job = sched.set_enabled(hits[0]["id"], want)
        return f"{'Resumed' if want else 'Paused'} '{job['name']}'."
    if act in ("run", "run_now", "test"):
        target = job_id or name
        hits = [j for j in sched.list() if j["id"] == target or j["name"] == target]
        if not hits:
            return f"No scheduled job named '{target}'."
        res = sched.run_now(hits[0]["id"])
        return res.get("error") or f"Ran '{hits[0]['name']}' right now."
    return ("Unknown action. Use add / list / remove / enable / disable / run.")


def to_public(job: dict) -> dict:
    """Job record shaped for the dashboard (adds the human cadence + age)."""
    out = dict(job)
    out["when"] = describe(job)
    nxt = job.get("next_run")
    out["next_in_s"] = round(max(0.0, (nxt or 0) - time.time()), 1) if nxt else None
    last = job.get("last_run") or 0
    out["last_age_s"] = round(time.time() - last, 1) if last else None
    return out
