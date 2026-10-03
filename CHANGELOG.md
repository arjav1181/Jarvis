# Changelog

All notable changes. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **The welcome ceremony had never once run from a clap.** `_welcome_panels` was
  a nested function inside `_build_app` and was called from `_welcome`, a method
  outside it. Every clap raised `NameError`, and the `try`/`except` turned it
  into "The welcome did not run: …", which reads as flaky rather than
  impossible.
- **One malformed tool schema stopped the assistant hearing anything.**
  `scene` declared a parameter as `{"type": "OBJECT", "array": True}`, which is
  not valid JSON Schema. Gemini forbids extra keys, so it rejected the entire
  tool list — no wake word, no microphone, no replies, and no visible error.
  All 68 declarations are now audited.
- **Memory did not remember and did not recall, because the turns were never
  written.** The live conversation only ever became a one-or-two sentence
  summary. `recall` searches `bot_chats/`, which only sub-bot messages ever
  reached. Real turns are now persisted.
- **Knowledge search raised `TypeError` on every lookup** — the parameter is
  `k`, not `k_limit` — so it read as "JARVIS has forgotten everything".
- **`/api/display` and the godseye proxy answered 500**, from nested helpers
  removed during earlier refactoring.
- **"Good morning." reached the reader as "morning."** The character pass
  treated any sentence-initial `good/great/well/right/perfect` as filler; six of
  fourteen ordinary replies were silently mangled.
- **`weather_report` was not a weather report.** It opened a Google search and
  said "Showing the weather for London".
- **Snow was advised to carry an umbrella**, because snow sat behind the rain
  branch in the weather prose.

### Added
- **`/api/health`** — public liveness: assistant state, whether you are logged
  in, whether the Gemini key is set, whether delegation is ready, the wake
  word, and recent errors. It exists because three wrong diagnoses were made
  before it did.
- **`core/errorlog.py`** — a bounded in-memory ring of recent errors, redacted
  *before storage*, with no accessor for the unredacted buffer.
- **`tools/check_scope.py`** — reports any name a function calls that is bound
  nowhere it can see. Found four `NameError`s that `py_compile` cannot.
- **`agent_work`** — the Antigravity SDK as a tool the model can actually
  reach, at the strictest approval tier.
- **Ten hot-loadable plugins** and a validated, non-shadowing installer.
- **A city setting** in the dashboard, replacing an environment variable that
  could only be changed by redeploying.
- **Four welcome stings** with an in-dashboard audition, because picking music
  from a waveform is not taste.

### Changed
- **Delegation now uses the OpenAI-compatible gateway** first, falling back to
  Gemini. Secondary calls and the coder already preferred it; delegation did
  not, because it shells out to a separate binary with its own provider config.
- Removed six features at the user's request — 3D avatar and lip-sync, email
  campaigns, invoicing, the lead engine and its CRM, proposal and invoice
  generation, and eight preset work-role agents. **−6,023 lines.**
- `billing.money()` moved into `core/docs.py` before billing was deleted, so
  prices in documents still format.

### Security
- Secrets are redacted before the error log stores them, not on the way out.
- Plugin installs are refused by name when the repository already ships that
  plugin, instead of writing a file that loses at every scan.

## [0.1.0]

First working release: voice loop, shared computer with takeover, ceremony,
memory, plugins, approvals, dashboard.
