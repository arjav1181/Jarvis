import json
import os
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # Prefer JARVIS_DATA / /data (HF Spaces volume) when present.
    try:
        from core.data_paths import data_root
        root = data_root()
        if root != Path(__file__).resolve().parent.parent:
            return root
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data["gemini_api_key"] = gemini_api_key.strip()

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'JARVIS' if not set."""
    return load_api_keys().get("assistant_name", "JARVIS") or "JARVIS"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "JARVIS"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE    = "Charon"


def get_voice() -> str:
    """Return the configured Live voice, falling back to the default if unset
    or if the stored value is not a voice we recognise."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_wake_word_enabled() -> bool:
    """Whether local wake-word gating is on (assistant sleeps until 'Hey Jarvis')."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["wake_word_enabled"] = bool(enabled)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_push_to_talk_enabled() -> bool:
    """Hold-a-key-to-speak. When on, the mic is closed unless the chord is held."""
    return load_api_keys().get("push_to_talk_enabled", False)


def save_push_to_talk_enabled(enabled: bool) -> None:
    _save_flag("push_to_talk_enabled", enabled)


HUD_STYLES = ("face", "core")


def get_hud_style() -> str:
    """Which centrepiece the HUD draws: the animated head, or the reactor core.

    Taste, not capability — both render in the same software painter and cost
    about the same. Defaults to the head because that is what MARK LIV shipped
    with; anyone who preferred the older look can switch back in ⚙ and the
    choice survives a restart.
    """
    v = str(load_api_keys().get("hud_style", "face")).strip().lower()
    return v if v in HUD_STYLES else "face"


def save_hud_style(style: str) -> None:
    s = str(style or "").strip().lower()
    _save_flag("hud_style", s if s in HUD_STYLES else "face")


# ── Live-session tuning ──────────────────────────────────────────────────────
# Everything here is optional and has a working default, so an untouched
# config behaves exactly like a configured one. Each value is also a way out:
# if a future model dislikes one of these, set it back and nothing else changes.

def get_thinking_enabled() -> bool:
    """Whether the Live model may spend tokens thinking before it answers.

    Off by default. A voice assistant is judged on how fast it starts talking,
    and the reasoning that actually needs deliberation in this app is delegated
    to the planning tools, which run on a separate non-Live model.
    """
    return bool(load_api_keys().get("thinking_enabled", False))


def save_thinking_enabled(enabled: bool) -> None:
    _save_flag("thinking_enabled", enabled)


def get_turn_tuning() -> dict:
    """How eagerly the server decides you have stopped speaking.

    OFF by default, and that default was earned. Cutting turns shorter looks
    like a free speed win and is not: proactive audio has to judge whether an
    utterance was even addressed to the assistant, and a turn clipped early
    gives it less to judge, so it stays quiet — and the reply to your first
    sentence only arrives once your second one has given it enough context.
    That reads as the assistant being a turn behind, which is far worse than
    the fraction of a second the tuning saves.

    Turn it on with "turn_tuning": {"enabled": true} if your own microphone and
    speaking pace suit it. `silence_ms` is the one that is felt: the pause the
    server sits through before accepting your turn is over.
    """
    cfg = load_api_keys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled":    bool(cfg.get("enabled", False)),
        "silence_ms": _int("silence_ms", 550, 200, 3000),
        "prefix_ms":  _int("prefix_ms", 150, 0, 1000),
        # "high" = quicker to decide speech has ended.
        "end_sensitivity":   str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }


def save_turn_tuning(values: dict) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cur = data.get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    data["turn_tuning"] = cur
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_proactive_audio_enabled() -> bool:
    """Whether the model gets to decide an utterance was not aimed at it and
    stay quiet.

    On by default — it is what stops the assistant answering the room. But it
    is also the first thing to switch off if replies ever seem to arrive a turn
    late: what looks like lag is usually the model having judged your previous
    sentence as not addressed to it, and only changing its mind once the next
    one arrives.
    """
    return bool(load_api_keys().get("proactive_audio", True))


def save_proactive_audio_enabled(enabled: bool) -> None:
    _save_flag("proactive_audio", enabled)


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def get_media_resolution() -> str:
    """How finely the model tokenises the screenshots and camera frames it is
    sent. 'medium' keeps on-screen text readable at a fraction of the tokens a
    full-resolution frame costs; 'low' is cheaper still but starts losing small
    text, which is most of what screen captures are for."""
    v = str(load_api_keys().get("media_resolution", "medium")).strip().lower()
    return v if v in MEDIA_RESOLUTIONS else "medium"


def save_media_resolution(value: str) -> None:
    v = str(value or "").strip().lower()
    _save_flag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")


def _save_flag(key: str, value) -> None:
    """Read-modify-write one key without disturbing the rest of the config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[key] = bool(value) if isinstance(value, bool) else value
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> None:
    """Read-modify-write one or more keys in api_keys.json.

    Every setter in this file open-coded this. Collapsing it here means a new
    setting is one line, and there is one place where a corrupt config file is
    handled instead of nine."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update(fields)
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# several printer plugins) can share ONE namespace.
def get_plugin_config(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    """Merge `values` into a namespace's stored config (read-modify-write, like
    every other helper here). Only the provided keys are touched."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


# ── OpenAI-compatible gateway (dual-backend LLM) ────────────────────────────
# Optional: when base URL + key are set, auxiliary/tool LLM calls try the
# gateway first (core/gemini.py) and voice failover can run STT→chat→TTS
# through it when Gemini Live is out of quota. Every field is also settable
# via env — env wins over the file, and the dash shows ENV-LOCKED for those.

