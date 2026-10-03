# Plugins

A plugin is the cheap way to add a capability. Write one file; nothing else
needs to change.

## The shape

```python
PLUGIN = {
    "name": "my_plugin",                 # ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$, unique
    "description": "…",                  # what the model uses to decide
    "parameters": {"type": "OBJECT", "properties": {…}, "required": []},
}

def run(parameters: dict, player=None, session_memory=None) -> str:
    return "prose back to the model"
```

Drop it in `plugins/` (shipped) or install it at runtime from the ⬡ panel
(persistent, hot-reloads, no rebuild).

## The rules

- **Valid JSON Schema.** `{"type": "ARRAY", "items": {...}}` — not
  `{"type": "OBJECT", "array": True}`. Gemini **rejects the entire tool list**
  when one declaration is malformed, so a typo in one plugin takes the whole
  assistant mute. This is not theoretical; it happened.
- **Register the policy.** Add a tier to `core/policy.py`, per action. Read-only
  is free; writes, sends, spends and deletes ask.
- **Never return an empty string.** It is indistinguishable from "nothing to
  say". Every failure returns a reason.
- **Never shadow a core tool or another plugin.** The loader rejects it and says
  which one it collided with.
- **Lazy imports.** Do not import heavy modules at module scope; plugins load at
  startup.

## Shipping one

Ten ship in `plugins/`: `recall`, `day_glance`, `summarise`, `quiz_me`,
`exam_cram`, `scene`, `agent_work`, `health_svc`, `github_ops`, `discord_ops`,
`homelab`. `recall` and `day_glance` are the two worth reading first.

## Installing at runtime

⬡ → paste or write → **VALIDATE** (it runs in a subprocess) → **INSTALL**. It is
remote code execution by design, so the route is token-gated and installation is
refused if the repository already ships that plugin name.
