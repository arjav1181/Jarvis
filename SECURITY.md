# Security

## Reporting a vulnerability

Please **do not open a public issue** for anything exploitable. Email
**arjav.3003jain@gmail.com** with:

- what an attacker can do, and what they need in order to do it
- the route, tool or file involved
- a reproduction if you have one

You will get an acknowledgement within a few days. There is no formal bug
bounty and no SLA, because this is one person's spare time — but a real report
is read and usually fixed.

## What is already in place

Worth knowing, so you do not spend effort re-checking it:

| | |
|---|---|
| **Auth** | Bearer tokens, held in memory only. A Space restart drops every session, which logs everyone out — this is deliberate, not a bug. |
| **Approval** | `core/policy.py` gates every tool. Read-only is free; writes, sends, spends and deletes ask first. Unregistered tools fall through to the fail-closed tier. |
| **Human-in-the-loop** | `core/confirm.py` is the only way a pending action resolves. Callers cannot self-approve. |
| **Secrets** | `secret:NAME` resolves in memory. Values never reach the model, a transcript, a log line or a screenshot. Unknown names fail closed. |
| **Browser** | JARVIS can hand over: the takeover button pauses it and a human drives. |
| **Plugins** | Validated in a subprocess before being saved. Cannot shadow a core tool or another plugin. Installing is remote code execution by design, so every plugin route is token-gated. |
| **Error log** | Recent errors are redacted *before storage*, and there is no accessor for the unredacted buffer. |
| **Uploads** | Filenames are stripped of path separators and served from one directory only. |

## Known weaknesses

Stated plainly, because a security page that only lists strengths is marketing:

- **Sessions are in memory.** A restart logs everyone out; it does not
  invalidate anything an attacker obtained beforehand.
- **`/api/gcal/calendar.ics` is unauthenticated.** It exists so a calendar
  client can subscribe. It currently returns an empty calendar — but once
  Google Calendar is connected it will serve your calendar to anyone who asks.
  This is the most serious open item and it is tracked in
  `docs/known-issues.md`.
- **`/api/display/{aid}` is unauthenticated**, for the same reason: a sandboxed
  iframe cannot send headers. Anyone who can guess an artifact ID can read it.
- **Bot isolation is organisational, not a security boundary.** A second bot
  does not get a second browser, and the shared credentials are shared.
- **Plugins are trusted code.** The validation prevents accidents, not malice.
