"""E2E — the display surface (model-authored pages, embeds, charts).

Usage:
  python3 e2e/test_display.py

The point of these checks is the sandbox. A model-written page is a guest on
this origin: it can script, draw, fetch and play, and it must NOT be able to
read the dashboard's token, its storage, or call an authenticated endpoint.
The frame attribute is asserted, the CSP is asserted, the page is fetched and
executed in a real browser, and the page is given every chance to escape.
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
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3105")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_display"
LOG = Path("/tmp/jarvis_e2e_display.log")
CHROMIUM = os.environ.get("JARVIS_E2E_CHROMIUM") or "/repl/tools/bin/chromium"
# must match DISP_SANDBOX in dashboard/static/app.html and SANDBOX_ATTR in core/display.py
DISP_SANDBOX = "allow-scripts allow-forms allow-modals allow-popups allow-downloads"

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        # on success only echo detail that carries data (counts, sizes); a
        # failure-reason string under PASS just confuses the next reader
        show = detail if detail and any(c.isdigit() for c in str(detail)) else ""
        print(f"  PASS  {name}" + (f" — {show}" if show else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}", flush=True)


# ── units ────────────────────────────────────────────────────────────────────

def unit_store() -> None:
    print("== unit: display store + sandbox ==", flush=True)
    os.environ["JARVIS_DATA"] = tempfile_dir()
    sys.path.insert(0, str(ROOT))
    from core import display as D

    dirty = ('<base href="https://evil.example/">'
             '<meta http-equiv="refresh" content="0;url=https://evil.example">'
             '<a href="x" target="_blank">go</a>'
             '<script>draw()</script><script src="https://cdn/x.js"></script>')
    clean = D.sanitize_html(dirty)
    check("sandbox.base_removed", "<base" not in clean, clean[:60])
    check("sandbox.meta_refresh_removed", "http-equiv" not in clean)
    check("sandbox.scripts_kept",
          "<script>draw()</script>" in clean and 'src="https://cdn/x.js"' in clean)
    check("sandbox.noopener_added", 'rel="noopener"' in clean)
    check("sandbox.attr_has_no_same_origin",
          "allow-same-origin" not in D.SANDBOX_ATTR
          and "allow-scripts" in D.SANDBOX_ATTR, D.SANDBOX_ATTR)

    h = D.save("html", title="Wiring: DHT11", html="<h1>DHT11</h1>",
               warning="resistor value inferred")
    # every model-written page is stored as a COMPLETE themed document, so the
    # model's own markup survives but can never drift off the dashboard palette
    page = D.html_of(h["id"])
    check("store.html_saved",
          h["id"].startswith("d-") and h["has_html"]
          and "<h1>DHT11</h1>" in page
          and page.lstrip().lower().startswith("<!doctype"), str(h)[:80])
    check("store.theme_injected_on_model_page",
          "--pri:#00d4ff" in page and "--bg:#00060a" in page)
    check("store.model_page_still_selfcontained",
          "<script" in D.html_of(D.save("html", title="S", html="<script>go()</script>")["id"]))
    u = D.save("url", title="Docs", url="https://example.com/x")
    c = D.save("chart", title="S", spec={"type": "bar", "data": [{"label": "a", "value": 1}]})
    img = D.save("image", title="P", url="data:image/png;base64,AAAA")
    txt = D.save("text", title="N", text="hello")
    kinds = sorted({r["kind"] for r in D.listing(limit=50)})
    check("store.all_kinds", kinds == ["chart", "html", "image", "text", "url"], kinds)
    for kind, kwargs, frag in [
            ("html", {"html": ""}, "no html"),
            ("url", {"url": "ftp://x"}, "http"),
            ("url", {"url": "javascript:alert(1)"}, "http"),
            ("chart", {"spec": None}, "spec"),
            ("image", {"url": "file:///etc/passwd"}, "data"),
            ("text", {"text": "  "}, "nothing"),
            ("nope", {}, "kind")]:
        try:
            D.save(kind, **kwargs)
            check(f"store.rejects_{kind}_{frag}", False, "accepted")
        except ValueError:
            check(f"store.rejects_{kind}_{frag}", True)

    # a pinned artifact must survive anything else being shown
    D.set_pinned(h["id"])
    for n in range(D.KEEP + 12):
        D.save("text", title=f"flood{n}", text="x")
    alive = [r for r in D.listing(limit=200) if r["id"] == h["id"]]
    check("store.pinned_survives_flood", len(alive) == 1, str(len(alive)))
    check("store.listing_bounded",
          len([r for r in D.listing(limit=200) if not r.get("pinned")]) <= D.KEEP)
    fresh = D.save("text", title="to delete", text="bye")
    check("store.delete", D.delete(fresh["id"]) and D.get(fresh["id"]) is None)
    check("store.delete_missing_false", D.delete("d-nope") is False)
    check("describe.speaks_warning", "check" in D.describe(h).lower()
          or "warning" in D.describe(h).lower(), D.describe(h))


def tempfile_dir() -> str:
    import tempfile
    return tempfile.mkdtemp(prefix="display_e2e_")


# ── server ───────────────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    # SO_REUSEADDR mirrors what uvicorn does; without it a TIME_WAIT socket
    # left by the previous run makes a bare bind() report a false "in use".
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_http(url: str, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _kill_stray() -> None:
    """Kill a leftover server on this port. Never match on our own command line.

    Matching a substring like b'test_display' would also match the shell that
    launched this file (its cmdline contains the path), which means the test
    kills its own parent. The only safe discriminator is the env var, which
    exists on the spawned server and nowhere else.
    """
    me = os.getpid()
    ancestors = set()
    p = me
    for _ in range(12):
        if p <= 1:
            break
        ancestors.add(p)
        try:
            p = int(open(f"/proc/{p}/stat").read().rsplit(") ", 1)[1].split()[1])
        except Exception:
            break
    tag = f"JARVIS_PORT={PORT}".encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me or pid in ancestors:
            continue
        try:
            env = open(f"/proc/{entry}/environ", "rb").read()
            exe = os.readlink(f"/proc/{entry}/exe")
        except Exception:
            continue
        if tag in env and "python" in exe:
            try:
                os.kill(pid, 9)
            except Exception:
                pass
    time.sleep(0.3)


def start_server() -> subprocess.Popen:
    _kill_stray()
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} in use")
    if LOG.exists():
        LOG.unlink()
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"JARVIS_MODE": "server", "JARVIS_PORT": str(PORT),
                "JARVIS_DATA": str(DATA), "PYTHONUNBUFFERED": "1"})
    proc = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT),
                            env=env, stdout=LOG.open("w"),
                            stderr=subprocess.STDOUT, start_new_session=True)
    if not _wait_http(f"{BASE}/login", 45):
        proc.kill()
        raise RuntimeError("server did not start:\n" +
                           (LOG.read_text(errors="replace")[-1500:] if LOG.exists() else ""))
    return proc


def api(path: str, method: str = "GET", body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            try:
                return r.status, (json.loads(raw) if raw else {})
            except Exception:
                return r.status, {"_raw": raw[:120].decode(errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except Exception:
            return e.code, {}


def get_token() -> str:
    st, key = api("/api/bootstrap-key", "POST")
    if st != 200 or not key.get("key"):
        raise RuntimeError(f"bootstrap failed {st}")
    with urllib.request.urlopen(f"{BASE}/auto-login?key={key['key']}", timeout=10) as r:
        html = r.read().decode(errors="replace")
    return re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html).group(1)


def server_checks(token: str) -> None:
    print("== server: /api/display ==", flush=True)

    for path, method, body in [("/api/display", "GET", None),
                               ("/api/display", "POST", {"kind": "text", "text": "x"}),
                               ("/api/display/d-x/data", "GET", None),
                               ("/api/display/close", "POST", {})]:
        st, _ = api(path, method, body, token=None)
        check(f"http.401{method}{path}", st == 401, str(st))

    st, d = api("/api/display", token=token)
    check("api.empty", st == 200 and d.get("items") == [], f"{st} {d}")

    # a model-authored page: the escape attempts are deliberate
    hostile = """<!DOCTYPE html><html><head><title>escape test</title></head><body>
