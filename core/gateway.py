"""
One client for an OpenAI-compatible gateway (base URL + bearer + model).

WHY THIS EXISTS
    Gemini free-tier quota runs out mid-session. When it does, two things
    need a way to keep working through any OpenAI-compatible endpoint the
    user points at (OpenAI, Azure-openai-compatible proxies, LM Studio,
    Ollama's /v1, vLLM, LiteLLM, …):

      1. AUXILIARY / TOOL CALLS — core/gemini.py tries this first for the
         FAST and SMART tiers, before its Live→REST ladder. SEARCH is
         deliberately excluded: grounded search needs grounding_metadata a
         gateway cannot return.

      2. VOICE FAILOVER — main.py's fallback loop streams STT → chat → TTS
         through here while Gemini Live is on 429-cooldown, and probes Live
         in the background to come back automatically.

STREAMING, AND WHY IT IS NOT OPTIONAL HERE
    The whole point of the failover is latency: waiting for a full reply
    before starting speech feels dead. Every entry point that can stream,
    does:

      chat_stream      SSE deltas, sentence-split as they arrive, tool-call
                       deltas accumulated until [DONE]
      transcribe_stream post-utterance stream:true — partial transcript
                       events while the file is being processed
      speak_stream     response_format=pcm body pulled in 4800-byte chunks
                       and pushed straight into audio_in_queue

    Each has a one-shot fallback for gateways that do not implement
    stream=true — the feature degrades, never breaks.

CONFIG
    All values come from memory.config_manager.get_openai_settings():
    env wins over config/api_keys.json per field. Empty base URL = disabled.

COOLDOWN
    A 429 or 5xx on an endpoint cools THAT endpoint for 60s (mirror of
    core/gemini.py's _cool). During cooldown callers skip the gateway
    instead of paying a round trip that is guaranteed to fail.
"""
from __future__ import annotations

import io
import json
import re
import time
import wave
from typing import Iterable, Iterator, Optional

import requests

from memory.config_manager import get_openai_settings

# Sentence boundary for chat_stream deltas — copied from the (dead)
# llm_client.py rather than imported: this module must not depend on it.
_SENT_END = re.compile(r'(?<=[.!?])\s+|(?<=\n)\s*\n')

_COOLDOWN_SECONDS = 60.0
_cooldown: dict[str, float] = {}

_TIMEOUT_S = 60.0


# ── settings / cooldown ──────────────────────────────────────────────────────

def settings() -> dict:
    """Resolved gateway settings (env wins)."""
    return get_openai_settings()["values"]


def enabled() -> bool:
    return bool((settings().get("openai_base_url") or "").strip())


def chat_model() -> str:
    return (settings().get("openai_model") or "").strip()


def voice_model() -> str:
    """The model to ask for SPEECH, which is not the model used for chat.

    This deliberately does NOT fall back to the chat model any more. With
    `voice_model` empty it used to resolve to whatever `openai_model` was —
    here `auto/best-coding-fast`, a coding model. Local TTS was doing the real
    work, so nobody noticed until the day local TTS failed: the fallback then
    asked a coding model to synthesise speech and failed too, taking the voice
    with it. An unset voice model now means "no gateway voice", which is a
    state that can be reported instead of a second, quieter failure.
    """
    return str(settings().get("voice_model") or "").strip()


def voice_configured() -> bool:
    return bool(enabled() and voice_model())


def _cool(path: str) -> None:
    _cooldown[path] = time.monotonic() + _COOLDOWN_SECONDS


def _cooling(path: str) -> bool:
    until = _cooldown.get(path, 0.0)
    if until and time.monotonic() < until:
        return True
    _cooldown.pop(path, None)
    return False


