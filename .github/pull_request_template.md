## What this changes

<!-- One or two sentences. If it fixes a bug, say what was broken. -->

## Which layer

- [ ] `core/` — the engine
- [ ] `plugins/` — a capability
- [ ] `dashboard/` — UI or HTTP
- [ ] `actions/` — legacy tools
- [ ] docs / CI only

## Checklist

- [ ] `python -m compileall -q core main.py ui.py dashboard actions plugins`
- [ ] `python tools/check_scope.py main.py dashboard/server.py ui.py`
- [ ] The relevant `e2e/` suite passes, and there is a test that **fails without this change**
- [ ] A new or changed tool has a `core/policy.py` entry, tiered per action
- [ ] No secret, credential, transcript or screenshot can reach a log line
- [ ] Every failure path returns a reason rather than an empty string
- [ ] No name shadows a core tool or an existing plugin

## Anything a reviewer should know

<!-- Anything you were unsure about, or deliberately left alone. This is the
     most useful section in the whole template. -->
