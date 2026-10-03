"""Settings a person can change, stored where they survive a restart.

The city was an environment variable, which is fine for a deployment and useless
for a person: it means the only way to tell Jarvis where you are is to edit the
Space's variables and redeploy. That is not a setting, that is a build step.

So the things a human is expected to change live here instead — one JSON file on
the persistent volume — and environment variables stay what they are good at:
deployment-wide defaults and overrides for tests.

Precedence is deliberate and one-directional: an explicit environment variable
wins over the stored value. An operator who sets JARVIS_CITY in the Space is
making a statement about the deployment, and a value typed into a dashboard
should not silently outrank it. Every key is stored as a string because there is
no schema to validate against, and a settings file that refuses to load is a
settings file that loses the user's work.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

#: Keys a person may set. Anything else in the file is ignored rather than
#: trusted, so a hand-edited file cannot introduce a setting nothing reads.
KNOWN = ("city",)

_FILE = "settings.json"


def _path() -> Path:
    from core.data_paths import data_root
    d = data_root()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / _FILE


def _read() -> dict:
    try:
        raw = _path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k in KNOWN}
    except Exception:
        return {}
    return {}


def _write(data: dict) -> bool:
    """Write atomically. A half-written settings file is a lost settings file,
    and this runs on a persistent volume that outlives the process."""
    target = _path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".settings-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            return True
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception:
        return False


def get(key: str, default: str = "") -> str:
    """The stored value, unless the environment overrides it."""
    if key not in KNOWN:
        return default
    env = os.environ.get(_env_name(key))
    if env is not None and env.strip():
        return env.strip()
    val = _read().get(key, default)
    return str(val).strip() if val is not None else default


def set_(key: str, value: str) -> dict:
    """Store a value. An empty string clears it, which is a real setting:
    "I have not told you where I am" is different from a stale guess."""
    if key not in KNOWN:
        return {"ok": False, "error": f"Unknown setting '{key}'."}
    data = _read()
    text = (value or "").strip()
    if text:
        data[key] = text
    else:
        data.pop(key, None)
    ok = _write(data)
    return {"ok": ok, "key": key, "value": data.get(key, ""),
            "error": "" if ok else "Could not write the settings file."}


def all_settings() -> dict:
    """Everything, with environment overrides applied — what the dashboard shows."""
    out = {}
    for key in KNOWN:
        out[key] = get(key)
    return out


def _env_name(key: str) -> str:
    return "JARVIS_CITY" if key == "city" else f"JARVIS_{key.upper()}"