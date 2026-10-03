"""Shared brain — the SAME mind for local mode and server mode.

Everything here is pure Python (stdlib + sibling core modules): no Qt, no
audio, no display. Both bodies import from here, so a prompt fix, a persona
tweak or a memory rule lands in both at once. The bodies differ ONLY in I/O:
microphone vs browser mic, face vs orb, local tools vs server-safe tools.

Server-safe policy: a tool runs server-side only when its TOOL/PLUGIN dict
opts in with `"server": True`. Machine-local tools (screen, apps, volume)
stay desktop-only, and the limits block says so out loud.
"""

import re
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
PROMPT_PATH = _BASE / "core" / "prompt.txt"

_FALLBACK_PROMPT = (
    "You are JAARVIS, a real-time personal AI assistant. "
    "Be concise, direct, and always use the provided tools to complete tasks. "
    "Never simulate or guess results — always call the appropriate tool."
)


def load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return _FALLBACK_PROMPT


def render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} by plain replace — a stray brace in hand-written wording
    must never take the app down (same contract as the desktop builder)."""
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def describe_tools(declarations) -> str:
    """One line per capability, straight from the live tool declarations."""
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


def describe_limits(*, body: str, has_vision: bool, has_mic: bool) -> str:
    """What is out of reach, stated as architecture rather than rules."""
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
    ]
    if body == "server":
        out.append(
            "- You run on a server: its files, network and stats are yours, but "
            "the user's screen, speakers, apps and devices live on their desktop "
            "body, which you cannot touch from here. Say so plainly when asked.")
    else:
        out.append(
            "- You act on this machine only. You cannot reach the user's other "
            "devices, accounts or hardware except through the tools listed above.")
    out.append(
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.")
    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def server_safe(declaration) -> bool:
    """Opt-in: only tools declaring `"server": True` run in server mode."""
    try:
        if isinstance(declaration, dict):
            return declaration.get("server") is True
        return getattr(declaration, "server", False) is True
    except Exception:
        return False


def persona_brief(persona_key: str) -> str:
    try:
        from core.personas import PERSONAS, valid_persona
        return PERSONAS[valid_persona(persona_key)]["brief"] + "\n"
    except Exception:
        return ""


def address_line(user_name: str) -> str:
    if user_name:
        return (f"ADDRESS: Always call the user '{user_name}'.")
    return ('ADDRESS: Address the user with the ordinary respectful form '
            'for a superior in the language you are currently speaking — '
            '"sir" in English, its everyday equivalent in any other language.')


def time_block(now=None) -> str:
    now = now or datetime.now()
    return ("[CURRENT DATE & TIME]\n"
            f"Right now it is: {now.strftime('%A, %B %d, %Y — %I:%M %p')}\n"
            "Use this to calculate exact times for reminders.\n\n")


def assemble_system(*, asst_name: str, user_name: str = "",
                    persona_key: str = "jarvis", memory_str: str = "",
                    declarations=None, platform: str = "",
                    capabilities: str = "", limits: str = "",
                    body: str = "local") -> str:
    """Full system instruction from the same parts in both bodies."""
    _ = body  # kept for call-site clarity; limits text already carries it
    template = load_system_prompt()
    sys_prompt = render_prompt(template, {
        "assistant_name": asst_name,
        "platform": platform,
        "capabilities": capabilities,
        "limits": limits,
    })
    parts = [time_block(),
             f"[IDENTITY]\nYour name is {asst_name}. "
             f"Always refer to yourself as {asst_name}.\n{address_line(user_name)}\n\n",
             persona_brief(persona_key)]
    if memory_str:
        parts.append(memory_str)
    parts.append(sys_prompt)
    return "\n".join(parts)
