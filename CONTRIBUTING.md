# Contributing

Thanks for looking. This is a personal project kept by one person, so the
bar for a pull request is not "perfect" — it is "does not break the thing".

## Before you open anything

1. **Open an issue first** for anything larger than a bug fix. It is much
   cheaper to agree on an approach than to rebase a rejected 4,000-line diff.
2. **Search the issue tracker.** It is short, because the project is young.
3. Say which of the four layers you are touching, so review can be focused:
   `core/` (engine), `plugins/` (capabilities), `dashboard/` (UI + HTTP),
   `actions/` (legacy tools).

## The rules that matter

These are not style preferences. Breaking them breaks users.

- **Every tool passes through `core/policy.py`.** A new tool with
  consequences and no policy entry falls through to the fail-closed `spend`
  tier — which means it asks about everything, including reads, and users learn
  to approve without reading.
- **Secrets are never strings.** Use `secret:NAME`. The model must never see a
  value, and neither may a log line, a transcript or a screenshot.
- **Never swallow an error silently.** Every failure path returns a reason. An
  empty return is indistinguishable from "nothing to say" and that is how the
  ceremony went years without anyone noticing it had never run.
- **A new name may not shadow a core tool or an existing plugin.** The loaders
  reject it and say which one it collided with.
- **Never commit build output, caches or credentials.** `.gitignore` covers
  them; `config/api_keys.json` is created on first run and stays untracked.

## Tests

```bash
pip install -r requirements-server.txt
python -m playwright install chromium

python e2e/test_ceremony.py        # one suite
python e2e/test_plugins.py
```

13 suites run in CI. Three (`test_agent`, `test_tasks`, `test_phase3`) open raw
sockets and spawn subprocesses and are excluded; if you fix them, add them to
the matrix in `.github/workflows/tests.yml`.

A new capability needs a test that fails without it. `tools/check_scope.py` is
not optional either — it catches names that resolve nowhere, which `py_compile`
cannot and the suite did not.

## Style

Match the file you are editing. These files carry a house style: module
docstrings that explain *why* rather than *what*, comments that record the
reasoning and the bug that motivated them, and honest error strings that say
what is missing and how to fix it.

```bash
python -m compileall -q core main.py ui.py dashboard actions plugins
python tools/check_scope.py main.py dashboard/server.py ui.py
```