def _base() -> str:
    b = (settings().get("openai_base_url") or "").strip().rstrip("/")
    if b.endswith("/v1"):
        return b
    return b + "/v1"


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = (settings().get("openai_api_key") or "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _post(path: str, **kwargs) -> requests.Response:
    """POST with cooldown bookkeeping. Raises on transport error; returns the
    response regardless of status (callers decide what a 4xx means)."""
    if _cooling(path):
        raise RuntimeError(f"gateway {path} cooling down")
    try:
        r = requests.post(_base() + path, headers=_headers(),
                          timeout=kwargs.pop("timeout", _TIMEOUT_S), **kwargs)
    except requests.RequestException as e:
        _cool(path)
        raise RuntimeError(f"gateway unreachable: {e}") from e
    # 404 has shown up as an HTML edge page from omniroute (wrong path or
    # cold worker) — cooling stops the assistant from hammering a dead route.
    if r.status_code == 429 or r.status_code == 404 or r.status_code >= 500:
        _cool(path)
        raise RuntimeError(f"gateway {path}: HTTP {r.status_code}")
    return r


def _require_chat() -> tuple[str, str]:
    if not enabled():
        raise RuntimeError("gateway disabled (no base URL)")
    m = chat_model()
    if not m:
        raise RuntimeError("gateway: openai_model not set")
    return _base(), m


# ── tool conversion (Gemini function declarations → OpenAI) ──────────────────

_TYPE_MAP = {
    "OBJECT": "object", "STRING": "string", "INTEGER": "integer",
    "NUMBER": "number", "BOOLEAN": "boolean", "ARRAY": "array",
    "NULL": "null",
    # already-lowercase or unknown → pass through lowercased
}


def _lower_schema(node):
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "type" and isinstance(v, str):
                out[k] = _TYPE_MAP.get(v.upper(), v.lower())
            else:
                out[k] = _lower_schema(v)
        return out
    if isinstance(node, list):
        return [_lower_schema(x) for x in node]
    return node


def to_openai_tools(decls: Iterable) -> list[dict]:
    """Gemini-style declarations ({name, description, parameters{type:OBJECT…}})
    → OpenAI chat.completions tools[]. Accepts dicts and objects with attrs."""
    tools = []
    for d in decls or ():
        if isinstance(d, dict):
            name = d.get("name")
            desc = d.get("description") or ""
            params = d.get("parameters") or {"type": "object", "properties": {}}
        else:
            name = getattr(d, "name", None)
            desc = getattr(d, "description", "") or ""
            params = getattr(d, "parameters", None) or {
                "type": "object", "properties": {}}
        if not name:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": _lower_schema(params),
            },
        })
    return tools


# ── contents conversion (Gemini shapes → OpenAI messages) ────────────────────

def _gemini_contents_to_messages(contents, system: str = "") -> list[dict]:
    """str | list[str|Part|dict] → [{role:system?},{role:user, content:[...]}]."""
    items = contents if isinstance(contents, (list, tuple)) else [contents]
    parts: list = []
    for item in items:
        if isinstance(item, str):
            parts.append({"type": "text", "text": item})
            continue
        blob = getattr(item, "inline_data", None)
        if blob is not None:
            data = getattr(blob, "data", None)
            mime = getattr(blob, "mime_type", None) or "image/png"
            import base64
            if isinstance(data, bytes):
                b64 = base64.b64encode(data).decode("ascii")
            else:
                b64 = str(data or "")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
            continue
        txt = getattr(item, "text", None)
        if txt:
            parts.append({"type": "text", "text": txt})
            continue
        if isinstance(item, dict):
            if "text" in item:
                parts.append({"type": "text", "text": str(item["text"])})
            elif "inline_data" in item:
                idd = item["inline_data"] or {}
                import base64
                data = idd.get("data", "")
                if isinstance(data, bytes):
                    data = base64.b64encode(data).decode("ascii")
                parts.append({
                    "type": "image_url",
                    "image_url": {"url":
                                  f"data:{idd.get('mime_type','image/png')};base64,{data}"},
                })
    if not parts:
        parts = [{"type": "text", "text": ""}]
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": parts})
    return msgs


# ── chat (non-streaming — aux / gemini.py path) ──────────────────────────────

