"""core/computer.py — the one shared computer every bot sits at.

THIS IS THE PIECE THAT MAKES IT GROK BOT AND NOT AN API WRAPPER
    Grok Bot's bots do not call APIs. They *use a computer*: one persistent
    cloud machine with a browser, a filesystem and a terminal, shared by every
    bot on the account, with a persistent signed-in browser session. Their docs
    are blunt about what that means — "Do not use separate Bots as a security
    boundary", "the computer belongs to your account, not to an individual Bot".

    That is the architecture here too. One Chromium context, one launch, one
    profile directory that survives a restart. Cookies are shared, so a bot that
    signs in once has signed in for all of them — exactly their behaviour, and
    for exactly the same reason.

FOUR THINGS THIS DOES THAT AN API CLIENT CANNOT
    1. LIVE VIEW. You watch the browser: a screenshot on every action, streamed
       to the dashboard over a WebSocket. Their "Agent Computer" panel.
    2. YOU DRIVE. Take-over mode: you click and type, the bot stops touching the
       keyboard, and it picks up again when you hand control back. Their answer
       to a CAPTCHA, a 2FA prompt, or any page that wants a human.
    3. SECRETS. A password or one-time code is typed into a masked field in the
       UI, goes into an encrypted vault, and is filled into the page by the
       automation. It never enters the transcript, never enters a prompt, and
       is never returned to a model. Their explicit rule: "Do not send a
       password or one-time code in ordinary chat."
    4. A STEP LOG. Every navigation, click, keystroke and fill is recorded with
       its result, so the conversation shows tool activity alongside messages,
       and an approval can name the exact action rather than asking you to trust
       a summary.

WHAT IT IS NOT
    Not a security boundary, and neither is theirs. One profile means one set of
    cookies for every bot. This file says so where a caller can read it, because
    a shared profile that is mistaken for isolation is worse than one that is
    documented as shared.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

WORKSPACE = "workspace"          # their name for it, and it is a good one
VIEW_W, VIEW_H = 1280, 860
#: tabs reopened on wake — enough to be useful, few enough to stay quick
MAX_TABS = 5

_lock = threading.RLock()
_state: dict = {
    "pw": None, "browser": None, "context": None, "page": None,
    "started_at": 0.0, "owner": "", "handover": None,   # handover = user driving
    "steps": [],                                       # the step log
}
#: watchers: (callable(kind, payload)) — the dashboard's live view subscribes
_watchers: list[Callable[[str, dict], None]] = []
_secrets: dict[str, str] = {}      # in-memory only. See vault().


# ── the profile, which is the whole point ────────────────────────────────────

def _profile_dir() -> Path:
    """Where the browser's cookies live.

    Persistent on purpose. A fresh profile per bot would mean a sign-in per bot,
    which is the exact friction their shared computer removes.
    """
    from core.data_paths import data_root
    d = data_root() / "computer" / "profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workspace_dir() -> Path:
    from core.data_paths import data_root
    d = data_root() / WORKSPACE
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── lifecycle ────────────────────────────────────────────────────────────────

# ── the loop the browser lives on ─────────────────────────────────────────────
#
# Playwright's objects belong to the event loop that created them. The dashboard
# already owns a loop, and the scheduler runs on threads with none, so every
# call is funnelled onto one dedicated thread. That is what lets the browser
# survive between requests instead of dying with the request that started it.

import asyncio as _asyncio

_loop = None
_ready = None


def _boot():
    global _loop, _ready
    if _loop is not None:
        return _loop
    _loop = _asyncio.new_event_loop()
    _ready = threading.Event()

    def run():
        _asyncio.set_event_loop(_loop)
        _loop.call_soon(_ready.set)
        _loop.run_forever()

    t = threading.Thread(target=run, name="jarvis-computer", daemon=True)
    t.start()
    _ready.wait(timeout=10)
    return _loop


def _sync(coro, timeout: float = 60.0) -> Any:
    """Run a coroutine on the computer's own loop and wait for the result."""
    loop = _boot()
    fut = _asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


