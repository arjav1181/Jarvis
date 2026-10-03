"""Shared briefings — the same morning-briefing brain in both bodies.

Desktop fires it at boot; the server fires it on a schedule and holds it as
pending until someone connects ("while you were away..."). Content is memory +
time only — no network, no second model beyond the cheap text call that words
it. Pending queue lives next to the memory store.
"""

import json
import os
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent


def pending_path(base=None) -> Path:
    root = Path(base) if base else _BASE
    return root / "memory" / "briefing_pending.json"


def read_pending(base=None) -> list:
    try:
        return json.loads(pending_path(base).read_text(encoding="utf-8"))
    except Exception:
        return []


def push_pending(text: str, base=None) -> None:
    try:
        items = read_pending(base)
        items.append({"ts": datetime.now().isoformat(), "text": text})
        pending_path(base).write_text(json.dumps(items), encoding="utf-8")
    except Exception:
        pass


def take_pending(base=None) -> list:
    items = read_pending(base)
    try:
        pending_path(base).write_text("[]", encoding="utf-8")
    except Exception:
        pass
    return items


def word_briefing(memory: dict, now=None) -> str:
    """Render the briefing from memory without any model call (fallback)."""
    now = now or datetime.now()
    facts = []
    try:
        for cat, entries in (memory or {}).items():
            if not isinstance(entries, dict):
                continue
            for key, val in entries.items():
                v = val.get("value", "") if isinstance(val, dict) else val
                if v:
                    facts.append(f"{key}: {v}")
    except Exception:
        pass
    head = now.strftime("%A, %B %d — %I:%M %p")
    if not facts:
        return (f"{head}. Nothing on the books yet — a quiet day, "
                f"which I intend to keep that way.")
    top = "; ".join(facts[:6])
    return f"{head}. Still on file: {top}."


def model_briefing(memory: dict, api_key: str,
                   model: str = "gemini-2.5-flash") -> str:
    """Word the briefing with the cheap text model; fallback is word_briefing."""
    try:
        from google import genai
        from memory.memory_manager import format_memory_for_prompt
        client = genai.Client(api_key=api_key)
        prompt = ("Write a two-sentence spoken morning briefing for the user's "
                  "AI butler from these stored facts (or a quiet-day line if "
                  "empty). No lists, no markdown.\n\n"
                  + format_memory_for_prompt(memory))
        resp = client.models.generate_content(model=model, contents=prompt)
        text = (resp.text or "").strip()
        return text if text else word_briefing(memory)
    except Exception:
        return word_briefing(memory)


def scheduled_check(memory: dict, api_key: str, base=None) -> str | None:
    """One scheduler tick: word a briefing and queue it. Returns the text."""
    text = model_briefing(memory, api_key) if api_key else word_briefing(memory)
    push_pending(text, base)
    return text
