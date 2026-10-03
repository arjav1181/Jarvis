"""Telegram remote for Jaarvis — control from your phone, anywhere.

Long-polls the Telegram Bot API with plain urllib (zero new dependencies) on
its own thread. Pairs once with a code, then:
  incoming message from the paired chat -> main._on_text_command(text)
  assistant reply (turn_complete text)   -> send_message back to the chat

Security: the bot ignores every chat except the paired one; the pairing code
is single-use. Token + code live in config/api_keys.json (git-ignored), never
in code. Works from the desktop app AND from any always-on machine (VPS),
which is the "second body" setup: same code, Telegram as the UX.
"""

import json
import threading
import time
import urllib.parse
import urllib.request

_API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, params: dict, timeout: int = 40):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        _API.format(token=token, method=method), data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class TelegramRemote:
    def __init__(self, *, on_text, on_reply_logged=None):
        self._on_text = on_text
        self._log = on_reply_logged
        self._token = ""
        self._chat_id = ""
        self._pair_code = ""
        self._offset = 0
        self._thread = None
        self._stop = threading.Event()

    # -- config ---------------------------------------------------------
    def configure(self, *, token: str, chat_id: str = "",
                  pair_code: str = "") -> None:
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._pair_code = (pair_code or "").strip()

    @property
    def active(self) -> bool:
        return bool(self._token and self._chat_id)

    # -- lifecycle ------------------------------------------------------
    def start(self) -> bool:
        """Begin long-polling. True if polling (paired or awaiting pairing)."""
        if not self._token or (self._thread and self._thread.is_alive()):
            return False
        try:
            me = _call(self._token, "getMe", {}, timeout=15)
            if not me.get("ok"):
                return False
        except Exception:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, daemon=True,
                                        name="jaarvis-telegram")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    # -- outbound -------------------------------------------------------
    def send(self, text: str) -> bool:
        if not self.active:
            return False
        try:
            r = _call(self._token, "sendMessage",
                      {"chat_id": self._chat_id,
                       "text": text[:4000]}, timeout=20)
            return bool(r.get("ok"))
        except Exception:
            return False

    # -- inbound --------------------------------------------------------
    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                r = _call(self._token, "getUpdates",
                          {"offset": self._offset, "timeout": 30,
                           "allowed_updates": ["message"]}, timeout=45)
            except Exception:
                time.sleep(2)
                continue
            for upd in r.get("result", []):
                self._offset = max(self._offset, upd.get("update_id", 0) + 1)
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                cid = str(chat.get("id", ""))
                if self._chat_id:
                    if cid != self._chat_id:
                        continue
                    try:
                        self._on_text(text)
                    except Exception:
                        pass
                else:
                    # Unpaired: only the pairing code is accepted, once.
                    if self._pair_code and text == self._pair_code:
                        self._chat_id = cid
                        self._pair_code = ""
                        try:
                            self._on_paired(cid)
                        except Exception:
                            pass
                        self.send("The remote is active, sir. "
                                  "You can now control this machine from your phone.")
                    # Anything else from strangers: silence (no oracle).

    # -- hook filled by main.py -----------------------------------------
    def _on_paired(self, chat_id: str) -> None:
        """Persist the pairing (overridden by main loop)."""
