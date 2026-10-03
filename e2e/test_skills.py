"""Tests for core/skills.py — the reusable-method primitive.

The assertions are mostly about refusals, because a skill is the thing that
runs unattended. A skill that is accepted when it should be rejected, or
classified as read-only when it is not, is the failure mode that matters.

No network here. `run` is exercised with dry_run and against a fake loop.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="jarvis-test-skills-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def good(**over):
    d = {"name": "a-skill", "when": "when a thing is wrong",
         "inputs": "a repo", "steps": "1. look\n2. fix",
         "validate": "the suite passes", "returns": "a diff",
         "approval": "pushing needs approval"}
    d.update(over)
    return d


# ── the six parts are the contract ───────────────────────────────────────────

def test_all_six_required():
    from core import skills as S
    S.reset()
    check("skills.fields", set(S.FIELDS) ==
          {"when", "inputs", "steps", "validate", "returns", "approval"},
          str(S.FIELDS))
    for missing in S.FIELDS:
        if missing == "name":
            continue
        kwargs = good(name=f"no-{missing}")
        kwargs[missing] = ""
        try:
            S.save(**kwargs)
            check(f"skills.refuses_without_{missing}", False, "accepted")
        except ValueError as e:
            check(f"skills.refuses_without_{missing}", missing in str(e), str(e))
    rec = S.save(**good(name="complete"))
    check("skills.accepts_complete", rec.get("name") == "complete")


def test_no_implicit_readonly():
    """An approval field that is blank must be a refusal, not permission.

    This is the load-bearing safety property. If a blank approval were accepted
    as "no approval needed", a skill the author never finished writing would be
    schedulable unattended — and the only sign would be a silent send.
    """
    from core import skills as S
    S.reset()
    try:
        S.save(**good(name="blank", approval=""))
        check("skills.blank_refused", False, "accepted a blank approval")
    except ValueError as e:
        check("skills.blank_refused", "approval" in str(e), str(e))
    # nothing with an empty approval may exist on disk
    bad = [r["name"] for r in S.all_skills()
           if not str(r.get("approval") or "").strip()]
    check("skills.none_stored_blank", not bad, str(bad))
    # and a skill cannot be run either, even if written straight to disk
    p = S._path("sneaky")
    p.write_text('{"name": "sneaky", "steps": "x", "approval": ""}',
                 encoding="utf-8")
    r = S.run("sneaky", "task")
    check("skills.sneaky_refused_at_run", r["ok"] is False, str(r)[:90])
    p.unlink()


def test_readonly_normalisation():
    """"None" must mean one thing. If it did not, a skill would either be
    schedulable when the author did not intend it, or stuck asking forever."""
    from core import skills as S
    S.reset()
    for word in ("none", "None", "NONE", "no", "n/a", "never"):
        rec = S.save(**good(name=f"ro-{word}", approval=word, overwrite=True))
        check(f"skills.readonly({word})", S.readonly(rec) is True, rec["approval"])
        check(f"skills.readonly_spellings_unify({word})",
              rec["approval"].startswith("none"),
              rec["approval"])
    rec = S.save(**good(name="needs-ask", approval="send the email"))
    check("skills.ask_is_not_readonly", S.readonly(rec) is False)


# ── storage ──────────────────────────────────────────────────────────────────

def test_storage():
    from core import skills as S
    S.reset()
    S.save(**good(name="Stored Skill"))
    check("skills.slug", S.get("stored-skill") is not None,
          str([r["name"] for r in S.all_skills()]))
    try:
        S.save(**good(name="stored-skill"))
        check("skills.no_silent_overwrite", False, "overwrote without asking")
    except ValueError as e:
        check("skills.no_silent_overwrite", "overwrite" in str(e), str(e))
    S.save(**good(name="stored-skill", overwrite=True))
    check("skills.explicit_overwrite", S.get("stored-skill") is not None)
    check("skills.list", len(S.all_skills()) == 1, str(len(S.all_skills())))
    check("skills.delete", S.delete("stored-skill") is True)
    check("skills.delete_twice", S.delete("stored-skill") is False)


def test_completeness_of_stored():
    from core import skills as S
    S.reset()
    S.save(**good(name="c1"))
    for r in S.all_skills():
        check(f"skills.complete({r['name']})", not S.complete(r),
              str(S.complete(r)))


# ── the prompt a runner hands a model ────────────────────────────────────────

def test_prompt_shape():
    from core import skills as S
    S.reset()
    rec = S.save(**good(name="p1", bot="engineering"))
    p = S.as_prompt(rec, "the specific thing this time")
    for head in ("SKILL: p1", "THIS TIME", "WHEN TO USE", "INPUTS AND ACCESS",
                 "STEPS", "VALIDATE BEFORE YOU RETURN", "RETURN",
                 "NEEDS HUMAN APPROVAL"):
        check(f"skills.prompt.has({head[:18]})", head in p, p[:120])
    check("skills.prompt.task_included", "specific thing" in p)
    # a skill without a task is a method, and should read as one
    check("skills.prompt.no_task", "THIS TIME" not in S.as_prompt(rec))
    check("skills.prompt.bot", "engineering" in p)
    # and it must tell the model what to do when validation fails
    check("skills.prompt.failure_rule", "could not verify" in p, p[-200:])


# ── running ──────────────────────────────────────────────────────────────────

def test_run_requires_six_parts():
    from core import skills as S
    S.reset()
    S.reset()
    # write a deliberately incomplete skill straight to disk, bypassing save()
    p = S._path("broken")
    p.write_text('{"name": "broken", "steps": "do it"}', encoding="utf-8")
    r = S.run("broken", "something")
    check("skills.run_refuses_incomplete", r["ok"] is False, str(r)[:90])
    check("skills.run_names_gaps", "missing" in r.get("error", ""),
          r.get("error", ""))
    p.unlink()


def test_run_missing_skill():
    from core import skills as S
    S.reset()
    r = S.run("nope", "x")
    check("skills.run_missing", r["ok"] is False and "no skill" in r["error"],
          str(r)[:90])


def test_dry_run_changes_nothing():
    from core import skills as S
    S.reset()
    S.save(**good(name="d1"))
    before = S.get("d1").get("runs", 0)
    r = S.run("d1", "a task", dry_run=True)
    check("skills.dry_run.flag", r.get("dry_run") is True)
    check("skills.dry_run.has_prompt", "SKILL: d1" in r.get("prompt", ""))
    check("skills.dry_run.no_counter_move", S.get("d1").get("runs", 0) == before,
          "a dry run must not count as a run")


def test_tool_surface():
    from core import skills as S
    S.reset()
    out = S.tool("list")
    check("skills.tool.empty_helps", "reusable method" in out, out[:90])
    out = S.tool("create", name="half", steps="do a thing")
    check("skills.tool.partial_refused", "needs all six" in out, out[:90])
    S.save(**good(name="t1", bot="sales"))
    out = S.tool("list")
    check("skills.tool.list", "t1" in out and "sales" in out, out[:120])
    out = S.tool("show", name="t1")
    check("skills.tool.show", "STEPS" in out and "NEEDS HUMAN APPROVAL" in out,
          out[:120])
    out = S.tool("run", name="t1")
    check("skills.tool.run_needs_task", "one-off task" in out, out[:90])
    out = S.tool("delete", name="t1")
    check("skills.tool.delete", "Deleted" in out, out[:90])


def test_run_counts_and_records():
    from core import skills as S, coder as C
    S.reset()
    S.save(**good(name="r1"))

    def fake_run(goal, path, max_steps=20, timeout_s=900, on_step=None):
        return {"ok": True, "stopped": "done", "steps": 3, "seconds": 1.0,
                "changed": ["a.py"], "summary": "did the thing",
                "transcript": []}

    orig = C.run
    C.run = fake_run
    try:
        r = S.run("r1", "the task")
    finally:
        C.run = orig
    check("skills.run.ok", r["ok"] is True, str(r)[:120])
    check("skills.run.changed", r["changed"] == ["a.py"], str(r["changed"]))
    rec = S.get("r1")
    check("skills.counted", rec["runs"] == 1, str(rec["runs"]))
    check("skills.ok_counted", rec["ok"] == 1, str(rec))
    check("skills.last_run", (rec.get("last_run") or {}).get("ok") is True,
          str(rec.get("last_run")))

    # and a failure is recorded as a failure, not as a success
    def failing(goal, path, max_steps=20, timeout_s=900, on_step=None):
        return {"ok": False, "stopped": "budget", "steps": 20, "seconds": 9.0,
                "changed": [], "summary": "ran out", "transcript": []}

    C.run = failing
    try:
        r2 = S.run("r1", "another")
    finally:
        C.run = orig
    rec = S.get("r1")
    check("skills.failure_counted", rec["runs"] == 2 and rec["failed"] == 1,
          str(rec))
    check("skills.failure_flagged", r2["ok"] is False)


# ── declared to the model ────────────────────────────────────────────────────

def test_declared():
    with open(ROOT / "main.py", encoding="utf-8", newline="") as fh:
        src = fh.read().replace("\r\n", "\n")
    i = src.index("TOOL_DECLARATIONS = [")
    j = src.index("\n]\n", i) + 3
    ns = {}
    exec(compile(src[i:j], "<t>", "exec"), ns)
    names = [d["name"] for d in ns["TOOL_DECLARATIONS"]]
    check("skills.declared", "skills" in names)
    check("skills.no_dupes", len(names) == len(set(names)),
          str([n for n in set(names) if names.count(n) > 1]))
    check("skills.dispatched", 'elif name == "skills"' in src)
    from google.genai import types
    try:
        for d in ns["TOOL_DECLARATIONS"]:
            types.FunctionDeclaration(name=d["name"],
                                      description=d.get("description", ""),
                                      parameters=d.get("parameters", {}))
        check("skills.sdk_valid", True)
    except Exception as e:
        check("skills.sdk_valid", False, str(e)[:140])


if __name__ == "__main__":
    for fn in (test_all_six_required, test_no_implicit_readonly,
               test_readonly_normalisation, test_storage,
               test_completeness_of_stored, test_prompt_shape,
               test_run_requires_six_parts, test_run_missing_skill,
               test_dry_run_changes_nothing, test_tool_surface,
               test_run_counts_and_records, test_declared):
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