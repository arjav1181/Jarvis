"""Tests for the MCP client — the "connect it to everything" path.

These run against `e2e/fake_mcp_server.py`, which speaks the real protocol
over stdio. A mocked client would pass even with a wrong method name or a
mismatched result shape, which is precisely the class of bug this feature
cannot afford: a connector that silently returns nothing is worse than no
connector, because the assistant will report a capability it does not have.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="jarvis-test-mcp-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0
SERVER = str(ROOT / "e2e" / "fake_mcp_server.py")


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def test_config():
    from core import mcp as M
    M.reset()
    check("mcp.empty", M.servers() == [], str(M.servers()))
    M.add_server("notes", command=sys.executable, args=[SERVER])
    check("mcp.added", [s["name"] for s in M.servers()] == ["notes"])
    check("mcp.untrusted_by_default", M.get_server("notes")["trusted"] is False)
    # a server with neither command nor url is not a server
    M.save_servers([{"name": "empty", "command": "", "args": []}])
    check("mcp.rejects_commandless", [s["name"] for s in M.servers()] == [],
          str(M.servers()))
    M.add_server("notes", command=sys.executable, args=[SERVER])
    check("mcp.remove", M.remove_server("notes") is True)
    check("mcp.remove_twice", M.remove_server("notes") is False)


def test_handshake_and_tools():
    from core import mcp as M
    M.reset()
    M.add_server("notes", command=sys.executable, args=[SERVER])
    c = M.POOL.get("notes")
    names = [t["name"] for t in c.tools()]
    check("mcp.tools_listed", names == ["search_notes", "set_reminder"], str(names))
    out = c.call("search_notes", {"query": "budget", "limit": 2})
    check("mcp.call_returns_text", "budget" in out, out)
    check("mcp.call_is_str", isinstance(out, str))


def test_error_paths():
    from core import mcp as M
    M.reset()
    M.add_server("notes", command=sys.executable, args=[SERVER])
    c = M.POOL.get("notes")
    # a tool-level error must read as an error, not as an empty success
    out = c.call("search_notes", {})
    check("mcp.tool_error_marked", "Error" in out, out)
    # a missing binary must name the binary
    M.add_server("ghost", command="definitely-not-a-real-binary-xyz")
    try:
        M.POOL.get("ghost")
        check("mcp.missing_binary_raises", False, "no exception")
    except M.MCPError as e:
        check("mcp.missing_binary_raises", True)
        check("mcp.missing_binary_message", "PATH" in e.message or "not on" in e.message,
              e.message)
    # a server that refuses the handshake must not be marked healthy
    M.add_server("broken", command=sys.executable, args=[SERVER],
                 env={"FAKE_MCP_BROKEN": "1"})
    try:
        M.POOL.get("broken")
        check("mcp.broken_raises", False, "no exception")
    except M.MCPError as e:
        check("mcp.broken_raises", True)
        check("mcp.broken_reports", "handshake refused" in e.message, e.message)
    h = M.POOL.health()
    check("mcp.health_reports_down", h["broken"].get("running") is False, str(h["broken"]))
    check("mcp.health_has_error", bool(h["broken"].get("error")), str(h["broken"]))


def test_noise_tolerance():
    """A real server logs to stdout. The client must skip non-JSON lines."""
    from core import mcp as M
    M.reset()
    M.add_server("noisy", command=sys.executable, args=[SERVER],
                 env={"FAKE_MCP_JUNK": "1"})
    c = M.POOL.get("noisy")
    check("mcp.survives_stdout_noise", len(c.tools()) == 2, str(len(c.tools())))
    check("mcp.call_after_noise", "notes match" in c.call("search_notes", {"query": "x"}))


def test_isolation():
    """One broken server must not remove a working one's tools."""
    from core import mcp as M
    M.reset()
    M.add_server("good", command=sys.executable, args=[SERVER])
    M.add_server("bad", command=sys.executable, args=[SERVER],
                 env={"FAKE_MCP_BROKEN": "1"})
    M.refresh()
    decls = M.declarations()
    check("mcp.good_tools_present", any(d["name"].startswith("mcp__good__")
                                        for d in decls), str([d["name"] for d in decls]))
    check("mcp.bad_tools_absent", not any(d["name"].startswith("mcp__bad__")
                                          for d in decls))
    M.reset()


def test_declarations():
    """The schema must survive translation to what the API accepts."""
    from core import mcp as M
    from google.genai import types
    M.reset()
    M.add_server("notes", command=sys.executable, args=[SERVER])
    M.refresh()
    decls = M.declarations()
    check("mcp.two_declarations", len(decls) == 2, str(len(decls)))
    by = {d["name"]: d for d in decls}
    search = by.get("mcp__notes__search_notes")
    check("mcp.namespaced_name", search is not None, str(list(by)))
    p = search["parameters"]
    check("mcp.object_root", p.get("type") == "OBJECT", str(p))
    check("mcp.required_preserved", p.get("required") == ["query"], str(p))
    check("mcp.string_is_uppercase", p["properties"]["query"]["type"] == "STRING",
          str(p["properties"]["query"]))
    check("mcp.integer_is_uppercase",
          p["properties"]["limit"]["type"] == "INTEGER", str(p["properties"]["limit"]))
    rem = by["mcp__notes__set_reminder"]["parameters"]
    check("mcp.union_resolves", rem["properties"]["when"]["type"] == "STRING",
          str(rem["properties"]["when"]))
    check("mcp.union_keeps_description",
          "ISO" in (rem["properties"]["when"].get("description") or ""),
          str(rem["properties"]["when"]))
    check("mcp.enum_becomes_hint",
          "one of" in (rem["properties"]["repeat"].get("description") or ""),
          str(rem["properties"]["repeat"]))
    check("mcp.array_items_uppercase",
          rem["properties"]["tags"]["items"]["type"] == "STRING",
          str(rem["properties"]["tags"]))
    try:
        for d in decls:
            types.FunctionDeclaration(name=d["name"], description=d["description"],
                                      parameters=d["parameters"])
        check("mcp.sdk_accepts", True)
    except Exception as e:
        check("mcp.sdk_accepts", False, str(e)[:150])


