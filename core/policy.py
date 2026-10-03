"""core/policy.py — one place that decides what JARVIS is allowed to do.

Phase 5. Before this, "is this risky?" was answered per tool by hand, scattered
across the dispatch, and the only record of what happened was a print. That is
fine until the first thing goes wrong: nobody can answer "what did it do?", "who
asked?", or "why did that cost money?".

Four tiers, and the default is the point:

    read    inspect something; no side effect that anyone would notice
    act     change state on our own systems (tasks, schedules, display, memory)
    spend   costs money or talks to a third party on someone's behalf
    delete  destroys something

A tool that is not in the registry is treated as `spend` and needs approval.
The failure mode we care about is a *new* tool shipping by accident, so the
unknown case is the strict one, not the permissive one.

Everything is configurable from the dashboard (Phase 9's config console writes
here) and every decision is written to an append-only audit log.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

TIERS = ("read", "act", "spend", "delete")
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

# ── the registry ─────────────────────────────────────────────────────────────
# tier: read | act | spend | delete
# cap:  None            no money involved
#       float           the USD ceiling for ONE call (checked before it runs)
# approval: whether a call above needs the human gate
TOOLS: dict[str, dict[str, Any]] = {
    # ── read: looking is free ──
    "recall_memory":   {"tier": "read", "approval": False, "cap": None,
                        "what": "search saved memory"},
    "brief":           {"tier": "read", "approval": False, "cap": None,
                        "what": "write an agent's work order"},
    "system_status":   {"tier": "read", "approval": False, "cap": None,
                        "what": "system health"},
    "task_status":     {"tier": "read", "approval": False, "cap": None,
                        "what": "coding task status"},
    "display":         {"tier": "read", "approval": False, "cap": None,
                        "what": "put something on the screen"},

    # ── act: our own state, reversible-ish ──
    "agents":          {"tier": "act", "approval": False, "cap": None,
                        "what": "the agent roster"},
    "journal":         {"tier": "act", "approval": False, "cap": None,
                        "what": "the daily journal"},
    "goals":           {"tier": "act", "approval": False, "cap": None,
                        "what": "standing goals and the director"},
    "crew":            {"tier": "read", "approval": False, "cap": None,
                        "what": "the bots — reading is free, and a bot still "
                                "asks before anything outward-facing"},
    "skills":          {"tier": "read", "approval": False, "cap": None,
                        "what": "reusable methods — reading is free, saving "
                                "or running one asks"},
    "phone":           {"tier": "read", "approval": False, "cap": None,
                        "what": "the user's phone — reading is free, making "
                                "noise or opening something asks"},
    # Plugins, filed by what they can actually do. An unregistered tool falls
    # through to the fail-closed default, which is `spend` — so WITHOUT this
    # every plugin call asks, including a plain search. That is safe and
    # useless at the same time: a user asked to approve ten times a day stops
    # reading the prompt, and then it approves things too. So the read-only
    # ones are filed as reads, and the rest are not.
    "recall":          {"tier": "read", "approval": False, "cap": None,
                        "what": "searching what the user has said — reads "
                                "their own history and sends nothing"},
    "summarise":       {"tier": "read", "approval": False, "cap": None,
                        "what": "fetching a page and reading it — writes nothing"},
    "health_svc":      {"tier": "read", "approval": False, "cap": None,
                        "what": "a health check — changes nothing, ever"},
    "agent_work":      {"tier": "spend", "approval": True, "cap": None,
                        "what": "an autonomous agent that writes files and runs "
                                "code — the most consequential thing here, so "
                                "it always asks"},
    "delegate_agent":  {"tier": "act", "approval": True, "cap": None,
                        "what": "hand a task to a real agent, which runs code "
                                "and may touch files — so it always asks"},
    "day_glance":      {"tier": "read", "approval": False, "cap": None,
                        "what": "your day, read-only by default; adding or "
                                "ticking something off asks"},
    "quiz_me":         {"tier": "read", "approval": False, "cap": None,
                        "what": "a quiz — it keeps its own notes, none of yours"},
    "exam_cram":       {"tier": "read", "approval": False, "cap": None,
                        "what": "revision planning — its own notes"},
    "scene":           {"tier": "act", "approval": True, "cap": None,
                        "what": "running a scene is several actions at once, "
                                "so it always asks"},
    "github_ops":      {"tier": "read", "approval": False, "cap": None,
                        "what": "GitHub — reading is free, writing to a "
                                "public repository is not"},
    "discord_ops":     {"tier": "read", "approval": False, "cap": None,
                        "what": "Discord — reading is free, posting commits "
                                "you to other people"},
    "homelab":         {"tier": "act", "approval": True, "cap": None,
                        "what": "your machines — it can power things on and "
                                "off, so it always asks"},
    "welcome":         {"tier": "read", "approval": False, "cap": None,
                        "what": "the welcome ceremony — listening, looking and "
                                "speaking to the user, changing nothing they did "
                                "not ask for"},
    "computer":        {"tier": "read", "approval": False, "cap": None,
                        "what": "the shared computer — looking is free; "
                                "clicking, typing and navigating ask, because "
                                "typing is how something gets sent"},
    "coder":           {"tier": "read", "approval": False, "cap": None,
                        "what": "the coding agent — reading is free, making "
                                "changes asks"},
    # the action-loader tools, so a name without a recognised action still
    # lands on a sensible tier instead of the fail-closed default
    "web_search":      {"tier": "read", "approval": False, "cap": None,
                        "what": "a web search — reads, changes nothing"},
    "web_fetch":       {"tier": "read", "approval": False, "cap": None,
                        "what": "fetching a page"},
    "weather_report":  {"tier": "read", "approval": False, "cap": None,
                        "what": "the weather"},
    "flight_finder":   {"tier": "read", "approval": False, "cap": None,
                        "what": "flight prices"},
    "file_processor":  {"tier": "read", "approval": False, "cap": None,
                        "what": "reading and converting documents"},
    "desktop_control": {"tier": "act",  "approval": False, "cap": None,
                        "what": "the desktop"},
    "browser_control": {"tier": "act",  "approval": False, "cap": None,
                        "what": "the browser"},
    "computer_control": {"tier": "act", "approval": True, "cap": None,
                        "what": "mouse and keyboard"},
    "computer_settings": {"tier": "act", "approval": True, "cap": None,
                          "what": "system settings"},
    "play_music":      {"tier": "act",  "approval": False, "cap": None,
                        "what": "music playback"},
    "file_controller": {"tier": "act",  "approval": True,  "cap": None,
                        "what": "files — reading is free, changing asks"},
    "code_helper":     {"tier": "act",  "approval": True,  "cap": None,
                        "what": "writing and running code"},
    "dev_agent":       {"tier": "act",  "approval": True,  "cap": None,
                        "what": "the project builder"},
    "open_app":        {"tier": "act",  "approval": False, "cap": None,
                        "what": "opening an application"},
    "reminder":        {"tier": "act",  "approval": False, "cap": None,
                        "what": "a reminder"},
    "send_message":    {"tier": "spend", "approval": True,  "cap": None,
                        "what": "sending a message to another person"},
    "youtube_video":   {"tier": "read", "approval": False, "cap": None,
                        "what": "YouTube"},
    "game_updater":    {"tier": "act",  "approval": True,  "cap": None,
                        "what": "game updates"},
    "email":           {"tier": "read", "approval": False, "cap": None,
                        "what": "the mailbox — reading is free, sending asks"},
    "github":          {"tier": "read", "approval": False, "cap": None,
                        "what": "GitHub — reading is free, commenting asks"},
    "vercel":          {"tier": "read", "approval": False, "cap": None,
                        "what": "Vercel — reading is free, redeploying asks"},
    "hf":              {"tier": "read", "approval": False, "cap": None,
                        "what": "Hugging Face — reading is free, restarting asks"},
    "calendar":        {"tier": "act", "approval": False, "cap": None,
                        "what": "read and write the calendar"},
    "files":           {"tier": "act", "approval": False, "cap": None,
                        "what": "the document store"},
    "home":            {"tier": "act", "approval": False, "cap": None,
                        "what": "devices and scenes"},
    "browser":         {"tier": "act", "approval": False, "cap": None,
                        "what": "drive a real browser"},
    "voice":           {"tier": "read", "approval": False, "cap": None,
                        "what": "the voice loop"},
    "proactive":       {"tier": "act", "approval": False, "cap": None,
                        "what": "watches and briefings"},
    "save_memory":     {"tier": "act", "approval": False, "cap": None,
                        "what": "remember something"},
    "undo":            {"tier": "act", "approval": False, "cap": None,
                        "what": "undo the last action"},
    "manage_schedule": {"tier": "act", "approval": False, "cap": None,
                        "what": "add/change/remove scheduled jobs"},
    "manage_monitor":  {"tier": "act", "approval": False, "cap": None,
                        "what": "background monitoring jobs"},
    "screen_process":  {"tier": "act", "approval": False, "cap": None,
                        "what": "control the screen process"},
    "close_camera":    {"tier": "act", "approval": False, "cap": None,
                        "what": "close the camera"},
    "globe":           {"tier": "act", "approval": False, "cap": None,
                        "what": "drive the globe"},
    "maps":            {"tier": "act", "approval": False, "cap": None,
                        "what": "maps, geocoding, directions"},
    "leads":           {"tier": "read", "approval": False, "cap": None,
                        "what": "discover/score/enrich/draft leads"},

    # ── spend / act-on-the-world: a human should know ──
    "device":          {"tier": "act", "approval": True, "cap": None,
                        "what": "run something on a paired machine"},
    "code_task":       {"tier": "act", "approval": False, "cap": None,
                        "what": "start a coding task"},
    "delegate":        {"tier": "act", "approval": False, "cap": None,
                        "what": "spawn sub-agents"},
    "push_task":       {"tier": "spend", "approval": True, "cap": None,
                        "what": "push code to a remote"},
    "cancel_task":     {"tier": "act", "approval": False, "cap": None,
                        "what": "cancel a task"},
    "shutdown_jarvis": {"tier": "delete", "approval": True, "cap": None,
                        "what": "shut JARVIS down"},
    # Phase 6/7/8 land here as they ship; unknown tools fail closed.
    "knowledge":       {"tier": "act", "approval": False, "cap": None,
                        "what": "ingest/recall knowledge"},
    "calendar":        {"tier": "read", "approval": False, "cap": None,
                        "what": "read your calendar"},
    "agents":          {"tier": "act", "approval": False, "cap": None,
                        "what": "manage the agent roster"},
}

#: Anything charged per call that we know the price of. Keyed by tool; the
#: value is a ceiling, the real number comes from the tool's result.
COSTS: dict[str, float] = {}

#: Per-ACTION rules, because risk is not a property of the tool but of what you
#: asked it to do. Drafting an invoice is a draft; SENDING one is money moving.
#: Without this, either every invoice action needs approval (so listing your
#: invoices needs a human) or none of them do (so sending one does not).
ACTIONS: dict[str, dict[str, dict[str, Any]]] = {
    "crew": {
        "list":  {"tier": "read", "approval": False},
        "brief": {"tier": "read", "approval": False},
        # delegating is an act, and whatever the bot then does still passes the
        # gate on its own — a bot is not a way around approvals
        "say":   {"tier": "act",  "approval": False},
        "tell":  {"tier": "act",  "approval": False},
        "delegate": {"tier": "act", "approval": False},
    },
    "skills": {
        "list":   {"tier": "read", "approval": False},
        "show":   {"tier": "read", "approval": False},
        # writing a method that may later run unattended is not a small thing
        "create": {"tier": "act",  "approval": True},
        "save":   {"tier": "act",  "approval": True},
        "add":    {"tier": "act",  "approval": True},
        "new":    {"tier": "act",  "approval": True},
        "teach":  {"tier": "act",  "approval": True},
        # running one is gated by the skill's OWN approval field: a read-only
        # skill runs unattended, anything else asks
        "run":    {"tier": "act",  "approval": True},
        "delete": {"tier": "act",  "approval": True},
    },
    "phone": {
        # reading the device changes nothing
        "status":  {"tier": "read", "approval": False},
        "battery": {"tier": "read", "approval": False},
        "locate":  {"tier": "read", "approval": False},
        "read":    {"tier": "read", "approval": False},
        # making noise at someone, or asking to open something, asks first
        "buzz":    {"tier": "act", "approval": True},
        "notify":  {"tier": "act", "approval": True},
        "wake":    {"tier": "act", "approval": True},
        "open":    {"tier": "act", "approval": True},
        "report":  {"tier": "read", "approval": False},
    },
    # ── the action-loader tools ─────────────────────────────────────────────
    # None of these were registered, so every one of them fell through to
    # DEFAULT_TIER = "spend" and asked for approval — including a plain web
    # search, which is how the assistant came to look like it wanted permission
    # for everything. Reads are free; anything that touches the machine, the
    # filesystem or another person still asks.
    "web_search":      {"tier": "read",  "approval": False},
    "web_fetch":       {"tier": "read",  "approval": False},
    "weather_report":  {"tier": "read",  "approval": False},
    "flight_finder":   {"tier": "read",  "approval": False},
    "game_updater": {
        "list":              {"tier": "read", "approval": False},
        "download_status":   {"tier": "read", "approval": False},
        "schedule_status":   {"tier": "read", "approval": False},
        "install":           {"tier": "act",  "approval": True},
        "update":            {"tier": "act",  "approval": True},
        "schedule":          {"tier": "act",  "approval": True},
        "cancel_schedule":   {"tier": "act",  "approval": True},
    },
    "file_processor": {
        # reading and inspecting a document is free; rewriting one is not
        "list":         {"tier": "read", "approval": False},
        "info":         {"tier": "read", "approval": False},
        "stats":        {"tier": "read", "approval": False},
        "extract":      {"tier": "read", "approval": False},
        "extract_text": {"tier": "read", "approval": False},
        "extract_audio": {"tier": "read", "approval": False},
        "extract_frame": {"tier": "read", "approval": False},
        "summarize":    {"tier": "read", "approval": False},
        "analyze":      {"tier": "read", "approval": False},
        "word_count":   {"tier": "read", "approval": False},
        "validate":     {"tier": "read", "approval": False},
        "to_word":      {"tier": "act",  "approval": False},
        "to_csv":       {"tier": "act",  "approval": False},
        "convert":      {"tier": "act",  "approval": False},
        "compress":     {"tier": "act",  "approval": False},
        "resize":       {"tier": "act",  "approval": False},
        "trim":         {"tier": "act",  "approval": False},
        "format":       {"tier": "act",  "approval": False},
        "run":          {"tier": "act",  "approval": True},
    },
    "desktop_control": {
        "list":              {"tier": "read", "approval": False},
        "stats":             {"tier": "read", "approval": False},
        "current_wallpaper": {"tier": "read", "approval": False},
        "wallpaper_url":     {"tier": "read", "approval": False},
        "wallpaper":         {"tier": "act",  "approval": False},
        "task":              {"tier": "act",  "approval": True},
        "organize":          {"tier": "act",  "approval": True},
        "clean":             {"tier": "act",  "approval": True},
    },
    "browser_control": {
        "get_text":       {"tier": "read", "approval": False},
        "get_url":        {"tier": "read", "approval": False},
        "list_browsers":  {"tier": "read", "approval": False},
        "new_tab":        {"tier": "act",  "approval": False},
        "go_to":          {"tier": "act",  "approval": False},
        "forward":        {"tier": "act",  "approval": False},
        "back":           {"tier": "act",  "approval": False},
        "close_tab":      {"tier": "act",  "approval": True},
        "close_all":      {"tier": "act",  "approval": True},
        "close":          {"tier": "act",  "approval": True},
        "click":          {"tier": "act",  "approval": True},
        "press":          {"tier": "act",  "approval": True},
        "fill_form":      {"tier": "act",  "approval": True},
    },
    "computer_control": {
        "focus_window": {"tier": "read", "approval": False},
        "copy":         {"tier": "read", "approval": False},
        "right_click":  {"tier": "read", "approval": False},
        "move":         {"tier": "act",  "approval": False},
        "double_click": {"tier": "act",  "approval": False},
        "screen_click": {"tier": "act",  "approval": True},
        "press":        {"tier": "act",  "approval": True},
        "hotkey":       {"tier": "act",  "approval": True},
        "paste":        {"tier": "act",  "approval": True},
        "clear_field":  {"tier": "act",  "approval": True},
        "drag":         {"tier": "act",  "approval": True},
    },
    "computer_settings": {
        "scroll_up":    {"tier": "act", "approval": False},
        "scroll_down":  {"tier": "act", "approval": False},
        "volume_set":   {"tier": "act", "approval": True},
        "dark_mode":    {"tier": "act", "approval": True},
        "press_key":    {"tier": "act",  "approval": True},
    },
    "play_music": {
        "play":    {"tier": "act", "approval": False},
        "pause":   {"tier": "act", "approval": False},
        "resume":  {"tier": "act", "approval": False},
        "next":    {"tier": "act", "approval": False},
        "prev":    {"tier": "act", "approval": False},
        "previous": {"tier": "act", "approval": False},
        "stop":    {"tier": "act", "approval": False},
        "volume":  {"tier": "act", "approval": False},
    },
    "file_controller": {
        "list":         {"tier": "read", "approval": False},
        "read":         {"tier": "read", "approval": False},
        "search":       {"tier": "read", "approval": False},
        "create_file":  {"tier": "act",  "approval": False},
        "create_folder": {"tier": "act", "approval": False},
        "write":        {"tier": "act",  "approval": True},
        "rename":       {"tier": "act",  "approval": True},
        "move":         {"tier": "act",  "approval": True},
        "delete":       {"tier": "delete", "approval": True},
    },
    "code_helper": {
        "explain":       {"tier": "read", "approval": False},
        "optimize":      {"tier": "read", "approval": False},
        "screen_debug":  {"tier": "read", "approval": False},
        "write":         {"tier": "act",  "approval": True},
        "edit":          {"tier": "act",  "approval": True},
        "build":         {"tier": "act",  "approval": True},
        "run":           {"tier": "act",  "approval": True},
        "auto":          {"tier": "act",  "approval": True},
    },
    "dev_agent":       {"tier": "act",   "approval": True},
    "open_app":        {"tier": "act",   "approval": False},
    "reminder":        {"tier": "act",   "approval": False},
    # it reaches another person, so it always asks no matter how it is phrased
    "send_message":    {"tier": "spend", "approval": True},
    "youtube_video": {
        "search": {"tier": "read", "approval": False},
        "transcript": {"tier": "read", "approval": False},
        "play":   {"tier": "act",  "approval": False},
        "save":   {"tier": "act",  "approval": True},
        "download": {"tier": "act", "approval": True},
    },
    # Per action, so a read-only plugin stays free and only its consequential
    # actions ask. That is the whole difference between "approve everything"
    # and "approve the thing that actually does something".
    "recall": {
        "search": {"tier": "read", "approval": False},
        "count":  {"tier": "read", "approval": False},
        "forget": {"tier": "delete", "approval": True},   # it destroys history
    },
    "day_glance": {
        "glance": {"tier": "read", "approval": False},
        "list":   {"tier": "read", "approval": False},
        "add":    {"tier": "act",  "approval": True},
        "done":   {"tier": "act",  "approval": True},
        "focus":  {"tier": "act",  "approval": True},
    },
    "exam_cram": {
        "plan":     {"tier": "read", "approval": False},
        "drill":    {"tier": "read", "approval": False},
        "mock":     {"tier": "read", "approval": False},
        "progress": {"tier": "read", "approval": False},
        "reset":    {"tier": "delete", "approval": True},
    },
    "summarise": {
        "page": {"tier": "read", "approval": False},
        "file": {"tier": "read", "approval": False},
        "key":  {"tier": "read", "approval": False},
    },
    "health_svc": {
        "status":  {"tier": "read", "approval": False},
        "all":     {"tier": "read", "approval": False},
        "jobs":    {"tier": "read", "approval": False},
        "deploy":  {"tier": "read", "approval": False},
        "disk":    {"tier": "read", "approval": False},
        "plugins": {"tier": "read", "approval": False},
    },
    "quiz_me": {
        "start":  {"tier": "read", "approval": False},
        "ask":    {"tier": "read", "approval": False},
        "answer": {"tier": "read", "approval": False},
        "score":  {"tier": "read", "approval": False},
        "stop":   {"tier": "read", "approval": False},
    },
    "delegate_agent": {
        "run":   {"tier": "spend", "approval": True,
                  "what": "an agent runs code on your machine"},
        "check": {"tier": "read",  "approval": False},
    },
    "scene": {
        "list":   {"tier": "read", "approval": False},
        "run":    {"tier": "act",  "approval": True},
        "save":   {"tier": "act",  "approval": True},
        "delete": {"tier": "delete", "approval": True},
    },
    "welcome": {
        # reading and speaking to the user is not an outward-facing act
        "status":  {"tier": "read", "approval": False},
        "lines":   {"tier": "read", "approval": False},
        "weather": {"tier": "read", "approval": False},
        "faults":  {"tier": "read", "approval": False},
        # it opens pages on our own browser, so it asks like navigation does
        "run":     {"tier": "act",  "approval": True},
    },
    "computer": {
        # looking costs nothing: these never move the pointer or send anything
        "status":     {"tier": "read", "approval": False},
        "read":       {"tier": "read", "approval": False},
        "screenshot": {"tier": "read", "approval": False},
        "steps":      {"tier": "read", "approval": False},
        "secrets":    {"tier": "read", "approval": False},
        # start/stop are cheap and reversible, and asking would teach the user
        # to click through a prompt forever
        "start":      {"tier": "act",  "approval": False},
        "stop":       {"tier": "act",  "approval": False},
        # everything that can move the cursor, type, or navigate away. A click
        # can send a message, so it asks like one.
        "go":         {"tier": "act",  "approval": True},
        "click":      {"tier": "act",  "approval": True},
        "fill":       {"tier": "act",  "approval": True},
        "type":       {"tier": "act",  "approval": True},
        "press":      {"tier": "act",  "approval": True},
        "scroll":     {"tier": "act",  "approval": False},
        "back":       {"tier": "act",  "approval": True},
        "run_js":     {"tier": "act",  "approval": True},
        # hand-over only ever REDUCES what a bot may do, so it must never be
        # the thing that needs permission
        "handover":   {"tier": "act",  "approval": False},
    },
    "coder": {
        # reading the workspace changes nothing
        "ls":     {"tier": "read", "approval": False},
        "read":   {"tier": "read", "approval": False},
        "grep":   {"tier": "read", "approval": False},
        "status": {"tier": "read", "approval": False},
        # an agent that writes code and runs commands is not a small thing
        "go":     {"tier": "act",  "approval": True},
        "undo":   {"tier": "act",  "approval": True},
    },
    "github": {
        "repos":   {"tier": "read", "approval": False},
        "list":    {"tier": "read", "approval": False},
        "me":      {"tier": "read", "approval": False},
        "whoami":  {"tier": "read", "approval": False},
        "prs":     {"tier": "read", "approval": False},
        "issues":  {"tier": "read", "approval": False},
        "ci":      {"tier": "read", "approval": False},
        "runs":    {"tier": "read", "approval": False},
        "failing": {"tier": "read", "approval": False},
        "log":     {"tier": "read", "approval": False},
        # writing to a repository is a public act, always asks
        "comment": {"tier": "act",  "approval": True},
        "open":    {"tier": "act",  "approval": True},
        "create":  {"tier": "act",  "approval": True},
    },
    "vercel": {
        "projects": {"tier": "read", "approval": False},
        "list":     {"tier": "read", "approval": False},
        "me":       {"tier": "read", "approval": False},
        "deploys":  {"tier": "read", "approval": False},
        "broken":   {"tier": "read", "approval": False},
        "status":   {"tier": "read", "approval": False},
        # this is what the public is being served — never automatic
        "redeploy": {"tier": "act",  "approval": True},
        "retry":    {"tier": "act",  "approval": True},
        "rollback": {"tier": "act",  "approval": True},
    },
    "hf": {
        "models":   {"tier": "read", "approval": False},
        "search":   {"tier": "read", "approval": False},
        "model":    {"tier": "read", "approval": False},
        "info":     {"tier": "read", "approval": False},
        "spaces":   {"tier": "read", "approval": False},
        "datasets": {"tier": "read", "approval": False},
        "me":       {"tier": "read", "approval": False},
        "restart":  {"tier": "act",  "approval": True},
    },
    "email": {
        # reading a mailbox changes nothing, so it is free
        "inbox":         {"tier": "read",  "approval": False},
        "list":          {"tier": "read",  "approval": False},
        "read":          {"tier": "read",  "approval": False},
        "unread":        {"tier": "read",  "approval": False},
        "sent":          {"tier": "read",  "approval": False},
        "status":        {"tier": "read",  "approval": False},
        "budget":        {"tier": "read",  "approval": False},
        "replies":       {"tier": "read",  "approval": False},
        "draft":         {"tier": "act",   "approval": False},
        "compose":       {"tier": "act",   "approval": False},
        "write":         {"tier": "act",   "approval": False},
        # these commit the user to a stranger, so they always ask
        "send":          {"tier": "spend", "approval": True},
        "send_draft":    {"tier": "spend", "approval": True},
        # a credential is a door key, whatever tier it is filed under
        "configure":     {"tier": "act",   "approval": True},
        "set":           {"tier": "act",   "approval": True},
    },
    "knowledge": {
        "add":      {"tier": "act", "approval": False},
        "add_url":  {"tier": "act", "approval": False},
        "search":   {"tier": "read", "approval": False},
        "list":     {"tier": "read", "approval": False},
        "recent":   {"tier": "read", "approval": False},
        "stats":    {"tier": "read", "approval": False},
        "forget":   {"tier": "delete", "approval": True},
    },
    "leads": {
        "discover":  {"tier": "act", "approval": False},
        "score":     {"tier": "act", "approval": False},
        "enrich":    {"tier": "act", "approval": False},
        "draft":     {"tier": "act", "approval": False},
        "ignore":    {"tier": "delete", "approval": True},
    },
    "agents": {
        "org":         {"tier": "read",   "approval": False},
        "roster":      {"tier": "read",   "approval": False},
        "describe":    {"tier": "read",   "approval": False},
        "hire":        {"tier": "act",    "approval": True,
                        "what": "hire or change an agent, including its budget"},
        "hire_no_budget": {"tier": "act", "approval": False,
                        "what": "hire or change an agent with no budget"},
        "retire":      {"tier": "act",    "approval": False},
        "delete":      {"tier": "delete", "approval": True,
                        "what": "fire and erase an agent"},
    },
    "brief": {
        "create":      {"tier": "act",    "approval": True,
                        "what": "send work to an agent (costs their budget)"},
        "list":        {"tier": "read",   "approval": False},
    },
    "journal": {
        "add":     {"tier": "act",    "approval": False},
        "search":  {"tier": "read",   "approval": False},
        "list":    {"tier": "read",   "approval": False},
        "decide":  {"tier": "act",    "approval": False},
        "forget":  {"tier": "delete", "approval": True},
    },
    "calendar": {
        "list":    {"tier": "read",   "approval": False},
        "agenda":  {"tier": "read",   "approval": False},
        "add":     {"tier": "act",    "approval": False},
        "delete":  {"tier": "delete", "approval": True},
    },
    "files": {
        "list":    {"tier": "read",   "approval": False},
        "read":    {"tier": "read",   "approval": False},
        "upload":  {"tier": "act",    "approval": True,
                    "what": "store a document"},
        "attach":  {"tier": "act",    "approval": False},
        "delete":  {"tier": "delete", "approval": True},
    },
    "home": {
        "list":    {"tier": "read",   "approval": False},
        "device":  {"tier": "act",    "approval": True,
                    "what": "register a device or scene"},
        "scene":   {"tier": "act",    "approval": True},
        "run":     {"tier": "act",    "approval": False,
                    "what": "turn something on or off (security is gated separately)"},
        "run_scene": {"tier": "act",  "approval": False},
    },
    "browser": {
        "status":  {"tier": "read",   "approval": False},
        "open":    {"tier": "read",   "approval": False,
                    "what": "open a page on the allowlist"},
        "click":   {"tier": "act",    "approval": False},
        "allow":   {"tier": "act",    "approval": True,
                    "what": "widen the browser allowlist"},
        "deny":    {"tier": "act",    "approval": False},
    },
    "proactive": {
        "status":  {"tier": "read",   "approval": False},
        "briefing": {"tier": "read",  "approval": False},
        "check":   {"tier": "read",   "approval": False},
        "watch":   {"tier": "act",    "approval": False},
        "unwatch": {"tier": "delete", "approval": True},
    },
}

DEFAULT_TIER = "spend"          # fail closed for unknown tools
UNKNOWN_NEEDS_APPROVAL = True

_lock = threading.RLock()
_overrides: Optional[dict] = None          # per-tool tier/approval from the UI
_spend: list[dict] = []                    # today's spend events
_day: str = ""
_rates: dict[str, list[float]] = {}


# ── config ───────────────────────────────────────────────────────────────────

def _cfg_path() -> Path:
    return data_root() / "policy.json"


def _default_cfg() -> dict:
    return {
        # $0 a day until someone raises it. An AI that can spend is not
        # autonomous, it is a liability with good manners.
        "daily_spend_cap_usd": 0.0,
        "per_call_cap_usd": 25.0,
        "rate_limit_per_min": 30,
        "auto_approve_read": True,
        "auto_approve_act": True,
        "require_approval_spend": True,
        "require_approval_delete": True,
        "tiers": {},          # per-tool overrides, filled by the dashboard
        "actions": {},        # per-tool per-action overrides
    }


def config() -> dict:
    global _overrides
    with _lock:
        if _overrides is None:
            cfg = _default_cfg()
            try:
                p = _cfg_path()
                if p.exists():
                    loaded = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        cfg.update(loaded)
            except Exception:
                pass
            _overrides = cfg
        return dict(_overrides)


def save_config(patch: dict) -> dict:
    """Merge a settings patch. Unknown keys are ignored, numbers are coerced,
    and a cap can never be set negative — a negative cap is a bug, not a policy."""
    global _overrides
    with _lock:
        cfg = config()
        for k, v in (patch or {}).items():
            if k not in cfg:
                continue
            if isinstance(cfg[k], bool):
                cfg[k] = bool(v)
            elif isinstance(cfg[k], (int, float)):
                try:
                    cfg[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
            elif isinstance(cfg[k], dict):
                if isinstance(v, dict):
                    merged = dict(cfg[k])
                    merged.update({str(a): b for a, b in v.items()})
                    cfg[k] = merged
        _cfg_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        _overrides = cfg
        return dict(cfg)


def reset() -> None:
    """Test/deploy hook — drop the cached config so it is re-read."""
    global _overrides, _spend, _day, _rates
    with _lock:
        _overrides = None
        _spend = []
        _day = ""
        _rates = {}


# ── the decision ─────────────────────────────────────────────────────────────

class Decision:
    __slots__ = ("tool", "tier", "allowed", "needs_approval", "reason", "cost")

    def __init__(self, tool: str, tier: str, allowed: bool,
                 needs_approval: bool, reason: str = "", cost: float = 0.0):
        self.tool = tool
        self.tier = tier
        self.allowed = allowed
        self.needs_approval = needs_approval
        self.reason = reason
        self.cost = cost

    def as_dict(self) -> dict:
        return {"tool": self.tool, "tier": self.tier, "allowed": self.allowed,
                "needs_approval": self.needs_approval, "reason": self.reason,
                "cost": self.cost}

    def __repr__(self) -> str:
        return (f"Decision({self.tool!r} tier={self.tier} "
                f"allowed={self.allowed} approval={self.needs_approval})")


def _mcp_spec(tool: str) -> dict | None:
    """MCP tools are third-party code, so they do not inherit the built-in
    "reading is free" rule. Every call asks, unless the user has explicitly
    marked that one server trusted. This is the only place in the policy that
    keys off a tool NAME rather than a registered tool, because MCP tools do
    not exist until a server is connected."""
    if not str(tool or "").startswith("mcp__"):
        return None
    parts = str(tool).split("__")
    server = parts[1] if len(parts) > 1 else ""
    trusted = False
    try:
        from core import mcp as _mcp
        spec_row = _mcp.get_server(server) or {}
        trusted = bool(spec_row.get("trusted"))
    except Exception:
        trusted = False
    return {"tier": "act",
            "approval": not trusted,
            "cap": None,
            "what": f"the MCP server '{server}'"
                   + ("" if trusted else " (untrusted — asks every time)"),
            "_explicit": True}


def spec(tool: str, action: str = "") -> dict:
    """Effective spec: tool default, then the action's own rule, then whatever
    the dashboard overrode, then fail-closed for anything unrecognised.

    Precedence is deliberate: an explicit dashboard override beats a shipped
    default, so the user is always the final authority.
    """
    dyn = _mcp_spec(tool)
    if dyn is not None:
        return dyn
    cfg = config()
    base = dict(TOOLS.get(tool) or {})
    if not base:
        base = {"tier": DEFAULT_TIER, "approval": UNKNOWN_NEEDS_APPROVAL,
                "cap": None,
                "what": "an unrecognised tool — treated as the strict case"}
    act = str(action or "").strip().lower()
    # _explicit means "somebody actually said so" — a shipped per-action rule
    # or a dashboard override. Without it, the tier defaults below would
    # re-impose approval on a rule that deliberately says otherwise, and the
    # dashboard could never relax anything.
    _explicit = False
    shipped = (ACTIONS.get(tool) or {}).get(act)
    if shipped:
        base.update(shipped)
        base["action"] = act
        _explicit = True
    cfg_actions = (cfg.get("actions") or {}).get(tool) or {}
    over = dict((cfg.get("tiers") or {}).get(tool) or {})
    over.update(cfg_actions.get(act) or {})
    if over.get("tier") in TIERS:
        base["tier"] = over["tier"]
    if "approval" in over:
        base["approval"] = bool(over["approval"])
        _explicit = True
    if "what" in over:
        base["what"] = str(over["what"])[:120]
    base["_explicit"] = _explicit
    return base


def check(tool: str, args: Optional[dict] = None, *, actor: str = "user") -> Decision:
    """May this run? Pure — does not gate, does not log. `gate()` does both."""
    cfg = config()
    action = ""
    if isinstance(args, dict):
        action = str(args.get("action") or "").strip().lower()
    sp = spec(tool, action)
    tier = sp["tier"]

    if tier not in TIER_RANK:
        tier = DEFAULT_TIER

    # rate limit: per tool, sliding minute
    if not _rate_ok(f"{actor}:{tool}", float(cfg.get("rate_limit_per_min") or 30)):
        return Decision(tool, tier, False, False,
                        f"rate limited: more than {cfg.get('rate_limit_per_min')} "
                        f"'{tool}' calls a minute")

    # per-call spend ceiling, checked BEFORE the call, not after
    cost = float(COSTS.get(tool) or 0.0)
    call_cap = float(cfg.get("per_call_cap_usd") or 0.0)
    if cost and call_cap and cost > call_cap:
        return Decision(tool, tier, False, False,
                        f"this call would cost ${cost:.2f}, over the "
                        f"${call_cap:.2f} per-call cap", cost)

    needs = bool(sp.get("approval"))
    if not needs and not sp.get("_explicit"):
        if tier == "read":
            needs = not bool(cfg.get("auto_approve_read"))
        elif tier == "act":
            needs = not bool(cfg.get("auto_approve_act"))
        elif tier == "spend":
            needs = bool(cfg.get("require_approval_spend"))
        elif tier == "delete":
            needs = bool(cfg.get("require_approval_delete"))
    return Decision(tool, tier, True, needs, sp.get("what", ""), cost)


def gate(tool: str, args: Optional[dict] = None, *, actor: str = "user",
         target: str = "") -> Decision:
    """`check` + an audit row. Returns the decision; the caller gates on it."""
    d = check(tool, args, actor=actor)
    audit(tool, d.tier, actor=actor, target=target or _target_of(args or {}),
          result="blocked" if not d.allowed else
                 ("needs_approval" if d.needs_approval else "allowed"),
          detail=d.reason)
    return d


def _rate_ok(key: str, per_min: float) -> bool:
    if per_min <= 0:
        return True
    now = time.time()
    with _lock:
        hits = [t for t in _rates.get(key, []) if now - t < 60]
        if len(hits) >= per_min:
            _rates[key] = hits
            return False
        hits.append(now)
        _rates[key] = hits
    return True


def _target_of(args: dict) -> str:
    """A short, human-readable target for the audit row and the approval card."""
    for key in ("repo", "device", "target", "url", "query", "place", "to",
                "path", "name", "key", "message", "id"):
        val = args.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val)[:120]
    return ""


# ── spend ────────────────────────────────────────────────────────────────────

def _today() -> str:
    return time.strftime("%Y-%m-%d")


def spent_today() -> float:
    global _day
    with _lock:
        if _day != _today():
            return 0.0
        return round(sum(float(e.get("amount") or 0.0) for e in _spend), 4)


def budget() -> dict:
    cfg = config()
    cap = float(cfg.get("daily_spend_cap_usd") or 0.0)
    spent = spent_today()
    return {"day": _today(), "spent_usd": spent, "cap_usd": cap,
            "remaining_usd": max(0.0, round(cap - spent, 4)),
            "exhausted": cap > 0 and spent >= cap}


def may_spend(amount: float, *, what: str = "", actor: str = "user") -> tuple[bool, str]:
    """Check the daily budget. Returns (allowed, reason)."""
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, "could not read the amount"
    if amount <= 0:
        return True, ""
    b = budget()
    if b["cap_usd"] <= 0:
        return False, (f"the daily spend cap is $0 — raise it in the dashboard "
                       f"before anything can cost money")
    if b["spent_usd"] + amount > b["cap_usd"]:
        return False, (f"${amount:.2f} would take the day to "
                       f"${b['spent_usd'] + amount:.2f}, over the "
                       f"${b['cap_usd']:.2f} cap")
    return True, ""


def record_spend(amount: float, *, what: str = "", actor: str = "user",
                 target: str = "") -> dict:
    """Add to today's spend. Call this when money actually leaves, not before."""
    global _day, _spend
    try:
        amount = round(float(amount), 4)
    except (TypeError, ValueError):
        amount = 0.0
    with _lock:
        if _day != _today():
            _day = _today()
            _spend = []
        entry = {"at": time.time(), "amount": amount, "what": str(what)[:200],
                 "actor": actor, "target": str(target)[:120]}
        _spend.append(entry)
    audit("spend", "spend", actor=actor, target=target or str(what)[:120],
          result=f"${amount:.4f}", detail="recorded spend")
    return entry


