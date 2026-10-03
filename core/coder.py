"""core/coder.py — the coding agent, native.

THE PROBLEM WITH THE OLD PATH
    `actions/dev_agent.py` and `actions/code_helper.py` are one-shot: plan, then
    write a file, then hope. They cannot read a file the plan did not predict,
    cannot run the thing they just wrote, and cannot see a test failure and fix
    it. That is not a coding agent, it is a very confident text generator.

    The obvious fix was to shell out to an external agent. We tried. It shares
    our OpenAI-compatible gateway credentials but picks its own model, and on
    the first run it resolved to `gemini-3-pro-image-preview` and POSTed an
    image model to /v1/chat/completions — a 400 in 13 seconds. Two brains with
    two model configurations, one of which nobody was maintaining, is exactly
    the failure mode we already got burned by once. So the loop lives here, on
    `core/gemini.py`, which already has the fallback ladder and the gateway.

WHAT THIS ACTUALLY IS
    An observe → decide → act loop over a small, sharp action space:

        ls · read · grep · write · edit · run · git · done

    `edit` is a targeted string replacement rather than a full overwrite,
    because a full overwrite of a file the model has not read in full is how an
    agent silently destroys the 400 lines below what it was editing. `write`
    still exists for new files, and refuses to clobber an existing one without
    saying so.

WHY IT CANNOT RUN AWAY
    A step budget, a wall-clock budget, and a refusal list for commands that
    cannot be undone from inside this process. `rm -rf` on a workspace is
    refused rather than trusted; the user can always run it themselves in a
    second, but an agent that can delete a tree unattended is not a tool, it is
    a liability with a language model attached.

    Every write is snapshotted first, so `undo` restores the previous content
    without needing git.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root
from core import gemini

MAX_STEPS = 40
DEFAULT_TIMEOUT_S = 900
MAX_READ_BYTES = 120_000
MAX_OUTPUT_CHARS = 8_000

#: Commands that cannot be undone from inside this process, refused outright.
#: Not "ask first" — a loop that can be talked into `rm -rf /` mid-task is not
#: a loop with a safety rail, it is a loop with a suggestion.
REFUSED = (
    re.compile(r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/"),
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r":\(\)\s*\{.*\};?\s*:"),          # fork bomb
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
    re.compile(r"\bshutdown\b|\breboot\b|\bhalt\b"),
    re.compile(r"\bchown\s+-R\s+.*\s+/(\s|$)"),
    re.compile(r"\bchmod\s+-R\s+777\s+/(\s|$)"),
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+clean\s+-[a-z]*f"),
    re.compile(r"\bnpm\s+publish\b"),
    re.compile(r"\bcurl\b[^|]*\|\s*(ba)?sh"),
    re.compile(r"\bwget\b[^|]*\|\s*(ba)?sh"),
    re.compile(r">\s*/dev/sd[a-z]"),
)

#: Read-only commands that need no approval.
SAFE_CMD = re.compile(
    r"^\s*(ls|cat|head|tail|wc|grep|rg|find|file|stat|du|df|which|echo|"
    r"pwd|cd|git\s+(status|diff|log|show|branch|rev-parse|ls-files)|"
    r"python3?\s+-c|python3?\s+-m\s+(py_compile|compileall|json\.tool)|"
    r"pytest|node\s+-e|node\s+--check|npm\s+(test|run\s+build))\b")

SYSTEM = """You are JARVIS's coding agent. You work in ONE workspace directory \
and you finish what you are asked.

HOW YOU WORK
Look before you write. Read the files you are about to change. Never overwrite \
a file you have not read; use `edit`, which changes only the lines you name.

Smallest change that solves the problem. Match the surrounding style. Do not \
reformat code you were not asked to touch. Do not add dependencies you were not \
asked to add.

THE COMPUTER
`browse` opens a URL and reads it. `click` and `fill` act on the page, `shot` \
says what is on screen. It is the SAME browser the user is looking at, with \
their sign-in and their cookies, so what you need is often already logged in. \
Read the page before you act on it. For a password use `secret:NAME` in `fill` \
rather than writing the value out — the vault types it and it never reaches \
this conversation. If an action comes back REFUSED because the user has \
control of the computer, wait for them; never work around it, and never try to \
solve a CAPTCHA or a 2FA yourself.

Verify your own work. If the repo has tests, run them and read the output. If \
they fail because of your change, fix them and run them again. An unverified \
change is not a finished change.

