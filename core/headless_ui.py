"""
core/headless_ui.py — the dashboard's stand-in for the Qt UI (server mode).

Mirrors the JarvisUI surface main.py actually touches, so JarvisLive runs
unchanged: every write_log/set_state/show_* call lands here instead of a
widget, and anything worth seeing is broadcast to the FastAPI dashboard as
a WebSocket message the app.html client already understands.

Design rules:
  * No Qt, no sounddevice — safe on a box with no display and no audio.
  * `_win` returns self (with a `_ready` flag) so main.py's
    `self.ui._win._ready` reconfig wait works unchanged.
  * Broadcasts go through asyncio.run_coroutine_threadsafe on the loop
    given to bind_dashboard(), so this class is safe from any thread.
  * write_log prints everything (the console is the operator's log) but
    only broadcasts SYS/ERR/NET-style lines — chat turns are already
    broadcast by _receive_audio and must not appear twice.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from memory.config_manager import get_assistant_name, is_configured


class HeadlessUI:
    def __init__(self):
        self._ready: bool = is_configured()
        self._muted: bool = False
        self._state: str = "SLEEPING"
        self._dash = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._assistant_name: str = get_assistant_name()
        self._lvl_t: float = 0.0

        # Callback slots — assigned by JarvisLive.__init__ / run(), exactly
        # like the MainWindow attributes they replace. Plain attributes so
        # the existing `self.ui.on_* = ...` assignments just work.
        self.on_push_to_talk = None
        self.ptt_hold = None
        self.on_text_command = None
        self.on_remote_clicked = None
        self.on_interrupt = None
        self.on_voice_change = None
        self.on_audio_device_change = None
        self.get_plugins = None
        self.get_plugin_settings = None
        self.request_say = None
        self.wake_is_ready = None
        self.wake_get_state = None
        self.on_wake_toggle = None
        self.on_wake_manual = None
        self.on_wake_install = None

    # ── MainWindow shim: main.py pokes ui._win._ready ──────────────────────

    @property
    def _win(self):
        return self

    @property
    def ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        """Called after a successful /api/save-key — release the connect wait."""
        self._ready = True
        self._assistant_name = get_assistant_name()
        self._bcast({"type": "setup", "done": True})

    def snapshot(self) -> dict:
        """What the assistant is doing, in one dict, for the health route.

        Read-only and deliberately dull: a state string and whether anything is
        listening. It exists because "the dashboard loads but nothing works" has
        three quite different causes — the assistant is asleep, the assistant
        never started, or the browser is not authenticated — and none of them
        look different from the outside.
        """
        return {"state": str(self._state),
                "ready": bool(self._ready),
                "listening": self._state == "LISTENING",
                "speaking": self._state in ("SPEAKING", "PROCESSING")}

    def bind_dashboard(self, dash, loop) -> None:
        self._dash = dash
        self._loop = loop
        # Force an initial status so the pill is never stuck on "Connecting"
        # (set_state no-ops when the value is unchanged).
        try:
            self._bcast({"type": "status",
                         "state": "sleeping" if self._state == "SLEEPING" else "active",
                         "raw": self._state.upper()})
        except Exception:
            pass

    # ── broadcast plumbing ─────────────────────────────────────────────────

    def _bcast(self, msg: dict) -> None:
        dash, loop = self._dash, self._loop
        if dash is None or loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(dash.broadcast(msg), loop)
        except Exception:
            pass

    # ── properties main.py reads/writes ────────────────────────────────────

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, v: bool) -> None:
        v = bool(v)
        if v != self._muted:
            self._muted = v
            self._bcast({"type": "mute", "muted": v})

    @property
    def current_file(self) -> str | None:
        return None

    @property
    def assistant_name(self) -> str:
        return self._assistant_name

    # ── log & state ────────────────────────────────────────────────────────

    def write_log(self, text: str) -> None:
        t = str(text)
        try:
            print(t, flush=True)
        except Exception:
            pass
        # Chat turns ("You: …", "JARVIS: …", "[Web]: …") are already
        # broadcast by _receive_audio / appended client-side — only status
        # lines go to the dashboard feed.
        if t.startswith(("SYS:", "ERR:", "NET:", "FILE:",
                         "[Actions]", "[Plugins]", "[Wake]")):
            self._bcast({"type": "sys", "text": t})

    def set_state(self, state: str) -> None:
        s = str(state or "")
        if s == self._state:
            return
        self._state = s
        # app.html setStatus: active/sleeping/fallback; raw feeds the HUD canvas.
        up = s.upper()
        mapped = "sleeping" if up == "SLEEPING" else "active"
        self._bcast({"type": "status", "state": mapped, "raw": up})

    def set_audio_level(self, level: float) -> None:
        # ~20 Hz max — enough for the HUD waveform without flooding the feed.
        now = time.time()
        if now - self._lvl_t < 0.05:
            return
        self._lvl_t = now
        try:
            v = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            return
        self._bcast({"type": "audio_level", "v": v})

    def wait_for_api_key(self) -> None:
        while not self._ready:
            time.sleep(0.1)

    # ── confirm gate (core/confirm.py binds these) ─────────────────────────

    def show_confirm(self, title: str, detail: str) -> None:
        self._bcast({"type": "confirm",
                     "title": str(title)[:120],
                     "detail": str(detail)[:300]})
        # The dashboard banner is only useful when someone is looking at it.
        # The phone is not, so the same gate goes out as a push with
        # Accept / Reject buttons that resolve this very confirmation.
        # Failure here is silent by design — the on-screen card still works.
        try:
            from core import push as _push
            if _push.sub_count():
                import threading
                threading.Thread(
                    target=lambda: _push.notify_approval(
                        str(title)[:80], str(detail)[:180],
                        url="/?confirm=1"),
                    daemon=True, name="push-approval").start()
        except Exception:
            pass

    def hide_confirm(self) -> None:
        self._bcast({"type": "confirm", "clear": True})

    # ── content / review / quiz panels ─────────────────────────────────────

    def show_content(self, title: str, text: str) -> None:
        self._bcast({"type": "content",
                     "title": str(title)[:48],
                     "text": str(text)[:4000]})

    def show_quiz(self, topic: str, questions, grade=None) -> None:
        n = len(questions or [])
        self._bcast({"type": "content",
                     "title": str(topic or "Quiz"),
                     "text": f"{n} question quiz ready — answer in chat."})

    def hide_quiz(self) -> None:
        pass

    def show_review(self, title: str, summary: str, findings, unclear=None) -> None:
        n = len(findings or [])
        self._bcast({"type": "content",
                     "title": str(title or "Review")[:48],
                     "text": f"{summary or ''}\n\n{n} finding(s).".strip()[:4000]})

    # ── camera / avatar / phone (no-ops — no local hardware in server mode)

    def show_camera_frame(self, img_bytes: bytes) -> None:
        pass

    def start_camera_stream(self) -> None:
        pass

    def stop_camera_stream(self) -> None:
        pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        pass

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        pass

    def notify_phone_connected(self) -> None:
        pass  # _on_phone_connected already write_log()s the SYS line

    def start_speaking(self) -> None:
        self.set_state("SPEAKING")

    def stop_speaking(self) -> None:
        if not self._muted:
            self.set_state("LISTENING")

    # ── API key setup overlay ──────────────────────────────────────────────

    def prompt_reconfig(self) -> None:
        self._ready = False
        self._bcast({"type": "setup"})
        self.write_log("SYS: API key required — enter it in the dashboard.")