async def start(owner: str = "") -> dict:
    """Bring up the one computer. Idempotent — four calls, one browser."""
    with _lock:
        return await _start_async(owner)


async def _start_async(owner: str = "") -> dict:
    """Bring up the one computer. Idempotent — four calls, one browser."""
    with _lock:
        if _state["page"] is not None:
            try:
                if not _state["page"].is_closed():
                    _state["owner"] = owner or _state["owner"]
                    return {"ok": True, "already": True,
                            "workspace": str(workspace_dir()),
                            "uptime": round(time.time() - _state["started_at"], 1)}
            except Exception:
                _state["page"] = None
        try:
            import asyncio
            from playwright.async_api import async_playwright
        except Exception as e:
            return {"ok": False, "error": f"Playwright is not available: {e}"}
        try:
            # Two corrections, both found by running this rather than reading it.
            #
            # `user_data_dir` is not a `new_context` argument — it belongs to
            # `launch_persistent_context`, which is the call that keeps cookies
            # on disk. That is the whole point of a shared computer, so it is
            # the one that is used.
            #
            # And the SYNC API cannot run inside the asyncio loop the dashboard
            # is already in: Playwright raises "you are using Sync API inside
            # the asyncio loop". So this is async, and every sync-looking
            # function below is run on a dedicated thread with its own event
            # loop — which is also how the browser keeps living between calls.
            _state["pw"] = await async_playwright().start()
            args = ["--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-gpu", "--hide-scrollbars"]
            exe = None
            for cand in (os.environ.get("JARVIS_E2E_CHROMIUM"),
                         "/repl/tools/bin/chromium",
                         "/usr/bin/chromium"):
                if cand and Path(cand).exists():
                    exe = cand
                    break
            ctx = await _state["pw"].chromium.launch_persistent_context(
                str(_profile_dir()),          # <- the session that persists
                headless=True, args=args, viewport={"width": VIEW_W,
                                                   "height": VIEW_H},
                ignore_https_errors=True, **({"executable_path": exe} if exe else {}))
            _state["context"] = ctx
            _state["browser"] = None          # persistent contexts own it
            _state["page"] = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _state["page"].set_default_timeout(30_000)
            # Put back what was open. A computer that comes back to
            # about:blank is not a computer you were using five seconds ago —
            # and the profile persists cookies, so the pages still work.
            remembered = [u for u in _state.get("tabs", []) if u][:MAX_TABS]
            if remembered:
                for u in remembered[:-1]:
                    try:
                        await ctx.new_page().goto(u, wait_until="domcontentloaded",
                                                  timeout=20_000)
                    except Exception:
                        pass
                try:
                    await _state["page"].goto(remembered[-1],
                                              wait_until="domcontentloaded",
                                              timeout=25_000)
                except Exception:
                    pass
            _state["started_at"] = time.time()
            _state["owner"] = owner
            _emit("ready", {"url": _url()})
            return {"ok": True, "already": False,
                    "workspace": str(workspace_dir())}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}


def stop() -> dict:
    """Shut the computer down. Close must happen on the loop that owns the
    browser objects, so it is funnelled there like everything else."""
    ctx, br, pw = _state.get("context"), _state.get("browser"), _state.get("pw")
    # keep the addresses, drop the pages: the profile keeps the cookies, and
    # the next wake reopens these.
    try:
        kept = []
        for pg in (ctx.pages if ctx is not None else []):
            u = str(pg.url or "")
            if u and not u.startswith("about:") and u not in kept:
                kept.append(u)
        _state["tabs"] = kept[:MAX_TABS]
    except Exception:
        pass
    async def close_all():
        for o in (ctx, br):
            if o is not None:
                try:
                    await o.close()
                except Exception:
                    pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
    try:
        if _loop is not None:
            _sync(close_all(), timeout=20)
    except Exception:
        pass
    for k in ("context", "browser", "pw", "page"):
        _state[k] = None
    _secrets.clear()
    return {"ok": True}


