# JARVIS

A self-hosted AI assistant with a voice, a memory, and a browser. It runs on a
Hugging Face Space, keeps its state on a persistent volume, and answers in your
dashboard, on your phone, and through the clapping you have been told twice is
a feature.

[![Space](https://img.shields.io/badge/Space-live-5b3d9f)](https://abc1181-jaarvis.hf.space)
[![Python](https://img.shields.io/badge/python-3.11-3776ab?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT%20%2B%20non--commercial-brightgreen)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1000%20passing-4c1)](e2e/)
[![Tools](https://img.shields.io/badge/tools-68-blueviolet)](main.py)

> **Personal, non-commercial use.** The MIT licence covers our code. The vendored
> God's Eye View tree bundles one **CC BY-NC-SA** dataset, which makes the
> repository as distributed non-commercial. See [LICENSE](LICENSE). If you want
> to use this commercially, delete that dataset or drop `godseye/` first.

---

## What it actually does

- **Talks and listens.** Wake word ("Hey Jarvis") or push-to-talk, streamed both
  ways. Clap twice and it plays a sting and greets you.
- **Remembers.** Every turn is written to a transcript you can search later.
  Memory is not a summary of a summary — it is what was actually said.
- **Drives a real browser.** A shared Chromium it navigates, clicks and types
  in, with live screenshots and a takeover button for when it meets a login.
- **Delegates.** Open-ended work goes to an autonomous agent that plans, runs
  code and spawns its own subagents. Single-step work stays a single tool call.
- **Asks before it acts.** Read-only is free. Anything that writes, spends,
  sends or deletes asks first — every time.

| | |
|---|---|
| **Live** | [abc1181-jaarvis.hf.space](https://abc1181-jaarvis.hf.space) |
| **Stack** | Python 3.11 · FastAPI · Playwright · Gemini Live |
| **Licence** | MIT for our code; non-commercial overall (see above) |
| **Tests** | 1,000+ assertions across 13 e2e suites, run in CI |

---

## Quick start

```bash
git clone https://github.com/FatihMakes/Mark-LIV.git
cd Mark-LIV
pip install -r requirements.txt          # desktop
pip install -r requirements-server.txt   # headless / Space
python main.py                           # desktop UI
python main.py --server                  # headless dashboard
```

Then open the printed dashboard URL and set a password. A Space is the same
image with `SERVER_MODE=1` — see [docs/deployment.md](docs/deployment.md).

**Configuration** is one file, `config/api_keys.json`, created on first run and
never committed. Every key is documented in
[docs/configuration.md](docs/configuration.md).

---

## Architecture

```
browser ─┬─ WebSocket (auth) ─┬─ DashboardServer ─┬─ FastAPI + WebSocket
phone   ─┘                   │                    ├─ /api/*  (token-gated)
                            │                    └─ /api/health (public liveness)
                            └─ JarvisLive ──── Gemini Live (voice, duplex)
                                   │
                                   ├── policy.py ──── ask / allow-once / always
                                   ├── confirm.py ─── human-in-the-loop gate
                                   ├── computer.py ── shared Chromium + secrets vault
                                   ├── crew.py ────── named bots, transcripts
                                   ├── plugins/ ───── hot-loadable capabilities
                                   └── actions/ ───── legacy file-backed tools
```

Four ideas hold it together:

1. **The policy gate is upstream of everything.** No tool runs without passing
   through `core/policy.py`. Read-only is free; anything consequential asks.
2. **Secrets are never strings in a transcript.** A tool asks for
   `secret:NAME`; the value is held in memory, never logged, never sent to the
   model, never in a screenshot.
3. **A bad plugin cannot take the app down.** Every plugin is validated in a
   subprocess before it is saved, and a plugin that fails to load is reported
   and skipped rather than imported.
4. **Silence is a bug.** Every failure path returns a reason. A tool that
   quietly does nothing is worse than one that says "no key configured",
   because silence reads as "nothing to say".

---

## Documentation

| | |
|---|---|
| [Architecture](docs/architecture.md) | how the pieces fit, and why |
| [Configuration](docs/configuration.md) | every environment variable and setting |
| [Plugins](docs/plugins.md) | writing, installing and shipping capabilities |
| [Tools & approvals](docs/tools.md) | the tool list and the permission model |
| [Deployment](docs/deployment.md) | Space, Docker, and the Windows client |
| [Contributing](CONTRIBUTING.md) | how to work on this |
| [Security](SECURITY.md) | reporting a vulnerability |
| [Credits](CREDITS.md) | every dependency and its licence |

---

## Status

Honest, because a project that overstates itself is not worth trusting:

- **Works:** voice, memory, tools, plugins, browser control, approvals, ceremony.
- **In progress:** the Windows client that pairs with this Space, and the
  Antigravity agent runtime behind delegation.
- **Not done:** per-bot screens (there is one shared browser), per-click
  approvals (approval is per turn), and offline model support.
- **Known rough edges** are tracked in [docs/known-issues.md](docs/known-issues.md).

---

## Credits

Built on the shoulders of several projects, each credited with its licence in
[CREDITS.md](CREDITS.md). No code was copied from any unlicensed repository.

MIT © 2026 Arjav Jain