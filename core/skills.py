"""core/skills.py — the primitive JARVIS was missing: a reusable method.

WHY THIS FILE EXISTS
    `core/agents.py` hires a bot with a name, a persona, a tool allowlist and a
    budget. What it does NOT give it is a *procedure*. Every run re-derives the
    steps from a persona string in prose, so the bot is a capable colleague who
    forgets how you like things done between shifts.

    xAI's Grok Bot names the fix exactly. From its own docs: "A skill is a
    reusable set of instructions for how to do a task... Start with a one-time
    task. Make it reliable, save the method as a skill, and only then automate
    it." And a useful skill has six parts:

        1. When to use it
        2. Required inputs and access
        3. The sequence of work
        4. How to validate the result
        5. What to return
        6. What requires approval

    Those six are the columns here, not a summary of them. A skill missing
    `approval` is refused rather than run, because part 6 is the one that makes
    unattended execution safe — a procedure with no stated boundary has not
    decided where it stops, and we do not get to decide it for it.

WHERE A SKILL RUNS
    The Space container. It is a persistent cloud computer with a browser
    (Playwright), a filesystem and a terminal, and it does not stop when anyone
    closes a laptop. That is the same shape as Grok Bot's per-account computer,
    arrived at from the other direction: we did not add a cloud VM because we
    already had one.

THE ONE RULE THAT MAKES THIS SAFE
    A skill says what it will do; the policy gate says what it is *allowed* to
    do. They are separate on purpose. A skill written by the model can claim it
    is safe, and that claim is not trusted — every action it attempts still
    passes through `core/policy.py` on its way out. The skill's own approval
    field decides whether it may run unattended at all; the gate decides each
    individual step.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

#: Grok Bot's six parts, in its order. Used as the shape of the prompt the
#: runner gives the model, so a skill reads the same way it was written.
FIELDS = ("when", "inputs", "steps", "validate", "returns", "approval")

FIELD_HELP = {
    "when": "When this skill should be used, and when it should NOT be.",
    "inputs": "What it needs to run: files, URLs, an account, a path.",
    "steps": "The sequence of work, in order, as instructions.",
    "validate": "How to know the result is correct before returning it.",
    "returns": "What the caller gets back, and in what shape.",
    "approval": "What must a human approve before it happens. "
                "Say 'none' only if the skill is genuinely read-only.",
}

REQUIRED = FIELDS


# ── storage ──────────────────────────────────────────────────────────────────

def _dir() -> Path:
    from core.data_paths import data_root
    d = data_root() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return s[:60] or "skill"


def _path(name: str) -> Path:
    return _dir() / f"{_slug(name)}.json"


def all_skills() -> list[dict]:
    rows = []
    for p in sorted(_dir().glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("name"):
            rows.append(rec)
    return rows


def get(name: str) -> Optional[dict]:
    p = _path(name)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def save(name: str, *, when: str = "", inputs: str = "", steps: str = "",
         validate: str = "", returns: str = "", approval: str = "",
         bot: str = "", tags: Optional[list] = None,
         overwrite: bool = False) -> dict:
    """Write a skill. All six fields are required; that is the whole point.

    `approval` may be "none" — that is an explicit, recorded decision, not an
    omission — and the wording is normalised so "None" and "none" cannot both
    exist and mean different things.
    """
    nm = str(name or "").strip()[:80]
    if not nm:
        raise ValueError("a skill needs a name")
    if get(nm) and not overwrite:
        raise ValueError(f"'{nm}' already exists — overwrite=true to replace it")
    body = {"when": when, "inputs": inputs, "steps": steps,
            "validate": validate, "returns": returns, "approval": approval}
    missing = [f for f in REQUIRED if not str(body.get(f) or "").strip()]
    if missing:
        raise ValueError(
            "a skill needs all six parts; missing: " + ", ".join(missing))
    # "none" is a decision, and it must be one spelling of it
    if str(body["approval"]).strip().lower() in ("none", "no", "nothing",
                                                 "n/a", "never"):
        body["approval"] = "none (read-only; nothing is sent, spent or changed)"
    rec = {"name": nm, "slug": _slug(nm), "bot": str(bot or "").strip()[:60],
           "tags": [str(t)[:30] for t in (tags or [])][:8],
           "created": time.time(), "updated": time.time(),
           "runs": 0, "ok": 0, "failed": 0, "last_run": None, **body}
    _path(nm).write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    return rec


def delete(name: str) -> bool:
    p = _path(name)
    if not p.exists():
        return False
    p.unlink()
    return True


def reset() -> None:
    """Test hook — forget every skill."""
    for p in _dir().glob("*.json"):
        try:
            p.unlink()
        except Exception:
            pass


def readonly(rec: dict) -> bool:
    """Does this skill claim to be safe to run unattended?"""
    return str(rec.get("approval") or "").strip().lower().startswith("none")


def complete(rec: dict) -> list[str]:
    """Which of the six are missing. A stored skill should have none."""
    return [f for f in FIELDS if not str(rec.get(f) or "").strip()]


# ── prompt construction ──────────────────────────────────────────────────────

def as_prompt(rec: dict, task: str = "") -> str:
    """The instruction block a runner hands to a model.

    `task` is the one-off thing to do this time. A skill without it is a method;
    with it, a job.
    """
    out = [f"SKILL: {rec['name']}"]
    if rec.get("bot"):
        out.append(f"Owned by the bot {rec['bot']}.")
    if task:
        out.append(f"\nTHIS TIME: {task}")
    out.append(f"\nWHEN TO USE\n{rec['when']}")
    out.append(f"\nINPUTS AND ACCESS YOU NEED\n{rec['inputs']}")
    out.append(f"\nSTEPS\n{rec['steps']}")
    out.append(f"\nVALIDATE BEFORE YOU RETURN\n{rec['validate']}")
    out.append(f"\nRETURN\n{rec['returns']}")
    out.append(f"\nNEEDS HUMAN APPROVAL\n{rec['approval']}")
    out.append(
        "\nDo the steps in order. If the validation cannot be satisfied, say "
        "so and stop — do not return a result you could not verify. If a step "
        "needs approval, stop there and say what you would do and why, rather "
        "than doing it.")
    return "\n".join(out)


# ── running one ──────────────────────────────────────────────────────────────

def run(name: str, task: str = "", *, path: str = "", max_steps: int = 20,
        timeout_s: int = 900, dry_run: bool = False,
        progress=None) -> dict:
    """Execute a skill on the Space.

    The loop itself is `core/coder.py`'s — an agent that reads, writes, runs and
    checks, against the same workspace and the same refusal list. A skill is not
    a new execution engine; it is a saved procedure that the existing one is
    given. That is deliberate: two agent loops would mean two places where a
    permission bug could hide.
    """
    from core import coder as _coder

    rec = get(name)
    if not rec:
        return {"ok": False, "error": f"no skill named '{name}'"}
    gaps = complete(rec)
    if gaps:
        return {"ok": False,
                "error": f"'{name}' is missing: " + ", ".join(gaps) +
                         " — a skill without all six parts has not decided "
                           "where it stops"}

    if dry_run:
        return {"ok": True, "dry_run": True, "skill": rec["name"],
                "readonly": readonly(rec),
                "prompt": as_prompt(rec, task)}

    started = time.time()
    goal = f"{as_prompt(rec, task)}\n\nWork in the workspace and report what you did."
    result = _coder.run(goal, path, max_steps=max_steps, timeout_s=timeout_s,
                        on_step=progress)

    ok = bool(result.get("ok"))
    _count(name, ok, result)
    return {
        "ok": ok,
        "skill": rec["name"],
        "bot": rec.get("bot") or "",
        "readonly": readonly(rec),
        "task": task,
        "steps": result.get("steps", 0),
        "stopped": result.get("stopped", ""),
        "seconds": result.get("seconds", 0),
        "changed": result.get("changed", []),
        "summary": result.get("summary", ""),
        "transcript": result.get("transcript", []),
    }


def _count(name: str, ok: bool, result: dict) -> None:
    rec = get(name)
    if not rec:
        return
    rec["runs"] = int(rec.get("runs") or 0) + 1
    rec["ok" if ok else "failed"] = int(rec.get("ok" if ok else "failed") or 0) + 1
    rec["last_run"] = {"at": time.time(), "ok": ok,
                       "steps": result.get("steps", 0),
                       "stopped": result.get("stopped", ""),
                       "summary": str(result.get("summary") or "")[:300]}
    rec["updated"] = time.time()
    try:
        _path(name).write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                               encoding="utf-8")
    except Exception:
        pass


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, name: str = "", when: str = "", inputs: str = "",
         steps: str = "", validate: str = "", returns: str = "",
         approval: str = "", task: str = "", bot: str = "", path: str = "",
         overwrite: bool = False, dry_run: bool = False) -> str:
    """Prose back. Reading a skill is free; running one follows its own field."""
    a = str(action or "").strip().lower()
    try:
        if a in ("", "list", "roster"):
            rows = all_skills()
            if not rows:
                return ("No skills saved yet. A skill is a reusable method — "
                        "when to use it, what it needs, the steps, how to "
                        "validate, what to return, and what needs approval. "
                        "Do a task once, make it reliable, then save it.")
            out = []
            for r in rows:
                mark = "read-only" if readonly(r) else "needs approval"
                out.append(f"- {r['name']} [{mark}]"
                           + (f" · bot {r['bot']}" if r.get("bot") else "")
                           + (f" · {r['runs']} run(s)" if r.get("runs") else ""))
            return "\n".join(out)

        if a in ("show", "read", "get"):
            rec = get(name)
            if not rec:
                return f"No skill named '{name}'."
            body = as_prompt(rec)
            last = rec.get("last_run") or {}
            if last:
                body += (f"\n\nLAST RUN  {'ok' if last.get('ok') else 'FAILED'}"
                         f" · {last.get('steps')} steps · {last.get('summary')}")
            return body

        if a in ("create", "save", "add", "new", "teach"):
            if not steps:
                return ("I need the steps. A skill is six parts: when to use "
                        "it, what it needs, the steps, how to validate, what to "
                        "return, and what needs approval — the last one is "
                        "what makes it safe to run unattended.")
            try:
                rec = save(name, when=when, inputs=inputs, steps=steps,
                           validate=validate, returns=returns,
                           approval=approval, bot=bot, overwrite=overwrite)
            except ValueError as e:
                return str(e)
            return (f"Saved the skill '{rec['name']}'. "
                    + ("It is read-only, so it can run unattended."
                       if readonly(rec)
                       else "It needs approval, so it will ask before "
                            "running."))

        if a in ("run", "do", "execute", "start"):
            rec = get(name)
            if not rec:
                return f"No skill named '{name}'."
            if not task:
                return "What should it do this time? Give me the one-off task."
            if dry_run:
                r = run(name, task, path=path, dry_run=True)
                return ("DRY RUN — nothing was done. Here is exactly what it "
                        "would be told:\n\n" + r["prompt"])
            r = run(name, task, path=path)
            lines = [f"Ran '{r['skill']}' — {r['steps']} step(s), "
                     f"{r['seconds']}s, stopped: {r['stopped']}."]
            if r["changed"]:
                lines.append("Changed:\n" + "\n".join(f"  {c}" for c in r["changed"]))
            lines.append("")
            lines.append(r["summary"])
            if not r["ok"]:
                lines.append("\nIt did not finish cleanly — say that plainly "
                             "rather than reporting success.")
            return "\n".join(lines)

        if a in ("delete", "remove", "forget"):
            return (f"Deleted the skill '{name}'." if delete(name)
                    else f"No skill named '{name}'.")

        return "Unknown action. Use list / show / create / run / delete."
    except Exception as e:
        return f"skills: {type(e).__name__}: {e}"[:200]