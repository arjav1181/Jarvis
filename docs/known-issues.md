# Known issues

Written down rather than discovered later. Each says what breaks, why it has
not been fixed, and what it would take.

## Serious

### `/api/gcal/calendar.ics` serves your calendar to anyone

It exists so a calendar client can subscribe, which cannot send an
`Authorization` header. It is unauthenticated. It returns an **empty** calendar
today, because Google Calendar is not connected.

**The moment you connect Google Calendar, anyone who requests that URL gets
your calendar.** No token, no login.

Fixing it means either a signed, expiring token in the URL, or dropping the
feed and reading the dashboard instead. Not done because it changes how you
subscribe.

### `/api/display/{aid}` is unauthenticated

Same cause: a sandboxed iframe cannot send headers. Anyone who can guess an
artifact ID can read it. IDs are not secret and not unguessable.

## Real, not urgent

### One shared browser, not one per bot

`core/computer.py` owns a single Chromium. Two bots asking for the browser get
the same one, and a bot can see what another was doing. The approval gate and
per-bot attribution are enforced; the isolation is not.

Needs one browser per bot, which means one profile and one event loop each.

### Approval is per turn, not per click

You approve a tool call for the turn. A multi-step plan approved once will
complete all of it. Per-click approval means the gate sits inside the tool loop
rather than around it.

### Delegation depends on a binary that may not be there

`where='space'` delegation shells out to `opencode`. If it is not installed the
task fails with a clear message, but "delegate this" is the obvious thing to
say and it fails often enough to be worth knowing.

### The Windows client does not exist

The intent is an installable client that pairs with the Space so it can drive
the machine. `devices/pair`, `devices/ping` and `devices/exec` exist and
`jarvisd.py` is the daemon, but there is no installer and no pairing UI a
non-technical person can use.

## Deliberate, not bugs

- **Three test suites do not run in CI** (`test_agent`, `test_tasks`,
  `test_phase3`). They open raw sockets and spawn subprocesses and hang. Fixing
  them is real work; excluding them visibly is better than a flaky badge.
- **`godseye/_server/dist/` is committed** — 428 build artefacts. The Dockerfile
  now builds from source with the committed copy as a fallback, so the tracked
  copy can be dropped once CI runs `npm run build:check` on a green build.
- **The wake word and the Antigravity SDK are optional installs.** Both report
  their state on `/api/health` rather than failing quietly.
