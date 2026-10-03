"""core/journal_hooks.py — make the journal fill itself in.

Nothing here is clever. Each hook wraps one existing function, records what
happened, and calls through. The rules:

  * **never raise.** A journal must not be able to break invoicing or a
    scheduler tick, so every wrapper is wrapped.
  * **never double-wrap.** `wire()` is called at startup and a reload may call it
    again; the marker attribute makes the second call a no-op.
  * **never wrap what you don't own.** If a signature changes underneath us the
    wrapper still calls through with `*args, **kwargs`, so the worst case is a
    log line that says less, not a crash.
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable

from core import journal as J

MARK = "_jarvis_journal_hook"


def _wrap(obj: Any, attr: str, label: str) -> bool:
    """Wrap `obj.attr` once, recording a journal entry from its return value."""
    fn = getattr(obj, attr, None)
    if fn is None or getattr(fn, MARK, False):
        return False

    @functools.wraps(fn)
    def inner(*args: Any, **kwargs: Any):
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            _safe(J.record_event, "error", f"{label} failed",
                  body=f"{type(e).__name__}: {e}"[:200], ok=False)
            raise
        try:
            _note(label, out, args, kwargs)
        except Exception:
            pass
        return out

    setattr(inner, MARK, True)
    setattr(obj, attr, inner)
    return True


def _safe(fn: Callable, *a: Any, **kw: Any) -> None:
    try:
        fn(*a, **kw)
    except Exception:
        pass


def _note(label: str, out: Any, args: tuple, kwargs: dict) -> None:
    if label == "agent.run":
        job = (out or {}).get("job", "") if isinstance(out, dict) else ""
        agent = (out or {}).get("agent", "") if isinstance(out, dict) else ""
        ok = bool((out or {}).get("ok", True)) if isinstance(out, dict) else True
        J.record_event("done" if ok else "error",
                       f"{agent or 'agent'}: {job or 'ran'}"[:180],
                       tags=["agent", agent.lower() or "agent"], ok=ok)
    elif label == "agent.deliver":
        if isinstance(out, dict):
            J.entry("done", f"{out.get('agent')} filed "
                            f"{out.get('title', '')} (v{out.get('version')})"[:180],
                    body=str(out.get("summary") or "")[:400],
                    tags=["deliverable", str(out.get("kind", "")).lower()],
                    refs=[out.get("id", "")], actor=str(out.get("agent") or "JARVIS"))
    elif label == "scheduled.job":
        name = str(kwargs.get("name") or (args[0] if args else "") or "job")
        text = out if isinstance(out, str) else ""
        if text.strip():
            J.entry("event", f"scheduled: {name}"[:180], body=text[:800],
                    tags=["scheduler"], actor="scheduler")
        else:
            J.entry("event", f"scheduled: {name} (nothing to report)"[:180],
                    tags=["scheduler"], actor="scheduler")
    elif label == "approval":
        # kwargs carries `accepted`; the pending title is in the confirm queue
        accepted = kwargs.get("accepted")
        if accepted is None and len(args) >= 2:
            accepted = args[1]
        J.entry("decision",
                ("approved: " if accepted else "rejected: ")
                + str(kwargs.get("title") or "something")[:160],
                tags=["approval"], actor="user")
    elif label == "invoice":
        if isinstance(out, dict) and out.get("status"):
            J.entry("money", f"invoice {out.get('number', '?')} → "
                            f"{out['status']}"[:180],
                    body=f"{out.get('total', 0)} {out.get('currency', '')} "
                         f"for {out.get('client', '')}".strip(),
                    tags=["billing", str(out.get("status"))], refs=[out.get("id", "")])


# ── the three subsystems worth listening to ──────────────────────────────────

def hook_agents(A) -> bool:
    a = _wrap(A, "log_run", "agent.run")
    b = _wrap(A, "deliver", "agent.deliver")
    return bool(a or b)


def hook_scheduler() -> bool:
    from core import scheduler as S
    # wrap every registered handler, so all job kinds are covered at once
    got = False
    for name, fn in list(S.HANDLERS.items()):
        if getattr(fn, MARK, False):
            continue

        @functools.wraps(fn)
        def inner(job: dict, _name: str = name, _fn=fn) -> str:
            t0 = time.time()
            try:
                out = _fn(job)
            except Exception as e:
                _safe(J.record_event, "error", f"scheduled job {_name} crashed",
                      body=f"{type(e).__name__}: {e}"[:200], ok=False)
                raise
            try:
                dur = int((time.time() - t0) * 1000)
                _note("scheduled.job", out, (), {"name": _name, "ms": dur})
            except Exception:
                pass
            return out

        setattr(inner, MARK, True)
        S.HANDLERS[name] = inner
        got = True
    return got


def hook_policy() -> bool:
    from core import confirm as C
    return _wrap(C, "resolve", "approval")


def wire() -> int:
    """Call once at startup. Returns how many hooks took."""
    from core import agents as A
    n = 0
    # bound, zero-arg callables — hook_agents needs the module, and passing a
    # bare name here silently raised TypeError into the except below
    for fn in (lambda: hook_agents(A), hook_scheduler, hook_policy):
        try:
            n += 1 if fn() else 0
        except Exception as e:
            print(f"[journal] hook {fn} failed: {type(e).__name__}: {e}")
    return n
