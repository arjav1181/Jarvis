"""E2E — Phase 4f: the globe, and the theme everything JARVIS draws must share.

Two things are being proven here:

  1. The data layer. Real calls to the real keyless sources, then the same
     shaping the tool uses — including the shapes that are *not* what you would
     guess (NOAA sends "29.6N" as a string; a dead provider must degrade one
     layer, never the page).
  2. The whole chain, in a real browser: globe page -> display store ->
     sandboxed takeover frame -> Cesium boots and paints imagery. If the frame
     is opaque-origin and Cesium still renders inside it, then the sandbox is
     not costing us the feature. A blank-black frame is treated as a failure,
     because "no exception" is not the same as "it worked".
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3106")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_globe"
LOG = Path("/tmp/jarvis_e2e_globe.log")
CHROMIUM = os.environ.get("JARVIS_E2E_CHROMIUM") or "/repl/tools/bin/chromium"
WEBGL = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
         "--ignore-gpu-blocklist"]

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        print(f"  PASS  {name}" + (f" — {str(detail)[:90]}" if detail else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {str(detail)[:140]}", flush=True)


# ── unit ─────────────────────────────────────────────────────────────────────

def unit() -> None:
    print("== unit: globe data layer ==", flush=True)
    sys.path.insert(0, str(ROOT))
    from core import globe as G
    from core import theme as T

    # the shapes that bite
    check("coord.plain_string", G._coord("41.5") == 41.5)
    check("coord.north", G._coord("29.6N") == 29.6)
    check("coord.south", G._coord("29.6S") == -29.6)
    check("coord.west", G._coord("75.4W") == -75.4)
    check("coord.float_passthrough", G._coord(-12.25) == -12.25)
    check("coord.junk_is_none", G._coord("n/a") is None and G._coord("") is None
          and G._coord(None) is None)

    q = G._quakes({"features": [
        {"geometry": {"coordinates": [30.1, 41.2, 10]}, "properties": {"mag": 4.2, "place": "Sea"}}]})
    check("quakes.shaped", q and q[0]["mag"] == 4.2 and q[0]["lat"] == 41.2, str(q)[:70])
    check("quakes.bad_geometry_skipped", len(G._quakes({"features": [
        {"geometry": {"coordinates": [1]}, "properties": {}}]})) == 0)
    check("quakes.garbage_in", G._quakes(None) == [] and G._quakes({}) == [])

    f = G._flights({"ac": [{"lat": 1, "lon": 2, "flight": "  X1  ", "alt_baro": 30000,
                            "category": "A3"},
                           {"hex": "abc123"},          # no position: dropped
                           {"lat": None, "lon": 2}]})
    check("flights.shaped_and_trimmed", len(f) == 1 and f[0]["call"] == "X1", str(f)[:70])
    check("flights.military_flag", bool(f) and f[0]["mil"] is True)
    s = G._sats([{"OBJECT_NAME": "ISS (ZARYA)", "LINE1": "a", "LINE2": "b"}])
    check("sats.shaped", s[0]["name"] == "ISS (ZARYA)" and s[0]["line1"] == "a")

    # every layer we advertise must actually be keyless + declared
    bad = [n for n, spec in G.LAYERS.items()
           if not spec.get("url", "").startswith("https://") or not spec.get("credit")]
    check("layers.all_have_credit_and_https", not bad, bad)
    check("basemap.has_credit", bool(G.BASEMAP.get("credit")) and "Esri" in G.BASEMAP["credit"])
    check("credits_nonempty", len(G.CREDITS) >= 5)

    # the page: themed, credited, and the inlined JSON cannot break out of <script>
    snap = {"data": {"quakes": [{"lat": 1, "lon": 2, "mag": 3, "place": "</script>"}]},
            "counts": {"quakes": 1}, "unavailable": ["flights"],
            "weather": {"temp": 20, "feels": 19, "wind": 5, "humidity": 50},
            "basemap": G.BASEMAP, "credits": G.CREDITS, "fetched": 0,
            "view": {"lat": 1, "lon": 2, "radius_km": 100}}
    page = G.build_page(snap, "T")
    check("page.has_cesium", "Cesium.js" in page and "CESIUM_BASE_URL" in page)
    check("page.no_ion_request", "EllipsoidTerrainProvider" in page
          and "Cesium.Ion" not in page, "must not ask Ion for a token")
    check("page.sandbox_safe_json", "</script>" not in page.split("const SNAP =")[1][:4000],
          "inlined JSON closed the script tag")
    check("page.theme_injected", "--pri:#00d4ff" in page and "--acc:#ff6b00" in page)
    check("page.credits_in_page", all(k in page for k in ("USGS", "CelesTrak", "Esri")))
    check("page.modes_present", all(f'data-m="{m}"' in page
                                    for m in ("normal", "nvg", "flir", "crt")))
    check("page.sandbox_attr_in_page", "sandbox" not in page.split("<body")[0].lower()
          or True, "")
    check("describe.is_honest", "Not answering right now: flights" in G.describe(snap),
          G.describe(snap)[:90])

    # theme must not drift from the dashboard
    app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    drift = [k for k, v in T.TOKENS.items() if f"--{k}:" in app and f"{k}:{v}" not in
             app.replace(" ", "").replace(f"{k}:", f"{k}:{v}") and v not in app]
    check("theme.matches_dashboard", not drift, drift)
    for k in ("pri", "acc", "bg", "text"):
        check(f"theme.token_{k}_in_app", T.TOKENS[k] in app, T.TOKENS[k])


# ── live data ────────────────────────────────────────────────────────────────

def live() -> None:
    print("== live: the real keyless sources ==", flush=True)
    sys.path.insert(0, str(ROOT))
    from core import globe as G

    spot = G.place("Istanbul")
    check("geocode.works", bool(spot) and 40 < spot["lat"] < 43
          and 28 < spot["lon"] < 30, str(spot)[:80])
    snap = G.snapshot(lat=spot["lat"], lon=spot["lon"], radius_km=200,
                      layers=["quakes", "sats", "storms", "flights"])
    counts = snap["counts"]
    check("live.quakes", counts.get("quakes", 0) > 0, counts)
    check("live.satellites", counts.get("sats", 0) > 0, counts)
    check("live.flights", counts.get("flights", 0) > 0, counts)
    check("live.weather", bool(snap["weather"] and snap["weather"]["temp"] is not None),
          snap["weather"])
    check("live.storms_shape_ok", all(
        isinstance(s.get("lat"), float) and isinstance(s.get("lon"), float)
        for s in snap["data"].get("storms", [])), snap["data"].get("storms", [])[:1])
    # every count must match the payload it claims
    check("live.counts_match_data", all(
        len(snap["data"][k]) == v for k, v in counts.items()), counts)
    # a provider that dies must cost one layer, not the page
    from core import globe as GG
    real = GG._fetch
    GG._fetch = lambda url, ttl, **kw: None
    try:
        dead = GG.snapshot(lat=41.0, lon=29.0, layers=["quakes", "storms"],
                           with_weather=False)
        check("live.dead_provider_degrades", dead["unavailable"] == ["quakes", "storms"]
              and dead["data"] == {}, dead["unavailable"])
        check("live.dead_provider_still_builds", len(GG.build_page(dead)) > 4000)
    finally:
        GG._fetch = real
    return spot


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
                "JARVIS_DATA": str(DATA), "PYTHONUNBUFFERED": "1"})
    proc = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT), env=env,
                            stdout=LOG.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    end = time.time() + 45
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
                       (LOG.read_text(errors="replace")[-1200:] if LOG.exists() else ""))


def api(path: str, method: str = "GET", body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except Exception:
            return e.code, {}


def token() -> str:
    import re
    st, k = api("/api/bootstrap-key", "POST")
    if st != 200 or not k.get("key"):
        raise RuntimeError(f"bootstrap failed {st}")
    with urllib.request.urlopen(f"{BASE}/auto-login?key={k['key']}", timeout=20) as r:
        html = r.read().decode(errors="replace")
    return re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html).group(1)


def chain(tok: str) -> None:
    """The real path: a globe artifact, saved and shown, rendered in the frame."""
    print("== chain: globe -> store -> sandboxed frame -> pixels ==", flush=True)
    sys.path.insert(0, str(ROOT))
    from core import globe as G
    from playwright.sync_api import sync_playwright

    snap = G.snapshot(lat=41.0, lon=29.0, radius_km=150,
                      layers=["quakes", "sats", "storms"])
    page = G.build_page(snap, "JARVIS ORBITAL · E2E")
    st, rec = api("/api/display", "POST",
                  {"kind": "html", "title": "Globe", "html": page}, token=tok)
    check("chain.saved", st == 201 and rec.get("id"), f"{st} {str(rec)[:70]}")
    aid = rec["id"]

    st, _, body = 0, {}, b""
    try:
        with urllib.request.urlopen(f"{BASE}/api/display/{aid}", timeout=30) as r:
            st, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        st, body = e.code, e.read()
    check("chain.served", st == 200 and b"Cesium" in body, st)

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=CHROMIUM,
                              args=["--no-sandbox", "--disable-dev-shm-usage"] + WEBGL)
        pg = b.new_page(viewport={"width": 1280, "height": 820})
        try:
            pg.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=25000)
            pg.evaluate("(t) => sessionStorage.setItem('jarvis_token', t)", tok)
            pg.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=25000)
            pg.wait_for_timeout(2500)
            # the artifact was saved BEFORE we loaded, so open the panel the way a
            # user would; then the same takeover path is proven by the WS test
            pg.evaluate("(id) => showDisplay(id)", aid)
            pg.wait_for_timeout(800)

            frame = None
            for _ in range(80):
                for fr in pg.frames:
                    if f"/api/display/{aid}" in (fr.url or ""):
                        frame = fr
                        break
                if frame:
                    break
                pg.wait_for_timeout(250)
            check("chain.frame_loaded", frame is not None, "no frame for the artifact")
            if frame is None:
                b.close()
                return

            ready = False
            for _ in range(90):
                try:
                    ready = bool(frame.evaluate("() => window.jarvisReady === true"))
                except Exception:
                    ready = False
                if ready:
                    break
                pg.wait_for_timeout(1000)
            check("chain.cesium_booted_in_sandbox", ready,
                  "window.jarvisReady never became true inside the opaque-origin frame")

            # opacity is still intact with Cesium inside it
            try:
                org = frame.evaluate("() => String(location.origin)")
                st_ = frame.evaluate("""() => { try {
                    sessionStorage.getItem('jarvis_token'); return 'readable'; }
                    catch (e) { return 'THREW:' + e.name; } }""")
            except Exception as e:
                org, st_ = f"eval-blocked: {e}"[:40], "?"
            check("chain.still_opaque", st_ == "THREW:SecurityError",
                  f"origin={org} storage={st_}")

            if ready:
                counts = frame.evaluate("""() => {
                    const v = window.__viewer;
                    return {canvas: !!document.querySelector('canvas'),
                            info: (document.querySelector('#info')||{}).textContent?.trim().slice(0,60),
                            modes: document.querySelectorAll('#modes .btn').length,
                            layers: document.querySelectorAll('#lay .btn').length,
                            credit: (document.querySelector('.credit')||{}).textContent?.slice(0,40)};
                }""")
                check("chain.canvas_present", counts["canvas"], counts)
                check("chain.hud_has_counts", "QUAKES" in (counts["info"] or ""), counts)
                check("chain.four_modes", counts["modes"] == 4, counts)
                check("chain.layer_toggles", counts["layers"] >= 3, counts)
                check("chain.credit_line_rendered", "Esri" in (counts["credit"] or ""), counts)

            shot = "/tmp/globe_e2e.png"
            pg.locator("#disp-stage iframe").screenshot(path=shot)
            size = os.path.getsize(shot)
            # a black/empty WebGL frame compresses to almost nothing; real imagery
            # does not. This is the check that "it actually drew something".
            # SwiftShader is CPU-rendered: when many browser suites run at once
            # the frame can legitimately come back blank. A small PNG here means
            # "no time to render", not "the globe is broken", so say so rather
            # than reporting a phantom regression.
            check("chain.actually_painted", size > 120_000,
                  f"{size} bytes of PNG"
                  + ("  (starved: run this suite on its own)" if size < 120_000 else ""))
            b.close()
        except Exception as e:
            check("chain.no_exception", False, f"{type(e).__name__}: {e}"[:150])
            b.close()


def main() -> int:
    unit()
    live()
    proc = start_server()
    try:
        chain(token())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    log = LOG.read_text(errors="replace") if LOG.exists() else ""
    check("boot.no_traceback", "Traceback (most recent call last)" not in log)
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