# (config key, primary env, alternate env, default)
_OPENAI_FIELDS = (
    ("openai_base_url",  "JARVIS_OPENAI_BASE_URL",  "OPENAI_BASE_URL",  ""),
    ("openai_api_key",   "JARVIS_OPENAI_API_KEY",   "OPENAI_API_KEY",   ""),
    ("openai_model",     "JARVIS_OPENAI_MODEL",     "OPENAI_MODEL",     ""),
    ("voice_model",      "JARVIS_VOICE_MODEL",      "",                 ""),
    ("openai_voice",     "JARVIS_OPENAI_VOICE",     "",                 "alloy"),
    ("stt_model",        "JARVIS_STT_MODEL",        "",                 "whisper-1"),
    ("tts_model",        "JARVIS_TTS_MODEL",        "",                 "tts-1"),
    ("stt_mode",         "JARVIS_STT_MODE",         "",                 "auto"),
    ("tts_mode",         "JARVIS_TTS_MODE",         "",                 "auto"),
    ("voice_fallback",   "JARVIS_VOICE_FALLBACK",   "",                 "true"),
)

_STT_TTS_MODES = ("auto", "gateway", "local")


def _env_first(env_a: str, env_b: str, file_val, default):
    """Env (primary, then alternate) wins over the config file over the default.
    Returns (value, env_locked)."""
    if env_a:
        v = os.environ.get(env_a)
        if v is not None and v.strip() != "":
            return v.strip(), True
    if env_b:
        v = os.environ.get(env_b)
        if v is not None and v.strip() != "":
            return v.strip(), True
    if file_val is not None and str(file_val).strip() != "":
        return str(file_val).strip(), False
    return default, False


def get_openai_settings() -> dict:
    """Resolved gateway settings. `values` is what the app uses (env wins);
    `env_locked` maps each key to whether an env var is overriding the file
    (for the dash badge); `raw_file` is the un-merged file view."""
    file_data = load_api_keys()
    values, env_locked, raw_file = {}, {}, {}
    for key, env_a, env_b, default in _OPENAI_FIELDS:
        fv = file_data.get(key)
        val, locked = _env_first(env_a, env_b, fv, default)
        if key in ("stt_mode", "tts_mode") and val not in _STT_TTS_MODES:
            val = default
        if key == "voice_fallback":
            val = str(val).strip().lower() not in ("0", "false", "no", "off")
        values[key] = val
        env_locked[key] = locked
        raw_file[key] = fv
    return {"values": values, "env_locked": env_locked, "raw_file": raw_file}


def save_openai_settings(**fields) -> None:
    """Merge provided gateway fields into api_keys.json. Only known keys are
    written; empty-string values persist (user cleared a field deliberately).
    An env-locked key still writes the file (env keeps winning at read time) —
    the dash shows the badge so it is not a surprise."""
    allowed = {k for k, _, _, _ in _OPENAI_FIELDS}
    payload = {k: v for k, v in fields.items() if k in allowed}
    if not payload:
        return
    for k in ("stt_mode", "tts_mode"):
        if k in payload and payload[k] not in _STT_TTS_MODES:
            payload[k] = "auto"
    if "voice_fallback" in payload:
        payload["voice_fallback"] = bool(payload["voice_fallback"])
    _patch_config(**payload)


# ── Map / space providers (Phase 4f) ─────────────────────────────────────────
# The globe is the real God's Eye View. It renders keyless (Esri satellite),
# but a Google Maps key unlocks photorealistic 3D Tiles — the look in the
# project's own videos — and a Cesium ion token gives real terrain elevation.
# Both are *browser-side* keys by design (their README says to restrict them at
# the provider); we inject them into the page we serve rather than baking them
# into the vendored build, so adding one is a settings save, not a rebuild.
#
# Env wins over the file, same as the gateway block above, so the tidier option
# on a Space is a repo secret rather than anything pasted into a dashboard.
_PROVIDER_FIELDS = (
    ("google_maps_key", "JARVIS_GOOGLE_MAPS_KEY", "GOOGLE_MAPS_API_KEY", ""),
    ("cesium_ion_token", "JARVIS_CESIUM_ION_TOKEN", "CESIUM_ION_TOKEN", ""),
)


def get_provider_settings() -> dict:
    """Resolved map/space provider keys, with `env_locked` per key."""
    file_data = load_api_keys()
    values, env_locked, raw_file = {}, {}, {}
    for key, env_a, env_b, default in _PROVIDER_FIELDS:
        fv = file_data.get(key)
        val, locked = _env_first(env_a, env_b, fv, default)
        values[key] = val
        env_locked[key] = locked
        raw_file[key] = fv
    return {"values": values, "env_locked": env_locked, "raw_file": raw_file}


def save_provider_settings(**fields) -> None:
    """Merge known provider keys into api_keys.json. Unknown keys are ignored;
    an empty string persists (the user cleared it on purpose)."""
    allowed = {k for k, _, _, _ in _PROVIDER_FIELDS}
    payload = {k: str(v or "").strip() for k, v in fields.items() if k in allowed}
    if payload:
        _patch_config(**payload)


def mask_secret(v: str) -> str:
    """Show the last four only, like the gateway settings do."""
    v = str(v or "")
    return f"\u2022\u2022\u2022\u2022{v[-4:]}" if v else ""


def voice_model_effective() -> str:
    """voice_model when set, else openai_model — one call for the fallback loop."""
    v = get_openai_settings()["values"]
    return (v.get("voice_model") or "").strip() or (v.get("openai_model") or "").strip()


def gateway_enabled() -> bool:
    """True when a base URL is configured (key may be empty for local gateways)."""
    v = get_openai_settings()["values"]
    return bool((v.get("openai_base_url") or "").strip())


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")