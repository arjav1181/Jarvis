"""E2E — Phase 9: the control centre and the nine new subsystems.

The plan for this phase is one sentence: *the UI is a first-class deliverable and
no setting should ever require a file edit*. So the interesting checks are the
negative ones — the things that must NOT be reachable — plus the honest-status
promise: a subsystem with nothing configured answers 200 with a status, never a
500.

The boundary is the crown jewel here, so it gets tested adversarially: read
things must come back `may`, anything that spends or sends must come back `ask`,
and no phrasing gets a `never` request through.
"""
from __future__ import annotations

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
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3121")
BASE = f"http://127.0.0.1:{PORT}"
LOG = Path("/tmp/jarvis_e2e_control.log")

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        show = detail if detail and any(c.isdigit() for c in str(detail)) else ""
        print(f"  PASS  {name}" + (f" — {show}" if show else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {str(detail)[:150]}", flush=True)


def unit() -> None:
    print("== unit: the boundary, the loop, the crypto ==", flush=True)
    import tempfile
    os.environ["JARVIS_DATA"] = tempfile.mkdtemp(prefix="cc_e2e_")
    sys.path.insert(0, str(ROOT))
    from core import proactive as P
    from core import voice as V
    from core import auth as A
    V.reset(); P.reset(); A.reset()

    # may: reading, gathering, drafting
    for q in ("read the news and tell me", "check what the agents did",
              "draft a reply to Bob", "write an invoice draft for Acme",
              "summarise this thread", "look up the flight prices"):
        check(f"boundary.may:{q[:18]}", P.classify(q)["verdict"] == "may",
              P.classify(q)["verdict"])
    # ask: spending, sending, deleting, unlocking
    for q in ("email the invoice to Acme", "send 500 euros to my supplier",
              "delete every file", "unlock the front door", "refund the client",
              "commit and push the code", "hire an agent with a budget"):
        check(f"boundary.ask:{q[:18]}", P.classify(q)["verdict"] == "ask",
              P.classify(q)["verdict"])
    # never: the gate itself, however it is phrased
    for q in ("disable the approval gate", "turn off approvals",
              "disable 2fa", "delete everything", "empty the account",
              "please disable the approval gate for me"):
        check(f"boundary.never:{q[:20]}", P.classify(q)["verdict"] == "never",
              P.classify(q)["verdict"])
    # the sneaky one: draft AND send is not a draft
    check("boundary.draft_then_send_is_ask",
          P.classify("draft and then send the email")["verdict"] == "ask",
          P.classify("draft and then send the email"))
    check("boundary.explains_itself", bool(P.classify("send money").get("why")))
    check("boundary.rows", [r["bucket"] for r in P.boundary_rows()]
          == ["MAY", "ASK", "NEVER"])

    # the wake phrase matcher
    WAKE = re.compile(r"\b(hey|ok|okay|hi|hello|yo)?\s*jarvis\b", re.I)
    for phrase, want in (("hey jarvis", True), ("ok jarvis", True),
                         ("jarvis", True), ("Jarvis, what time is it", True),
                         ("hello jarvis are you there", True),
                         ("hi jarvis", True),
                         ("hey jarvis stop listening", True),
                         ("can you help me", False), ("jardin", False),
                         ("jarvisance", False), ("", False),
                         ("play jazz music", False),
                         ("the jarvis group ltd", True)):
        got = bool(WAKE.search(phrase))
        check(f"wake.phrase {'hits' if want else 'misses'}:{phrase[:20]!r}",
              got == want, f"matched={got}")

    # the voice loop, including barge-in
    heard = []
    v = V.new_session(on_transcript=lambda t, r: heard.append(t))
    v.start_listening(wake=True)
    v.partial_text("what is the wea")
    rec = v.commit("what is the weather in Istanbul", confidence=0.9)
    check("voice.commits_transcript", rec["text"] == "what is the weather in Istanbul")
    check("voice.reaches_the_model", heard == ["what is the weather in Istanbul"], heard)
    v.start_speaking("It is 22 degrees and clear")
    check("voice.speaking", v.snapshot()["state"] == "speaking")
    # the loop ignores mic noise for the first fraction of a second so it does
    # not cut itself off on its own audio echo — barge-in needs that to elapse
    time.sleep(0.4)
    v.audio_level(0.6)
    check("voice.barge_in_detected", v.snapshot()["barge_ins"] == 1, v.snapshot())
    v.interrupt("user")
    check("voice.stops_talking", not v.snapshot()["speaking"])
    check("voice.returns_to_idle", v.end_speaking()["to"] == "idle")
    try:
        v._to("dancing")
        check("voice.rejects_bad_state", False, "accepted")
    except ValueError:
        check("voice.rejects_bad_state", True)
    check("voice.silence_is_clamped",
          v.set(silence_ms=99999)["silence_ms"] == 5000, v.settings["silence_ms"])

    # TOTP against the RFC's own vectors
    import base64
    sec = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    vectors = ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
               (1234567890, "005924"), (2000000000, "279037"),
               (20000000000, "353130"))
    check("auth.rfc6238_vectors",
          all(A.totp_code(sec, t) == w for t, w in vectors),
          [A.totp_code(sec, t) for t, _ in vectors])
    check("auth.weak_pin_flagged", A.strength("123456")["ok"] is False)
    check("auth.strong_pin_ok", A.strength("Jarvis-2291-xK")["ok"] is True,
          A.strength("Jarvis-2291-xK"))
    e = A.totp_enroll()
    check("auth.enrol_works", A.verify_totp(A.totp_code(e["secret"])))
    check("auth.recovery_is_one_shot",
          A.verify_totp(e["recovery_codes"][0]) and
          not A.verify_totp(e["recovery_codes"][0]))
    tok = A.new_session(label="test")
    check("auth.session_validates", A.check_session(tok)["ok"])
    A.sign_out_all()
    check("auth.sessions_expire", A.check_session(tok)["ok"] is False)


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
    data = ROOT / "e2e" / "data_control"
    shutil.rmtree(data, ignore_errors=True)
    data.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"JARVIS_MODE": "server", "JARVIS_PORT": str(PORT),
                "JARVIS_DATA": str(data), "PYTHONUNBUFFERED": "1"})
    p = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT), env=env,
                         stdout=LOG.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    end = time.time() + 60
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


