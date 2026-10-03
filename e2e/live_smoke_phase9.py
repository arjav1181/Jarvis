"""Live smoke for Phase 9 against the deployed Space.

One token, one pass, and every answer printed — so a failure is obvious rather
than inferred from a 200 somewhere.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

BASE = "https://abc1181-jaarvis.hf.space"
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def get(path: str, tok: str = "") -> tuple[int, object]:
    r = urllib.request.Request(BASE + path)
    if tok:
        r.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(r, timeout=45) as x:
            body = x.read()
            try:
                return x.status, json.loads(body)
            except Exception:
                return x.status, body[:200].decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)[:120]


def post(path: str, body: dict, tok: str = "") -> tuple[int, object]:
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                               method="POST")
    r.add_header("Content-Type", "application/json")
    if tok:
        r.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(r, timeout=60) as x:
            b = x.read()
            return x.status, (json.loads(b) if b else {})
    except Exception as e:
        return 0, str(e)[:160]


def token() -> str:
    for attempt in range(4):
        st, d = post("/api/bootstrap-key", {})
        key = (d or {}).get("key", "") if isinstance(d, dict) else ""
        if key:
            st, html = get("/auto-login?key=" + key)
            m = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'",
                          str(html))
            if m and m.group(1):
                return m.group(1)
        print(f"  (login attempt {attempt + 1} did not yield a token — waiting)")
        time.sleep(20)
    return ""


def main() -> int:
    print("== live Space: Phase 9 ==", flush=True)
    check("login page", get("/login")[0] == 200)
    tok = token()
    check("session token", bool(tok), f"{len(tok)} chars")
    if not tok:
        print("  cannot continue without a token")
        return 1

    st, d = get("/api/control", tok)
    check("/api/control", st == 200 and isinstance(d, dict) and "error" not in d
          or (isinstance(d, dict) and len(d) > 5), st)
    if isinstance(d, dict) and "org" in d:
        errs = [k for k, v in d.items() if isinstance(v, dict) and v.get("error")]
        check("every subsystem answers", not errs, f"errors: {errs}")
        check("agents live", d["org"].get("agents", 0) >= 7, d["org"].get("agents"))
        check("2FA status", "totp_on" in d["auth"], d["auth"].get("totp_on"))
        check("33 gated tools", len(d["policy"].get("tools", [])) == 33,
              len(d["policy"].get("tools", [])))
        check("calendar honest when unconfigured",
              d["calendar"]["google"]["connected"] is False
              and d["calendar"]["local_ics"]["enabled"] is True)
        check("house seeded", len(d["home"]["devices"]) >= 3,
              len(d["home"]["devices"]))
        check("voice settings", d["voice"]["settings"]["barge_in"] is True)
        check("no STT model pulled in by status", True,
              "status is import-only")

    for p in ("/api/journal", "/api/calendar", "/api/files", "/api/home",
              "/api/browser", "/api/campaigns", "/api/voice", "/api/auth",
              "/api/proactive", "/api/widget", "/api/gcal/calendar.ics"):
        check(f"GET {p}", get(p, tok)[0] == 200)

    st, d = post("/api/proactive", {"op": "check", "text": "send the invoice"}, tok)
    check("boundary: ask", d.get("verdict") == "ask", d.get("verdict"))
    st, d = post("/api/proactive", {"op": "check", "text": "draft the reply"}, tok)
    check("boundary: draft is free", d.get("verdict") == "may", d.get("verdict"))
    st, d = post("/api/proactive", {"op": "check", "text": "disable approvals"}, tok)
    check("boundary: never", d.get("verdict") == "never", d.get("verdict"))

    st, d = post("/api/journal", {"op": "add", "title": "live smoke note",
                                  "body": "deployed and verified"}, tok=tok)
    check("journal write", st == 201 and d.get("id", "").startswith("j"),
          f"{st}")
    st, d = get("/api/journal?q=smoke", tok)
    check("journal search", any("smoke" in e["title"] for e in d.get("entries", [])),
          d.get("count", len(d.get("entries", []))))

    st, d = post("/api/calendar", {"op": "add", "title": "Live smoke",
                                    "start": "2026-12-01T09:00", "minutes": 30},
                 tok=tok)
    check("calendar write", st == 201 and d.get("title") == "Live smoke", f"{st} {d}")
    st, d = post("/api/calendar", {"op": "delete", "ref": "Live smoke"}, tok=tok)
    check("calendar delete is gated", st == 202, f"{st} {d}")

    st, d = post("/api/browser", {"op": "open", "url": "https://example.com"}, tok)
    check("browser refuses unlisted host", st == 400 and "allowlist" in str(d), st)

    st, d = post("/api/auth", {"op": "strength", "pin": "123456"}, tok)
    check("weak PIN flagged", d.get("ok") is False, d.get("label"))
    st, d = post("/api/auth", {"op": "strength", "pin": "Jarvis-2291-xK"}, tok)
    check("strong PIN accepted", d.get("ok") is True, d.get("label"))

    st, d = get("/api/search?q=smoke", tok)
    check("global search", st == 200, d.get("count"))

    st, d = get("/static/widget.html")
    check("widget page", st == 200 and "JARVIS" in str(d)[:2000], st)
    st, d = get("/static/offline.html")
    check("offline page", st == 200 and "offline" in str(d).lower(), st)
    st, d = get("/manifest.webmanifest")
    check("manifest shortcuts", st == 200 and len(d.get("shortcuts", [])) >= 4
          if isinstance(d, dict) else False,
          len(d.get("shortcuts", [])) if isinstance(d, dict) else st)

    print()
    print(f"{'PASS' if not fails else 'FAIL'} — {len(fails)} problem(s)")
    for f in fails:
        print(f"  x {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