def test_routing():
    from core import mcp as M
    M.reset()
    M.add_server("notes", command=sys.executable, args=[SERVER])
    out = M.route("mcp__notes__search_notes", {"query": "x"})
    check("mcp.route_works", "notes match" in out, out)
    for bad, why in (("not_mcp", "bad prefix"),
                     ("mcp__notes__does_not_exist", "unknown tool"),
                     ("mcp__nosuch__tool", "unknown server")):
        try:
            M.route(bad, {})
            check(f"mcp.route_rejects_{why}", False, "no exception")
        except M.MCPError:
            check(f"mcp.route_rejects_{why}", True)


def test_declarations_never_block():
    """A connector must never be able to delay the assistant's startup.

    This is the regression test for the bug where building the tool list
    started every server synchronously, so three hung servers added 75 seconds
    of silence before JARVIS could hear anything.
    """
    from core import mcp as M
    M.reset()
    for i in range(3):
        M.add_server(f"slow{i}", command=sys.executable, args=[SERVER],
                     env={"FAKE_MCP_SLOW": "1"})
    t0 = time.time()
    decls = M.declarations()
    elapsed = time.time() - t0
    check("mcp.declarations_instant", elapsed < 0.5, f"{elapsed:.2f}s")
    check("mcp.declarations_cold_is_empty", decls == [], str(len(decls)))
    t0 = time.time()
    M.declarations()
    check("mcp.declarations_instant_again", time.time() - t0 < 0.5)


def test_cache_purged_on_remove():
    """A removed server must stop advertising tools that no longer answer."""
    from core import mcp as M
    M.reset()
    M.add_server("notes", command=sys.executable, args=[SERVER])
    M.refresh()
    check("mcp.cached_present", len(M.declarations()) == 2, str(len(M.declarations())))
    M.remove_server("notes")
    check("mcp.cache_purged", M.declarations() == [], str(M.declarations()))
    # and the in-flight client is stopped too
    M.reset()


def test_restart():
    """A server that dies must come back on next use, not stay broken."""
    from core import mcp as M
    M.reset()
    M.add_server("notes", command=sys.executable, args=[SERVER])
    c = M.POOL.get("notes")
    c.proc.kill()
    c.proc.wait(timeout=5)
    check("mcp.dead_is_not_alive", c.alive() is False)
    t0 = time.time()
    fresh = M.POOL.get("notes")
    check("mcp.auto_restarts", fresh.alive() and len(fresh.tools()) == 2,
          f"{time.time() - t0:.1f}s")
    M.reset()


def test_trust():
    """Untrusted asks; trusted does not. This is the whole safety story."""
    from core import mcp as M, policy as P
    M.reset()
    M.add_server("notes", command=sys.executable, args=[SERVER], trusted=False)
    check("mcp.untrusted_asks",
          P.check("mcp__notes__search_notes", {}).needs_approval is True)
    M.add_server("notes", command=sys.executable, args=[SERVER], trusted=True)
    check("mcp.trusted_does_not_ask",
          P.check("mcp__notes__search_notes", {}).needs_approval is False)
    # a server that does not exist at all still fails closed
    check("mcp.unknown_server_fails_closed",
          P.check("mcp__ghost__anything", {}).needs_approval is True)
    # the built-in connectors keep their own rules — MCP must not have leaked
    check("mcp.builtin_email_still_free",
          P.check("email", {"action": "inbox"}).needs_approval is False)
    check("mcp.builtin_email_send_still_asks",
          P.check("email", {"action": "send"}).needs_approval is True)


def test_model_tool():
    from core import mcp as M
    M.reset()
    out = M.tool("list")
    check("mcp.tool_empty_state", "No MCP servers" in out, out)
    out = M.tool("add", name="notes", command=sys.executable, args=SERVER)
    check("mcp.tool_add_reports_tools", "2 tool(s)" in out, out)
    out = M.tool("list")
    check("mcp.tool_list_reports_up", "up" in out, out)
    M.add_server("ghost", command="definitely-not-real-xyz")
    out = M.tool("list")
    check("mcp.tool_list_reports_down", "DOWN" in out, out)
    out = M.tool("call", name="notes", call="search_notes",
                 arguments='{"query":"q"}')
    check("mcp.tool_call", "notes match" in out, out)
    M.reset()


if __name__ == "__main__":
    for fn in (test_config, test_handshake_and_tools, test_error_paths,
               test_noise_tolerance, test_isolation, test_declarations,
               test_declarations_never_block, test_cache_purged_on_remove,
               test_routing, test_restart, test_trust, test_model_tool):
        try:
            fn()
        except Exception as e:
            import traceback
            check(fn.__name__, False, f"raised {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\nPASS {COUNT - len(FAILS)}  FAIL {len(FAILS)}")
    for f in FAILS:
        print(f"  FAIL  {f}")
    sys.exit(1 if FAILS else 0)
