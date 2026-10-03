#!/usr/bin/env python3
"""A real MCP server, in ~60 lines, for testing core/mcp.py offline.

This speaks the actual protocol over stdio — the same newline-delimited
JSON-RPC a real server does — rather than mocking the client. That matters:
a mock proves the client calls the methods it thinks it calls, and a typo in
a method name or a shape mismatch passes straight through a mock. This fails
loudly on a protocol mistake, which is the only thing worth testing.

Usage:  python e2e/fake_mcp_server.py            (normal, two tools)
        FAKE_MCP_BROKEN=1 python ...             (fails the handshake)
        FAKE_MCP_JUNK=1 python ...               (emits non-JSON noise first)
"""
import json
import os
import sys
import time

TOOLS = [
    {
        "name": "search_notes",
        "description": "Search the user's notes and return matching excerpts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "what to look for"},
                "limit": {"type": "integer", "description": "max results"},
            },
            "required": ["query"],
        },
    },
    {
        # deliberately awkward schema: a union type and an enum, both of which
        # the Gemini declaration path has to survive
        "name": "set_reminder",
        "description": "Set a reminder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "when": {"anyOf": [{"type": "string"}, {"type": "integer"}],
                         "description": "ISO stamp or offset in minutes"},
                "repeat": {"type": "string", "enum": ["never", "daily", "weekly"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["when"],
        },
    },
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}],
            "isError": bool(is_error)}


def main():
    if os.environ.get("FAKE_MCP_JUNK"):
        # a real server logs to stdout; the client must skip non-JSON lines
        sys.stdout.write("fake-mcp-server starting up\n")
        sys.stdout.write("not json at all\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            if os.environ.get("FAKE_MCP_BROKEN"):
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32000,
                                "message": "handshake refused by test"}})
                return
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-notes", "version": "1"},
            }})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "search_notes":
                q = args.get("query")
                if not q:
                    send({"jsonrpc": "2.0", "id": mid,
                          "result": text_result("query is required", True)})
                else:
                    send({"jsonrpc": "2.0", "id": mid, "result": text_result(
                        f"2 notes match {q!r}: 'standup notes' and "
                        f"'{q} follow-up'.")})
            elif name == "set_reminder":
                send({"jsonrpc": "2.0", "id": mid, "result": text_result(
                    f"Reminder set for {args.get('when')} "
                    f"(repeat={args.get('repeat', 'never')}).")})
            else:
                send({"jsonrpc": "2.0", "id": mid,
                      "result": text_result(f"no tool {name}", True)})
        elif method and mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601,
                            "message": f"method not found: {method}"}})
        if os.environ.get("FAKE_MCP_SLOW"):
            time.sleep(5)


if __name__ == "__main__":
    main()
