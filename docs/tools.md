# Tools and approvals

## The permission model

| Tier | Means | Example |
|---|---|---|
| `read` | Free | search your transcripts, fetch a page, a health check |
| `act` | Asks first | tick off a todo, run a scene, drive the browser |
| `spend` | Asks, no escape hatch | send real email, an autonomous agent |

Per-action entries refine this, so `recall` is free for `search` and asks for
`forget`. **An unregistered tool falls through to `spend`** — correct, and
useless in practice, because a user asked to approve ten times a day stops
reading the prompt. Register yours.

## Human-in-the-loop

`core/confirm.py` is the only path that resolves a pending action. Callers
cannot mark their own work approved. When a browser login or a 2FA code
appears, the assistant hands over rather than guessing.

## Secrets

Ask for `secret:NAME`. The vault resolves it in memory; the model never sees the
value, and neither does a log line, a transcript or a screenshot.

## Using it

1. **Start small.** `recall`, `day_glance`, `summarise`.
2. **Before you trust it**, know how it fails: read the error strings. They are
   written to be read by you, not to look technical.
3. **Before you trust it with anything real**, check it has a policy entry. No
   entry means it asks about everything.
