# config/__init__.py
import json, os, platform
from pathlib import Path

# Repo-local default; data-root (/data on HF Spaces) wins when present.
_CONFIG_PATH = Path(__file__).parent / "api_keys.json"


def _resolve_config_path() -> Path:
    try:
        from core.data_paths import config_dir
        p = config_dir() / "api_keys.json"
        if p.is_file():
            return p
    except Exception:
        pass
    return _CONFIG_PATH


def _platform_os() -> str:
    """Auto-detect OS when config file is absent."""
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )


def get_config() -> dict:
    try:
        with open(_resolve_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_os() -> str:
    """Returns: 'windows' | 'mac' | 'linux'"""
    return get_config().get("os_system", _platform_os()).lower()

def is_windows() -> bool: return get_os() == "windows"
def is_mac()     -> bool: return get_os() == "mac"
def is_linux()   -> bool: return get_os() == "linux"
