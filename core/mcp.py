"""Model Context Protocol — the "connect it to everything" answer.

The alternative to this file was writing forty connector modules by hand. MCP
already solves that problem: an MCP server publishes a list of tools with JSON
Schemas, and any client can call them. So JARVIS speaks MCP, and every MCP
server in the world becomes available to the assistant without a line of
integration code from us.

WHY THIS IS NOT BUILT ON THE `mcp` PYTHON PACKAGE
    The wire protocol is newline-delimited JSON-RPC 2.0 over stdio, and it is
    about two hundred lines. Depending on the SDK instead would mean the single
    most valuable connector feature in the project dies whenever that package
    fails to install or drifts a version. More importantly, it would be
    untestable offline: a dependency you cannot import is a feature you cannot
    regression-test, and this project's whole value is that it can be.

THE TRUST MODEL, WHICH IS THE ACTUAL DESIGN
    An MCP server is arbitrary third-party code that the user chose to install.
    It can do anything the process can do. So MCP tools do NOT inherit the
    "reading is free" rule that the built-in connectors get — every call asks
    for approval, unless the user has explicitly marked that particular server
    trusted in the dashboard. Fail-closed is the only defensible default when
    the code behind a tool is not ours.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Any, Optional

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 90
HANDSHAKE_TIMEOUT = 25

# Tools that are pure reads still ask, because the *server* is untrusted — but
# the reason is surfaced so the approval card is honest about why.
UNTRUSTED_REASON = ("this comes from an MCP server, which is third-party code "
                    "the user installed — not JARVIS's own logic")


class MCPError(RuntimeError):
    def __init__(self, message: str, *, server: str = ""):
        super().__init__(message)
        self.message = message
        self.server = server


# ── config ───────────────────────────────────────────────────────────────────

def _path():
    from core.data_paths import config_dir
    return config_dir() / "mcp_servers.json"


def servers() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    return [s for s in data if isinstance(s, dict) and s.get("name")]


def save_servers(rows: list[dict]) -> list[dict]:
    clean = []
    for s in rows:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        cmd = str(s.get("command") or "").strip()
        url = str(s.get("url") or "").strip()
        if not cmd and not url:
            continue
        clean.append({
            "name": name,
            "command": cmd,
            "args": [str(a) for a in (s.get("args") or [])],
            "env": {str(k): str(v) for k, v in (s.get("env") or {}).items()},
            "url": url,
            # trusted means "I have read this server's code and I do not want to
            # be asked about it every time". It is opt-in, per server, and the
            # default is False — see the module docstring.
            "trusted": bool(s.get("trusted")),
            "timeout": int(s.get("timeout") or DEFAULT_TIMEOUT),
        })
    from core.data_paths import config_dir
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return clean


def add_server(name: str, command: str = "", args: Optional[list] = None,
               env: Optional[dict] = None, url: str = "",
               trusted: bool = False) -> dict:
    rows = [s for s in servers() if s["name"] != str(name).strip()]
    rows.append({"name": name, "command": command, "args": args or [],
                 "env": env or {}, "url": url, "trusted": bool(trusted)})
    return next(s for s in save_servers(rows) if s["name"] == str(name).strip())


def remove_server(name: str) -> bool:
    rows = servers()
    keep = [s for s in rows if s["name"] != name]
    if len(keep) == len(rows):
        return False
    save_servers(keep)
    return True


def get_server(name: str) -> Optional[dict]:
    return next((s for s in servers() if s["name"] == name), None)


def reset() -> None:
    """Test/deploy hook — drop every server and stop every child process.

    Without this, a test that adds a server leaves it configured for the next
    one, and the failure surfaces three tests later as an unrelated count
    mismatch. Matches the `reset()` hook the other core modules expose.
    """
    POOL.shutdown()
    for p in (_path(), _cache_path()):
        try:
            p.unlink()
        except Exception:
            pass


# ── the stdio client ─────────────────────────────────────────────────────────

class StdioClient:
    """One running MCP server. Not thread-safe on its own; `pool` serialises."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.name = str(spec.get("name") or "")
        self.proc: Optional[subprocess.Popen] = None
        self._id = 0
        self._lock = threading.Lock()
        self._tools: list[dict] = []
        self.error = ""

    # ── lifecycle ──
    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        if self.spec.get("url"):
            raise MCPError(
                "HTTP/SSE MCP servers need a transport JARVIS does not speak "
                "yet — this client is stdio. Point the server at a stdio "
                "command, or run it behind a stdio bridge.", server=self.name)
        cmd = str(self.spec.get("command") or "").strip()
        if not cmd:
            raise MCPError("no command configured", server=self.name)
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self.proc = subprocess.Popen(
                [cmd] + [str(a) for a in (self.spec.get("args") or [])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, text=True, bufsize=1,
                cwd=os.getcwd())
        except FileNotFoundError:
            raise MCPError(f"'{cmd}' is not on PATH in this environment",
                           server=self.name) from None
        except Exception as e:
            raise MCPError(f"could not start: {type(e).__name__}: {e}",
                           server=self.name) from None
        self._handshake()

    def _handshake(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "jarvis", "version": "1"},
        }, timeout=HANDSHAKE_TIMEOUT)
        self._notify("notifications/initialized", {})
        self.refresh_tools()

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stderr_tail(self, n: int = 400) -> str:
        if not self.proc or not self.proc.stderr:
            return ""
        try:
            os.set_blocking(self.proc.stderr.fileno(), False)
            return (self.proc.stderr.read() or "")[-n:]
        except Exception:
            return ""

    # ── protocol ──
    def _write(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPError("server is not running", server=self.name)
        try:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.proc = None
            raise MCPError(f"server closed the pipe: {type(e).__name__}",
                           server=self.name) from None

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _rpc(self, method: str, params: dict, *,
             timeout: Optional[int] = None) -> Any:
        with self._lock:
            self._id += 1
            want = self._id
            self._write({"jsonrpc": "2.0", "id": want,
                         "method": method, "params": params})
            if not self.proc or not self.proc.stdout:
                raise MCPError("server is not running", server=self.name)
            # A server that dies mid-call must not hang the voice loop
            # forever, so the read is bounded and the exit is checked.
            deadline = time.time() + int(timeout or self.spec.get("timeout")
                                         or DEFAULT_TIMEOUT)
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    self.proc = None
                    raise MCPError(
                        f"server exited with code {self.proc and '?'} — "
                        f"{self.stderr_tail(200) or 'no output'}",
                        server=self.name)
                line = self.proc.stdout.readline()
                if not line:
                    time.sleep(0.02)
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue          # servers log non-JSON to stdout; skip it
                if msg.get("id") != want:
                    continue          # a notification or a stale reply
                if "error" in msg:
                    err = msg["error"] or {}
                    raise MCPError(
                        f"{err.get('code', '?')}: "
                        f"{err.get('message', 'unknown error')}"[:200],
                        server=self.name)
                return msg.get("result")

    def refresh_tools(self) -> list[dict]:
        res = self._rpc("tools/list", {}) or {}
        self._tools = [t for t in (res.get("tools") or [])
                       if isinstance(t, dict) and t.get("name")]
        return self._tools

    def tools(self) -> list[dict]:
        return list(self._tools)

    def call(self, tool: str, args: Optional[dict] = None,
             timeout: Optional[int] = None) -> str:
        res = self._rpc("tools/call",
                        {"name": tool, "arguments": args or {}},
                        timeout=timeout)
        return _flatten(res)


def _flatten(res: Any) -> str:
    """MCP returns content blocks; the model wants prose.

    A block that is not text is described rather than dropped — silently
    returning "" for an image result would have the assistant claim a tool
    did nothing when it actually returned something.
    """
    if res is None:
        return "(no result)"
    if isinstance(res, str):
        return res
    if isinstance(res, dict) and res.get("isError"):
        blocks = res.get("content") or []
        txt = " ".join(b.get("text", "") for b in blocks
                       if isinstance(b, dict) and b.get("type") == "text")
        return f"Error from the tool: {txt.strip() or 'no detail'}"
    blocks = (res.get("content") if isinstance(res, dict) else None) or []
    if not blocks:
        return json.dumps(res, ensure_ascii=False)[:2000] if isinstance(res, dict) else str(res)[:2000]
    out: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            out.append(str(b))
            continue
        t = b.get("type")
        if t == "text":
            out.append(str(b.get("text") or ""))
        elif t == "image":
            out.append(f"[image returned, {len(b.get('data',''))} base64 chars]")
        elif t == "audio":
            out.append("[audio returned]")
        elif t == "resource":
            res_ = b.get("resource") or {}
            out.append(str(res_.get("text") or res_.get("uri") or "[resource]"))
        else:
            out.append(f"[{t} returned]")
    return "\n".join(p for p in out if p).strip() or "(empty result)"


# ── the pool ─────────────────────────────────────────────────────────────────

class _Pool:
    """Keeps one live client per server, started on first use.

    Servers are spawned lazily rather than at boot: a user with eight MCP
    servers configured should not pay eight process spawns on every restart,
    and a broken sixth server should not stop the other seven from working.
    """

    def __init__(self):
        self._clients: dict[str, StdioClient] = {}
        self._lock = threading.Lock()
        self._health: dict[str, dict] = {}

    def get(self, name: str) -> StdioClient:
        spec = get_server(name)
        if not spec:
            raise MCPError(f"no MCP server named '{name}'", server=name)
        with self._lock:
            c = self._clients.get(name)
            if c and c.alive():
                return c
            if c:
                c.stop()
            c = StdioClient(spec)
            try:
                c.start()
            except MCPError as e:
                self._health[name] = {"ok": False, "error": e.message}
                raise
            self._clients[name] = c
            self._health[name] = {
                "ok": True, "tools": len(c.tools()),
                "error": "", "at": time.time()}
            return c

    def invalidate(self, name: str) -> None:
        with self._lock:
            c = self._clients.pop(name, None)
            if c:
                c.stop()
        # a server we just stopped is a server whose tool list is now unknown;
        # keeping the cache would advertise tools that no longer answer
        data = _read_cache()
        if data.pop(name, None) is not None:
            _write_cache(data)

    def health(self) -> dict:
        out = {}
        for spec in servers():
            name = spec["name"]
            c = self._clients.get(name)
            h = dict(self._health.get(name) or {})
            h["running"] = bool(c and c.alive())
            h["tool_count"] = len(c.tools()) if (c and c.alive()) else h.get("tools", 0)
            h["trusted"] = bool(spec.get("trusted"))
            h["command"] = spec.get("command") or spec.get("url") or ""
            out[name] = h
        return out

    def all_tools(self) -> list[tuple[str, dict]]:
        """(server_name, tool) for every tool every server offers.

        One server failing is logged into health and skipped — a broken
        connector must never remove the tools of a working one.
        """
        out = []
        for spec in servers():
            name = spec["name"]
            try:
                for t in self.get(name).tools():
                    out.append((name, t))
            except MCPError as e:
                self._health[name] = {"ok": False, "error": e.message,
                                      "at": time.time()}
        return out

    def shutdown(self) -> None:
        with self._lock:
            for c in self._clients.values():
                try:
                    c.stop()
                except Exception:
                    pass
            self._clients.clear()


POOL = _Pool()


# ── Gemini tool declarations ─────────────────────────────────────────────────

def _sanitize(schema: Any, depth: int = 0) -> Any:
    """JSON Schema → the subset Gemini accepts.

    MCP servers are free to use anyOf/oneOf/$ref/additionalProperties, and the
    API rejects the whole declaration if it sees one. Rather than drop the
    schema, an unrecognised construct collapses to its most informative
    string form — the model still learns what the argument means.
    """
    if depth > 6 or not isinstance(schema, dict):
        return {"type": "string"}
    out: dict = {}
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        out["type"] = "OBJECT"
        props: dict = {}
        for k, v in (schema.get("properties") or {}).items():
            props[str(k)] = _sanitize(v, depth + 1)
        out["properties"] = props
        req = [str(r) for r in (schema.get("required") or []) if str(r) in props]
        if req:
            out["required"] = req
        desc = str(schema.get("description") or "").strip()
        if desc:
            out["description"] = desc[:400]
        return out
    if t == "array":
        return {"type": "ARRAY",
                "items": _sanitize(schema.get("items") or {}, depth + 1)}
    if t in ("integer",):
        return {"type": "INTEGER"}
    if t in ("number",):
        return {"type": "NUMBER"}
    if t in ("boolean",):
        return {"type": "BOOLEAN"}
    if t == "string" or t is None:
        # `string` had no branch of its own, so every string in every MCP
        # schema fell through to the last resort below and came back
        # lowercased — which the API silently mishandles. Union types also land
        # here, because an anyOf resolves to its first branch.
        out = {"type": "STRING"}
        enum = schema.get("enum")
        if isinstance(enum, list) and enum and all(
                isinstance(e, (str, int, float, bool)) for e in enum):
            out["description"] = "one of: " + ", ".join(str(e) for e in enum[:24])
        elif schema.get("description"):
            out["description"] = str(schema["description"])[:400]
        if t is None:
            for key in ("anyOf", "oneOf", "allOf"):
                branch = schema.get(key)
                if isinstance(branch, list) and branch:
                    resolved = _sanitize(branch[0], depth + 1)
                    if "description" not in resolved and "description" in out:
                        resolved["description"] = out["description"]
                    return resolved
        return out
    if t == "null":
        return {"type": "STRING"}
    # last resort: never reject the declaration over an unknown type
    return {"type": "STRING"}


def declaration(server: str, tool: dict) -> dict:
    """One MCP tool as a Gemini FunctionDeclaration.

    The name is namespaced `mcp__<server>__<tool>` because two servers may
    both publish a tool called `search`, and a flat namespace would silently
    drop one of them.
    """
    raw = str(tool.get("name") or "tool")
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in raw)
    return {
        "name": f"mcp__{server}__{safe}"[:64],
        "description": (f"[MCP: {server}] "
                        + str(tool.get("description") or raw)[:900]),
        "parameters": _sanitize(tool.get("inputSchema")
                                or {"type": "object", "properties": {}}),
    }


