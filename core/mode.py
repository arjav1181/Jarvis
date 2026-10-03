"""
core/mode.py — single source of truth for desktop vs server mode.

JARVIS_MODE=server (or headless) flips the whole app to full headless
operation: no Qt window, no local sounddevice streams, the FastAPI
dashboard is the only UI, and voice in/out flows through the browser.
Every other module imports SERVER_MODE from here so the two modes can
never disagree about which one is active.
"""

from __future__ import annotations

import os


def is_server_mode() -> bool:
    """True when JARVIS_MODE is 'server' or 'headless' (case-insensitive)."""
    return (os.environ.get("JARVIS_MODE") or "").strip().lower() in ("server", "headless")


SERVER_MODE: bool = is_server_mode()
