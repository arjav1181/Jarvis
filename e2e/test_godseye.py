"""E2E — Phase 4f part two: the real God's Eye View, vendored and driven.

What makes this suite different from test_globe.py: there is no mock-up here.
The actual upstream project is vendored at a pinned commit, served from our own
mount, its own Node server is proxied, and a real browser drives its real
command vocabulary — the same one its OpenAI voice uses, reached through
`runner` because `actionExecutor` refuses everything when there is no voice
session. If any layer of that chain breaks, this goes red.

Two things it refuses to accept as "working":
  * an action that returns {ok: true} but changed nothing visible — so the
    camera is read back with get_current_view_state and compared;
  * a black frame — a WebGL canvas that never painted compresses to almost
    nothing, so the screenshot size is asserted.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3108")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_godseye"
LOG = Path("/tmp/jarvis_e2e_godseye.log")
CHROMIUM = os.environ.get("JARVIS_E2E_CHROMIUM") or "/repl/tools/bin/chromium"
WEBGL = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
         "--ignore-gpu-blocklist"]

GODSEYE_APP = (ROOT / "godseye" / "_server" / "dist")

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        show = detail if detail and any(c.isdigit() for c in str(detail)) else ""
        print(f"  PASS  {name}" + (f" — {show}" if show else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {str(detail)[:150]}", flush=True)


# ── the vendored artifact ────────────────────────────────────────────────────

def vendored() -> None:
    print("== vendored build ==", flush=True)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("bg", ROOT / "scripts" / "build_godseye.py")
    bg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bg)
    from core import godseye as G

    check("vendor.app_present", G.available(), str(G.GODSEYE_DIR))
    check("vendor.dist_in_place",
          (G.SERVER_DIR / "dist" / "index.html").exists(),
          "vite preview serves from _server/dist; anywhere else 404s every asset")
    check("vendor.server_sources", (G.SERVER_DIR / "package-lock.json").exists())
    problems = bg.verify(G.GODSEYE_ROOT)
    check("vendor.build_verifies", not problems, problems)

    mf = G.manifest()
    check("vendor.pinned", mf.get("commit") == bg.PIN, mf.get("commit"))
    check("vendor.licensed_as_mit", "MIT" in str(mf.get("code_license")))
    check("vendor.noncommercial_flagged", "non-commercial" in str(mf.get("data_terms")),
          "the cable dataset is CC BY-NC-SA and that has to stay written down")
    check("vendor.manifest_has_vocabulary",
          len(G.actions()) == len(bg.ACTIONS) == 30 and len(G.layers()) == 29,
          f"{len(G.actions())} actions / {len(G.layers())} layers")

    # every action we can send must exist in the shipped bundle
    bundle = "".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in bg._browser_js(G.GODSEYE_ROOT))
    missing = [a for a in G.actions() if f'"{a}"' not in bundle]
    check("vendor.actions_present_in_bundle", not missing, missing[:5])
    check("vendor.bridge_entry_present", "__gevVoiceCommands" in bundle)

    # the /api rewrite, and the third-party URLs it must not have touched
    check("vendor.api_namespaced", '"/api/gev/' in bundle)
    leftover = re.findall(r'(?<![A-Za-z0-9.\-])/api/(?!gev/)', bundle)
    check("vendor.no_orphan_api_calls", not leftover, f"{len(leftover)} left")
    for host in bg.PROTECTED_HOSTS:
        check(f"vendor.protected_{host.split('.')[0]}", f"{host}/api/" in bundle)


def bridge() -> None:
    print("== bridge: intents -> their vocabulary ==", flush=True)
    from core import godseye as G

    try:
        G.cmd("self_destruct")
        check("bridge.rejects_unknown_action", False, "accepted")
    except ValueError:
        check("bridge.rejects_unknown_action", True)
    try:
        G.build_commands(style="disco")
        check("bridge.rejects_bad_style", False, "accepted")
    except ValueError:
        check("bridge.rejects_bad_style", True)
    check("bridge.unknown_layer_dropped", G.build_commands(show=["nope"]) == [])
    check("bridge.place_uses_their_geocoder",
          G.build_commands(place="Istanbul") ==
          [{"action": "fly_to_location", "args": {"query": "Istanbul"}}])
    check("bridge.coords_when_no_place",
          G.build_commands(lat=41.0, lon=29.0)[0]["args"] ==
          {"latitude": 41.0, "longitude": 29.0})
    cmds = G.build_commands(place="Tokyo", style="nvg", zoom="globe",
                            show=["flights", "satellites"], hide=["traffic"])
    acts = [c["action"] for c in cmds]
    check("bridge.batch_order", acts == ["set_visual_style", "zoom_to_globe",
                                         "fly_to_location",
                                         "set_layer_visibility",
                                         "set_layer_visibility",
                                         "set_layer_visibility"], acts)
    hidden = [c for c in cmds if c["args"].get("visible") is False]
    check("bridge.hide_preserved", hidden and hidden[0]["args"]["layerId"] == "traffic")
    d = G.describe(cmds)
    check("bridge.describe_speaks", "flying to it" in d and "Tokyo" not in d, d)
    check("bridge.describe_admits_dead_api",
          "not answering" in G.describe(cmds, live=False), G.describe(cmds, live=False))


# ── server ───────────────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_server() -> subprocess.Popen:
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} in use")
    if LOG.exists():
        LOG.unlink()
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"JARVIS_MODE": "server", "JARVIS_PORT": str(PORT),
                "JARVIS_DATA": str(DATA), "PYTHONUNBUFFERED": "1",
                "JARVIS_GODSEYE": "1"})
    proc = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT), env=env,
                            stdout=LOG.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    end = time.time() + 60
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/login", timeout=2) as r:
                if r.status < 500:
                    return proc
        except Exception:
            pass
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError("server did not start:\n" +
                       (LOG.read_text(errors="replace")[-1500:] if LOG.exists() else ""))


_tok: str | None = None


def _token() -> str:
    global _tok
    if _tok is None:
        req = urllib.request.Request(BASE + "/api/bootstrap-key", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            key = json.loads(r.read())["key"]
        with urllib.request.urlopen(f"{BASE}/auto-login?key={key}", timeout=30) as r:
            html = r.read().decode(errors="replace")
        _tok = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html).group(1)
    return _tok


def get(path: str, token: str | None = None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def served() -> None:
    print("== served: the app on our mount ==", flush=True)
    st, body, _ = get("/godseye/")
    html = body.decode(errors="replace")
    check("mount.index", st == 200 and "<!DOCTYPE html" in html, st)
    check("mount.base_is_ours",
          html.count("/godseye/") >= 2 and '"/cesium/' not in html,
          "assets must be referenced under our mount or they 404")
    refs = re.findall(r'(?:src|href)="(/godseye/[^"]+)"', html)
    bad = [r for r in refs if r.endswith((".png", ".svg", ".js", ".css"))
           and not r.endswith(("/logo.svg", "/mic.svg"))]
    for r in list(dict.fromkeys(refs))[:6]:
        code, _, _ = get(r)
        check(f"mount.asset{r[7:][:28]}", code == 200, code)
    st, js, _ = get("/godseye/assets/" + (re.search(r"assets/(index-[A-Za-z0-9_-]+\.js)", html)
                                           .group(1) if re.search(r"assets/(index-[A-Za-z0-9_-]+\.js)", html) else ""))
    check("mount.bundle_served", st == 200 and len(js) > 500_000, f"{st} {len(js)}B")

    # path traversal must not escape the vendored tree
    for evil in ("/godseye/../main.py", "/godseye/../../etc/passwd",
                 "/godseye/%2e%2e/main.py"):
        code, b, _ = get(evil)
        leaked = b"Jarvis" in b or b"root:" in b
        check(f"mount.blocks{evil[-18:]}", not leaked, f"{code} leaked={leaked}")

    # the data API is proxied, not reimplemented
    st, b, _ = get("/api/godseye")
    check("state.gated", st == 401, st)
    # the bug that shipped once: the godseye branch sat inside _onDisplayMsg,
    # which only ever runs for type=display, so nothing ever popped up
    app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    check("client.handler_defined", "function _onGodseyeMsg(m)" in app)
    check("client.dispatcher_registers_it",
          "if (m.type === 'godseye')" in app and "_onGodseyeMsg(m)" in app)
    check("client.not_nested_in_display",
          app.count("if (m.type === 'godseye')") == 1
          and "m.type === 'godseye') {" not in app,
          "a { branch means it is nested somewhere it cannot fire")
    check("client.button_present", 'id="gev-btn"' in app)
    check("client.keys_panel_present",
          'id="gk-in"' in app and "toggleGevKeys" in app
          and "/api/godseye/providers" in app)
    check("server.has_injector",
          "inject_map_providers" in (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8"))
    check("client.sandbox_allows_same_origin",
          "allow-scripts allow-same-origin" in app,
          "the parent must be able to call runner() on the frame")

    st, b, _ = get("/api/godseye", token=_token())
    state = json.loads(b) if b else {}
    check("state.endpoint", st == 200 and state.get("actions") == 30, state)
    check("state.reports_commit", str(state.get("commit", "")).startswith("b210ab0"),
          state.get("commit"))
    st, b, _ = get("/api/gev/cyclones")
    if state.get("api_alive"):
        # their route returns a wrapped NOAA document (schemaVersion/source/
        # attribution), not a bare storm list — assert the wrapper and the credit
        doc = {}
        try:
            doc = json.loads(b)
        except Exception:
            pass
        check("api.proxied_cyclones",
              st == 200 and doc.get("source", "").startswith("NOAA")
              and "attribution" in doc, f"{st} {len(b)}B {list(doc)[:4]}")
    else:
        check("api.503_when_node_down", st == 503, st)
    st, b, _ = get("/api/godseye/../../api/display")
    check("api.no_traversal", st in (400, 404), st)


# ── the real proof ───────────────────────────────────────────────────────────

def drive() -> None:
    print("== browser: JARVIS drives the real app ==", flush=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=CHROMIUM,
                              args=["--no-sandbox", "--disable-dev-shm-usage"] + WEBGL)
        pg = b.new_page(viewport={"width": 1400, "height": 880})
        errs: list[str] = []
        gev_api: list = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:90]))
        bare_api: list = []

        def _saw(r):
            u = r.url.split(str(PORT))[-1]
            if "/api/gev/" in u:
                gev_api.append((r.status, u[:44]))
            elif "/api/" in u and "/api/display" not in u and "/api/gev" not in u:
                bare_api.append((r.status, u[:44]))
        pg.on("response", _saw)
        try:
            pg.goto(f"{BASE}/godseye/", wait_until="load", timeout=120000)
            ready = False
            for _ in range(90):
                ready = pg.evaluate("() => !!(window.__gevVoiceCommands && window.__gevVoiceCommands.runner)")
                if ready:
                    break
                pg.wait_for_timeout(1000)
            check("drive.surface_reachable", ready, "no runner() on window.__gevVoiceCommands")
            if not ready:
                b.close()
                return
            check("drive.no_bare_api_calls", not bare_api, bare_api[:4])
            check("drive.api_calls_namespaced", all("/api/gev/" in u for _, u in gev_api),
                  gev_api[:4])

            # their runner cancels a camera move while another one is in flight,
            # and the app flies its own intro camera on boot — so let it settle
            # and put camera actions last, or we would be testing a race
            pg.wait_for_timeout(6000)
            res = pg.evaluate("""async () => {
              const V = window.__gevVoiceCommands, o = {};
              const run = async (k, a, args) => { try { o[k] = await V.runner(a, args); }
                catch(e){ o[k] = {err: String(e).slice(0,120)}; } };
              await run('style', 'set_visual_style', {style: 'nvg'});
              await run('layer', 'set_layer_visibility', {layerId: 'earthquakes', visible: true});
              const before = await V.runner('get_current_view_state', {});
              await run('globe', 'zoom_to_globe', {});
              await new Promise(r => setTimeout(r, 2500));
              await run('fly', 'fly_to_location', {query: 'Istanbul'});
              await run('bogus', 'set_layer_visibility', {layerId: 'not-a-layer', visible: true});
              await new Promise(r => setTimeout(r, 6000));
              const after = await V.runner('get_current_view_state', {});
              o.moved = {beforeLat: before.camera.latitude, afterLat: after.camera.latitude,
                         beforeH: Math.round(before.camera.heightM),
                         afterH: Math.round(after.camera.heightM),
                         beforeStyle: before.style, afterStyle: after.style};
              return o;
            }""")
            for key in ("fly", "style", "layer"):
                v = res.get(key) or {}
                check(f"drive.action_{key}", v.get("ok") is True and "err" not in v,
                      v.get("err") or v)
            # zoom_to_globe is allowed to come back cancelled: their runner
            # supersedes a camera move whenever one is already in flight, and
            # their own idle camera starts moves of its own. What matters is
            # that it did not throw and that the camera ended up somewhere new.
            g = res.get("globe") or {}
            check("drive.action_globe", "err" not in g and "action" in g, g)
            check("drive.no_voice_abort",
                  "Voice action cancelled" not in json.dumps(res),
                  "actionExecutor's guard leaked in — we must be on runner()")
            check("drive.their_validation_survives",
                  "Unknown data layer" in json.dumps(res.get("bogus", {})),
                  res.get("bogus"))
            mv = res.get("moved") or {}
            check("drive.camera_actually_moved",
                  abs((mv.get("afterLat") or 0) - (mv.get("beforeLat") or 0)) > 0.5
                  or abs((mv.get("afterH") or 0) - (mv.get("beforeH") or 0)) > 1000, mv)
            check("drive.view_state_readable",
                  isinstance(mv.get("afterLat"), (int, float)), mv)

            # and the path JARVIS actually uses: a {type:'godseye'} message
            dash = b.new_page(viewport={"width": 1280, "height": 800})
            dash.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
            dash.evaluate("(t) => sessionStorage.setItem('jarvis_token', t)", _token())
            dash.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=30000)
            dash.wait_for_timeout(2500)
            dash.evaluate("() => _onGodseyeMsg({commands: []})")
            dash.wait_for_timeout(2500)
            check("client.message_opens_the_globe",
                  dash.locator("#gev-take").count() == 1
                  and dash.locator("#gev-frame").count() == 1,
                  "the takeover did not appear from a godseye message")
            check("client.frame_points_at_the_app",
                  (dash.locator("#gev-frame").get_attribute("src") or "").endswith("/godseye/"),
                  dash.locator("#gev-frame").get_attribute("src"))
            dash.close()

            shot = "/tmp/godseye_e2e.png"
            pg.screenshot(path=shot)
            size = os.path.getsize(shot)
            check("drive.actually_painted", size > 120_000, f"{size} bytes of PNG")
            check("drive.no_page_errors", not errs, errs[:2])
        except Exception as e:
            check("drive.no_exception", False, f"{type(e).__name__}: {e}"[:150])
            b.close()


def injector() -> None:
    """The injection contract, asserted directly on the real vendored HTML.

    This used to be asserted over HTTP, where it was both racy (the save bounces
    their Node process) and unprovable when it failed — 6 bytes of nothing. The
    injector is module-level precisely so it can be tested here, in-process,
    with the actual file the Space ships.
    """
    print("== injector: the key contract, in-process ==", flush=True)
    from dashboard.server import inject_map_providers as f
    page = (GODSEYE_APP / "index.html").read_text(encoding="utf-8")
    check("inj.noop_when_unset", f(page, "", "") == page, "page was modified with no keys")
    out = f(page, "AIzaE2E-TEST-0000", "ion_test_0000")
    check("inj.google_present", "__GOOGLE_MAPS_API_KEY__" in out)
    check("inj.google_value_exact", '"AIzaE2E-TEST-0000"' in out)
    check("inj.google_before_the_bundle",
          out.find("__GOOGLE_MAPS_API_KEY__") < out.find("assets/index-"),
          "the app reads the global while loading, so it has to come first")
    check("inj.ion_after_cesium",
          out.find("defaultAccessToken") > out.find("cesium/Cesium.js"),
          "Ion token must be set once Cesium exists")
    check("inj.google_only_leaves_ion_out",
          f(page, "AIzaOnly", "").count("defaultAccessToken") == 0)
    # a value that would otherwise close the script tag early
    evil = f(page, 'k"></script><script>alert(1)</script>', "")
    check("inj.no_script_breakout",
          evil.count("__GOOGLE_MAPS_API_KEY__") == 1
          and "</script><script>alert" not in evil,
          "json.dumps alone does not escape < — the injector must")


def providers(tok: str) -> None:
    """A key must unlock the real thing at serve time, with no rebuild."""
    print("== providers: the photorealistic unlock ==", flush=True)
    import urllib.request as _u

    def post(path: str, body: dict):
        req = _u.Request(BASE + path, data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {tok}")
        try:
            with _u.urlopen(req, timeout=180) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    st, page, _ = get("/godseye/")
    check("prov.absent_by_default", st == 200 and b"__GOOGLE_MAPS_API_KEY__" not in page,
          f"a key leaked into the page with none configured ({len(page)}B)")

    st, d = post("/api/godseye/providers", {"google_maps_key": "AIzaE2E-TEST-0000",
                                           "cesium_ion_token": "ion_test_0000"})
    check("prov.saved", st == 200 and d.get("has_google") and d.get("has_ion"), d)
    check("prov.never_echoes_the_key",
          "AIzaE2E-TEST-0000" not in json.dumps(d) and d.get("google_masked", "").endswith("0000"),
          d.get("google_masked"))

    st, page, _ = get("/godseye/")
    check("prov.page_still_served", st == 200 and b"<!DOCTYPE html" in page,
          f"{st} {len(page)}B")

def main() -> int:
    vendored()
    bridge()
    proc = start_server()
    try:
        served()
        injector()
        providers(_token())
        drive()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    log = LOG.read_text(errors="replace") if LOG.exists() else ""
    check("boot.no_traceback", "Traceback (most recent call last)" not in log)
    check("boot.supervised_node", "God'sEye" in log or "God's Eye" in log,
          log[-300:] if "God" not in log else "")
    print()
    print(f"PASS {len(_passes)}  FAIL {len(_failures)}")
    for f in _failures:
        print(f"  x {f}")
    return 1 if _failures else 0


def _hard_exit(code: int) -> None:
    """Leave immediately. A suite that has printed its result can still hang in
    interpreter teardown (a Playwright browser, a thread that never joins), which
    is the difference between a 2-minute suite and a 15-minute one."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    _hard_exit(main())
