"""Phase 1 integration test: jarvisd pairing + /ws/agent protocol.

Usage:
  python3 e2e/test_agent.py

Boots main.py in server mode on :3101 with a fresh JARVIS_DATA, then checks:
  pairing (rate-limit last), token auth on /ws/agent, the request/reply
  protocol (including chunk-vs-reply dispatch), exec/fs allowlists in
  jarvisd, and device_gone on mid-request disconnect.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_AGENT_E2E_PORT") or "3101")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_agent"
LOG = Path("/tmp/jarvis_e2e_agent.log")

sys.path.insert(0, str(ROOT))

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
    # SO_REUSEADDR: connections from the previous run leave TIME_WAIT entries
    # on this port — uvicorn binds with reuse too, so TIME_WAIT is not a
    # conflict; only a live listener is.
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
        if b"JARVIS_PORT=3101" in env:
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass
    time.sleep(0.3)


def start_server() -> subprocess.Popen:
    _kill_stray()
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} already in use")
    if LOG.exists():
        LOG.unlink()
    # Fresh data root every run: pairing state (agents.json) must start clean.
    import shutil
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "JARVIS_MODE": "server",
        "JARVIS_PORT": str(PORT),
        "JARVIS_DATA": str(DATA),
        "PYTHONUNBUFFERED": "1",
    })
    logf = LOG.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        cwd=str(ROOT), env=env,
        stdout=logf, stderr=subprocess.STDOUT,
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
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
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


def api(path: str, method: str = "GET", body: dict | None = None,
        token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except Exception:
            return e.code, {"_raw": raw[:200].decode(errors="replace")}
    except Exception as e:
        return 0, {"error": str(e)}


def get_token() -> str:
    st, key = api("/api/bootstrap-key", "POST")
    if st != 200 or not key.get("key"):
        raise RuntimeError(f"bootstrap-key failed: {st} {key}")
    with urllib.request.urlopen(
            f"{BASE}/auto-login?key={key['key']}", timeout=10) as r:
        html = r.read().decode(errors="replace")
    m = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html)
    if not m:
        raise RuntimeError("no jarvis_token in auto-login page")
    return m.group(1)


# ── fake daemon ──────────────────────────────────────────────────────────────

class FakeDaemon:
    """Plays jarvisd's side of /ws/agent: replies to requests, streams a
    chunk BEFORE the reply on ping (dispatch-order regression), and can go
    silent+dead mid-request to exercise device_gone."""

    def __init__(self, url: str):
        import websockets.sync.client as wsc
        self._wsc = wsc
        self.url = url
        self.ws = None
        self.thread = None
        self.silent = False
        self.die_on_request = False
        self.requests: list[dict] = []
        self.closed = threading.Event()
        self._lock = threading.Lock()

    def connect(self, timeout: float = 8.0) -> str | None:
        try:
            self.ws = self._wsc.connect(self.url, open_timeout=timeout,
                                        close_timeout=2)
        except Exception as e:
            return f"connect failed: {e}"
        self.closed.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return None

    def _loop(self) -> None:
        ws = self.ws
        try:
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                with self._lock:
                    self.requests.append(msg)
                if self.die_on_request:
                    # Device "drops off the network" mid-request.
                    try:
                        ws.close()
                    except Exception:
                        pass
                    break
                if self.silent:
                    continue
                rid = msg.get("id")
                mtype = msg.get("type")
                if mtype == "ping" or mtype == "status":
                    # Chunk FIRST: if the server resolves futures before
                    # checking typed messages, this truncates the reply.
                    self._send({"type": "chunk", "id": rid, "text": "streaming…"})
                    self._send({
                        "id": rid, "ok": True,
                        "status": {
                            "host": "fake-pc", "os": "TestOS 1.0",
                            "py": "3.11.0", "cwd": "/tmp",
                            "uptime_s": 42, "cpu": 7.5, "ram": 33.3,
                            "caps": {"exec": True, "fs": True,
                                     "opencode": False, "screen": False},
                            "jarvisd": "1.0",
                        },
                    })
                elif mtype == "exec":
                    self._send({"id": rid, "ok": True, "rc": 0,
                                "out": "hi from limb\n"})
                else:
                    self._send({"id": rid, "ok": False,
                                "error": f"fake cannot handle {mtype}"})
        except Exception:
            pass
        finally:
            self.closed.set()

    def _send(self, obj: dict) -> None:
        try:
            self.ws.send(json.dumps(obj))
        except Exception:
            pass

    def close(self) -> None:
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.closed.set()

    def wait_closed(self, timeout: float = 5.0) -> bool:
        return self.closed.wait(timeout)

    def saw(self, mtype: str) -> bool:
        with self._lock:
            return any(m.get("type") == mtype for m in self.requests)


# ── jarvisd unit checks (imported directly) ──────────────────────────────────

async def _jarvisd_exec(cmd: str) -> dict:
    import jarvisd
    return await jarvisd._exec(cmd)


def run_async(coro):
    return asyncio.run(coro)


def main() -> int:
    print("Phase 1 agent E2E — booting server…", flush=True)
    proc = start_server()
    daemon: FakeDaemon | None = None
    try:
        token = get_token()
        check("auth.bootstrap_token", bool(token) and len(token) > 20,
              f"len={len(token)}")

        # 1. Unauthorized access -------------------------------------------------
        st, _ = api("/api/devices")
        check("http.devices_unauth", st == 401, f"got {st}")

        # 2. Clean state + pair code --------------------------------------------
        st, d = api("/api/devices", token=token)
        check("http.devices_empty", st == 200 and d.get("devices") == [],
              f"st={st} {d}")
        pair_code = d.get("pair_code") or ""
        check("pair.code_8chars", len(pair_code) == 8, f"code={pair_code!r}")

        # 3. Pairing --------------------------------------------------------------
        st, body = api("/api/agent/pair", "POST",
                       {"code": "WRONG123", "name": "nope"})
        check("pair.bad_code_403", st == 403, f"st={st} {body}")
        st, body = api("/api/agent/pair", "POST",
                       {"code": pair_code, "name": "e2e-limb",
                        "os": "TestOS 1.0",
                        "caps": {"exec": True, "fs": True}})
        dev_token = body.get("token") or ""
        check("pair.good_code_token", st == 200 and len(dev_token) > 30,
              f"st={st}")

        st, d = api("/api/devices", token=token)
        names = [x["name"] for x in d.get("devices", [])]
        check("pair.appears_offline", "e2e-limb" in names
              and not any(x["online"] for x in d["devices"]),
              f"{d.get('devices')}")

        # 4. WS auth ---------------------------------------------------------------
        # Starlette rejects a WebSocket whose handler returns before accept()
        # with HTTP 403 at the handshake (same pattern as /ws/audio-out); a
        # server that accepted-then-closed would surface 4001 instead. Either
        # way the socket must NOT come up.
        from websockets.sync.client import connect
        bad_url = f"ws://127.0.0.1:{PORT}/ws/agent?token=not-a-real-token"
        outcome = "connected"
        try:
            ws = connect(bad_url, open_timeout=6, close_timeout=3)
            try:
                ws.recv(timeout=2)
                outcome = "connected_and_recv"
            except Exception as e:
                outcome = f"closed: {e}"
            ws.close()
        except Exception as e:
            outcome = f"rejected: {e}"
        check("ws.bad_token_rejected",
              outcome.startswith("rejected") or outcome.startswith("closed"),
              outcome)

        # 5. Good token: online, ping (chunk-first), exec ---------------------------
        url = f"ws://127.0.0.1:{PORT}/ws/agent?token={dev_token}"
        daemon = FakeDaemon(url)
        err = daemon.connect()
        check("ws.connect", err is None, err or "")
        time.sleep(0.5)

        st, d = api("/api/devices", token=token)
        online = [x for x in d.get("devices", []) if x.get("online")]
        check("ws.device_online", len(online) == 1
              and online[0]["name"] == "e2e-limb", f"{d.get('devices')}")

        t0 = time.time()
        st, r = api("/api/devices/ping", "POST", {"device": "e2e-limb"},
                    token=token)
        elapsed = time.time() - t0
        check("proto.ping_ok_despite_chunk",
              st == 200 and r.get("ok") and r.get("status", {}).get("host")
              == "fake-pc",
              f"st={st} {r} ({elapsed:.2f}s)")
        check("proto.ping_ms", isinstance(r.get("ms"), int), f"{r.get('ms')}")
        check("proto.chunk_before_reply_seen", daemon.saw("status"),
              f"{len(daemon.requests)} reqs, types="
              f"{[m.get('type') for m in daemon.requests]}")

        st, r = api("/api/devices/exec", "POST",
                    {"device": "e2e-limb", "cmd": "echo hi"}, token=token)
        check("proto.exec_ok", st == 200 and r.get("ok")
              and "hi from limb" in (r.get("out") or ""), f"{st} {r}")

        st, r = api("/api/devices/exec", "POST", {"device": "e2e-limb"},
                    token=token)
        check("http.exec_no_cmd_400", st == 400, f"st={st}")

        # 6. jarvisd unit: allowlist + fs guards -------------------------------------
        res = run_async(_jarvisd_exec("echo hello-jarvis"))
        check("jarvisd.exec_echo_ok", res.get("ok") and "hello-jarvis"
              in res.get("out", ""), f"{res}")
        res = run_async(_jarvisd_exec("python -c 'print(1)'"))
        check("jarvisd.exec_python_needs_approval",
              res.get("needs_approval") is True, f"{res}")
        res = run_async(_jarvisd_exec("rm -rf /tmp/x"))
        check("jarvisd.exec_rm_needs_approval",
              res.get("needs_approval") is True, f"{res}")
        res = run_async(_jarvisd_exec("git push origin main"))
        check("jarvisd.exec_git_push_blocked",
              res.get("needs_approval") is True, f"{res}")
        res = run_async(_jarvisd_exec("echo a && echo b"))
        check("jarvisd.exec_metachars_blocked",
              res.get("needs_approval") is True, f"{res}")
        import jarvisd
        g = jarvisd._fs_guard("/home/u/.ssh/id_rsa")
        check("jarvisd.fs_guard_ssh", bool(g), g or "NOT BLOCKED")
        g = jarvisd._fs_guard("/repo/config/api_keys.json")
        check("jarvisd.fs_guard_apikeys", bool(g), g or "NOT BLOCKED")
        g = jarvisd._fs_guard("/tmp/harmless.txt")
        check("jarvisd.fs_guard_allows_normal", g is None, g or "")

        # 6b. approved=True (human pressed CONFIRM) steps past the allowlist
        res = run_async(jarvisd._exec("python -c 'print(42)'", approved=True))
        check("jarvisd.approved_runs_python",
              res.get("ok") and "42" in (res.get("out") or "")
              and not res.get("needs_approval"), f"{res}")
        probe = Path("/tmp/jarvis-agent-e2e-probe")
        probe.mkdir(exist_ok=True)
        (probe / "f.txt").write_text("x")
        res = run_async(jarvisd._exec(f"rm -rf {probe}", approved=True))
        check("jarvisd.approved_runs_rm",
              res.get("ok") and not probe.exists(), f"{res}")
        res = run_async(jarvisd._exec("rm -rf /tmp/other"))
        check("jarvisd.unapproved_rm_still_blocked",
              res.get("needs_approval") is True, f"{res}")

        # 6c. confirm gate — the flow the device tool now parks approvals on
        from core import confirm as cg
        shown: list = []
        hidden: list = []
        ran: list = []
        cg.bind(show=lambda t, d: shown.append((t, d)),
                hide=lambda: hidden.append(True),
                log=lambda m: None)
        msg = cg.request("test-key", "Run on e2e-limb?", "echo hi",
                         lambda: ran.append("yes") or "Done.")
        check("gate.request_pending",
              "CONFIRMATION_PENDING" in msg and shown
              and cg.pending_title() == "Run on e2e-limb?", msg[:70])
        cg.resolve(False)
        time.sleep(0.2)
        check("gate.cancel_runs_nothing", not ran and hidden,
              f"ran={ran} hidden={hidden}")
        msg = cg.request("test-key", "Run on e2e-limb?", "echo hi",
                         lambda: ran.append("yes") or "Done.")
        check("gate.request_again", "CONFIRMATION_PENDING" in msg, msg[:40])
        cg.resolve(True)
        time.sleep(0.3)
        check("gate.confirm_runs_action", ran == ["yes"], f"ran={ran}")
        check("gate.pending_cleared", cg.pending_title() == "",
              cg.pending_title())

        # 6d. /api/confirm endpoint (what the CONFIRM card presses)
        st, _ = api("/api/confirm", "POST", {"accept": True})
        check("confirm.unauth_401", st == 401, f"st={st}")
        st, b = api("/api/confirm", "POST", {"accept": True}, token=token)
        check("confirm.authed_ok", st == 200 and b.get("ok") is True,
              f"{st} {b}")

        # 6e. re-pairing the same name retires the old credential.
        # (Stale twins piled up live: exec targeted an offline duplicate.)
        st, b1 = api("/api/agent/pair", "POST",
                     {"code": pair_code, "name": "dup-limb"})
        st2, b2 = api("/api/agent/pair", "POST",
                      {"code": pair_code, "name": "dup-limb"})
        st3, d = api("/api/devices", token=token)
        dupes = [x for x in d.get("devices", []) if x["name"] == "dup-limb"]
        check("pair.retires_stale_twin",
              st == 200 and st2 == 200 and st3 == 200 and len(dupes) == 1,
              f"pairs={st}/{st2} records={len(dupes)}")

        # 6f. target selection — online twin beats the offline exact twin;
        # an exact offline name beats a substring match on a live device
        # (wrong box is worse than offline).
        from dashboard.server import DashboardServer
        srv = object.__new__(DashboardServer)
        srv._agents = {"old": {"name": "twin-limb"},
                       "new": {"name": "twin-limb"}}
        srv._agent_socks = {"new": object()}
        h, _ = srv.agent_target("twin-limb")
        check("target.online_twin_wins", h == "new", f"picked={h}")
        srv._agents = {"off": {"name": "alpha"}, "on": {"name": "alpha-2"}}
        srv._agent_socks = {"on": object()}
        h, _ = srv.agent_target("alpha")
        check("target.exact_offline_beats_substring", h == "off",
              f"picked={h}")

        # 7. device_gone: request in flight when the limb dies ---------------------
        daemon.silent = True
        daemon.die_on_request = True
        t0 = time.time()
        st, r = api("/api/devices/ping", "POST", {"device": "e2e-limb"},
                    token=token)
        elapsed = time.time() - t0
        check("proto.device_gone_fast",
              st == 200 and r.get("error") == "device_gone" and elapsed < 6,
              f"{st} {r} ({elapsed:.2f}s)")
        daemon.silent = False
        daemon.die_on_request = False
        daemon.wait_closed(5)
        time.sleep(0.5)

        st, d = api("/api/devices", token=token)
        check("ws.device_offline", d.get("devices")
              and all(not x["online"] for x in d["devices"]),
              f"{d.get('devices')}")

        # 8. Rate limit (LAST — locks pairing for 60s) --------------------------------
        got_lock = False
        attempts = 0
        for i in range(14):
            attempts += 1
            st, _ = api("/api/agent/pair", "POST",
                        {"code": "WRONG999", "name": "x"})
            if st == 429:
                got_lock = True
                break
        check("rate_limit.locks", got_lock, f"after {attempts} bad codes")
        st, _ = api("/api/agent/pair", "POST", {"code": pair_code, "name": "y"})
        check("rate_limit.blocks_correct_too", st == 429, f"st={st}")

        # 9. Boot log sanity ------------------------------------------------------------
        log = LOG.read_text(errors="replace") if LOG.exists() else ""
        check("boot.pair_code_logged", "Agent pair code:" in log)
        check("boot.no_traceback", "Traceback" not in log)

    finally:
        if daemon:
            daemon.close()
        stop_server(proc)

    print(f"\nPASS {len(_passes)}  FAIL {len(_failures)}", flush=True)
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
