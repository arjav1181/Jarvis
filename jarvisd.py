#!/usr/bin/env python3
"""jarvisd — the JARVIS limb daemon (Phase 1: device connectivity).

Runs on YOUR machines (PC / NAS / VPS) and dials OUT to the JARVIS Space
over WebSocket — no port-forwarding, no inbound firewall holes. The Space
sends requests ({id, type, payload}); this daemon answers them.

  Pair once:    python jarvisd.py --server https://abc1181-jaarvis.hf.space --pair ABCD1234
  Run:          python jarvisd.py
  Options:      --name mypc  --server URL  --token TOKEN  --config path

Config lives in jarvisd.json NEXT TO THIS FILE (server, name, token) — it is
gitignored and never deployed. Only the token's sha256 ever exists server-side.

Capabilities: status, exec (allowlisted shell), fs.list, fs.read (guarded),
run_opencode (streams output back as chunks). Anything outside the allowlist
comes back as needs_approval — Phase 5 turns that into a one-tap phone confirm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

CONFIG_PATH = HERE / "jarvisd.json"
VERSION = "1.0"
_BOOT = time.time()

# ── exec policy ──────────────────────────────────────────────────────────────
# Auto-run: read-only / inspection commands with no shell metacharacters.
# Everything else (rm, git push, python -c, npm run, curl, shutdown, unknown)
# → needs_approval. Phase 5 wires that into the phone confirm gate.

_SHELL_METAS = set("&|;<>`$(){}!\n")

_SAFE_CMD = {
    "ls", "dir", "pwd", "echo", "whoami", "hostname", "date", "uptime",
    "uname", "df", "du", "free", "ps", "tasklist", "ipconfig", "ifconfig",
    "ping", "tracert", "traceroute", "cat", "head", "tail", "wc", "env",
    "printenv", "systeminfo", "id", "git", "docker", "kubectl", "go",
    "node", "npm", "java", "python", "python3", "curl", "wget",
}
_SAFE_GIT = {"status", "log", "diff", "branch", "show", "remote", "ls-files",
             "shortlog", "blame", "tag", "describe"}
_SAFE_DOCKER = {"ps", "images", "logs", "stats", "version", "info", "top",
                "inspect", "diff", "port"}
_SAFE_KUBECTL = {"get", "describe", "logs", "top", "version", "cluster-info"}
# These are in _SAFE_CMD only for their --version/help forms:
_SAFE_FLAG_ONLY = {"node", "npm", "java", "python", "python3", "go"}

_FORBIDDEN_FS = (
    "api_keys.json", "certs", ".ssh", "credentials", "client_secret",
    ".env", "token.json", "long_term.json", "jarvisd.json", "agents.json",
)


def _caps() -> dict:
    screen = False
    try:
        import pyautogui  # noqa: F401
        screen = True
    except Exception:
        screen = False
    return {
        "exec": True,
        "fs": True,
        "opencode": bool(shutil.which("opencode")),
        "screen": screen,
    }


# ── config ───────────────────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def to_ws_url(server: str) -> str:
    s = server.strip().rstrip("/")
    if s.startswith("http://"):
        s = "ws://" + s[7:]
    elif s.startswith("https://"):
        s = "wss://" + s[8:]
    elif not s.startswith(("ws://", "wss://")):
        s = "wss://" + s
    # keep scheme://host[:port] only — path is appended by the caller
    parts = s.split("://", 1)
    return parts[0] + "://" + parts[1].split("/", 1)[0]


def pair(server: str, code: str, name: str) -> str:
    import requests
    url = server.rstrip("/") + "/api/agent/pair"
    r = requests.post(url, json={"code": code, "name": name,
                                 "os": platform.platform()[:80],
                                 "caps": _caps()}, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code != 200 or not data.get("token"):
        raise SystemExit(f"pairing failed ({r.status_code}): "
                         f"{data.get('error', r.text[:120])}")
    return data["token"]


# ── capabilities ─────────────────────────────────────────────────────────────

def _status_payload() -> dict:
    cpu = ram = -1.0
    try:
        from core import sysmetrics
        cpu = round(sysmetrics.cpu_percent(interval=0.2), 1)
        ram = sysmetrics.memory_stats()["percent"]
    except Exception:
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.2)
            ram = psutil.virtual_memory().percent
        except Exception:
            pass
    return {
        "host": socket.gethostname(),
        "os": platform.platform()[:120],
        "py": platform.python_version(),
        "cwd": os.getcwd(),
        "uptime_s": round(time.time() - _BOOT),
        "cpu": cpu,
        "ram": ram,
        "caps": _caps(),
        "jarvisd": VERSION,
    }


def _fs_guard(path: str) -> str | None:
    try:
        resolved = str(Path(path).expanduser().resolve())
    except Exception as e:
        return f"unresolvable path: {e}"
    low = resolved.replace("\\", "/").lower()
    for bad in _FORBIDDEN_FS:
        if bad in low:
            return f"path is off-limits ({bad})"
    return None


async def _exec(cmd: str, timeout: float = 30.0,
                approved: bool = False) -> dict:
    """approved=True: a human pressed CONFIRM on the dashboard (the server
    only sets this flag from core/confirm.py's resolve path). The allowlist
    is a guard for unattended LLM calls — an explicit human approval steps
    past it. Still shell=False: shlex-split argv, never a shell."""
    cmd = (cmd or "").strip()
    if not cmd:
        return {"ok": False, "error": "empty command"}
    if not approved and any(c in cmd for c in _SHELL_METAS):
        return {"ok": False, "needs_approval": True,
                "reason": "shell metacharacters require approval"}
    try:
        parts = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        return {"ok": False, "error": "unparseable command"}
    if os.name == "nt":
        parts = [p.strip('"') for p in parts]
    if not parts:
        return {"ok": False, "error": "empty command"}

    prog = Path(parts[0]).name.lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]
    if not approved:
        if prog not in _SAFE_CMD:
            return {"ok": False, "needs_approval": True,
                    "reason": f"'{prog}' is not on the auto-run allowlist"}

        if prog == "git" and len(parts) > 1 and parts[1] not in _SAFE_GIT:
            return {"ok": False, "needs_approval": True,
                    "reason": f"git {parts[1]} writes — needs approval"}
        if prog == "docker" and len(parts) > 1 and parts[1] not in _SAFE_DOCKER:
            return {"ok": False, "needs_approval": True,
                    "reason": f"docker {parts[1]} mutates containers — needs approval"}
        if prog == "kubectl" and len(parts) > 1 and parts[1] not in _SAFE_KUBECTL:
            return {"ok": False, "needs_approval": True,
                    "reason": f"kubectl {parts[1]} mutates the cluster — needs approval"}
        if prog == "curl" and not any(a in ("-I", "--head") for a in parts[1:]):
            return {"ok": False, "needs_approval": True,
                    "reason": "curl that isn't a HEAD check can send data — needs approval"}
        if prog in _SAFE_FLAG_ONLY and not any(
                a in ("--version", "-v", "--help", "-h", "-version", "help")
                for a in parts[1:]):
            if prog in ("python", "python3", "node", "go"):
                return {"ok": False, "needs_approval": True,
                        "reason": f"{prog} can run arbitrary code — needs approval"}
            return {"ok": False, "needs_approval": True,
                    "reason": f"{prog} beyond --version/--help — needs approval"}

    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=os.getcwd(),
        )
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": f"timed out after {timeout:.0f}s"}
    text = (out or b"").decode(errors="replace")
    clipped = len(text) > 20000
    return {"ok": proc.returncode == 0, "rc": proc.returncode,
            "out": text[-20000:], "truncated": clipped}


async def _fs_list(path: str) -> dict:
    target = path or os.getcwd()
    guard = _fs_guard(target)
    if guard:
        return {"ok": False, "error": guard}
    p = Path(target).expanduser()
    if not p.is_dir():
        return {"ok": False, "error": "not a directory"}
    entries = []
    try:
        for i, child in enumerate(sorted(p.iterdir(), key=lambda c: c.name)):
            if i >= 500:
                entries.append({"name": "…", "dir": False, "more": True})
                break
            try:
                st = child.stat()
                entries.append({"name": child.name, "dir": child.is_dir(),
                                "size": st.st_size, "mtime": round(st.st_mtime)})
            except Exception:
                entries.append({"name": child.name, "dir": False})
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "path": str(p), "entries": entries}


async def _fs_read(path: str, limit: int = 65536) -> dict:
    guard = _fs_guard(path)
    if guard:
        return {"ok": False, "error": guard}
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "error": "not a file"}
    try:
        if p.stat().st_size > 5_000_000:
            return {"ok": False, "error": "file too large (>5MB)"}
        data = p.read_bytes()[: max(1024, min(int(limit), 262144))]
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    if b"\x00" in data[:4096]:
        return {"ok": False, "error": "binary file — refusing"}
    return {"ok": True, "path": str(p),
            "text": data.decode(errors="replace"),
            "size": p.stat().st_size}


async def _run_opencode(payload: dict, send) -> dict:
    which = shutil.which("opencode")
    if not which:
        return {"ok": False, "error": "opencode is not installed on this device"}
    prompt = str(payload.get("prompt") or "")[:8000].strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    cwd = str(payload.get("cwd") or os.getcwd())
    if not Path(cwd).is_dir():
        return {"ok": False, "error": f"cwd does not exist: {cwd}"}
    model = str(payload.get("model") or "").strip()

    argv = [which, "run"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd,
            # opencode keys its project root off PWD — keep it in sync with
            # cwd or it writes into whatever tree jarvisd was started from.
            env={**os.environ, "PWD": cwd},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
    except Exception as e:
        _TASKS.pop(tid, None)
        return {"ok": False, "error": str(e)[:300]}

    rid = payload.get("_rid") or ""
    tail: list[str] = []
    sent = 0

    async def _pump():
        nonlocal sent
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            tail.append(text)
            if len(tail) > 40:
                tail.pop(0)
            if sent < 400:
                sent += 1
                try:
                    await send({"type": "chunk", "id": rid, "text": text})
                except Exception:
                    pass

    pump = asyncio.create_task(_pump())
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=600)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        pump.cancel()
        return {"ok": False, "error": "opencode timed out after 600s"}
    await pump
    return {"ok": rc == 0, "rc": rc,
            "tail": "\n".join(tail)[-4000:],
            "chunks_sent": sent}


# ── Phase 2: task.run / task.push / task.cancel ─────────────────────────────
# task.run executes opencode in an isolated git worktree (marker-guarded so
# task.push can only ever touch trees WE created); task.push is refused
# unless the server sets approved=True — which only happens after a human
# presses CONFIRM on the dashboard.

_TASKS: dict[str, dict] = {}          # task_id → {"proc": ..., "wt": str}
_WT_MARKER = ".jarvis-worktree"


def _git(path: str, *args: str, timeout: float = 120.0) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", path, *args],
                           capture_output=True, text=True,
                           timeout=timeout, shell=False)
        # strip: rev-parse etc. append a newline — used raw as a path below,
        # an unstripped ".git\n" made us write info/exclude where git
        # never looks.
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 127, str(e)[:300]


def _is_git(path: str) -> bool:
    return bool(path) and Path(path, ".git").exists()


async def _run_task(payload: dict, send) -> dict:
    tid = str(payload.get("task_id") or "")[:40]
    prompt = str(payload.get("prompt") or "")[:8000].strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    which = shutil.which("opencode")
    if not which:
        return {"ok": False,
                "error": "opencode is not installed on this device"}
    base = str(payload.get("cwd") or os.getcwd())
    if payload.get("cwd") and not Path(base).is_dir():
        return {"ok": False, "error": f"cwd does not exist: {base}"}
    model = str(payload.get("model") or "").strip()

    run_cwd, wt, branch = base, "", ""
    if _is_git(base):
        tag = tid or f"{int(time.time())}"
        wt = str(Path(base).parent /
                 f".jarvis-wt-{Path(base).name}-{tag}")
        if Path(wt).exists():
            # same task re-run or a crashed leftover: only remove OUR tree
            if Path(wt, _WT_MARKER).exists():
                _git(Path(wt).parent, "worktree", "remove", "--force", wt)
            if Path(wt).exists():
                return {"ok": False,
                        "error": f"worktree exists and is not ours: {wt}"}
        branch = f"jarvis/{tag}"
        rc, out = _git(base, "branch", "-D", branch)
        rc, out = _git(base, "worktree", "add", wt, "-b", branch)
        if rc != 0:
            wt, branch = "", ""     # fall back to in-place, noted below
        else:
            # Keep our marker out of status/diff/commit for good: the shared
            # info/exclude covers this worktree and the main tree.
            try:
                rc2, common = _git(base, "rev-parse", "--git-common-dir")
                if rc2 == 0:
                    cdir = Path(common)
                    if not cdir.is_absolute():
                        cdir = Path(base) / cdir
                    exc = cdir / "info" / "exclude"
                    exc.parent.mkdir(parents=True, exist_ok=True)
                    cur = exc.read_text(encoding="utf-8") if exc.exists() else ""
                    if _WT_MARKER not in cur:
                        with exc.open("a", encoding="utf-8") as f:
                            f.write(f"\n{_WT_MARKER}\n")
            except OSError:
                pass
            try:
                Path(wt, _WT_MARKER).write_text(
                    f"{tid}\n{branch}\n", encoding="utf-8")
            except OSError:
                pass
            run_cwd = wt

    where = f"worktree {wt}" if wt else f"in-place {run_cwd}"
    if tid:
        _TASKS[tid] = {"proc": None, "wt": wt}
    rid = str(payload.get("_rid") or "")
    tail: list[str] = []
    sent = 0
    argv = [which, "run"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=run_cwd,
            # opencode resolves its project root from PWD; setting only cwd
            # leaves a stale PWD pointing at the wrong tree (writes escaped
            # the worktree before this).
            env={**os.environ, "PWD": run_cwd},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    if tid:
        _TASKS[tid]["proc"] = proc

    async def _pump():
        nonlocal sent
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            tail.append(text)
            if len(tail) > 400:
                tail.pop(0)
            if sent < 1500:
                sent += 1
                try:
                    await send({"type": "chunk", "id": rid, "text": text})
                except Exception:
                    pass

    pump = asyncio.create_task(_pump())
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=1800)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        pump.cancel()
        _TASKS.pop(tid, None)
        return {"ok": False, "error": "opencode timed out after 1800s"}
    await pump
    _TASKS.pop(tid, None)
    tail_txt = "\n".join(tail)[-4000:]

    diff, changed, out_branch = "", [], ""
    if rc == 0:
        if wt:
            _git(wt, "add", "-A")
            _, d = _git(wt, "diff", "--cached")
            diff = d[:400_000]
            _, st = _git(wt, "status", "--porcelain")
            changed = [l[3:] for l in st.splitlines()
                       if l.strip() and l[3:] != _WT_MARKER][:200]
            out_branch = branch
        elif _is_git(run_cwd):
            _, d = _git(run_cwd, "diff")
            diff = d[:400_000]
            _, st = _git(run_cwd, "status", "--porcelain")
            changed = [l[3:] for l in st.splitlines()
                       if l.strip() and l[3:] != _WT_MARKER][:200]
            _, b = _git(run_cwd, "rev-parse", "--abbrev-ref", "HEAD")
            out_branch = b.strip()
    res = {"ok": rc == 0, "rc": rc, "tail": tail_txt,
           "diff": diff, "branch": out_branch, "wt": wt,
           "worktree": bool(wt), "changed": changed,
           "chunks_sent": sent, "where": where}
    if rc != 0:
        res["error"] = f"opencode exited rc={rc}"
    return res


async def _task_push(payload: dict) -> dict:
    if not payload.get("approved"):
        return {"ok": False, "needs_approval": True,
                "reason": "pushing to origin requires human approval"}
    wt = str(payload.get("wt") or "")
    branch = str(payload.get("branch") or "")
    if not wt or not branch.startswith("jarvis/"):
        return {"ok": False, "error": "invalid worktree/branch"}
    p = Path(wt)
    if not p.is_dir() or not Path(p, _WT_MARKER).exists():
        # marker guard: task.push may only touch trees task.run created
        return {"ok": False, "error": "not a jarvis worktree — refusing"}
    _git(wt, "add", "-A")
    tid = ""
    try:
        tid = (Path(p, _WT_MARKER).read_text(encoding="utf-8")
               .splitlines() or [""])[0]
    except OSError:
        pass
    _git(wt, "-c", "user.name=JARVIS", "-c", "user.email=jarvis@local",
         "commit", "-m", f"jarvis {tid}: task {tid} (approved push)")
    rc, out = _git(wt, "push", "-u", "origin", branch, timeout=180.0)
    if rc != 0:
        return {"ok": False,
                "error": out.strip()[-400:] or "git push failed"}
    return {"ok": True, "remote": f"origin/{branch}",
            "out": out.strip()[-400:]}


async def _task_cancel(payload: dict) -> dict:
    tid = str(payload.get("task_id") or "")[:40]
    entry = _TASKS.pop(tid, None)
    if not entry or entry.get("proc") is None:
        return {"ok": True, "note": "no running task with that id"}
    try:
        entry["proc"].kill()
    except Exception:
        pass
    return {"ok": True, "note": "killed"}


# ── request dispatch ─────────────────────────────────────────────────────────

async def handle(msg: dict, send) -> dict:
    kind = str(msg.get("type") or "")
    payload = msg.get("payload") or {}
    rid = str(msg.get("id") or "")

    if kind == "ping":
        return {"ok": True, "pong": round(time.time(), 3)}
    if kind == "status":
        return {"ok": True, "status": _status_payload()}
    if kind == "exec":
        res = await _exec(str(payload.get("cmd") or ""),
                          approved=bool(payload.get("approved")))
        res["cmd"] = str(payload.get("cmd") or "")[:200]
        if payload.get("approved"):
            res["approved"] = True
        return res
    if kind == "fs.list":
        return await _fs_list(str(payload.get("path") or ""))
    if kind == "fs.read":
        return await _fs_read(str(payload.get("path") or ""),
                              int(payload.get("limit") or 65536))
    if kind == "run_opencode":
        payload = dict(payload)
        payload["_rid"] = rid
        return await _run_opencode(payload, send)
    if kind == "task.run":
        payload = dict(payload)
        payload["_rid"] = rid
        return await _run_task(payload, send)
    if kind == "task.push":
        return await _task_push(payload)
    if kind == "task.cancel":
        return await _task_cancel(payload)
    return {"ok": False, "error": f"unknown request type: {kind}"}


# ── connection loop ──────────────────────────────────────────────────────────

async def run_loop(server: str, token: str) -> None:
    from websockets.asyncio.client import connect

    url = to_ws_url(server) + "/ws/agent?token=" + token
    backoff = 1.0
    while True:
        t0 = time.time()
        try:
            async with connect(url, max_size=8 * 1024 * 1024,
                               ping_interval=20, ping_timeout=20,
                               open_timeout=20) as ws:
                print(f"[jarvisd] connected to {to_ws_url(server)}", flush=True)
                backoff = 1.0
                send_lock = asyncio.Lock()

                async def send(obj: dict) -> None:
                    async with send_lock:
                        await ws.send(json.dumps(obj))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(msg, dict) or "id" not in msg:
                        continue

                    async def _serve(m=msg, snd=send) -> None:
                        rid = str(m.get("id") or "")
                        try:
                            res = await handle(m, snd)
                        except Exception as e:
                            res = {"ok": False, "error": str(e)[:300]}
                        res["id"] = rid
                        try:
                            await snd(res)
                        except Exception:
                            pass

                    # Long ops (opencode) must not stall the receive loop.
                    asyncio.create_task(_serve())
        except KeyboardInterrupt:
            print("[jarvisd] stopping", flush=True)
            return
        except Exception as e:
            lived = time.time() - t0
            if lived > 60:
                backoff = 1.0
            print(f"[jarvisd] connection lost: {type(e).__name__}: {e} "
                  f"(reconnect in {backoff:.0f}s)", flush=True)
            await asyncio.sleep(backoff + min(backoff, 5) * 0.2)
            backoff = min(backoff * 2, 60)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="JARVIS limb daemon")
    ap.add_argument("--server", help="Space URL, e.g. https://abc1181-jaarvis.hf.space")
    ap.add_argument("--pair", metavar="CODE", help="pair once with a code, save the token")
    ap.add_argument("--name", help="device name shown on the dashboard")
    ap.add_argument("--token", help="device token (overrides saved config)")
    ap.add_argument("--config", default=str(CONFIG_PATH), help="config path")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)

    server = (args.server or os.environ.get("JARVISD_SERVER")
              or cfg.get("server") or "").strip()
    name = (args.name or os.environ.get("JARVISD_NAME")
            or cfg.get("name") or socket.gethostname()).strip()
    token = (args.token or os.environ.get("JARVISD_TOKEN")
             or cfg.get("token") or "").strip()

    if args.pair:
        if not server:
            raise SystemExit("--pair needs --server")
        token = pair(server, args.pair, name)
        cfg.update({"server": server, "name": name, "token": token})
        save_config(cfg_path, cfg)
        print(f"[jarvisd] paired as '{name}' — token saved to {cfg_path}", flush=True)

    if not server or not token:
        raise SystemExit(
            "not configured — run: python jarvisd.py --server <url> --pair <code>")

    cfg.update({"server": server, "name": name, "token": token})
    if not cfg_path.exists():
        save_config(cfg_path, cfg)

    print(f"[jarvisd] {name} ({platform.system()} {platform.release()}) "
          f"→ {to_ws_url(server)}", flush=True)
    print(f"[jarvisd] caps: {json.dumps(_caps())}", flush=True)
    try:
        asyncio.run(run_loop(server, token))
    except KeyboardInterrupt:
        print("[jarvisd] bye", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