# ── the tool cache ───────────────────────────────────────────────────────────
#
# WHY THIS EXISTS
#     Building the tool list happens on the assistant's startup path. If that
#     path starts every MCP server and waits for a handshake, then one slow
#     server adds its connect timeout to every boot — three hung servers meant
#     75 seconds of silence before JARVIS could hear a word. A connector must
#     never be able to delay the assistant existing.
#
#     So: the tool list is cached to disk, `declarations()` reads the cache and
#     never blocks, and a refresh happens on a background thread. The tools are
#     therefore one connect behind on a cold start, which is a far better
#     failure than a voice assistant that will not start.


def _cache_path():
    from core.data_paths import config_dir
    return config_dir() / "mcp_tools.json"


def _read_cache() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def cached_tools() -> dict:
    """{server: [tool, ...]} as last seen. Instant, no process spawning."""
    rows = servers()
    if not rows:
        return {}
    data = _read_cache()
    # only servers that still exist, and only ones we have actually seen
    return {name: data[name] for name in data
            if name in {s["name"] for s in rows} and isinstance(data[name], list)}


def declarations() -> list[dict]:
    """Tool declarations from the cache. Never blocks, never spawns."""
    out = []
    for server, tools in cached_tools().items():
        for t in tools:
            try:
                out.append(declaration(server, t))
            except Exception:
                continue
    return out


