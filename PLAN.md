# JARVIS Capability Expansion — Plan

**Goal:** turn JARVIS from a voice assistant into an autonomous operator — an **AI company in a box**: controls your devices, builds real apps and projects, delegates work to a fleet of sub-agents (code, ads, ideas, copy, research, leads), and runs your freelance business — starting with **finding leads on its own** — with a confirm-gate on anything risky. **Everything is configurable and monitorable from a powerful dashboard.**

---

## 0. Current state (ground truth)

| Layer | Reality today |
|---|---|
| Brain | HF Space (cpu-basic): Gemini Live voice + OpenAI-gateway fallback, ≤8-round tool loop, ~28 tools |
| Tools | 20 actions + 8 inline; 3 HF-only plugins (`github_ops`→`gh`, `discord_ops`→REST, `homelab`→docker/ping — all execute **on the Space**) |
| Connectivity | **Inbound only** — browsers/phones connect in. Nothing reaches your PC/NAS/VPS. No tunnel, no daemon |
| Exec | `dev_agent`/`code_helper` subprocess on the machine they run on (= the Space, weak) |
| Scheduling | OS-native (cron/schtasks) + 4 hardcoded asyncio loops; nothing model-callable, nothing persisted across restarts |
| State | `long_term.json` memory, no RAG, no transcript persistence, no job/task store |
| Safety | `/api/confirm` flow exists (underused), device tokens exist but in RAM |

**Two blockers discovered:**
1. **Repo divergence** — HF has 3 plugins local git never had; local has months of uncommitted changes HF doesn't. Must sync before big work.
2. **The Space cannot reach your devices** — the entire device-control vision hinges on a new **outbound agent daemon**.

---

## 1. Architecture: Brain + Limbs

```
┌─ HF Space (brain) ─────────────────────────────┐
│ Gemini Live voice · dashboard · approvals      │
│ task store · scheduler · lead engine · CRM     │
│ cloud connectors (email/calendar/payments)     │
│ sub-agent fleet (coder · scout · copy · ads)   │
└──────┬─────────────────┬─────────────────┬─────┘
       │ /ws/agent (outbound WSS, device token)
┌──────┴──────┐   ┌──────┴──────┐   ┌──────┴──────┐
│ jarvisd@PC  │   │ jarvisd@NAS │   │ jarvisd@VPS │
│ opencode,   │   │ docker,     │   │ CI, bots,   │
│ screen/apps │   │ files, gh   │   │ schedulers  │
│ repos, gh   │   │ backups     │   │             │
└─────────────┘   └─────────────┘   └─────────────┘
   Phone = dashboard + push alerts + one-tap approvals
```