# ── audit log ────────────────────────────────────────────────────────────────

AUDIT_KEEP = 2000


def _audit_path() -> Path:
    return data_root() / "audit.jsonl"


def audit(action: str, tier: str = "", *, actor: str = "user", target: str = "",
          result: str = "", detail: str = "", cost: float = 0.0) -> dict:
    """Append one row. Append-only on purpose: an audit log you can rewrite is
    not an audit log. Rotation keeps it bounded on a small volume."""
    row = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "action": str(action)[:80], "tier": str(tier)[:16],
           "actor": str(actor)[:40], "target": str(target)[:160],
           "result": str(result)[:120], "detail": str(detail)[:300],
           "cost": round(float(cost or 0.0), 4)}
    try:
        p = _audit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass       # never let logging break the action it is logging
    return row


def audit_tail(limit: int = 100, *, action: str = "", tier: str = "",
               actor: str = "") -> list[dict]:
    """Most recent first. Reads the tail only — this file can be large."""
    p = _audit_path()
    if not p.exists():
        return []
    want = max(1, min(int(limit or 100), AUDIT_KEEP))
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if action and row.get("action") != action:
            continue
        if tier and row.get("tier") != tier:
            continue
        if actor and row.get("actor") != actor:
            continue
        out.append(row)
        if len(out) >= want:
            break
    return out


