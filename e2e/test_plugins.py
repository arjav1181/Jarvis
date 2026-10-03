"""Adding a capability without a release.

The plugin contract was always tiny — a PLUGIN dict and a run() function — and
always correct. What was missing was the route there: discovery scanned one
directory, once, at startup, so a plugin meant a commit and a rebuild, and
editing core looked like the faster path. These tests cover the way in, and
they care most about the failure modes, because this surface accepts code.

The properties that matter, in order of how much they would hurt:

    A broken plugin is refused and NOT kept. Writing a file that cannot load,
    and then reporting success, is how a plugin directory fills with ghosts
    that fail on every boot with nobody knowing which one broke.

    A rejected plugin never takes the assistant with it. Discovery swallows
    import errors per file, and that has to stay true.

    A plugin cannot take a name that is already taken, cannot shadow a core
    tool, and cannot shadow a reviewed repository plugin from the volume.

    The filename is not a path. It comes from an upload, and `../../etc/x` is
    an ordinary string to receive from a form.

    Every route is behind the token, because installing a plugin is remote
    code execution by design and that must not be reachable anonymously.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="jarvis-test-plugins-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def good_plugin(name="uptime", desc="How long it has been up."):
    return (f'PLUGIN = {{\n    "name": "{name}",\n'
            f'    "description": "{desc}",\n'
            f'    "parameters": {{"type": "OBJECT", "properties": {{}}}},\n}}\n\n\n'
            f'def run(parameters, player=None, session_memory=None):\n'
            f'    return "{name} works"\n')


# ── the loader now looks in two places ───────────────────────────────────────

def test_two_directories_are_scanned():
    from core import plugins as P
    dirs = P.dirs()
    check("plugins.repo_dir", dirs and dirs[0].name == "plugins",
          f"the repository must be scanned first, got {[d.name for d in dirs]}")
    check("plugins.volume_dir",
          len(dirs) > 1 and dirs[1].name == "user_plugins",
          f"the persistent volume is missing: {[str(d) for d in dirs]}")
    check("plugins.volume_is_persistent",
          str(dirs[1]).startswith(str(Path(_TMP))) if len(dirs) > 1 else False,
          f"the runtime dir is not under the data root: {dirs[1] if len(dirs)>1 else '-'}")


def test_a_plugin_in_the_volume_is_discovered():
    from core import plugins as P
    from core.plugin_loader import discover_plugins
    P.install(good_plugin("vol_one"), "vol_one.py")
    reg = discover_plugins(plugins_dir=P.repo_dir(), core_tool_names=set(),
                           logger=lambda m: None,
                           extra_dirs=[P.user_dir()])
    names = {d.get("name") for d in reg.list_for_ui() if not d.get("error")}
    check("plugins.volume_plugin_found", "vol_one" in names,
          f"a plugin installed at runtime was not discovered: {names}")
    check("plugins.repo_plugins_still_found",
          {"github_ops", "homelab"} & names != set(),
          f"the repository plugins disappeared: {names}")
    # and it is callable
    r = reg.run("vol_one", {})
    check("plugins.volume_plugin_runs", "works" in str(r), f"run() returned {r!r}")


def test_the_repository_wins_a_name_clash():
    """A runtime plugin must not be able to quietly replace a reviewed one."""
    from core import plugins as P
    from core.plugin_loader import discover_plugins
    # homelab already exists in the repository. Written straight to disk rather
    # than installed, because install() now correctly refuses this by name —
    # and the loader still has to be robust if such a file arrives by any other
    # route, which is exactly what this proves.
    (P.user_dir() / "homelab.py").write_text(
        good_plugin("homelab", "a hijack attempt"), encoding="utf-8")
    reg = discover_plugins(plugins_dir=P.repo_dir(), core_tool_names=set(),
                           logger=lambda m: None,
                           extra_dirs=[P.user_dir()])
    clash = [d for d in reg.list_for_ui() if d.get("name") == "homelab"]
    errors = [d for d in clash if d.get("error")]
    check("plugins.clash_rejected", bool(errors),
          "a runtime plugin silently took a repository plugin's name")
    if errors:
        check("plugins.clash_says_which", "homelab.py" in errors[0]["error"],
              f"the clash does not name the other file: {errors[0]['error']!r}")
    # the repository one is the one that is live
    r = reg.run("homelab", {})
    check("plugins.repo_version_wins", "hijack" not in str(r),
          f"the runtime version won: {r!r}")


def test_a_core_tool_name_is_refused():
    from core import plugins as P
    from core.plugin_loader import discover_plugins
    P.install(good_plugin("computer", "hijack"), "computer.py")
    reg = discover_plugins(plugins_dir=P.repo_dir(), core_tool_names={"computer"},
                           logger=lambda m: None,
                           extra_dirs=[P.user_dir()])
    bad = [d for d in reg.list_for_ui()
           if d.get("file") == "computer.py" and d.get("error")]
    check("plugins.core_name_refused", bool(bad),
          "a plugin took the name of a core tool")
    if bad:
        check("plugins.core_name_says_why", "core tool" in bad[0]["error"],
              f"unclear reason: {bad[0]['error']!r}")


# ── nothing broken is ever kept ──────────────────────────────────────────────

def test_a_broken_plugin_is_refused_and_not_written():
    from core import plugins as P
    cases = {
        "syntax": "def run(:\n",
        "no_plugin_dict": "def run(p):\n    return 'x'\n",
        "bad_name": 'PLUGIN = {"name": 42, "description": "x",'
                    ' "parameters": {"type": "OBJECT"}}\n'
                    'def run(p):\n    return "x"\n',
        "no_description": 'PLUGIN = {"name": "ok", "parameters":'
                          ' {"type": "OBJECT"}}\ndef run(p):\n    return "x"\n',
        "no_run": 'PLUGIN = {"name": "ok", "description": "d",'
                  ' "parameters": {"type": "OBJECT"}}\n',
        "empty": "",
        "import_error": "import a_module_that_does_not_exist_xyz\n"
                        'PLUGIN = {"name": "ok", "description": "d",'
                        ' "parameters": {"type": "OBJECT"}}\n'
                        'def run(p):\n    return "x"\n',
    }
    d = P.user_dir()
    for label, src in cases.items():
        r = P.install(src, f"broken_{label}.py")
        check(f"plugins.refuses.{label}", not r.get("ok"),
              f"it accepted a broken plugin: {r}")
        check(f"plugins.refuses.{label}.not_written",
              not (d / f"broken_{label}.py").exists(),
              "a refused plugin was written anyway")
        if not r.get("ok"):
            check(f"plugins.refuses.{label}.says_why", bool(r.get("error")),
                  "refused with no reason given")


def test_a_plugin_that_raises_at_import_is_refused():
    from core import plugins as P
    src = ('raise ValueError("boom at import")\n'
           'PLUGIN = {"name": "boom", "description": "d",'
           ' "parameters": {"type": "OBJECT"}}\n'
           'def run(p):\n    return "x"\n')
    r = P.install(src, "boom.py")
    check("plugins.import_boom_refused", not r.get("ok"),
          f"a plugin that raised at import was accepted: {r}")
    check("plugins.import_boom_not_written",
          not (P.user_dir() / "boom.py").exists(), "it was written anyway")
    check("plugins.import_boom_located", "boom at import" in str(r.get("error")),
          f"the reason did not survive: {r.get('error')!r}")


def test_a_good_plugin_is_kept_and_usable():
    from core import plugins as P
    r = P.install(good_plugin("keeper"), "keeper.py")
    check("plugins.keeps_good", r.get("ok") and r.get("name") == "keeper",
          f"{r}")
    f = P.user_dir() / "keeper.py"
    check("plugins.wrote_it", f.is_file() and f.stat().st_size > 50, "not written")
    check("plugins.no_part_file_left",
          not list(P.user_dir().glob(".*.part")),
          "a temporary file was left behind")
    # installing the same name twice is refused, not silently overwritten
    r2 = P.install(good_plugin("keeper", "different"), "keeper.py")
    check("plugins.no_silent_overwrite", not r2.get("ok"),
          f"it overwrote a plugin without saying so: {r2}")


def test_a_repository_plugin_cannot_be_shadowed_from_the_volume():
    """Installing a copy of something the repository already ships looks like
    it worked and then loses at every scan, with a collision message the user
    has to decode. Refuse it up front, by name."""
    from core import plugins as P
    r = P.install(good_plugin("homelab", "a copy"), "homelab_copy.py")
    check("plugins.repo_name_refused_on_install", not r.get("ok"),
          f"a copy of a repository plugin was accepted: {r}")
    check("plugins.refusal_names_the_file",
          "homelab.py" in str(r.get("error", "")),
          f"the refusal does not say which file: {r.get('error')!r}")
    check("plugins.refusal_not_written",
          not (P.user_dir() / "homelab_copy.py").exists(),
          "the losing copy was written anyway")
    check("plugins.name_lookup_works",
          P._name_taken_in_repo("homelab") == "homelab.py",
          f"repo name lookup failed: {P._name_taken_in_repo('homelab')!r}")
    check("plugins.name_lookup_free_name",
          P._name_taken_in_repo("definitely_not_a_plugin") == "",
          "a free name was reported as taken")


def test_removing():
    from core import plugins as P
    P.install(good_plugin("temporary"), "temporary.py")
    r = P.remove("temporary.py")
    check("plugins.removes", r.get("ok"), f"{r}")
    check("plugins.gone", not (P.user_dir() / "temporary.py").exists(), "still there")
    r2 = P.remove("temporary.py")
    check("plugins.remove_twice_is_safe", not r2.get("ok"), f"{r2}")
    # A repository plugin is NOT deletable from here. Pick one that has no
    # runtime twin, because an earlier test deliberately installs a runtime
    # homelab.py to prove the repository wins the clash — and removing THAT one
    # is correct, so asserting on it would be asserting the wrong thing.
    have_runtime = {p.name for p in P.user_dir().glob("*.py")}
    repo_only = next(p for p in sorted(P.repo_dir().glob("*.py"))
                     if not p.name.startswith("_") and p.name not in have_runtime)
    r3 = P.remove(repo_only.name)
    check("plugins.repo_not_deletable", not r3.get("ok"),
          f"it claimed to delete the repository plugin {repo_only.name}: {r3}")
    check("plugins.repo_still_there", repo_only.is_file(),
          f"a repository plugin was deleted: {repo_only.name}")
    check("plugins.repo_refusal_explains",
          "repository" in str(r3.get("error", "")).lower(),
          f"unclear refusal: {r3.get('error')!r}")
    # and the removal path is structurally confined to the volume
    import re as _re
    src = _src("core/plugins.py")
    i = src.index("def remove(")
    m = _re.search(r"\ndef ", src[i + 5:])
    body = src[i:i + 5 + (m.start() if m else len(src))]
    check("plugins.remove_only_touches_volume", "user_dir()" in body
          and "repo_dir()" not in body,
          "remove() can reach outside the runtime directory")


# ── the filename is not a path ───────────────────────────────────────────────

def test_filenames_cannot_escape():
    from core import plugins as P
    hostile = ["../../etc/passwd", "..\\\\..\\\\windows\\\\system32",
               "/etc/shadow", "....//....//x", "a/../../b"]
    for name in hostile:
        safe = P.safe_name(name)
        check(f"plugins.safe_name.{name[:12]}", "/" not in safe and "\\" not in safe
              and ".." not in safe, f"{name!r} became {safe!r}")
    # and installing one actually stays inside the directory
    before = sorted(p.name for p in P.user_dir().iterdir())
    P.install(good_plugin("escaped"), "../../escaped.py")
    after = sorted(p.name for p in P.user_dir().iterdir())
    check("plugins.install_stays_inside",
          all("/" not in n and "\\" not in n for n in after),
          f"a file landed outside the directory: {set(after) - set(before)}")
    check("plugins.escape_actually_wrote", any("escaped" in n for n in after),
          f"the install did not happen at all: {after}")
    check("plugins.nothing_above_the_dir", not (P.user_dir().parent / "escaped.py").exists(),
          "a file was written one level above the plugins directory")


def test_only_python_files():
    from core import plugins as P
    for ext in (".txt", ".sh", ".exe", ".json", ""):
        r = P.install(good_plugin("x"), f"thing{ext}")
        check(f"plugins.rejects_ext{ext or '_none'}",
              not r.get("ok") or ext == ".py",
              f"a {ext or 'extensionless'} file was accepted: {r}")
    r = P.install("x" * (P.MAX_PLUGIN_BYTES + 10), "huge.py")
    check("plugins.rejects_huge", not r.get("ok"), f"accepted: {r}")


# ── the surface is authenticated ─────────────────────────────────────────────

def test_every_plugin_route_is_behind_the_token():
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    srv._tokens = {"t"}
    with TestClient(srv.app) as c:
        routes = [("get", "/api/plugins", {}),
                  ("post", "/api/plugins", {"json": {"source": "x"}}),
                  ("post", "/api/plugins/validate", {"json": {"source": "x"}}),
                  ("post", "/api/plugins/remove", {"json": {"file": "x.py"}}),
                  ("post", "/api/plugins/reload", {})]
        for method, path, kw in routes:
            r = getattr(c, method)(path, **kw)
            check(f"plugins.auth.{path.split('/')[-1] or 'list'}",
                  r.status_code == 401,
                  f"{method.upper()} {path} answered {r.status_code} with no token")
        # and they work WITH the token
        r = c.get("/api/plugins", headers={"Authorization": "Bearer t"})
        check("plugins.list_with_token", r.status_code == 200, f"{r.status_code}")
        d = r.json()
        check("plugins.list_shape",
              "files" in d and "active" in d and "installable" in d,
              f"unexpected payload keys: {sorted(d)}")


def test_reload_without_a_live_app_says_so():
    """Reloading swaps the registry the live app reads each turn. With no live
    app there is nothing to swap, and pretending otherwise would leave the user
    thinking a plugin is live when it is not."""
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    srv._tokens = {"t"}
    with TestClient(srv.app) as c:
        r = c.post("/api/plugins/reload", headers={"Authorization": "Bearer t"})
        d = r.json()
        check("plugins.reload_explains", not d.get("ok") and "not running" in
              str(d.get("error", "")), f"{d}")


def test_installing_reloads_when_a_live_app_exists():
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    srv._tokens = {"t"}
    calls = {"n": 0}

    class FakeLive:
        def reload_plugins(self):
            calls["n"] += 1
            return {"ok": True, "active": ["fresh"]}

    srv.bind_live(FakeLive())
    with TestClient(srv.app) as c:
        r = c.post("/api/plugins", headers={"Authorization": "Bearer t"},
                   json={"source": good_plugin("fresh"), "filename": "fresh.py"})
        d = r.json()
        check("plugins.install_ok", d.get("ok"), f"{d}")
        check("plugins.install_reloads", calls["n"] == 1,
              f"the registry was not reloaded ({calls['n']} times)")
        check("plugins.install_reports_reload",
              (d.get("reload") or {}).get("ok") is True, f"{d.get('reload')}")
        c.post("/api/plugins/remove", headers={"Authorization": "Bearer t"},
               json={"file": "fresh.py"})
        check("plugins.remove_reloads", calls["n"] == 2,
              "removing did not reload the registry")


def test_bind_live_is_the_only_way_in():
    """A method that is not there cannot be called, and a None live app cannot
    be mistaken for a working one."""
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    check("plugins.starts_unbound", getattr(srv, "_live", "missing") is None,
          "the server starts with something already bound")
    check("plugins.reload_helper_exists", hasattr(srv, "_reload_plugins"),
          "no reload helper on the server")
    r = srv._reload_plugins()
    check("plugins.reload_reports_unbound", not r.get("ok"), f"{r}")


def test_inventory_never_imports_anything():
    """A listing must not be the thing that crashes: it stats files and reads
    the name out of the text."""
    from core import plugins as P
    d = P.user_dir()
    (d / "not_python.txt").write_text("nothing here", encoding="utf-8")
    (d / "empty.py").write_text("", encoding="utf-8")
    (d / "weird.py").write_text("# no PLUGIN at all\n", encoding="utf-8")
    inv = P.inventory()
    check("plugins.inventory_safe", isinstance(inv.get("files"), list),
          f"inventory did not return a list: {inv}")
    names = {f["name"] for f in inv["files"]}
    check("plugins.inventory_includes_weird", "weird" in names,
          f"a plugin with no PLUGIN dict vanished from the listing: {names}")
    check("plugins.inventory_marks_runtime",
          all(f["runtime"] for f in inv["files"] if f["file"] == "weird.py"),
          "a volume plugin was not marked as runtime")


def _src(name):
    with open(ROOT / name, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


if __name__ == "__main__":
    for fn in (test_two_directories_are_scanned,
               test_a_plugin_in_the_volume_is_discovered,
               test_the_repository_wins_a_name_clash,
               test_a_core_tool_name_is_refused,
               test_a_broken_plugin_is_refused_and_not_written,
               test_a_plugin_that_raises_at_import_is_refused,
               test_a_good_plugin_is_kept_and_usable,
               test_a_repository_plugin_cannot_be_shadowed_from_the_volume,
               test_removing,
               test_filenames_cannot_escape, test_only_python_files,
               test_every_plugin_route_is_behind_the_token,
               test_reload_without_a_live_app_says_so,
               test_installing_reloads_when_a_live_app_exists,
               test_bind_live_is_the_only_way_in,
               test_inventory_never_imports_anything):
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