"""Hardening: the failures that make an assistant look broken rather than busy.

Every test here corresponds to a bug that actually shipped, or to a whole class
of them that this file now closes. They are grouped by the symptom the user
reports, not by the module, because the symptom is what matters when one fires.

  * "it asks for permission for everything" — tools nobody registered, falling
    through to a fail-closed default.
  * "it does not know what it can do" — the model told nothing about its own
    systems or its own environment, so it guessed, or searched the web.
  * "it claimed it could do something it cannot" — a handler that swallowed an
    exception and returned nothing.
  * "it broke on startup" — an annotation naming a type the module never
    imported.
"""
import ast
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="jarvis-test-hard-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def _src(name):
    with open(ROOT / name, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def _static_tools():
    src = _src("main.py")
    i = src.index("TOOL_DECLARATIONS = [")
    j = src.index("\n]\n", i) + 3
    ns = {}
    exec(compile(src[i:j], "<t>", "exec"), ns)
    return src, [d["name"] for d in ns["TOOL_DECLARATIONS"]]


# ── "it asks for permission for everything" ──────────────────────────────────

def test_every_tool_is_registered():
    """A tool nobody named in the policy falls through to DEFAULT_TIER=spend."""
    from core import policy as P
    from core import action_loader as AL
    reg = AL.discover_actions(ROOT / "actions")
    action_names = [d.get("name") for d in reg.get_tool_declarations()]
    check("hard.actions.discovered", len(action_names) >= 15, str(len(action_names)))

    listed = set(P.TOOLS) | set(P.ACTIONS)
    missing = [n for n in action_names if n not in listed]
    check("hard.actions.registered", not missing, str(missing))

    _, static = _static_tools()
    unlisted = [n for n in static if n not in listed and n != "mcp"]
    check("hard.statics.registered", not unlisted, str(unlisted))

    for t in ("web_search", "web_fetch", "weather_report", "flight_finder"):
        d = P.check(t, {})
        check(f"hard.{t}.free", d.tier == "read" and not d.needs_approval,
              f"tier={d.tier} approval={d.needs_approval}")
    for t, a in (("send_message", {}), ("file_controller", {"action": "delete"}),
                 ("browser_control", {"action": "fill_form"}),
                 ("email", {"action": "send"}),
                 ("vercel", {"action": "redeploy"}),
                 ("github", {"action": "comment"}),
                 ("hf", {"action": "restart"})):
        d = P.check(t, a)
        check(f"hard.{t}.asks", d.needs_approval, f"tier={d.tier}")


def test_declared_tools_are_dispatched():
    """A declared tool with no handler is a guaranteed failure."""
    src, declared = _static_tools()
    handled = set()

    class V(ast.NodeVisitor):
        def visit_Compare(self, node):
            left = node.left
            if isinstance(left, ast.Name) and left.id == "name":
                for op, cmp_ in zip(node.ops, node.comparators):
                    if isinstance(op, ast.Eq) and isinstance(cmp_, ast.Constant) \
                            and isinstance(cmp_.value, str):
                        handled.add(cmp_.value)
                    if isinstance(op, ast.In):
                        items = cmp_.elts if isinstance(cmp_, ast.Tuple) else [cmp_]
                        for e in items:
                            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                                handled.add(e.value)
            self.generic_visit(node)

    V().visit(ast.parse(src))
    prefixes = set(re.findall(r'elif name\.startswith\("([^"]+)"\)', src))
    orphan = [d for d in declared
              if d not in handled and not any(d.startswith(p) for p in prefixes)]
    check("hard.no_orphan_tools", not orphan, str(orphan))
    check("hard.no_dupes", len(declared) == len(set(declared)),
          str([n for n in set(declared) if declared.count(n) > 1]))


# ── "it does not know what it can do" ────────────────────────────────────────

def _connector_block():
    src = _src("main.py")
    i = src.index("def _connector_block")
    j = src.index("def _describe_tools")
    ns = {}
    exec(compile(src[i:j], "<t>", "exec"), ns)
    return ns["_connector_block"](), src


def test_it_knows_its_own_systems():
    body, src = _connector_block()
    for probe in ("YOUR OWN SYSTEMS", "Gmail", "Google Calendar", "GitHub",
                  "Vercel", "Hugging Face", "MCP servers",
                  "Never substitute a web search"):
        check(f"hard.aware.{probe[:20]}", probe in body, body[:100])
    check("hard.aware.in_prompt", "parts.append(_con)" in src,
          "the block is built but never given to the model")


def test_it_knows_where_it_is_running():
    body, src = _connector_block()
    check("hard.env.block_present", "WHERE YOU ARE RUNNING" in body, body[:90])
    check("hard.env.in_prompt", "parts.append(_con)" in src)

    from actions import screen_processor as SP
    had = os.environ.pop("DISPLAY", None)
    had_w = os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        check("hard.env.detects_headless", SP.headless() is True)
        try:
            SP._capture_screen()
            check("hard.env.screen_guarded", False, "no error raised")
        except RuntimeError as e:
            check("hard.env.screen_guarded", "no screen" in str(e).lower(), str(e)[:80])
            check("hard.env.no_raw_x11", "$display" not in str(e).lower(), str(e)[:80])
    finally:
        if had:
            os.environ["DISPLAY"] = had
        if had_w:
            os.environ["WAYLAND_DISPLAY"] = had_w


def test_screen_error_is_human():
    """The bug: 'Cannot connect to display: display is unset or invalid'."""
    from actions import screen_processor as SP
    had = os.environ.pop("DISPLAY", None)
    had_w = os.environ.pop("WAYLAND_DISPLAY", None)
    try:
        try:
            SP._capture_screen()
            text = ""
        except RuntimeError as e:
            text = str(e)
        check("hard.screen.speaks_english", "running on a server" in text, text[:90])
        check("hard.screen.offers_alternative", "read" in text.lower(), text[:90])
        check("hard.screen.no_x11_jargon", "display" not in text.lower(), text[:90])
    finally:
        if had:
            os.environ["DISPLAY"] = had
        if had_w:
            os.environ["WAYLAND_DISPLAY"] = had_w


# ── "it claimed it could do something it cannot" ─────────────────────────────

def test_empty_results_are_not_silent():
    src = _src("main.py")
    i = src.index("def _empty_result_note")
    j = src.index("def _describe_tools")
    ns = {}
    exec(compile(src[i:j], "<t>", "exec"), ns)
    f = ns["_empty_result_note"]
    out = f("weather_report", "")
    check("hard.empty.explains", "returned nothing" in out, out[:90])
    check("hard.empty.tells_the_model", "Do not report success" in out, out[:130])
    check("hard.empty.keeps_real_text", f("weather_report", "18C") == "18C")
    check("hard.empty.keeps_dict", f("calendar", {"a": 1}) == {"a": 1})
    check("hard.empty.keeps_list", f("x", [1, 2]) == [1, 2])
    check("hard.empty.handles_none", "returned nothing" in f("github", None))
    check("hard.empty.wired", "_empty_result_note(name, result)" in src)


# ── "it broke on startup" ────────────────────────────────────────────────────

def test_no_unimported_annotations():
    """main.py has no `from __future__ import annotations`, so an annotation that
    names a type the module never imported is a NameError at import time. The
    whole assistant dies and the user sees a blank dashboard.

    This exact class shipped once while hardening: the guard function was
    annotated `result: Any`, `Any` was never imported, and both the app and the
    two suites that boot a server went down together.
    """
    import builtins
    for mod in ("main.py", "dashboard/server.py"):
        src = _src(mod)
        tree = ast.parse(src)
        known = set(dir(builtins))
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    known.add((a.asname or a.name).split(".")[0])
            if isinstance(n, (ast.Assign, ast.AnnAssign)):
                tgt = n.targets[0] if isinstance(n, ast.Assign) else n.target
                if isinstance(tgt, ast.Name):
                    known.add(tgt.id)
        future = "from __future__ import annotations" in src
        missing = set()
        for n in ast.walk(tree):
            anns = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = list(n.args.args) + list(n.args.kwonlyargs)
                anns = [a.annotation for a in args if a.annotation]
                if n.returns:
                    anns.append(n.returns)
                va = n.args.vararg
                if va is not None and getattr(va, "annotation", None) is not None:
                    anns.append(va.annotation)
            for a in anns:
                for x in ast.walk(a):
                    if isinstance(x, ast.Name) and x.id not in known:
                        missing.add(x.id)
        if future:
            # annotations are strings, so an unknown name is harmless
            continue
        check(f"hard.annotations.{mod}", not missing, str(sorted(missing)))


def test_policy_is_importable_alone():
    """policy.py must not need main.py to answer a question. The gate runs on
    the hot path of every tool call, including from the dashboard."""
    import subprocess
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'.');"
         "from core import policy as P;"
         "d = P.check('web_search', {});"
         "print(d.tier, d.needs_approval)"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    check("hard.policy.standalone", r.returncode == 0 and "read False" in r.stdout,
          (r.stdout + r.stderr)[-140:])


if __name__ == "__main__":
    for fn in (test_every_tool_is_registered, test_declared_tools_are_dispatched,
               test_it_knows_its_own_systems, test_it_knows_where_it_is_running,
               test_screen_error_is_human, test_empty_results_are_not_silent,
               test_no_unimported_annotations, test_policy_is_importable_alone):
        try:
            fn()
        except Exception as e:
            import traceback
            check(fn.__name__, False, f"raised {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\nPASS {COUNT - len(FAILS)}  FAIL {len(FAILS)}")
    for f in FAILS:
        print(f"  FAIL  {f}")
    sys.exit(1 if FAILS else 0)