def refresh(wait: float = 0.0) -> dict:
    """Connect to every server and rewrite the cache. Explicit, and slow.

    Called by the dashboard and by the `mcp` tool, where a human is waiting and
    a few seconds of honest latency is fine.
    """
    got: dict = {}
    health = POOL.health()
    for spec in servers():
        name = spec["name"]
        try:
            got[name] = POOL.get(name).tools()
        except MCPError as e:
            health[name] = {"ok": False, "error": e.message, "at": time.time()}
    merged = {k: v for k, v in _read_cache().items() if k in got or k not in got}
    merged.update(got)
    _write_cache(merged)
    return health


def refresh_async() -> None:
    """Kick a refresh off and return immediately."""
    th = threading.Thread(target=_refresh_quiet, daemon=True)
    th.start()


def _refresh_quiet() -> None:
    try:
        refresh()
    except Exception:
        pass


def route(prefixed_name: str, args: dict) -> str:
    """Call an MCP tool from its prefixed name. Returns prose for the model."""
    parts = str(prefixed_name or "").split("__")
    if len(parts) < 3 or parts[0] != "mcp":
        raise MCPError(f"'{prefixed_name}' is not an MCP tool")
    server, raw = parts[1], "__".join(parts[2:])
    # recover the original name: the declaration replaced non-alphanumerics
    spec = get_server(server)
    if not spec:
        raise MCPError(f"no MCP server named '{server}'", server=server)
    actual = next((t.get("name") for t in POOL.get(server).tools()
                   if "".join(ch if (ch.isalnum() or ch == "_") else "_"
                              for ch in str(t.get("name"))) == raw), None)
    if not actual:
        raise MCPError(f"'{raw}' is no longer offered by {server}", server=server)
    return POOL.get(server).call(actual, args or {})


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, name: str = "", command: str = "",
         args: str = "", env: str = "", url: str = "", trusted: bool = False,
         call: str = "", arguments: str = "") -> str:
    """Let the assistant manage the connector fleet by voice.

    `list` is the common one and is deliberately informative: the user asked
    what JARVIS is connected to, and the answer should include the servers that
    are configured but DOWN, because a silently missing connector is the exact
    failure that makes an assistant feel less capable than it is.
    """
    a = str(action or "").strip().lower()
    try:
        if a in ("", "list", "status", "servers"):
            rows = servers()
            if not rows:
                return ("No MCP servers configured. One is all it takes to add "
                        "any tool ecosystem — say 'add an MCP server'.")
            h = POOL.health()
            out = [f"{len(rows)} MCP server(s):"]
            for s in rows:
                st = h.get(s["name"], {})
                mark = "up" if st.get("running") else "DOWN"
                n = st.get("tool_count") or 0
                trust = "trusted" if s.get("trusted") else "asks first"
                line = f"- {s['name']}: {mark}, {n} tool(s), {trust}"
                if not st.get("running") and st.get("error"):
                    line += f" — {str(st['error'])[:90]}"
                out.append(line)
            return "\n".join(out)

        if a in ("add", "install", "connect"):
            if not command and not url:
                return ("Give me the command to run. For example: add an MCP "
                        "server for Notion, command npx, arguments "
                        "-y @notionhq/mcp-server")
            argv = [x for x in str(args or "").split() if x]
            envd = {}
            for pair in str(env or "").split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    envd[k.strip()] = v.strip()
            if not name:
                name = (str(command).split("/")[-1].split(" ")[0]
                        or f"mcp{len(servers()) + 1}")[:40]
            s = add_server(name, command, argv, envd, url, trusted)
            # prove it works now, rather than reporting success for a server
            # that cannot start
            try:
                c = POOL.get(name)
                n = len(c.tools())
                return (f"{name} is connected — {n} tool(s) available, now "
                        f"named mcp__{name}__*.")
            except MCPError as e:
                return (f"{name} is saved but did not start: {e.message} "
                        f"Fix the command and I will pick it up.")

        if a in ("remove", "delete", "uninstall"):
            ok = remove_server(name)
            if ok:
                POOL.invalidate(name)
                return f"{name} removed. Its tools are gone from my list."
            return f"No MCP server named '{name}'."

        if a in ("trust", "untrust"):
            rows = servers()
            if not any(x["name"] == name for x in rows):
                return f"No MCP server named '{name}'."
            for x in rows:
                if x["name"] == name:
                    x["trusted"] = bool(trusted)
            save_servers(rows)
            return (f"{name} is now trusted — I will not ask before calling it."
                    if trusted else
                    f"{name} is untrusted — I will ask before every call.")

        if a in ("call", "run", "use"):
            if not call:
                return "Which tool? Give me the mcp__server__tool name."
            try:
                argv = json.loads(arguments) if arguments else {}
            except Exception:
                argv = {}
            return route(call if call.startswith("mcp__")
                         else f"mcp__{name}__{call}", argv)

        return "Unknown action. Use list / add / remove / trust / call."
    except MCPError as e:
        return f"MCP: {e.message}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:180]