def status() -> dict:
    p = _state.get("page")
    up = False
    try:
        up = p is not None and not p.is_closed()
    except Exception:
        up = False
    return {"up": up, "url": _url() if up else "",
            "title": _title() if up else "",
            "error": _state.get("error") or "",
            "errors": int(_state.get("errors") or 0),
            "owner": _state.get("owner") or "",
            "tabs": list(_state.get("tabs", []))[:MAX_TABS],
            "uptime": round(time.time() - _state["started_at"], 1) if up else 0,
            "handover": _state.get("handover") or "",
            "steps": len(_state.get("steps") or []),
            "workspace": str(workspace_dir()),
            "profile": str(_profile_dir()),
            "shared": True}


def _url() -> str:
    try:
        return _sync(_page().url, timeout=15)
    except Exception:
        return ""


# ── the step log and the live view ────────────────────────────────────────────

def _emit(kind: str, payload: dict) -> None:
    for cb in list(_watchers):
        try:
            cb(kind, payload)
        except Exception:
            pass


def subscribe(cb: Callable[[str, dict], None]) -> Callable[[], None]:
    _watchers.append(cb)
    return lambda: _watchers.remove(cb) if cb in _watchers else None


def _step(action: str, detail: str, ok: bool = True, shot: str = "") -> None:
    row = {"at": time.time(), "action": action, "detail": detail[:300],
           "ok": ok}
    steps = _state.setdefault("steps", [])
    steps.append(row)
    del steps[:-300]
    _emit("step", {**row, "shot": shot})


def steps(limit: int = 40) -> list[dict]:
    return list(_state.get("steps") or [])[-limit:]


def _shot(quality: int = 45) -> str:
    try:
        raw = _sync(_page().screenshot(type="jpeg", quality=quality,
                                        timeout=15_000), timeout=25)
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def frame() -> dict:
    """One frame of the live view."""
    if _status_up() is False:
        return {"up": False}
    return {"up": True, "url": _url(), "shot": _shot(),
            "handover": _state.get("handover") or "",
            "title": _title()}


def _status_up() -> bool:
    try:
        return _state["page"] is not None and not _state["page"].is_closed()
    except Exception:
        return False


def _title() -> str:
    try:
        return _sync(_page().title(), timeout=15)
    except Exception:
        return ""


# ── take-over: the human drives, the bot waits ──────────────────────────────

def handover(mode: str = "user", note: str = "") -> dict:
    """Hand the keyboard to a human, or take it back.

    This is the answer to a CAPTCHA, a 2FA prompt, or any page that has decided
    it wants a person. While it is set, every bot-initiated input is refused
    rather than queued — a bot typing into a page while you are mid-CAPTCHA is
    how you fail one.
    """
    m = str(mode or "").lower()
    with _lock:
        _state["handover"] = "user" if m in ("user", "take", "takeover", "1") else ""
    _step("handover", f"{_state.get('handover') or 'bot'} has control"
                      + (f" — {note}" if note else ""), True, _shot(30))
    return {"ok": True, "handover": _state.get("handover") or ""}


def _blocked() -> bool:
    return bool(_state.get("handover") == "user")


# ── the secret vault ─────────────────────────────────────────────────────────
#
# In memory only, on purpose. Grok Bot says a secure secret request is masked
# and "not added to the conversation"; storing it to disk would make it survive
# a restart, which is a worse property than losing it. A password typed here
# lasts until the computer stops, and the automation is what writes it into the
# page — the model never sees the value.

def put_secret(key: str, value: str) -> dict:
    k = str(key or "").strip().lower()[:40]
    v = str(value or "")
    if not k:
        return {"ok": False, "error": "give it a name, like 'github'"}
    if not v:
        return {"ok": False, "error": "no value"}
    _secrets[k] = v
    return {"ok": True, "key": k, "length": len(v),
            "note": "held in memory only; never enters the transcript"}


