# Configuration

Two places, and the rule between them is deliberate:

- **Environment variables** are deployment-wide. Setting one in the Space is a
  statement about the deployment, and it **wins**.
- **`config/api_keys.json`** is what a person changes. Created on first run,
  never committed.

## The most important ones

| Variable | Effect if unset |
|---|---|
| `GEMINI_API_KEY` | No voice, no thinking. Reports itself on `/api/health`. |
| `JARVIS_CITY` | Weather stays **silent**. A wrong city is worse than none. |
| `JARVIS_DASHBOARD_TOKEN` | The dashboard generates one and prints it. |
| `SERVER_MODE=1` | Headless: dashboard instead of the desktop UI. |
| `JARVIS_OPENAI_BASE_URL` | Delegation falls back to `opencode`'s own provider. |
| `JARVIS_OPENAI_MODEL` | Delegation uses no explicit model. |

## Voice and ceremony

| Variable | Default | Meaning |
|---|---|---|
| `JARVIS_WELCOME_ENABLED` | `1` | Clap-to-greet |
| `JARVIS_WELCOME_PANELS` | — | Panels the welcome opens |
| `JARVIS_WAKE_WORD` | on | "Hey Jarvis" |
| `JARVIS_PTT` | — | Push-to-talk instead |

## Gateway

`JARVIS_OPENAI_BASE_URL`, `JARVIS_OPENAI_API_KEY`, `JARVIS_OPENAI_MODEL`,
`JARVIS_VOICE_MODEL`, `JARVIS_STT_MODEL`, `JARVIS_TTS_MODEL`,
`JARVIS_STT_MODE`, `JARVIS_TTS_MODE`, `JARVIS_VOICE_FALLBACK`.

## Setting the city

Type it into the ceremony panel (✧) and press **SET**. That writes
`settings.json` on the volume and survives a restart.

An env var still overrides it, on purpose: an operator setting `JARVIS_CITY` in
the Space is making a statement about the deployment, and a value typed into a
dashboard should not silently outrank that.
