# Architecture

Four layers, and the order matters more than the contents.

```
browser / phone
      │  WebSocket, Bearer token
      ▼
DashboardServer ───────────── FastAPI, /api/*, /ws/*
      │  bind_live()
      ▼
JarvisLive ────────── the assistant: Gemini Live duplex audio
      │
      ├─ policy.py ──── every tool call passes through here
      ├─ confirm.py ─── human-in-the-loop, the only resolve() path
      ├─ computer.py ── shared Chromium, its own event loop, secret vault
      ├─ crew.py ────── named bots, transcripts on the volume
      ├─ agent_runtime ─ Antigravity: the agent work is delegated to
      ├─ plugin_loader ─ hot-loadable capabilities
      └─ action_loader ─ legacy file-backed tools
```

## Why the policy gate is first

`core/policy.py` is consulted before any tool executes. Three tiers — `read`,
`act`, `spend` — plus a per-action table, so reading your own history is free
while ticking off a todo asks.

**Anything unregistered falls through to `spend`.** That is deliberate and it is
the reason every plugin is registered: a tool that asks about everything,
including harmless reads, trains you to approve without reading.

## Why the browser has its own event loop

A cold Chromium start is seconds. Called inline it freezes the voice socket,
the panels and every other request for exactly as long. `core/computer.py`
therefore owns a dedicated asyncio loop and is only ever called through
`asyncio.to_thread`.

This cost an afternoon once: the dashboard heartbeat went from 3,119 ms to
118 ms when it was fixed.

## Why secrets are not strings

A tool asks for `secret:NAME`. `core/computer.py` resolves it from an in-memory
vault, uses it, and drops it. The value never enters a prompt, a transcript, a
log line or a screenshot. Unknown names fail closed rather than being passed
through as literal text — otherwise `secret:hunter2` would quietly become the
string "secret:hunter2".

## Why plugins are validated out of process

Installing a plugin is remote code execution by design. It is validated in a
subprocess before being written, so a plugin that hangs or explodes on import
takes nothing down with it. Repository plugins win name collisions, so a
runtime plugin cannot shadow reviewed code.

## State

Everything durable is under the data root — on a Space, a persistent volume:

| | |
|---|---|
| `bot_chats/*.json` | transcripts, one file per bot. What `recall` searches. |
| `settings.json` | what a person changed. Env vars override it. |
| `ceremony/track.txt` | which welcome sting you picked |
| `knowledge.sqlite` | the knowledge base |
| `user_plugins/` | plugins you installed at runtime |