#: how a caller asks for a held secret: "secret:github" or "{github}". Explicit
#: on purpose. If a bare word were treated as a secret name, then forgetting the
#: secret and retyping its name would silently type the NAME into the password
#: box — a leak that looks like success. An explicit marker fails loudly instead.
_SECRET_REF = re.compile(r"^(?:secret:|\{)([a-z0-9_.-]{1,40})\}?$", re.I)


def take_secret(key: str) -> str:
    return _secrets.get(str(key or "").strip().lower(), "")


def resolve_secret(value: str) -> tuple[str, bool, str]:
    """(text_to_type, was_a_secret, problem)

    A plain value is literal text. "secret:github" is looked up in the vault,
    and a name that is not there is an ERROR rather than a literal — the caller
    asked for a secret and did not get one, which must never look like success.
    """
    raw = str(value or "")
    m = _SECRET_REF.match(raw.strip())
    if not m:
        return raw, False, ""
    name = m.group(1).lower()
    found = take_secret(name)
    if not found:
        return "", True, (f"I have no secret called \"{name}\", so I typed "
                          f"nothing. Add it in the vault first.")
    return found, True, ""


def secret_keys() -> list[str]:
    return sorted(_secrets)


def forget_secret(key: str) -> dict:
    _secrets.pop(str(key or "").strip().lower(), None)
    return {"ok": True}


# ── the actions a bot takes at the computer ───────────────────────────────────

def _wake(bot: str = "") -> None:
    """Every action wakes the computer first. A bot told to fill in a form has
    no business being told it has no computer — it just has one, the same way
    a person would find one."""
    if _status_up() is False:
        _sync(start(bot), timeout=90)


def goto(url: str, bot: str = "") -> dict:
    if _blocked():
        return _refuse("navigate", "you have control of the computer")
    _wake(bot)
    u = str(url or "").strip()
    if not u:
        return {"ok": False, "error": "no url"}
    if not u.startswith(("http://", "https://", "about:", "file:")):
        u = "https://" + u
    try:
        _sync(_page().goto(u, wait_until="domcontentloaded"), timeout=60)
    except Exception as e:
        _note_error(f"could not open {u}: {type(e).__name__}")
        _step("goto", u, False, _shot(30))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    _clear_error()
    text = _text(2000)
    _step("goto", u, True, _shot())
    return {"ok": True, "url": _url(), "title": _title(), "text": text}


def _note_error(msg: str) -> None:
    """Remember the last real failure, so status() can be honest about it.

    Without this the computer looks perfectly healthy while every click times
    out, and any surface that reports on it — the welcome ceremony included —
    has nothing to go on but the absence of a signal.
    """
    _state["error"] = str(msg)[:160]
    _state["errors"] = int(_state.get("errors") or 0) + 1


def _clear_error() -> None:
    _state["error"] = ""


def _page():
    p = _state.get("page")
    if p is None:
        raise RuntimeError("the computer is not running")
    return p


def _text(limit: int = 2000) -> str:
    try:
        t = _sync(_page().inner_text("body", timeout=8_000), timeout=20)
    except Exception:
        return ""
    return re_sub(str(t))[:limit]


def re_sub(s: str) -> str:
    import re
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", s or "")).strip()


def read(limit: int = 2500) -> dict:
    """The page in front of you, as words."""
    _wake("")
    return {"ok": True, "url": _url(), "title": _title(),
            "text": _text(limit)}


def click(selector: str = "", x: int = 0, y: int = 0, bot: str = "") -> dict:
    if _blocked():
        return _refuse("click", "you have control of the computer")
    _wake(bot)
    try:
        if selector:
            _sync(_page().click(selector, timeout=12_000), timeout=25)
            what = selector
        else:
            _sync(_page().mouse.click(x, y), timeout=25)
            what = f"({x},{y})"
        _sync(_page().wait_for_timeout(450), timeout=10)
    except Exception as e:
        _step("click", f"{what if selector else (x, y)}", False, _shot(30))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    _step("click", what, True, _shot())
    return {"ok": True, "url": _url(), "title": _title()}


