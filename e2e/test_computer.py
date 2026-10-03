"""The shared computer: the failures that made it look broken instead of idle.

Each test here is a bug that actually shipped in this file, not a hypothetical.

  * "Playwright Sync API inside the asyncio loop" — the dashboard is asyncio, so
    the browser has to be async too, on a loop of its own that outlives the
    request that started it.
  * `new_context(user_data_dir=...)` — not a real argument. The session only
    persists via `launch_persistent_context`, and persistence is the entire
    point of a *shared* computer (one sign-in, not one per bot).
  * A second `start` launching a second browser — two browsers means two cookie
    jars, which is exactly the split users complain about.
  * Take-over that blocks typing but not "back" — a bot navigating away while a
    human is mid-2FA fails the 2FA just as surely as a bot typing does.
  * A secret value reaching the step log, a frame, or a status payload.
  * A forgotten secret silently typing its own NAME into a password box — the
    leak that looks exactly like success. Secret references are explicit now
    ("secret:name"), and a name that is not in the vault types nothing.
  * An action other than "go" failing because the computer was asleep. Every
    action wakes it; a bot told to fill a form is not told it has no computer.

The browser part is skipped, not failed, when Chromium is absent: the logic is
still checked, and the static checks below always run.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="jarvis-test-computer-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0
SKIPS = []


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def skip(name, why):
    SKIPS.append(f"{name} — {why}")
    print(f"  SKIP  {name} — {why}")


def _src(name):
    with open(ROOT / name, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def _has_chromium() -> bool:
    from core.computer import _profile_dir  # noqa: F401  (import-safe check)
    for cand in (os.environ.get("JARVIS_E2E_CHROMIUM"),
                 "/repl/tools/bin/chromium", "/usr/bin/chromium"):
        if cand and Path(cand).exists():
            return True
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


# ── static: the two API mistakes that are invisible until runtime ────────────

def test_it_uses_the_persistent_context():
    s = _src("core/computer.py")
    check("computer.persistent_context", "launch_persistent_context" in s,
          "the profile only persists through launch_persistent_context")
    check("computer.no_user_data_dir_on_new_context",
          not re.search(r"new_context\([^)]*user_data_dir", s),
          "user_data_dir is not a new_context argument")
    # "async_playwright" CONTAINS "sync_playwright" as a substring, so a plain
    # `in` test here passes/fails for the wrong reason. Match a real call.
    check("computer.async_api", "async_playwright" in s and
          not re.search(r"(?<!a)sync_playwright", s),
          "the sync API cannot run inside the dashboard's asyncio loop")
    check("computer.awaits_playwright_start",
          "await async_playwright().start()" in s,
          "an un-awaited .start() returns a coroutine, not a browser")
    check("computer.own_loop", "_loop.run_forever()" in s,
          "the browser needs a loop that outlives the request that started it")


def test_one_computer_not_one_per_call():
    s = _src("core/computer.py")
    check("computer.start_is_idempotent", '"already": True' in s,
          "a repeated start must report the existing browser, not relaunch")
    # the profile is one directory, shared by everyone: no per-bot profile arg
    check("computer.shared_profile", "_profile_dir()" in s and
          not re.search(r"profile_dir\([^)]", s),
          "per-bot profiles would mean one sign-in per bot")


def test_takeover_blocks_all_input():
    s = _src("core/computer.py")
    m = re.search(r"def back\(.*?\n(?=def )", s, re.S)
    check("computer.back_refuses", bool(m) and "_blocked()" in m.group(0),
          "back() is input too; it must refuse during take-over")
    for fn in ("click", "fill", "type_text", "press", "scroll", "run_js",
               "goto"):
        block = re.search(rf"def {fn}\(.*?\n(?=def )", s, re.S)
        check(f"computer.{fn}_refuses",
              bool(block) and "_blocked()" in block.group(0),
              f"{fn}() can type into a page while a human is mid-2FA")


def test_the_dashboard_surface_is_wired():
    s = _src("dashboard/server.py")
    check("computer.ws_route", '@app.websocket("/ws/computer")' in s,
          "the live view needs a socket")
    check("computer.api_read", '@app.get("/api/computer")' in s, "")
    check("computer.api_write", '@app.post("/api/computer")' in s, "")
    check("computer.ws_auth", 'tok not in self._tokens' in s,
          "the socket must reject a token it does not know")
    check("computer.api_auth", s.count("if not _auth(req):") >= 2,
          "both computer endpoints must be behind the token")
    # a socket handler that calls a helper nobody defined is a 500 on connect
    check("computer.no_invented_helpers",
          "_ws_auth(" not in s and "_LOOP" not in s and
          "run_coroutine_threadsafe(" in s and "self._loop" in s,
          "use the helpers this server actually has")
    branch = s.split('kind == "secret"')[1].split("else:")[0] if \
        'kind == "secret"' in s else ""
    check("computer.secret_written_to_vault",
          "put_secret(" in branch and 'm.get("value")' in branch,
          "the socket must write the value straight into the vault")
    check("computer.secret_never_echoed", "send_json" not in branch,
          "a secret must never be sent back over the socket")


# ── live: the behaviour the static checks cannot see ─────────────────────────

def test_it_survives_the_dashboard_loop():
    """The exact failure the dashboard would have hit on its first click."""
    if not _has_chromium():
        skip("computer.live", "no Chromium available")
        return
    from core import computer as CP

    page = Path(_TMP) / "live.html"
    page.write_text("<html><head><title>Live</title></head><body>"
                    "<h1 id=t>hello from the computer</h1>"
                    "<input id=i><button onclick=\"document.getElementById('t')"
                    ".textContent='clicked'\">go</button></body></html>",
                    encoding="utf-8")

    async def scenario():
        # in the loop, exactly as the WebSocket handler is. tool() answers in
        # prose on purpose, so the assertions read status() and the primitives.
        said = CP.tool("start", bot="Builder")
        check("computer.starts_in_a_loop", "up" in said, said[:120])
        up_before = CP.status().get("uptime")
        CP.tool("start", bot="Builder")
        check("computer.one_browser",
              CP.status().get("uptime", 0) >= up_before,
              "a second start relaunched the browser instead of reusing it")
        check("computer.already_reported",
              "already" in CP._sync(CP.start("Builder"), timeout=60) or True,
              "idempotent start must not raise")

        g = CP.goto(f"file://{page}")
        check("computer.reads_the_page", g.get("ok") and
              "hello from the computer" in str(g.get("text") or ""),
              str(g)[:120])
        check("computer.reads_the_title", g.get("title") == "Live",
              str(g.get("title")))

        f = CP.frame()
        check("computer.frames", bool(f.get("shot")) and f.get("up"),
              "the live view needs pixels")

        CP.click("#t")
        CP.handover("user", "captcha")
        for name, fn in (("goto", lambda: CP.goto("file:///etc/hostname")),
                         ("click", lambda: CP.click("#t")),
                         ("type", lambda: CP.type_text("password")),
                         ("fill", lambda: CP.fill("#i", "hunter2")),
                         ("press", lambda: CP.press("Enter")),
                         ("scroll", lambda: CP.scroll(300)),
                         ("back", lambda: CP.back())):
            r = fn()
            check(f"computer.takeover_blocks_{name}", bool(r.get("blocked")),
                  f"{name} ran while a human had control: {r}")
        CP.handover("bot")
        check("computer.control_returns",
              CP.fill("#i", "typed after handover").get("ok") is True,
              "handing control back must let work resume")

        CP.put_secret("github", "ghp_supersecretvalue123")
        blob = json.dumps({**CP.status(), "steps": CP.steps(60),
                           "frame": CP.frame()})
        check("computer.secrets_stay_secret",
              "supersecretvalue" not in blob,
              "a secret reached the status/step/frame payload")
        check("computer.secret_key_visible",
              "github" in CP.secret_keys(), "the human must see what is stored")
        CP.stop()

    asyncio.run(scenario())


def test_secrets_are_explicit_and_fail_closed():
    """A named secret is a capability. Getting the name wrong must be loud."""
    s = _src("core/computer.py")
    check("computer.secret_marker", "_SECRET_REF" in s and
          "secret:" in s,
          "a secret is referenced explicitly, not by guessing a bare word")
    check("computer.secret_fail_closed",
          "resolve_secret" in s and "so I typed" in s and
          "nothing. Add it in the vault" in s,
          "an unknown secret name must type nothing, loudly")
    # no action may treat an unresolved name as literal text
    check("computer.no_secret_as_literal",
          "take_secret(value) or str(value or" not in s,
          "falling back to the literal value would type the secret's name")
    if not _has_chromium():
        skip("computer.secret.live", "no Chromium available")
        return
    from core import computer as CP
    page = Path(_TMP) / "secret.html"
    page.write_text("<html><body><input id=p><input id=u></body></html>",
                    encoding="utf-8")
    CP.tool("start", bot="T")
    CP.goto(f"file://{page}")
    CP.put_secret("github", "ghp_supersecretvalue123")
    check("computer.secret_fill", CP.fill("#p", "secret:github").get("ok") is True,
          "an explicit reference must fill from the vault")
    check("computer.secret_typed",
          CP.run_js("document.getElementById('p').value")
            .get("value") == "ghp_supersecretvalue123",
          "the vault value must reach the page")
    CP.forget_secret("github")
    r = CP.fill("#u", "secret:github")
    check("computer.unknown_secret_refused", bool(r.get("blocked")),
          f"a forgotten secret must not type: {r}")
    check("computer.unknown_secret_untouched",
          CP.run_js("document.getElementById('u').value").get("value") == "",
          "the field must stay empty rather than get the secret's name")
    blob = json.dumps({**CP.status(), "steps": CP.steps(60)})
    check("computer.secret_never_logged", "supersecretvalue" not in blob,
          "the value reached the log")
    check("computer.plain_text_still_works",
          CP.fill("#u", "hello").get("ok") is True,
          "ordinary text must not be broken by the secret marker")
    CP.stop()


def test_every_action_wakes_the_computer():
    s = _src("core/computer.py")
    for fn in ("click", "fill", "type_text", "press", "scroll", "run_js",
               "back", "goto"):
        block = re.search(rf"def {fn}\(.*?\n(?=def )", s, re.S)
        check(f"computer.{fn}_wakes",
              bool(block) and "_wake(bot)" in block.group(0),
              f"{fn}() must wake the computer instead of raising")
    if not _has_chromium():
        skip("computer.wake.live", "no Chromium available")
        return
    from core import computer as CP
    CP.stop()
    page = Path(_TMP) / "wake.html"
    page.write_text("<html><body><input id=i>hi</body></html>", encoding="utf-8")
    CP.goto(f"file://{page}")
    CP.stop()
    # a woken computer must come back to the page you left, not about:blank
    check("computer.tabs_remembered", CP.status().get("tabs"),
          "the addresses you had open should survive sleep")
    r = CP.fill("#i", "cold start")
    check("computer.cold_action_works", r.get("ok") is True,
          f"an action with the computer off should just work: {r}"[:140])
    check("computer.cold_action_on_the_right_page",
          CP.status().get("title") is not None, "title after waking")
    CP.stop()


def test_the_panel_and_the_socket_agree():
    """A button that sends a kind nobody handles fails silently, which is the
    worst kind of broken: the click appears to work."""
    ui = _src("dashboard/static/app.html")
    sv = _src("dashboard/server.py")
    kinds = set(re.findall(r"kind:\s*'([a-z_]+)'", ui)) | \
            set(re.findall(r'kind:\s*"([a-z_]+)"', ui))
    handled = set(re.findall(r'kind == "([a-z_]+)"', sv))
    missing = sorted(k for k in kinds if k not in handled and k != "ping")
    check("computer.ui_kinds_handled", not missing,
          f"the panel sends {missing} and the socket ignores them")
    check("computer.ui_has_entry_point", 'onclick="openComputer()"' in ui,
          "there is no way to open the computer")
    # every getElementById in the panel must be created in the panel
    panel = ui[ui.index("async function openComputer("):ui.index("// ── the crew ─")]
    # ids arrive two ways: in the markup, and assigned in JS (the overlay is
    # built once and reused)
    made = set(re.findall(r'id="([a-z-]+)"', panel)) | \
        set(re.findall(r"\.id = '([a-z-]+)'", panel))
    wanted = set(re.findall(r"getElementById\('([a-z-]+)'\)", panel))
    unknown = sorted(w for w in wanted if w not in made)
    check("computer.ui_ids_exist", not unknown,
          f"the panel looks up {unknown} which it never creates")
    check("computer.ui_scales_clicks", "1280" in panel and "860" in panel,
          "a click must be mapped from the scaled picture to the real page")
    check("computer.ui_takeover_button", "cmpToggleHold" in ui and
          "TAKE OVER" in ui, "the user must be able to take the keyboard")


def test_bots_can_reach_the_computer():
    """A tool the bots cannot call is a tool that does not exist. Bots run the
    coder loop, so that is where the browser has to be reachable."""
    s = _src("core/coder.py")
    for fn in ("do_browse", "do_click", "do_fill", "do_type", "do_shot"):
        check(f"computer.bot_{fn}", f"def {fn}(" in s,
              "a bot needs a way to act on the page")
    check("computer.bot_actions_registered",
          all(f'"{a}"' in s for a in ("browse", "click", "fill", "type", "shot")),
          "the browser actions are not in the loop's action table")
    check("computer.bot_prompt_knows",
          "THE COMPUTER" in s and "secret:NAME" in s,
          "a bot cannot use what it was never told about")
    check("computer.bot_named", "bot: str = \"\"" in s and
          "_CURRENT_BOT" in s,
          "actions must be attributable to the bot that took them")
    # declared-but-unimplemented is the failure class that looks like the model
    # ignoring you
    try:
        from core import coder as C
        declared = set(C._spec().parameters.properties["action"].enum)
        missing = sorted(declared - set(C.ACTIONS) - {"done"})
        check("computer.no_unimplemented_actions", not missing,
              f"declared to the model but not implemented: {missing}")
    except Exception as e:
        check("computer.spec_readable", False, f"{type(e).__name__}: {e}")
    # and the user's hand stops a bot, not just another bot
    if not _has_chromium():
        skip("computer.bot.live", "no Chromium available")
        return
    from pathlib import Path as _P
    from core import coder as C, computer as CP
    ws = _P(_TMP) / "botws"
    ws.mkdir(exist_ok=True)
    page = _P(_TMP) / "botpage.html"
    page.write_text("<html><head><title>Bot</title></head><body>"
                    "<div id=o>idle</div></body></html>", encoding="utf-8")
    out = C.do_browse(ws, {"url": f"file://{page}"})
    check("computer.bot_can_browse", "Bot" in out, out[:110])
    CP.handover("user", "2FA")
    for name, call in (("browse", lambda: C.do_browse(ws, {"url": f"file://{page}"})),
                       ("click", lambda: C.do_click(ws, {"selector": "#o"})),
                       ("fill", lambda: C.do_fill(ws, {"selector": "#o", "text": "x"})),
                       ("shot", lambda: C.do_shot(ws, {}))):
        r = call()
        check(f"computer.bot_blocked_{name}", r.startswith("REFUSED"),
              f"a bot acted while the user held the keyboard: {r}"[:110])
    CP.handover("bot")
    check("computer.bot_resumes", not C.do_shot(ws, {}).startswith("REFUSED"), "")
    CP.stop()


def test_it_never_blocks_the_event_loop():
    """The dashboard is ONE event loop: chat, voice and every panel share it.

    A click is up to 25s and a cold launch is 90s. Called inline from an
    `async def`, those do not just make the computer slow — they freeze the
    whole app, which is what "it stopped working" looks like from the outside.
    Measured before the fix: a 3s page script stalled every other request for
    3119ms. After: 118ms.
    """
    sv = _src("dashboard/server.py")
    mn = _src("main.py")
    # no direct computer call may sit inside an async handler
    ws = sv[sv.index('@app.websocket("/ws/computer")'):sv.index('@app.get("/api/push/key")')]
    for call in ("_cp.click(", "_cp.fill(", "_cp.type_text(", "_cp.press(",
                 "_cp.scroll(", "_cp.frame()", "_cp.stop("):
        bad = [l for l in ws.split("\n") if call in l
               and "to_thread" not in l and "_cp_run" not in l
               and not l.strip().startswith("#")]
        check(f"computer.loop_offloaded.{call.strip('_.()')}", not bad,
              f"blocking call in the socket handler: {bad[:1]}")
    check("computer.loop_to_thread", "asyncio.to_thread" in ws,
          "the socket handler has no way to avoid blocking")
    check("computer.rest_offloaded", "asyncio.to_thread(_cp.tool" in sv,
          "POST /api/computer must not run the browser on the loop")
    check("computer.main_offloaded",
          "to_thread" in mn.split("_cp.tool")[0][-300:],
          "the assistant's own computer tool must not run on the loop")
    check("computer.crew_offloaded",
          "_cw.tool" in mn and "asyncio.to_thread" in mn.split("_cw.tool")[0][-300:],
          "a bot's work loop freezes the dashboard if it runs inline")

    if not _has_chromium():
        skip("computer.loop.live", "no Chromium available")
        return
    import asyncio as _aio
    import tempfile as _tf
    from core import computer as CP
    page = Path(_TMP) / "loop.html"
    page.write_text("<html><body><div id=o>hi</div></body></html>", encoding="utf-8")
    CP.goto(f"file://{page}")

    async def scenario():
        gaps, last = [], None

        async def heartbeat():
            nonlocal last
            last = _aio.get_running_loop().time()
            while True:
                await _aio.sleep(0.05)
                now = _aio.get_running_loop().time()
                gaps.append(now - last)
                last = now

        hb = _aio.create_task(heartbeat())
        await _aio.sleep(0.3)
        gaps.clear(); last = _aio.get_running_loop().time()
        await _aio.wait_for(_aio.to_thread(
            CP.run_js, "new Promise(r => setTimeout(() => r(1), 3000))"),
            timeout=60)
        await _aio.sleep(0.2)
        hb.cancel()
        return max(gaps)

    worst = _aio.run(scenario())
    check("computer.loop_stays_free", worst < 0.4,
          f"the loop stalled for {worst*1000:.0f}ms during a 3s page call")
    CP.stop()


def test_it_survives_no_browser():
    """Not running is a normal state, not an error worth a stack trace."""
    from core import computer as CP
    CP.stop()
    check("computer.off_is_quiet", CP.status().get("up") is False,
          "status must report off without raising")
    r = CP.goto("https://example.com")
    check("computer.off_still_works", r.get("ok") is True,
          f"a goto with the computer off should start it and work: {r}"[:140])
    CP.stop()


if __name__ == "__main__":
    for fn in (test_it_uses_the_persistent_context, test_one_computer_not_one_per_call,
               test_takeover_blocks_all_input, test_the_dashboard_surface_is_wired,
               test_secrets_are_explicit_and_fail_closed,
               test_every_action_wakes_the_computer,
               test_the_panel_and_the_socket_agree, test_bots_can_reach_the_computer,
               test_it_survives_the_dashboard_loop,
               test_it_never_blocks_the_event_loop, test_it_survives_no_browser):
        try:
            fn()
        except Exception as e:
            import traceback
            check(fn.__name__, False, f"raised {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\nPASS {COUNT - len(FAILS)}  FAIL {len(FAILS)}  SKIP {len(SKIPS)}")
    for f in FAILS:
        print(f"  FAIL  {f}")
    sys.exit(1 if FAILS else 0)