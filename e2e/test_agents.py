"""E2E — Phase 8: the company (agent roster, work orders, deliverables).

The plan's promise is "JARVIS as CEO" and "configure agents in the dashboard".
So this file checks the two things that would quietly make that a lie:

  1. a new agent is a row, not a class — hire one from the API and it exists,
     with its own persona, tools, budget and success rate;
  2. nobody spends money without the user knowing — an agent with no budget
     cannot take a paid job, and a budget granted through the API is audited.
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
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3119")
BASE = f"http://127.0.0.1:{PORT}"
LOG = Path("/tmp/jarvis_e2e_agents.log")

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
    print("== unit: the roster is config, not code ==", flush=True)
    import tempfile
    os.environ["JARVIS_DATA"] = tempfile.mkdtemp(prefix="ag_e2e_")
    sys.path.insert(0, str(ROOT))
    from core import agents as A
    A.reset()

    n = A.seed()
    check("seed.the_starter_crew", n == 7, n)
    rows = A.roster()
    check("seed.roles_cover_the_plan",
          {"Engineering", "Sales", "Marketing", "Research", "Operations"}
          <= {r["role"] for r in rows}, sorted({r["role"] for r in rows}))
    check("seed.not_reseeded", A.seed() == 0,
          "a second seed would undo the user's edits")
    check("seed.personas_written", all(r["persona"] for r in rows))

    # a role added today, with no code change
    a = A.hire("Night Shift", role="Operations", persona="Quiet. Numbers only.",
               tools="knowledge, invoice", budget_usd_day=0.5)
    check("hire.row_created", a["name"] == "Night Shift" and a["role"] == "Operations")
    check("hire.tools_parsed", a["tools"] == ["knowledge", "invoice"], a["tools"])
    check("hire.found_by_partial", A.find("night")["name"] == "Night Shift")
    check("hire.in_roster", len(A.roster()) == 8, len(A.roster()))

    w = A.brief("Night Shift", "Report on collections")
    for probe in ("# Night Shift — Operations", "Quiet. Numbers only.",
                  "## Tools you may use", "knowledge, invoice",
                  "## Daily budget", "$0.50", "## The job", "Report on collections"):
        check(f"brief.has_{re.sub(r'[^a-z0-9]+', '_', probe.lower())[:34]}",
              probe in w["text"], probe)
    check("brief.returns_tools", w["tools"] == ["knowledge", "invoice"])

    # budget enforcement, before the work happens
    check("budget.zero_is_free", A.may_spend("Night Shift", 0.0)[0])
    ok, why = A.may_spend("Night Shift", 0.2)
    check("budget.allowed_under_cap", ok, why)
    ok, why = A.may_spend("Night Shift", 5.0)
    check("budget.refuses_over_cap", not ok and "budget" in why, why)
    A.hire("Coder")           # no budget at all
    ok, why = A.may_spend("Coder", 0.01)
    check("budget.no_budget_means_no_spend", not ok and "no daily budget" in why, why)
    check("budget.unknown_agent", A.may_spend("Nobody", 0.0)[0] is False)
    A.record_spend("Night Shift", 0.3)
    check("budget.spend_accumulates", A.spent("Night Shift") == 0.3,
          A.spent("Night Shift"))
    ok, _ = A.may_spend("Night Shift", 0.3)
    check("budget.counts_todays_spend", not ok, "0.3 spent + 0.3 asked > 0.5 cap")

    # versions
    A.deliver("Night Shift", "report", "Collections", "first pass")
    second = A.deliver("Night Shift", "report", "Collections", "second pass")
    check("versions.same_title_bumps", second["version"] == 2, second["version"])
    check("versions.other_title_is_v1",
          A.deliver("Night Shift", "report", "Churn", "x")["version"] == 1)
    A.review(second["id"], "Coder", "revise", "add the overdue list")
    d = A.get_deliverable(second["id"])
    check("review.recorded", d["status"] == "reviewed" and len(d["reviews"]) == 1)
    check("review.byline", d["reviews"][0]["reviewer"] == "Coder", d["reviews"][0])
    try:
        A.review("nope", "Coder", "pass")
        check("review.unknown_404s", False, "accepted")
    except ValueError:
        check("review.unknown_404s", True)

    # runs and the org number
    A.log_run("Night Shift", job="shift 1", ok=True, cost=0.1)
    A.log_run("Night Shift", job="shift 2", ok=False)
    o = A.org()
    # 7 seeded + Night Shift; "Coder" was already on the seeded roster, so
    # hiring it again updated it rather than adding a 9th
    check("org.counts", o["agents"] == 8 and o["active"] == 8, o)
    check("org.success_rate", o["success_rate"] == 0.5, o["success_rate"])
    check("org.spend_today", abs(o["spent_usd_today"] - 0.4) < 1e-6,
          o["spent_usd_today"])
    rate = {r["name"]: r for r in A.roster()}["Night Shift"]
    check("org.per_agent_rate", rate["success_rate"] == 0.5, rate)
    check("org.per_agent_spend", rate["spent_usd_total"] >= 0.4, rate)
    check("org.describe", "agents active" in A.describe(), A.describe())

    # a shift gives real numbers, and does not fake the work
    txt = A.shift("Night Shift")
    check("shift.has_snapshot", "## What changed" in txt or "## Daily budget" in txt)
    check("shift.logs_the_run", len(A.recent_runs()) >= 3)

    check("retire.switches_off", A.retire("Night Shift")["enabled"] is False)
    check("retire.keeps_history",
          len(A.deliverables(agent="Night Shift")) == 3)
    _roster = {r["name"]: r for r in A.roster()}
    check("retire.status", _roster["Night Shift"]["status"] == "retired",
          _roster["Night Shift"].get("status"))
    check("roster.marks_idle", A.roster()[0]["status"] in ("idle", "working", "retired"))
    try:
        A.shift("Night Shift")
        check("retired_refuses_to_work", False, "accepted")
    except ValueError as e:
        check("retired_refuses_to_work", "retired" in str(e), str(e))
    check("delete.removes", A.retire("Night Shift", delete=True)["deleted"] == "Night Shift")
    check("delete.gone", A.find("Night Shift") is None)
    try:
        A.retire("Ghost")
        check("retire.unknown_404s", False, "accepted")
    except ValueError:
        check("retire.unknown_404s", True)


def policy() -> None:
    print("== unit: the gate on hiring and spending ==", flush=True)
    from core import policy as P
    P.reset()
    check("hire_free_without_budget",
          P.check("agents", {"action": "hire_no_budget"}).needs_approval is False)
    check("hire_gated_with_budget",
          P.check("agents", {"action": "hire"}).needs_approval is True)
    check("brief_costs_money",
          P.check("brief", {"action": "create"}).needs_approval is True)
    check("filing_is_free",
          P.check("deliverable", {"action": "record"}).needs_approval is False)
    check("firing_is_delete",
          P.check("agents", {"action": "delete"}).tier == "delete")


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
    data = ROOT / "e2e" / "data_agents"
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


def api(path: str, body: dict | None = None, tok: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               method="POST" if data else "GET")
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
    print("== server: /api/agents, /api/deliverables ==", flush=True)
    tok = token()
    check("auth.401_agents", api("/api/agents")[0] == 401)
    check("auth.401_post", api("/api/agents", {"op": "hire", "name": "x"})[0] == 401)
    check("auth.401_deliverables", api("/api/deliverables")[0] == 401)

    st, d = api("/api/agents", tok=tok)
    rows = d.get("agents") or []
    check("get.seeds_on_first_read", st == 200 and len(rows) == 7, len(rows))
    check("get.has_org", "org" in d and d["org"]["agents"] == 7, d.get("org"))
    check("get.has_roles", "Sales" in (d.get("roles") or []), d.get("roles"))
    check("get.view_org", api("/api/agents?view=org", tok=tok)[1].get("active") == 7)
    check("get.view_runs",
          "runs" in api("/api/agents?view=runs", tok=tok)[1])

    # hire without a budget: allowed, and it is a row
    st, a = api("/api/agents", {"op": "hire", "name": "Night Shift",
                                "role": "Operations", "persona": "Quiet.",
                                "tools": "knowledge, invoice"}, tok=tok)
    check("post.hires", st == 201 and a.get("name") == "Night Shift", f"{st} {a}")
    check("post.tools_stored", a.get("tools") == ["knowledge", "invoice"], a.get("tools"))

    # hire WITH a budget: must be refused until the user says so
    st, d = api("/api/agents", {"op": "hire", "name": "Moneybags",
                                "role": "Research", "persona": "Reads sources.",
                                "budget_usd_day": 2.0}, tok=tok)
    check("post.budget_needs_approval",
          st == 202 and d.get("needs_approval") is True, f"{st} {d}")
    st, roster = api("/api/agents", tok=tok)
    check("post.gated_hire_did_nothing",
          not any(x["name"] == "Moneybags" for x in roster["agents"]))
    st, d = api("/api/policy", {"actions": {
        "agents": {"hire": {"approval": False},
                   "delete": {"approval": False}}}}, tok=tok)
    cfg = d.get("config", {})
    check("policy.takes_agent_actions",
          st == 200 and "agents" in (cfg.get("actions") or {}),
          sorted(cfg))
    st, mb = api("/api/agents", {"op": "hire", "name": "Moneybags",
                                "role": "Research", "persona": "Reads sources.",
                                "budget_usd_day": 2.0}, tok=tok)
    check("post.budget_allowed_once_relaxed",
          st == 201 and mb.get("budget_usd_day") == 2.0, f"{st} {mb}")

    st, b = api("/api/agents", {"op": "brief", "name": "Night Shift",
                                "job": "Report on collections"}, tok=tok)
    check("post.brief", st == 200 and "## The job" in b.get("text", ""), st)

    st, o = api("/api/agents", {"op": "shift", "name": "Night Shift"}, tok=tok)
    check("post.shift", st == 200 and "Night Shift" in o.get("order", ""), st)

    st, r = api("/api/deliverables", {"op": "record", "agent": "Night Shift",
                                      "kind": "report", "title": "Collections",
                                      "body": "first pass", "summary": "€1,200 in"},
                tok=tok)
    check("deliverable.records", st == 201 and r.get("version") == 1, f"{st} {r}")
    ref = r.get("id")
    st, r2 = api("/api/deliverables", {"op": "record", "agent": "Night Shift",
                                       "kind": "report", "title": "Collections",
                                       "body": "second pass"}, tok=tok)
    check("deliverable.version_bumps", r2.get("version") == 2, r2.get("version"))
    st, rv = api("/api/deliverables", {"op": "review", "ref": ref,
                                      "reviewer": "user", "verdict": "pass",
                                      "note": "good"}, tok=tok)
    check("deliverable.review", st == 200 and rv["status"] == "reviewed", st)
    st, d = api("/api/deliverables", tok=tok)
    rows = d.get("deliverables") or []
    check("deliverable.lists", st == 200 and len(rows) == 2, len(rows))
    check("deliverable.filter_kind", len(api(
        "/api/deliverables?kind=report", tok=tok)[1]["deliverables"]) == 2)
    check("deliverable.filter_agent_misses",
          len(api("/api/deliverables?agent=Coder",
                  tok=tok)[1]["deliverables"]) == 0)
    # deleting a deliverable is a delete: it is refused until the user relaxes it
    st, d = api("/api/deliverables", {"op": "delete", "ref": ref}, tok=tok)
    check("deliverable.delete_is_gated",
          st == 202 and d.get("needs_approval") is True, f"{st} {d}")
    st, still = api("/api/deliverables", tok=tok)
    check("deliverable.gated_delete_kept_it",
          any(x["id"] == ref for x in still["deliverables"]))
    api("/api/policy", {"actions": {"deliverable": {
        "delete": {"approval": False}}}}, tok=tok)
    st, d = api("/api/deliverables", {"op": "delete", "ref": ref}, tok=tok)
    check("deliverable.delete", st == 200 and d.get("deleted"), f"{st} {d}")
    st, d = api("/api/deliverables", {"op": "delete", "ref": "gone"}, tok=tok)
    check("deliverable.delete_missing_404", st == 404, st)

    st, a = api("/api/agents", {"op": "retire", "name": "Night Shift"}, tok=tok)
    check("post.retire", st == 200 and a.get("enabled") is False, st)
    st, d = api("/api/agents", {"op": "wat"}, tok=tok)
    check("post.unknown_op_400", st == 400, st)
    st, d = api("/api/agents", {"op": "hire", "name": ""}, tok=tok)
    check("post.no_name_400", st == 400, st)

    st, d = api("/api/audit?action=agents.hire&limit=5", tok=tok)
    check("hiring_is_audited", bool(d.get("rows")), d.get("rows"))
    check("audit.says_the_budget", "2.00" in json.dumps(d["rows"][0]), d["rows"][0])

    app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    for probe, label in [('id="ag-btn"', "company button"),
                         ("function openAgents(", "company panel"),
                         ("_agSave", "hire form"),
                         ("_agShift", "shift button"),
                         ('id="dl-btn"', "deliverables button"),
                         ("function openDeliverables(", "deliverables panel"),
                         ("_dlReview", "review buttons")]:
        check(f"client.{label.replace(' ', '_')}", probe in app, probe)
    mp = (ROOT / "main.py").read_text(encoding="utf-8")
    for tool in ("agents", "brief", "deliverable"):
        check(f"tool.{tool}_declared", f'"name": "{tool}"' in mp)
    check("tool.delegate_is_org_aware", "_ag.brief(who, prompt)" in mp
          and 'source="agent" if who else "voice"' in mp)


def main() -> int:
    unit()
    policy()
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


if __name__ == "__main__":
    sys.exit(main())