def chat(messages: list[dict], tools: Optional[list[dict]] = None,
         model: str = "", timeout: float = _TIMEOUT_S) -> dict:
    """One-shot chat.completions. Returns parsed JSON; raises on HTTP error."""
    if not model:
        model = chat_model()
    if not model:
        raise RuntimeError("gateway: openai_model not set")
    if not enabled():
        raise RuntimeError("gateway disabled")
    body = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    r = _post("/chat/completions", json=body, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"gateway chat: HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def chat_text(contents, system: str = "", timeout: float = _TIMEOUT_S) -> str:
    """Gemini-style `contents` in, reply text out — the aux call used by
    core/gemini.py's gateway rung."""
    msgs = _gemini_contents_to_messages(contents, system=system)
    data = chat(msgs, timeout=timeout)
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


# ── chat (streaming — voice fallback path) ───────────────────────────────────

def chat_stream(messages: list[dict], tools: Optional[list[dict]] = None,
                model: str = "", timeout: float = _TIMEOUT_S) -> Iterator:
    """Yield events from an SSE chat stream:
        ("sentence", text)   one complete sentence of assistant text
        ("tool_calls", list) accumulated OpenAI tool_call objects (after done text)
        ("done", full_text)  stream finished; full assistant text
    Falls back to one-shot on gateways that ignore stream=true (no SSE
    body → parse the plain JSON and emit the same events)."""
    if not model:
        model = chat_model()
    if not model:
        raise RuntimeError("gateway: openai_model not set")
    if not enabled():
        raise RuntimeError("gateway disabled")
    body = {"model": model, "messages": messages, "stream": True}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    r = _post("/chat/completions", json=body, timeout=timeout,
              stream=True, allow_redirects=True)
    if r.status_code >= 400:
        raise RuntimeError(f"gateway chat: HTTP {r.status_code}: {r.text[:200]}")

    ctype = (r.headers.get("content-type") or "").lower()
    if "text/event-stream" not in ctype:
        # Gateway ignored stream=true — plain JSON body.
        try:
            data = r.json()
        except Exception:
            raise RuntimeError("gateway chat: non-SSE, unparseable body")
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = (msg.get("content") or "").strip()
        for m in _split_sentences(text):
            yield ("sentence", m)
        if msg.get("tool_calls"):
            yield ("tool_calls", msg["tool_calls"])
        yield ("done", text)
        return

    buf = ""
    full: list[str] = []
    tool_acc: dict[int, dict] = {}

    for raw in r.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        choices = obj.get("choices") or []
        if not choices:
            continue
        ch = choices[0]
        delta = ch.get("delta") or {}

        content = delta.get("content")
        if content:
            full.append(content)
            buf += content
            last = 0
            for m in _SENT_END.finditer(buf):
                piece = buf[last:m.end()]
                if piece.strip():
                    yield ("sentence", piece)
                last = m.end()
            buf = buf[last:]

        for tc in delta.get("tool_calls") or ():
            idx = int(tc.get("index") or 0)
            slot = tool_acc.setdefault(idx, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]

    tail = buf.strip()
    if tail:
        yield ("sentence", tail)
    full_text = "".join(full).strip()
    if tool_acc:
        calls = [tool_acc[i] for i in sorted(tool_acc)]
        calls = [c for c in calls if c["function"].get("name")]
        if calls:
            yield ("tool_calls", calls)
    yield ("done", full_text)


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    parts, pos = [], 0
    for m in _SENT_END.finditer(text):
        parts.append(text[pos:m.end()])
        pos = m.end()
    if pos < len(text):
        parts.append(text[pos:])
    return [p for p in parts if p.strip()]


# ── STT ──────────────────────────────────────────────────────────────────────

def _pcm_to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def transcribe(pcm: bytes, rate: int = 16000, model: str = "") -> str:
    """One-shot STT. pcm is mono int16. Raises on failure."""
    if not enabled():
        raise RuntimeError("gateway disabled")
    model = model or (settings().get("stt_model") or "whisper-1")
    wav = _pcm_to_wav(pcm, rate)
    # multipart, not JSON
    path = "/audio/transcriptions"
    if _cooling(path):
        raise RuntimeError("gateway stt cooling down")
    try:
        r = requests.post(
            _base() + path,
            headers={"Authorization": _headers().get("Authorization", "")},
            files={"file": ("speech.wav", wav, "audio/wav")},
            data={"model": model},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as e:
        _cool(path)
        raise RuntimeError(f"gateway stt unreachable: {e}") from e
    if r.status_code == 429 or r.status_code >= 500:
        _cool(path)
        raise RuntimeError(f"gateway stt: HTTP {r.status_code}")
    if r.status_code >= 400:
        raise RuntimeError(f"gateway stt: HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = r.json()
        return (data.get("text") or "").strip()
    except Exception:
        return r.text.strip()


def transcribe_stream(pcm: bytes, rate: int = 16000,
                      model: str = "") -> Iterator[str]:
    """Yield transcript strings — progressive partials then (if the stream
    yields a final distinct chunk) the final. Consumers dedupe/append.
    Falls back to one-shot when the gateway has no SSE (first yield is the
    complete transcript in that case)."""
    if not enabled():
        raise RuntimeError("gateway disabled")
    model = model or (settings().get("stt_model") or "whisper-1")
    wav = _pcm_to_wav(pcm, rate)
    path = "/audio/transcriptions"
    if _cooling(path):
        raise RuntimeError("gateway stt cooling down")

    try:
        r = requests.post(
            _base() + path,
            headers={"Authorization": _headers().get("Authorization", ""),
                     "Accept": "text/event-stream"},
            files={"file": ("speech.wav", wav, "audio/wav")},
            data={"model": model, "stream": "true"},
            timeout=_TIMEOUT_S,
            stream=True,
        )
    except requests.RequestException as e:
        _cool(path)
        raise RuntimeError(f"gateway stt unreachable: {e}") from e
    if r.status_code == 429 or r.status_code >= 500:
        _cool(path)
        raise RuntimeError(f"gateway stt: HTTP {r.status_code}")
    if r.status_code >= 400:
        raise RuntimeError(f"gateway stt: HTTP {r.status_code}: {r.text[:200]}")

    ctype = (r.headers.get("content-type") or "").lower()
    if "text/event-stream" not in ctype:
        # one-shot JSON (or plain text) body
        try:
            data = r.json()
            txt = (data.get("text") or "").strip()
        except Exception:
            txt = r.text.strip()
        if txt:
            yield txt
        return

    acc = ""
    for raw in r.iter_lines(decode_unicode=True):
        if not raw:
            continue
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            break
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # OpenAI realtime-ish delta fields seen across gateways
        piece = (obj.get("delta") or obj.get("text")
                 or obj.get("transcript") or "")
        if not piece and isinstance(obj.get("choices"), list) and obj["choices"]:
            piece = (obj["choices"][0].get("delta") or {}).get("content") or ""
        if piece:
            acc += piece
            yield acc


# ── TTS ──────────────────────────────────────────────────────────────────────

def speak_pcm(text: str, model: str = "", voice: str = "") -> bytes:
    """Speech via the gateway. Refuses, loudly, when no voice model is set."""
    if not (model or voice_model()):
        raise RuntimeError(
            "gateway speech is not configured: set voice_model to a model that "
            "can synthesise audio. The chat model is not one of them.")
    """One-shot TTS → mono int16 PCM at ~24 kHz. Raises on failure."""
    chunks = list(speak_stream(text, model=model, voice=voice))
    return b"".join(chunks)


def speak_stream(text: str, model: str = "", voice: str = "") -> Iterator[bytes]:
    """Yield PCM chunks (mono int16, 24 kHz assumed) as they arrive.
    Tries response_format=pcm first; on failure retries wav and decodes
    via miniaudio → linear-resamples to 24 kHz."""
    if not enabled():
        raise RuntimeError("gateway disabled")
    v = settings()
    model = model or (v.get("tts_model") or "tts-1")
    voice = voice or (v.get("openai_voice") or "alloy")
    body = {"model": model, "voice": voice, "input": text,
            "response_format": "pcm"}
    path = "/audio/speech"

    if not _cooling(path):
        try:
            r = requests.post(_base() + path, headers=_headers(), json=body,
                              timeout=_TIMEOUT_S, stream=True)
        except requests.RequestException as e:
            _cool(path)
            r = None
            err = e
        else:
            err = None
        if r is not None:
            if r.status_code == 429 or r.status_code >= 500:
                _cool(path)
                r = None
            elif r.status_code >= 400:
                r = None  # fall through to wav retry (some gateways lack pcm)
            else:
                ctype = (r.headers.get("content-type") or "").lower()
                # pcm comes back as application/octet-stream; a JSON error
                # body sneaking through counts as failure too
                if "application/json" not in ctype:
                    got = False
                    for chunk in r.iter_content(chunk_size=4800):
                        if chunk:
                            got = True
                            yield chunk
                    if got:
                        return

    # Fallback: wav → miniaudio → 24k mono s16
    body["response_format"] = "wav"
    try:
        r = requests.post(_base() + path, headers=_headers(), json=body,
                          timeout=_TIMEOUT_S)
    except requests.RequestException as e:
        _cool(path)
        raise RuntimeError(f"gateway tts unreachable: {e}") from e
    if r.status_code == 429 or r.status_code >= 500:
        _cool(path)
        raise RuntimeError(f"gateway tts: HTTP {r.status_code}")
    if r.status_code >= 400:
        raise RuntimeError(f"gateway tts: HTTP {r.status_code}: {r.text[:200]}")
    pcm = _wav_bytes_to_pcm24k(r.content)
    # emit in the same slice size audio_in_queue expects pieces of
    for i in range(0, len(pcm), 4800):
        yield pcm[i:i + 4800]


def _wav_bytes_to_pcm24k(data: bytes, target_rate: int = 24000) -> bytes:
    """Decode wav/mp3 bytes to mono s16 @ target_rate. Prefers miniaudio;
    if only a wav header is available, falls back to the stdlib wave module."""
    try:
        import miniaudio
        decoded = miniaudio.decode(data, nchannels=1, sample_rate=target_rate,
                                   output_format=miniaudio.SampleFormat.SIGNED16)
        return bytes(decoded.samples)
    except Exception:
        pass
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            sw = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            raise RuntimeError("unsupported wav sample width")
        if ch > 1:
            # average channels to mono
            import struct
            n = len(raw) // 2
            samples = struct.unpack(f"<{n}h", raw[:n * 2])
            mono = []
            for i in range(0, n, ch):
                mono.append(sum(samples[i:i + ch]) // ch)
            raw = struct.pack(f"<{len(mono)}h", *mono)
        if rate != target_rate:
            raw = _linear_resample_s16(raw, rate, target_rate)
        return raw
    except Exception as e:
        raise RuntimeError(f"tts decode failed: {e}") from e


def _linear_resample_s16(pcm: bytes, src: int, dst: int) -> bytes:
    import struct
    n = len(pcm) // 2
    if n == 0 or src == dst:
        return pcm
    samples = struct.unpack(f"<{n}h", pcm[:n * 2])
    out_len = int(n * dst / src)
    out = []
    for i in range(out_len):
        pos = i * src / dst
        i0 = int(pos)
        i1 = min(i0 + 1, n - 1)
        frac = pos - i0
        out.append(int(samples[i0] * (1 - frac) + samples[i1] * frac))
    return struct.pack(f"<{len(out)}h", *out)


# ── engine availability (for the dash) ───────────────────────────────────────

def engine_availability() -> dict:
    """What can actually run right now — powers the settings dropdowns."""
    import importlib.util

    def _has(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False

    v = settings()
    return {
        "gateway_configured": enabled(),
        "gateway_chat_model": chat_model(),
        "gateway_voice_model": voice_model(),
        "local_stt": _has("faster_whisper"),
        "local_tts": _has("edge_tts"),
        "miniaudio": _has("miniaudio"),
        "stt_mode": v.get("stt_mode", "auto"),
        "tts_mode": v.get("tts_mode", "auto"),
        "voice_fallback": v.get("voice_fallback", True),
    }
