"""
core/data_paths.py — where persistent files live.

Priority:
  1. JARVIS_DATA env (explicit override / E2E)
  2. /data when it exists and is writable (HF Spaces persistent storage)
  3. repo root (local desktop)

Config, memory, uploads and certs all hang off this so a Space with a
/data volume keeps keys and history across restarts.
"""
from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    env = (os.environ.get("JARVIS_DATA") or "").strip()
    if env:
        p = Path(env)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    hf = Path("/data")
    if hf.is_dir():
        try:
            probe = hf / ".jarvis_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return hf
        except OSError:
            pass
    return repo_root()


def config_dir() -> Path:
    p = data_root() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p


def memory_dir() -> Path:
    p = data_root() / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p


def uploads_dir() -> Path:
    p = data_root() / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def certs_dir() -> Path:
    p = config_dir() / "certs"
    p.mkdir(parents=True, exist_ok=True)
    return p