def fill(selector: str, value: str, bot: str = "") -> dict:
    """Type into a field. `value` may be the NAME of a held secret, in which
    case the real value never passes through the caller."""
    if _blocked():
        return _refuse("fill", "you have control of the computer")
    _wake(bot)
    v, was_secret, problem = resolve_secret(value)
    if problem:
        _step("fill", f"{selector} (secret refused)", False)
        return {"ok": False, "blocked": True, "error": problem}
    try:
        _sync(_page().fill(selector, v, timeout=12_000), timeout=25)
    except Exception as e:
        _note_error(f"could not fill {selector}: {type(e).__name__}")
        _step("fill", selector, False, _shot(30))
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    # the log records the field, never the value
    _step("fill", f"{selector}" + (" (from a held secret)" if was_secret else ""),
          True, _shot())
    return {"ok": True}


def type_text(text: str, bot: str = "") -> dict:
    if _blocked():
        return _refuse("type", "you have control of the computer")
    _wake(bot)
    out, was_secret, problem = resolve_secret(text)
    if problem:
        _step("type", "secret refused", False)
        return {"ok": False, "blocked": True, "error": problem}
    text = out
    try:
        _sync(_page().keyboard.type(str(text or ""), delay=18), timeout=60)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    _step("type", (f"{len(str(text or ''))} characters"
                   + (" (from a held secret)" if was_secret else "")),
          True, _shot(30))
    return {"ok": True}


def press(key: str, bot: str = "") -> dict:
    if _blocked():
        return _refuse("press", "you have control of the computer")
    _wake(bot)
    try:
        _sync(_page().keyboard.press(str(key or "Enter")), timeout=25)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    _step("press", str(key), True, _shot())
    return {"ok": True}


def scroll(amount: int = 400, bot: str = "") -> dict:
    if _blocked():
        return _refuse("scroll", "you have control of the computer")
    _wake(bot)
    try:
        _sync(_page().mouse.wheel(0, int(amount)), timeout=20)
        _sync(_page().wait_for_timeout(250), timeout=10)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    _step("scroll", str(amount), True, _shot(30))
    return {"ok": True}


def back(bot: str = "") -> dict:
    # "Back" is input too. A bot navigating away while a human is mid-2FA is
    # the same failure as a bot typing, so take-over has to stop it here too.
    if _blocked():
        return _refuse("go back", "you have control of the computer")
    _wake(bot)
    try:
        _sync(_page().go_back(wait_until="domcontentloaded"), timeout=45)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    _step("back", "", True, _shot(30))
    return {"ok": True, "url": _url(), "title": _title()}


def _refuse(action: str, why: str) -> dict:
    return {"ok": False, "blocked": True,
            "error": f"You have control of the computer right now, so I did not "
                     f"try to {action}. Hand control back when you are done."}


