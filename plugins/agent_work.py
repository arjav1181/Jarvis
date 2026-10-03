"""Hand a real task to a real agent.

Everything else in here is a tool JARVIS calls. This is the thing that IS an
agent: one that plans, runs code, manages files, browses, and hands work to
subagents of its own. `core/coder.py` shells out to a CLI and hopes.
`core/agents.py` is a roster with a budget. Neither can work out that a task
needs three steps in an unexpected order, and neither notices when it is wrong
and tries again.

Google's Antigravity SDK is that harness already built — Python, so it drops
into a Python codebase, and it runs on the Gemini key this Space already has.
Subagent delegation, declarative tool policies, human-in-the-loop, MCP and
lifecycle hooks come with it as first-class features rather than five separate
modules we maintain.

WHEN TO USE THIS, AND WHEN NOT TO
    Use it when the work is open-ended and the route is not known in advance:
    "find every place this breaks", "fix the failing test and explain why it
    was failing". Do NOT use it for anything with a known single step —
    `computer` for the browser, `recall` for what was said, `summarise` for a
    page, `day_glance` for the day. An agent costs a lot more than a tool call
    and is far harder to predict, so it is the escalation, not the default.

Every failure returns a reason rather than silence. A delegation that quietly
does nothing is worse than one that says "no key configured", because silence
reads as the agent having had nothing to say.
"""

from core import agent_runtime as _ar

PLUGIN = {
    "name": "agent_work",
    "description": (
        "Delegate an open-ended task to a real autonomous agent: it plans, runs "
        "code, reads and writes files, searches the web, and can hand work to "
        "its own subagents. Use when the steps are not known in advance — "
        "'find every caller of this', 'fix the failing test and say why it was "
        "failing', 'audit this repo for X'. For anything with one known step "
        "use the specific tool instead: computer for the browser, recall for "
        "what was said, summarise for a page, day_glance for your day. Slower "
        "and less predictable than a tool call, so it is the escalation."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": "What the agent should do, stated as a complete "
                               "brief. Include what 'done' looks like and any "
                               "constraint that matters — it cannot ask you "
                               "anything mid-task.",
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Seconds to allow. Default 240. Raise it for "
                               "genuinely large jobs.",
            },
        },
        "required": ["task"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    task = str(params.get("task") or "").strip()
    if not task:
        return "I was not given a task to delegate. Say what the agent should do."

    try:
        timeout = float(params.get("timeout") or _ar.DEFAULT_TIMEOUT)
    except Exception:
        timeout = _ar.DEFAULT_TIMEOUT

    if player:
        try:
            player.write_log(f"JARVIS: delegating to an agent — {task[:70]}")
        except Exception:
            pass

    out = _ar.run_turn(task, timeout=timeout)

    if not out.get("ok"):
        reason = str(out.get("reason") or "unknown failure")
        return f"I could not delegate that: {reason}"

    text = str(out.get("text") or "").strip()
    if not text:
        return ("The agent finished but said nothing back, which usually means "
                "it could not act on the brief. A more specific task would help.")
    took = out.get("seconds")
    tail = f" ({took}s)" if took else ""
    return f"The agent finished{tail}:\n\n{text}"