<div id="out">pending</div>
<script>
  var got = {origin: String(location.origin), token: '(none)', cookie: '(none)', api: '(none)', top: '(none)'};
  (async () => {
    try { got.token = sessionStorage.getItem('jarvis_token') || '(empty)'; } catch (e) { got.token = 'THREW: ' + e.name; }
    try { got.cookie = document.cookie || '(empty)'; } catch (e) { got.cookie = 'THREW: ' + e.name; }
    try {
      var t = null; try { t = sessionStorage.getItem('jarvis_token'); } catch (e) {}
      var r = await fetch('/api/display', {headers: {'Authorization': 'Bearer ' + (t || 'x')}});
      got.api = 'status ' + r.status;
    } catch (e) { got.api = 'THREW: ' + e.name; }
    try { top.__probe = 1; got.top = 'REACHED'; } catch (e) { got.top = 'blocked: ' + e.name; }
    document.getElementById('out').textContent = 'RESULT ' + JSON.stringify(got);
  })();
</script></body></html>"""
    st, rec = api("/api/display", "POST",
                  {"kind": "html", "title": "escape test", "html": hostile,
                   "warning": "values inferred"}, token=token)
    aid = rec.get("id")
    check("api.html_saved", st == 201 and aid, f"{st} {str(rec)[:90]}")
    st, d = api(f"/api/display/{aid}/data", token=token)
    check("api.data_shape",
          st == 200 and d.get("kind") == "html" and "html" in d
          and d.get("warning") == "values inferred", f"{st} {sorted(d)[:6]}")

    # the page route is unauthenticated on purpose (it runs in an opaque-origin
    # frame) and must carry a CSP that forbids reaching back into the app
    st, hdrs, body = raw_get(f"/api/display/{aid}")
    csp = hdrs.get("content-security-policy") or ""
    check("page.served", st == 200 and b"escape test" in body, str(st))
    check("page.csp_present",
          "default-src" in csp and "script-src" in csp
          and "'unsafe-inline'" in csp, csp[:90])
    check("page.no_store", hdrs.get("cache-control") == "no-store", str(hdrs.get("cache-control")))

    st, lst = api("/api/display", token=token)
    check("api.listed", any(r["id"] == aid for r in lst.get("items", [])), str(lst)[:90])

    # pin / close
    st, r = api(f"/api/display/{aid}/pin", "POST", {"pinned": True}, token=token)
    check("api.pin", st == 200 and r.get("pinned") is True, f"{st} {str(r)[:70]}")
    st, r = api(f"/api/display/{aid}/pin", "POST", {"pinned": True}, token=None)
    check("api.pin_401", st == 401, str(st))

    # other kinds over HTTP
    st, c = api("/api/display", "POST",
                {"kind": "chart", "title": "Readings",
                 "spec": {"type": "bar", "unit": "C", "title": "temps",
                          "data": [{"label": "a", "value": 21}, {"label": "b", "value": 25}]}},
                token=token)
    check("api.chart_saved", st == 201 and c.get("kind") == "chart", f"{st} {str(c)[:70]}")
    st, u = api("/api/display", "POST",
                {"kind": "url", "title": "Docs", "url": "https://example.com/page"},
                token=token)
    check("api.url_saved", st == 201, f"{st} {str(u)[:70]}")
    st, r = api("/api/display", "POST",
                {"kind": "url", "url": "javascript:alert(1)"}, token=token)
    check("api.url_rejects_javascript", st == 400, str(st))
    st, r = api("/api/display", "POST", {"kind": "chart", "spec": "notadict"}, token=token)
    check("api.chart_rejects_junk", st == 400, str(st))

    st, _, _ = raw_get(f"/api/display/{c['id']}")
    check("page.chart_falls_back", st == 200, str(st))

    # the client must frame it without allow-same-origin
    with urllib.request.urlopen(f"{BASE}/", timeout=10) as r:
        page = r.read()
    check("client.panel_present", b"showDisplay()" in page and b"disp-ov" in page)
    check("client.sandbox_attr",
          b"allow-scripts" in page and b"allow-same-origin" not in
          page.split(b"DISP_SANDBOX")[1][:200], "")
    check("client.chart_renderer",
          b"_dispChart" in page and b"polyline" in page and b"<rect" in page)

    # and a real browser confirms the page cannot reach the app
    escape = browser_escape(aid, token)
    control = browser_escape(aid, token, DISP_SANDBOX + " allow-same-origin")
    check("browser.probe_actually_ran", "token" in escape, str(escape)[:120])
    check("browser.no_token_readable",
          escape.get("token") == "THREW: SecurityError", str(escape.get("token")))
    check("browser.no_cookie_readable",
          escape.get("cookie") == "THREW: SecurityError", str(escape.get("cookie")))
    # the control: identical probe, only allow-same-origin added. It MUST be
    # able to read the token -- that is what makes the assertions above real.
    check("browser.control_is_same_origin",
          control.get("token") == token and "REACHED" in str(control.get("top")),
          str(control)[:120])
    check("browser.api_call_blocked",
          "THREW" in str(escape.get("api")) or "status 4" in str(escape.get("api")),
          str(escape.get("api")))
    check("browser.cannot_reach_top", "blocked" in str(escape.get("top")),
          str(escape.get("top")))

    st, r = api("/api/display/close", "POST", {"id": aid}, token=token)
    check("api.close_one", st == 200, str(st))
    st, r = api("/api/display/close", "POST", {"id": aid}, token=token)
    check("api.close_gone_404", st == 404, str(st))
    st, r = api("/api/display/close", "POST", {}, token=token)
    check("api.close_all", st == 200, str(st))
    st, d = api("/api/display", token=token)
    check("api.empty_again", d.get("items") == [], str(d)[:80])

    log = LOG.read_text(errors="replace") if LOG.exists() else ""
    check("boot.no_traceback", "Traceback (most recent call last)" not in log)


def raw_get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def browser_escape(aid: str, token: str, sandbox: str = DISP_SANDBOX) -> dict:
    """Load the artifact in a frame with `sandbox`, and see what the page got.

    Note: Chrome does NOT report "null" from location.origin for an opaque
    frame -- it returns the frame's URL. Opacity is therefore asserted
    behaviourally (storage/cookie throw, top unreachable), and a same-origin
    control run proves those assertions can actually tell the two apart.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"origin": f"no playwright: {e}"}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROMIUM,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()
        try:
            page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=20000)
            page.evaluate("(t) => sessionStorage.setItem('jarvis_token', t)", token)
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
            # Same frame attributes the panel uses.
            page.evaluate(
                """([url, sandbox]) => {
                    const ov = document.createElement('div');
                    ov.id = 'x-sandbox-probe';
                    ov.style.cssText = 'position:fixed;inset:0;z-index:99999;background:#000';
                    const f = document.createElement('iframe');
                    f.setAttribute('sandbox', sandbox);
                    f.setAttribute('referrerpolicy', 'no-referrer');
                    f.style.cssText = 'width:100%;height:100%;border:0';
                    f.src = url;
                    ov.appendChild(f);
                    document.body.appendChild(ov);
                }""", [f"/api/display/{aid}", sandbox])
            frame = None
            for _ in range(60):
                for fr in page.frames:
                    if f"/api/display/{aid}" in (fr.url or ""):
                        frame = fr
                        break
                if frame:
                    break
                page.wait_for_timeout(250)
            if not frame:
                return {"origin": "no frame"}
            page.wait_for_timeout(1200)
            txt = frame.locator("#out").inner_text()
            m = re.search(r"RESULT (\{.*\})", txt)
            browser.close()
            return json.loads(m.group(1)) if m else {"raw": txt[:200]}
        except Exception as e:
            browser.close()
            return {"error": f"{type(e).__name__}: {e}"[:160]}


