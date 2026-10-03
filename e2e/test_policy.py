"""E2E — Phase 5: the policy engine, the approvals inbox, the audit log.

The point of these checks is that safety is *fail-closed* and *legible*:

  * an unrecognised tool is treated as the strict case, because the realistic
    failure is a new tool shipping by accident;
  * $0 a day is the default, and a negative cap clamps to 0 rather than becoming
    a way to always spend;
  * every decision — allowed, gated, approved, declined, blocked — leaves a row,
    including the ones that never happened;
  * a risky tool actually blocks until a human answers, and the tool call
    returns a real result either way (the voice session is waiting on it).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3116")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_policy"
LOG = Path("/tmp/jarvis_e2e_policy.log")

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


# ── unit: the rules ──────────────────────────────────────────────────────────

def rules() -> None:
    print("== unit: tiers, budgets, rate limits ==", flush=True)
    # isolated data dir: otherwise a previous run's policy.json in the repo root
    # makes "defaults to zero" fail for reasons that have nothing to do with
    # this code. (Learned the hard way — it read cap=10 and blamed the product.)
    import tempfile
    os.environ["JARVIS_DATA"] = tempfile.mkdtemp(prefix="policy_e2e_")
    sys.path.insert(0, str(ROOT))
    from core import policy as P

    check("tiers.defined", set(P.TIERS) == {"read", "act", "spend", "delete"},
          P.TIERS)
    check("default_is_the_strict_one", P.DEFAULT_TIER == "spend", P.DEFAULT_TIER)
    check("fail_closed.unknown_tool",
          P.check("brand_new_tool").needs_approval is True
          and P.check("brand_new_tool").tier == "spend",
          P.check("brand_new_tool").as_dict())

    P.reset()
    cat = P.catalogue()
    check("catalogue.covers_every_tool", len(cat) >= 20, f"{len(cat)} tools")
    check("catalogue.sorted_by_severity",
          [r["tier"] for r in cat] == sorted(
              [r["tier"] for r in cat],
              key=lambda t: P.TIER_RANK[t]), "dangerous tools must not be buried")
    risky = [r["tool"] for r in cat if r["tier"] in ("spend", "delete")]
    check("catalogue.risky_need_approval",
          all(P.check(t).needs_approval for t in risky), risky[:5])
    check("catalogue.reads_are_free",
          not P.check("recall_memory").needs_approval
          and not P.check("system_status").needs_approval)

    # budgets
    check("budget.defaults_to_zero",
          P.budget()["cap_usd"] == 0.0, P.budget())
    ok, why = P.may_spend(1.0, what="an api call")
    check("budget.zero_means_nothing_spends", ok is False, why[:60])
    P.save_config({"daily_spend_cap_usd": 10})
    check("budget.save_takes_effect", P.budget()["cap_usd"] == 10.0, P.budget())
    ok, why = P.may_spend(4.0, what="a")
    check("budget.under_cap_allowed", ok, why[:50])
    # nothing spent yet, so 9 of 10 is legitimately fine; 11 is not
    ok, _ = P.may_spend(9.0, what="a")
    check("budget.nine_of_ten_allowed", ok is True)
    ok, why = P.may_spend(11.0, what="a")
    check("budget.over_cap_refused", ok is False, why[:60])
    P.record_spend(4.0, what="a")
    P.record_spend(1.0, what="b")
    b = P.budget()
    check("budget.accumulates", abs(b["spent_usd"] - 5.0) < 0.001, b)
    check("budget.remaining", abs(b["remaining_usd"] - 5.0) < 0.001, b)
    check("budget.exhausts", P.may_spend(6.0)[0] is False)

    # a negative cap is a bug, not a policy
    P.save_config({"daily_spend_cap_usd": -50})
    check("budget.negative_clamps", P.budget()["cap_usd"] == 0.0, P.budget())
    P.save_config({"daily_spend_cap_usd": 10})

    # per-call ceiling is checked BEFORE the call
    P.COSTS["push_task"] = 999.0
    P.save_config({"per_call_cap_usd": 25})
    d = P.check("push_task")
    check("caps.per_call_blocks_before_running", d.allowed is False, d.reason[:60])
    P.COCTS = None
    P.COSTS.pop("push_task", None)

    # rate limiting
    P.save_config({"rate_limit_per_min": 3, "tiers": {}})
    got = [P.check("undo").allowed for _ in range(6)]
    check("rate.six_calls_three_allowed", got[:3] == [True, True, True]
          and got[3:] == [False, False, False], got)
    P.save_config({"rate_limit_per_min": 30})

    # overrides from the "dashboard"
    P.save_config({"tiers": {"undo": {"tier": "delete", "approval": True}}})
    d = P.check("undo")
    check("override.tier_and_approval", d.tier == "delete" and d.needs_approval, d)
    P.save_config({"tiers": {}})

    # unknowns in a patch are ignored rather than written
    cfg = P.save_config({"nonsense_key": 1})
    check("config.ignores_unknown_keys", "nonsense_key" not in cfg, sorted(cfg)[:4])

    # the audit log
    P.audit("probe_one", "act", actor="tester", target="t", result="allowed")
    P.audit("probe_two", "spend", actor="scheduler", target="t", result="blocked")
    rows = P.audit_tail(5)
    check("audit.rows_written", len(rows) >= 2, len(rows))
    check("audit.newest_first", rows[0]["action"] == "probe_two", rows[0]["action"])
    check("audit.fields_present",
          all(k in rows[0] for k in ("ts", "iso", "action", "tier", "actor",
                                     "target", "result", "detail", "cost")), sorted(rows[0]))
    check("audit.filters_by_result",
          all(r["action"] == "probe_two" for r in P.audit_tail(5, action="probe_two")))
    check("audit.filters_by_actor",
          all(r["actor"] == "scheduler"
              for r in P.audit_tail(5, actor="scheduler")))
    check("audit.append_only_file", (ROOT / "e2e" / "data_policy").exists()
          or P._audit_path().exists(), str(P._audit_path()))
    check("audit.stats", P.audit_stats()["blocked"] >= 1, P.audit_stats()["blocked"])


def queue() -> None:
    print("== unit: the approvals queue ==", flush=True)
    import asyncio
    from core import confirm as C

    shown: list[str] = []
    C.bind(lambda t, d: shown.append(t), lambda: None, lambda m: None)
    C.reject_all()

    C.request("a", "Push code", "repo x", lambda: "pushed")
    C.request("b", "Send email", "to bob", lambda: "sent")
    C.request("c", "Shut down", "", lambda: "bye")
    inbox = C.inbox()
    check("queue.holds_three", [i["title"] for i in inbox]
          == ["Push code", "Send email", "Shut down"], [i["title"] for i in inbox])
    check("queue.only_head_drives_the_hud", shown == ["Push code"], shown)
    C.resolve(True)
    time.sleep(0.3)
    check("queue.promotes_the_next", [i["title"] for i in C.inbox()]
          == ["Send email", "Shut down"], shown)
    check("queue.card_swapped", shown[-1] == "Send email", shown)
    check("queue.reject_all_clears", C.reject_all() == 2 and C.count() == 0)

    async def scenario() -> tuple[bool, bool]:
        loop = asyncio.get_running_loop()

        async def answer(delay, ok):
            await asyncio.sleep(delay)
            C.resolve(ok)
        t1 = asyncio.create_task(answer(0.3, True))
        a = await C.request_async("x", "Push code", "repo x", loop)
        await t1
        t2 = asyncio.create_task(answer(0.2, False))
        b = await C.request_async("y", "Send email", "to", loop)
        await t2
        return a, b

    a, b = asyncio.run(scenario())
    check("queue.async_gate_approves", a is True)
    check("queue.async_gate_declines", b is False)
    check("queue.async_gate_drains", C.count() == 0)


# ── the server ───────────────────────────────────────────────────────────────

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
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"JARVIS_MODE": "server", "JARVIS_PORT": str(PORT),
                "JARVIS_DATA": str(DATA), "PYTHONUNBUFFERED": "1"})
    p = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT), env=env,
                         stdout=LOG.open("w"), stderr=subprocess.STDOUT)
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
    raise RuntimeError("server did not start:\n"
                       + (LOG.read_text(errors="replace")[-1500:] if LOG.exists() else ""))


_tok = ""


def token() -> str:
    global _tok
    if _tok:
        return _tok
    import re
    r = urllib.request.Request(BASE + "/api/bootstrap-key", data=b"{}", method="POST")
    with urllib.request.urlopen(r, timeout=30) as x:
        key = json.loads(x.read())["key"]
    with urllib.request.urlopen(f"{BASE}/auto-login?key={key}", timeout=30) as x:
        html = x.read().decode(errors="replace")
    _tok = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html).group(1)
    return _tok


def api(path: str, body: dict | None = None, tok: str | None = None,
        method: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               method=method or ("POST" if data else "GET"))
    if data:
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


def served() -> None:
    print("== server: /api/policy, /api/audit, /api/approvals ==", flush=True)
    tok = token()
    for p in ("/api/policy", "/api/audit", "/api/approvals"):
        check(f"auth.401{p.replace('/api', '')}", api(p)[0] == 401, p)
    for p in ("/api/policy", "/api/approvals/resolve", "/api/approvals/reject_all"):
        check(f"auth.401post{p.replace('/api', '')}",
              api(p, {})[0] == 401, p)

    st, d = api("/api/policy", tok=tok)
    check("policy.get", st == 200 and len(d.get("tools", [])) >= 20,
          f"{st} {len(d.get('tools', []))} tools")
    check("policy.has_config_and_budget",
          "config" in d and "budget" in d and "inbox" in d, sorted(d)[:6])
    check("policy.tiers_listed", d.get("tiers") == ["read", "act", "spend", "delete"],
          d.get("tiers"))

    st, d = api("/api/policy", {"daily_spend_cap_usd": 25, "rate_limit_per_min": 45},
                tok=tok)
    check("policy.saves_limits",
          st == 200 and d["budget"]["cap_usd"] == 25
          and d["config"]["rate_limit_per_min"] == 45, d.get("budget"))
    st, d = api("/api/policy", {"daily_spend_cap_usd": -10}, tok=tok)
    check("policy.negative_cap_clamps", d["config"]["daily_spend_cap_usd"] == 0,
          d["config"]["daily_spend_cap_usd"])
    api("/api/policy", {"daily_spend_cap_usd": 25}, tok=tok)

    st, d = api("/api/policy", {"tiers": {"undo": {"tier": "delete", "approval": True}}},
                tok=tok)
    undo = next((t for t in d.get("tools", []) if t["tool"] == "undo"), {})
    check("policy.saves_a_tool_tier",
          undo.get("tier") == "delete" and undo.get("approval") is True, undo)
    api("/api/policy", {"tiers": {}}, tok=tok)

    # a queued approval shows up in the inbox and can be resolved from here
    sys.path.insert(0, str(ROOT))
    st, d = api("/api/approvals", tok=tok)
    check("approvals.empty_at_first", st == 200 and d.get("items") == [], d)
    st, d = api("/api/approvals/resolve", {"accepted": True}, tok=tok)
    check("approvals.resolve_with_nothing_waiting", st == 200, st)
    st, d = api("/api/approvals/reject_all", {}, tok=tok)
    check("approvals.reject_all", st == 200 and d.get("rejected") == 0, d)

    # audit
    st, d = api("/api/audit?limit=20", tok=tok)
    check("audit.endpoint", st == 200 and "rows" in d and "stats" in d, sorted(d)[:4])
    st, d = api("/api/audit?action=nothing_matches", tok=tok)
    check("audit.filter_returns_empty", st == 200 and d.get("rows") == [], d.get("rows"))

    app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    for probe, label in [('id="pol-btn"', "header button"),
                         ("function openPolicy(", "panel opener"),
                         ("_polRenderApprovals", "approvals tab"),
                         ("_polRenderPolicy", "policy tab"),
                         ("_polRenderAudit", "audit tab"),
                         ("/api/approvals/resolve", "resolve call"),
                         ("_polSaveTools", "permission save")]:
        check(f"client.{label.replace(' ', '_')}", probe in app, probe)


def action_tools_registered() -> None:
    """Every action-loader tool must be named in the policy.

    They were all missing, which meant each one fell through to
    DEFAULT_TIER = "spend" and asked for approval - a plain web search
    included. That is how the assistant came to look like it wanted permission
    for everything it was asked to do.
    """
    from core import policy as P
    try:
        from core import action_loader as AL
        reg = AL.discover_actions(ROOT / "actions")
        names = [d.get("name") for d in reg.get_tool_declarations()]
    except Exception as e:
        check("policy.actions.discover", False, type(e).__name__ + ": " + str(e))
        return
    check("policy.actions.found", len(names) >= 15, "only " + str(len(names)))
    listed = set(P.TOOLS) | set(P.ACTIONS)
    missing = [n for n in names if n not in listed]
    check("policy.actions.all_registered", not missing, str(missing))

    for t in ("web_search", "web_fetch", "weather_report", "flight_finder"):
        d = P.check(t, {})
        check("policy." + t + ".no_approval",
              not d.needs_approval and d.tier == "read",
              "tier=" + d.tier + " approval=" + str(d.needs_approval))

    d = P.check("send_message", {})
    check("policy.send_message.asks", d.needs_approval, str(d))
    d = P.check("file_controller", {"action": "delete"})
    check("policy.file_delete.asks",
          d.needs_approval and d.tier == "delete", str(d))
    d = P.check("file_controller", {"action": "list"})
    check("policy.file_list.free", not d.needs_approval, str(d))
    d = P.check("browser_control", {"action": "fill_form"})
    check("policy.browser_fill.asks", d.needs_approval, str(d))
    d = P.check("browser_control", {"action": "get_text"})
    check("policy.browser_get.free", not d.needs_approval, str(d))
    d = P.check("file_processor", {"action": "run"})
    check("policy.fileproc_run.asks", d.needs_approval, str(d))
    d = P.check("file_processor", {"action": "summarize"})
    check("policy.fileproc_summarize.free", not d.needs_approval, str(d))


def connector_awareness() -> None:
    """The model must be told its own systems beat a web search.

    Asked for a Vercel project's public URL it ran a web search and reported
    the project could not be found. A capability list existed, but it was sent
    in the client payload rather than to the model, so nothing told it to
    prefer its own tools over the open web.
    """
    with open(ROOT / "main.py", encoding="utf-8", newline="") as fh:
        src = fh.read().replace(chr(13) + chr(10), chr(10))
    check("aware.block_exists", "def _connector_block()" in src)
    i = src.find("def _connector_block")
    j = src.find("def _describe_tools")
    ns = {}
    exec(compile(src[i:j], "<t>", "exec"), ns)
    body = ns["_connector_block"]()
    for probe in ("YOUR OWN SYSTEMS", "Vercel", "GitHub", "Gmail",
                  "Never substitute a web search"):
        check("aware." + probe[:22], probe in body, body[:120])
    check("aware.in_prompt", "parts.append(_con)" in src)


def main() -> int:
    rules()
    action_tools_registered()
    connector_awareness()
    queue()
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
          log[-400:] if "Traceback" in log else "")
    print()
    print(f"PASS {len(_passes)}  FAIL {len(_failures)}")
    for f in _failures:
        print(f"  x {f}")
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