- **Space** = reasoning, orchestration, cloud APIs, state (on `/data` volume so restarts don't wipe it).
- **Limb (`jarvisd`)** = a small Python daemon per machine: dials **out** to the Space (no port-forwarding), auto-starts (schtasks/systemd), reconnects with backoff. Exposes: guarded shell, **`opencode run`**, screen/app control (reuses existing `computer_control`/`desktop`/pyautogui code), filesystem, docker, git/gh.
- Work runs where **you** point it — daemons are the sensible home for heavy jobs, the Space stays available for anything you choose to run there. (Fun fact: today's "RAM 94%" reading of the Space came from the broken hardware monitor itself — Phase 0 makes all these numbers real.)

---

## 2. Phases

### Phase 0 — Sync the repos + fix the hardware monitor ✅
- Pull the 3 HF-only plugins into local git, commit the entire uncommitted baseline, adopt the deploy script into the repo. **Single source of truth restored.** E2E 31/31 gate.
- **Fix the hardware monitor (wrong readings on the HF Space):** `/api/metrics` (`dashboard/server.py:1365`, HUD panel) and `actions/system_monitor.py` use raw psutil, which inside a container reports **host** values — RAM %, uptime, net, load are not the Space's own. Make it cgroup-aware: CPU from `/sys/fs/cgroup/cpu.stat` deltas, RAM from `memory.current`/`memory.max`, container uptime, disk via `shutil.disk_usage('/data')`, temps shown as N/A in a container; `jarvisd` devices (Phase 1) report true host metrics from their own machines instead.

### Phase 1 — Device connectivity: `jarvisd` + `/ws/agent` ✅
- **Server:** new WS endpoint (auth: extend existing pairing → per-device agent token), device registry (name/OS/caps/online), JSON request↔response protocol (`exec`, `run_opencode`, `screen.*`, `fs.*`, `status`), all audited.
- **Daemon:** ~400-line Python, capability modules = thin wrappers around existing action code (imports the same repo).
- **Policy from day 1:** shell commands run through an allowlist; destructive/network ops marked `risky` → routed to approvals (Phase 5) even before it's fully built in.
- **Dashboard:** **Devices panel** — online status, last seen, quick ping/shell/test.
- ✅ *Delivers: "control my devices while being in the server."*

### Phase 2 — Agentic coding + delegation ✅
- **`actions/code_task.py`** tool: `{prompt, repo, device?, model?}` → task record.
- **`core/tasks.py`** — persisted store (`/data/tasks.json` + per-task logs), states `queued→running→done/failed`, broadcast over the existing WS (new `task` message type) → **Tasks panel** (list, live log stream, diff preview, retry, approve-push).
- **Execution = your choice, not automatic tiers:** every task has a **where** — *my machine* (daemon runs `opencode run` in the real repo, worktree-isolated, streams output back, pushes/PRs only after approval) or *the Space* (existing `dev_agent` for small edits). You choose per task by voice ("on my PC") **and/or** set a default in dashboard settings; JARVIS never overrides it — at most it *suggests* ("this repo only exists on your PC").
- **Delegation primitive:** `delegate` tool — model drafts a plan of subtasks, spawns parallel workers (opencode sessions on daemons + gateway sub-agents for research/writing), monitors, aggregates, reports. Plus `task_status` / `cancel`.
- ✅ *Delivers: "agentic coding tasks itself, delegation, uses opencode."*

### Phase 3 — Scheduler, made persistent ✅
- **`core/scheduler.py`**: model-callable `manage_schedule` tool (cron/interval/one-shot, add/list/remove), persisted on `/data`, jobs inject into the existing command queue so scheduled work becomes a normal turn. Migrates the 4 hardcoded loops + `background_monitor` onto it.
- **Push alerts — shipped:** Web Push over a real PWA (manifest, service worker, generated icons). Alerts: "task finished", "needs approval". Approvals are **one-tap on the phone**: the notification carries Accept / Reject buttons, and the tap posts a short-lived HMAC-signed action token to `/api/push/respond`, which resolves the *same* `core/confirm.py` gate the dashboard card resolves. Voice, dashboard and phone are one gate — a command typed into the panel is no longer a way around it. (Telegram bot as a fallback channel: not needed yet.)

### Phase 4 — Lead engine: the company core ✅ (4a shipped) (3–5 days)
The one non-negotiable: **it finds leads itself.**

```
DISCOVER ──► QUALIFY ──► ENRICH ──► DRAFT ──► [APPROVE] ──► SEND ──► TRACK
   ▲                                                          │
   └──────────────── scheduled loop (every N hours) ◄─────────┘
```

- **Discover** (official APIs/RSS only — no ToS scraping): HN Algolia, Reddit JSON, GitHub search (`gh` — "looking for contributors / hiring" signals), job-board & marketplace RSS feeds, ProductHunt, IndieHackers, `web_search`/`web_fetch` for `"hiring {skill}"` queries. X API is paid — defer; LinkedIn has no legit API — never scrape.
- **Qualify:** LLM scores each hit (budget signal, fit vs. skills/memory, recency, contactability) → `lead` records in `/data/crm.sqlite`.
- **Enrich:** find public contact channel via official pages/APIs.
- **Draft:** proposal/DM/email from templates + rates/portfolio in memory → **waits for one-tap approval** (Phase 5).
- **Send:** Telegram (bot API, easiest) → email (Gmail OAuth or Resend/SendGrid, with warmup + compliance: consent, unsubscribe, caps).
- **Track:** kanban statuses (`new → shortlisted → contacted → replied → won/lost`), scheduled follow-ups, **daily pipeline section in the morning briefing**, **Leads panel** in the dashboard.
- ✅ *Demo moment: "JARVIS, find me 10 leads for Next.js work" → scored list + drafted outreach waiting on your phone.*

**4a — shipped:** `core/leads/` (SQLite CRM at `/data/crm.sqlite`, dedupe across crossposts via normalised URL hashes, per-source rate limits honoured from the store, a run budget, and partial-failure reporting so a dead endpoint is visible rather than silent). Sources are official APIs/feeds only: HN Algolia (`search_by_date` + the monthly *Who is hiring* thread), RemoteOK, We Work Remotely, Remotive, Arbeitnow (remote+tech only), Product Hunt (Atom), GitHub search. **Reddit is off by default** — it 403s cloud IPs, so it only works from a desktop runner or with a registered OAuth app. Discovery is a scheduler job (`lead discovery`, every 6h, phone-alerting when it finds something) plus a model-callable `leads` tool. Panel: the 📡 **Leads** inbox — run now, per-source health, offer editor (read from memory), filters, ignore.

**4b next:** LLM scoring + kanban. Then 4c enrich/draft (approval reuses the Phase 3 phone gate) and 4d Gmail: send over SMTP with an app password, IMAP reply-watch every 15 min, warmup + caps + opt-out, never auto-send.

### Phase 4e — The display surface: JARVIS can put anything on your screen (1 day)
**Source of the idea:** FatihMakes' video *"Just creating templates of some stuff with Jarvis"*. The video shows one instance — a photo of a sensor on the camera feed, and JARVIS answering with a **wiring-diagram template** (three component blocks, labelled pins, colour-coded power/ground/signal wires, plus a step-by-step connection guide it then narrates). The wiring diagram is the *example*, not the point. The point is the capability class: **ask for anything, and it appears on the screen** — a diagram, a dashboard, a mockup, a live chart, a page from the internet, a picture, a small tool. Like the film.

- **`core/display.py`** — artifact store. Kinds: `html` (model-written page), `url` (a real site, framed), `image`, `text`, `chart` (structured data → **we** draw the SVG). One JSON doc per artifact under `/data/display/`, so the screen survives a Space rebuild. History capped (40) and pruned by age — **pinned artifacts survive unconditionally**, because "keep this on my screen" has to mean it.
- **Who draws it:** for `chart` the model emits data and our code draws, so a chart survives a phone screen and a dark theme. For everything else **the model writes the markup itself** — a hand-rolled renderer can only draw what its author imagined, and "anything" means the markup has to come from the model.
- **The sandbox is the whole safety story.** Model-written pages run in a frame with `sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads"` and, deliberately, **without `allow-same-origin`** — so the page gets an opaque origin and cannot read the dashboard's `sessionStorage`, cannot see the session token, cannot call an authenticated endpoint, and cannot touch the window it lives in. `<base>` and meta-refresh are stripped, `target=_blank` gets `rel=noopener`, the page is served with a CSP and `no-store`. A real browser test in `e2e/test_display.py` loads a deliberately hostile page through the exact frame attributes the panel uses and asserts it gets `origin: null`, no token, no cookie, a blocked API call, and no `top`.
- **Model-callable `display` tool:** `show` (html/chart/image/text), `embed` (url), `list`, `close`, `pin`. Triggers on "show me", "draw", "plot", "put X on the screen", "open <url>". New artifacts auto-open the panel only if it is already open — a diagram appearing by itself is delightful, a panel stealing focus mid-conversation is not.
- **◱ Display panel** in the header: artifact list with kind/age/pinned, the stage, a warning strip for inferred values, open-in-tab / pin / remove per artifact, and a chart renderer (line · bar · pie · area) that draws SVG from data.
- **Honesty rule:** a `warning` field. Anything the model *inferred* — a resistor value, a pinout, a price — renders with a visible "check the datasheet" strip. Templates the user saves and pins are the ones they have vouched for.
- ✅ *Demo moment: "JARVIS, show me how to wire a DHT11 to an ESP32" (or anything else at all) → it is on the screen, in seconds, and it is yours to keep.*

**Status (as of this commit):** `core/display.py`, the `display` tool, the `/api/display*` endpoints, the ◱ panel and `e2e/test_display.py` are **written but not yet gated or deployed** — the display suite has not been run to green, nothing is committed, and the Space is still on 4d. Next: run `e2e/test_display.py`, the four existing suites, secret scan, commit, deploy, and a live smoke (ask for a diagram, confirm the frame is sandboxed on the real Space).

### Phase 4f — Spatial intelligence: the globe, and a map you can argue with (2–3 days)
**Source of the idea:** the [God's Eye View](https://github.com/bilawalsidhu/gods-eye-view) project (43.6k ⭐, MIT) — *"a spy-satellite simulator in your browser, except the data is real."* A photorealistic Earth with live flights, ships, satellites, earthquakes, fires and cameras, and it answers voice commands like *"take me to Tokyo"*, *"draw the route from the Capitol to Zilker Park"*, *"fly the route we just drew"*.

**What we take, and what we do not.** The **code is MIT** — reuseable with attribution — but the **data is not**: OpenSky is non-commercial, TeleGeography's cable map is CC BY-NC-SA, Vantor's flood imagery is CC BY-NC. Personal-use project (user, 2026-09-27) → those are fair game, and every layer still carries its own credit line.

**We vendor the real thing.** `scripts/build_godseye.py` clones their repo at a **pinned commit**, `vite build`s it, flattens their nested `dist/godseye/` (their build bakes a `/godseye/` base), rewrites their `/api/*` calls to `/api/gev/*` so they cannot collide with this project's API on the same origin, and commits the result to `godseye/_server/`. Their Node server runs in the Space image; the dashboard serves the app itself off disk and proxies only the data routes to it. The action and layer vocabularies come out of that build's `MANIFEST.json`, so the build script is the single source of truth and JARVIS cannot address a layer that does not exist.

- **JARVIS replaces their voice.** The app exposes `window.__gevVoiceCommands`. `actionExecutor(name, args)` is what their OpenAI-Realtime voice calls — and it throws *"Voice action cancelled"* for everything when no realtime session is live. Directly underneath, **`runner(name, args)`** is the same vocabulary without that guard. So JARVIS calls `runner`: **30 actions, 29 layers**, no OpenAI key, no second brain, and their own validation still guards us (an unknown layer comes back `Unknown data layer`).
- **Two globes, on purpose.** `core/globe.py` is our own small Cesium page: no Node, no 35MB, always available, themed by `core/theme.py`. The vendored app is the full console. The `globe` tool prefers the real one and falls back to ours, so the tool never dead-ends.
- **Keyless, official, live, both ways:** USGS earthquakes, CelesTrak TLEs (SGP4 in-browser), OpenSky + adsb.lol aircraft, GDACS, NOAA cyclones/radar/lightning, NASA GIBS, Esri World Imagery, NIFC fire perimeters, GDELT, plus our own Open-Meteo weather. Cached and rate-limited wherever we are the one fetching.
- **Routing is theirs, and JARVIS plans it.** Their OSRM `fly_route` + `directions` layer are keyless: *"draw the walking route from the Capitol to Zilker Park"* → *"fly the route we just drew."* Building a second router would have been duplicate work, so instead JARVIS speaks it — "plan me a day in Istanbul" geocodes the stops, draws the route and flies it.
- **`core/theme.py` — the theme is injected, not remembered.** Every page the model writes (4e `html` artifacts, globe, maps) gets the dashboard's real tokens — `--pri:#00d4ff`, `--acc:#ff6b00`, `--acc2:#ffcc00`, `--bg:#00060a`, `--border:#0d3347`, the type scale, the glow — so *nothing* JARVIS draws can drift off-theme. (Hard requirement from the user: everything matches the dashboard.)
- **Maps stay maps.** `core/maps.py` keeps doing what maps are for: geocoding, nearby, provider links, and — with keyless **Valhalla/OSRM** — real turn-by-turn routes with alternatives, stops and POIs, narrated by JARVIS. The globe is the view *from orbit*; the map is the view *at street level*. They share the geocoder and the theme, nothing else.
- **No key required.** Without a Google Maps key we get Esri World Imagery + NASA GIBS — Earth from orbit, commercially safe, and a Google key dropped into settings later unlocks photorealistic 3D Tiles with no code change.
- ✅ *Demo moment: "JARVIS, take me to Istanbul" → the globe flies there, the live layer is on, and every quake in the last 24h is a dot you can click.*

**Status:** shipped. `core/theme.py` (injected into every page JARVIS draws), `core/globe.py` + `core/godseye.py`, the vendored app at `godseye/`, the `globe` tool, the 🛰 surface in the display takeover, and `e2e/test_globe.py` + `e2e/test_godseye.py`. 4f's second half — the app driven by voice, on the Space, with their API proxied — is verified end to end: the real app loads from our mount, its own data calls arrive via `/api/gev/`, and a browser drives `zoom_to_globe`, `fly_to_location` (*"Istanbul, Fatih, Turkey"*), `set_visual_style` and `set_layer_visibility` (*"Earthquakes (24h)"*) with the camera verifiably moved.

**Keyless by default, unlockable without a rebuild:** no Google Maps key means Esri satellite rather than photorealistic 3D Tiles. The globe reads `window.__GOOGLE_MAPS_API_KEY__`, and the vendored build is never rebuilt to add one — the dashboard **injects it at serve time** (`inject_map_providers`), so a key is a settings save: **🛰 → KEYS** inside the globe, or a Space secret (`JARVIS_GOOGLE_MAPS_KEY` / `GOOGLE_MAPS_API_KEY`, and `CESIUM_ION_TOKEN` for real terrain) which wins over the file. Keys live in `config/api_keys.json` — never committed — and the API only ever returns them masked. Their in-app **POWER UP** key panel is dev-server-only, so this row is the supported way to add one here.

### Phase 5 — Safety: policy engine + approvals (1–2 days, gates Phase 4 sending)
- Capability tiers: `read / act / spend / delete`. Risky = send-message, create-invoice, push-code, delete, shutdown → **approval request** on phone (extend existing `/api/confirm` to tasks/leads, with payload preview).
- **Audit log** (action, actor, target, result) + **spend caps** (e.g. $0/day auto until raised), rate limits.

### Phase 6 — Knowledge & continuity (1–2 days)
- Persist conversation transcripts (SQLite on `/data`), **RAG**: Gemini embeddings + `sqlite-vec`, `knowledge` tool (ingest notes/URLs/client docs, semantic recall within the prompt budget). It remembers clients, rates, and past decisions instead of re-asking.

### Phase 7 — Company breadth (as-needed, after leads convert)
Gmail+Calendar → Stripe invoicing → proposals PDF + e-sign → storage. Each connector = an action following the existing `TOOL` contract + an OAuth setup screen. **Only build what the pipeline actually reaches.**

### Phase 8 — The AI Company: sub-agent org (3–5 days)
- **Agent roster:** named roles you configure in the dashboard — each agent = `{name, role, persona/system prompt, model + gateway, allowed tools, budget, schedule}`. Starter crew: **Coder** (opencode via Phase 2), **Lead Scout** (Phase 4 discovery), **Copywriter** (proposals, ads, emails), **Ads Manager** (campaign concepts + ad copy), **Researcher** (web/deep research), **Builder** (apps, landing pages, scaffolds), **Reporter** (daily briefings).
- **JARVIS = CEO:** `delegate` becomes org-aware — break a goal ("launch a landing page for X + 5 ads + 20 leads") into work packages, assign them to agents, track dependencies, set *reviewer* agents to check each other's output, merge results, report by voice and dashboard.
- **Real deliverables:** apps/projects land as repos (Phase 2) and deployed artifacts; ad sets, slogans, landing copy, lead batches, research memos — all tracked in a **Deliverables** panel with version history.
- **Configurable + monitorable:** every agent visible live in the dashboard — status, queue, tokens/$ per agent, success rate, **pause/retire kill switch**, per-agent model choice.
- ✅ *Delivers: "runs an artificial AI company — delegates to sub-agents that deliver ads, ideas, leads, and real projects."*

### Phase 9 — Control Center: the dashboard at full power (built incrementally, final polish 2–3 days)
The UI is a first-class deliverable — the bar is "so good and powerful":
- **Panels:** Agents (P8) · Tasks (P2) · Leads/CRM kanban (P4) · Devices (P1) · Scheduler jobs (P3) · Approvals inbox (P5) · Deliverables (P8) · Knowledge/RAG (P6) · Connectors & API keys (P7) · Audit log · Sysmon/HUD (P0 fix).
- **Config console:** every knob lives in the UI — models/voices, agent definitions, discovery sources & cadence, send caps, risk tiers, tool permissions, where tasks run by default. **No file edits ever required.**
- **Monitoring:** live WS streams (logs, task progress, lead events), per-agent cost/time charts, health of Space + all daemons, custom alert rules.
- **Craft:** mobile-first (the phone is the control surface), dark HUD aesthetic to match today's UI, searchable everything, keyboard shortcuts, one-tap approvals with payload preview.
- *Rule: every phase ships its dashboard panel as part of the phase — P9 is consolidation, the config console, and polish.*

---

## 3. Sequencing

```
0 sync + hw-monitor fix → 1 devices → 2 coding/delegation (runs where YOU choose)
      → 3 scheduler+push → 4a leads → 4b score → 4c draft+approve → 4d send
      → 4e display surface → 4f globe + trip maps → 5 approvals → 6 knowledge → 7 billing
      → 8 AI-company org → 9 control-center dash
```

**Reuse, don't rebuild:** pairing/device tokens, `/api/confirm`, WS broadcast+history (new `task`/`lead` message types), action/plugin `TOOL` contract, `dev_agent`/`code_helper`, `web_fetch`/`web_search`, `gh`/discord plugins, `memory_manager`, dashboard overlay patterns.

**Guardrails held:** never touch `ui.py`/`core/tts.py`; never stage `config/`; E2E 31/31 + secret scan + hash-verified deploy every phase; official APIs only; Space stays light (heavy = daemons).

**Risks called out:** HF cpu RAM/sleep (daemons absorb the load; paid hardware later if wanted), lead-source ToS (RSS/official APIs only), cold-email deliverability (approve-gate + caps + warmup), X/LinkedIn APIs (deferred).

---

**Suggested MVP cut for fastest wow:** Phase 0 → 1 → 2 (coding runs on **your machine or the Space — your choice per task**) → 3 → 4a (leads appearing in a dashboard panel) — with 5 gating any send. Then 8 + 9 for the full AI-company experience.

**Decisions locked (user, 2026-09-25):** devices = Windows PC + phone + NAS + cloud VPS · company core = self-serve lead finding · coding = **user's explicit choice per task (your machine or the Space — never auto-tiered)** · autonomy = confirm risky actions · scope = full AI company (sub-agent org delivering code, apps, ads, ideas, leads) · everything configurable + monitorable via a top-tier dashboard · hardware monitor must read real Space/container values, not host values.