def run_js(script: str, bot: str = "") -> dict:
    """Escape hatch for a page whose selectors we do not know. Refused while
    the human has control, and logged, because it is the least inspectable
    thing here."""
    if _blocked():
        return _refuse("run script", "you have control of the computer")
    _wake(bot)
    try:
        val = _sync(_page().evaluate(str(script or "")), timeout=30)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:180]}
    _step("js", str(script or "")[:120], True, _shot(30))
    return {"ok": True, "value": val}


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, url: str = "", selector: str = "",
         text: str = "", key: str = "", amount: int = 400, x: int = 0,
         y: int = 0, bot: str = "", value: str = "", limit: int = 2000,
         mode: str = "") -> str:
    """Prose back, and honest about a computer that is not there."""
    a = str(action or "").strip().lower()
    try:
        if a in ("", "status"):
            st = status()
            if not st["up"]:
                return ("The computer is not running. Start it first — it is "
                        "off until something asks for it.")
            return (f"Up {st['uptime']}s · {st['title'] or st['url']} · "
                    f"{st['steps']} actions logged"
                    + (f" · YOU HAVE CONTROL" if st["handover"] else "")
                    + f" · workspace {st['workspace']}")

        if a in ("start", "boot", "wake"):
            r = _sync(start(bot), timeout=90)
            return (f"The computer is up — workspace {r.get('workspace')}."
                    if r.get("ok") else f"It did not start: {r.get('error')}")

        if a in ("stop", "shutdown", "sleep"):
            return "Computer stopped." if stop().get("ok") else "Could not stop it."

        if a in ("go", "goto", "open", "visit", "browse"):
            r = goto(url, bot)
            if not r.get("ok"):
                return f"I could not open that: {r.get('error')}"
            body = str(r.get("text") or "")[:limit]
            return (f"At {r.get('url')}\nTitle: {r.get('title')}\n\n{body}")

        if a in ("read", "text", "page"):
            if _status_up() is False:
                return "The computer is not running."
            body = read(limit).get("text") or ""
            _step("read", f"{len(body)} characters", True, "")
            return body or "(the page had no readable text)"

        if a == "click":
            r = click(selector, x, y, bot)
            return (f"Clicked. Now at {r.get('url')}"
                    if r.get("ok") else f"I could not click: {r.get('error')}")

        if a in ("fill", "type_into"):
            r = fill(selector, text, bot)
            return (f"Filled {selector}." if r.get("ok")
                    else f"I could not fill that: {r.get('error')}")

        if a in ("type", "keyboard"):
            r = type_text(text, bot)
            return "Typed." if r.get("ok") else f"I could not type: {r.get('error')}"

        if a in ("press", "key", "enter"):
            r = press(key or "Enter", bot)
            return f"Pressed {key or 'Enter'}." if r.get("ok") else str(r.get("error"))

        if a == "scroll":
            r = scroll(amount, bot)
            return "Scrolled." if r.get("ok") else str(r.get("error"))

        if a == "back":
            r = back(bot)
            return f"Back at {r.get('url')}" if r.get("ok") else str(r.get("error"))

        if a in ("handover", "takeover", "control", "hand over"):
            want = str(mode or text or "user").strip().lower()
            if want not in ("user", "bot"):
                return ("I did not change anything: hand over needs mode=user "
                        "(the user takes the keyboard) or mode=bot.")
            r = handover(want, str(value or url or ""))
            who = "you have" if r["handover"] else "the bot has"
            return (f"Done — {who} control of the computer. Every action is "
                    f"refused until it is handed back."
                    if r["handover"] else
                    f"Done — the bot has control of the computer again.")

        if a in ("secret", "secrets", "vault"):
            keys = secret_keys()
            if not keys:
                return ("No secrets are stored. The user adds them in the "
                        "vault panel; I never receive their values.")
            return ("Secrets I may use (names only — I cannot read the "
                    "values): " + ", ".join(keys)
                    + ".\nUse one as secret:NAME when filling a field.")

        if a in ("screenshot", "shot", "see", "look"):
            st = status()
            if not st["up"]:
                return "The computer is off, so there is nothing to look at."
            f = frame()
            return (f"The screen shows {st['title'] or st['url'] or 'a blank page'}"
                    f" at {st['url']}. The live panel carries the picture; "
                    f"action=read gets me the words on it.")

        if a == "steps":
            rows = steps(limit=40)
            return ("\n".join(f"- {r['action']}: {r['detail']}"
                               + ("" if r["ok"] else "  (failed)")
                               for r in rows) or "Nothing done yet.")

        return ("Unknown action. Use status / start / stop / go / read / "
                "screenshot / click / fill / type / press / scroll / back / "
                "hand over / secrets / steps.")
    except Exception as e:
        return f"computer: {type(e).__name__}: {e}"[:200]