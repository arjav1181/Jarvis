"""core/voice.py — the voice loop, as a state machine you can look at.

Real JARVIS is not "a microphone and a speaker". It is a loop with states, and
almost everything that feels wrong about a voice assistant is a bug in the state
transitions: it talks over you, it loses the first word, it keeps talking after
you have stopped caring. So the states are explicit here, every transition is
recorded, and barge-in is a first-class operation rather than an accident.

    idle ──wake/push──▶ listening ──silence──▶ transcribing
      ▲                     │                      │
      │                     └──barge-in──────────┐ │
      │                                            ▼ │
      └──interrupt── speaking ◀──tts chunks──── thinking
                            └──end/error──▶ idle

Three decisions worth stating:

  * **Barge-in cancels the audio, not the answer.** When you talk over the
    speech, the audio stops immediately and the turn is *kept* — a half-spoken
    answer is still an answer, and throwing it away makes the assistant feel
    deaf. The transcript is kept and flagged so the caller can re-ask if it was
    cut off mid-word.

  * **Transcription is the client's job by default.** The browser's speech
    recognition gives streaming partials and near-zero latency for free; running
    Whisper in the Space would add seconds and a model download. The server-side
    engines in `core/stt.py` are still wired in for clients that push raw audio
    instead — the phone over a slow link, or a limb with a mic.

  * **Nothing here talks to a model.** The loop produces a transcript and a
    state; the existing model dispatch answers it. Voice is a transport, not a
    brain, which is why it can be tested with no API key at all.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Optional

from core.data_paths import data_root

STATES = ("idle", "listening", "transcribing", "thinking", "speaking")
SILENCE_MS = 1100          # end of utterance
BARGE_WINDOW = 6.0         # seconds of speech still "in flight" after a wake

_lock = threading.RLock()
_path = None
_cache: Optional[dict] = None


def _file():
    global _path
    if _path is None:
        _path = data_root() / "voice.json"
    return _path


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            d = json.loads(_file().read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d.setdefault("turns", [])
        d.setdefault("settings", {
            "wake_word": False, "auto_listen": False, "barge_in": True,
            "silence_ms": SILENCE_MS, "tts": True, "stt": "browser",
        })
        _cache = d
        return d


def _save() -> None:
    with _lock:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)


def reset() -> None:
    global _cache, _path
    with _lock:
        _cache = None
        _path = None


# ── the live session ─────────────────────────────────────────────────────────

class VoiceSession:
    """One conversation's voice state. The dashboard holds one of these per
    connected client; the server holds a mirror for scheduled speak."""

    def __init__(self, on_transcript: Optional[Callable[[str, dict], Any]] = None,
                 on_speak: Optional[Callable[[str], Any]] = None):
        self.id = "v-" + uuid.uuid4().hex[:8]
        self.state = "idle"
        self.partial = ""
        self.turns: list[dict] = []
        self.speaking = False
        self.speak_id = ""
        self.born = time.time()
        self.last_activity = self.born
        self.cancelled = 0
        self.barge_ins = 0
        self._on_transcript = on_transcript
        self._on_speak = on_speak
        self._heard: list[float] = []

    # ── transitions ──────────────────────────────────────────────────────────

    def _to(self, state: str, **extra: Any) -> dict:
        if state not in STATES:
            raise ValueError(f"'{state}' is not a voice state")
        old, self.state = self.state, state
        self.last_activity = time.time()
        rec = {"id": self.id, "from": old, "to": state, "at": self.last_activity,
               **extra}
        self.turns.append(rec)
        self.turns = self.turns[-200:]
        return rec

    def start_listening(self, *, wake: bool = False) -> dict:
        """Push-to-talk or wake word. `wake` is recorded so the transcript can be
        told apart from a deliberate press — a wake word is much more likely to
        be a false positive and deserves a 'did you mean' instead of a confident
        answer to a half-heard word."""
        if self.state == "speaking" and self.barge_in:
            self.interrupt("barge_in")
        return self._to("listening", wake=bool(wake), partial="")

    def partial_text(self, text: str) -> dict:
        """Streaming partial from the browser. Kept, not committed."""
        self.partial = str(text or "")
        self.last_activity = time.time()
        return {"state": self.state, "partial": self.partial}

    def audio_level(self, level: float) -> dict:
        """Client reports a mic level. Used for two things: knowing the user is
        still talking, and detecting barge-in while we speak."""
        lv = float(level or 0.0)
        now = time.time()
        if lv > 0.08:
            self._heard.append(now)
            self._heard = [t for t in self._heard if now - t < 3.0]
            if self.state == "speaking" and self.barge_in and now - self.speaking_since() > 0.35:
                self.interrupt("barge_in")
        return {"state": self.state, "heard": len(self._heard),
                "barge": bool(self.speaking and self._heard)}

    def speaking_since(self) -> float:
        for r in reversed(self.turns):
            if r.get("to") == "speaking":
                return float(r.get("at") or self.born)
        return self.born

    def commit(self, text: str, *, source: str = "browser",
               confidence: float = 0.0) -> dict:
        """The utterance is over. This is the only place a transcript is born."""
        said = str(text or "").strip()
        if not said:
            return self._to("idle", empty=True)
        interrupted = self.cancelled > 0
        rec = {"id": self.id, "text": said, "source": source,
               "confidence": float(confidence or 0), "at": time.time(),
               "interrupted": interrupted}
        self.partial = ""
        self.cancelled = 0
        self._to("transcribing", chars=len(said), source=source)
        self._to("thinking", text=said[:200])
        if self._on_transcript:
            try:
                self._on_transcript(said, rec)
            except Exception:
                pass
        with _lock:
            d = _load()
            d["turns"].append({**rec, "chars": len(said)})
            d["turns"] = d["turns"][-200:]
            _save()
        return {**rec, "state": self.state}

    def start_speaking(self, text: str) -> dict:
        if self.state == "speaking" and self.barge_in:
            self.interrupt("barge_in")
        rec = self._to("speaking", chars=len(str(text or "")),
                       speak_id="sp-" + uuid.uuid4().hex[:6])
        self.speak_id = rec.get("speak_id", "")
        self.speaking = True
        if self._on_speak:
            try:
                self._on_speak(str(text or ""))
            except Exception:
                pass
        return rec

    def end_speaking(self) -> dict:
        self.speaking = False
        self.speak_id = ""
        return self._to("idle", finished=True)

    def interrupt(self, why: str = "user") -> dict:
        """Stop talking now. The in-flight turn is kept, not thrown away."""
        self.cancelled += 1
        if self.speaking:
            self.barge_ins += 1
        was = self.speaking
        self.speaking = False
        self.speak_id = ""
        rec = self._to("listening" if was else "idle", interrupt=why)
        return rec

    def error(self, message: str) -> dict:
        return self._to("idle", error=str(message)[:200])

    def snapshot(self) -> dict:
        return {"id": self.id, "state": self.state, "partial": self.partial,
                "speaking": self.speaking, "barge_ins": self.barge_ins,
                "cancelled": self.cancelled, "turns": len(self.turns),
                "age": int(time.time() - self.born),
                "idle": int(time.time() - self.last_activity)}

    # ── settings, shared by every session ────────────────────────────────────

    @property
    def settings(self) -> dict:
        return _load()["settings"]

    @property
    def barge_in(self) -> bool:
        return bool(self.settings.get("barge_in", True))

    def set(self, **kw: Any) -> dict:
        with _lock:
            s = _load()["settings"]
            for k, v in kw.items():
                if k in s:
                    s[k] = v
            if "silence_ms" in kw:
                s["silence_ms"] = max(400, min(int(kw["silence_ms"] or 0), 5000))
            _save()
            return dict(s)


# ── server-side transcription, for clients that push audio ───────────────────

_stt = None


def stt_available() -> bool:
    """Can we transcribe on the server *if asked to*? Import-only, so this is
    cheap and never touches the network."""
    if _stt is not None:
        return True
    try:
        from core.stt import WhisperSTT  # noqa: F401
        return True
    except Exception:
        return False


def stt():
    """Lazily bring up the offline engine. Absent, and the client-side path is
    the only one — which is the normal case."""
    global _stt
    if _stt is not None:
        return _stt
    try:
        from core.stt import WhisperSTT
        _stt = WhisperSTT()
    except Exception:
        _stt = None
    return _stt


def transcribe_audio(audio: bytes, sample_rate: int = 16000) -> str:
    """Bytes in, text out. Raises rather than guessing, so the caller can tell
    the user their audio was not understood instead of acting on nonsense."""
    if not audio:
        return ""
    eng = stt()
    if eng is None:
        raise RuntimeError("no local transcription engine — the browser is "
                           "doing the speech recognition")
    import numpy as np
    samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        return (eng.transcribe(samples) or "").strip()
    except Exception as e:
        raise RuntimeError(f"transcription failed: {e}") from None


# ── the surface the dashboard and the tools use ──────────────────────────────

def status() -> dict:
    with _lock:
        d = _load()
    turns = d.get("turns") or []
    return {
        "states": list(STATES),
        "settings": d["settings"],
        "turns": len(turns),
        "words": sum(int(t.get("chars") or 0) for t in turns),
        "last": turns[-1] if turns else None,
        # deliberately NOT stt(): instantiating Whisper downloads and loads a
        # model, and a status endpoint must never do that as a side effect
        "local_stt": stt_available(),
        "wake_word_installed": _wake_ready(),
    }


def _wake_ready() -> bool:
    try:
        from core import wake_word as W
        return bool(W.is_ready())
    except Exception:
        return False


def set_settings(**kw: Any) -> dict:
    with _lock:
        s = _load()["settings"]
        for k, v in kw.items():
            if k in s:
                s[k] = v
        if "silence_ms" in kw:
            s["silence_ms"] = max(400, min(int(kw["silence_ms"] or 0), 5000))
        _save()
        return dict(s)


def history(limit: int = 20) -> list[dict]:
    with _lock:
        return list(reversed(_load().get("turns") or []))[:max(1, min(int(limit or 20), 200))]


def new_session(**kw: Any) -> VoiceSession:
    return VoiceSession(**kw)


def describe() -> str:
    st = status()
    s = st["settings"]
    bits = [f"voice: {st['turns']} turn(s)"]
    if s.get("wake_word"):
        bits.append("wake word on" if st["wake_word_installed"]
                    else "wake word wanted but not installed")
    if s.get("barge_in"):
        bits.append("barge-in on")
    bits.append("speech recognition: "
                + ("on the server" if st["local_stt"] else "in the browser"))
    return ", ".join(bits) + "."
