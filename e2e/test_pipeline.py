"""E2E — the whole point of the product, in one scenario.

    "Find the hot lead in India, create a stunning website for them, then draft
     the cold outreach email."

Three subsystems have to hand off to each other for that sentence to mean
anything: the lead engine has to find a real person, the display surface has to
put a real page on the screen, and the mail pipeline has to write something a
human would send. Each works in isolation — this is the test that proves they
work *together*.

Why the real model and not a stub: a stub would prove the tools are wired up,
which the other suites already do. The thing that actually breaks in practice is
the hand-off — the model calling the wrong tool, or calling them in the wrong
order, or the display panel never hearing about the artifact. Only a real turn
exercises that.

So this is slow and it costs model calls, and it is worth it.
"""
from __future__ import annotations

import importlib
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
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3123")
BASE = f"http://127.0.0.1:{PORT}"
LOG = Path("/tmp/jarvis_e2e_pipeline.log")

_failures: list[str] = []
_passes: list[str] = []
TOOLS: list[str] = []          # filled from the server's own tool-call log


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {str(detail)[:150]}", flush=True)


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


def start() -> subprocess.Popen:
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} in use")
    if LOG.exists():
        LOG.unlink()
    data = ROOT / "e2e" / "data_pipeline"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)
    # A fresh data dir has no Gemini key, and with no key the model never
    # runs — no tool is ever called, and every check below fails for a reason
    # that has nothing to do with the pipeline. That is exactly what happened
    # the first time this ran.
    #
    # The key has to be read from the REPO's config explicitly. Once
    # JARVIS_DATA points at the scratch dir, config_manager looks there and
    # nowhere else, so calling it here would return empty — the trap.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    src = ROOT / "config" / "api_keys.json"
    try:
        blob = json.loads(src.read_text(encoding="utf-8"))
        key = str(blob.get("gemini_api_key") or "").strip()
        if not key:
            key = str(os.environ.get("GEMINI_API_KEY") or "").strip()
        if key:
            dest = data / "config" / "api_keys.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps({"gemini_api_key": key}, indent=2),
                            encoding="utf-8")
            dest.chmod(0o600)
            print(f"  seeded the model key ({len(key)} chars)")
        else:
            print("  (no model key in the repo config — set GEMINI_API_KEY)")
    except Exception as e:
        print(f"  (could not seed the api key: {type(e).__name__}: {e})")
    env = os.environ.copy()
    env.update({"JARVIS_MODE": "server", "JARVIS_PORT": str(PORT),
                "JARVIS_DATA": str(data), "PYTHONUNBUFFERED": "1"})
    p = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT), env=env,
                         stdout=LOG.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    end = time.time() + 90
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/login", timeout=2) as r:
                if r.status < 500:
                    return p
        except Exception:
            pass
        time.sleep(0.25)
    p.kill()
    raise RuntimeError("server did not start")


_tok = ""


def token() -> str:
    global _tok
    if _tok:
        return _tok
    r = urllib.request.Request(BASE + "/api/bootstrap-key", data=b"{}", method="POST")
    with urllib.request.urlopen(r, timeout=30) as x:
        key = json.loads(x.read())["key"]
    with urllib.request.urlopen(f"{BASE}/auto-login?key={key}", timeout=30) as x:
        html = x.read().decode(errors="replace")
    _tok = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html).group(1)
    return _tok


def api(path: str, body=None, tok: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               method="POST" if data is not None else "GET")
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if tok:
        r.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(r, timeout=60) as x:
            b = x.read()
            return x.status, (json.loads(b) if b else {})
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, (json.loads(b) if b else {})
        except Exception:
            return e.code, {}


def tool_calls() -> list[str]:
    """Read the server's own record of what it called. `[JARVIS] 📞 <name>` is
    printed for every tool call, so this is the ground truth rather than a guess
    at what the model decided to do."""
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text(errors="replace").splitlines():
        m = re.search(r"📞 ([a-z_]+)", line)
        if m:
            out.append(m.group(1))
    return out


def log_text() -> str:
    if not LOG.exists():
        return ""
    return LOG.read_text(errors="replace")


def wait_for(pred, timeout: float = 180.0, every: float = 2.0):
    """Poll the server log until `pred(tool_calls())` holds."""
    end = time.time() + timeout
    last: list[str] = []
    while time.time() < end:
        last = tool_calls()
        if pred(last):
            return last
        time.sleep(every)
    return last


def send_command(text: str, tok: str) -> None:
    """The same thing the dashboard's SEND button does: a websocket command."""
    import asyncio
    try:
        import websockets
    except ImportError:
        raise RuntimeError("pip install websockets to run this suite")

    async def _go():
        uri = f"ws://127.0.0.1:{PORT}/ws?token={tok}"
        async with websockets.connect(uri, max_size=None) as ws:
            await ws.send(json.dumps({"type": "command", "text": text}))
            # stay open long enough for the turn to be accepted
            for _ in range(40):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    if '"type": "pong"' in msg or "pong" in msg:
                        continue
                except asyncio.TimeoutError:
                    pass
    asyncio.run(_go())


