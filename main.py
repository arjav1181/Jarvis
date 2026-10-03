import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen


# ── Console must survive non-UTF-8 code pages ────────────────────────────────
# Every status line in this file carries an emoji, and on a legacy Windows
# console the active code page is the system one — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# because most of these prints sit inside the receive loop it takes the session
# down on startup. Reconfiguring to UTF-8 with a replacement fallback costs
# nothing and makes the app launch the same way in every locale.
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import os
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from core.mode import SERVER_MODE

import numpy as np
from google import genai
from google.genai import types

if SERVER_MODE:
    # Server mode: no local sounddevice, no Qt window — the browser is the
    # only UI and supplies both the microphone and the speakers. Placeholders
    # keep every other import and annotation in this file valid.
    sd = None
    JarvisUI = None
else:
    import sounddevice as sd
    from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier,
)

# The file-backed tools (open_app, web_search, browser_control, …) are no longer
# imported or declared here — they self-describe via a TOOL dict in their own
# actions/*.py file and are auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_jarvis,
# system_status).
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors,
)
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import (
    get_brief_enabled, get_media_resolution, get_proactive_audio_enabled,
    get_push_to_talk_enabled, get_thinking_enabled, get_turn_tuning, get_voice,
    get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
    save_voice,
)
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.echo                 import EchoGuard
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# How long the assistant stays awake with no user speech before it auto-sleeps
# again (wake-word mode only).
WAKE_SLEEP_TIMEOUT = 120.0   # seconds (2 minutes)

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"


def _resolve_api_config_path() -> Path:
    """Keys live under the data root (/data on HF Spaces), not the repo copy."""
    try:
        from core.data_paths import config_dir
        return config_dir() / "api_keys.json"
    except Exception:
        return BASE_DIR / "config" / "api_keys.json"


API_CONFIG_PATH = _resolve_api_config_path()
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# ── Viseme extraction ─────────────────────────────────────────────────────────
# The avatar's mouth used to be driven by one RMS value per ~200 ms write batch,
# which is five updates a second averaged over a fifth of a second — it could
# only ever flap. These read the *shape* of each 20 ms slice straight from the
# spectrum of the audio being played, so no transcript, no forced alignment and
# no language assumption: it works the same for Turkish and English.
#
# Two numbers come out. Openness tracks the first formant — F1 climbs as the jaw
# drops, so /a/ reads open and /i/ or /u/ read closed. Width tracks the second —
# F2 is high for spread vowels (/i/, /e/) and low for rounded ones (/u/, /o/).
# Extra time beyond the device's reported output latency before the microphone
# is trusted again: covers room decay and the speaker's own settling.
_TAIL_MARGIN = 0.25

_VIS_WIN = 1024        # ~43 ms analysis window at 24 kHz: enough for formants
_VIS_HOP = 480         # 20 ms between frames, i.e. 50 shapes a second

# Delay from handing the first bytes of a reply to an already-running output
# stream to hearing them: one callback period, plus whatever the DAC adds.
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE      # ~43 ms
# How far past the device's own buffer the mouth's timeline may drift before it
# is re-anchored. The buffer is the hard limit on how much audio can be queued
# ahead, so anything beyond it plus a margin for clock error is impossible.
_CURSOR_SLACK = 0.15

# Erring early is the safe direction. A viewer tolerates a mouth that moves
# slightly before the sound far better than one that moves after it — the
# broadcast limits are about 45 ms of lag against 125 ms of lead — so where
# this is uncertain it is biased to lead.


def _pcm_visemes(samples, sr: int = 24000):
    """Slice a PCM block into (level, openness, width) frames, one per 20 ms.

    Returns [] on anything unexpected — the mouth falls back to loudness-only
    articulation rather than the caller having to handle an error.
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < _VIS_WIN:
            return []
        win = np.hanning(_VIS_WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(_VIS_WIN, 1.0 / sr)
        b_f1_lo = (freqs >= 150) & (freqs < 450)     # F1 of close vowels
        b_f1_hi = (freqs >= 450) & (freqs < 1100)    # F1 of open vowels
        b_f2_bk = (freqs >= 600) & (freqs < 1300)    # F2 of rounded vowels
        b_f2_fr = (freqs >= 1700) & (freqs < 3200)   # F2 of spread vowels
        b_hiss = (freqs >= 3800) & (freqs < 8000)    # fricatives

        # One frame per hop across the *whole* block. Stepping only while a full
        # window fits stopped 1024 - 480 samples short of the end, so a 200 ms
        # batch yielded 160 ms of schedule: the mouth ran out of frames before
        # the audio ran out of sound, and each batch no longer lined up with the
        # end of the one before it. Losing 20 % of every batch is most of why
        # the mouth did not track the words.
        out = []
        for start in range(0, x.size, _VIS_HOP):
            # The level gates closures, so it is measured over exactly this
            # 20 ms and never looks ahead. The spectrum needs a longer window
            # to resolve formants and may be short-filled at the very end.
            level = _pcm_level(x[start:start + _VIS_HOP])
            seg = x[start:start + _VIS_WIN]
            if seg.size < _VIS_WIN:
                seg = np.concatenate([seg, np.zeros(_VIS_WIN - seg.size,
                                                    dtype=np.float32)])
            if level <= 0.0:
                out.append((0.0, 0.0, 0.0))
                continue
            mag = np.abs(np.fft.rfft((seg - seg.mean()) * win))
            f1l, f1h = float(mag[b_f1_lo].sum()), float(mag[b_f1_hi].sum())
            f2b, f2f = float(mag[b_f2_bk].sum()), float(mag[b_f2_fr].sum())
            hiss = float(mag[b_hiss].sum())

            openness = f1h / (f1l + f1h + 1e-6)
            width = (f2f - f2b) / (f2f + f2b + 1e-6)
            # A wide-open jaw physically cannot purse, so openness damps width.
            # /a/ has a low enough F2 to read as "rounded" on the bands alone;
            # letting openness suppress the width term is what keeps an open
            # vowel from pursing.
            width *= (1.0 - openness) ** 0.8
            # Fricatives are formed with a nearly closed mouth.
            h = hiss / (f1l + f1h + f2b + f2f + hiss + 1e-6)
            openness *= 1.0 - 0.65 * min(1.0, h * 2.5)
            out.append((level,
                        float(min(1.0, max(0.0, openness))),
                        float(min(1.0, max(-1.0, width)))))
        return out
    except Exception:
        return []


def _connector_block() -> str:
    """What JARVIS is connected to, and the rule that stops it guessing.

    The model only learns its tools from forty function declarations. Asked for
    the public URL of a Vercel project it answered from the open web and said
    the project could not be found, because "look it up" is what every other
    assistant it has ever been has done and nothing told it otherwise.

    So this says it plainly, every turn: these are the user's own systems, they
    are behind these tools, and the web is not a substitute for them.
    """
    try:
        from core import github as _gh, vercel as _vc, hf as _hf
        from core import mail as _ml
        from core import gcal as _gc
    except Exception:
        return ""
    rows = []
    for label, fn in (
        ("Gmail — the inbox, and sending from the user's own address",
         lambda: bool(_ml.config().get("configured"))),
        ("Google Calendar — the real calendar", _gc.configured),
        ("GitHub — their repositories, pull requests and CI", _gh.configured),
        ("Vercel — their deployments and production state", _vc.configured),
        ("Hugging Face — their models, Spaces and datasets", _hf.configured),
    ):
        try:
            up = bool(fn())
        except Exception:
            up = False
        rows.append(f"- {label} — {'connected' if up else 'not connected yet'}")
    try:
        from core import mcp as _mcpm
        n = len(_mcpm.cached_tools())
    except Exception:
        n = 0
    rows.append(f"- MCP servers — {n} tool(s) available" if n
                else "- MCP servers — none connected")
    # What this machine physically has. The model reached for
    # screen_process while operating a browser and got "Cannot connect to
    # display", because nothing had told it there is no screen to reach for.
    try:
        from actions.screen_processor import headless as _hl
        no_screen = _hl()
    except Exception:
        no_screen = False
    env = []
    if no_screen:
        env.append("This is a SERVER, not a desktop: there is no screen "
                   "to photograph and no camera attached. Never ask to see "
                   "either here - say plainly that there is none. To find "
                   "out what is on a web page, use the browser tool and read "
                   "what it returns: it gives you the page as text, which is "
                   "more use than a photograph of it.")
    else:
        env.append("This machine has a desktop, so the screen and camera "
                   "tools work and you may photograph them when it helps.")
    env.append("Where no tool can answer something, say what you cannot do "
               "and why. Do not substitute a web search for a tool that "
               "owns the answer.")
    head = ("WHERE YOU ARE RUNNING" + chr(10) + " ".join(env) + chr(10) + chr(10) + "YOUR OWN SYSTEMS" + chr(10) + chr(10).join(rows) + chr(10) + chr(10))
    return head + (
        "These are the user's real accounts and infrastructure. When the "
        "question is about their email, their calendar, their code, their "
        "deploys, their models or their domains, answer it from the tool "
        "that owns it. Never substitute a web search for it: the open web "
        "cannot know what is on their Vercel, in their inbox, or in their "
        "repository, so searching produces a confident invention. If a tool "
        "reports that it is not connected, say that plainly and offer to "
        "connect it - that is a useful answer and it is the true one.")

def _empty_result_note(name: str, result) -> str:
    """Make a silent failure loud, in one place.

    Across core/ and actions/ there are well over a hundred `except: pass` and
    `except: return ""` handlers. Most are correct — an optional import, a
    best-effort probe, a cache miss — but a few sit on the path a tool uses, and
    when one of those returns an empty string the model has no way to tell a
    successful no-op from a failure. It either retries forever or, worse,
    reports a capability it does not have.

    Rather than audit a hundred call sites and risk breaking the ninety-nine
    that are fine, the guarantee is made once, here, at the boundary: a tool
    that produced nothing says so, and says how long it took, which is enough
    for the model to say something true to the user.
    """
    if isinstance(result, (dict, list, tuple, set)):
        return result
    if result is None or (isinstance(result, str) and not result.strip()):
        return (f"{name} finished but returned nothing — it has no result for "
                f"that. It may not be configured, or not available on this "
                f"machine. Do not report success; say plainly that it gave no "
                f"answer, and offer to set it up.")
    return result


def _describe_tools(declarations) -> str:
    """One line per capability, straight from the live tool declarations.

    Derived rather than written down: the action and plugin registries are
    discovered at startup, so whatever the user has installed is what the model
    is told it can do. Adding a plugin extends this by itself, and removing one
    stops the model from claiming an ability it no longer has.
    """
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\n".join(lines)


def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    """The other half of self-knowledge: what is out of reach, and why.

    Derived from how the program is actually built, not from a list of refusals.
    A model that knows its boundaries stops improvising around them, and stating
    them as architecture rather than as rules keeps the answer honest in any
    language.
    """
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template.

    A plain replace rather than str.format: the file is meant to be edited by
    hand, and a stray brace in someone's own wording must never take the app
    down at startup.
    """
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    env = (os.environ.get("JARVIS_GEMINI_API_KEY") or "").strip()
    if env:
        return env
    path = API_CONFIG_PATH
    if not path.is_file():
        # data root may differ from the process-capture path (e.g. /data
        # mounted after import). Prefer memory.config_manager when available.
        try:
            from memory.config_manager import get_gemini_key
            key = get_gemini_key()
            if key:
                return key
        except Exception:
            pass
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# Transcript chunks shorter than this may legitimately repeat ("evet, evet"),
# so only longer ones are treated as duplicates.
_REPEAT_MIN = 12


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    """True if this transcript chunk has already been seen this turn.

    Guards against the API re-sending the tail of a response across the several
    turn_completes a tool-using turn produces.
    """
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined


# Quota / overload markers that mean Live itself is out of pool — the signal
# to drop to the gateway voice fallback instead of backoff-retrying the same
# dead endpoint. Checked only AFTER the invalid-key branch in the connect
# loop, so a bad key still prompts reconfiguration as before.
_QUOTA_MARKERS = (
    "resource_exhausted", "quota", "rate limit", "rate_limit",
    "unavailable",
)


def _is_quota_error(err_str: str) -> bool:
    s = (err_str or "").lower()
    if any(m in s for m in _QUOTA_MARKERS):
        return True
    # bare HTTP/gRPC codes as whole tokens ("429", "503", …) — \b stops
    # "1500 tokens" from matching 500.
    return bool(re.search(r"\b(429|500|502|503)\b", s))

def _clean_transcript(text: str) -> str:
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()


def _polish_reply(text: str) -> str:
    """Run the character pass on a COMPLETE reply, not on a stream chunk.

    The live path delivers text in fragments, and a fragment is not a sentence.
    Polishing per chunk deleted partial filler ("Let me know if you need
    anything") and lost real words mid-reply. So this is called once, on the
    assembled reply, at the two points where a reply is actually finished: the
    browser text feed and the gateway chat. Everywhere else, the stream passes
    through untouched.
    """
    try:
        from core import character as _ch
        return _ch.polish(text) or text
    except Exception:
        return text


TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — vision capture/injection,
    # camera stream, memory writes, the monitor engine, and shutdown. All other
    # tools live in their own action file and are auto-discovered by
    # core.action_loader (see JarvisLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "JARVIS checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "manage_schedule",
        "description": (
            "Create, list, pause, resume, remove or manually run SCHEDULED jobs — "
            "things JARVIS does on its own at a set time, with no prompt needed later. "
            "Use it whenever the user asks for something recurring or at a specific time: "
            "'every morning at 8', 'check X in 20 minutes', 'remind me Friday', "
            "'stop doing that check', 'what is scheduled?'. "
            "The prompt is the job's instruction to yourself, so write it as if the user "
            "were typing it then ('brief me on the morning headlines and the weather'). "
            "Cadence: kind=interval with spec like '30m'/'2h'/'1d'; kind=daily with spec "
            "'08:00'; kind=cron with a 5-field spec ('30 9 * * 1-5'); kind=once with "
            "'45m' or an ISO timestamp. Use kind=interval spec='30m' for 'every 30 minutes'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type":        "string",
                    "description": "add | list | remove | enable | disable | run",
                },
                "name": {
                    "type":        "string",
                    "description": "Short name for the job, e.g. 'morning brief'.",
                },
                "kind": {
                    "type":        "string",
                    "description": "interval | daily | cron | once (default: interval)",
                },
                "spec": {
                    "type":        "string",
                    "description": "Cadence: '30m'/'2h'/'1d' | '08:00' | '30 9 * * 1-5' | '45m'",
                },
                "prompt": {
                    "type":        "string",
                    "description": "What the job should DO when it fires — an instruction to yourself.",
                },
                "job_id": {
                    "type":        "string",
                    "description": "Job id, for remove/enable/disable/run when the name is ambiguous.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "display",
        "description": (
            "Put something on the user's screen. This is a real display surface, not a "
            "description: whatever you show appears immediately in the dashboard (and on "
            "their phone) and stays there until they close it.\n"
            "action=show with kind=html is the powerful one: write a COMPLETE self-contained "
            "HTML page (inline CSS, inline JS, canvas, SVG) and it renders in a sandboxed "
            "frame — use it for wiring diagrams, dashboards, mockups, small tools, games, "
            "anything with a layout. Inline every style and script; external CDNs work but "
            "add nothing reliable.\n"
            "action=show with kind=chart when you have DATA (not a design): pass spec with "
            "type line|bar|pie|area and data [{label,value}...] and the dashboard draws it, "
            "so it looks right on any screen size.\n"
            "action=embed frames a real web page (kind=url).\n"
            "action=image shows a picture from a URL or data: URI.\n"
            "action=list shows what is on screen now, action=close clears it, "
            "action=pin keeps one permanently.\n"
            "Use it whenever the user says 'show me', 'draw', 'make me a ... dashboard/diagram/"
            "page/visual', 'put X on the screen', 'plot/chart the data', 'open <url>'.\n"
            "Put a warning in the 'warning' field whenever a value is inferred (a resistor "
            "value, a pinout, a price) so the user knows to check it.\n"
            + __import__("core.theme", fromlist=["theme"]).GUIDANCE
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "show | embed | image | list | close | pin | unpin"},
                "kind": {"type": "string",
                         "description": "html | url | image | chart | text (default html)"},
                "title": {"type": "string", "description": "Short title shown above it."},
                "html": {"type": "string",
                         "description": "kind=html: a complete HTML page, or just a <body> fragment if you prefer."},
                "url": {"type": "string", "description": "kind=url / image: the address."},
                "text": {"type": "string", "description": "kind=text: plain text, styled by the dashboard."},
                "spec": {"type": "object",
                         "description": "kind=chart: {type, title, unit, data:[{label,value}]}"},
                "warning": {"type": "string",
                            "description": "One line the user must check, e.g. 'resistor value inferred — verify against the datasheet'."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "globe",
        "description": (
            "The live Earth. This is the real God\u2019s Eye View project running on "
            "our server, not a mock-up: photorealistic satellite imagery, live "
            "aircraft and military flights, satellites, earthquakes, wildfires, "
            "cyclones, radar, lightning, ship traffic, cameras, and visual presets. "
            "It takes over the screen until they dismiss it, and you drive it by "
            "voice \u2014 you are the voice, not a second assistant.\n"
            "action=show (default) opens it. place= flies the camera somewhere. "
            "style= normal|nvg|flir|crt|anime|god changes the look. "
            "zoom= in|out|globe.\n"
            "show_layers / hide_layers take layer ids; the common ones are "
            "flights, military, local-adsb, satellites, earthquakes, "
            "fire-perimeters, traffic, rocket-launches, wind, weather-radar, "
            "weather-lightning, weather-cyclones, ais-live-vessels, radio, "
            "transit, bikeshare, cctv, alpr-cameras, military-installations. "
            "Ask for them by name and I will map it.\n"
            "route_from / route_to draw a real street route between two places and "
            "fly it. iss=true jumps to the next ISS pass. "
            "nearest_aircraft=true picks the closest aircraft.\n"
            "action=layers just reports what is live, without showing anything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "STRING",
                           "description": "show | layers (default: show)"},
                "place": {"type": "STRING",
                          "description": "City, landmark or address to fly to"},
                "style": {"type": "STRING",
                          "description": "normal | nvg | flir | crt | anime | god"},
                "zoom": {"type": "STRING", "description": "in | out | globe"},
                "show_layers": {"type": "ARRAY", "items": {"type": "STRING"}},
                "hide_layers": {"type": "ARRAY", "items": {"type": "STRING"}},
                "route_from": {"type": "STRING", "description": "Trip start"},
                "route_to": {"type": "STRING", "description": "Trip end"},
                "iss": {"type": "BOOLEAN",
                        "description": "Jump to the next ISS pass"},
                "nearest_aircraft": {"type": "BOOLEAN",
                                     "description": "Select the closest aircraft"}
            }
        },
    },
    {
        "name": "knowledge",
        "description": (
            "Your own memory of documents and past conversations. Use it instead of "
            "asking the user something they have already told you.\n"
            "action=search (default): semantic search over stored documents AND past "
            "conversation turns. Use it before you ask any question about their "
            "clients, rates, projects, decisions or preferences \u2014 and before you "
            "say 'I don\u2019t know' about something they mentioned earlier.\n"
            "action=add: store a fact, note, client detail or decision "
            "(title + text) so it is recallable later. action=add_url: fetch a page "
            "and store it. action=recent: what was said recently. action=list: what "
            "documents exist. action=forget: delete one by id. "
            "action=stats: what is in there.\n"
            "Be selective about what you store: durable facts about their work, "
            "clients, rates and decisions. Never store a secret, a password or an API "
            "key, and never store a passing pleasantry."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "STRING",
                           "description": "search | add | add_url | recent | list | "
                                          "forget | stats (default: search)"},
                "query": {"type": "STRING", "description": "What to look for"},
                "title": {"type": "STRING", "description": "Title, for add"},
                "text": {"type": "STRING", "description": "Body, for add"},
                "url": {"type": "STRING", "description": "Page to fetch, for add_url"},
                "source": {"type": "STRING", "description": "note | url | client | decision"},
                "id": {"type": "INTEGER", "description": "Document id, for forget"},
                "limit": {"type": "INTEGER", "description": "How many results"}
            }
        },
    },
    {
        "name": "maps",
        "description": (
            "Look up places, get map links and rough distances. "
            "action=find searches for a place ('the nearest hospital in Kadikoy'), "
            "action=nearby finds the closest of a type to the phone's last location, "
            "action=directions gives distance and a link between two places, "
            "action=link returns links to a place in a chosen app, "
            "action=pin remembers a place ('the client's office') so you can refer to it later. "
            "Always give the user a link in the map app they use — they can be on Google, "
            "Apple or Ola Maps; provider picks which (default google)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type":        "string",
                    "description": "find | nearby | directions | link | pin",
                },
                "query": {
                    "type":        "string",
                    "description": "What to look for, e.g. 'pharmacy in Kadikoy'.",
                },
                "to": {
                    "type":        "string",
                    "description": "Destination, for action=directions.",
                },
                "provider": {
                    "type":        "string",
                    "description": "google | apple | ola | osm (default google)",
                },
                "name": {
                    "type":        "string",
                    "description": "A short name to remember the place under, for action=pin.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "device",
        "description": (
            "Control paired devices (the user's PC, NAS, VPS — limbs running "
            "jarvisd). action=list shows paired devices and who is online; "
            "action=status/ping checks one (omit device for the default one); "
            "action=exec runs an allowlisted read-only shell command on it "
            "(anything riskier returns needs_approval — tell the user it needs "
            "confirmation). Use when the user asks about or wants something "
            "done on 'my computer/my PC' while talking through the server."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "enum": ["list", "status", "ping", "exec"],
                    "description": "What to do on the device.",
                },
                "device": {
                    "type": "STRING",
                    "description": "Device name (see list); omit for the default/only online device.",
                },
                "cmd": {
                    "type": "STRING",
                    "description": "Shell command for action=exec (read-only commands run automatically).",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "code_task",
        "description": (
            "Start an agentic coding task: opencode works on the prompt and "
            "streams its output to the dashboard Tasks panel. "
            "where='device' runs it on a paired device (the repo is "
            "worktree-isolated there, so the user's files stay untouched "
            "until they approve the push); where='space' runs it here. "
            "Use 'auto' (default) for the user's configured default target. "
            "repo = absolute path of the project ON the target machine. "
            "Pushing to origin always requires the user's confirmation "
            "afterwards — never claim a push happened on your own."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {
                    "type": "STRING",
                    "description": "What opencode should build/change — specific files and behavior.",
                },
                "where": {
                    "type": "STRING",
                    "enum": ["auto", "device", "space"],
                    "description": "Where to run it (auto = the user's default).",
                },
                "repo": {
                    "type": "STRING",
                    "description": "Absolute path of the project on the target machine.",
                },
                "device": {
                    "type": "STRING",
                    "description": "Device name for where=device (omit for the default online one).",
                },
                "model": {
                    "type": "STRING",
                    "description": "Optional opencode model id.",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "task_status",
        "description": (
            "Report coding-task progress. With task_id: that task's status "
            "and recent log tail. Without: the most recent task. Call after "
            "starting a code_task or when the user asks how work is going."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_id": {"type": "STRING", "description": "Task id like t-1a2b3c."},
            },
            "required": [],
        },
    },
    {
        "name": "cancel_task",
        "description": "Cancel a queued or running coding task when the user asks to stop/abort it.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_id": {"type": "STRING", "description": "Task id to cancel."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "push_task",
        "description": (
            "Ask to push a completed task's branch to origin. Returns a "
            "confirmation sentence — the dashboard CONFIRM card must be "
            "pressed by the user before anything is pushed. Never say it was "
            "pushed until task_status shows pushed=true."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task_id": {"type": "STRING", "description": "Done task id to push."},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "delegate",
        "description": (
            "Delegate a multi-part goal to PARALLEL coding tasks. First draft "
            "2-6 independent, fully self-contained subtask prompts yourself, "
            "then call this once with the whole list — each becomes its own "
            "code_task (worktree-isolated, streams in the Tasks panel). Use "
            "for multi-part builds; for a single job call code_task directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {"type": "STRING", "description": "The overall goal (for the reply message)."},
                "subtasks": {
                    "type": "ARRAY",
                    "description": "2-6 independent subtasks.",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "prompt": {"type": "STRING", "description": "Self-contained opencode prompt."},
                            "where": {"type": "STRING", "enum": ["auto", "device", "space"]},
                            "repo": {"type": "STRING", "description": "Absolute project path on the target."},
                        },
                        "required": ["prompt"],
                    },
                },
            },
            "required": ["goal", "subtasks"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },

    {
        "name": "agents",
        "description": (
            "Your company\u2019s roster. You are the CEO; these report to you.\n"
            "action=org (default): the company at a glance \u2014 headcount, runs, "
            "deliverables, success rate, today\u2019s spend.\n"
            "action=roster: every agent with its role, tools, budget, spend and "
            "success rate.\n"
            "action=hire: create or change an agent. Needs a name, a role, a "
            "persona (how it thinks and writes), the tools it may use, and "
            "optionally a daily budget in USD and a schedule such as "
            "\u201cevery 6h\u201d or \u201cdaily 08:00\u201d. HIRING WITH A BUDGET "
            "NEEDS THE USER\u2019S APPROVAL.\n"
            "action=retire: switch an agent off without erasing its history.\n"
            "action=delete: fire and erase an agent (needs approval).\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "org | roster | hire | retire | delete"},
                "name": {"type": "STRING"},
                "role": {"type": "STRING",
                         "description": "Coder, Sales, Marketing, Research, "
                                        "Engineering, Operations, Design, "
                                        "Support, Finance"},
                "persona": {"type": "STRING",
                            "description": "how this agent thinks and writes"},
                "tools": {"type": "STRING",
                          "description": "tool names it may use, space separated"},
                "budget_usd_day": {"type": "NUMBER"},
                "schedule": {"type": "STRING",
                             "description": "every 6h | daily 08:00 | empty "
                                            "for none"},
            },
            "required": [],
        },
    },
    {
        "name": "brief",
        "description": (
            "Give an agent a job and get the work order back. The order carries "
            "that agent\u2019s persona, its allowed tools and its budget \u2014 send "
            "it to that agent or to a coding task as the prompt. Use this "
            "instead of inventing a persona on the fly, so the company stays "
            "consistent. action=create (default) or list."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "create | list"},
                "agent": {"type": "STRING",
                          "description": "who is doing the work"},
                "job": {"type": "STRING",
                        "description": "what they must produce"},
                "context": {"type": "STRING"},
                "kind": {"type": "STRING",
                         "description": "ad_copy | proposal | report | code | note"},
                "cost_usd": {"type": "NUMBER",
                             "description": "what this costs that agent\u2019s "
                                            "daily budget"},
            },
            "required": ["agent", "job"],
        },
    },

    {
        "name": "journal",
        "description": (
            "What happened, and when — the dated record behind the memory. Use it "
            "for anything of the form \u201cwhat did we decide about X\u201d, \u201cwhat "
            "happened on Tuesday\u201d, \u201cwhy did we do it that way\u201d.\n"
            "action=add (default): write an entry. kind=note (default) | decision "
            "| lesson | metric | error. A decision you make with the user belongs "
            "here \u2014 it is how you still know next month.\n"
            "action=search: lexical search across the whole history.\n"
            "action=list: recent entries; kind= and days= narrow it.\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "add | search | list"},
                "kind": {"type": "STRING",
                         "description": "note | decision | lesson | metric | error"},
                "title": {"type": "STRING"},
                "body": {"type": "STRING"},
                "tags": {"type": "STRING", "description": "comma separated"},
                "query": {"type": "STRING"},
                "days": {"type": "NUMBER"},
            },
            "required": [],
        },
    },
    {
        "name": "calendar",
        "description": (
            "The calendar. Reads your real Google Calendar once it is connected "
            "(🔌 Connectors), and always works through a local .ics file, so "
            "scheduling needs no account.\n"
            "action=list (default): what is coming up. action=agenda: one "
            "sentence, for reading out loud.\n"
            "action=add: put something in. Give start as \u201c2026-10-01T09:00\u201d "
            "or \u201ctomorrow 9am\u201d, plus minutes.\n"
            "action=delete: remove an event (needs approval).\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "list | agenda | add | delete"},
                "title": {"type": "STRING"},
                "start": {"type": "STRING"},
                "end": {"type": "STRING"},
                "minutes": {"type": "NUMBER"},
                "where": {"type": "STRING"},
                "notes": {"type": "STRING"},
                "ref": {"type": "STRING"},
                "days": {"type": "NUMBER"},
            },
            "required": [],
        },
    },
    {
        "name": "files",
        "description": (
            "The document store. Uploaded files are text-extracted and indexed, so "
            "you can ask what a contract said without opening it.\n"
            "action=list (default): find files by name, tag or client. "
            "action=read: the text of one file. action=attach: pin a file to a "
            "client or a deliverable. action=delete: remove it (needs approval). "
            "action=upload: store bytes you already have as base64 (needs "
            "approval).\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "list | read | attach | delete | upload"},
                "id": {"type": "STRING"},
                "query": {"type": "STRING"},
                "tag": {"type": "STRING"},
                "client": {"type": "STRING"},
                "deliverable": {"type": "STRING"},
                "filename": {"type": "STRING"},
                "data_b64": {"type": "STRING"},
                "title": {"type": "STRING"},
            },
            "required": [],
        },
    },
    {
        "name": "home",
        "description": (
            "The house. Devices you registered and the scenes built from them "
            "\u2014 paired limbs, MQTT, Home Assistant, or plain webhooks.\n"
            "action=list (default): every device and scene, with what is on.\n"
            "action=run: one device, one op (on / off / toggle). action=run_scene: "
            "a whole scene. Both accept dry_run=true, which tells you what would "
            "happen and changes nothing \u2014 use it first on anything new.\n"
            "action=device / scene: register one (needs approval).\n"
            "Anything that unlocks, arms, or opens a door is always refused "
            "without the user\u2019s explicit go-ahead, whatever you ask for.\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "list | run | run_scene | device | scene"},
                "name": {"type": "STRING"},
                "op": {"type": "STRING", "description": "on | off | toggle"},
                "dry_run": {"type": "BOOLEAN"},
                "kind": {"type": "STRING",
                         "description": "paired | mqtt | hass | webhook | virtual"},
                "where": {"type": "STRING"},
                "steps": {"type": "ARRAY",
                          "description": "[{device, op, after}] for a scene",
                          "items": {"type": "OBJECT", "properties": {
                              "device": {"type": "STRING"},
                              "op": {"type": "STRING"},
                              "after": {"type": "STRING"}}}},
            },
            "required": [],
        },
    },
    {
        "name": "browser",
        "description": (
            "A real browser you can drive. Only hosts on the allowlist are "
            "reachable, and robots.txt is honoured \u2014 you cannot turn this into a "
            "scraper, and if a site disallows a path I will say so.\n"
            "action=open (default): load a page and read it back as text. "
            "action=click: click some text on a page, then read where it lands. "
            "action=status: is the site up.\n"
            "action=allow: add a host the user approved (needs approval). "
            "action=deny: remove one.\n"
            "Never invent a host that is not on the list, and never claim to have "
            "read a page you did not open.\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "open | click | status | allow | deny"},
                "url": {"type": "STRING"},
                "text": {"type": "STRING", "description": "text to click"},
                "host": {"type": "STRING"},
                "urls": {"type": "ARRAY",
                         "items": {"type": "STRING"}},
            },
            "required": [],
        },
    },
    {
        "name": "proactive",
        "description": (
            "What you may do without being asked, written down so we both know "
            "the line.\n"
            "MAY: read, gather, draft, schedule, summarise, tell me. MUST ASK: "
            "anything that spends, sends, deletes, unlocks or promises. NEVER, "
            "even if I ask twice: changes to the approval gate itself, or moving "
            "all the money \u2014 you do not get to widen your own permissions.\n"
            "action=status (default): the boundary and the ledger. "
            "action=briefing: the morning brief, on demand. action=check: classify "
            "a request before you act on it. action=watch: a standing question on "
            "a cadence \u2014 watches may look and report, never act.\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "status | briefing | check | watch | "
                                          "unwatch"},
                "text": {"type": "STRING",
                         "description": "for check: what you are about to do"},
                "name": {"type": "STRING"},
                "prompt": {"type": "STRING"},
                "every": {"type": "STRING", "description": "every 6h | daily"},
                "kind": {"type": "STRING",
                         "description": "check | summarise | draft"},
            },
            "required": [],
        },
    },
    {
        "name": "voice",
        "description": (
            "The voice loop, as a state machine you can read: idle, listening, "
            "transcribing, thinking, speaking. Use it to check the microphone is "
            "alive, to set how the loop behaves (barge_in, silence_ms, wake word), "
            "or to read back the last few turns when someone says \u201cwhat did you "
            "hear\u201d.\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "status | history | settings"},
                "barge_in": {"type": "BOOLEAN"},
                "silence_ms": {"type": "NUMBER"},
                "wake_word": {"type": "BOOLEAN"},
                "tts": {"type": "BOOLEAN"},
                "limit": {"type": "NUMBER"},
            },
            "required": [],
        },
    },

    {
        "name": "goals",
        "description": (
            "What you are working on. Standing goals, and the position you hold "
            "on each — these are yours, already written down, and you do not "
            "re-derive them or recite them unless asked.\n"
            "action=list (default): every goal, where it stands, your next "
            "move, and anything waiting on the user.\n"
            "action=set: a standing goal. Give a title, why it matters, and a "
            "position if you already know one. Keep it few — three or four.\n"
            "action=position: update where a goal stands, and why. THIS IS THE "
            "IMPORTANT ONE — it is how you sound like you remember. Say where "
            "it actually is, never that it is going fine.\n"
            "action=advance: move the number, with the evidence.\n"
            "action=note: record something that happened against a goal.\n"
            "action=standup: the briefing — state first, then the next move.\n"
            "action=act: do one thing toward the goal that is furthest from "
            "done (dry_run=true to see what it would do first).\n"
            "action=close / pause: finish, drop or park a goal.\n"
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "list | set | position | advance | "
                                          "note | standup | act | close | pause | "
                                          "clear"},
                "ref": {"type": "STRING", "description": "the goal"},
                "title": {"type": "STRING"},
                "why": {"type": "STRING", "description": "why it matters"},
                "target": {"type": "STRING"},
                "text": {"type": "STRING",
                         "description": "for position/note: what it says"},
                "next_move": {"type": "STRING",
                              "description": "what you will do about it"},
                "metric": {"type": "STRING", "description": "what is counted"},
                "start": {"type": "NUMBER"},
                "goal": {"type": "NUMBER", "description": "the target value"},
                "value": {"type": "NUMBER", "description": "for advance"},
                "unit": {"type": "STRING"},
                "deadline": {"type": "STRING"},
                "note": {"type": "STRING"},
                "dry_run": {"type": "BOOLEAN"},
            },
            "required": [],
        },
    },

    {
        "name": "email",
        "description": (
            "The mailbox. Reads the real Gmail inbox once it is connected, and "
            "sends from the address the user set up.\n"
            "action=inbox (default): the latest messages. Set unread=true for "
            "only unread. Read this before claiming to know what is waiting.\n"
            "action=status: the sending address and how much of today's budget "
            "is left.\n"
            "action=draft: write a message and STOP. Drafting sends nothing.\n"
            "action=send: actually send it. This always asks the user first, "
            "and is refused if the daily budget or the gap between sends is "
            "exhausted — say so plainly rather than trying again.\n"
            "action=sent: what has gone out.\n"
            "DRAFT BY DEFAULT. Never send because the message sounds like it "
            "should be sent; send when the user asks or approves."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "inbox | unread | draft | send | sent | "
                                          "status | replies | configure"},
                "to": {"type": "STRING", "description": "recipient address"},
                "subject": {"type": "STRING"},
                "body": {"type": "STRING", "description": "the message text"},
                "limit": {"type": "INTEGER", "description": "how many"},
                "unread": {"type": "BOOLEAN"},
                "query": {"type": "STRING", "description": "IMAP search terms"},
            },
            "required": [],
        },
    },

    {
        "name": "github",
        "description": (
            "The user's GitHub. Read it to answer questions about their code "
            "instead of guessing.\n"
            "action=repos (default): repositories, most recently pushed.\n"
            "action=prs / issues: open pull requests and issues. Give repo as "
            "owner/repo.\n"
            "action=ci: recent workflow runs on a repo.\n"
            "action=failing: only the runs that broke. This is the one to use "
            "when asked 'is CI green' or 'what broke'.\n"
            "action=log: the tail of a run's log, where the real error is.\n"
            "action=comment / open: post a comment or open an issue. Both ASK "
            "the user first — they are public acts."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "repos | prs | issues | ci | failing | "
                                          "log | comment | open | me"},
                "repo": {"type": "STRING", "description": "owner/repo"},
                "number": {"type": "STRING", "description": "issue/PR or run id"},
                "text": {"type": "STRING", "description": "comment body"},
                "title": {"type": "STRING", "description": "issue title"},
                "branch": {"type": "STRING"},
                "limit": {"type": "INTEGER"},
            },
            "required": [],
        },
    },
    {
        "name": "vercel",
        "description": (
            "The user's Vercel infrastructure.\n"
            "action=projects (default): projects and their current state.\n"
            "action=deploys: recent deployments, newest first.\n"
            "action=broken: deployments that did not finish. Use this to answer "
            "'is anything broken in prod' without reading the whole history.\n"
            "action=redeploy: redeploy an id. This ASKS first, because it changes "
            "what the public is served."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "projects | deploys | broken | redeploy | me"},
                "project": {"type": "STRING"},
                "uid": {"type": "STRING", "description": "deployment id"},
                "limit": {"type": "INTEGER"},
            },
            "required": [],
        },
    },
    {
        "name": "hf",
        "description": (
            "Hugging Face: models, Spaces and datasets. Public repos work "
            "without any connection, so this is often answerable immediately.\n"
            "action=models (default): search models. action=model: one model's "
            "downloads, licence and whether it is gated.\n"
            "action=spaces: Spaces and their runtime stage — a Space sitting in "
            "BUILDING or PAUSED is usually a crashed Space.\n"
            "action=restart: restart a Space. This ASKS first."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "models | model | spaces | datasets | restart | me"},
                "query": {"type": "STRING", "description": "search terms or author"},
                "id": {"type": "STRING", "description": "model or space id"},
                "limit": {"type": "INTEGER"},
            },
            "required": [],
        },
    },
    {
        "name": "mcp",
        "description": (
            "External tool servers (Model Context Protocol). This is how you "
            "reach anything that has an MCP server — a CRM, a database, a "
            "design tool, a trading API, a smart-home bridge — without anyone "
            "having written a connector for it.\n"
            "action=list (default): every server, whether it is UP or DOWN, how "
            "many tools it offers, and whether it asks before calling. Check "
            "this before claiming you cannot do something.\n"
            "action=add: connect one. Give the command and its arguments, and "
            "it is verified immediately — if it does not start you are told "
            "why rather than being told it worked.\n"
            "action=remove: disconnect it.\n"
            "action=trust: stop asking before calling that server. Only if the "
            "user says they trust it.\n"
            "action=call: run one tool directly, when the user names it.\n"
            "Their tools appear to you as mcp__SERVER__TOOL and every call "
            "asks for approval unless the user has trusted that server."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "list | add | remove | trust | untrust | call"},
                "name": {"type": "STRING", "description": "the server name"},
                "command": {"type": "STRING", "description": "executable, e.g. npx"},
                "args": {"type": "STRING", "description": "arguments, space separated"},
                "env": {"type": "STRING", "description": "K=V,K=V"},
                "url": {"type": "STRING"},
                "trusted": {"type": "BOOLEAN"},
                "call": {"type": "STRING", "description": "tool to call"},
                "arguments": {"type": "STRING", "description": "JSON arguments"},
            },
            "required": [],
        },
    },
    {
        "name": "coder",
        "description": (
            "A real coding agent. It works in a workspace directory: reads "
            "files, searches, edits them, runs commands and tests, reads the "
            "output, and keeps going until the job is done or it is genuinely "
            "blocked. This is the tool for anything that is really a software "
            "task — a bug, a feature, a failing test, a refactor.\n"
            "action=go: give it the goal in plain English. It will work, run "
            "the tests, and report what it changed and how it verified it. "
            "This takes minutes, not seconds — say that it is working.\n"
            "action=status: the workspace and what is in it.\n"
            "action=undo: put the last file it changed back.\n"
            "action=read / ls / grep: look at the workspace yourself.\n"
            "path= which directory. Default is a persistent workspace under "
            "the data root, so say so if the user means a project elsewhere.\n"
            "Report what it says, including when it stops early and why. Do not "
            "claim it succeeded unless it said it verified the work."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "go | status | undo | read | ls | grep"},
                "goal": {"type": "STRING", "description": "the task, in English"},
                "path": {"type": "STRING", "description": "workspace directory"},
                "read": {"type": "STRING", "description": "file, for read/ls/grep"},
                "pattern": {"type": "STRING", "description": "regex, for grep"},
            },
            "required": [],
        },
    },
    {
        "name": "phone",
        "description": (
            "The user's phone. The JARVIS address is a web app: opened on the "
            "phone and added to the home screen it installs like an app and "
            "registers itself here.\n"
            "action=status: is it installed, can it be reached, and what has it "
            "reported. Check this before claiming the phone cannot do something.\n"
            "action=battery / locate: what the phone last reported. Free to read.\n"
            "action=buzz: a notification and a vibration pattern.\n"
            "action=wake: a notification that stays on screen until tapped.\n"
            "action=open: ask the phone to open a URL. Deep links work, so "
            "'open Spotify' means opening the Spotify app. It is a tap away — a "
            "web app cannot move the screen by itself, so say that plainly "
            "rather than claiming you opened it.\n"
            "This device CANNOT tap or swipe anything outside its own window, "
            "and you must never say it did."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "status | battery | locate | buzz | "
                                          "wake | open | read"},
                "device": {"type": "STRING"},
                "title": {"type": "STRING"},
                "body": {"type": "STRING"},
                "url": {"type": "STRING", "description": "for open"},
                "pattern": {"type": "STRING",
                            "description": "vibrate ms, e.g. 200,100,200"},
                "kind": {"type": "STRING", "description": "for read"},
            },
            "required": [],
        },
    },
    {
        "name": "welcome",
        "description": (
            "The welcome ceremony — what happens between the clap and the "
            "work. Music, a greeting, an honest status line, the weather, and "
            "then the ask.\n"
            "action=run: perform it now and speak it. A clap from the phone or "
            "the laptop mic does this on its own; you only need this when you "
            "want it without clapping.\n"
            "action=status: whether it is on, which voice, the city, and which "
            "track is loaded.\n"
            "action=lines: the words, without performing them.\n"
            "action=weather: just today's conditions and what they imply.\n"
            "action=faults: what is actually wrong. The ceremony refuses to "
            "say all systems are operational while something is broken, and "
            "this is what it would be looking at."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "run | status | lines | weather | faults"},
                "panel": {"type": "string",
                          "description": "space-separated URLs to open on the "
                                         "shared computer as part of it"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "computer",
        "description": (
            "The shared computer. One real browser with one sign-in, one set "
            "of cookies and one workspace, which you and the bots take turns "
            "at. Use it for anything with a page behind it.\n"
            "action=status: is it on, where is it, who is holding it.\n"
            "action=go / read: open a URL, or read what the current page says. "
            "Reading a page you are already on is free.\n"
            "action=click: click a selector, or x/y coordinates when there is "
            "no selector.\n"
            "action=fill: type into a field. To use a password the user has "
            "stored, pass secret:NAME (or {NAME}) instead of the password - "
            "the value is typed by the vault and never enters this "
            "conversation. A name that is not stored types nothing and fails.\n"
            "action=type / press / scroll / back: keyboard and navigation.\n"
            "action=screenshot: what the screen looks like right now.\n"
            "action=hand over: give the user the keyboard (mode=user) or take "
            "it back (mode=bot). Call it BEFORE a sign-in, 2FA or CAPTCHA and "
            "wait - while the user has control every bot action is refused, on "
            "purpose.\n"
            "Never retype a password the user typed for you, never guess one, "
            "and never work around a CAPTCHA. Ask for the hand-over instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "status | go | read | screenshot | "
                                          "click | fill | type | press | scroll "
                                          "| back | hand over | secrets"},
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string",
                         "description": "for fill/type, or secret:NAME"},
                "key": {"type": "string", "description": "for press"},
                "amount": {"type": "integer", "description": "for scroll"},
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "mode": {"type": "string",
                         "description": "for hand over: user | bot"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "skills",
        "description": (
            "Reusable methods. A skill is six parts: when to use it, what it "
            "needs, the steps, how to validate, what to return, and what needs "
            "human approval. Save a job you have done once and it can be rerun "
            "unattended.\n"
            "action=list: every saved skill, and whether it is read-only.\n"
            "action=show: the full procedure.\n"
            "action=create: save a method. ALL SIX parts are required - a "
            "procedure that has not decided where it stops is refused rather "
            "than run. approval='none' means genuinely read-only; say it only "
            "when nothing is sent, spent, or changed.\n"
            "action=run: do it once, with the one-off task. dry_run=true shows "
            "exactly what it would be told without doing it.\n"
            "Do the task properly once first. A skill saved from a job that did "
            "not work is a machine for repeating the mistake."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING",
                           "description": "list | show | create | run | delete"},
                "name": {"type": "STRING"},
                "when": {"type": "STRING", "description": "when to use it"},
                "inputs": {"type": "STRING", "description": "what it needs"},
                "steps": {"type": "STRING", "description": "the work, in order"},
                "validate": {"type": "STRING", "description": "how to check it worked"},
                "returns": {"type": "STRING", "description": "what comes back"},
                "approval": {"type": "STRING", "description": "what needs a human"},
                "task": {"type": "STRING", "description": "the one-off job"},
                "bot": {"type": "STRING", "description": "which bot owns it"},
                "path": {"type": "STRING"},
                "overwrite": {"type": "BOOLEAN"},
                "dry_run": {"type": "BOOLEAN"},
            },
            "required": [],
        },
    },
    {
        "name": "crew",
        "description": (
            "The bots. Each one has its own name, role, memory and saved "
            "methods, and you message them individually. Use this when work "
            "belongs to a specialist rather than to you — 'ask the Researcher', "
            "'have the Ads Manager look at this'.\n"
            "action=list: the roster, with each bot's methods.\n"
            "action=brief: what one bot is and what it can reach.\n"
            "action=say: hand a message to a bot. If it asks for work, the bot "
            "does the work as itself, running its own method if one fits. "
            "force=work skips the talk/work judgement.\n"
            "Delegating does not skip the approval gate. A bot proposes exactly "
            "as you would, and anything that sends, spends or deletes still "
            "asks."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "list | brief | say"},
                "bot": {"type": "STRING", "description": "which bot"},
                "message": {"type": "STRING", "description": "what to tell it"},
                "force": {"type": "STRING", "description": "work | talk"},
                "path": {"type": "STRING", "description": "workspace"},
            },
            "required": [],
        },
    },
]
class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


