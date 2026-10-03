"""Playwright E2E for the JARVIS server-mode dashboard.

Usage:
  python3 e2e/test_dashboard.py

Starts a clean DashboardServer on :3100, drives Chromium through
pairing → HUD → WS → metrics → command → settings, exits non-zero on failure.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3100")
BASE = f"http://127.0.0.1:{PORT}"
CHROMIUM = os.environ.get("JARVIS_E2E_CHROMIUM") or "/repl/tools/bin/chromium"
LOG = Path("/tmp/jarvis_e2e_server.log")

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}", flush=True)


def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _kill_stray_e2e() -> None:
    me = str(os.getpid())
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            env = open(f"/proc/{pid}/environ", "rb").read()
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
        except Exception:
            continue
        if b"JARVIS_PORT=3100" in env or b"test_dashboard" in cmd:
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass
    time.sleep(0.3)


def start_server() -> subprocess.Popen:
    _kill_stray_e2e()
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} already in use")
    if LOG.exists():
        LOG.unlink()
    env = os.environ.copy()
    env.update(
        {
            "JARVIS_MODE": "server",
            "JARVIS_PORT": str(PORT),
            "JARVIS_DATA": str(ROOT / "e2e" / "data"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    data = ROOT / "e2e" / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg = data / "config" / "api_keys.json"
    if cfg.exists():
        cfg.unlink()
    # Fresh auth state too: a password left by the previous run makes
    # set-password demand the current one (correct server behavior).
    pwf = data / "config" / "dashboard_auth.json"
    if pwf.exists():
        pwf.unlink()
    logf = LOG.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        cwd=str(ROOT),
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        # Its own session, so stop_server() can signal the whole tree. Without
        # this the child shares OUR process group and the group-kill in
        # stop_server signals the test itself, which is a hang with no output.
        start_new_session=True,
    )
    if not _wait_http(f"{BASE}/login", 30):
        tail = LOG.read_text(errors="replace")[-2000:] if LOG.exists() else ""
        proc.kill()
        raise RuntimeError("server did not come up:\n" + tail)
    return proc


def stop_server(proc) -> None:
    """Stop the server AND everything it started.

    The old version called `proc.terminate()` on the python process alone.
    But main.py spawns the globe's Node server, that child inherits the stdout
    pipe, and killing the parent does not close the pipe the child is holding.
    Anything reading it — including this test's own drain — then blocks
    forever, and the suite appears to hang after it has already printed its
    summary. That is exactly what was happening, and it left an orphan
    `npx vite` chain running after every run.

    Killing the whole process group fixes both halves: no orphan, and the
    pipe closes. `start_new_session=True` at spawn is what makes the group
    exist; without it there is no group to signal and we are back to orphans.
    """
    import os
    import signal
    mine = os.getpgrp()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            pgid = os.getpgid(proc.pid)
            # Never signal our own group. If the child did not get
            # start_new_session=True it inherited ours, and a group-kill would
            # terminate the test runner itself — a silent hang with no output,
            # which is far harder to diagnose than a plain leak.
            if pgid != mine:
                os.killpg(pgid, sig)
            else:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=6)
            return
        except Exception:
            continue


def _maybe_json(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw[:200].decode(errors="replace")}


def api(path: str, method: str = "GET", body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, _maybe_json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read())


def run_browser() -> dict:
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    page_errors: list[str] = []
    out: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=CHROMIUM,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on(
            "console",
            lambda m: console_errors.append(m.text) if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        # 1. Login page
        page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_selector("#key", timeout=8000)
        check("login.title", "JARVIS" in page.title(), page.title())
        check("login.key_input", page.locator("#key").count() == 1)
        check("login.connect_btn", page.locator("button:has-text('CONNECT')").count() >= 1)
        page.wait_for_selector("button:has-text('GET PAIRING KEY')", timeout=8000)
        check("login.pairing_btn", True)

        # 2. Pair via GET PAIRING KEY (mints + auto doLogin → location.href='/')
        page.click("button:has-text('GET PAIRING KEY')")
        try:
            page.wait_for_url(re.compile(rf"{re.escape(BASE)}/?$"), timeout=12000)
            check("login.redirect_home", True, page.url)
        except Exception:
            err = ""
            if page.locator("#err").count():
                err = page.locator("#err").inner_text()
            keyval = page.locator("#key").input_value() if page.locator("#key").count() else ""
            check("login.redirect_home", False, f"err={err!r} key={keyval!r} url={page.url}")
            raise RuntimeError("pairing navigation failed")

        # 3. HUD shell
        page.wait_for_selector("#pill", timeout=10000)
        html = page.content()
        for needle, label in [
            ("MARK LIV", "hud.brand"),
            ("SYS MONITOR", "hud.sys_monitor"),
            ('id="hud"', "hud.canvas"),
            ('id="feed"', "hud.feed"),
            ('id="log"', "hud.activity_log"),
        ]:
            check(label, needle in html)

        tok = page.evaluate("() => sessionStorage.getItem('jarvis_token')")
        key = page.evaluate("() => sessionStorage.getItem('jarvis_key')")
        out["token"] = tok or ""
        out["key"] = key or ""
        check("auth.token", bool(tok) and len(tok) > 20, f"len={len(tok or '')}")

        # 4. Status pill — server pushes status on WS join (or WS open sys line)
        page.wait_for_function(
            """() => {
              const t = document.getElementById('st');
              const txt = (t && t.textContent) || '';
              if (/Active|Sleeping|Failover|Reconnecting|Offline/i.test(txt)) return true;
              const feed = document.getElementById('feed');
              return !!(feed && /Remote session active/.test(feed.textContent||''));
            }""",
            timeout=12000,
        )
        st = page.locator("#st").inner_text()
        check("ws.status_pill", bool(st.strip()), st)

        # Observer socket — history replay + optional ping
        count = page.evaluate(
            """async (tok) => await new Promise((resolve) => {
              const ws = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(tok)}`);
              let n = 0, types = [];
              ws.onmessage = (e) => {
                try { const m = JSON.parse(e.data); types.push(m.type || '?'); n++; } catch {}
                if (types.includes('ping') || n >= 3) { try { ws.close(); } catch {} resolve({n, types}); }
              };
              ws.onerror = () => resolve({n, types, err: true});
              setTimeout(() => { try { ws.close(); } catch {} resolve({n, types, timeout:true}); }, 5000);
            })""",
            tok,
        )
        types = count.get("types") or []
        check(
            "ws.history_or_ping",
            not count.get("err"),
            f"n={count.get('n')} types={types[:8]}",
        )

        # 5. Metrics
        metrics = page.evaluate(
            """async (tok) => {
              const r = await fetch('/api/metrics', {headers:{Authorization:'Bearer '+tok}});
              return {status: r.status, body: await r.json().catch(()=>null)};
            }""",
            tok,
        )
        check("api.metrics_authed", metrics.get("status") == 200, str(metrics.get("status")))
        body = metrics.get("body") or {}
        check(
            "api.metrics_fields",
            all(k in body for k in ("cpu", "mem", "uptime")),
            str(sorted(body.keys()) if isinstance(body, dict) else body),
        )
        un = page.evaluate("async () => (await fetch('/api/metrics')).status")
        check("api.metrics_unauth_401", un == 401, str(un))

        # 6. Settings overlay — dismiss first-run setup (no Gemini key in e2e data)
        has_setup = page.locator("#setup-ov").count() > 0
        check("ui.setup_overlay_shown", has_setup, "expected on clean data")
        page.evaluate("() => { try { hideSetup(); } catch (_) {} }")
        page.wait_for_timeout(200)
        page.click("#cfg-btn")
        page.wait_for_selector("#cfg-ov", timeout=5000)
        check("ui.settings_open", page.locator("#cfg-ov").is_visible())
        page.locator("#cfg-ov button:has-text('CANCEL')").click()
        page.wait_for_timeout(200)
        closed = page.evaluate(
            """() => {
              const o = document.getElementById('cfg-ov');
              return !o || o.offsetParent === null || getComputedStyle(o).display === 'none';
            }"""
        )
        check("ui.settings_close", bool(closed))

        # 6b. Devices overlay (Phase 1 jarvisd panel) — the overlay JS only
        # runs on click, so syntax checks can't catch runtime errors here.
        check("ui.devices_button", page.locator("#dev-btn").count() == 1)
        page.locator("#dev-btn").click()
        page.wait_for_timeout(600)
        check("ui.devices_open", page.locator("#dev-ov").is_visible())
        dev_code = page.evaluate(
            "() => (document.getElementById('dev-code') || {}).value || ''")
        check("ui.devices_pair_code", len(dev_code) == 8, f"code={dev_code!r}")
        dev_cmd = page.locator("#dev-cmd").inner_text() if page.locator("#dev-cmd").count() else ""
        check("ui.devices_pair_cmd",
              "jarvisd.py" in dev_cmd and "--pair" in dev_cmd, dev_cmd)
        dev_list = page.locator("#dev-list").inner_text() if page.locator("#dev-list").count() else ""
        check("ui.devices_empty_state",
              "No devices paired" in dev_list and "Failed" not in dev_list,
              dev_list[:80])
        page.locator("#dev-ov").click(position={"x": 5, "y": 5})
        page.wait_for_timeout(200)
        dev_closed = page.evaluate(
            """() => {
              const o = document.getElementById('dev-ov');
              return !o || o.offsetParent === null || getComputedStyle(o).display === 'none';
            }"""
        )
        check("ui.devices_close", bool(dev_closed))

        # 6c. Password login — set it (we hold a token now), verify the
        # Settings panel reflects it. The actual password login UI is
        # exercised later when the test lands on /login without a token.
        set_st = page.evaluate(
            """async () => {
              const tok = sessionStorage.getItem('jarvis_token');
              const r = await fetch('/api/auth/set-password', {
                method: 'POST',
                headers: {'Content-Type': 'application/json',
                          'Authorization': 'Bearer ' + tok},
                body: JSON.stringify({password: 'e2e-pass-123',
                                      current: 'e2e-pass-123'})
              });
              return r.status;
            }"""
        )
        check("pw.set_password", set_st == 200, f"st={set_st}")
        page.locator("#cfg-btn").click()
        page.wait_for_timeout(500)
        pw_state = page.locator("#cfg_pw_state").inner_text() \
            if page.locator("#cfg_pw_state").count() else ""
        check("pw.settings_state", "Password set" in pw_state, pw_state)
        pw_btn = page.locator("#cfg_pw_btn").inner_text() \
            if page.locator("#cfg_pw_btn").count() else ""
        check("pw.settings_btn_change", "CHANGE" in pw_btn.upper(), pw_btn)
        page.locator("#cfg-ov button:has-text('CANCEL')").click()
        page.wait_for_timeout(200)

        # 7. Command input
        found_cmd = page.locator("#inp").count() == 1
        check("ui.command_input", found_cmd)
        if found_cmd:
            page.fill("#inp", "system status")
            page.click(".btn-send")
            page.wait_for_timeout(2500)
            feed_txt = page.locator("#feed").inner_text() if page.locator("#feed").count() else ""
            log_txt = page.locator("#log").inner_text() if page.locator("#log").count() else ""
            check(
                "ui.feed_or_log_nonempty",
                bool(feed_txt.strip() or log_txt.strip()),
                f"feed={len(feed_txt)} log={len(log_txt)}",
            )

        # 8. Reload keeps session
        page.reload(wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector("#pill", timeout=10000)
        check("session.persist_reload", page.url.rstrip("/") == BASE.rstrip("/"), page.url)

        # 9. No token → login
        page.evaluate("() => sessionStorage.removeItem('jarvis_token')")
        page.goto(f"{BASE}/", wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        check("auth.redirect_without_token", "/login" in page.url, page.url)

        # 9b. Password login UI on /login (password already set in 6c)
        mode_txt = page.locator("#mode-btn").inner_text().strip() \
            if page.locator("#mode-btn").count() else ""
        check("login.use_password_btn",
              page.locator("#mode-btn").is_visible() and mode_txt == "USE PASSWORD",
              mode_txt)
        page.locator("#mode-btn").click()
        page.wait_for_timeout(250)
        check("login.pw_mode",
              page.locator("#pw").is_visible()
              and not page.locator("#key").is_visible()
              and page.locator("#main-btn").inner_text().strip() == "LOG IN")
        page.fill("#pw", "wrong-password-xyz")
        page.click("#main-btn")
        page.wait_for_timeout(700)
        pw_err = page.locator("#err").inner_text() if page.locator("#err").count() else ""
        check("login.pw_wrong_error", "Wrong password" in pw_err, pw_err)
        check("login.still_on_login", "/login" in page.url, page.url)
        page.fill("#pw", "e2e-pass-123")
        page.click("#main-btn")
        page.wait_for_timeout(1000)
        pw_tok = page.evaluate("() => sessionStorage.getItem('jarvis_token')")
        check("login.pw_login_works",
              "/login" not in page.url and bool(pw_tok),
              f"url={page.url} tok={bool(pw_tok)}")

        serious = [
            e
            for e in console_errors + page_errors
            if not re.search(
                r"favicon|ResizeObserver|AudioContext|autoplay|ERR_|"
                r"404 \(Not Found\)|401 \(Unauthorized\)",
                e,
                re.I,
            )
        ]
        check("console.no_page_errors", len(serious) == 0, "; ".join(serious[:3]))

        shot = ROOT / "e2e" / "hud.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            pass

        browser.close()
    return out


def main() -> int:
    print(f"E2E base={BASE} chromium={CHROMIUM}", flush=True)
    print("─" * 60, flush=True)
    proc = None
    try:
        proc = start_server()
        print(f"server pid={proc.pid} log={LOG}", flush=True)

        for path, want in [("/", 200), ("/login", 200), ("/api/setup/state", 200)]:
            st, _ = api(path)
            check(f"http{path}", st == want, f"got {st}")

        st, _ = api("/api/metrics")
        check("http/api/metrics_unauth", st == 401, f"got {st}")

        # Browser pairs first (bootstrap still open on clean boot)
        session = run_browser()
        tok = session.get("token") or ""
        key = session.get("key") or ""

        if tok:
            st3, _ = api("/api/metrics", token=tok)
            check("api.metrics_with_token", st3 == 200, str(st3))
        if key:
            st4, _ = api("/login", "POST", {"pin": key})
            check("api.pin_onetime", st4 == 401, str(st4))

        st5, b5 = api("/api/bootstrap-key", "POST")
        check("api.bootstrap_locked", st5 == 403, f"st={st5} {b5}")

        # Password auth (persistent login) — API surface
        st, b = api("/api/auth/state")
        check("api.auth_state", st == 200 and b.get("password_set") is True,
              f"{st} {b}")
        st, b = api("/api/auth/set-password", "POST", {"password": "short"},
                    token=tok)
        check("api.pw_too_short_400", st == 400, f"{st} {b}")
        st, b = api("/api/auth/set-password", "POST",
                    {"password": "another-pass-9", "current": "wrong-pass"},
                    token=tok)
        check("api.pw_wrong_current_401", st == 401, f"{st} {b}")
        st, b = api("/api/auth/set-password", "POST",
                    {"password": "no-token-pass"})
        check("api.pw_set_unauth_401", st == 401, f"{st} {b}")
        st, b = api("/api/auth/login", "POST", {"password": "definitely-wrong"})
        check("api.pw_login_bad_401",
              st == 401 and b.get("error") == "bad_password", f"{st} {b}")
        st, b = api("/api/auth/login", "POST", {"password": "e2e-pass-123"})
        check("api.pw_login_ok",
              st == 200 and b.get("token") and b.get("device_token"),
              f"{st} token={bool(b.get('token'))}")

        # Rate limit LAST — locks password login for 60s (separate from the
        # pairing limiter checked above).
        got_lock = False
        tries = 0
        for i in range(14):
            tries += 1
            st, _ = api("/api/auth/login", "POST", {"password": "nope-nope-1"})
            if st == 429:
                got_lock = True
                break
        check("api.pw_login_rate_limit", got_lock, f"after {tries} tries")
    except Exception as e:
        check("e2e.exception", False, repr(e))
        try:
            if LOG.exists():
                print("── server log tail ──", flush=True)
                print(LOG.read_text(errors="replace")[-2500:], flush=True)
        except Exception:
            pass
    finally:
        if proc:
            stop_server(proc)

    print("─" * 60, flush=True)
    print(f"PASS {len(_passes)}  FAIL {len(_failures)}", flush=True)
    for f in _failures:
        print(f"  ✗ {f}", flush=True)
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
