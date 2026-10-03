"""Playwright E2E — Phase 3: persistent scheduler + phone push.

Usage:
  python3 e2e/test_phase3.py

Three sections, no model calls, no network pushes:

  1. core/scheduler.py units — cron parsing, spec validation, persistence,
     catch-up behaviour, one-shot self-disable, the model-facing manage().
  2. core/push.py units — VAPID stability, signed action tokens (tamper +
     expiry), subscription store, graceful degradation.
  3. A real server on :3103 — PWA assets with correct content types, the
     schedule API round-trip, WS job events, and /api/push/respond resolving
     a live core/confirm.py gate exactly like the dashboard button does.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3103")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_p3"
LOG = Path("/tmp/jarvis_e2e_p3.log")

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {detail}", flush=True)


# ── unit: scheduler ──────────────────────────────────────────────────────────

def unit_scheduler() -> None:
    print("== unit: core/scheduler.py ==", flush=True)
    tmp = tempfile.mkdtemp(prefix="sched_e2e_")
    os.environ["JARVIS_DATA"] = tmp
    sys.path.insert(0, str(ROOT))
    from core import scheduler as S

    # cron
    check("cron.weekday_hit", S.cron_matches("30 9 * * 1-5", datetime(2026, 9, 28, 9, 30)))
    check("cron.weekend_miss", not S.cron_matches("30 9 * * 1-5", datetime(2026, 9, 27, 9, 30)))
    check("cron.step", S.cron_matches("*/15 * * * *", datetime(2026, 9, 26, 14, 45))
          and not S.cron_matches("*/15 * * * *", datetime(2026, 9, 26, 14, 46)))
    check("cron.names", S.cron_matches("0 8 * * mon", datetime(2026, 9, 28, 8, 0)))
    check("cron.dom_or_dow",
          S.cron_matches("0 0 13 * fri", datetime(2026, 11, 13, 0, 0))
          and S.cron_matches("0 0 13 * fri", datetime(2026, 11, 6, 0, 0)))
    nxt = S.cron_next("0 8 * * *", datetime(2026, 9, 26, 9, 0))
    check("cron.next", str(nxt) == "2026-09-27 08:00:00", str(nxt))
    check("cron.garbage", S.cron_next("every tuesday", datetime(2026, 9, 26)) is None)
    check("cron.bad_field", S.cron_matches("99 * * * *", datetime(2026, 9, 26, 0, 0)) is False)

    # validation
    check("validate.ok_interval", S.validate("x", "interval", "30m") == "")
    check("validate.ok_daily", S.validate("x", "daily", "08:00") == "")
    check("validate.rejects_time", "08:00" in S.validate("x", "daily", "25:99"))
    check("validate.rejects_kind", S.validate("x", "weekly", "30m") != "")
    check("validate.rejects_empty_name", S.validate("", "interval", "30m") != "")
    check("validate.rejects_cron", S.validate("x", "cron", "30 9 * *") != "")

    # store round-trip
    sched = S.Scheduler()
    sched.load()
    j = sched.add("morning brief", "daily", "08:00", prompt="brief me")
    fresh = S.Scheduler()
    fresh.load()
    check("store.persists",
          [x["name"] for x in fresh.list()] == ["morning brief"],
          str([x["name"] for x in fresh.list()]))
    check("store.next_run_future", j["next_run"] and j["next_run"] > time.time())
    check("public.shape", S.to_public(j)["when"] == "daily at 08:00"
          and S.to_public(j)["next_in_s"] is not None)

    # fire → on_fire, reschedule
    got: list[str] = []
    bcasts: list[dict] = []
    fresh.bind(on_fire=lambda job, text: got.append(text), broadcast=bcasts.append)
    fresh.run_now(j["id"])
    after = fresh.get(j["id"])
    check("fire.dispatch", got == ["brief me"], str(got))
    check("fire.rescheduled",
          after["next_run"] > time.time() and after["runs"] == 1,
          f"next={after['next_run']} runs={after['runs']}")
    check("fire.broadcast", any(b.get("type") == "job" for b in bcasts))

    # once disables itself
    once = fresh.add("one shot", "once", "45m", prompt="ping")
    fresh.run_now(once["id"])
    o2 = fresh.get(once["id"])
    check("once.self_disables", o2["enabled"] is False and o2["next_run"] is None,
          str(o2["enabled"]))

    # overdue job does not storm: past the catch-up limit it is skipped
    stale = fresh.add("stale", "interval", "30m", prompt="old")
    fresh._jobs[stale["id"]]["next_run"] = time.time() - (S.CATCHUP_LIMIT_S + 60)
    fires: list[str] = []
    fresh.bind(on_fire=lambda job, text: fires.append(job["id"]))
    for job in fresh.due():
        if time.time() - float(job["next_run"] or 0) > S.CATCHUP_LIMIT_S:
            fresh._reschedule(fresh._jobs[job["id"]], time.time())
    fresh.bind(on_fire=lambda job, text: fires.append(job["id"]))
    for job in fresh.due():
        fresh._fire(job, "scheduled")
    check("catchup.no_storm", stale["id"] not in fires
          and fresh.get(stale["id"])["next_run"] > time.time(), str(fires))

    # toggle + remove
    fresh.set_enabled(j["id"], False)
    check("toggle.pause", fresh.get(j["id"])["enabled"] is False)
    check("remove.ok", fresh.remove(j["id"]) is True)
    check("remove.gone", fresh.remove(j["id"]) is False)

    # model-facing prose
    S._SCHEDULER = fresh
    out = S.manage("add", name="water plants", kind="daily", spec="19:00",
                   prompt="remind me the plants need water")
    check("manage.add", "Scheduled" in out and "daily at 19:00" in out, out)
    out2 = S.manage("list")
    check("manage.list", "water plants" in out2, out2)
    out3 = S.manage("remove", name="water plants")
    check("manage.remove", "Removed" in out3, out3)
    out4 = S.manage("add", name="bad", kind="daily", spec="99:99", prompt="x")
    check("manage.rejects_bad_spec", "could not schedule" in out4, out4)
    out5 = S.manage("add", name="nohandler", kind="interval", spec="5m")
    check("manage.needs_prompt", "Tell me what the job should DO" in out5, out5)
    check("manage.unknown_action", "Unknown action" in S.manage("frobnicate"))


# ── unit: push ───────────────────────────────────────────────────────────────

def unit_push() -> None:
    print("== unit: core/push.py ==", flush=True)
    tmp = tempfile.mkdtemp(prefix="push_e2e_")
    os.environ["JARVIS_DATA"] = tmp
    sys.path.insert(0, str(ROOT))
    from core import push

    keys = push.get_vapid_keys()
    check("vapid.generated", bool(keys.get("publicKey")) and bool(keys.get("privatePKCS8")))
    check("vapid.stable", push.get_vapid_keys()["publicKey"] == keys["publicKey"])

    tok = push.sign_action({"kind": "confirm", "accept": True})
    pl = push.verify_action(tok)
    check("token.roundtrip", pl and pl["accept"] is True, str(pl))
    body, _, sig = tok.partition(".")
    check("token.tamper", push.verify_action(body + "." + "A" * len(sig)) is None)
    check("token.garbage", push.verify_action("nope.nope") is None)
    import hmac, hashlib
    raw = push._b64e(json.dumps(
        {"kind": "confirm", "iat": int(time.time()) - 9999},
        separators=(",", ":"), sort_keys=True).encode())
    expired = raw + "." + push._b64e(
        hmac.new(push._action_secret(), push._b64d(raw), hashlib.sha256).digest())
    check("token.expiry", push.verify_action(expired) is None)

    r = push.add_sub({"endpoint": "https://push.example/one",
                      "keys": {"p256dh": "a", "auth": "b"}}, ua="e2e")
    check("sub.added", r["subs"] == 1, str(r))
    push.add_sub({"endpoint": "https://push.example/one",
                  "keys": {"p256dh": "a2", "auth": "b2"}})
    check("sub.dedupe", push.sub_count() == 1, str(push.sub_count()))
    try:
        push.add_sub({"endpoint": "x"})
        check("sub.validated", False, "bad subscription accepted")
    except ValueError:
        check("sub.validated", True)
    res = push.notify("hello", "world")
    check("notify.no_crash", res["subs"] == 1, str(res))
    appr = push.notify_approval("Push t-1 to origin?", "2 files changed")
    check("notify.approval_calls", appr["subs"] == 1, str(appr))
    st = push.status()
    check("status.shape",
          st["subs"] == 1 and st["has_vapid"] and "pywebpush" in st, str(st))
    check("unsubscribe", push.remove_sub("https://push.example/one")["subs"] == 0)


# ── server ───────────────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_http(url: str, timeout: float = 30.0) -> bool:
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


def _kill_stray() -> None:
    me = str(os.getpid())
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            env = open(f"/proc/{pid}/environ", "rb").read()
            cmd = open(f"/proc/{pid}/cmdline", "rb").read()
        except Exception:
            continue
        if f"JARVIS_PORT={PORT}".encode() in env or b"test_phase3" in cmd:
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass
    time.sleep(0.3)


def start_server() -> subprocess.Popen:
    _kill_stray()
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} in use")
    if LOG.exists():
        LOG.unlink()
    import shutil
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"JARVIS_MODE": "server", "JARVIS_PORT": str(PORT),
                "JARVIS_DATA": str(DATA), "PYTHONUNBUFFERED": "1"})
    logf = LOG.open("w")
    proc = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT),
                            env=env, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    if not _wait_http(f"{BASE}/login", 40):
        proc.kill()
        raise RuntimeError("server did not start:\n" +
                           (LOG.read_text(errors="replace")[-2000:] if LOG.exists() else ""))
    return proc


def api(path: str, method: str = "GET", body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw) if raw else {}
            except Exception:
                return r.status, {"_raw": raw[:120].decode(errors="replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"_raw": raw[:120].decode(errors="replace")}


def raw_get(path: str, token: str | None = None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_token() -> str:
    st, key = api("/api/bootstrap-key", "POST")
    if st != 200 or not key.get("key"):
        raise RuntimeError(f"bootstrap failed {st} {key}")
    with urllib.request.urlopen(f"{BASE}/auto-login?key={key['key']}", timeout=10) as r:
        html = r.read().decode(errors="replace")
    m = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html)
    if not m:
        raise RuntimeError("no token in auto-login page")
    return m.group(1)


def server_checks() -> None:
    print("== server: PWA + schedule + push ==", flush=True)
    token = get_token()

    # 1. PWA assets — content types matter: a manifest served as text/html
    #    is silently ignored by every browser.
    st, h, raw = raw_get("/manifest.webmanifest")
    mf = json.loads(raw) if st == 200 else {}
    check("pwa.manifest",
          st == 200 and "manifest" in h.get("content-type", "")
          and mf.get("display") == "standalone" and len(mf.get("icons", [])) >= 3,
          f"st={st} ct={h.get('content-type')}")
    check("pwa.manifest_shortcuts",
          {s["name"] for s in mf.get("shortcuts", [])} >= {"Tasks", "Schedule"},
          str([s.get("name") for s in mf.get("shortcuts", [])]))
    st, h, raw = raw_get("/sw.js")
    check("pwa.sw", st == 200 and "javascript" in h.get("content-type", "")
          and h.get("service-worker-allowed") == "/"
          and b"push" in raw and b"notificationclick" in raw,
          f"st={st} ct={h.get('content-type')} swa={h.get('service-worker-allowed')}")
    for icon, label in [("/static/icon-192.png", "pwa.icon192"),
                        ("/static/icon-512.png", "pwa.icon512"),
                        ("/static/icon-maskable-512.png", "pwa.icon_maskable"),
                        ("/apple-touch-icon.png", "pwa.apple_icon")]:
        st, h, raw = raw_get(icon)
        check(label, st == 200 and h.get("content-type") == "image/png"
              and raw[:8] == b"\x89PNG\r\n\x1a\n", f"st={st}")
    st, _, _ = raw_get("/static/icon-999.png")
    check("pwa.icon_404", st == 404, str(st))
    st, h, raw = raw_get("/")
    check("pwa.head_links",
          st == 200 and b"manifest.webmanifest" in raw and b"apple-touch-icon" in raw
          and b"apple-mobile-web-app-capable" in raw)
    check("pwa.sw_registered",
          b"serviceWorker.register('/sw.js'" in raw)

    # 2. scheduler API
    st, d = api("/api/schedule")
    check("sched.401", st == 401, str(st))
    st, d = api("/api/schedule", token=token)
    from core.scheduler import BUILTIN_JOBS
    names = [j["name"] for j in d.get("jobs", [])]
    check("sched.defaults_registered",
          set(BUILTIN_JOBS) <= set(names), f"want {sorted(BUILTIN_JOBS)} got {names}")
    check("sched.no_orphan_builtins",
          all(j["name"] in BUILTIN_JOBS for j in d.get("jobs", [])
              if j.get("source") == "builtin"), str(names))
    st, d = api("/api/schedule", "POST", {"name": "bad", "kind": "daily",
                                          "spec": "99:99", "prompt": "x"}, token=token)
    check("sched.rejects_bad", st == 400 and "08:00" in d.get("error", ""), str(d))
    st, j = api("/api/schedule", "POST",
                {"name": "e2e brief", "kind": "daily", "spec": "07:15",
                 "prompt": "brief me", "notify": True}, token=token)
    jid = j.get("id", "")
    check("sched.create", st == 201 and jid.startswith("j-") and j.get("when") == "daily at 07:15",
          f"{st} {j}")
    st, d = api(f"/api/schedule/{jid}", token=token)
    check("sched.detail", st == 200 and d["job"]["id"] == jid and "log" in d, str(st))
    st, d = api(f"/api/schedule/{jid}", "POST", {"enabled": False}, token=token)
    check("sched.pause", st == 200 and d["enabled"] is False, str(d))
    st, d = api(f"/api/schedule/{jid}", "POST", {"enabled": True}, token=token)
    check("sched.resume", st == 200 and d["enabled"] is True, str(d))
    st, d = api("/api/schedule/nope", token=token)
    check("sched.unknown_404", st == 404, str(st))

    # WS: creating a job must push a live event, not just answer the POST
    from websockets.sync.client import connect
    ws_box: list[dict] = []
    ws = connect(f"ws://127.0.0.1:{PORT}/ws?token={token}", open_timeout=8)
    stop = threading.Event()

    def reader():
        try:
            for raw in ws:
                try:
                    ws_box.append(json.loads(raw))
                except Exception:
                    pass
                if stop.is_set():
                    break
        except Exception:
            pass
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(0.6)
    st, d = api("/api/schedule", "POST",
                {"name": "ws job", "kind": "interval", "spec": "45m",
                 "prompt": "check X"}, token=token)
    ws_jid = d.get("id", "")
    time.sleep(1.2)
    check("sched.ws_event",
          any(m.get("type") == "job" and (m.get("job") or {}).get("id") == ws_jid
              for m in ws_box),
          str([m.get("type") for m in ws_box][:8]))
    stop.set()
    try:
        ws.close()
    except Exception:
        pass

    st, d = api(f"/api/schedule/{ws_jid}/remove", "POST", {}, token=token)
    check("sched.remove", st == 200, str(d))
    st, d = api(f"/api/schedule/{ws_jid}/remove", "POST", {}, token=token)
    check("sched.remove_twice_404", st == 404, str(st))

    # 3. push API
    st, d = api("/api/push/key")
    check("push.key_401", st == 401, str(st))
    st, d = api("/api/push/key", token=token)
    check("push.key", st == 200 and len(d.get("publicKey", "")) > 20, str(st))
    sub = {"endpoint": "https://push.example/e2e",
           "keys": {"p256dh": "a", "auth": "b"}}
    st, d = api("/api/push/subscribe", "POST", {"subscription": sub}, token=token)
    check("push.subscribe", st == 201 and d.get("subs") == 1, f"{st} {d}")
    st, d = api("/api/push/status", token=token)
    check("push.status", st == 200 and d.get("subs") == 1 and d.get("has_vapid"), str(d))
    st, d = api("/api/push/subscribe", "POST", {"subscription": {"endpoint": "x"}}, token=token)
    check("push.subscribe_validated", st == 400, str(st))
    # respond is the one unauthenticated endpoint — the token is the credential
    st, d = api("/api/push/respond", "POST", {"token": "forged.token"})
    check("push.respond_forged_401", st == 401, str(st))

    # 4. one-tap approval: mint a real token, have the server resolve a real gate
    sys.path.insert(0, str(ROOT))
    os.environ["JARVIS_DATA"] = str(DATA)      # same store the server uses
    from core import confirm as gate, push as push_mod
    # a different process owns the server's gate, so drive the server's gate
    # through its own HTTP surface: the task-push approval card.
    ran = threading.Event()
    st, d = api("/api/push/respond", "POST",
                {"token": push_mod.sign_action(
                    {"kind": "confirm", "accept": True, "iat": 0})})
    check("push.respond_zero_iat_401", st == 401, str(st))
    st, d = api("/api/push/respond", "POST",
                {"token": push_mod.sign_action({"kind": "bogus", "accept": True})})
    check("push.respond_unknown_kind_400", st == 400, str(st))
    st, d = api("/api/push/test", "POST", {}, token=token)
    check("push.test_attempts_send",
          st == 200 and "sent" in d and d.get("subs") == 1, f"{st} {d}")
    st, d = api("/api/push/unsubscribe", "POST",
                {"endpoint": sub["endpoint"]}, token=token)
    check("push.unsubscribe", st == 200 and d.get("subs") == 0, str(d))
    st, d = api("/api/push/test", "POST", {}, token=token)
    check("push.test_without_subs_400", st == 400, str(st))

    # boot must be clean
    log = LOG.read_text(errors="replace") if LOG.exists() else ""
    check("boot.no_traceback", "Traceback (most recent call last)" not in log)
    check("boot.scheduler_line", "[Scheduler]" in log,
          [l for l in log.splitlines() if "Scheduler" in l][:2])

    approval_tap_checks(token)


# ── the crown jewel: a phone tap resolving a real gate ───────────────────────

class _FakeLimb:
    """Minimal jarvisd stand-in: records every request the server forwards."""

    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def connect(self) -> str | None:
        from websockets.sync.client import connect
        try:
            self.ws = connect(self.url, open_timeout=8, close_timeout=2)
        except Exception as e:
            return str(e)
        threading.Thread(target=self._loop, daemon=True).start()
        return None

    def _send(self, obj: dict) -> None:
        try:
            self.ws.send(json.dumps(obj))
        except Exception:
            pass

    def _loop(self) -> None:
        try:
            for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                with self._lock:
                    self.requests.append(msg)
                if msg.get("type") == "exec":
                    # Mirror jarvisd: anything outside the allowlist comes
                    # back as needs_approval until the server says a human
                    # approved it. A fake that obeyed everything would make
                    # the gate untestable.
                    if not (msg.get("payload") or {}).get("approved"):
                        self._send({"id": msg.get("id"), "ok": False,
                                    "needs_approval": True,
                                    "reason": "not on the auto-run allowlist"})
                    else:
                        self._send({"id": msg.get("id"), "ok": True, "rc": 0,
                                    "out": "ran on the fake pc\n"})
                elif msg.get("type") in ("ping", "status"):
                    self._send({"id": msg.get("id"), "ok": True,
                                "status": {"host": "fake-pc", "os": "TestOS",
                                           "py": "3.11", "cwd": "/tmp",
                                           "uptime_s": 1, "cpu": 1, "ram": 2,
                                           "caps": {"exec": True, "fs": True},
                                           "jarvisd": "1.0"}})
        except Exception:
            pass

    def execs(self) -> list[dict]:
        with self._lock:
            return [m for m in self.requests if m.get("type") == "exec"]

    def approved_execs(self) -> list[dict]:
        return [m for m in self.execs()
                if (m.get("payload") or {}).get("approved")]

    def close(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


def approval_tap_checks(token: str) -> None:
    """ACCEPT and REJECT from a signed token, resolved by the server's own gate.

    The dangerous half of Web Push is not delivery — it is that a tap must
    move exactly the same gate the on-screen button moves, and nothing else.
    """
    print("== one-tap approval through /api/push/respond ==", flush=True)
    from core import push as push_mod

    st, d = api("/api/devices", token=token)
    code = d.get("pair_code") or ""
    st, body = api("/api/agent/pair", "POST",
                   {"code": code, "name": "p3-limb", "os": "TestOS",
                    "caps": {"exec": True, "fs": True}})
    dev_token = body.get("token") or ""
    if st != 200 or not dev_token:
        check("tap.pair", False, f"{st} {body}")
        return
    limb = _FakeLimb(f"ws://127.0.0.1:{PORT}/ws/agent?token={dev_token}")
    err = limb.connect()
    check("tap.limb_online", err is None, err or "")
    time.sleep(0.6)

    # A command outside the allowlist must park at the gate, NOT run.
    st, r = api("/api/devices/exec", "POST",
                {"device": "p3-limb", "cmd": "reboot the machine now"},
                token=token)
    check("tap.exec_needs_approval",
          st == 200 and r.get("needs_approval") is True,
          f"{st} {r}")
    time.sleep(0.4)
    # The daemon did see the probe (that is how it knew to refuse) — what must
    # NOT exist is an approved run.
    check("tap.nothing_ran_yet", not limb.approved_execs(),
          f"probes={len(limb.execs())}")

    # The phone taps ACCEPT: signed token, no bearer header.
    tok = push_mod.sign_action({"kind": "confirm", "accept": True})
    st, r = api("/api/push/respond", "POST", {"token": tok})
    check("tap.accept_200",
          st == 200 and r.get("ok") and r.get("had_pending") is True,
          f"{st} {r}")
    for _ in range(24):
        got = limb.approved_execs()
        if got:
            break
        time.sleep(0.25)
    check("tap.accept_runs_once", len(got) == 1, str(limb.execs()))
    check("tap.accept_flagged", bool(got and got[0].get("payload", {}).get("approved")),
          str(got[0] if got else {}))

    # REJECT must leave the device untouched.
    st, r = api("/api/devices/exec", "POST",
                {"device": "p3-limb", "cmd": "delete everything now"},
                token=token)
    check("tap.exec2_needs_approval", r.get("needs_approval") is True, str(r))
    before = len(limb.approved_execs())
    st, r = api("/api/push/respond", "POST",
                {"token": push_mod.sign_action({"kind": "confirm", "accept": False})})
    time.sleep(1.0)
    check("tap.reject_no_exec",
          st == 200 and r.get("ok") and len(limb.approved_execs()) == before,
          f"st={st} approved {before}→{len(limb.approved_execs())}")

    # A tap with nothing pending is a no-op, not a crash.
    st, r = api("/api/push/respond", "POST",
                {"token": push_mod.sign_action({"kind": "confirm", "accept": True})})
    check("tap.no_pending_ok", st == 200 and r.get("had_pending") is False, f"{st} {r}")
    limb.close()


def main() -> int:
    unit_scheduler()
    unit_push()
    proc = start_server()
    try:
        server_checks()
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