def takeover_checks(token: str) -> None:
    """What the user actually asked for: a display takes the screen.

    A real browser is logged in, an artifact is saved the way the model saves
    one, and the artifact must take the viewport by itself -- with the same
    sandbox the panel uses. Esc dismisses it. A background job (source=
    scheduler) must NOT take over: it only badges the button, so a 6am report
    cannot hijack a call.
    """
    print("== browser: display takes the screen ==", flush=True)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        check("takeover.playwright_available", False, str(e)[:80])
        return
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, executable_path=CHROMIUM,
                              args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = b.new_page(viewport={"width": 1280, "height": 800})
        try:
            page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=20000)
            page.evaluate("(t) => sessionStorage.setItem('jarvis_token', t)", token)
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)  # let the dashboard socket settle

            html = ("<!DOCTYPE html><body style='background:#071018;color:#8ff'>"
                    "<h1 id='h'>takeover</h1></body></html>")
            st, rec = api("/api/display", "POST",
                          {"kind": "html", "title": "Takeover", "html": html}, token=token)
            check("takeover.saved", st == 201, f"{st} {str(rec)[:60]}")
            _wait_id(page, "disp-take", 12000)
            check("takeover.opens_by_itself",
                  page.locator("#disp-take").count() == 1,
                  "overlay did not appear without any click")
            if page.locator("#disp-take").count():
                box = page.locator("#disp-take").bounding_box() or {}
                vh = box.get("height", 0) / 800
                check("takeover.fills_screen", vh > 0.95, f"{vh:.0%} of viewport")
                fr = page.locator("#disp-take iframe")
                check("takeover.iframe_present", fr.count() == 1, str(fr.count()))
                if fr.count():
                    sb = fr.get_attribute("sandbox") or ""
                    check("takeover.iframe_sandboxed",
                          "allow-scripts" in sb and "allow-same-origin" not in sb, sb)
                    inner = page.frames[-1]
                    h1 = ""
                    for _ in range(40):
                        try:
                            h1 = inner.locator("#h").inner_text(timeout=1500)
                            break
                        except Exception:
                            page.wait_for_timeout(250)
                    check("takeover.page_actually_rendered", "takeover" in h1, h1[:40])
                # Esc dismisses it
                page.keyboard.press("Escape")
                page.wait_for_timeout(700)
                check("takeover.esc_dismisses",
                      page.locator("#disp-take").count() == 0, "still open")

            # a second display comes back even though the last one was dismissed
            st, rec2 = api("/api/display", "POST",
                           {"kind": "chart", "title": "Leads", "spec": {
                               "type": "bar", "data": [{"label": "a", "value": 3},
                                                       {"label": "b", "value": 7}]}},
                           token=token)
            _wait_id(page, "disp-take", 12000)
            check("takeover.reopens_after_dismiss",
                  page.locator("#disp-take").count() == 1, str(st))
            if page.locator("#disp-take").count():
                check("takeover.chart_svg_drawn",
                      page.locator("#disp-take-stage svg rect").count() >= 2,
                      str(page.locator("#disp-take-stage svg rect").count()))
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)

            # a background job badges instead of taking over
            st, rec3 = api("/api/display", "POST",
                           {"kind": "text", "title": "Nightly", "text": "done",
                            "source": "scheduler"}, token=token)
            check("takeover.scheduler_saved", st == 201, f"{st} {str(rec3)[:50]}")
            page.wait_for_timeout(2500)
            check("takeover.scheduler_does_not_hijack",
                  page.locator("#disp-take").count() == 0,
                  "a background job took over the screen")
            check("takeover.scheduler_badges",
                  page.locator("#disp-badge").count() == 1
                  and page.locator("#disp-badge").is_visible(),
                  "no badge on the display button")
            # opening the panel clears the badge
            page.evaluate("showDisplay()")
            page.wait_for_timeout(1200)
            check("takeover.badge_clears_on_open",
                  page.locator("#disp-badge").count() == 0
                  or not page.locator("#disp-badge").is_visible(), "badge stuck")
            b.close()
        except Exception as e:
            check("takeover.no_exception", False, f"{type(e).__name__}: {e}"[:140])
            b.close()


def _wait_id(page, sel: str, timeout_ms: int) -> None:
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        if page.locator(f"#{sel}").count():
            return
        page.wait_for_timeout(250)


def main() -> int:
    unit_store()
    proc = start_server()
    try:
        # one-shot: the second bootstrap is refused, exactly as on the live Space
        token = get_token()
        server_checks(token)
        takeover_checks(token)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    print()
    print(f"PASS {len(_passes)}  FAIL {len(_failures)}")
    for f in _failures:
        print(f"  ✗ {f}")
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
