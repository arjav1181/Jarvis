"""Phase 2 integration test: tasks (agentic coding).

Usage:
  python3 e2e/test_tasks.py

Covers, without any model calls:
  1. TaskStore units (create/update/log/default_where/persistence)
  2. jarvisd worktree runner against a FAKE opencode binary — real git
     worktree, real diff, marker-guarded approved push to a bare remote
  3. /api/tasks endpoints over HTTP: auth, config, create→done via a fake
     daemon (chunks routed into the task log), WS task broadcasts, the
     approval-gated push (confirm card → /api/confirm → daemon push),
     cancel, retry, and the Space missing-repo failure path
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_TASKS_E2E_PORT") or "3102")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_tasks"
LOG = Path("/tmp/jarvis_e2e_tasks.log")

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
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _kill_stray() -> None:
    me = str(os.getpid())
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or pid == me:
            continue
        try:
            env = open(f"/proc/{pid}/environ", "rb").read()
        except Exception:
            continue
        if f"JARVIS_PORT={PORT}".encode() in env:
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass
    time.sleep(0.3)


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


def start_server() -> subprocess.Popen:
    _kill_stray()
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} already in use")
    if LOG.exists():
        LOG.unlink()
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


def poll(fn, timeout: float = 15.0, interval: float = 0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    return None


# ── fake daemon for task requests ────────────────────────────────────────────

class TaskFake:
    """Plays jarvisd's side for task.run/push/cancel. 'slow' prompts delay
    the run reply in a background thread so cancel can land mid-flight."""

    def __init__(self, url: str):
        import websockets.sync.client as wsc
        self._wsc = wsc
        self.url = url
        self.ws = None
        self.thread = None
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def connect(self, timeout: float = 8.0) -> str | None:
        try:
            self.ws = self._wsc.connect(self.url, open_timeout=timeout,
                                        close_timeout=2)
        except Exception as e:
            return f"connect failed: {e}"
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return None

    def _loop(self) -> None:
        try:
            for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                with self._lock:
                    self.requests.append(msg)
                t = msg.get("type")
                if t in ("task.run", "task.push", "task.cancel"):
                    if t == "task.run":
                        th = threading.Thread(
                            target=self._handle_run, args=(msg,), daemon=True)
                        th.start()
                    else:
                        self._reply(msg)
        except Exception:
            pass

    def _handle_run(self, msg: dict) -> None:
        payload = msg.get("payload") or {}
        prompt = str(payload.get("prompt") or "")
        if "slow" in prompt:
            time.sleep(4)
        rid = msg.get("id")
        self._send({"type": "chunk", "id": rid, "text": "fake-opencode: start"})
        self._send({"type": "chunk", "id": rid, "text": "fake-opencode: done"})
        if "failnow" in prompt:
            self._send({"id": rid, "ok": False, "rc": 2,
                        "error": "opencode exited rc=2", "tail": "boom"})
            return
        if "no-wt" in prompt:
            self._send({"id": rid, "ok": True, "rc": 0, "tail": "no changes",
                        "diff": "", "branch": "", "wt": "",
                        "worktree": False, "changed": []})
            return
        self._send({
            "id": rid, "ok": True, "rc": 0,
            "tail": "fake-opencode: done",
            "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n",
            "branch": f"jarvis/{payload.get('task_id', 'x')}",
            "wt": "/tmp/.jarvis-wt-fake-" + str(payload.get("task_id")),
            "worktree": True, "changed": ["app.py"],
        })

    def _reply(self, msg: dict) -> None:
        payload = msg.get("payload") or {}
        if msg.get("type") == "task.push":
            if not payload.get("approved"):
                self._send({"id": msg.get("id"), "ok": False,
                            "needs_approval": True, "reason": "gate"})
            else:
                self._send({"id": msg.get("id"), "ok": True,
                            "remote": "origin/" + str(payload.get("branch"))})
        elif msg.get("type") == "task.cancel":
            self._send({"id": msg.get("id"), "ok": True,
                        "note": "killed"})

    def _send(self, obj: dict) -> None:
        try:
            self.ws.send(json.dumps(obj))
        except Exception:
            pass

    def saw(self, mtype: str, pred=None) -> bool:
        with self._lock:
            return any(m.get("type") == mtype and (pred is None or pred(m))
                       for m in self.requests)

    def close(self) -> None:
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


# ── unit checks ──────────────────────────────────────────────────────────────

def unit_store() -> None:
    from core.tasks import TaskStore
    d = Path(tempfile.mkdtemp())
    s = TaskStore(root=d)
    t = s.create(prompt="unit prompt", where="device", repo="/x")
    check("store.create", t["status"] == "queued" and t["id"].startswith("t-"))
    s.start(t["id"])
    s.append_log(t["id"], "line1")
    s.finish(t["id"], "done", diff="d", branch="b")
    check("store.lifecycle",
          s.get(t["id"])["status"] == "done"
          and s.read_log(t["id"]) == "line1")
    s.set_default_where("space")
    s2 = TaskStore(root=d)
    check("store.persist",
          s2.get(t["id"])["status"] == "done"
          and s2.default_where() == "space"
          and s2.resolve_where("auto") == "space")
    shutil.rmtree(d, ignore_errors=True)


def _git(path: str, *args: str) -> None:
    subprocess.run(["git", "-C", path, *args], check=True,
                   capture_output=True, shell=False)


def unit_daemon() -> None:
    """jarvisd task.run/push against a fake opencode — real worktree math."""
    import jarvisd
    tmp = Path(tempfile.mkdtemp())
    try:
        bindir = tmp / "bin"
        bindir.mkdir()
        fake = bindir / "opencode"
        fake.write_text(
            "#!/bin/sh\n"
            'case "$*" in *failme*) echo boom; exit 1;; esac\n'
            'echo "line one"\n'
            'echo "line two"\n'
            'echo "made by opencode" > feature.txt\n'
            "exit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(bindir) + os.pathsep + old_path

        repo = tmp / "proj"
        repo.mkdir()
        _git(str(repo), "init", "-b", "main")
        _git(str(repo), "config", "user.email", "e2e@test")
        _git(str(repo), "config", "user.name", "e2e")
        (repo / "app.py").write_text("old\n", encoding="utf-8")
        _git(str(repo), "add", "-A")
        _git(str(repo), "commit", "-m", "init")
        bare = tmp / "remote.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       capture_output=True)
        _git(str(repo), "remote", "add", "origin", str(bare))

        async def _send(o):
            pass

        res = asyncio.run(jarvisd._run_task(
            {"task_id": "t-unit1", "prompt": "add feature",
             "cwd": str(repo)}, _send))
        wt = res.get("wt") or ""
        check("dmon.worktree_run",
              res.get("ok") is True and res.get("worktree") is True
              and wt and Path(wt, ".jarvis-worktree").exists()
              and "feature.txt" in (res.get("diff") or ""),
              f"wt={wt} diff_lines={len((res.get('diff') or '').splitlines())}")
        check("dmon.changed_list",
              res.get("changed") == ["feature.txt"]
              and res.get("branch") == "jarvis/t-unit1",
              f"changed={res.get('changed')} branch={res.get('branch')}")

        r = asyncio.run(jarvisd._task_push(
            {"wt": wt, "branch": "jarvis/t-unit1"}))
        check("dmon.push_unapproved_blocked",
              r.get("needs_approval") is True, str(r)[:100])

        evil = tmp / "evil-wt"
        evil.mkdir()
        r = asyncio.run(jarvisd._task_push(
            {"approved": True, "wt": str(evil),
             "branch": "jarvis/t-unit1"}))
        check("dmon.push_non_marker_refused",
              r.get("ok") is False and "worktree" in str(r.get("error", "")),
              str(r)[:100])

        r = asyncio.run(jarvisd._task_push(
            {"approved": True, "wt": wt, "branch": "jarvis/t-unit1"}))
        out = subprocess.run(
            ["git", "--git-dir", str(bare), "branch", "--list",
             "jarvis/t-unit1"], capture_output=True, text=True)
        tree = subprocess.run(
            ["git", "--git-dir", str(bare), "ls-tree", "-r",
             "jarvis/t-unit1", "--name-only"],
            capture_output=True, text=True)
        check("dmon.approved_push_lands",
              r.get("ok") is True and "jarvis/t-unit1" in out.stdout
              and ".jarvis-worktree" not in tree.stdout
              and "feature.txt" in tree.stdout,
              f"push={r} tree={tree.stdout.split()}")

        res2 = asyncio.run(jarvisd._run_task(
            {"task_id": "t-unit2", "prompt": "failme now",
             "cwd": str(repo)}, _send))
        check("dmon.failed_run", res2.get("ok") is False
              and res2.get("rc") == 1, str(res2)[:120])

        r = asyncio.run(jarvisd._task_cancel({"task_id": "t-none"}))
        check("dmon.cancel_unknown", r.get("ok") is True, str(r))
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)


# ── server section ───────────────────────────────────────────────────────────

def run_server_checks() -> None:
    token = get_token()

    st, _ = api("/api/tasks")
    check("api.unauth_401", st == 401, f"st={st}")

    st, d = api("/api/tasks", token=token)
    check("api.list_empty", st == 200 and d.get("tasks") == []
          and d.get("default_where") == "device", f"{st} {d}")

    st, d = api("/api/tasks/config", "POST",
                {"default_where": "space"}, token=token)
    st2, d2 = api("/api/tasks", token=token)
    check("api.config_roundtrip",
          st == 200 and d2.get("default_where") == "space",
          f"{st} {d2.get('default_where')}")
    api("/api/tasks/config", "POST", {"default_where": "device"},
        token=token)

    st, d = api("/api/tasks", "POST",
                {"prompt": "needs a device", "where": "device"},
                token=token)
    check("api.no_device_error", st == 400 and "online" in d.get("error", ""),
          f"{st} {d}")

    # pair the fake daemon
    st, dd = api("/api/devices", token=token)
    code = dd.get("pair_code") or ""
    st, b = api("/api/agent/pair", "POST",
                {"code": code, "name": "limb-task"})
    check("pair.ok", st == 200 and b.get("token"), f"{st}")
    fake = TaskFake(f"ws://127.0.0.1:{PORT}/ws/agent?token={b['token']}")
    err = fake.connect()
    check("fake.connected", err is None, err or "")

    # browser WS — collect task/confirm broadcasts
    import websockets.sync.client as wsc
    ws_msgs: list[dict] = []
    bws = wsc.connect(f"ws://127.0.0.1:{PORT}/ws?token={token}",
                      open_timeout=8, close_timeout=2)

    def _drain():
        try:
            for raw in bws:
                try:
                    ws_msgs.append(json.loads(raw))
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=_drain, daemon=True).start()

    # create → run → done (chunks must land in the task log)
    st, t = api("/api/tasks", "POST",
                {"prompt": "normal run", "where": "device"},
                token=token)
    tid = t.get("id", "")
    check("api.create", st == 200 and tid.startswith("t-"), f"{st} {t}")
    done = poll(lambda: (lambda r: r[1] if r[0] == 200
                         and r[1].get("status") in ("done", "failed")
                         else None)(api(f"/api/tasks/{tid}", token=token)),
                timeout=15)
    check("api.task_done", bool(done) and done.get("status") == "done",
          str(done and done.get("status")))
    if done:
        check("api.task_diff",
              bool(done.get("diff")) and done.get("branch")
              and done.get("wt") and done.get("pushed") is False,
              f"branch={done.get('branch')}")
    st, lg = api(f"/api/tasks/{tid}/log?tail=100", token=token)
    logtxt = lg.get("log", "")
    check("api.log_has_chunks",
          "fake-opencode: start" in logtxt and "fake-opencode: done" in logtxt,
          logtxt.replace("\n", " | ")[:120])
    got = poll(lambda: any(m.get("type") == "task" and
                           m.get("event") == "created" and
                           m.get("task", {}).get("id") == tid
                           for m in ws_msgs), timeout=6)
    check("ws.task_events", bool(got),
          f"{sum(1 for m in ws_msgs if m.get('type')=='task')} task msgs")

    # push → confirm card → human CONFIRM → fake receives approved push
    st, d = api(f"/api/tasks/{tid}/push", "POST", token=token)
    check("push.pending_card", st == 200 and d.get("pending"),
          str(d)[:120])
    check("push.not_yet_sent",
          not fake.saw("task.push"), "daemon must wait for CONFIRM")
    got = poll(lambda: any(m.get("type") == "confirm" and
                           "Push" in str(m.get("title", ""))
                           for m in ws_msgs), timeout=6)
    check("ws.confirm_card", bool(got), "")
    st, _ = api("/api/confirm", "POST", {"accept": True}, token=token)
    pushed = poll(lambda: (lambda r: r[1].get("pushed")
                           if r[0] == 200 else None)(
                       api(f"/api/tasks/{tid}", token=token)), timeout=10)
    got = poll(lambda: fake.saw(
        "task.push", lambda m: (m.get("payload") or {}).get("approved") is True),
        timeout=8)
    check("push.after_confirm", pushed is True and got,
          f"pushed={pushed} daemon_approved={got}")

    st, d = api(f"/api/tasks/{tid}/push", "POST", token=token)
    check("push.twice_rejected", st == 400 and "pushed" in d.get("error", ""),
          f"{st} {d}")

    # task without worktree → push refused
    st, t2 = api("/api/tasks", "POST",
                 {"prompt": "no-wt changes", "where": "device"},
                 token=token)
    tid2 = t2.get("id", "")
    poll(lambda: (lambda r: r[1].get("status") in ("done", "failed")
                  and r[1] or None)(
        api(f"/api/tasks/{tid2}", token=token)), timeout=10)
    st, d = api(f"/api/tasks/{tid2}/push", "POST", token=token)
    check("push.no_worktree_error", st == 400
          and "worktree" in d.get("error", ""), f"{st} {d}")

    # cancel mid-flight (fake delays the run reply 4s)
    st, t3 = api("/api/tasks", "POST",
                 {"prompt": "slow task", "where": "device"},
                 token=token)
    tid3 = t3.get("id", "")
    time.sleep(0.8)
    st, d = api(f"/api/tasks/{tid3}/cancel", "POST", token=token)
    st2, r3 = api(f"/api/tasks/{tid3}", token=token)
    time.sleep(4)   # late reply must NOT overwrite the cancel
    _, r3b = api(f"/api/tasks/{tid3}", token=token)
    check("cancel.works_and_sticks",
          st == 200 and r3.get("status") == "cancelled"
          and r3b.get("status") == "cancelled"
          and fake.saw("task.cancel"),
          f"st={st} status={r3.get('status')}→{r3b.get('status')}")

    # retry → new id, runs again
    st, d = api(f"/api/tasks/{tid2}/retry", "POST", token=token)
    ok = st == 200 and d.get("id") and d["id"] != tid2
    if ok:
        poll(lambda: (lambda r: r[1].get("status") in ("done", "failed")
                      and r[1] or None)(
            api(f"/api/tasks/{d['id']}", token=token)), timeout=10)
    check("retry.new_task", bool(ok), f"{st} {d.get('id')}")

    # space path with a missing repo → fails fast, no opencode run
    st, t4 = api("/api/tasks", "POST",
                 {"prompt": "anything", "where": "space",
                  "repo": "/nonexistent-e2e-repo-xyz"},
                 token=token)
    fin = poll(lambda: (lambda r: r[1] if r[0] == 200
                        and r[1].get("status") in ("done", "failed")
                        else None)(api(f"/api/tasks/{t4.get('id')}",
                                       token=token)), timeout=10)
    check("space.missing_repo_fails",
          bool(fin) and fin.get("status") == "failed"
          and "does not exist" in fin.get("tail", ""),
          str(fin and fin.get("tail"))[:100])

    try:
        bws.close()
    except Exception:
        pass
    fake.close()


def main() -> int:
    print("== unit: TaskStore ==", flush=True)
    unit_store()
    print("== unit: jarvisd worktree runner (fake opencode) ==", flush=True)
    unit_daemon()
    print("== server: /api/tasks + approvals ==", flush=True)
    proc = start_server()
    try:
        run_server_checks()
    finally:
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
