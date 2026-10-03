"""The agent JARVIS delegates to.

Everything else in here is a tool JARVIS calls. This is the thing that *is* an
agent: one that plans, runs code, manages files, browses, and can hand work to
subagents of its own.

We were hand-rolling that. `core/coder.py` shells out to a CLI and hopes.
`core/agents.py` is a roster with a budget. Neither of them can decide that a
task needs three steps in an unexpected order, and neither can notice it is
wrong and try again. Google's Antigravity SDK is that harness already built —
and it is Python, it runs on the Gemini key this Space already has, and it
brings subagent delegation, declarative tool policies, human-in-the-loop, MCP
and lifecycle hooks as first-class features instead of five separate modules we
maintain ourselves.

Scope, deliberately: this is the smallest thing that proves the runtime works.
One turn in, one answer out. The things that come after — letting the crew's
hired agents run on it, letting code_task use it, replacing coder.py — are
separate decisions, and this module is shaped so they do not require rewriting
it.

Every failure path returns a reason. A delegation that silently does nothing is
worse than one that says "no key configured", because the user reads silence as
the agent deciding it had nothing to say.
"""

import os
import time
from typing import Any, Optional

#: How long one turn may take before we give up on it. Long, because an agent
#: that is planning and running code is legitimately slow, and short enough that
#: a wedged turn cannot hold the caller for the whole request timeout.
DEFAULT_TIMEOUT = 240.0


def _env(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def api_key() -> str:
    """The key, read the same way core/gemini.py reads it, so there is one
    answer to "is Gemini configured" rather than two that can disagree."""
    return _env("JARVIS_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def sdk() -> Any:
    """The SDK module, or None. Lazy, because importing it pulls a runtime
    binary and nothing here should cost anything until it is actually used."""
    try:
        import google.antigravity as A
        return A
    except Exception:
        return None


def available() -> tuple[bool, str]:
    """(can we delegate?, why not). Never raises — callers show this to a user."""
    if not api_key():
        return False, ("no Gemini key. Set JARVIS_GEMINI_API_KEY or "
                       "GEMINI_API_KEY on the Space and I can delegate.")
    if sdk() is None:
        return False, ("the Antigravity SDK is not installed here. It is an "
                       "optional extra, so the app runs fine without it — but I "
                       "cannot delegate until it is present.")
    return True, "ready"


async def run_turn_async(task: str) -> dict:
    """One turn, awaited. `Agent` is an async context manager and `chat()` is a
    coroutine, so there is no honest synchronous version of this — the sync
    wrapper below exists only because tool dispatch is synchronous."""
    A = sdk()
    cfg = A.LocalAgentConfig()
    async with A.Agent(cfg) as agent:
        response = await agent.chat(task)
        return str(await response.text())


def run_turn(task: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Hand one task to an agent and return what it said.

    Returns a dict with `ok` and either `text` or `reason`. It does not raise:
    this is called from inside a tool dispatch, where an exception becomes
    "Tool 'x' failed" in front of the user and the actual cause is a line in a
    log nobody reads.
    """
    task = (task or "").strip()
    if not task:
        return {"ok": False, "reason": "I was given no task to delegate."}

    ok, why = available()
    if not ok:
        return {"ok": False, "reason": why}

    try:
        import asyncio
    except Exception as e:                                   # pragma: no cover
        return {"ok": False, "reason": f"asyncio is unavailable: {e}"}

    # Tools are dispatched from a worker thread precisely so they cannot block
    # the event loop, which means there is no loop running here and asyncio.run
    # is the correct way in. If that ever stops being true this fails loudly
    # with a clear reason instead of deadlocking.
    try:
        asyncio.get_running_loop()
        return {"ok": False,
                "reason": ("Delegation has to run off the event loop. This call "
                           "was made from inside one, which is a bug in the "
                           "caller rather than something I can work around.")}
    except RuntimeError:
        pass

    started = time.time()

    async def _bounded() -> dict:
        try:
            return {"ok": True, "text": await asyncio.wait_for(
                run_turn_async(task), timeout=timeout)}
        except asyncio.TimeoutError:
            return {"ok": False,
                    "reason": f"The agent did not finish within {int(timeout)}s."}
        except Exception as e:
            return {"ok": False,
                    "reason": f"The agent failed on this task: "
                              f"{type(e).__name__}: {e}"}

    try:
        out = asyncio.run(_bounded())
    except Exception as e:
        return {"ok": False, "reason": f"The agent runtime stopped: {e}"}
    out["seconds"] = round(time.time() - started, 2)
    return out