def audit_stats() -> dict:
    """What the Approvals/Audit panel shows at a glance."""
    rows = audit_tail(500)
    by_tier: dict[str, int] = {}
    blocked = 0
    spend = 0.0
    for r in rows:
        by_tier[r.get("tier") or "?"] = by_tier.get(r.get("tier") or "?", 0) + 1
        if r.get("result") == "blocked":
            blocked += 1
        spend += float(r.get("cost") or 0.0)
    return {"rows_sampled": len(rows), "by_tier": by_tier, "blocked": blocked,
            "spend_in_sample": round(spend, 4), "budget": budget()}


# ── the catalogue the dashboard renders ──────────────────────────────────────

def catalogue(tool: str = "") -> list[dict]:
    """Tools and their effective tiers, for the config console.

    With `tool`, the rows are that tool's ACTIONS — which is the only way to
    read "sending an invoice needs approval, listing them does not" in the UI.
    """
    if tool:
        rows = []
        for act in sorted(set(list((ACTIONS.get(tool) or {}).keys())
                              + list(((config().get("actions") or {})
                                     .get(tool) or {}).keys()))):
            sp = spec(tool, act)
            rows.append({"tool": tool, "action": act, "tier": sp["tier"],
                         "approval": bool(sp["approval"]),
                         "what": sp.get("what", "")})
        rows.sort(key=lambda r: (TIER_RANK.get(r["tier"], 9), r["action"]))
        return rows
    names = sorted(set(list(TOOLS.keys())))
    out = []
    for n in names:
        sp = spec(n)
        out.append({"tool": n, "tier": sp["tier"], "approval": bool(sp["approval"]),
                    "what": sp.get("what", ""), "cost": COSTS.get(n, 0.0),
                    "known": n in TOOLS,
                    "actions": sorted((ACTIONS.get(n) or {}).keys())})
    out.sort(key=lambda r: (TIER_RANK.get(r["tier"], 9), r["tool"]))
    return out


def describe() -> str:
    b = budget()
    cfg = config()
    return (f"policy: ${b['spent_usd']:.2f} of ${b['cap_usd']:.2f} today, "
            f"{len(TOOLS)} tools registered, "
            f"{sum(1 for name in TOOLS if spec(name)['approval'])} need approval")
