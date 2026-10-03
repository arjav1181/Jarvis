"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float
    #: Set instead of `run` when the caller is an async tool loop that is
    #: *waiting* for the decision. resolve() then signals the future rather than
    #: calling a thunk, and the tool call returns a real FunctionResponse.
    on_resolve: Optional[Callable[[bool], None]] = None


# A queue, not a single slot. Phase 5 asks "what is waiting on me?", which a
# single pending cannot answer — two risky things can be waiting at once, and
# the second must not overwrite the first. The HEAD is the card on the HUD; the
# rest are the inbox. The public API (request/resolve/pending_title) is
# unchanged, so every existing caller keeps working.
_queue: "list[_Pending]" = []
_lock = threading.Lock()


def _head() -> Optional[_Pending]:
    return _queue[0] if _queue else None


def _prune_locked() -> None:
    """Drop anything that has been waiting past the timeout."""
    now = time.monotonic()
    for p in list(_queue):
        if now - p.at > TIMEOUT_SECONDS:
            _log(f"SYS: Confirmation expired \u2014 {p.title}")
    _queue[:] = [p for p in _queue if now - p.at <= TIMEOUT_SECONDS]

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim."""
    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    with _lock:
        _prune_locked()
        queued = len(_queue)
        _queue.append(_Pending(key=key, title=title, detail=detail,
                               run=run, at=time.monotonic()))
        # only the head drives the card; a queued item must not replace one
        # the user is already looking at
        head = _head()

    if queued == 0:
        try:
            _show_cb(head.title, head.detail)
        except Exception as e:
            with _lock:
                if head in _queue:
                    _queue.remove(head)
            return f"Could not ask for confirmation: {e}. Nothing was done."

    _log(f"SYS: Awaiting confirmation — {title}")
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {title}. "
        f"Say ONE short sentence in the user's own language telling them you need "
        f"them to confirm it on the HUD before you do it. Do not claim it is done."
    )


def resolve(accepted: bool) -> None:
    """Called by the UI when the user presses CONFIRM or CANCEL.

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    with _lock:
        _prune_locked()
        p = _queue.pop(0) if _queue else None
        nxt = _head()

    if p is None:
        return

    # swap the card for the next thing waiting, rather than leaving a dead HUD
    if _show_cb is not None and nxt is not None:
        try:
            _show_cb(nxt.title, nxt.detail)
        except Exception:
            pass
    elif _hide_cb is not None:
        try:
            _hide_cb()
        except Exception:
            pass

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Confirmation expired — {p.title}")
        return

    if p.on_resolve is not None:
        try:
            p.on_resolve(bool(accepted))
        except Exception as e:
            _log(f"ERR: {p.title} signal failed — {e}")
        return

    if not accepted:
        _log(f"SYS: Cancelled — {p.title}")
        return

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed — {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        _prune_locked()
        p = _head()
        return p.title if p else ""


def inbox() -> list[dict]:
    """Everything waiting on a human, oldest first. This is the Approvals panel."""
    with _lock:
        _prune_locked()
        return [{"key": p.key, "title": p.title, "detail": p.detail,
                 "waiting_s": round(max(0.0, time.monotonic() - p.at), 1)}
                for p in _queue]


def count() -> int:
    with _lock:
        _prune_locked()
        return len(_queue)


def reject_all() -> int:
    """Deny everything waiting. The panic button."""
    with _lock:
        n = len(_queue)
        _queue.clear()
    if n and _hide_cb is not None:
        try:
            _hide_cb()
        except Exception:
            pass
    return n


def expire() -> None:
    """Drop timed-out items and refresh the card. Called by the dashboard poll."""
    with _lock:
        had = len(_queue)
        _prune_locked()
        now = len(_queue)
        head = _head()
    if now < had:
        if head is not None and _show_cb is not None:
            try:
                _show_cb(head.title, head.detail)
            except Exception:
                pass
        elif _hide_cb is not None:
            try:
                _hide_cb()
            except Exception:
                pass


# ── the async gate ───────────────────────────────────────────────────────────

async def request_async(key: str, title: str, detail: str, loop) -> bool:
    """Wait for a human on the caller's event loop. Returns True if approved.

    The tool dispatch is async and the voice session is waiting for a
    FunctionResponse for that exact call, so the approval has to BLOCK the call
    rather than run the action later on a worker thread the way request() does.
    Otherwise the session waits forever for a response to a call that already
    returned a sentence.
    """
    import asyncio as _aio

    if _show_cb is None:
        return False

    fut = loop.create_future()

    def _set(value: bool) -> None:
        if not fut.done():
            fut.set_result(value)

    def _signal(accepted: bool) -> None:
        try:
            loop.call_soon_threadsafe(_set, accepted)
        except Exception:
            _set(accepted)

    with _lock:
        _prune_locked()
        head_empty = not _queue
        _queue.append(_Pending(key=key, title=title, detail=detail,
                               run=lambda: "", at=time.monotonic(),
                               on_resolve=_signal))
        head = _head()

    if head_empty:
        try:
            _show_cb(head.title, head.detail)
        except Exception as e:
            _log(f"ERR: could not show confirmation — {e}")
            with _lock:
                if head in _queue:
                    _queue.remove(head)
            return False

    _log(f"SYS: Awaiting confirmation — {title}")
    try:
        return bool(await _aio.wait_for(fut, TIMEOUT_SECONDS + 5))
    except _aio.TimeoutError:
        with _lock:
            if head in _queue:
                _queue.remove(head)
        _log(f"SYS: Confirmation timed out — {title}")
        return False
    except Exception:
        return False