def _exc_text(exc: BaseException) -> str:
    """Flatten an exception (and nested group members) so message matching
    sees reasons like 1008 that ExceptionGroup's own str() omits."""
    parts = [str(exc)]
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            parts.append(_exc_text(sub))
    return "\n".join(parts)


class JarvisLive:
    def __init__(self, ui: "JarvisUI"):
        self.ui             = ui
        self._asst_name     = "JARVIS"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._sched                    = None   # core/scheduler.py instance
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self._last_out_logged      = ""      # de-dupes a re-sent transcript tail
        # Push-to-talk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # `stream.write()` returns when the buffer accepts the audio, not when the
        # speaker has finished with it, so sound is still in the room after the
        # speaking flag drops. Streaming the microphone during that gap is how an
        # assistant ends up answering itself. Measured from the device rather than
        # guessed; see _play_audio.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        # Wall-clock time at which the audio written next will begin to sound.
        # The mouth is scheduled against this, never against "now": batches are
        # handed to the device far faster than they play, so "now" ran the lips
        # ahead of the words and cut every schedule short. 0 = nothing playing.
        self._play_cursor          = 0.0
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._briefing_active  = False          # True while the startup briefing is speaking
        self._speak_lock       = asyncio.Lock() # serializes model utterances (speak/briefing)
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary
        # Live rejects send_realtime_input while a tool_call is pending and
        # aborts the socket (1008) if the client keeps streaming after goAway.
        self._tool_call_pending = False
        self._live_input_blocked = False
        self._session_gen = 0  # bumps each connect; invalidates stale goAway rolls
        # Residual-1008 escalation: preview knobs are dropped one at a time as
        # idle aborts accumulate. `_total_idle_1008` never resets for the life
        # of the process — the old streak was wiped on every reconnect that
        # followed a >90 s session, so a periodic abort (long talk → 1008 →
        # reconnect) always read streak=1 and escalation never fired.
        # `_idle_1008_streak` only counts aborts that land close together
        # (a true storm) and drives the reconnect backoff.
        self._idle_1008_streak = 0
        self._total_idle_1008 = 0
        self._last_1008_at = 0.0
        self._compress_live = True   # dropped after the first idle 1008
        self._resumption_live = True # dropped after repeated idle 1008s
        self._uplink_hold_until = 0.0  # monotonic; drop mic frames until then

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it
        self._tuned_live    = True  # turn-taking / media / thinking knobs; same fallback

        # Gateway voice failover: "live" while Gemini Live owns the mic,
        # "gateway" while the STT→chat→TTS fallback loop is running. Text
        # entry points branch on this when self.session is None.
        self._voice_backend = "live"
        self._gw_history: list[dict] = []   # rolling ~10 turns, fallback chat only
        self._gw_turn_q: asyncio.Queue | None = None
        self._gw_whisper = None             # cached faster-whisper instance

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        from core import plugins as _plug
        self._plugin_registry = discover_plugins(
            plugins_dir=_plug.repo_dir(),
            core_tool_names=_core_names,
            # the persistent volume is scanned too, so a plugin added from the
            # panel is a capability and not a release
            extra_dirs=[d for d in [_plug.user_dir()] if d is not None],
            # Console gets the full boot transcript; the activity log gets only
            # what the user has to know about. Every plugin loading correctly is
            # the expected case and does not belong in their conversation.
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel

        # ── Wake word ────────────────────────────────────────────────────────
        # _awake gates the mic (see _listen_audio) and the background speakers.
        # It is True whenever wake word is OFF, so default behaviour is unchanged.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT

        # Restore the saved push-to-talk preference. Doing it here rather than
        # in __init__ means the hotkey thread only exists once there is a
        # session to talk to.
        if get_push_to_talk_enabled() and not SERVER_MODE:
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")
        # UI control surface for the Wake Word settings section.
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

    # ── Wake word: state machine ─────────────────────────────────────────────

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    def reload_plugins(self) -> dict:
        """Re-scan the plugin directories and swap the registry.

        Tool declarations are rebuilt from the registry on every turn, so there
        is nothing to restart: whatever this returns is callable on the next
        message. That is the entire point — a plugin should not need a deploy
        to become useful.
        """
        from core import plugins as _plug
        from core.plugin_loader import discover_plugins as _dp
        _core = set()
        try:
            _core |= self._action_registry.names()
        except Exception:
            pass
        reg = _dp(plugins_dir=_plug.repo_dir(), core_tool_names=_core,
                  logger=lambda m: print(f"[Plugins] {m}"),
                  extra_dirs=[d for d in [_plug.user_dir()] if d is not None],
                  notify=lambda m: self.ui.write_log(f"SYS: {m}"))
        self._plugin_registry = reg
        self.ui.get_plugins = reg.list_for_ui
        self.ui.get_plugin_settings = reg.settings_schemas
        return {"ok": True,
                "active": sorted(d.get("name", "") for d in reg.list_for_ui()
                                 if d.get("valid", True))}

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self._utter(instruction)
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop:
            return
        # Gateway fallback: no Live session, but the turn worker can still
        # take a typed command (skips STT, goes straight to chat + TTS).
        if not self.session:
            if self._voice_backend == "gateway":
                asyncio.run_coroutine_threadsafe(
                    self._gw_enqueue_text(text), self._loop)
            return
        # Respect wake-word sleep: a typed command must not be answered while
        # asleep either (the sleep gate is not just for the mic). Wake first with
        # "Hey Jarvis" or the WAKE NOW button.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        asyncio.run_coroutine_threadsafe(self._utter(text), self._loop)

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice. It is dropped when the
            # tail expires. What the guard learned about the room always stays.
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        # A window-scoped chord is a real limitation, not a detail — say it once
        # in the log so nobody wonders why it does nothing while another app is
        # focused. Reporting it must never be able to undo the thing it reports.
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            # Holding the key is also a way to wake it, so push-to-talk works
            # without having to say the wake word first.
            if self._wake_enabled and not self._awake:
                self._awake = True
                self._last_user_speech = time.monotonic()
        try:
            self.ui.set_state("LISTENING" if held else "SLEEPING")
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        # Browser playback keeps its own scheduled buffers — stop those too.
        self._flush_browser_audio()
        self._play_cursor = 0.0     # next batch starts a fresh timeline
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    # ── Server-mode helpers (dashboard callbacks) ──────────────────────────

    def _flush_browser_audio(self) -> None:
        """Tell connected browser players to stop pending audio (interrupt)."""
        if not (self._dashboard is not None and self._loop is not None):
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._dashboard.send_audio_control({"type": "flush"}),
                self._loop,
            )
        except Exception:
            pass

    def _toggle_mute(self) -> bool:
        """Dashboard mute button — returns the new muted state."""
        self.ui.muted = not self.ui.muted
        self.ui.write_log(
            f"SYS: Microphone {'muted' if self.ui.muted else 'unmuted'}."
        )
        return bool(self.ui.muted)

    def _on_key_saved(self) -> None:
        """A Gemini key was saved via the dashboard — release the connect wait."""
        try:
            if hasattr(self.ui, "mark_ready"):
                self.ui.mark_ready()
        except Exception:
            pass
        self.ui.write_log("SYS: API key saved — connecting...")

    def _on_remote_voice(self, voice: str) -> None:
        """GET/POST /api/voice changed the Live voice — rebuild the session."""
        try:
            save_voice(voice)
        except Exception:
            pass
        self._on_voice_change()

    def _remote_wake(self) -> None:
        """Dashboard WAKE / command wake — wake only, never toggle to sleep."""
        if self._wake_enabled and not self._awake:
            self.wake(reason="remote")

    async def _wait_audio_idle(self, timeout: float, quiet: float = 0.35) -> bool:
        """Wait until playback has been quiet for `quiet` seconds.

        True when the play loop is not speaking, the PCM queue is empty, and
        that state holds continuously — so a second utterance cannot start
        while the first is still in flight (the multi-voice failure mode).
        """
        deadline = time.monotonic() + timeout
        quiet_since: float | None = None
        while time.monotonic() < deadline:
            with self._speaking_lock:
                speaking = self._is_speaking
            q_empty = self.audio_in_queue is None or self.audio_in_queue.empty()
            if not speaking and q_empty:
                if quiet_since is None:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= quiet:
                    return True
            else:
                quiet_since = None
            await asyncio.sleep(0.05)
        return False

    async def _wait_speaking_started(self, timeout: float = 6.0) -> bool:
        """Wait until this turn has begun producing audio (or timed out)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._speaking_lock:
                if self._is_speaking:
                    return True
            if self.audio_in_queue is not None and not self.audio_in_queue.empty():
                return True
            if self._turn_done_event is not None and self._turn_done_event.is_set():
                # Turn finished without audio (text-only) — nothing more to wait for.
                return True
            await asyncio.sleep(0.05)
        return False

    async def _settle_after_turn(self) -> None:
        """Local playback going quiet is NOT the server finishing the turn.

        The mic uplink resumes the moment speaking stops. If the server is
        still finalising its response, streaming PCM in that window is what
        closes the socket with 1008 — always right after a large injected
        answer (briefing phase 2, first web_search readout). Wait for
        turn_complete, then gate the uplink briefly and drop stale mic frames.
        """
        if self._turn_done_event is not None:
            try:
                await asyncio.wait_for(self._turn_done_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        self._uplink_hold_until = time.monotonic() + 0.75
        if self.out_queue is not None:
            try:
                while True:
                    self.out_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

    async def _utter(self, text: str, *, wait_prev: bool = True, drain: bool = True) -> None:
        """Send one model utterance with exclusive access to the audio path.

        Holds `_speak_lock` for the whole generate→play cycle so stacked
        `speak()` calls (actions, briefing, monitors) cannot interleave two
        Live responses in `audio_in_queue`.
        """
        text = (text or "").strip()
        if not text:
            return
        async with self._speak_lock:
            if wait_prev:
                await self._wait_audio_idle(timeout=8.0)
            if not self.session:
                return
            # send_client_content during a pending tool_call or after goAway
            # aborts the socket (1008) → reconnect loop → audible chop.
            for _ in range(80):   # ≤8 s
                if not self.session or (
                    not self._tool_call_pending and not self._live_input_blocked
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                print("[JARVIS] utter dropped — tool/goAway still pending")
                return
            if not self.session:
                return
            if self._turn_done_event:
                self._turn_done_event.clear()
            # Mic frames queued while we waited for idle are stale relative
            # to this new turn — sending them after send_client_content
            # races the response and 1008s the socket (mid-answer abort).
            if self.out_queue is not None:
                try:
                    while True:
                        self.out_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": text}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[JARVIS] utter failed: {e}")
                return
            if drain:
                await self._wait_speaking_started(timeout=8.0)
                await self._wait_audio_idle(timeout=30.0)
                await self._settle_after_turn()

    def speak(self, text: str):
        if not self._loop:
            return
        if self.session:
            asyncio.run_coroutine_threadsafe(self._utter(text), self._loop)
        elif self._voice_backend == "gateway":
            asyncio.run_coroutine_threadsafe(
                self._gw_enqueue_text(text), self._loop)
        else:
            return

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _tool_declarations(self) -> list:
        """Inline + action + plugin + MCP declarations — one source for
        both the Live config and the gateway fallback's OpenAI tools.

        MCP is last and it is fail-soft: a server that is not running, or
        a tool whose schema the API would reject, must cost the model that
        one tool and nothing else. A connector going down should never be
        the reason the assistant stops answering.
        """
        decls = (TOOL_DECLARATIONS
                 + self._action_registry.get_tool_declarations()
                 + self._plugin_registry.get_tool_declarations())
        try:
            from core import mcp as _mcp
            for d in _mcp.declarations():
                try:
                    types.FunctionDeclaration(
                        name=d["name"], description=d["description"],
                        parameters=d["parameters"])
                    decls.append(d)
                except Exception as e:
                    print(f"[MCP] skipped {d.get('name')}: {type(e).__name__}")
            # Refresh on a thread. Reading the cache is instant, but the cache
            # only fills if something connects - and this runs on the startup
            # path, where blocking for a handshake would be the assistant
            # going silent rather than a slow tool.
            _mcp.refresh_async()
        except Exception as e:
            print(f"[MCP] unavailable: {type(e).__name__}: {e}")
        return decls
    def _system_prompt_text(self) -> str:
        """The full system instruction string (time + identity + memory +
        rendered prompt.txt). Shared by Live (`_build_config`) and the
        gateway voice fallback's chat `system` message, so the assistant
        never changes personality mid-failover."""
        from datetime import datetime

        # Load customization from config (data root on HF Spaces)
        try:
            from memory.config_manager import get_assistant_name, get_user_name
            self._asst_name = get_assistant_name()
            _user_name = get_user_name()
        except Exception:
            try:
                _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
                self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
                _user_name = (_cfg.get("user_name") or "").strip()
            except Exception:
                self._asst_name = "JARVIS"
                _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        # Address form is a property of the language being spoken, so it is
        # stated as a principle rather than a two-language lookup — the model
        # already knows the respectful register of whatever language it is in.
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Address the user with the ordinary respectful form '
                      'for a superior in the language you are currently speaking — '
                      '"sir" in English, its everyday equivalent in any other '
                      'language. Never an archaic or aristocratic form, and never '
                      'the form from a different language than the one you are '
                      'speaking in this sentence.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = self._tool_declarations()
        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": _describe_tools(_all_decls),
            "limits": _describe_limits(
                has_vision="screen_process" in _names,
                has_mic=True,
            ),
        })

        # Phase 6: what we already know about what they are talking about.
        # Query-scoped, so it costs ~700 characters rather than the whole
        # library, and the model can search for anything else on demand.
        try:
            from core import knowledge as _kn
            _know = _kn.prompt_block()
        except Exception:
            _know = ""

        # Phase 9: the character, and what it is working on. Both are prompt
        # blocks rather than code, because a personality and a position are both
        # things the model has to be *told* every turn — it cannot remember
        # either, and asking it to would be a lie about what it can do.
        try:
            from core import character as _ch
            _character = _ch.style_block()
        except Exception:
            _character = ""
        try:
            from core import goals as _gl
            _working = _gl.prompt_block()
        except Exception:
            _working = ""

        parts = [time_ctx, identity_ctx]
        try:
            _con = _connector_block()
        except Exception:
            _con = ""
        if _con:
            parts.append(_con)
        if mem_str:
            parts.append(mem_str)
        if _know:
            parts.append(_know)
        if _working:
            parts.append(_working)
        if _character:
            parts.append(_character)
        parts.append(sys_prompt)
        return "\n".join(parts)

    def _build_config(self) -> types.LiveConnectConfig:
        sys_prompt = self._system_prompt_text()
        _all_decls = self._tool_declarations()

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=sys_prompt,
            tools=[{"function_declarations": _all_decls}],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        # Hand back the handle captured from the last session_resumption
        # update. `handle=None` is exactly the old behaviour (ask for
        # handles, start fresh), so the first connect of a run is unchanged.
        # Omitted entirely after repeated idle 1008s (preview knob).
        if self._resumption_live:
            cfg["session_resumption"] = types.SessionResumptionConfig(
                handle=self._resume_handle
            )
        # Sliding-window compression: session never dies from a full context
        # window. Off after repeated idle 1008s — it is a preview knob and
        # was the only config change still live when sessions aborted with
        # zero uplink (tool=False blocked=False, no goAway).
        if self._compress_live:
            cfg["context_window_compression"] = types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in config/api_keys.json to true to let it reason instead.
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    def _policy_actor(self) -> str:
        """Who is asking. Voice is the user; a dashboard/phone command is
        'remote'; a scheduled job says 'scheduler' and is held to the same
        policy, which is the entire point of a policy engine."""
        return str(getattr(self, "_actor", "") or "user")

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        """Every model tool call goes through here, so this is the only place
        that has to know about policy. Phase 5.

        Three outcomes: blocked (hard stop, nothing ran), approved-by-human
        (waited, then ran), or allowed (ran straight away). Every one of them is
        written to the audit log, including the ones that never happened.
        """
        from core import policy as _pol
        name = fc.name
        args = dict(fc.args or {})
        actor = self._policy_actor()
        d = _pol.gate(name, args, actor=actor)
        if not d.allowed:
            print(f"[Policy] BLOCKED {name} ({d.tier}) \u2014 {d.reason}")
            if self._dashboard:
                try:
                    asyncio.create_task(self._dashboard.broadcast({
                        "type": "policy",
                        "event": "blocked", "tool": name, "tier": d.tier,
                        "reason": d.reason}))
                except Exception:
                    pass
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": ("I did not do that: " + d.reason
                                     + " Say it differently, or change the "
                                       "policy in the dashboard.")})
        if d.needs_approval:
            from core import confirm as _cf
            what = d.reason or name
            print(f"[Policy] {name} ({d.tier}) needs approval \u2014 asking")
            try:
                _ploop = self._loop or asyncio.get_event_loop()
                ok = await _cf.request_async(
                    f"policy:{name}", f"Approve: {name}", what, _ploop)
            except Exception as e:
                print(f"[Policy] gate failed ({e}) \u2014 refusing")
                ok = False
            if not ok:
                _pol.audit(name, d.tier, actor=actor, result="declined",
                           detail=what)
                if self._dashboard:
                    try:
                        asyncio.create_task(self._dashboard.broadcast({
                            "type": "policy", "event": "declined",
                            "tool": name, "tier": d.tier}))
                    except Exception:
                        pass
                return types.FunctionResponse(
                    id=fc.id, name=name,
                    response={"result": ("The user did not approve that, so I "
                                         "did not do it.")})
            _pol.audit(name, d.tier, actor=actor, result="approved", detail=what)
            if self._dashboard:
                try:
                    asyncio.create_task(self._dashboard.broadcast({
                        "type": "policy", "event": "approved", "tool": name,
                        "tier": d.tier}))
                except Exception:
                    pass
        return await self._execute_tool_ungated(fc)

    async def _execute_tool_ungated(self, fc) -> types.FunctionResponse:
        """The dispatch itself, with no policy in front of it.

        Split out from _execute_tool so the gate can wrap it: an approved
        action has to be able to run the exact same code path it would have
        taken without asking.
        """
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")


        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    # The image is attached to this same exchange, so there is
                    # nothing to stall for and nothing to announce. Asking for an
                    # acknowledgement here is what produced two spoken answers —
                    # the model filled that turn by answering the question from
                    # imagination, then answered it again once it could see.
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured and attached to this "
                        f"same exchange. Do not acknowledge and do not answer yet — the image "
                        f"is arriving with this result. Reply once, from what you actually see "
                        f"in it."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "manage_schedule":
                from core import scheduler as _sched_mod
                result = await asyncio.to_thread(
                    _sched_mod.manage,
                    args.get("action", ""),
                    name=args.get("name", "") or "",
                    kind=(args.get("kind", "") or "").strip().lower(),
                    spec=args.get("spec", "") or "",
                    prompt=args.get("prompt", "") or "",
                    job_id=args.get("job_id", "") or "",
                )
                if self._dashboard:
                    asyncio.create_task(self._dashboard.broadcast(
                        {"type": "job", "by": "model"}))

            elif name == "display":
                from core import display as _disp
                action = (args.get("action") or "show").strip().lower()
                if action in ("list", "show_list"):
                    rows = await asyncio.to_thread(_disp.listing, 12)
                    if not rows:
                        result = ("The screen is empty. Ask me to show you "
                                  "something and it will appear there.")
                    else:
                        result = "On the display: " + "; ".join(
                            f"{r['title']} ({r['kind']})" for r in rows[:6])
                elif action in ("close", "clear", "hide"):
                    for r in await asyncio.to_thread(_disp.listing, 10):
                        await asyncio.to_thread(_disp.delete, r["id"])
                    if self._dashboard:
                        asyncio.create_task(self._dashboard.broadcast(
                            {"type": "display", "event": "cleared"}))
                    result = "Cleared the display."
                elif action in ("pin", "unpin"):
                    aid = str(args.get("id") or "").strip()
                    rows = await asyncio.to_thread(_disp.listing, 20)
                    target = next((r for r in rows if r["id"] == aid), None) or (rows[0] if rows else None)
                    if not target:
                        result = "There is nothing on the display to pin."
                    else:
                        got = await asyncio.to_thread(
                            _disp.set_pinned, target["id"], action == "pin")
                        result = (f"{'Pinned' if got and got.get('pinned') else 'Unpinned'} "
                                  f"{target['title']}.")
                else:
                    kind = str(args.get("kind") or ("url" if action == "embed"
                                                    else "image" if action == "image"
                                                    else "html")).lower()
                    spec = args.get("spec")
                    if isinstance(spec, str):
                        try:
                            spec = json.loads(spec)
                        except Exception:
                            spec = None
                    try:
                        rec = await asyncio.to_thread(
                            _disp.save, kind,
                            title=str(args.get("title") or ""),
                            html=str(args.get("html") or ""),
                            url=str(args.get("url") or ""),
                            text=str(args.get("text") or ""),
                            spec=spec if isinstance(spec, dict) else None,
                            warning=str(args.get("warning") or ""),
                            source="model")
                    except ValueError as e:
                        result = f"I could not show that: {e}"
                    else:
                        result = _disp.describe(rec) + (
                            " Tell the user what they are looking at in one "
                            "short sentence, and stop.")
                        if self._dashboard:
                            asyncio.create_task(self._dashboard.broadcast(
                                {"type": "display", "event": "shown", "kind": rec["kind"], "source": rec.get("source", "model"),
                                 "id": rec["id"]}))

            elif name == "globe":
                from core import godseye as _gev
                g_action = (args.get("action") or "show").strip().lower()
                g_place = str(args.get("place") or "").strip()
                g_from = str(args.get("route_from") or "").strip()
                g_to = str(args.get("route_to") or "").strip()
                g_show = args.get("show_layers")
                g_show = [str(x) for x in g_show] if isinstance(g_show, list) else []
                g_hide = args.get("hide_layers")
                g_hide = [str(x) for x in g_hide] if isinstance(g_hide, list) else []
                g_cmds: list = []
                try:
                    if g_from and g_to:
                        g_cmds = (await asyncio.to_thread(_gev.route, g_from, g_to))["commands"]
                    else:
                        g_cmds = await asyncio.to_thread(
                            _gev.build_commands,
                            place=g_place,
                            style=str(args.get("style") or "").strip().lower(),
                            zoom=str(args.get("zoom") or "").strip().lower(),
                            show=g_show, hide=g_hide,
                            iss=bool(args.get("iss")),
                            nearest_aircraft=bool(args.get("nearest_aircraft")))
                except ValueError as e:
                    result = str(e)
                else:
                    if g_action == "layers":
                        st = _gev.state()
                        result = (
                            f"God\u2019s Eye View is "
                            f"{'running' if st['api_alive'] else 'running without its data server'}"
                            f" with {st['layers']} layers available "
                            f"({', '.join(st['layers'][:8]) if st.get('layers') else ''}"
                            f"\u2026). Layers you can ask for by name.")
                    else:
                        result = _gev.describe(g_cmds, live=_gev.api_alive()) + (
                            " Tell the user what they are looking at in one short "
                            "sentence, and stop.")
                        if self._dashboard:
                            asyncio.create_task(self._dashboard.broadcast(
                                {"type": "godseye", "commands": g_cmds,
                                 "url": _gev.url()}))
            elif name in ("agents", "brief", "deliverable"):
                from core import agents as _ag
                _ag.seed()
                a_act = (args.get("action") or "").strip().lower()
                if name == "agents":
                    if a_act in ("", "org", "overview", "status"):
                        o = _ag.org()
                        rate = ("n/a" if o["success_rate"] is None
                                else f"{round(o['success_rate'] * 100)}%")
                        result = (
                            f"Company: {o['active']}/{o['agents']} agents active, "
                            f"{o['working']} working. {o['runs_today']} runs today "
                            f"({o['ok_today']} ok, {rate} success). "
                            f"{o['deliverables']} deliverables. "
                            f"${o['spent_usd_today']:.2f} spent today.")
                    elif a_act in ("roster", "list", "ls"):
                        rows = _ag.roster()
                        if not rows:
                            result = "No agents yet \u2014 hire the first one."
                        else:
                            lines = []
                            for r in rows:
                                bit = (f"${r['spent_usd_total']:.2f} all-time"
                                       if r.get("spent_usd_total") else "")
                                tools = (" ".join(r.get("tools") or [])
                                         or "no tools")
                                lines.append(f"- {r['name']} "
                                             f"({r.get('role', 'other')}), "
                                             f"{r['status']}, tools: {tools}"
                                             + (f", {bit}" if bit else ""))
                            result = "\n".join(lines)
                    elif a_act in ("hire", "add", "new", "update", "edit"):
                        result = _agents_hire(_ag, args,
                                              float(args.get("budget_usd_day") or 0))
                    elif a_act in ("retire", "disable", "off"):
                        result = _agents_retire(_ag, args, delete=False)
                    elif a_act in ("delete", "fire", "remove"):
                        result = _agents_retire(_ag, args, delete=True)
                    else:
                        result = ("Unknown action. Use org / roster / hire / "
                                  "retire / delete.")
                elif name == "brief":
                    if a_act in ("list", "ls"):
                        result = "\n".join(
                            f"- {r['name']}: "
                            f"{' '.join(r.get('tools') or []) or 'no tools'}"
                            for r in _ag.roster()) or "No agents yet."
                    else:
                        who = str(args.get("agent") or "")
                        cost = float(args.get("cost_usd") or 0)
                        ok, why = _ag.may_spend(who, cost)
                        if not ok:
                            result = "I can't hand that out: " + why
                        else:
                            w = _ag.brief(who, str(args.get("job") or ""),
                                          deliverable_kind=str(args.get("kind")
                                                               or "note"),
                                          extra=str(args.get("context") or ""))
                            if cost:
                                _ag.record_spend(who, cost, what="brief")
                            result = (f"Work order for {w['agent']} "
                                      f"({len(w['text'])} chars, tools: "
                                      f"{', '.join(w['tools']) or 'none'}):\n\n"
                                      f"{w['text']}")
                else:
                    if a_act in ("", "list", "ls"):
                        rows = _ag.deliverables(
                            kind=str(args.get("kind") or ""),
                            agent=str(args.get("agent") or ""))
                        if not rows:
                            result = "No deliverables yet."
                        else:
                            result = "\n".join(
                                f"- {d['title']} (v{d['version']}, {d['kind']}, "
                                f"by {d['agent']}, {d['status']})"
                                for d in rows[:20])
                    elif a_act in ("record", "add", "new"):
                        d = _ag.deliver(str(args.get("agent") or "JARVIS"),
                                        str(args.get("kind") or "note"),
                                        str(args.get("title") or ""),
                                        str(args.get("body") or ""),
                                        summary=str(args.get("summary") or ""))
                        _ag.log_run(d["agent"], job=d["title"], ok=True,
                                    deliverable_id=d["id"])
                        result = (f"Filed '{d['title']}' as v{d['version']} "
                                  f"({d['id']}) by {d['agent']}.")
                    elif a_act in ("review", "grade"):
                        d = _ag.review(str(args.get("ref") or ""),
                                       str(args.get("reviewer") or "JARVIS"),
                                       str(args.get("verdict") or "pass"),
                                       str(args.get("note") or ""))
                        result = (f"Reviewed '{d['title']}': "
                                  f"{len(d['reviews'])} review(s) on record.")
                    elif a_act in ("delete", "remove"):
                        gone = _ag.delete_deliverable(str(args.get("ref") or ""))
                        result = gone or "No deliverable matched."
                    else:
                        result = "Unknown action. Use list / record / review / delete."
            elif name in ("github", "vercel", "hf"):
                from core import github as _gh, vercel as _vc, hf as _hf
                _a = str(args.get("action") or "")
                _lim = int(args.get("limit") or 15)
                if name == "github":
                    result = _gh.tool(_a, repo=str(args.get("repo") or ""),
                                      number=str(args.get("number") or ""),
                                      text=str(args.get("text") or ""),
                                      title=str(args.get("title") or ""),
                                      limit=_lim,
                                      branch=str(args.get("branch") or ""))
                elif name == "vercel":
                    result = _vc.tool(_a, project=str(args.get("project") or ""),
                                      uid=str(args.get("uid") or ""),
                                      limit=_lim)
                else:
                    result = _hf.tool(_a, query=str(args.get("query") or ""),
                                      id=str(args.get("id") or ""), limit=_lim)

            elif name == "crew":
                from core import crew as _cw
                # A bot handed real work runs the agent loop, which can be
                # minutes, and can now also drive the browser. Inline, that
                # would freeze the voice socket and every panel for the whole
                # run — the dashboard has to stay usable while a bot works,
                # because watching it work is the point.
                result = await asyncio.to_thread(
                    _cw.tool,
                    str(args.get("action") or ""),
                    bot=str(args.get("bot") or ""),
                    message=str(args.get("message") or ""),
                    path=str(args.get("path") or ""),
                    force=str(args.get("force") or ""))
            elif name == "skills":
                from core import skills as _sk
                result = _sk.tool(
                    str(args.get("action") or ""),
                    name=str(args.get("name") or ""),
                    when=str(args.get("when") or ""),
                    inputs=str(args.get("inputs") or ""),
                    steps=str(args.get("steps") or ""),
                    validate=str(args.get("validate") or ""),
                    returns=str(args.get("returns") or ""),
                    approval=str(args.get("approval") or ""),
                    task=str(args.get("task") or ""),
                    bot=str(args.get("bot") or ""),
                    path=str(args.get("path") or ""),
                    overwrite=bool(args.get("overwrite")),
                    dry_run=bool(args.get("dry_run")))
            elif name == "phone":
                from core import phone as _ph
                result = _ph.tool(
                    str(args.get("action") or ""),
                    device=str(args.get("device") or ""),
                    title=str(args.get("title") or ""),
                    body=str(args.get("body") or ""),
                    url=str(args.get("url") or ""),
                    pattern=str(args.get("pattern") or ""),
                    kind=str(args.get("kind") or ""))
            elif name == "welcome":
                from core import ceremony as _wel
                # the weather call is a network round-trip and opening panels
                # is a real browser, so it leaves the loop
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        _wel.tool, str(args.get("action") or ""),
                        panel=str(args.get("panel") or "")),
                    timeout=180)
            elif name == "computer":
                from core import computer as _cp
                # Off the event loop. A cold launch is a real Chromium start
                # and a navigation is a real page load; called inline, both
                # would freeze the voice socket, the panels and every other
                # request for as long as the browser took. The computer keeps
                # its own loop, so the wait belongs on a worker thread.
                result = await asyncio.to_thread(
                    _cp.tool,
                    str(args.get("action") or ""),
                    url=str(args.get("url") or ""),
                    selector=str(args.get("selector") or ""),
                    text=str(args.get("text") or ""),
                    key=str(args.get("key") or ""),
                    amount=int(args.get("amount") or 400),
                    x=int(args.get("x") or 0), y=int(args.get("y") or 0),
                    bot=str(args.get("bot") or "assistant"),
                    mode=str(args.get("mode") or ""),
                    value=str(args.get("note") or ""))
            elif name == "coder":
                from core import coder as _coder
                result = _coder.tool(
                    str(args.get("action") or ""),
                    goal=str(args.get("goal") or ""),
                    path=str(args.get("path") or ""),
                    read=str(args.get("read") or ""),
                    pattern=str(args.get("pattern") or ""))
            elif name == "mcp":
                from core import mcp as _mcpt
                result = _mcpt.tool(
                    str(args.get("action") or ""),
                    name=str(args.get("name") or ""),
                    command=str(args.get("command") or ""),
                    args=str(args.get("args") or ""),
                    env=str(args.get("env") or ""),
                    url=str(args.get("url") or ""),
                    trusted=bool(args.get("trusted")),
                    call=str(args.get("call") or ""),
                    arguments=str(args.get("arguments") or ""))
            elif name.startswith("mcp__"):
                from core import mcp as _mcp
                try:
                    result = _mcp.route(name, dict(args or {}))
                except _mcp.MCPError as e:
                    result = f"That tool did not work: {e.message}"
            elif name == "email":
                from core import mail as _m
                _st = None
                result = _m.tool(
                    str(args.get("action") or ""),
                    to=str(args.get("to") or ""),
                    subject=str(args.get("subject") or ""),
                    body=str(args.get("body") or ""),
                    limit=int(args.get("limit") or 10),
                    unread=bool(args.get("unread")),
                    query=str(args.get("query") or ""),
                    store=None)

            elif name == "goals":
                from core import goals as _goals
                result = _goals.tool(
                    action=str(args.get("action") or ""),
                    ref=str(args.get("ref") or args.get("title") or ""),
                    **{k: v for k, v in args.items()
                       if k not in ("action", "ref", "title", "type")})

            elif name in ("journal", "calendar", "files", "home", "browser",
                          "proactive", "voice"):
                a_act = (args.get("action") or "").strip().lower()
                if name == "journal":
                    from core import journal as _j
                    if a_act in ("", "list", "recent"):
                        rows = _j.recent(int(args.get("days") or 30),
                                         kind=str(args.get("kind") or ""), limit=25)
                        st = _j.stats()
                        result = (f"{st['entries']} entries over "
                                  f"{st['days']} day(s) since {st['first_day']}.\n"
                                  + ("\n".join(
                                      f"- [{_day(e['at'])} · {e['kind']}] {e['title']}"
                                      for e in rows[:15]) or "nothing yet"))
                    elif a_act in ("search", "find", "recall"):
                        rows = _j.search(str(args.get("query") or args.get("title") or ""),
                                         days=int(args.get("days") or 90), limit=12)
                        result = ("\n".join(
                            f"- [{_day(e['at'])}] {e['title']}"
                            + (f" — {e['body'][:100]}" if e.get("body") else "")
                            for e in rows)
                            or f"Nothing in the journal about "
                               f"'{args.get('query') or args.get('title')}'.")
                    elif a_act in ("add", "note", "decide", "lesson"):
                        kind = ("decision" if a_act == "decide"
                                else "lesson" if a_act == "lesson" else "note")
                        tags = [t for t in str(args.get("tags") or "").split(",")
                                if t.strip()]
                        rec = _j.entry(kind, str(args.get("title") or ""),
                                       str(args.get("body") or ""), tags=tags)
                        result = f"Noted ({rec['id']}): {rec['title']}"
                    elif a_act in ("digest", "today"):
                        result = _j.digest()
                    else:
                        result = "Unknown action. Use add / search / list / digest."
                elif name == "calendar":
                    from core import gcal as _g
                    if a_act in ("", "list", "events", "upcoming"):
                        evs = _g.events(int(args.get("days") or 7))
                        if not evs:
                            result = ("Nothing on the calendar. "
                                      + ("Google is not connected yet, so this is "
                                         "the local .ics file." if not _g.configured()
                                         else ""))
                        else:
                            lines = []
                            for e in evs[:12]:
                                t = _g._parse_when(e.get("start"))
                                when = t.strftime("%a %d %b %H:%M") if t else "?"
                                lines.append(f"- {when}  {e['title']}"
                                             + (f"  @{e['where']}" if e.get("where") else ""))
                            result = f"{len(evs)} coming up:\n" + "\n".join(lines)
                    elif a_act in ("agenda", "brief"):
                        result = _g.agenda()
                    elif a_act in ("add", "create", "book", "schedule"):
                        ev = _g.create(str(args.get("title") or ""),
                                       args.get("start"),
                                       end=str(args.get("end") or ""),
                                       minutes=int(args.get("minutes") or 60),
                                       where=str(args.get("where") or ""),
                                       notes=str(args.get("notes") or ""))
                        when = _g._parse_when(ev.get("start"))
                        result = (f"Added '{ev['title']}' for "
                                  f"{when.strftime('%A %H:%M') if when else ev['start']}"
                                  f" ({ev['source']}).")
                    elif a_act in ("delete", "remove", "cancel"):
                        result = str(_g.delete(str(args.get("ref")
                                                  or args.get("title") or "")))
                    else:
                        result = "Unknown action. Use list / agenda / add / delete."
                elif name == "files":
                    from core import files as _f
                    if a_act in ("", "list", "find", "search"):
                        rows = _f.find(str(args.get("query") or ""),
                                       tag=str(args.get("tag") or ""),
                                       client=str(args.get("client") or ""))
                        st = _f.stats()
                        result = (f"{st['files']} file(s), {st['indexed']} indexed, "
                                  f"{st['bytes'] // 1024}KB.\n"
                                  + ("\n".join(
                                      f"- {r['name']}"
                                      + (f"  [{r['client']}]" if r.get("client") else "")
                                      + (f"  v{r.get('extracted')}" if r.get("extracted")
                                         and r["extracted"] != "none" else "")
                                      for r in rows[:20]) or "nothing matching"))
                    elif a_act in ("read", "text", "open"):
                        body = _f.text_of(str(args.get("id") or ""))
                        rec = _f.get(str(args.get("id") or "")) or {}
                        result = (f"{rec.get('name', 'file')}"
                                  + (f" — no text I could read" if not body else "")
                                  + (f"\n\n{body[:8000]}" if body else ""))
                    elif a_act == "attach":
                        result = str(_f.attach(str(args.get("id") or ""),
                                               client=str(args.get("client") or ""),
                                               deliverable=str(args.get("deliverable") or "")))
                    elif a_act in ("upload", "store"):
                        import base64
                        raw = base64.b64decode(str(args.get("data_b64") or ""))
                        rec = _f.store(raw, str(args.get("filename") or "file"),
                                       title=str(args.get("title") or ""),
                                       client=str(args.get("client") or ""),
                                       actor="assistant")
                        result = f"Stored {rec['name']} ({rec['bytes']} bytes)."
                    elif a_act in ("delete", "remove"):
                        result = str(_f.delete(str(args.get("id") or "")))
                    else:
                        result = "Unknown action. Use list / read / attach / upload / delete."
                elif name == "home":
                    from core import home as _h
                    _h.seed()
                    if a_act in ("", "list", "status", "devices"):
                        rows = _h.devices()
                        scs = _h.scenes()
                        result = (rows and "\n".join(
                            f"- {d['name']} ({d['kind']}) — {d['state']}"
                            + ("  [security gated]" if d.get("gated") else "")
                            for d in rows) or "no devices yet")
                        if scs:
                            result += "\nScenes: " + ", ".join(
                                s["name"] for s in scs)
                    elif a_act in ("run", "device_run"):
                        r = _h.run_device(str(args.get("name") or ""),
                                          str(args.get("op") or ""),
                                          dry_run=bool(args.get("dry_run")))
                        if r.get("needs_approval"):
                            result = f"I need your OK: {r['reason']}"
                        else:
                            result = (f"{r['device']} → {r.get('state', r['op'])}"
                                      + (" (dry run, nothing happened)"
                                         if r.get("dry_run") else "")
                                      + (f" — {r['error']}" if not r.get("ok") else ""))
                    elif a_act in ("run_scene", "scene_run", "scene"):
                        r = _h.run_scene(str(args.get("name") or ""),
                                         dry_run=bool(args.get("dry_run")))
                        if r.get("dry_run") and r.get("reason"):
                            result = f"{r['scene']}: {r['reason']}"
                        else:
                            result = (f"{r['scene']}: {r.get('worked', 0)}/"
                                      f"{r.get('steps', 0)} steps worked")
                    elif a_act == "device":
                        r = _h.device(str(args.get("name") or ""),
                                      str(args.get("kind") or "virtual"),
                                      where=str(args.get("where") or ""))
                        result = f"{r['name']} registered ({r['kind']})."
                    else:
                        result = "Unknown action. Use list / run / run_scene / device."
                elif name == "browser":
                    from core import browser as _b
                    if a_act in ("", "open", "read", "fetch", "visit"):
                        r = _b.open_page(str(args.get("url") or ""))
                        body = " ".join(str(r["text"]).split())
                        result = (f"{r['title']}  ({r['url']}, {r['chars']} chars)\n"
                                  + body[:6000]
                                  + (f"\n\nLinks: "
                                     + ", ".join(l["text"] for l in r["links"][:12])
                                     if r.get("links") else ""))
                    elif a_act in ("click", "go"):
                        r = _b.click_and_read(str(args.get("url") or ""),
                                              str(args.get("text") or ""))
                        result = (f"{r['title']}  ({r['url']})\n"
                                  + " ".join(str(r["text"]).split())[:5000])
                    elif a_act == "status":
                        rows = _b.status_of(list(args.get("urls") or
                                                 [str(args.get("url") or "")]))
                        result = "\n".join(
                            f"- {r['url']}: " + ("up" if r.get("ok")
                                                 else f"down ({r.get('error','')})")
                            for r in rows)
                    elif a_act in ("allow", "allowlist"):
                        result = str(_b.allow(str(args.get("host") or args.get("url") or "")))
                    elif a_act == "deny":
                        result = str(_b.deny(str(args.get("host") or args.get("url") or "")))
                    elif a_act in ("list", "allowed"):
                        result = ("Allowed: " + (", ".join(
                            h["host"] for h in _b.allowed() if h["on"]) or "nothing")
                                  or "nothing is on the allowlist yet")
                    else:
                        result = "Unknown action. Use open / click / status / allow / deny."
                elif name == "proactive":
                    from core import proactive as _p
                    if a_act in ("", "status", "boundary"):
                        st = _p.status()
                        result = (_p.describe() + f"\n{st['ledger']} actions logged, "
                                  f"{st['unattended_today']} today.")
                    elif a_act in ("briefing", "brief", "morning"):
                        result = _p.briefing(force=True)
                    elif a_act in ("check", "classify"):
                        v = _p.classify(str(args.get("text") or ""))
                        result = f"{v['verdict'].upper()} — {v['why']}"
                    elif a_act in ("watch", "add"):
                        w = _p.watch(str(args.get("name") or ""),
                                     str(args.get("prompt") or ""),
                                     every=str(args.get("every") or "6h"),
                                     kind=str(args.get("kind") or "check"))
                        result = f"Watching '{w['name']}' every {w['every']}. "\
                                 f"I will look and tell you, not act."
                    elif a_act in ("unwatch", "remove"):
                        result = str(_p.unwatch(str(args.get("name") or "")))
                    else:
                        result = "Unknown action. Use status / briefing / check / watch."
                else:  # voice
                    from core import voice as _v
                    if a_act in ("", "status", "state"):
                        st = _v.status()
                        result = (_v.describe()
                                  + f" State machine: {' → '.join(st['states'])}."
                                  + (f" Last heard: \"{st['last']['text'][:80]}\""
                                     if st.get("last") else ""))
                    elif a_act in ("history", "turns"):
                        rows = _v.history(int(args.get("limit") or 10))
                        result = ("\n".join(
                            f"- [{_hhm(r.get('at'))}] {r.get('text', '')[:100]}"
                            for r in rows) or "no voice turns yet")
                    elif a_act in ("settings", "set", "configure"):
                        kw = {k: v for k, v in args.items()
                              if k in ("barge_in", "silence_ms", "wake_word",
                                       "auto_listen", "tts")}
                        s = _v.set_settings(**kw) if kw else _v.status()["settings"]
                        result = "Voice settings: " + ", ".join(
                            f"{k}={v}" for k, v in s.items())
                    else:
                        result = "Unknown action. Use status / history / settings."

            elif name == "knowledge":
                from core import knowledge as _kn
                k_action = (args.get("action") or "search").strip().lower()
                try:
                    k_limit = int(args.get("limit") or 5)
                except (TypeError, ValueError):
                    k_limit = 5
                k_query = str(args.get("query") or "").strip()
                if k_action in ("add", "note", "remember"):
                    k_text = str(args.get("text") or "").strip()
                    k_title = str(args.get("title") or k_query or "note").strip()
                    if not k_text:
                        result = "Give me something to remember (the text field)."
                    else:
                        try:
                            rec = await asyncio.to_thread(
                                _kn.ingest, k_title, k_text,
                                source=str(args.get("source") or "note"),
                                meta={"by": "jarvis"})
                            result = (f"Remembered \u2018{rec['title']}\u2019 "
                                      f"({rec['chunks']} part"
                                      f"{'s' if rec['chunks'] != 1 else ''}). "
                                      f"I will recall it when it comes up.")
                        except ValueError as e:
                            result = f"I could not store that: {e}"
                elif k_action in ("add_url", "url", "fetch"):
                    k_url = str(args.get("url") or "").strip()
                    if not k_url.startswith("http"):
                        result = "That is not a URL I can fetch."
                    else:
                        try:
                            k_title, k_text = await asyncio.to_thread(
                                _kn.fetch_url, k_url)
                            rec = await asyncio.to_thread(
                                _kn.ingest, str(args.get("title") or k_title)[:160],
                                k_text, source="url", uri=k_url)
                            result = (f"Stored \u2018{rec['title']}\u2019 from that "
                                      f"page ({rec['chunks']} parts).")
                        except Exception as e:
                            result = f"I could not read that page: {str(e)[:120]}"
                elif k_action in ("list", "docs"):
                    rows = await asyncio.to_thread(_kn.docs, 30)
                    result = ("Nothing stored yet."
                              if not rows else
                              "Stored: " + "; ".join(
                                  f"{r['id']}: {r['title']} ({r['chunks']}p)"
                                  for r in rows[:20]))
                elif k_action in ("forget", "delete", "remove"):
                    try:
                        k_id = int(args.get("id"))
                    except (TypeError, ValueError):
                        result = "Which one? Give me the document id."
                    else:
                        ok = await asyncio.to_thread(_kn.forget, k_id)
                        result = ("Forgotten." if ok
                                  else f"No document with id {k_id}.")
                elif k_action in ("stats", "status"):
                    st = await asyncio.to_thread(_kn.stats)
                    result = (f"{st['docs']} documents, {st['chunks']} parts "
                              f"({st['embedded_chunks']} searchable), "
                              f"{st['turns']} conversation turns, "
                              f"{st['mode']} search.")
                elif k_action in ("recent", "said", "transcript"):
                    rows = await asyncio.to_thread(_kn.recent_turns, 12)
                    result = ("Nothing recorded yet." if not rows else " | ".join(
                        f"{r['role']}: {r['text'][:90]}" for r in rows))
                else:
                    if not k_query:
                        result = "What should I look for?"
                    else:
                        hits = await asyncio.to_thread(
                            # The parameter is `k`, not `k_limit`. The wrong name
                            # raised TypeError on every knowledge lookup, so
                            # search was never short of documents — it was
                            # broken by a typo, and the model read a traceback
                            # instead of an answer.
                            _kn.search, k_query, k=k_limit)
                        if not hits:
                            result = (f"Nothing in memory about that. If the user "
                                      f"tells you, use action=add to keep it.")
                        else:
                            parts_out = []
                            for h in hits:
                                where = h.get("title") or h.get("source") or "memory"
                                parts_out.append(f"[{where}] "
                                                f"{str(h.get('text') or '')[:220]}")
                            result = ("What I already know:\n" + "\n".join(parts_out)
                                      + "\nUse this instead of asking again.")

            elif name == "maps":
                from core import maps as _maps
                action = (args.get("action") or "find").strip().lower()
                provider = (args.get("provider") or "google").strip().lower()
                if provider not in _maps.PROVIDERS:
                    provider = "google"
                query = str(args.get("query") or "").strip()
                if action in ("find", "nearby", "link", "pin"):
                    if not query:
                        result = "What should I look for?"
                    else:
                        found = await asyncio.to_thread(_maps.search, query, limit=3)
                        if not found:
                            result = f"I could not find '{query}'."
                        else:
                            top = found[0]
                            origin = None
                            if action == "nearby":
                                try:
                                    from core import phone as _phone
                                    loc = (_phone.state() or {}).get("location") or {}
                                    if loc.get("lat") is not None:
                                        origin = {"lat": loc["lat"],
                                                  "lon": loc["lon"]}
                                except Exception:
                                    origin = None
                            lines = [f"{i + 1}. {_maps.describe(p, origin)}"
                                     for i, p in enumerate(found)]
                            if action == "pin":
                                saved = await asyncio.to_thread(
                                    _maps.save_pin, top,
                                    str(args.get("name") or ""))
                                if "error" in saved:
                                    result = saved["error"]
                                else:
                                    lines.append(
                                        f"Saved as '{saved['name']}' — ask me "
                                        f"for it by that name.")
                                    result = "\n".join(lines)
                            else:
                                result = "\n".join(lines) + (
                                    f"\n{_maps.LABELS[provider]}: "
                                    f"{top['links'][provider]}")
                elif action == "directions":
                    dest = str(args.get("to") or "").strip()
                    if not query or not dest:
                        result = "Tell me both ends: from where, and to where."
                    else:
                        a = await asyncio.to_thread(_maps.search, query, limit=1)
                        b = await asyncio.to_thread(_maps.search, dest, limit=1)
                        if not a or not b:
                            result = "I could not find both ends of that trip."
                        else:
                            km = _maps.haversine_km(a[0]["lat"], a[0]["lon"],
                                                    b[0]["lat"], b[0]["lon"])
                            result = (
                                f"{a[0]['label']} to {b[0]['label']} is about "
                                f"{km:.1f} km — {_maps.travel_hint(km)}.\n"
                                f"{_maps.LABELS[provider]}: "
                                f"{b[0]['links'][provider]}")
                else:
                    result = ("Unknown action — use find, nearby, directions, "
                              "link or pin.")

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif name == "device":
                dash = getattr(self, "_dashboard", None)
                if dash is None:
                    result = "Device control needs the dashboard server running."
                else:
                    act = (args.get("action") or "list").lower().strip()
                    dev = (args.get("device") or "").strip()
                    if act == "list":
                        devs = dash.devices_public()
                        if not devs:
                            result = ("No devices paired yet. On the device run: "
                                      "python jarvisd.py --server <url> --pair <code> "
                                      "(the pair code is on the dashboard Devices panel).")
                        else:
                            result = "Devices: " + "; ".join(
                                f"{d['name']} ({d['os'].split(';')[0][:30]}) — "
                                f"{'online' if d['online'] else 'offline'}"
                                for d in devs)
                    elif act in ("status", "ping"):
                        r = await dash.agent_request(dev, "status", {}, timeout=8)
                        if r.get("ok") and r.get("status"):
                            st = r["status"]
                            result = (f"{r.get('device')}: online — host {st.get('host')}, "
                                      f"cpu {st.get('cpu')}%, ram {st.get('ram')}%, "
                                      f"up {st.get('uptime_s')}s, caps {st.get('caps')}")
                        else:
                            result = (f"Device {r.get('device') or dev or '?'}: "
                                      f"{r.get('error', 'unreachable')}"
                                      + (f" ({r.get('hint')})" if r.get("hint") else ""))
                    elif act == "exec":
                        cmd = (args.get("cmd") or "").strip()
                        if not cmd:
                            result = "exec needs a cmd."
                        else:
                            r = await dash.agent_request(dev, "exec", {"cmd": cmd}, timeout=45)
                            if r.get("needs_approval"):
                                # Off the auto-run allowlist → park it behind
                                # the real dashboard confirmation card
                                # (core/confirm.py). request() returns at once
                                # with the sentence the model should SAY; the
                                # task runs only if a human presses CONFIRM.
                                from core import confirm as confirm_gate
                                if confirm_gate.pending_title():
                                    result = (
                                        "A confirmation is already waiting on "
                                        "the dashboard. Ask the user to press "
                                        "CONFIRM or CANCEL there first.")
                                else:
                                    dev_name = r.get("device") or dev or "device"
                                    reason = r.get(
                                        "reason", "not on the auto-run list")
                                    loop = asyncio.get_running_loop()

                                    async def _approved_exec(d=dev_name, c=cmd):
                                        rr = await dash.agent_request(
                                            d, "exec",
                                            {"cmd": c, "approved": True},
                                            timeout=60)
                                        if rr.get("ok"):
                                            out = (rr.get("out") or "").strip()
                                            return (f"ran OK on {d}:\n"
                                                    f"{out[:1500] or '(no output)'}")
                                        return (
                                            f"failed on {d}: "
                                            f"{rr.get('error') or rr.get('reason') or 'unknown error'}"
                                            + (f"\n{rr.get('out', '')[:400]}"
                                               if rr.get("out") else "")
                                            + ("\n(If this device still asks "
                                               "for approval, update jarvisd.py "
                                               "on it and restart the daemon.)"
                                               if rr.get("needs_approval") else ""))

                                    def _worker():
                                        # confirm.resolve() runs on a worker
                                        # thread — hop back onto the live loop;
                                        # agent_request's futures belong to it.
                                        return asyncio.run_coroutine_threadsafe(
                                            _approved_exec(), loop
                                        ).result(timeout=120)

                                    result = confirm_gate.request(
                                        key=f"device-exec-{dev_name}",
                                        title=f"Run on {dev_name}?",
                                        detail=f"$ {cmd[:180]}\n{reason}"[:290],
                                        run=_worker,
                                    )
                            elif r.get("ok"):
                                out = (r.get("out") or "").strip()
                                result = (f"{r.get('device')} ran '{cmd}':\n"
                                          f"{out[:1500] or '(no output)'}")
                            else:
                                result = (f"{r.get('device')} failed: "
                                          f"{r.get('error') or r.get('reason') or 'unknown error'}"
                                          + (f"\n{r.get('out', '')[:500]}" if r.get("out") else ""))
                    else:
                        result = f"Unknown device action: {act} (use list/status/ping/exec)."

            elif name in ("code_task", "task_status", "cancel_task",
                          "push_task", "delegate"):
                dash = getattr(self, "_dashboard", None)
                if dash is None:
                    result = "Coding tasks need the dashboard server running."
                else:
                    tid = (args.get("task_id") or "").strip()
                    if name == "code_task":
                        r = await dash.start_task(
                            prompt=str(args.get("prompt") or ""),
                            where=str(args.get("where") or "auto"),
                            repo=str(args.get("repo") or ""),
                            device=str(args.get("device") or ""),
                            model=str(args.get("model") or ""),
                            source="voice")
                        if "error" in r:
                            result = r["error"]
                        else:
                            on = (f"your PC ({r.get('device')})"
                                  if r.get("where") == "device"
                                  else "the Space")
                            result = (
                                f"Task {r['id']} started — running on {on}. "
                                "Its log streams live in the dashboard Tasks "
                                "panel and I'll announce it when it finishes. "
                                "After it's done say 'push it' — that opens a "
                                "confirm card before anything leaves the "
                                "machine.")
                    elif name == "task_status":
                        store = dash._tasks_store
                        if tid:
                            rec = store.get(tid)
                        else:
                            recs = store.list(limit=5)
                            rec = recs[0] if recs else None
                        if not rec:
                            result = ("No such task." if tid
                                      else "No coding tasks yet.")
                        else:
                            st = rec.get("status")
                            where = rec.get("where", "?")
                            line = (f"Task {rec['id']} — {st} · {where}"
                                    + (f" · {rec.get('device')}"
                                       if rec.get("device") else "")
                                    + f" · \"{rec.get('prompt', '')[:80]}\"")
                            changed = rec.get("changed") or []
                            if st == "done":
                                if rec.get("pushed"):
                                    line += " — pushed to origin ✓"
                                elif isinstance(changed, list) and changed:
                                    line += (f" — {len(changed)} file(s) "
                                             "changed; say 'push it' to "
                                             "publish after review.")
                                else:
                                    line += " — no changes made."
                            if st in ("failed", "cancelled"):
                                tail = store.read_log(rec["id"], 6).strip()
                                if tail:
                                    line += "\nLast log:\n" + tail
                            result = line
                    elif name == "cancel_task":
                        if not tid:
                            result = "cancel_task needs a task_id."
                        else:
                            r = await dash.cancel_task(tid)
                            result = (r.get("error")
                                      or f"Task {tid} cancelled.")
                    elif name == "push_task":
                        if not tid:
                            result = "push_task needs a task_id."
                        else:
                            r = await dash.push_task(tid)
                            result = (r.get("error") or r.get("pending")
                                      or "Push request sent.")
                    elif name == "delegate":
                        subs = args.get("subtasks")
                        if not isinstance(subs, list) or not subs:
                            result = ("delegate needs the subtasks list — "
                                      "draft the plan first, then call once.")
                        else:
                            ids: list[str] = []
                            err = ""
                            for s in subs[:8]:
                                if not isinstance(s, dict):
                                    continue
                                prompt = str(s.get("prompt") or "")
                                who = str(s.get("agent") or "").strip()
                                if who:
                                    # org-aware: the agent's own persona
                                    # leads the prompt, and the run is
                                    # logged against them
                                    try:
                                        from core import agents as _ag
                                        if _ag.find(who) is None:
                                            raise ValueError(
                                                f"no agent called '{who}' "
                                                f"— hire them first")
                                        prompt = _ag.brief(who, prompt)["text"]
                                        _ag.set_busy(who, True)
                                    except ValueError as e:
                                        err = str(e)
                                        break
                                rr = await dash.start_task(
                                    prompt=prompt,
                                    where=str(s.get("where") or "auto"),
                                    repo=str(s.get("repo") or ""),
                                    source="agent" if who else "voice")
                                if "error" in rr:
                                    if who:
                                        _ag.set_busy(who, False)
                                    err = rr["error"]
                                    break
                                ids.append(rr["id"])
                                if who:
                                    _ag.log_run(
                                        who,
                                        job=str(s.get("prompt") or "")[:120],
                                        ok=True, deliverable_id=rr["id"])
                            if not ids:
                                result = f"Delegation failed: {err}"
                            elif err:
                                result = (f"Started {len(ids)} tasks "
                                          f"({', '.join(ids)}) then stopped: "
                                          f"{err}")
                            else:
                                result = (
                                    f"Delegated {len(ids)} parallel tasks: "
                                    f"{', '.join(ids)}. All streaming in the "
                                    "Tasks panel; I'll report as they land.")

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")

        # A tool that declared itself NON_BLOCKING also says when its answer may
        # re-enter the conversation. Without this the model finishes whatever it
        # was saying and then reads the result out on top of it — which, for
        # something like a phone call already ringing, is exactly the noise the
        # non-blocking call was meant to avoid. Tools that declared nothing get
        # the API default and behave as they always have.
        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        result = _empty_result_note(name, result)
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )

    async def _send_realtime(self):
        # Frames held while a tool_call is pending — Live 1008s if we stream
        # input in that window, but dropping the user's words made mic input
        # feel dead for the whole tool. Replay them after the tool response.
        # goAway (_live_input_blocked) still drops: the socket is going away.
        _tool_hold: list[dict] = []
        while True:
            msg = await self.out_queue.get()
            if self._live_input_blocked:
                _tool_hold.clear()
                continue
            # Post-connect settle: Live 1008s if phone/mic PCM races the first
            # frames of a resumed session (resume handle still being applied).
            if time.monotonic() < self._uplink_hold_until:
                continue
            # Half-duplex uplink: Live 1008s when mic PCM keeps arriving
            # while our TTS is still streaming out. The browser already
            # gates; this catches frames already sitting in out_queue
            # (enqueued just before speaking started, or a speaking-flag race).
            # Also hold while audio_in_queue has data — covers the gap
            # between first inbound PCM and set_speaking(True).
            with self._speaking_lock:
                _speaking = self._is_speaking
            _audio_out_busy = (
                _speaking
                or (
                    self.audio_in_queue is not None
                    and not self.audio_in_queue.empty()
                )
            )
            if _audio_out_busy:
                _tool_hold.clear()
                continue
            if self._tool_call_pending:
                _tool_hold.append(msg)
                if len(_tool_hold) > 120:   # ~5–8 s — keep the tail, drop oldest
                    _tool_hold.pop(0)
                continue
            if _tool_hold:
                # Tool just finished: send held frames first so the utterance
                # that started during the tool is not truncated from the front.
                held, _tool_hold = _tool_hold, []
                for h in held:
                    await self.session.send_realtime_input(
                        audio=types.Blob(
                            data=h["data"],
                            mime_type=h.get("mime_type", "audio/pcm"),
                        )
                    )
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so JARVIS can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Barge-in ─────────────────────────────────────────────────────
            # While JARVIS talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears JARVIS.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if jarvis_speaking:
                # Nothing is streamed while JARVIS talks.
                #
                # Interrupting by voice used to live here: `EchoGuard` can pick a
                # user out from under our own echo, and `core/echo.py` still does
                # that for the tail below. Re-enabling is small — classify each
                # block here and call interrupt() after `required_blocks` of
                # agreement — but it depends on the listener's room, so it stays
                # out until it can be tried on real hardware.
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0      # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk ─────────────────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it, which is the whole point: nothing leaves the machine
            # unless you are holding the key.
            if self._ptt_enabled and not self._ptt_held:
                return

            if not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _flush_pending_vision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64
        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        # Label the source. Without it the image arrives carrying nothing but
        # the user's own sentence, and a screenshot of this app — which has a
        # face in the middle of it — got read as a photo of the user. What the
        # label *means* is explained once, in the generated [SELF] block.
        src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
               else "[IMAGE SOURCE: SCREEN CAPTURE]")
        await self.session.send_client_content(
            turns={"role": "user", "parts": [
                {"inline_data": {"mime_type": mime_t, "data": b64}},
                {"text": f"{src}\n\n{question}"},
            ]},
            turn_complete=True,
        )

        if self._vision_cam_active:
            # Camera: stay busy until JARVIS has finished speaking the answer,
            # then close the preview.
            self._vision_cam_active    = False
            self._vision_close_pending = True
        else:
            self._vision_busy = False
        return True

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    # Server is about to tear the socket down. Keep sending
                    # and Live aborts with 1008; stop uplink now and roll the
                    # session ourselves with the resumption handle.
                    _ga = getattr(response, "go_away", None)
                    if _ga is not None and not self._live_input_blocked:
                        _tl = getattr(_ga, "time_left", None) or "?"
                        print(f"[JARVIS] ⏳ goAway time_left={_tl} — rolling session")
                        self._live_input_blocked = True
                        self.ui.write_log("SYS: Live session rolling over — reconnecting.")
                        asyncio.create_task(
                            self._roll_after_audio(self._session_gen)
                        )
                        # Keep receiving until the roll unwinds the TaskGroup.

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice AND made the avatar mouth it twice.
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._last_out_logged = ""   # new exchange
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                self._remember("user", full_in)
                                # Phase 6: persist the turn. Cheap, and it is
                                # what makes "what did I say yesterday" a
                                # query instead of a scroll-back. Never allowed
                                # to break the turn it is recording.
                                try:
                                    from core import knowledge as _kn
                                    _kn.record_turn("user", full_in)
                                    _kn.note_query(full_in)
                                except Exception:
                                    pass
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = _polish_reply(" ".join(out_buf).strip())
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                self._remember("assistant", full_out)
                                try:
                                    from core import knowledge as _kn
                                    _kn.record_turn("jarvis", full_out)
                                except Exception:
                                    pass
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            if self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        # Block mic/phone realtime frames until the tool
                        # response is sent — Live 1008s on input in this window.
                        self._tool_call_pending = True
                        try:
                            fn_responses = []
                            for fc in response.tool_call.function_calls:
                                print(f"[JARVIS] 📞 {fc.name}")
                                fr = await self._execute_tool(fc)
                                fn_responses.append(fr)
                            await self.session.send_tool_response(
                                function_responses=fn_responses
                            )
                            await self._flush_pending_vision()
                        finally:
                            self._tool_call_pending = False

                    if getattr(response, "tool_call_cancellation", None):
                        # Server gave up on the tool — clear the gate so
                        # _utter / _send_realtime stop waiting forever.
                        self._tool_call_pending = False
                        print("[JARVIS] 📞 tool call cancelled")
        except _ReconnectSignal:
            raise
        except Exception as e:
            if "1008" in _exc_text(e) and "abort" in _exc_text(e).lower():
                print("[JARVIS] ❌ Recv: 1008 aborted")
            else:
                print(f"[JARVIS] ❌ Recv: {e}")
                traceback.print_exc()
            raise

    async def _roll_after_audio(self, gen: int) -> None:
        """After goAway: drain playback, then voluntary reconnect (keeps context).

        `gen` is the session generation at goAway time — if a 1008 already
        rebuilt the session while we waited, do not bounce the new one."""
        try:
            await self._wait_audio_idle(timeout=20.0, quiet=0.3)
        except Exception:
            pass
        if gen != self._session_gen:
            return
        if self._live_input_blocked:
            self.request_reconnect(keep_context=True, reason="session rollover")

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # Ask the device how far behind the speakers actually are, rather than
        # assuming. This is what the echo tail is sized from, so a machine with a
        # large audio buffer gets a correspondingly longer guard — and one with a
        # tiny buffer is not penalised with a delay it does not need.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[JARVIS] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD waveform and the avatar's mouth from JARVIS's
                # own voice. The batch is up to 200 ms long, so we hand over a
                # *schedule* of 20 ms viseme frames instead of a single averaged
                # level and let the HUD play it out in step with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The stream was
                    # started at launch and its callback has been pulling
                    # silence ever since, so the first bytes of a reply reach
                    # the speaker about one callback period later — NOT one
                    # buffer later. `stream.latency` reports the buffer's
                    # capacity, which is how much can be queued ahead, and on
                    # Windows that is commonly 300-500 ms. Anchoring on it put
                    # the entire schedule a buffer late; that is the half second
                    # of lag, and it grew with whatever the device reported.
                    #
                    # After the anchor nothing needs measuring: the device
                    # consumes at exactly realtime, so each batch sounds one
                    # batch-duration after the one before it. The cursor is
                    # re-anchored only when it leaves the range physically
                    # possible — behind `now` means the device drained and this
                    # batch starts a fresh stretch of speech, while further
                    # ahead than the buffer can hold means it has drifted.
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        self.ui.push_visemes(frames, hop, at)
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the microphone.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    async def _play_audio_browser(self) -> None:
        """Server mode: stream Gemini's PCM out to the dashboard instead of a speaker.

        Same batching / turn-completion contract as _play_audio, minus the
        sounddevice stream and the avatar visemes. The browser schedules the
        buffers itself and gates its own mic uplink while playback is active
        (half-duplex), so no local echo path exists to guard.
        """
        print("[JARVIS] 🌐 Browser audio out started")
        _BATCH = 9600   # ~200 ms at 24 kHz / 16-bit mono — fewer, fuller WS
                        # frames absorb proxy jitter instead of surfacing as chop
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                batch = bytearray(chunk)
                while len(batch) < _BATCH:
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        # Under-filled: wait briefly for the next slice so we
                        # ship ~100–200 ms frames instead of 50 ms crumbs.
                        if len(batch) >= 4800:
                            break
                        try:
                            more = await asyncio.wait_for(
                                self.audio_in_queue.get(), timeout=0.03
                            )
                            batch.extend(more)
                        except asyncio.TimeoutError:
                            break

                data = bytes(batch)
                # Keep the echo guard's model of our own output current — the
                # phone mic path still consults the speaking flag / tail.
                try:
                    pcm = np.frombuffer(data, dtype=np.int16)
                    lvl = _pcm_level(pcm)
                    self._out_level = lvl
                    self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                    self.ui.set_audio_level(lvl)
                except Exception:
                    pass

                if self._dashboard:
                    await self._dashboard.send_audio(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[JARVIS] ❌ Browser play: {e}")
            raise
        finally:
            self.set_speaking(False)

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Phase 2 waits for Phase 1
                    audio to fully drain so the two never overlap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "top world news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        self._briefing_active = True
        try:
            # ── Phase 1: instant greeting ─────────────────────────────────────────
            # The briefing fires before the user has said anything, so the
            # remembered language is the only signal there is. It is a starting
            # point, not a setting: the moment they reply, their language wins.
            lang_clause = (f" Speak this greeting in {lang}, then follow the "
                           f"user's own language from their first reply onward."
                           if lang else "")
            name_clause = f" Address the user as {name}." if name else ""

            # Inject last session context if available — pop removes it so it's never repeated
            last = await asyncio.to_thread(pop_last_session)
            session_clause = ""
            if last:
                try:
                    _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                    _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
                except Exception:
                    _when = "last time"
                session_clause = (
                    f" Also briefly and naturally mention that {_when}: {last['summary']}"
                )

            p1 = (
                f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's news now.{session_clause} "
                f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
            )

            # Hold the speak lock for Phase 1 and wait until its audio fully
            # drains — Phase 2 must never start while Phase 1 is still playing.
            async with self._speak_lock:
                if self._turn_done_event:
                    self._turn_done_event.clear()
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": p1}]},
                    turn_complete=True,
                )
                print("[JARVIS] Briefing phase 1 (greeting) sent.")
                await self._wait_speaking_started(timeout=8.0)
                await self._wait_audio_idle(timeout=30.0)
                await self._settle_after_turn()

            # ── Phase 2: news (fires only after Phase 1 audio is silent) ──────
            lang_str = (f" Speak in {lang} unless the user has since "
                        f"spoken another language, in which case use theirs."
                        if lang else "")

            try:
                news_text = await asyncio.wait_for(asyncio.wrap_future(news_future), timeout=8.0)
            except Exception as e:
                self.ui.write_log(f"SYS: News fetch timed out/failed: {e!r}")
                news_text = ""

            if not self.session:
                return

            failed = (not news_text) or news_text.startswith(
                ("No news found", "Search failed", "Please provide")
            )
            if not failed:
                # Show on UI content panel immediately
                self.ui.show_content("NEWS — top world news today", news_text)

                p2 = (
                    f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                    "Pick ONE headline, summarise it in one sentence, then say the full list "
                    f"is displayed on screen. Do not call any tools.{lang_str}"
                )
            else:
                self.ui.write_log(
                    f"SYS: News unavailable — backend returned: {news_text[:120]!r}"
                )
                p2 = (
                    "News headlines could not be fetched right now. "
                    f"Let the user know briefly.{lang_str}"
                )

            await self._utter(p2, wait_prev=True, drain=True)
            print("[JARVIS] Briefing phase 2 (news) sent.")
        except Exception as e:
            print(f"[Briefing] error: {e}")
            self.ui.write_log("SYS: Could not fetch the news for the briefing.")
        finally:
            self._briefing_active = False

    # ── Session memory ──────────────────────────────────────────────────────────

    def _remember(self, role: str, text: str) -> None:
        """Write a real turn into the transcript store.

        This did not exist, and that is why memory looked broken.

        The live conversation lived in `self._session_log`, in memory, only long
        enough to be flattened into a one-or-two sentence summary at the end of
        a session. So `recall` — which searches data/bot_chats/*.json — found
        nothing, because the only thing that ever wrote to bot_chats was
        crew.say(), meaning messages addressed to SUB-bots. Your own
        conversation with JARVIS was never stored anywhere at all.

        That is the whole of "it does not remember anything and does not recall
        anything": the turns were never written, so there was nothing to read
        back. Same schema, same file, same directory recall already reads.

        Called from both the voice path and the typed path, because "I said it
        out loud" and "I typed it" are the same memory.

        Best-effort by design: failing to write a memory must never interrupt a
        reply, so every error here is swallowed.
        """
        t = _clean_transcript(str(text or "")).strip()
        if not t:
            return
        try:
            from core import crew as _cr
            _cr._append("jarvis", {"role": role, "text": t[:8000]})
        except Exception:
            pass

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core import gemini
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            if summary:
                save_session_summary(summary, lang)
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Server (HF free tier) idles near 90% RAM by design — speaking
            # that every cooldown is noise. HUD metrics are unaffected.
            if SERVER_MODE:
                print(f"[Monitor] {alert[:120]}")
                continue
            # Don't interrupt an active conversation or the startup briefing
            if self._briefing_active:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            q_busy = self.audio_in_queue is not None and not self.audio_in_queue.empty()
            if speaking or q_busy or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self._utter(alert)
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────
    #
    # The old `while True: sleep(1800)` loop that called monitor_check_all is
    # gone. The work now lives in the persistent scheduler as the builtin job
    # "topic monitor check" (handler: monitor_check) — same cadence, same
    # guards, but it survives restarts, can be paused from the panel, and is
    # model-callable via manage_schedule.


    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session or not self._awake:
                continue

            if self._briefing_active:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            q_busy = self.audio_in_queue is not None and not self.audio_in_queue.empty()
            if q_busy:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self._utter(prompt)
                print("[JARVIS] Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Persistent scheduler ─────────────────────────────────────────────────────
    #
    # Started once per process, deliberately OUTSIDE the session task group:
    # a dropped Live session must not silently drop the user's schedule, and a
    # persisted job has to survive the reconnect. It replaces the old
    # `while True: sleep(30m)` background-monitor loop — that loop is gone,
    # its work now runs as a job (see core/scheduler.py ensure_defaults).

    def _start_scheduler(self) -> None:
        from core.scheduler import get_scheduler
        self._sched = get_scheduler()
        self._sched.bind(on_fire=self._on_sched_fire,
                         broadcast=self._sched_bcast)
        # Load + register the migrated jobs synchronously so the boot log
        # reports the truth; the loop repeats both (idempotent) when it starts.
        self._sched.load()
        self._sched.ensure_defaults()
        self._sched.start()
        print(f"[Scheduler] {len(self._sched.list())} job(s) ready "
              f"from the core/scheduler.py store")

    def _sched_bcast(self, msg: dict) -> None:
        """Forward a scheduler event to the dashboard (thread-safe)."""
        dash, loop = self._dashboard, self._loop
        if not dash or not loop or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(dash.broadcast(msg), loop)
        except Exception:
            pass

    def _on_sched_fire(self, job: dict, text: str) -> None:
        """A job produced something. Decide how it reaches the user."""
        if not text:
            return
        name = job.get("name", "job")
        if job.get("notify"):
            try:
                from core import push as _push
                _push.notify(f"Job ran — {name}"[:120], text[:160],
                             data={"url": "/?panel=schedule"},
                             tag=f"jarvis-job-{job.get('id')}")
            except Exception:
                pass
        # Handler jobs that opt in (the old monitor loop) must never wake a
        # sleeping assistant or cut into a sentence — same guards the loop had.
        if job.get("requires_awake"):
            if not self.session or not self._awake or self._briefing_active:
                return
            with self._speaking_lock:
                if self._is_speaking:
                    return
        self.ui.write_log(f"SYS: Scheduled job '{name}' fired.")
        if self._dashboard is not None:
            # Server mode: the exact path /api/command uses — waits for a
            # session, wakes if asleep, speaks the result.
            self._dashboard._command_queue.put_nowait(text)
            return
        try:
            self._on_text_command(text)
        except Exception as e:
            print(f"[Scheduler] Could not run '{name}': {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            # ── Wake-word gate (mirrors the PC mic path) ────────────────────
            # While asleep the browser mic never reaches Gemini — frames go to
            # the local detector instead, so nothing leaves the machine until
            # "Hey Jarvis" is heard. The detector copies each frame internally.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    try:
                        det.feed(np.frombuffer(chunk.get("data", b""),
                                               dtype=np.int16))
                    except Exception:
                        pass
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session or self._voice_backend == "gateway":
                        break
                    await asyncio.sleep(0.1)
                # A remote command is deliberate control and the phone user
                # has no desktop WAKE button — so it wakes JARVIS if asleep.
                if self._wake_enabled and not self._awake and (
                        self.session or self._voice_backend == "gateway"):
                    self.wake(reason="remote command")
                if self.session:
                    await self._utter(text, wait_prev=True, drain=False)
                    self.ui.write_log(f"[Web]: {text}")
                elif self._voice_backend == "gateway":
                    self.ui.write_log(f"[Web]: {text}")
                    if self._dashboard:
                        await self._dashboard.broadcast({
                            "type": "log", "speaker": "user",
                            "text": text,
                            "ts": datetime.now().isoformat(),
                        })
                    if self._gw_turn_q:
                        await self._gw_turn_q.put({"text": text, "logged": True})
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── Gateway voice failover ────────────────────────────────────────────────
    # Live is out of quota. The mic keeps flowing (the wake/mute/PTT/echo
    # gates all live upstream in the producer callbacks), but instead of
    # send_realtime_input → receive, a local VAD segments utterances and each
    # one runs STT → chat (streamed, tools via _execute_tool, rolling ~10-turn
    # history) → TTS straight into audio_in_queue — so the existing play loops,
    # interrupt(), and the dash all work unchanged. A probe task tries Live
    # every 2 minutes; the first success tears this down and the connect loop
    # takes over again with zero backoff.

    class _GwRecovered(Exception):
        """Raised by the probe when Live answers again — unwrapped below."""

    def _gw_fallback_allowed(self) -> bool:
        """True when voice_fallback is on AND a gateway chat model is usable."""
        try:
            from memory.config_manager import get_openai_settings
            from core import gateway as _gw
            v = get_openai_settings()["values"]
            if not v.get("voice_fallback", True):
                return False
            return bool(_gw.enabled() and _gw.voice_model())
        except Exception:
            return False

    def _gw_seed_history(self) -> list[dict]:
        """Rolling chat history seeded from the session log so the fallback
        remembers what was said on Live before quota ran out. Last 10 turns."""
        hist: list[dict] = []
        for line in self._session_log[-20:]:
            if line.startswith("User: "):
                hist.append({"role": "user", "content": line[6:]})
            elif line.startswith("[Web]: "):
                hist.append({"role": "user", "content": line[7:]})
            elif line.startswith(f"{self._asst_name}: "):
                hist.append({"role": "assistant",
                             "content": line[len(self._asst_name) + 2:]})
        return hist[-10:]

    async def _gw_enqueue_text(self, text: str) -> None:
        """Typed/desktop command → fallback turn queue (skip STT)."""
        text = (text or "").strip()
        if not text or not self._gw_turn_q:
            return
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        self.ui.write_log(f"You: {text}")
        if self._dashboard:
            await self._dashboard.broadcast({
                "type": "log", "speaker": "user", "text": text,
                "ts": datetime.now().isoformat(),
            })
        await self._gw_turn_q.put({"text": text, "logged": True})

    async def _gw_iter(self, sync_gen):
        """Adapt a blocking sync generator (core/gateway streams) to async by
        pumping it on an executor thread into a queue."""
        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def pump():
            try:
                for ev in sync_gen:
                    loop.call_soon_threadsafe(q.put_nowait, ("ev", ev))
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, ("err", e))
            finally:
                loop.call_soon_threadsafe(q.put_nowait, ("end", None))

        asyncio.get_running_loop().run_in_executor(None, pump)
        while True:
            kind, *rest = await q.get()
            if kind == "end":
                break
            if kind == "err":
                raise rest[0]
            yield rest[0]

    def _gw_whisper_transcribe(self, pcm: bytes) -> str:
        """Local faster-whisper STT (runs on a thread). Cached model."""
        if self._gw_whisper is None:
            from core.stt import WhisperSTT
            self._gw_whisper = WhisperSTT()   # "base" — lazy model download
        audio = (np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0)
        return self._gw_whisper.transcribe(audio)

    async def _gw_stt(self, pcm: bytes) -> str:
        """Utterance → text. Mode: auto (gateway→local) | gateway | local.
        In gateway/auto, streams partials to the dash as `partial_user`."""
        from memory.config_manager import get_openai_settings
        from core import gateway as gw

        mode = get_openai_settings()["values"].get("stt_mode", "auto")
        errs: list[str] = []

        if mode in ("auto", "gateway") and gw.enabled():
            try:
                final = ""
                gen = gw.transcribe_stream(pcm, rate=SEND_SAMPLE_RATE)
                async for partial in self._gw_iter(gen):
                    final = partial or final
                    if final and self._dashboard:
                        await self._dashboard.broadcast(
                            {"type": "partial_user", "text": final})
                final = (final or "").strip()
                if final:
                    return final
                raise RuntimeError("empty transcript")
            except Exception as e:
                if mode == "gateway":
                    raise
                errs.append(f"gateway: {e}")

        if mode in ("auto", "local"):
            try:
                return await asyncio.to_thread(self._gw_whisper_transcribe, pcm)
            except Exception as e:
                errs.append(f"local: {e}")

        raise RuntimeError("; ".join(errs) or "no STT engine")

    async def _gw_tts_chunks(self, text: str):
        """Yield 24 kHz mono s16 PCM chunks for `text`. Gateway pcm stream
        first (auto/gateway), EdgeTTS→miniaudio decode as local fallback."""
        from memory.config_manager import get_openai_settings
        from core import gateway as gw

        mode = get_openai_settings()["values"].get("tts_mode", "auto")
        errs: list[str] = []

        if mode in ("auto", "gateway") and gw.enabled():
            try:
                async for chunk in self._gw_iter(gw.speak_stream(text)):
                    yield chunk
                return
            except Exception as e:
                if mode == "gateway":
                    raise
                errs.append(f"gateway: {e}")

        if mode in ("auto", "local"):
            try:
                import edge_tts

                async def _synth() -> bytes:
                    comm = edge_tts.Communicate(text, "en-US-GuyNeural")
                    buf = bytearray()
                    async for ch in comm.stream():
                        if ch["type"] == "audio":
                            buf.extend(ch["data"])
                    return bytes(buf)

                mp3 = await _synth()
                pcm = await asyncio.to_thread(gw._wav_bytes_to_pcm24k, mp3)
                for i in range(0, len(pcm), 4800):
                    yield pcm[i:i + 4800]
                return
            except Exception as e:
                errs.append(f"local: {e}")

        raise RuntimeError("; ".join(errs) or "no TTS engine")

    async def _gw_speak(self, text: str) -> None:
        """Speak one sentence through the fallback, respecting interrupt.
        Chunks go in as 2400-byte slices — the same unit _receive_audio
        uses, so interrupt()'s drain and the play loops behave identically."""
        text = (text or "").strip()
        if not text or self._interrupted:
            return
        try:
            async for chunk in self._gw_tts_chunks(text):
                if self._interrupted:
                    break
                for i in range(0, len(chunk), 2400):
                    if self._interrupted:
                        break
                    self.audio_in_queue.put_nowait(chunk[i:i + 2400])
        except Exception as e:
            print(f"[GwVoice] TTS error: {e}")
            self.ui.write_log(f"ERR: TTS — {str(e)[:100]}")

    def _gw_push_history(self, role: str, content) -> None:
        self._gw_history.append({"role": role, "content": content})
        if len(self._gw_history) > 10:
            self._gw_history = self._gw_history[-10:]

    async def _gw_turn(self, item: dict) -> None:
        """One full fallback turn: (STT) → chat_stream + tools → TTS.
        Runs on the single turn worker, so TTS sentences stay ordered."""
        from core import gateway as gw

        # ── user text ────────────────────────────────────────────────────
        if "text" in item:
            user_text = (item["text"] or "").strip()
            if not user_text:
                return
            if not item.get("logged"):
                self.ui.write_log(f"You: {user_text}")
                if self._dashboard:
                    await self._dashboard.broadcast({
                        "type": "log", "speaker": "user", "text": user_text,
                        "ts": datetime.now().isoformat(),
                    })
        else:
            try:
                user_text = await self._gw_stt(item["pcm"])
            except Exception as e:
                print(f"[GwVoice] STT failed: {e}")
                self.ui.write_log(f"ERR: STT — {str(e)[:100]}")
                return
            if not user_text:
                return
            self.ui.write_log(f"You: {user_text}")
            if self._dashboard:
                # final line replaces the partial
                await self._dashboard.broadcast({
                    "type": "log", "speaker": "user", "text": user_text,
                    "ts": datetime.now().isoformat(),
                })
        self._session_log.append(f"User: {user_text}")
        self._remember("user", user_text)
        self._last_user_speech = time.monotonic()
        self._last_out_logged = ""

        # ── pending vision from before this turn (captured but not yet
        # flushed — e.g. set on the Live path just as quota died) ────────
        pending_image = None
        if self._pending_vision:
            import base64 as _b64
            img_b, mime_t, question, angle = self._pending_vision
            self._pending_vision = None
            src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
                   else "[IMAGE SOURCE: SCREEN CAPTURE]")
            b64 = _b64.b64encode(img_b).decode("ascii")
            pending_image = [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime_t};base64,{b64}"}},
                {"type": "text", "text": f"{src}\n\n{question}"},
            ]
            if self._vision_cam_active:
                self._vision_cam_active = False
                self._vision_close_pending = True
            else:
                self._vision_busy = False

        # One user turn: plain text normally; multimodal when an image is
        # still waiting to be shown to the model.
        if pending_image:
            self._gw_history.append({
                "role": "user",
                "content": ([{"type": "text", "text": user_text}]
                            if user_text else []) + pending_image,
            })
        else:
            self._gw_push_history("user", user_text)

        # ── system prompt + tools ────────────────────────────────────────
        system = self._system_prompt_text()
        tools = gw.to_openai_tools(self._tool_declarations())
        model = gw.voice_model()

        # Ordered TTS: sentences from the stream land on this queue while the
        # stream keeps being read (tool deltas must not wait on TTS HTTP).
        tts_q: asyncio.Queue = asyncio.Queue()

        async def _tts_worker():
            while True:
                s = await tts_q.get()
                if s is None:
                    return
                await self._gw_speak(s)

        tts_task = asyncio.create_task(_tts_worker())
        spoken_parts: list[str] = []

        try:
            # ── chat + tool loop (≤8 rounds) ─────────────────────────────
            round_msgs: list[dict] = []   # extra msgs appended for next round
            for _round in range(8):
                messages = ([{"role": "system", "content": system}]
                            + list(self._gw_history) + round_msgs)
                round_msgs = []
                full_text = ""
                tool_calls = None

                gen = gw.chat_stream(messages, tools=tools or None, model=model)
                async for ev in self._gw_iter(gen):
                    if self._interrupted:
                        break
                    kind = ev[0]
                    if kind == "sentence":
                        await tts_q.put(ev[1])
                        spoken_parts.append(ev[1])
                    elif kind == "tool_calls":
                        tool_calls = ev[1]
                    elif kind == "done":
                        full_text = ev[1]

                if self._interrupted:
                    self._interrupted = False
                    full_text = ""
                    tool_calls = None

                if not tool_calls:
                    if full_text:
                        # the reply is complete here, so this is the one safe
                        # place to run the character pass over it
                        full_text = _polish_reply(full_text)
                        if full_text:
                            self._gw_push_history("assistant", full_text)
                    break   # plain answer — turn over

                # One assistant message carrying BOTH any preamble text and
                # all tool_calls, then one `tool` message per call — the
                # exact shape chat.completions expects (two assistant
                # messages, or a per-call assistant msg, confuse strict
                # gateways).
                tc_shim = []
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    tc_shim.append({
                        "id": tc.get("id") or f"gw_{fn.get('name') or 'call'}",
                        "type": "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "{}",
                        },
                    })
                self._gw_history.append({
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": tc_shim,
                })

                # Execute tools via the same path Live uses.
                for tc, shim in zip(tool_calls, tc_shim):
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = (json.loads(raw_args)
                                if isinstance(raw_args, str) else dict(raw_args))
                    except Exception:
                        args = {}

                    class _Fc:   # shim — _execute_tool reads .name/.args/.id
                        pass
                    fc = _Fc()
                    fc.name = name
                    fc.args = args
                    fc.id = shim["id"]

                    print(f"[JARVIS] 📞 {name}")
                    try:
                        fr = await self._execute_tool(fc)
                        resp = getattr(fr, "response", None) or {"result": "ok"}
                    except Exception as e:
                        resp = {"result": f"error: {e}"}

                    self._gw_history.append({
                        "role": "tool", "tool_call_id": fc.id,
                        "content": json.dumps(resp, default=str),
                    })

                    # Vision captured inside the tool → attach as the next
                    # user turn (image_url part), exactly like Live's flush.
                    if self._pending_vision and name == "screen_process":
                        import base64 as _b64
                        img_b, mime_t, question, angle = self._pending_vision
                        self._pending_vision = None
                        src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
                               else "[IMAGE SOURCE: SCREEN CAPTURE]")
                        b64 = _b64.b64encode(img_b).decode("ascii")
                        round_msgs.append({
                            "role": "user",
                            "content": [
                                {"type": "image_url",
                                 "image_url": {"url":
                                               f"data:{mime_t};base64,{b64}"}},
                                {"type": "text",
                                 "text": f"{src}\n\n{question}"},
                            ],
                        })
                        if self._vision_cam_active:
                            self._vision_cam_active = False
                            self._vision_close_pending = True
                        else:
                            self._vision_busy = False

            # Trim AFTER the tool loop so a pair is never cut in half mid-
            # flight. If the window still starts on an orphan tool response,
            # drop it (its assistant/tool_calls parent scrolled out).
            if len(self._gw_history) > 10:
                self._gw_history = self._gw_history[-10:]
                while (self._gw_history
                       and self._gw_history[0].get("role") == "tool"):
                    self._gw_history.pop(0)
        finally:
            await tts_q.put(None)
            try:
                await tts_task
            except Exception:
                pass

        # ── log the assistant reply once, like turn_complete does ─────────
        full_out = "".join(spoken_parts).strip() if not spoken_parts else \
            " ".join(p.strip() for p in spoken_parts if p.strip())
        if full_out:
            self._last_out_logged = full_out
            self.ui.write_log(f"{self._asst_name}: {full_out}")
            self._session_log.append(f"{self._asst_name}: {full_out}")
            self._remember("assistant", full_out)
            if self._dashboard:
                await self._dashboard.broadcast({
                    "type": "log", "speaker": "jarvis", "text": full_out,
                    "ts": datetime.now().isoformat(),
                })

        # camera auto-close after the vision answer, mirroring turn_complete
        if self._vision_close_pending:
            self._vision_close_pending = False
            self._vision_busy = False
            await asyncio.sleep(2.0)
            self.ui.stop_camera_stream()

        # let the play loops settle out of SPEAKING
        if self._turn_done_event:
            self._turn_done_event.set()

    async def _gw_collector(self) -> None:
        """out_queue (mic/phone PCM) → energy VAD → utterance queue.
        Wake/mute/PTT/echo gates already ran upstream in the producers."""
        turn = get_turn_tuning()
        silence_end_ms = (turn["silence_ms"] if turn.get("enabled") else 800)
        frame_ms = CHUNK_SIZE / SEND_SAMPLE_RATE * 1000      # ~64 ms
        lead_bytes = int(SEND_SAMPLE_RATE * 0.20) * 2        # 200 ms lead-in
        noise_floor = 300.0

        buf = bytearray()
        in_speech = False
        silence_ms = 0.0
        speech_ms = 0.0

        while True:
            try:
                msg = await asyncio.wait_for(self.out_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            data = msg.get("data") if isinstance(msg, dict) else None
            if not data:
                continue
            samples = np.frombuffer(data, dtype=np.int16)
            if samples.size == 0:
                continue
            rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
            on_threshold = max(400.0, noise_floor * 3.0)

            if not in_speech:
                if rms < on_threshold:
                    noise_floor = noise_floor * 0.95 + rms * 0.05
                if rms >= on_threshold:
                    in_speech = True
                    silence_ms = 0.0
                    speech_ms = 0.0
                    buf = bytearray()
                    # keep a short lead-in of whatever precedes speech
                    buf.extend(data[-lead_bytes:] if len(data) >= lead_bytes else data)
                    speech_ms += frame_ms
                continue

            buf.extend(data)
            if rms >= on_threshold:
                silence_ms = 0.0
                speech_ms += frame_ms
            else:
                silence_ms += frame_ms

            total_ms = speech_ms + silence_ms
            if silence_ms >= silence_end_ms or total_ms > 30_000:
                in_speech = False
                if speech_ms >= 250.0 and self._gw_turn_q is not None:
                    await self._gw_turn_q.put({"pcm": bytes(buf)})
                buf = bytearray()
                silence_ms = speech_ms = 0.0

    async def _gw_turn_worker(self) -> None:
        """Single consumer of the utterance/text queue — one turn at a time,
        so TTS output stays ordered exactly like a Live turn."""
        while True:
            item = await self._gw_turn_q.get()
            try:
                await self._gw_turn(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[GwVoice] turn error: {e}")
                traceback.print_exc()
                self.ui.write_log(f"ERR: turn — {str(e)[:120]}")
                if self._turn_done_event:
                    self._turn_done_event.set()

    async def _gw_recover_probe(self) -> None:
        """Every 2 min, try a short Live connect. Success tears the fallback
        down (via _GwRecovered) and the connect loop resumes immediately.
        An invalid-key error also exits so the main loop can prompt for a
        new key instead of probing forever."""
        while True:
            await asyncio.sleep(120)
            try:
                config = self._build_config()
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version":
                                  "v1alpha" if self._enhanced_live else "v1beta"},
                )
                async with client.aio.live.connect(
                        model=LIVE_MODEL, config=config):
                    pass   # connected — quota is back; tear down fallback
                print("[JARVIS] ✅ Live quota recovered — leaving voice fallback.")
                raise self._GwRecovered()
            except self._GwRecovered:
                raise
            except Exception as e:
                if "API key not valid" in str(e) or "1007" in str(e):
                    print("[JARVIS] ⚠ Key invalid during fallback — retrying Live.")
                    raise self._GwRecovered()
                # still quota'd (or network blip) — probe again later
                continue

    async def _run_gateway_voice_loop(self) -> None:
        """Own the session until Live recovers. Starts the same producers /
        consumers the Live TaskGroup runs — minus send_realtime/_receive —
        so mic, phone, playback, wake word and interrupt all keep working."""
        from core import gateway as gw

        if not self._gw_fallback_allowed():
            return

        self._voice_backend = "gateway"
        self.session = None
        self._interrupted = False
        self._pending_vision = None
        self._vision_cam_active = False
        self._vision_close_pending = False
        self._vision_busy = False
        self._gw_history = self._gw_seed_history()
        self._gw_turn_q = asyncio.Queue()

        # Queues may never have existed (failed on first connect) — create.
        if self.audio_in_queue is None:
            self.audio_in_queue = asyncio.Queue()
        if self.out_queue is None:
            self.out_queue = asyncio.Queue(maxsize=200)
        if self._turn_done_event is None:
            self._turn_done_event = asyncio.Event()
        self._turn_done_event.clear()

        # Namesake refresh so history seeding + logs use the config name.
        try:
            self._system_prompt_text()
        except Exception:
            pass

        if self._wake_enabled:
            self._ensure_wake_detector()
            if self._awake:
                self.ui.set_state("LISTENING")
            else:
                self.ui.set_state("SLEEPING")
        else:
            self._awake = True
            self.ui.set_state("LISTENING")

        self.ui.write_log(
            "SYS: Gemini Live out of quota — voice failover ACTIVE "
            "(gateway STT→chat→TTS). Probing Live every 2 min.")
        if self._dashboard:
            await self._dashboard.broadcast(
                {"type": "status", "state": "fallback"})
            await self._dashboard.broadcast({
                "type": "sys",
                "text": "Voice failover active — Live quota exhausted, "
                        "using gateway. Will reconnect automatically.",
            })
        print(f"[GwVoice] fallback start — model={gw.voice_model()} "
              f"stt={gw.settings().get('stt_mode')} "
              f"tts={gw.settings().get('tts_mode')}")

        recovered = False
        try:
            async with asyncio.TaskGroup() as tg:
                # Producers / consumers (same set the Live block starts)
                if not SERVER_MODE:
                    tg.create_task(self._listen_audio())
                if self._dashboard:
                    tg.create_task(self._relay_phone_audio())
                if SERVER_MODE:
                    tg.create_task(self._play_audio_browser())
                else:
                    tg.create_task(self._play_audio())
                # Fallback-specific
                tg.create_task(self._gw_collector())
                tg.create_task(self._gw_turn_worker())
                tg.create_task(self._gw_recover_probe())
        except self._GwRecovered:
            recovered = True
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            # TaskGroup wraps in an ExceptionGroup — look for _GwRecovered
            # anywhere inside; otherwise log and let the connect loop retry.
            def _has_rec(ex) -> bool:
                if isinstance(ex, self._GwRecovered):
                    return True
                sub = getattr(ex, "exceptions", None)
                return bool(sub) and any(_has_rec(x) for x in sub)

            if _has_rec(e):
                recovered = True
            else:
                print(f"[GwVoice] fallback loop error: {e}")
                traceback.print_exc()

        self._voice_backend = "live"
        self._gw_turn_q = None
        if recovered and self._dashboard:
            await self._dashboard.broadcast({
                "type": "sys", "text": "Gemini Live recovered — resuming."})
        self.ui.write_log("SYS: Voice failover ended — reconnecting to Live.")

    # ── main loop ───────────────────────────────────────────────────────────

    def _start_godseye(self) -> None:
        """Bring up the vendored God's Eye View server (best effort)."""
        try:
            from core import godseye as _gev
            st = _gev.start()
            print(f"[God'sEye] {st['note']} \u2014 {st['actions']} actions, "
                  f"{st['layers']} layers, api_alive={st['api_alive']}")
        except Exception as e:
            print(f"[God'sEye] unavailable: {e}")

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread. Skipped in server mode:
        # there are no local streams, so enumeration would only probe hardware
        # this process will never open.
        if not SERVER_MODE:
            audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            # so the panel can reload the plugin registry in place
            self._dashboard.bind_live(self)
            dash = self._dashboard
            dash.set_connect_callback(self._on_phone_connected)
            dash.set_wake_callback(self._remote_wake)
            dash.set_ptt_callback(self._on_ptt)
            dash.set_interrupt_callback(self.interrupt)
            dash.set_mute_callback(self._toggle_mute)
            dash.set_key_saved_callback(self._on_key_saved)
            dash.set_voice_callback(self._on_remote_voice)
            dash.set_wake_state_provider(self._wake_state)
            if hasattr(self.ui, "bind_dashboard"):
                self.ui.bind_dashboard(dash, self._loop)
            asyncio.create_task(dash.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
            # Phase 4f: the real God's Eye View is a separate Node process on
            # loopback; the dashboard proxies /godseye/* and /api/gev/* to it.
            # A daemon thread, because a globe that failed to start must never
            # be able to take the dashboard down with it.
            if os.environ.get("JARVIS_GODSEYE", "1").lower() not in ("0", "false", "no"):
                threading.Thread(target=self._start_godseye, name="godseye",
                                 daemon=True).start()
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        # One clock for every recurring job — process-scoped on purpose.
        try:
            self._start_scheduler()
        except Exception as e:
            print(f"[Scheduler] Disabled: {e}")

        # Server mode: hold the connect loop until a Gemini key arrives
        # through the dashboard (HeadlessUI._ready flips on /api/save-key).
        if SERVER_MODE and not getattr(self.ui, "ready", True):
            print("[JARVIS] No API key yet — open the dashboard and paste your "
                  "Gemini key to bring the session online.")
            while not getattr(self.ui, "ready", False):
                await asyncio.sleep(0.5)
            print("[JARVIS] API key present — connecting.")

        while True:
            do_failover = False   # set in the except chain on Live quota errors
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries proactive audio; if it gets rejected we fall
                # back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    _old_audio_q          = self.audio_in_queue
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    _had_interrupt = self._interrupted
                    self._interrupted          = False
                    self._tool_call_pending    = False
                    self._live_input_blocked   = False
                    self._session_gen          = getattr(self, "_session_gen", 0) + 1
                    # Let the resumed session settle before any uplink — phone
                    # mic and held frames were the race that 1008'd gen 3–5.
                    # The reconnect after an idle 1008 gets a longer hold: that
                    # abort usually lands while the server is still finishing
                    # the previous turn, and early mic frames restart the loop.
                    _recovered = getattr(self, "_total_idle_1008", 0) > 0
                    self._uplink_hold_until    = time.monotonic() + (2.5 if _recovered else 1.5)
                    # NOTE: do NOT reset the 1008 streak here from connection
                    # age. A session that talks for 2 minutes and then aborts
                    # still means the preview knobs are unsafe — wiping the
                    # streak on every such reconnect is what kept escalation
                    # (compression / resumption / proactive) from ever firing.
                    self._stable_since = time.monotonic()

                    print("[JARVIS] Connected.")
                    # Resume after a 1008: do NOT kill in-flight browser PCM —
                    # that mid-sentence flush is the audible start/stop chop on
                    # every reconnect. Only a user interrupt clears the player;
                    # otherwise carry whatever the dead session still held so
                    # the sentence finishes without a hole.
                    if _had_interrupt:
                        self._flush_browser_audio()
                    if (
                        not _had_interrupt
                        and _old_audio_q is not None
                        and self._dashboard is not None
                    ):
                        _carry = bytearray()
                        while True:
                            try:
                                _carry.extend(_old_audio_q.get_nowait())
                            except asyncio.QueueEmpty:
                                break
                        if _carry:
                            print(f"[JARVIS] 🔁 carried {len(_carry)} B PCM "
                                  f"across reconnect")
                            try:
                                await self._dashboard.send_audio(bytes(_carry))
                            except Exception:
                                pass
                    self.set_speaking(False)
                    if self._turn_done_event:
                        self._turn_done_event.clear()
                    # Drop stale phone-mic frames buffered during the outage so
                    # the first utterance after reconnect isn't a jumble of
                    # half-seconds from before the 1008 abort.
                    if self._dashboard is not None:
                        try:
                            q = self._dashboard._phone_audio_queue
                            while True:
                                q.get_nowait()
                        except Exception:
                            pass
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Hey Jarvis" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log("SYS: JARVIS online — sleeping. Say 'Hey Jarvis' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: JARVIS online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    if not SERVER_MODE:
                        tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    if SERVER_MODE:
                        tg.create_task(self._play_audio_browser())
                    else:
                        tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    # topic monitoring runs on the persistent scheduler now
                    # (see _start_scheduler) — not per-session.
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_sleep_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = _exc_text(e)

                # Residual 1008 (missed goAway / race): reconnect with a pause
                # that doubles while the aborts keep landing back-to-back with
                # zero uplink. A fixed 1 s pause was a tight loop — every cycle
                # cut mid-speech and looked like the 1008s were multiplying.
                # Checked BEFORE the resumption-handle branch: a 1008 on a
                # resumed session still has the handle in its config dump, so
                # "handle" in the text used to steal it into the reject path
                # (streak never climbed, session_resumption never dropped).
                if "1008" in err_str and "abort" in err_str.lower():
                    _now_1008 = time.monotonic()
                    _prev_1008 = getattr(self, "_last_1008_at", 0.0) or 0.0
                    # Storm = aborts landing within 2 min of each other.
                    # Isolated aborts (long talk between) keep streak at 1 so
                    # backoff stays snappy; the total counter still escalates
                    # the knobs.
                    if _prev_1008 and (_now_1008 - _prev_1008) < 120.0:
                        self._idle_1008_streak = getattr(self, "_idle_1008_streak", 0) + 1
                    else:
                        self._idle_1008_streak = 1
                    self._last_1008_at = _now_1008
                    self._total_idle_1008 = getattr(self, "_total_idle_1008", 0) + 1
                    streak = self._idle_1008_streak
                    total = self._total_idle_1008
                    # First abort comes back fast (0.5 s) so a mid-answer drop
                    # only clips a breath; a storm doubles to stop the loop.
                    delay = 0.5 if streak == 1 else min(2 ** (streak - 1), 30)
                    if total >= 1 and self._compress_live:
                        # Preview knob, on during every observed idle abort
                        # (zero uplink, no goAway). Drop it on the FIRST one.
                        self._compress_live = False
                        print("[JARVIS] 🔧 Dropping context_window_compression "
                              f"after {total} idle 1008s")
                    if total >= 3 and self._resumption_live:
                        # Compression alone does not stop the periodic abort
                        # (long sessions still die) — resumption is next.
                        self._resumption_live = False
                        self._resume_handle = None
                        print("[JARVIS] 🔧 Dropping session_resumption "
                              f"after {total} idle 1008s")
                    if total >= 5 and self._enhanced_live:
                        # v1alpha proactivity: last preview knob standing.
                        self._enhanced_live = False
                        print("[JARVIS] 🔧 Dropping proactive audio "
                              f"after {total} idle 1008s")
                    if total >= 5 and self._tuned_live:
                        self._tuned_live = False
                        print("[JARVIS] 🔧 Dropping turn/media tuning "
                              f"after {total} idle 1008s")
                    print(
                        "[JARVIS] Live session aborted (1008) — reconnecting in"
                        f" {delay}s..."
                        f" tool={self._tool_call_pending}"
                        f" blocked={self._live_input_blocked}"
                        f" briefing={self._briefing_active}"
                        f" gen={self._session_gen}"
                        f" streak={streak}"
                        f" total={total}"
                    )
                    self.ui.write_log("SYS: Live session dropped — reconnecting.")
                    self._conn_backoff = delay
                    await asyncio.sleep(delay)
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Turn-taking / media / thinking knobs rejected by the server
                # (preview API drift) — drop them first, because they are the
                # newest fields and the cheapest to lose. Proactive audio is
                # tried again on the next pass if the error persists.
                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Quota / overload on Live itself (connect-time 429 or a
                # mid-session death that tore the TaskGroup down). When the
                # gateway voice fallback is configured, drop into it instead
                # of backoff-retrying a pool that stays empty for minutes —
                # the loop below probes Live every 2 min and comes back
                # automatically. Invalid key was handled above, so a bad key
                # still prompts reconfiguration rather than failing over.
                if _is_quota_error(err_str) and self._gw_fallback_allowed():
                    do_failover = True

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if do_failover:
                    pass  # fallback runs below; no backoff sleep
                elif is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            # Gateway voice failover: run until the recover probe sees Live
            # answer again (or an unrecoverable error), then jump straight
            # back to the top of the connect loop with zero backoff.
            if do_failover:
                print("[JARVIS] ⚠ Live quota exhausted — starting gateway voice fallback.")
                try:
                    await self._run_gateway_voice_loop()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[JARVIS] ⚠ Voice fallback ended with error: {e}")
                    traceback.print_exc()
                self._voice_backend = "live"
                self._conn_backoff = 0
                continue

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def _main_server() -> None:
    """Server mode entry: HeadlessUI + JarvisLive, no Qt, no local audio."""
    from core.headless_ui import HeadlessUI

    env_key = (os.environ.get("JARVIS_GEMINI_API_KEY") or "").strip()
    if env_key:
        try:
            from memory.config_manager import save_api_keys
            save_api_keys(env_key)   # read-modify-write: merges, keeps other fields
            print("[JARVIS] Gemini API key loaded from JARVIS_GEMINI_API_KEY.")
        except Exception as e:
            print(f"[JARVIS] Could not save JARVIS_GEMINI_API_KEY: {e}")

    ui = HeadlessUI()
    jarvis = JarvisLive(ui)
    print("[JARVIS] Server mode — the dashboard is the only UI.")
    if not ui.ready:
        print("[JARVIS] No API key yet — paste it in the dashboard setup overlay.")
    try:
        asyncio.run(jarvis.run())
    except KeyboardInterrupt:
        print("\n🔴 Shutting down...")


def main():
    # Installed before anything else runs, so a failure during startup is
    # captured rather than lost. Serves redacted recent errors on /api/health.
    try:
        from core import errorlog as _el
        _el.install()
    except Exception:
        pass
    if SERVER_MODE:
        _main_server()
        return

    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()
def _agents_hire(_ag, args: dict, budget: float) -> str:
    """Create or change an agent, and refuse to look successful when the only
    thing the user would need to know is that a budget was granted."""
    name = str(args.get("name") or "").strip()
    if not name:
        return "An agent needs a name."
    fresh = _ag.find(name) is None
    if fresh and not str(args.get("persona") or "").strip():
        return ("A new agent needs a persona \u2014 a sentence or two on how it "
                "should think and write. Without that it is only a name.")
    a = _ag.hire(name, role=str(args.get("role") or ""),
                 persona=str(args.get("persona") or ""),
                 tools=str(args.get("tools") or ""),
                 model=str(args.get("model") or "default"),
                 budget_usd_day=budget,
                 schedule=str(args.get("schedule") or ""))
    sched = str(args.get("schedule") or "").strip()
    if sched:
        try:
            from core import scheduler as _sc
            _sc.get_scheduler().add(f"{a['name']} \u2014 shift", "interval",
                                   sched, prompt=f"Run {a['name']}'s shift.",
                                   source="org", notify=True)
        except Exception as e:  # a bad cadence must not lose the hire
            return (f"{a['name']} is on the roster, but I could not schedule "
                    f"the shift ({e}).")
    return (f"{a['name']} is on the roster as {a.get('role', 'other')} with "
            f"tools: {' '.join(a.get('tools') or []) or 'none'}"
            + (f", ${a['budget_usd_day']:.2f}/day" if a.get("budget_usd_day") else "")
            + (f", scheduled {sched}" if sched else "") + ".")
def _agents_retire(_ag, args: dict, *, delete: bool) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return "Which agent?"
    try:
        r = _ag.retire(name, delete=delete)
    except ValueError as e:
        return str(e)
    if delete:
        return f"{r['deleted']} is gone \u2014 history and all."
    return f"{r['name']} is switched off. Its history stays; switch it back any time."

def _day(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except Exception:
        return "?"


def _hhm(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except Exception:
        return "?"


if __name__ == "__main__":
    main()