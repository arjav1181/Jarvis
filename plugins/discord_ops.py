"""Discord — read and post in your servers by voice.

Uses the Discord REST API directly (zero new dependencies). Needs a bot token
in config (discord_bot_token) with Message Content intent; the channel id comes
from parameters. Say "read the announcements channel", "post to general that
the build is green". Voice-channel SPEAKING needs the gateway voice stack
(discord.py + PyNaCl + ffmpeg) — say "join voice" and it tells you exactly
what to install instead of pretending. Never raises.
"""

import json
import urllib.parse
import urllib.request

_API = "https://discord.com/api/v10"


def _read_token() -> str:
    try:
        from memory.config_manager import load_api_keys
        return load_api_keys().get("discord_bot_token", "") or ""
    except Exception:
        return ""


def _api(token: str, path: str, data: dict | None = None,
         timeout: int = 25) -> dict | list:
    req = urllib.request.Request(
        _API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bot {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "read").strip().lower()
    channel = (params.get("channel_id") or "").strip()
    token = _read_token()
    if not token:
        return ("Discord is not connected, sir — add a bot token as "
                "discord_bot_token in config and grant Message Content intent.")
    try:
        if action == "read":
            if not channel:
                return "Which channel id should I read, sir?"
            msgs = _api(token, f"/channels/{channel}/messages?limit=5")
            if not msgs:
                return "Channel is quiet, sir."
            lines = [f"{m.get('author', {}).get('username', '?')}: "
                     f"{m.get('content', '')[:200]}" for m in msgs]
            return "Latest messages:\n" + "\n".join(lines)
        if action == "send":
            if not channel:
                return "Which channel id should I post to, sir?"
            text = (params.get("text") or "").strip()[:1900]
            if not text:
                return "What should I say, sir?"
            _api(token, f"/channels/{channel}/messages", {"content": text})
            return "Posted, sir."
        if action == "voice":
            try:
                import discord  # noqa
            except Exception:
                return ("Voice channels need the voice stack, sir: pip install "
                        "'discord.py PyNaCl' plus ffmpeg, then ask again. "
                        "Text channels work right now.")
            return ("Voice stack present but voice sessions are not wired yet, "
                    "sir — text channels work right now.")
        return f"Unknown discord action '{action}' — try read, send, voice."
    except Exception as e:
        body = str(e)[:200]
        if "401" in body or "403" in body:
            return "Discord refused the token, sir — check it and the bot's role."
        return f"Discord failed, sir: {body}"


PLUGIN = {
    "server": True,  # cloud API, body-independent
    "name": "discord_ops",
    "description": ("Discord by voice: read recent channel messages, post to a "
                    "channel. Trigger on 'discord', 'read the channel', 'post to "
                    "discord'. Do NOT use for Telegram, WhatsApp or general chat."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "read, send, voice"},
            "channel_id": {"type": "STRING",
                           "description": "Discord channel id"},
            "text": {"type": "STRING", "description": "Message text (send only)"},
        },
        "required": [],
    },
}
