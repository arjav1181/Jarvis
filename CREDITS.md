# Credits and attribution

JARVIS is a derivative work. This file exists so that every upstream project is
credited in one place, with its licence and the exact commit we pinned — not
scattered across READMEs where a future reader will not find it.

---

## The project this is a fork of

**Mark-LIV — the Ultimate Cross-Platform Personal AI Assistant**
<https://github.com/FatihMakes/Mark-LIV> · author **FatihMakes**
([YouTube](https://www.youtube.com/@FatihMakes) ·
[Instagram](https://www.instagram.com/fatihmakes))

- **Licence:** CC BY-NC 4.0
- **Relationship:** this repository is a fork. `origin` still points at the
  original and the fork relationship is intact.
- **What this means for us, and it is not optional:** CC BY-NC is
  **non-commercial**. Every derivative of this project, including the
  deployment, must stay non-commercial and must keep attribution. We do not
  get to relicense the parts that came from there. If that ever needs to
  change commercially, the upstream code has to come out first.

Everything that was already in the repository when we started — the voice
pipeline, the Gemini Live session, the avatar, the desktop app, the file and
system actions, the dashboard, the plugin system, the browser control, the
scheduler, the agents framework, the policy gate — originates there. The work
we added on top is listed under "Added in this fork" below.

---

## Vendored third-party projects

### God's Eye View — the globe
<https://github.com/bilawalsidhu/gods-eye-view> · author **Bilawal Sidhu**

- **Licence:** MIT
- **Pinned at** `b210ab0fe4d71c7faa0268134e0aa5f3c53fc7fe` (`main@b210ab0`),
  recorded in `godseye/MANIFEST.json`.
- **Why it is vendored in full:** 30+ live data providers (ADS-B aircraft, CCTV,
  wildfire perimeters, wind, cyclones, radar, lightning, AIS vessels, transit,
  military installations, satellites, rocket launches, traffic) behind a Node
  server, and 24 control actions. That data is the value; we did not rewrite it.
- **Data terms are per-dataset, not MIT.** See upstream `DATA_SOURCES.md` in the
  vendored tree. At least one layer (`telegeography-submarine-cables`) is
  **CC BY-NC-SA**, which is why the vendored build is for personal use.
- **Our changes to it:** the JARVIS palette in
  `src/ui/styles/foundation.css`; `tools/build-dist.mjs`, because `vite build`
  deletes the static models and imagery it does not own. We did not modify the
  vendored application logic.
- **Their licence text:** `godseye/_server/LICENSE`

---

## Added in this fork

Everything in this section is ours, written on top of the two projects above.

| Area | What |
|---|---|
| **Identity** | `core/character.py` — the assistant's own pass over its replies, so the voice holds on turn 40 as well as turn 1 |
| **Goals** | `core/goals.py`, `core/director.py` — standing goals, a position on each, and one background move per run |
| **Connectors** | `core/svc.py`, `core/github.py`, `core/vercel.py`, `core/hf.py`, and the inbox surface in `core/mail.py` |
| **Model Context Protocol** | `core/mcp.py` — a from-scratch client for the MCP stdio protocol, so any MCP server becomes available without a hand-written connector |
| **Background watches** | `core/watches.py` — reports CI or a deploy breaking once, and stays quiet while it stays broken |
| **Container** | `Dockerfile`, `scripts/docker_deps_check.py`, the requirements audit |
| **Globe integration** | `tools/build-dist.mjs`; the palette change noted above |

### Protocols and specifications we implement

These are specifications, not code we copied, but they are worth naming because
the wire formats are theirs:

- **Model Context Protocol** — <https://modelcontextprotocol.io> — implemented
  directly from the specification in `core/mcp.py`.
- **Google Calendar / Gmail** — the `googleapiclient` surface; `core/gcal.py`
  talks to Google over raw `urllib`.
- **Web Push (RFC 8291), VAPID** — `core/push.py`.

### Music

Four stings ship in the repository under `dashboard/static/music/`, and the
ceremony panel lets you audition all four and choose. Two earlier picks were
rejected as "trash" — which was the correct verdict on a waveform and a swell
ratio. Neither of those is taste, so the fix was to stop picking: the four
differ in the one thing that matters for a welcome, which is how much the
music moves. Measured as level variation inside the 22-second window they span
0.25 to 0.86, the difference between something calm and something that drifts.

All four are **CC BY 4.0 from incompetech.com**, by **Kevin MacLeod**. Every
one of them is somebody else's work, so every one carries a credit, in the
data the server sends, in the panel, and here. The credit follows the choice,
because a track picked out of this set is still under a licence.

| id | track | what it is |
|---|---|---|
| `ethereal` | Ethereal Relaxation | drifting, wide, the most movement |
| `dreams` | Dreams Become Real | dreamy synths, a little sad |
| `thunderbird` | Thunderbird | steady and warm, settles early |
| `vanishing` | Vanishing | the calmest — almost still |

Each is built by `tools/prepare_welcome_music.py` from the unmodified upstream
file, so the binaries in git are reproducible rather than a mystery. What that
does: cuts the full track to 22 seconds, placing the cut on the loudest moment
in the window, fades it in over 0.30s and out over 1.2s, and sets the level by
measuring the finished file and correcting — the result measures −16 dBFS with
headroom to spare, so the limiter is not doing the work.

Choosing writes a pointer on the persistent volume rather than editing the
repository, so trying all four is a click and not a deploy. An upload still
outranks any of them, so a track of your own wins without ceremony.

Also free, same licence, and measured but not shipped:
<https://incompetech.com/music/royalty-free/index.html> — "Lightless Dawn",
"Long Time Coming" and "Impact Prelude" were considered and cut from the set.

### Ideas consulted, code not taken

The clap detector in `core/clap.py` and the ceremony in `core/ceremony.py`
were written for this repository, not ported. Two projects were read for the
conventions they had settled on, and the record is here so nobody has to guess
where the shape came from:

- **hectorg2211/jarvis** — a single-file Windows script that claps to trigger a
  desktop welcome flow. **No licence**, so nothing was copied: an unlicensed
  repository is all-rights-reserved by default, and this project does not
  vendor code it has no right to vendor. Its useful contribution was the idea
  that a welcome can be gesture-triggered, plus the tuning vocabulary
  (adaptive noise floor, a spike ratio, a refractory period).
- **canmenzo/jarvis-home-automation** — MIT. Read for what a good ceremony
  contains: a greeting that varies by hour, openers that rotate so it never
  sounds like a recording, weather with no API key commented on as advice
  rather than reported as numbers, and sign-offs that ask for the work. MIT
  permits reuse with attribution, which this line is.
- **monkerton345/jarvis-ai** — **no licence**. Read only for the voice
  convention; nothing copied.

The dialogue register ("Welcome home, sir", the formal British address, the
dry late-night aside) is the shared cultural shorthand everyone builds against.
It was written fresh rather than lifted from any transcript.

The music is deliberately not a streaming service. It is a wav or mp3 the user
supplies, uploaded through the panel onto the Space's own volume — no key, no
account, no per-call cost, and no rebuild needed to change it.

### Runtime services

Not open source, not vendored, but load-bearing:

- **Google Gemini** — the Live session, the model calls, the vision path.
- **Anthropic / OpenAI-compatible gateways** — the voice fallback and the chat
  fallback in `core/gateway.py`, pointed at a self-hosted OmniRoute endpoint.
- **CesiumJS** — Apache-2.0, shipped inside the vendored globe build
  (`godseye/_server/cesium/`). Its licence is retained in the vendored tree.

---

## Plugins that ship

Ten live in `plugins/`, and they are the answer to "I want a new feature" —
write one instead of editing core.

| plugin | what it does |
|---|---|
| `recall` | search every stored transcript — what was said, when, and by which bot |
| `day_glance` | the day in one breath: time, calendar, todo list, focus timer |
| `summarise` | strip a URL or file to what it actually says, reporting what it dropped |
| `quiz_me` | quiz on any topic, adapt to the answers, keep score across restarts |
| `exam_cram` | revision plan weighted by what you are actually worst at, plus mock papers |
| `scene` | run several things as one named routine — deep work, wrap up, morning |
| `health_svc` | read-only health: uptime, CPU, memory, disk, deploy, jobs, plugins |
| `github_ops` | GitHub repositories, pull requests, CI |
| `discord_ops` | Discord channels and messages |
| `homelab` | your own machines |

The risk model is deliberate. A plugin is registered in `core/policy.py` by
what it can actually do, per action — so reading your own history, fetching a
page, or asking how the Space is doing costs no prompt, while ticking off a
todo, running a scene, or forgetting a transcript asks every single time. An
unregistered tool falls through to the fail-closed `spend` default, which is
correct and useless at once: a user asked to approve ten times a day stops
reading the prompt, and then approves things too.

## Adding a capability

Plugins are the intended way to add a feature, and they are the reason the
core does not need to grow for every small thing.

A plugin is one Python file with a `PLUGIN` dict and a `run()` function —
`plugins/_template.py` is the whole contract. Start from it, or paste your code
straight into the panel's Plugins view.

Two places a plugin can live:

- **`plugins/` in this repository** — reviewed, version-controlled, shipped.
- **The persistent volume** (`<data>/user_plugins`) — written at runtime from
  the panel, survives rebuilds, and needs no release. The repository is scanned
  first, so a runtime plugin can never quietly shadow a reviewed one.

Installing is validated *before* it is kept: a plugin that does not import, has
no `PLUGIN` dict, or is missing `run()` is refused with the reason and never
written. Once installed the registry is reloaded in place, and because the tool
declarations are rebuilt every turn, it is callable on your very next message.
No restart, no rebuild, no deploy.

Worth being plain about: a plugin is Python that runs in the assistant's own
process. Installing one is remote code execution by design. Every route is
behind the dashboard token, and the only person who should hold that token is
you — the same position as every other authenticated surface here.

## If you add something

When you vendor, fork or import third-party code into this repository, add it
to this file with: the project, the author, the licence, the pinned commit, and
what you changed. That last one matters most — it is the difference between
"we use this" and "we changed this", and it is the only way a licence
obligation gets met honestly rather than hopefully.