If something is genuinely blocked — a missing secret, an ambiguous request, a \
decision that belongs to the user — stop and say so in `done` rather than \
guessing. A clear "I could not do X because Y" is a good outcome; a confident \
wrong answer is not."""


# ── workspace ────────────────────────────────────────────────────────────────

def workspace(path: str = "") -> Path:
    """Where the agent is allowed to work.

    Deliberately NOT ~/Desktop/JarvisProjects: that path does not exist in a
    container, which is where this mostly runs, and the old agents hardcoded
    it. Defaults under the data root so it is writable, persistent and
    container-safe; an explicit path always wins.
    """
    if path:
        return Path(path).expanduser().resolve()
    return (data_root() / "workspace").resolve()


def ensure_ws(path: str = "") -> Path:
    ws = workspace(path)
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return ws


# ── snapshots, so undo works without git ─────────────────────────────────────

def _snap_path(ws: Path, rel: str) -> Path:
    h = abs(hash((ws, rel))) % (10 ** 12)
    return (data_root() / "undo").joinpath(
        f"{h}.json")


def _snapshot(ws: Path, rel: str, previous: Optional[str]) -> None:
    """Record the previous content so undo needs no git.

    The workspace is stored WITH the snapshot. It used not to be, and the store
    is shared across workspaces — so `undo` in one project restored a file in
    another. That is not a cosmetic bug: it writes to a path the caller never
    named, in a tree they may not even have running.
    """
    p = _snap_path(ws, rel)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"path": rel, "was": previous,
                                 "ws": str(ws), "at": time.time()}),
                     encoding="utf-8")
        os.chmod(p, 0o600)
    except Exception:
        pass


def undo(path: str = "") -> dict:
    """Restore the last file this agent changed."""
    ws = workspace(path)
    all_snaps = sorted((data_root() / "undo").glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    # filter to this workspace before reading any of them
    snaps = []
    for cand in all_snaps:
        try:
            rec = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(rec.get("ws") or "") == str(ws):
            snaps.append((cand, rec))
    if not snaps:
        return {"ok": False, "why": f"nothing of mine to undo in {ws}"}
    snap_file, rec = snaps[0]
    rel = rec["path"]
    target = ws / rel
    was = rec.get("was")
    # The outcome is verified, not assumed. The first version of this function
    # wrote "ok: True, removed the file I created" whether or not it had
    # touched anything, so an undo against the wrong workspace reported a clean
    # success and changed nothing. An undo that lies is worse than no undo,
    # because the user trusts it and moves on.
    try:
        if was is None:
            if target.exists():
                target.unlink()
                if target.exists():
                    return {"ok": False, "why": f"could not remove {rel}"}
                action = "removed the file I created"
            else:
                # The snapshot is stale — the file is already gone, or it was
                # never in this workspace. Say that, do not claim a change.
                snap_file.unlink()
                return {"ok": True, "path": rel, "already": True,
                        "did": f"{rel} was already gone"}
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(was, encoding="utf-8")
            if target.read_text(encoding="utf-8", errors="replace") != was:
                return {"ok": False, "why": f"could not restore {rel}"}
            action = "put the previous contents back"
        snap_file.unlink()
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": rel, "did": action}


# ── the action space ─────────────────────────────────────────────────────────

def _safe_rel(ws: Path, raw: str) -> Path:
    """Resolve inside the workspace, or refuse. `..` cannot escape it."""
    p = (ws / str(raw or ".")).resolve()
    try:
        p.relative_to(ws)
    except ValueError:
        raise ValueError(f"'{raw}' is outside the workspace")
    return p


def _clip(s: str, n: int = MAX_OUTPUT_CHARS) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n… [{len(s) - n} more characters truncated]"


def do_ls(ws: Path, a: dict) -> str:
    p = _safe_rel(ws, a.get("path") or ".")
    if not p.is_dir():
        return f"not a directory: {a.get('path')}"
    rows = []
    for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if e.name.startswith(".") and e.name not in (".env.example",):
            continue
        if e.is_dir():
            n = sum(1 for _ in e.iterdir())
            rows.append(f"  {e.name}/  ({n} entries)")
        else:
            try:
                rows.append(f"  {e.name}  ({e.stat().st_size:,} b)")
            except OSError:
                rows.append(f"  {e.name}")
    return f"{a.get('path') or '.'}:\n" + ("\n".join(rows[:220]) or "  (empty)")


def do_read(ws: Path, a: dict) -> str:
    p = _safe_rel(ws, a.get("path") or "")
    if not p.is_file():
        return f"no such file: {a.get('path')}"
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"could not read: {type(e).__name__}"
    lines = raw.splitlines()
    start = max(1, int(a.get("start") or 1))
    end = int(a.get("end") or 0) or len(lines)
    if end < start:
        start, end = end, start
    if end - start > 1200:
        end = start + 1200
    out = []
    for i in range(start - 1, min(end, len(lines))):
        out.append(f"{i+1:5d}  {lines[i]}")
    head = f"{a.get('path')} ({len(lines)} lines, showing {start}-{min(end,len(lines))})\n"
    return _clip(head + "\n".join(out))


def do_grep(ws: Path, a: dict) -> str:
    pat = str(a.get("pattern") or "")
    if not pat:
        return "grep needs a pattern"
    try:
        rx = re.compile(pat, re.I)
    except re.error as e:
        return f"bad pattern: {e}"
    hits = []
    root = _safe_rel(ws, a.get("path") or ".")
    files = [root] if root.is_file() else [
        f for f in root.rglob("*")
        if f.is_file() and f.suffix in
        {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md",
         ".yml", ".yaml", ".txt", ".sh", ".toml", ".cfg", ".ini"}]
    for f in files:
        if any(part in (".git", "node_modules", "__pycache__", "dist",
                        ".venv", "venv") for part in f.parts):
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8",
                                                  errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f.relative_to(ws)}:{i}: {line.strip()[:160]}")
        except Exception:
            continue
        if len(hits) > 400:
            break
    return _clip("\n".join(hits) or f"nothing matching {pat!r}")


def do_write(ws: Path, a: dict) -> str:
    p = _safe_rel(ws, a.get("path") or "")
    content = str(a.get("content") or "")
    existed = p.exists()
    if existed and not a.get("overwrite"):
        # Silently clobbering a file the agent never read is the single most
        # destructive thing this loop could do, so it makes the model say so.
        return (f"REFUSED: {a.get('path')} already exists "
                f"({p.stat().st_size:,} b). Read it and use `edit`, or pass "
                f"overwrite=true if you really mean to replace it.")
    prev = p.read_text(encoding="utf-8", errors="replace") if existed else None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _snapshot(ws, str(a.get("path")), prev)
    verb = "Replaced" if existed else "Created"
    note = _syntax_gate(p)
    return (f"{verb} {a.get('path')} ({len(content):,} chars). "
            f"Undo is available." + ("\n" + note if note else "")
            + "\nTHE FILE NOW READS:\n" + _after_edit(p))


def do_edit(ws: Path, a: dict) -> str:
    p = _safe_rel(ws, a.get("path") or "")
    if not p.is_file():
        return f"no such file: {a.get('path')}"
    find = str(a.get("find") or "")
    repl = str(a.get("replace") or "")

    # Line-range form. Every coding agent the model has read offers this, so it
    # asks for it whether or not our prompt mentions it, and refusing it means
    # losing the edit rather than making it. 1-indexed, inclusive.
    if not find and (a.get("start_line") or a.get("end_line")):
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"could not read: {type(e).__name__}"
        lines = raw.splitlines(keepends=True)
        s0 = int(a.get("start_line") or 1)
        e0 = int(a.get("end_line") or s0)
        if s0 < 1 or e0 > len(lines) or e0 < s0:
            return (f"REFUSED: lines {s0}-{e0} are outside {a.get('path')} "
                    f"(1-{len(lines)}). Read it again.")
        if not repl.strip() and not a.get("allow_empty"):
            # An empty replacement over a line range is a deletion. That is
            # occasionally what you want and almost never what you meant, and
            # guessing wrong destroys work with no trace in the diff.
            return (f"REFUSED: replacing lines {s0}-{e0} of "
                    f"{a.get('path')} with nothing would delete them. Use "
                    f"`run` with rm if you mean to delete, or set "
                    f"allow_empty=true.")
        new = repl if repl.endswith("\n") or not repl else repl + "\n"
        out_text = "".join(lines[:s0 - 1]) + new + "".join(lines[e0:])
        if not out_text.strip() and raw.strip():
            return (f"REFUSED: that edit would leave {a.get('path')} empty. "
                    f"Write the whole file with `write` instead if that is "
                    f"genuinely what you want.")
        _snapshot(ws, str(a.get("path")), raw)
        p.write_text(out_text, encoding="utf-8")
        note = _syntax_gate(p)
        return (f"Edited {a.get('path')}: replaced lines {s0}-{e0}. "
                f"Undo is available." + ("\n" + note if note else "")
                + "\nTHE FILE NOW READS:\n" + _after_edit(p))

    if not find:
        return ("edit needs to know WHAT to replace. Either give `find` — the "
                "exact existing text, byte for byte — or give `start_line` and "
                "`end_line` to replace a line range. You gave neither, so the "
                "edit was not attempted and nothing changed. To append to a "
                "file, `edit` the last line and include what follows it in "
                "`replace`.")
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"could not read: {type(e).__name__}"
    if not repl.strip() and not a.get("allow_empty"):
        return ("REFUSED: that would delete the matched text. Pass "
                "allow_empty=true if deleting it is the intent.")
    n = raw.count(find)
    if n == 0:
        return (f"REFUSED: `find` does not appear in {a.get('path')}. "
                f"Whitespace must match exactly. Read the file again.")
    if n > 1 and not a.get("all"):
        return (f"REFUSED: `find` appears {n} times in {a.get('path')}. "
                f"Include more surrounding lines to make it unique, or set "
                f"all=true to replace every occurrence.")
    _snapshot(ws, str(a.get("path")), raw)
    p.write_text(raw.replace(find, repl, -1 if a.get("all") else 1),
                 encoding="utf-8")
    note = _syntax_gate(p)
    return (f"Edited {a.get('path')}: {n if a.get('all') else 1} "
            f"replacement(s). Undo is available."
            + ("\n" + note if note else "")
            + "\nTHE FILE NOW READS:\n" + _after_edit(p))


def do_run(ws: Path, a: dict) -> str:
    cmd = str(a.get("command") or "").strip()
    if not cmd:
        return "run needs a command"
    for rx in REFUSED:
        if rx.search(cmd):
            return (f"REFUSED: that command cannot be undone from inside this "
                    f"process. Run it yourself if you mean it. ({cmd[:120]})")
    if not SAFE_CMD.match(cmd):
        # Not on the read-only list. It still runs — the user asked for a
        # coding agent, and refusing `npm test` because of a regex would make
        # it useless — but the transcript records that it was unlisted.
        note = "[unlisted command] "
    else:
        note = ""
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(ws), capture_output=True,
                           text=True, timeout=int(a.get("timeout") or 120),
                           env={**os.environ, "PYTHONUNBUFFERED": "1"})
    except subprocess.TimeoutExpired:
        return f"REFUSED: timed out after {a.get('timeout') or 120}s"
    except Exception as e:
        return f"could not run: {type(e).__name__}: {e}"
    body = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return _clip(f"{note}$ {cmd}\n[exit {r.returncode}]\n" + (body or "(no output)"))


def do_git(ws: Path, a: dict) -> str:
    args = str(a.get("args") or "status")
    try:
        r = subprocess.run(["git"] + args.split(), cwd=str(ws),
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        return f"git failed: {type(e).__name__}: {e}"
    return _clip(f"$ git {args}\n[exit {r.returncode}]\n"
                 + ((r.stdout or "") + (r.stderr or "") or "(no output)"))


#: The model reaches for a different agent's vocabulary — `shell` for `run`,
#: `file` for `path` — because every coding agent it has ever read uses those
#: names. Rejecting a correct action over a synonym is the difference between a
#: coding agent and a very literal one, so the synonyms are mapped, not refused.
ACTION_ALIAS = {
    "shell": "run", "bash": "run", "sh": "run", "execute": "run", "cmd": "run",
    "view": "read", "open": "read", "cat": "read", "view_file": "read",
    "list": "ls", "list_dir": "ls", "list_files": "ls", "tree": "ls",
    "search": "grep", "find": "grep", "ripgrep": "grep",
    "str_replace": "edit", "replace": "edit", "patch": "edit", "update": "edit",
    "create": "write", "new_file": "write", "create_file": "write",
    "finish": "done", "complete": "done", "stop": "done", "final": "done",
}

ARG_ALIAS = {"file": "path", "file_path": "path", "filename": "path",
             "target": "path", "query": "pattern", "search": "pattern",
             "cmd": "command", "args": "command", "text": "replace",
             "old_string": "find", "old": "find", "new": "replace",
             "new_string": "replace", "body": "content", "code": "content"}


def _normalise(a: dict) -> dict:
    """Fold synonyms into our own names. `find` is preserved because `edit`
    also accepts a line range, and those two shapes are not interchangeable."""
    out = dict(a)
    act = str(out.get("action") or "").strip().lower()
    out["action"] = ACTION_ALIAS.get(act, act)
    for src, dst in ARG_ALIAS.items():
        if src in out and dst not in out:
            val = out[src]
            if dst == "command" and isinstance(val, list):
                val = " ".join(str(v) for v in val)
            out[dst] = val
    # `edit` reached with `content` and no `replace` meant the replacement
    # text. Without this the line-range form replaced the target lines with the
    # empty string and quietly deleted code — which is exactly what happened
    # before this line existed, six times over, until the file was one line.
    if out["action"] == "edit" and "content" in out:
        if "replace" not in out or not str(out.get("replace") or "").strip():
            # A `replace` that is present but empty loses to a non-empty
            # `content`. A model that sends both keys — which it does — would
            # otherwise have its whole-file replacement discarded as an empty
            # edit, and the deletion guard would (correctly) refuse it, so the
            # run would stall on a refusal caused by nothing but a spare key.
            out["replace"] = out.pop("content")
        else:
            out.pop("content", None)
    return out


def _after_edit(p: Path, limit: int = 40) -> str:
    """The first lines of the file as it now stands.

    Without this the model edits, then re-reads the whole file to discover what
    it just did — and a live run spent four of its fourteen steps doing exactly
    that, twice over the same file. It already knows: it made the edit.
    """
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if not lines:
        return "(the file is now empty)"
    body = "\n".join(f"{i+1:4d}  {l}" for i, l in enumerate(lines[:limit]))
    if len(lines) > limit:
        body += f"\n     … {len(lines) - limit} more lines"
    return body


def _syntax_gate(p: Path) -> str:
    """Does the file still parse? A good coding agent checks its own work
    before claiming it, and the cheapest possible check is whether the file is
    even valid Python.

    This is not theoretical. A live run left a duplicated `return` line that no
    observation mentioned until pytest failed much later — because nothing
    looked at the file between the edit and the test. The model would have
    caught it on its next read if it had known; this tells it immediately, at
    the step where it can still fix it cheaply.
    """
    if p.suffix != ".py":
        return ""
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"WARNING: could not re-read for the syntax check ({e})."
    try:
        compile(src, str(p), "exec")
        return ""
    except SyntaxError as e:
        line = e.lineno or 0
        lines = src.splitlines()
        near = ""
        if 0 < line <= len(lines):
            lo, hi = max(0, line - 3), min(len(lines), line + 2)
            near = "\n".join(f"  {i+1:4d} {lines[i]}" for i in range(lo, hi))
        return (f"THE FILE NO LONGER PARSES. This edit introduced a "
                f"SyntaxError, so nothing will run until it is fixed.\n"
                f"  line {e.lineno}: {e.msg}\n{near}")


# ── the computer ──────────────────────────────────────────────────────────────
#
# The same browser core.computer gives the user, so a bot and a person share
# one screen, one sign-in and one set of cookies instead of a bot quietly
# logging in as somebody else. Every function here takes (ws, a) like the rest.
#
# These run under the coder turn's single approval, the same way `run` (a shell)
# does. Per-click approval would need approval requests from inside a long
# agent loop, which nothing here supports yet; until it does, the honest
# boundary is the turn, not the click — and `secret:NAME` keeps passwords out
# of the transcript either way.


#: whose turn it is. crew sets this so a click is logged as "Builder" and not
#: as an anonymous agent, which is the difference between a legible step log
#: and a mystery.
_CURRENT_BOT = ""


def _bot_name() -> str:
    return str(_CURRENT_BOT or "the bot")


def _computer():
    from core import computer as C
    return C


def _held_by_user() -> str:
    """Refuse everything that moves the pointer while a human is driving."""
    C = _computer()
    if (C.status().get("handover") or "") == "user":
        return ("REFUSED: the user has control of the computer right now, so I "
                "did not touch it. Say when they are done and I will carry on.")


def do_browse(ws: Path, a: dict) -> str:
    """Open a URL, or read the page already open."""
    bad = _held_by_user()
    if bad:
        return bad
    C = _computer()
    url = str(a.get("url") or "").strip()
    if not url:
        return str(C.read().get("text") or "")[:2500]
    r = C.goto(url, bot=_bot_name())
    if not r.get("ok"):
        return f"could not open {url}: {r.get('error')}"
    body = str(r.get("text") or "")
    return (f"{r.get('title')} — {r.get('url')}\n\n{body[:2500]}")


def do_click(ws: Path, a: dict) -> str:
    bad = _held_by_user()
    if bad:
        return bad
    C = _computer()
    sel = str(a.get("selector") or "").strip()
    if sel:
        r = C.click(sel, bot=_bot_name())
    else:
        r = C.click("", int(a.get("x") or 0), int(a.get("y") or 0),
                    bot=_bot_name())
    if not r.get("ok"):
        return f"click failed: {r.get('error')}"
    return f"clicked. the page now shows {str(C.read().get('title') or '')[:80]}"


def do_fill(ws: Path, a: dict) -> str:
    """Fill a field. `text` may be secret:NAME so the value never reaches me."""
    bad = _held_by_user()
    if bad:
        return bad
    C = _computer()
    sel = str(a.get("selector") or "").strip()
    val = str(a.get("text") or "")
    if not sel:
        return "fill needs a selector."
    r = C.fill(sel, val, bot=_bot_name())
    if not r.get("ok"):
        return f"fill failed: {r.get('error')}"
    return ("filled from a held secret" if "secret" in val.lower()
            else "filled.")


def do_type(ws: Path, a: dict) -> str:
    bad = _held_by_user()
    if bad:
        return bad
    C = _computer()
    if a.get("key"):
        r = C.press(str(a["key"]), bot=_bot_name())
        return "pressed." if r.get("ok") else f"press failed: {r.get('error')}"
    if a.get("amount"):
        r = C.scroll(int(a["amount"]), bot=_bot_name())
        return "scrolled." if r.get("ok") else f"scroll failed: {r.get('error')}"
    r = C.type_text(str(a.get("text") or ""), bot=_bot_name())
    return "typed." if r.get("ok") else f"type failed: {r.get('error')}"


def do_shot(ws: Path, a: dict) -> str:
    """What the screen looks like, in words. The picture is in the live panel."""
    bad = _held_by_user()
    if bad:
        return bad
    C = _computer()
    st = C.status()
    if not st.get("up"):
        return "the computer is off."
    return (f"{st.get('title') or st.get('url') or 'a blank page'} "
            f"at {st.get('url')}")


ACTIONS = {"ls": do_ls, "read": do_read, "grep": do_grep, "write": do_write,
           "edit": do_edit, "run": do_run, "git": do_git,
           # the shared computer, same browser the user is looking at
           "browse": do_browse, "click": do_click, "fill": do_fill,
           "type": do_type, "shot": do_shot}


# ── structured actions ────────────────────────────────────────────────────────
#
# The first version of this loop asked the model for a JSON object in prose and
# parsed it. It worked about two thirds of the time; the rest of the time the
# model drifted into a different agent's answer format — "EDITED BROKEN.PY"
# followed by a code fence — and the loop had to either lose the edit or guess.
#
# So the actions are declared to the API as real function declarations and the
# schema does the parsing. The JSON path stays as a fallback for a model that
# answers with text despite the tools, because losing the loop entirely is worse
# than tolerating one badly-behaved response.

def _spec() -> Any:
    from google.genai import types
    return types.FunctionDeclaration(
        name="act",
        description=(
            "Perform exactly ONE action in the workspace, then stop and look "
            "at the result. Read before you edit. Verify with run before you "
            "declare the work finished."),
        parameters={"type": "OBJECT", "properties": {
            "action": {"type": "STRING",
                       "enum": ["ls", "read", "grep", "write", "edit",
                                "run", "git", "browse", "click", "fill",
                                "type", "shot", "done"],
                       "description": "what to do next"},
            "path": {"type": "STRING", "description": "file or directory"},
            "pattern": {"type": "STRING", "description": "regex, for grep"},
            "find": {"type": "STRING",
                     "description": "exact text to replace, byte for byte"},
            "replace": {"type": "STRING", "description": "replacement text"},
            "start_line": {"type": "INTEGER", "description": "1-indexed, inclusive"},
            "end_line": {"type": "INTEGER", "description": "1-indexed, inclusive"},
            "start": {"type": "INTEGER", "description": "first line, for read"},
            "end": {"type": "INTEGER", "description": "last line, for read"},
            "content": {"type": "STRING", "description": "whole file, for write"},
            "command": {"type": "STRING", "description": "shell command, for run"},
            "args": {"type": "STRING", "description": "git subcommand and flags"},
            "summary": {"type": "STRING",
                        "description": "what you changed and how you verified it"},
            "overwrite": {"type": "BOOLEAN",
                          "description": "for write over an existing file"},
            "all": {"type": "BOOLEAN",
                    "description": "replace every occurrence of find"},
            "timeout": {"type": "INTEGER", "description": "seconds, for run"},
            "url": {"type": "STRING", "description": "for browse"},
            "selector": {"type": "STRING", "description": "for click and fill"},
            "text": {"type": "STRING",
                     "description": "for type, or the value for fill. Use "
                                    "secret:NAME to fill from the held vault "
                                    "instead of writing a password here"},
            "key": {"type": "STRING", "description": "key to press, for type"},
            "amount": {"type": "INTEGER", "description": "pixels, for scroll"},
            "x": {"type": "INTEGER", "description": "with y, for click"},
            "y": {"type": "INTEGER", "description": "with x, for click"},
        }, "required": ["action"]})


def _config() -> Any:
    from google.genai import types
    return types.GenerateContentConfig(
        system_instruction=SYSTEM,
        tools=[types.Tool(function_declarations=[_spec()])],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True),
        temperature=0.15)


#: Preferred first, then the ladder as a fallback. Tool calling is not
#: supported identically on every model, and a coder that stops working because
#: one model dropped function support would be worse than a slower one.
_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")


def _openai_contents(contents: list) -> list[dict]:
    """Gemini `contents` → OpenAI `messages`, in the shape core/gateway.py
    already knows how to send.

    The system prompt leads. Rewiring this loop onto function calling quietly
    dropped it, and the loop got measurably worse: the model had no
    instruction to read before editing or to verify before finishing, so it
    made choices that only the transcript could explain.
    """
    out = [{"role": "system", "content": SYSTEM}]
    for turn in contents or []:
        role = turn.get("role") or "user"
        if role == "model":
            role = "assistant"
        text = "".join(p.get("text", "") for p in (turn.get("parts") or [])
                       if isinstance(p, dict))
        if not text:
            continue
        out.append({"role": role, "content": text})
    return out


def _gateway_action(contents: list, timeout_s: float) -> Optional[dict]:
    """The gateway rung.

    It goes first, and the reason is quota rather than preference. The Gemini
    free tier runs dry — a live run of this loop died at step 2 on
    429 RESOURCE_EXHAUSTED having changed nothing — and the whole reason
    core/gateway.py exists is that it is "a different quota pool from the free
    tier that runs dry". A coding agent that stops working when the free tier
    is empty is a coding agent that mostly does not work.
    """
    from core import gateway as _gw
    if not _gw.enabled():
        # Said out loud on purpose. This rung returned None in silence while a
        # test ran with JARVIS_DATA pointed at a scratch directory, and the
        # only visible symptom was "the coder stopped at step 2" — which reads
        # like a model problem, not a missing credential.
        print("[coder] gateway rung skipped: no openai_base_url configured")
        return None
    try:
        tools = _gw.to_openai_tools([_spec_dict()])
        r = _gw.chat(_openai_contents(contents), tools=tools,
                     timeout=timeout_s)
    except Exception as e:
        print(f"[coder] gateway rung unavailable: {type(e).__name__}: "
              f"{str(e)[:120]}")
        return None
    for choice in (r.get("choices") or []):
        for call in ((choice.get("message") or {}).get("tool_calls") or []):
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            if not name:
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            args["action"] = str(args.get("action") or name)
            return _normalise(args)
    return None


def _spec_dict() -> dict:
    """The declaration as a plain dict, for the OpenAI-shaped gateway path."""
    return {
        "name": "act",
        "description": ("Perform exactly ONE action in the workspace, then "
                        "stop and look at the result. Read before you edit. "
                        "Verify with run before you declare the work finished."),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING",
                       "enum": ["ls", "read", "grep", "write", "edit",
                                "run", "git", "done"]},
            "path": {"type": "STRING"}, "pattern": {"type": "STRING"},
            "find": {"type": "STRING"}, "replace": {"type": "STRING"},
            "start_line": {"type": "INTEGER"}, "end_line": {"type": "INTEGER"},
            "start": {"type": "INTEGER"}, "end": {"type": "INTEGER"},
            "content": {"type": "STRING"}, "command": {"type": "STRING"},
            "args": {"type": "STRING"}, "summary": {"type": "STRING"},
            "overwrite": {"type": "BOOLEAN"}, "all": {"type": "BOOLEAN"},
            "timeout": {"type": "INTEGER"},
        }, "required": ["action"]},
    }


def _next_action(contents: list, timeout_ms: int = 180_000) -> tuple[Optional[dict], str]:
    """One turn. Returns (action, raw_text) — the action from a function call
    when the model used the tool, else from parsed JSON, else None."""
    got = _gateway_action(contents, timeout_s=max(20.0, timeout_ms / 1000.0))
    if got is not None:
        return got, ""
    try:
        cli = gemini.client(timeout_ms=timeout_ms)
        cfg = _config()
        for model in _MODELS:
            try:
                r = cli.models.generate_content(model=model, contents=contents,
                                                config=cfg)
            except Exception as e:
                print(f"[coder] {model} failed: {type(e).__name__}: "
                      f"{str(e)[:120]}")
                continue
            text_bits = []
            for c in (getattr(r, "candidates", None) or []):
                for part in ((getattr(c, "content", None) or {}).parts or []):
                    fc = getattr(part, "function_call", None)
                    if fc is not None:
                        args = dict(fc.args or {})
                        args["action"] = str(args.get("action") or fc.name or "")
                        return _normalise(args), ""
                    if getattr(part, "text", None):
                        text_bits.append(part.text)
            raw = "\n".join(text_bits).strip()
            if raw:
                return _parse(raw), raw
    except Exception as e:
        print(f"[coder] tool call unavailable: {type(e).__name__}: {e}")
    return None, ""


# ── the loop ─────────────────────────────────────────────────────────────────

def _parse(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    if not raw.startswith("{"):
        i = raw.find("{")
        j = raw.rfind("}")
        if i < 0 or j <= i:
            return None
        raw = raw[i:j + 1]
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def run(goal: str, path: str = "", *, max_steps: int = MAX_STEPS,
        timeout_s: int = DEFAULT_TIMEOUT_S, on_step=None,
        bot: str = "") -> dict:
    """Work `goal` in the workspace until it is done, blocked, or out of budget.

    Returns a report the assistant can read out loud: what changed, what was
    verified, and — if it stopped early — exactly why, in the model's words
    rather than a generic failure.
    """
    global _CURRENT_BOT
    previous_bot = _CURRENT_BOT
    _CURRENT_BOT = str(bot or "")
    try:
        return _run(goal, path, max_steps=max_steps, timeout_s=timeout_s,
                    on_step=on_step)
    finally:
        # a leaked name would attribute the NEXT bot's browser actions to this
        # one, which is exactly the kind of wrong-but-plausible log nobody
        # catches. Always cleared, on every path including a crash.
        _CURRENT_BOT = previous_bot


def _run(goal: str, path: str = "", *, max_steps: int = MAX_STEPS,
         timeout_s: int = DEFAULT_TIMEOUT_S, on_step=None) -> dict:
    """Work `goal` in the workspace until it is done, blocked, or out of budget.

    Returns a report the assistant can read out loud: what changed, what was
    verified, and — if it stopped early — exactly why, in the model's words
    rather than a generic failure.
    """
    ws = ensure_ws(path)
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "summary": "no goal given"}

    started = time.time()
    transcript: list[dict] = []
    changed: list[str] = []
    # A short map of the workspace up front saves a `ls` round-trip on almost
    # every task, and the model cannot know the shape of a tree it has not seen.
    try:
        listing = _clip(do_ls(ws, {"path": "."}), 4000)
    except Exception:
        listing = "(could not list the workspace)"

    history = [{"role": "user", "parts": [{"text":
        f"WORKSPACE: {ws}\n\nCURRENT CONTENTS:\n{listing}\n\n"
        f"GOAL: {goal}\n\nTake the next action."}]}]
    stopped = "budget"
    summary = ""
    drift = 0

    for step in range(1, int(max_steps) + 1):
        if time.time() - started > timeout_s:
            stopped = "time"
            break
        # The function call is the channel. Prose-JSON is only consulted when a
        # model answers with text instead, and two failures in a row stop the
        # loop rather than letting it thrash against a model that has left the
        # format behind.
        a, raw = _next_action(history, timeout_ms=180_000)
        if a is None:
            a = _parse(raw) if raw else None
        if a is None:
            transcript.append({"step": step, "action": "no-action",
                               "output": _clip(raw or "(nothing usable returned)", 700)})
            drift += 1
            if drift >= 2:
                stopped = "unparseable"
                break
            history.append({"role": "user", "parts": [{"text":
                "Call the `act` tool with one action. Do not reply with prose "
                "or a code fence — the tool call is the whole reply."}]})
            continue
        drift = 0

        a = _normalise(a)
        act = str(a.get("action") or "").strip().lower()
        if act in ("done", "finish", "stop", "complete"):
            summary = str(a.get("summary") or a.get("result") or "")
            transcript.append({"step": step, "action": "done",
                               "output": _clip(summary, 1200)})
            stopped = "done"
            break

        fn = ACTIONS.get(act)
        if not fn:
            out = (f"unknown action '{act}'. Use one of: "
                   f"{', '.join(sorted(ACTIONS))}, done.")
        else:
            try:
                out = fn(ws, a)
            except ValueError as e:
                out = f"REFUSED: {e}"
            except Exception as e:
                out = f"that action failed: {type(e).__name__}: {e}"
            # Only a file that actually differs counts as changed. The first
            # version appended whenever the action was write/edit and the
            # output did not start with REFUSED — so a `file: null` typo
            # produced "no such file: None" and still landed in the list, and
            # the report claimed edits that never happened.
            if act in ("write", "edit") and not str(out).startswith(("REFUSED", "no such file")):
                # Normalise to a workspace-relative path before recording. The
                # model mixes "broken.py" and "/tmp/…/ws/broken.py" in the same
                # run, so the raw values put the same file in the list twice and
                # the report read as though two files had changed.
                rel = str(a.get("path") or "")
                if rel:
                    try:
                        rp = Path(rel)
                        rel = (rp.relative_to(ws).as_posix() if rp.is_absolute()
                               else Path(str(rel).lstrip("/")).as_posix())
                    except Exception:
                        pass
                    if rel and rel not in changed:
                        changed.append(rel)

        transcript.append({"step": step, "action": act,
                           "arg": _clip(json.dumps({k: v for k, v in a.items()
                                                    if k != "action"})[:400]),
                           "output": _clip(out, 1500)})
        if on_step:
            try:
                on_step(step, act, out)
            except Exception:
                pass
        history.append({"role": "user", "parts": [{"text":
            f"OBSERVATION\n{_clip(out, 6000)}\n\n"
            f"(files you have changed so far: "
            f"{', '.join(changed) or 'none'}) Continue, or `done` if finished."}]})

    if not summary:
        # Ran out of budget without ever saying done. Say so plainly, and ask
        # for a summary anyway — the model usually knows whether it is actually
        # finished, and a silent stop is the least useful possible outcome.
        try:
            summary = gemini.text(
                history + [{"role": "user", "parts": [{"text":
                    "You are out of budget. In 3 sentences: what did you "
                    "change, what is verified, what is unfinished? No JSON."}]}],
                tier=gemini.FAST, timeout_ms=60_000)
        except Exception:
            summary = ""
        summary = (summary.strip() or
                   f"Stopped after {len(transcript)} step(s) ({stopped}) without "
                   f"reporting a result.")

    return {
        "ok": stopped == "done",
        "stopped": stopped,
        "steps": len(transcript),
        "seconds": round(time.time() - started, 1),
        "workspace": str(ws),
        "changed": changed,
        "summary": summary.strip(),
        "transcript": transcript,
    }


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, goal: str = "", path: str = "",
         read: str = "", pattern: str = "") -> str:
    """Prose back, because this gets spoken or shown in the panel."""
    a = str(action or "").strip().lower()
    ws = ensure_ws(path)
    try:
        if a in ("", "go", "task", "build", "do", "code"):
            r = run(goal, path)
            # `run` returns a short report when it refuses to start at all
            # (no goal), so nothing below may assume the full shape.
            if "workspace" not in r:
                return str(r.get("summary") or r.get("why") or "nothing to do")
            lines = [f"Workspace: {r['workspace']}",
                     f"Stopped: {r['stopped']} after {r['steps']} step(s), "
                     f"{r['seconds']}s."]
            if r["changed"]:
                lines.append("Changed:\n" + "\n".join(f"  {c}" for c in r["changed"]))
            lines.append("")
            lines.append(r["summary"])
            return "\n".join(lines)

        if a in ("status", "where"):
            return (f"Workspace {ws}\n"
                    f"{'exists' if ws.is_dir() else 'does not exist yet'} · "
                    f"{len(list(ws.iterdir())) if ws.is_dir() else 0} entries")

        if a in ("undo", "revert"):
            r = undo(path)
            return (f"Undo: {r['did']} — {r['path']}" if r.get("ok")
                    else f"Nothing to undo: {r.get('why')}")

        if a in ("ls", "list", "read", "grep"):
            if a == "ls":
                return do_ls(ws, {"path": read or "."})
            if a == "read":
                return do_read(ws, {"path": read})
            return do_grep(ws, {"pattern": pattern, "path": read or "."})

        if a in ("transcript", "log"):
            return "Use the coder tool with action=go; the report includes the steps."

        return "Unknown action. Use go / status / undo / ls / read / grep."
    except Exception as e:
        return f"coder: {type(e).__name__}: {e}"[:200]