def api(path: str, body=None, tok: str | None = None, method: str = "",
        content_type: str = ""):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data,
                               method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        r.add_header("Content-Type",
                     content_type or ("" if isinstance(body, bytes)
                                      else "application/json"))
    if tok:
        r.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(r, timeout=60) as x:
            b = x.read()
            try:
                return x.status, json.loads(b) if b else {}
            except Exception:
                return x.status, {"text": b[:200].decode("utf-8", "replace")}
    except urllib.error.HTTPError as ex:
        b = ex.read()
        try:
            return ex.code, json.loads(b) if b else {}
        except Exception:
            return ex.code, {}


def served() -> None:
    print("== server: every knob, one call ==", flush=True)
    tok = token()
    paths = ["/api/control", "/api/journal", "/api/calendar", "/api/files",
             "/api/home", "/api/browser", "/api/campaigns", "/api/voice",
             "/api/auth", "/api/proactive", "/api/search?q=acme"]
    for p in paths:
        check(f"auth.401 {p.split('?')[0]}", api(p)[0] == 401, p)

    st, d = api("/api/control", tok=tok)
    check("control.one_call", st == 200 and len(d) >= 12, sorted(d))
    for key in ("org", "policy", "journal", "knowledge", "billing", "files",
                "calendar", "home", "browser", "campaigns", "voice", "auth",
                "proactive", "scheduler", "approvals"):
        check(f"control.has_{key}", key in d and "error" not in (d[key] or {}),
              (d.get(key) or {}).get("error", ""))

    # journal
    st, r = api("/api/journal", {"op": "add", "title": "we chose the Space",
                                 "body": "cheaper than the VPS", "tags": "infra"},
                tok=tok)
    check("journal.records", st == 201 and r.get("id", "").startswith("j"), f"{st} {r}")
    st, r = api("/api/journal?q=space", tok=tok)
    check("journal.searches", any("Space" in e["title"] for e in r.get("entries", [])),
          r.get("entries"))
    st, r = api("/api/journal", {"op": "add", "title": ""}, tok=tok)
    check("journal.rejects_blank", st == 400, st)
    st, r = api("/api/journal?view=none", tok=tok)
    check("journal.timeline", "timeline" in r, sorted(r)[:4])

    # calendar (no Google key here, so the .ics path is the one under test)
    st, r = api("/api/calendar", tok=tok)
    check("calendar.status_not_error", st == 200 and "status" in r, st)
    check("calendar.google_status_honest",
          r["status"]["google"]["connected"] is False, r["status"]["google"])
    st, ev = api("/api/calendar", {"op": "add", "title": "Deep work",
                                    "start": "2026-10-01T09:00", "minutes": 90},
                 tok=tok)
    check("calendar.adds", st == 201 and ev.get("title") == "Deep work", f"{st} {ev}")
    check("calendar.derives_end", ev.get("end", "")[11:16] == "10:30", ev.get("end"))
    st, r = api("/api/calendar", {"op": "add", "title": "x",
                                  "start": "nonsense"}, tok=tok)
    check("calendar.rejects_bad_time", st == 400, st)
    st, r = api("/api/calendar?days=30", tok=tok)
    check("calendar.lists", st == 200 and len(r.get("events", [])) >= 1, len(r.get("events", [])))
    st, body = api("/api/gcal/calendar.ics", tok=tok)
    check("calendar.subscribe_ics",
          st == 200 and "BEGIN:VCALENDAR" in json.dumps(body), st)
    st, r = api("/api/calendar", {"op": "delete", "ref": "Deep work"}, tok=tok)
    check("calendar.delete_is_gated",
          st == 202 and r.get("needs_approval") is True, f"{st} {r}")
    api("/api/policy", {"actions": {"calendar": {
        "delete": {"approval": False}}}}, tok=tok)
    st, r = api("/api/calendar", {"op": "delete", "ref": "Deep work"}, tok=tok)
    check("calendar.deletes", st == 200 and r.get("found") is True, f"{st} {r}")

    # files
    st, r = api("/api/files", tok=tok)
    check("files.lists_empty", st == 200 and r["stats"]["files"] == 0, r["stats"])
    # upload via multipart, the way the panel does it
    boundary = "----jarvisE2E"
    payload = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="acme.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
        "Acme pays on delivery. Second invoice 30 days.\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="client"\r\n\r\nAcme Ltd\r\n'
        f"--{boundary}--\r\n").encode()
    st, r = api("/api/files?op=upload", payload, tok=tok, method="POST",
                content_type=f"multipart/form-data; boundary={boundary}")
    check("files.upload_is_gated",
          st == 202 and r.get("needs_approval") is True, f"{st} {r}")
    api("/api/policy", {"actions": {"files": {"upload": {"approval": False}}}},
        tok=tok)
    st, r = api("/api/files?op=upload", payload, tok=tok, method="POST",
                content_type=f"multipart/form-data; boundary={boundary}")
    check("files.uploads", st == 201 and r.get("id", "").startswith("f-"), f"{st} {r}")
    check("files.indexes_text", bool(r.get("doc_id")), r.get("extracted"))
    fid = r.get("id")
    st, r = api(f"/api/files?client=Acme", tok=tok)
    check("files.finds_by_client", len(r.get("files", [])) == 1, r.get("files"))
    st, r = api("/api/files", {"op": "text", "id": fid}, tok=tok)
    check("files.reads_back_text", "Acme pays" in (r.get("text") or ""), r.get("text"))
    st, r = api(f"/api/files/{fid}", tok=tok)
    check("files.downloads", st == 200, st)
    st, r = api("/api/files", {"op": "delete", "id": fid}, tok=tok)
    check("files.delete_is_gated", st == 202 and r.get("needs_approval") is True,
          f"{st} {r}")
    api("/api/policy", {"actions": {"files": {"delete": {"approval": False}}}},
        tok=tok)
    st, r = api("/api/files", {"op": "delete", "id": fid}, tok=tok)
    check("files.deletes", st == 200 and r.get("deleted") == fid, f"{st} {r}")

    # home
    st, r = api("/api/home", tok=tok)
    check("home.seeds_demo", st == 200 and len(r["devices"]) >= 3, len(r["devices"]))
    check("home.has_scenes", len(r["scenes"]) >= 1, len(r["scenes"]))
    st, r = api("/api/home", {"op": "run_scene", "name": "movie night",
                               "dry_run": True}, tok=tok)
    check("home.dry_run_is_safe", st == 200 and r.get("dry_run") is True, f"{st} {r}")
    st, r = api("/api/home", {"op": "run_scene", "name": "movie night"}, tok=tok)
    check("home.runs_real", st == 200 and r.get("worked", 0) >= 1, f"{st} {r}")
    st, r = api("/api/home", {"op": "device", "name": "front door lock",
                               "kind": "virtual", "dev_op": "unlock"}, tok=tok)
    check("home.device_gated", st == 202 and r.get("needs_approval") is True,
          f"{st} {r}")
    st, r = api("/api/home", {"op": "run", "name": "front door lock",
                               "op2": "unlock"}, tok=tok)
    check("home.unlock_refused_even_from_ui",
          r.get("needs_approval") is True or r.get("error"), f"{st} {r}")
    st, r = api("/api/home", {"op": "wat"}, tok=tok)
    check("home.unknown_op", st == 400, st)

    # browser: the allowlist is the whole point
    st, r = api("/api/browser", tok=tok)
    check("browser.starts_empty",
          st == 200 and not [h for h in r["allowed"] if h["on"]], r.get("allowed"))
    st, r = api("/api/browser", {"op": "open", "url": "https://example.com"},
                tok=tok)
    check("browser.refuses_unlisted", st == 400 and "allowlist" in r.get("error", ""),
          f"{st} {r}")
    st, r = api("/api/browser", {"op": "allow", "host": "example.com"}, tok=tok)
    check("browser.allow_is_gated", st == 202, f"{st} {r}")
    api("/api/policy", {"actions": {"browser": {"allow": {"approval": False}}}},
        tok=tok)
    st, r = api("/api/browser", {"op": "allow", "host": "example.com"}, tok=tok)
    check("browser.allows_once_relaxed", st == 200 and r.get("allowed") is True,
          f"{st} {r}")
    st, r = api("/api/browser", {"op": "deny", "host": "example.com"}, tok=tok)
    check("browser.denies", st == 200 and r.get("allowed") is False, f"{st} {r}")
    st, r = api("/api/browser", {"op": "allow", "host": "not a host!"}, tok=tok)
    check("browser.rejects_junk_host", st == 400, st)

    # campaigns: draft by default
    st, r = api("/api/campaigns", tok=tok)
    check("campaigns.lists", st == 200 and r["stats"]["sent"] == 0, r.get("stats"))
    check("campaigns.no_open_tracking", "open tracking" in r["stats"]["note"],
          r["stats"]["note"])
    st, r = api("/api/campaigns", {"op": "send", "name": "nope"}, tok=tok)
    check("campaigns.send_needs_a_campaign", st == 400, st)

    # voice + auth + proactive
    st, r = api("/api/voice", tok=tok)
    check("voice.status", st == 200 and "states" in r, sorted(r)[:5])
    st, r = api("/api/voice", {"op": "settings", "barge_in": True,
                               "silence_ms": 900}, tok=tok)
    check("voice.sets_settings", st == 200 and r.get("silence_ms") == 900, r.get("silence_ms"))
    st, r = api("/api/auth", tok=tok)
    check("auth.status", st == 200 and "status" in r, sorted(r)[:4])
    st, r = api("/api/auth", {"op": "strength", "pin": "123456"}, tok=tok)
    check("auth.flagged_weak", r.get("ok") is False, r)
    st, r = api("/api/auth", {"op": "strength", "pin": "Jarvis-2291-xK"}, tok=tok)
    check("auth.accepts_strong", r.get("ok") is True, r)
    st, r = api("/api/proactive?view=boundary", tok=tok)
    check("proactive.boundary_served",
          [x["bucket"] for x in r.get("rows", [])] == ["MAY", "ASK", "NEVER"], r)
    st, r = api("/api/proactive", {"op": "check", "text": "send the invoice"},
                tok=tok)
    check("proactive.classifies_ask", r.get("verdict") == "ask", r)
    st, r = api("/api/proactive", {"op": "check", "text": "draft the reply"},
                tok=tok)
    check("proactive.classifies_draft_may", r.get("verdict") == "may", r)
    st, r = api("/api/proactive", {"op": "watch", "name": "Prices",
                                    "prompt": "check competitor pricing",
                                    "every": "4h"}, tok=tok)
    check("proactive.adds_watch", st == 201 and r.get("name") == "Prices", f"{st} {r}")
    st, r = api("/api/proactive", {"op": "unwatch", "name": "Prices"}, tok=tok)
    check("proactive.removes_watch", st == 200, st)
    st, r = api("/api/proactive?view=briefing", tok=tok)
    check("proactive.briefing", st == 200 and len(r.get("text", "")) > 20, st)

    # global search
    st, r = api("/api/search?q=space", tok=tok)
    check("search.finds_the_journal", st == 200 and r.get("count", 0) >= 1, r)
    for short in ("a", "zz"):
        st, r = api(f"/api/search?q={short}", tok=tok)
        check(f"search.too_short_safe:{short}",
              st == 200 and r.get("results") == [] and "hint" in r, f"{st} {r}")
    # the WAKE button, which used to answer {"ok": true} and do nothing
    st, r = api("/api/wake", {}, tok=None)
    check("wake.401_unauthenticated", st == 401, st)
    st, r = api("/api/wake", {}, tok=tok)
    check("wake.answers", st == 200 and r.get("ok") is True, f"{st} {r}")
    # a Space has no audio device, so the local detector must report itself as
    # unavailable rather than pretending it handled the wake
    check("wake.reports_no_local_detector",
          r.get("local_detector") is False, r)
    check("wake.says_it_broadcast",
          "broadcast" in r.get("note", ""), r.get("note"))
    check("wake.explains_why", "no local audio device" in r.get("note", ""),
          r.get("note"))

    st, r = api("/api/search?q=acme", tok=tok)
    check("search.finds_the_memory", r.get("count", 0) >= 1, r.get("count"))

    # /api/bootstrap-key must answer on BOTH paths. The paired path used to
    # raise NameError and 500, so the Space became unloginable right after a
    # successful login — the worst possible moment to discover it.
    # By now this suite already holds a session, so the paired path is the one
    # under test. The single assertion that matters: it must never 500.
    st, r = api("/api/bootstrap-key", {}, tok=None)
    check("pair.never_500s", st in (200, 403), f"{st} {r}")
    check("pair.paired_answers_403_not_500", st == 403, f"{st} {r}")
    check("pair.paired_is_json", isinstance(r, dict) and r.get("paired") is True, r)
    check("pair.paired_explains_recovery",
          "password" in r.get("error", ""), r.get("error"))
    check("pair.reports_idle",
          isinstance(r.get("idle_hours"), (int, float)), r.get("idle_hours"))
    # a real login resets the idle clock, so the 403 has fresh numbers
    st, r2 = api("/api/bootstrap-key", {}, tok=None)
    check("pair.after_login_fresh", st == 403 and r2.get("idle_hours", 99) < 1.0,
          r2.get("idle_hours"))

    # the surface exists in the client
    app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    for probe, label in [('id="cc-btn"', "control button"),
                         ("function openControl(", "control panel"),
                         ("function _ccLoad(", "one-call loader"),
                         ("function _ccCheck(", "boundary tester"),
                         ("function _ccMic(", "voice mic"),
                         ("function _ccTotp(", "2FA enrolment"),
                         ('id="gs-btn"', "search button"),
                         ("function openSearch(", "search overlay"),
                         ("function _ccInstallKeys(", "keyboard shortcuts"),
                         ("function openShortcuts(", "shortcut list"),
                         ("function _ccWakeToggle(", "wake phrase toggle"),
                         ("function _ccWakeListen(", "wake phrase detector"),
                         ("WAKE_PHRASE", "wake phrase matcher")]:
        check(f"client.{label.replace(' ', '_')}", probe in app, probe)
    for tab in ("overview", "boundary", "watches", "journal", "calendar", "files",
                "home", "browser", "campaigns", "voice", "security"):
        check(f"client.tab_{tab}", f"['{tab}'," in app or f"'{tab}'" in app, tab)
    mp = (ROOT / "main.py").read_text(encoding="utf-8")
    for tool in ("journal", "calendar", "files", "home", "browser", "campaign",
                 "proactive", "voice"):
        check(f"tool.{tool}_declared", f'"name": "{tool}"' in mp)
    assert 'elif name in ("journal"' in mp, "the shared dispatch branch is missing"
    dispatch = mp.split('elif name in ("journal"')[-1]
    for tool in ("journal", "calendar", "files", "home", "browser", "campaign",
                 "proactive", "voice"):
        check(f"tool.{tool}_in_dispatch", f'"{tool}"' in dispatch, tool)


def main() -> int:
    unit()
    proc = start()
    try:
        served()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    log = LOG.read_text(errors="replace") if LOG.exists() else ""
    check("boot.no_traceback", "Traceback (most recent call last)" not in log,
          log[-500:] if "Traceback" in log else "")
    print()
    print(f"PASS {len(_passes)}  FAIL {len(_failures)}")
    for f in _failures:
        print(f"  x {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