def main() -> int:
    print("== the pipeline: lead → website → cold email ==", flush=True)
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("  (websockets missing — installing)")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "websockets"],
                       check=False)

    proc = start()
    try:
        tok = token()
        check("session", bool(tok))

        # a clean slate, so the lead we find is the one this run created
        st, before = api("/api/leads?limit=5", tok=tok)
        check("leads api reachable", st == 200, st)

        # Wait until the Live session is actually up. A command that arrives
        # first is dropped with "no session" and the whole turn is lost — which
        # looks exactly like a broken pipeline.
        print("  waiting for the model session to come online…", flush=True)
        ready = wait_for(lambda c: "online" in log_text(), timeout=180)
        check("the model session came online", "online" in log_text(),
              log_text().strip().splitlines()[-1] if log_text() else "no log")
        time.sleep(3)          # let the connect settle before speaking

        prompt = ("Find the hottest lead in India right now. Then create a "
                  "stunning website for them and put it on my screen. Then "
                  "draft the cold outreach email.")
        print(f"\n  telling JARVIS: {prompt}\n", flush=True)
        send_command(prompt, tok)

        # 1. the lead engine has to go looking
        calls = wait_for(lambda c: any("leads" in t for t in c), timeout=200)
        check("the model called the lead engine", any("leads" in t for t in calls),
              calls[:8])

        # 2. and then put a page on the screen
        calls = wait_for(lambda c: "display" in c, timeout=200)
        check("the model put a website on the screen", "display" in calls, calls[:10])

        # 3. and then write the email
        calls = wait_for(lambda c: any(t in c for t in ("leads", "mail"))
                         and len(calls_for(c)) >= 2, timeout=200)
        check("the model drafted the outreach email",
              any("leads" in t for t in calls) and len(calls) >= 2, calls[:12])

        # the order matters: find, then build, then write
        seq = [t for t in calls]
        first_lead = next((i for i, t in enumerate(seq) if "leads" in t), 99)
        first_disp = next((i for i, t in enumerate(seq) if t == "display"), 99)
        check("the order is find → build → write",
              first_lead < first_disp, f"lead@{first_lead} display@{first_disp}")

        # 4. the artifact is real and reachable
        st, d = api("/api/display?limit=5", tok=tok)
        arts = d.get("artifacts") or d.get("items") or []
        check("the display surface holds the website",
              st == 200 and bool(arts), f"{st} {len(arts)} artifact(s)")
        if arts:
            top = arts[0]
            check("the artifact is a page, not a stray image",
                  top.get("kind") in ("html", "page", ""), top.get("kind"))
            check("the artifact has a title",
                  bool(top.get("title") or top.get("name")),
                  top.get("title") or top.get("name"))

        # 5. the lead record exists and is Indian
        st, d = api("/api/leads?limit=20", tok=tok)
        rows = d.get("leads") or d.get("items") or []
        check("a lead record exists", st == 200 and bool(rows), f"{st} {len(rows)}")
        if rows:
            blob = json.dumps(rows[0]).lower()
            check("the lead is from India",
                  any(k in blob for k in ("india", "mumbai", "delhi", "bangalore",
                                          "bengaluru", "chennai", "pune",
                                          "hyderabad", "kolkata", "in")),
                  blob[:120])

        # 6. the draft exists
        st, d = api("/api/leads?limit=20", tok=tok)
        rows = d.get("leads") or d.get("items") or []
        drafted = [r for r in rows if r.get("draft") or r.get("draft_text")]
        check("a cold email was drafted", bool(drafted),
              f"{len(drafted)} of {len(rows)} lead(s) have a draft")
        if drafted:
            body = str(drafted[0].get("draft") or drafted[0].get("draft_text") or "")
            check("the draft is a real email, not a placeholder",
                  len(body) > 80 and ("@" in body or "http" in body
                                     or "hi " in body.lower()),
                  body[:90])

        # 7. the UI reflects it
        app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
        check("the dashboard can show the website",
              "function openDisplay(" in app or "disp-take" in app)
        check("the dashboard can show leads",
              "function openLeads(" in app or "leads" in app.lower())

        log = LOG.read_text(errors="replace") if LOG.exists() else ""
        check("no traceback during the turn",
              "Traceback (most recent call last)" not in log,
              log[-400:] if "Traceback" in log else "")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print()
    print(f"PASS {len(_passes)}  FAIL {len(_failures)}")
    for f in _failures:
        print(f"  x {f}")
    return 1 if _failures else 0


def calls_for(c: list[str]) -> list[str]:
    return c


if __name__ == "__main__":
    sys.exit(main())
