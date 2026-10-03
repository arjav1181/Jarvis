"""
Run a whole scene, not four commands.

"Deep work" is three things that should happen together: start a timer, stop
the interruptions, and put the session on the list so it survives a restart.
Making the user say all three — and remember them tomorrow — is the thing
scenes exist to remove.

A scene is a named list of steps. Built-ins ship; you can add your own and they
are stored on the volume, so a scene you invented is still there after a
rebuild.

    action=list:     what scenes exist, and what each one does.
    action=run:      run one. This ASKS, every time — a scene is by
                    definition several actions at once, and that is exactly the
                    kind of thing you want to see before it happens.
    action=save:     make your own. (Asks.)
    action=delete:   remove one of yours. (Asks.)

Every step reports what it actually did, including the ones that failed. A
scene that says "done" when half of it did not run is worse than no scene, so
the summary counts successes and names the failures.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

#: What a built-in scene is made of, in plain words, so the model can describe
#: it before asking and the user knows what they are agreeing to.
BUILTIN: dict[str, dict] = {
    "deep_work": {
        "what": "start a 50 minute focus timer and put the session on the list",
        "steps": [{"call": "day_glance", "action": "focus",
                   "item": "deep work", "minutes": 50},
                  {"call": "day_glance", "action": "add",
                   "item": "deep work session"}],
    },
    "short_burst": {
        "what": "a 25 minute focus timer, and nothing else",
        "steps": [{"call": "day_glance", "action": "focus",
                   "item": "short burst", "minutes": 25}],
    },
    "morning": {
        "what": "read the day back to you — calendar, list, timer",
        "steps": [{"call": "day_glance", "action": "glance"}],
    },
    "wrap_up": {
        "what": "stop the timer and tell you what is still open",
        "steps": [{"call": "day_glance", "action": "focus", "item": "stop"},
                  {"call": "day_glance", "action": "list"}],
    },
    "recall": {
        "what": "go over what you have already decided today",
        "steps": [{"call": "recall", "action": "search", "query": "decide"},
                  {"call": "day_glance", "action": "list"}],
    },
}


def _custom_path() -> Path:
    from core.data_paths import data_root
    d = data_root() / "scenes"
    d.mkdir(parents=True, exist_ok=True)
    return d / "scenes.json"


def _custom() -> dict:
    try:
        return json.loads(_custom_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    _custom_path().write_text(json.dumps(d, indent=2), encoding="utf-8")


def _all() -> dict:
    out = {k: dict(v, builtin=True) for k, v in BUILTIN.items()}
    for k, v in _custom().items():
        out[k] = {**v, "builtin": False}
    return out


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in
                   str(s or "").lower()).strip("_")[:40]


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "list").strip().lower()
    name = _slug(parameters.get("name") or "")
    steps = parameters.get("steps")
    try:
        return _run(action, name, steps, parameters)
    except Exception as e:
        return f"The scene broke: {type(e).__name__}: {e}"[:200]


def _dispatch(step: dict) -> str:
    """Call one plugin by name. The whole point of a scene is that the user
    does not have to remember which tool does what."""
    call = str(step.get("call") or "").strip()
    if not call:
        return "a step named no tool"
    try:
        mod = __import__(f"plugins.{call}", fromlist=["run"])
    except Exception as e:
        return f"no plugin called {call} ({type(e).__name__})"
    fn = getattr(mod, "run", None)
    if not callable(fn):
        return f"the plugin {call} has no run()"
    args = {k: v for k, v in step.items() if k != "call"}
    try:
        return str(fn(args) or "")
    except Exception as e:
        return f"{call} failed: {type(e).__name__}: {e}"[:120]


def _run(action: str, name: str, steps, parameters) -> str:
    scenes = _all()

    if action in ("list", "", "what"):
        if not scenes:
            return "No scenes yet."
        lines = []
        for key in sorted(scenes):
            s = scenes[key]
            lines.append(f"- {key}{' (built-in)' if s.get('builtin') else ''}: "
                         f"{s.get('what', '')}")
        lines.append("Say run and the name. They ask first, because a scene is "
                     "several things at once.")
        return "\n".join(lines)

    if action in ("save", "add", "create"):
        if not name:
            return "What shall I call it?"
        if not isinstance(steps, list) or not steps:
            return ("Give me the steps as a list, each one a tool name plus "
                    "its arguments — for example "
                    "[{call: day_glance, action: focus, minutes: 15}]")
        clean = []
        for s in steps[:8]:
            if isinstance(s, dict) and s.get("call"):
                clean.append({k: v for k, v in s.items() if k != "call"} |
                             {"call": str(s["call"])})
        if not clean:
            return "None of those steps named a tool to call."
        d = _custom()
        d[name] = {"what": str(parameters.get("what") or "your scene"),
                   "steps": clean}
        _save(d)
        return (f"Saved '{name}' with {len(clean)} step(s). It will ask before "
                f"it runs anything.")

    if action in ("delete", "remove"):
        if not name:
            return "Which scene?"
        if name in BUILTIN:
            return f"'{name}' is built in, so it cannot be deleted."
        d = _custom()
        if name not in d:
            return f"No scene called '{name}'."
        d.pop(name, None)
        _save(d)
        return f"Deleted '{name}'."

    if action in ("run", "go", "start"):
        if not name:
            return f"Which one? I have: {', '.join(sorted(scenes))}"
        s = scenes.get(name)
        if not s:
            return (f"No scene called '{name}'. I have: "
                    f"{', '.join(sorted(scenes))}")
        results, failed = [], []
        for step in (s.get("steps") or [])[:8]:
            out = _dispatch(dict(step))
            failed_flag = out.lower().startswith(("no plugin", "a step named",
                                                 "failed", "the plugin"))
            (failed if failed_flag else results).append(out)
        head = f"Ran '{name}' — {len(results)} of " \
               f"{len(results) + len(failed)} step(s) worked."
        body = [r for r in results if r]
        tail = ([f"Failed: {f}" for f in failed] if failed else [])
        return " ".join([head] + body[:4] + tail)

    return "Actions: list | run | save | delete."


PLUGIN = {
    "name": "scene",
    "description": (
        "Run several things at once as one named routine — start a focus "
        "timer and log the session; wrap up and see what is left; go over "
        "today's decisions. Use this when the user says 'deep work', 'focus "
        "session', 'wrap up', 'morning', or asks to run or save a scene. "
        "Running one always asks first."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | run | save | delete"},
            "name": {"type": "STRING", "description": "which scene"},
            "what": {"type": "STRING",
                     "description": "for save: one line describing it"},
            # An array of objects, declared the way the API requires. This was
            # {"type": "OBJECT", "array": True}, which is not valid JSON Schema:
            # Gemini rejects the WHOLE tool list when one declaration is bad,
            # so a single malformed parameter here meant the live session never
            # connected at all — no wake word, no listening, no replies, and a
            # dashboard that looked perfectly healthy.
            "steps": {"type": "ARRAY",
                      "description": "for save: the steps, each with a 'call'",
                      "items": {
                          "type": "OBJECT",
                          "properties": {
                              "call": {"type": "STRING",
                                       "description": "tool to call, e.g. "
                                                      "day_glance"},
                              "action": {"type": "STRING",
                                         "description": "action for that tool"},
                          },
                      }},
        },
        "required": [],
    },
}
