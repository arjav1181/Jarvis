"""Tests for core/crew.py — messaging a bot, and it acting as itself.

The property that matters is memory. A bot with no transcript is the
assistant in a costume: every turn re-derives everything from its persona, so
"do it like last time" means nothing. A bot that remembers is a colleague.

No network: the model is faked, so these test the crew's behaviour and not the
model's.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="jarvis-test-crew-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def setup():
    from core import agents as A, skills as S
    A.reset(); A.seed(); S.reset()
    A.hire("Fixer", role="Engineering",
           persona="You fix code and verify it. Never claim success you did not see.",
           tools=["files"])
    S.save("check-deploys", when="you are asked whether anything is broken in production",
           inputs="the vercel connector", steps="1. ask vercel for broken deploys\n2. report",
           validate="every claim names a deployment", returns="one line per deploy",
           approval="none (read-only)", bot="Fixer")


def fake_text(prompts):
    """A model that answers from the last prompt, and remembers being told."""
    def t(contents, **kw):
        blob = " ".join(str(p.get("text", ""))
                        for turn in contents or []
                        for p in (turn.get("parts") or []) if isinstance(p, dict))
        if "Decide what the message is" in blob:
            return "TALK"
        if "called twice" in blob:
            return "yes"
        return "ack"
    return t


def test_brief():
    from core import crew as C
    setup()
    b = C.brief("Fixer")
    check("crew.brief.ok", b["ok"] is True, str(b)[:80])
    for probe in ("Fixer", "Engineering", "verify it", "check-deploys"):
        check(f"crew.brief.has({probe[:14]})", probe in b["brief"], b["brief"][:120])
    miss = C.brief("Nobody")
    check("crew.brief.missing", miss["ok"] is False, str(miss)[:80])


def test_memory_is_the_point():
    """Tell it twice; it must still be there on turn two."""
    from core import crew as C, gemini
    setup()
    orig = gemini.text
    gemini.text = fake_text(None)
    try:
        C.say("Fixer", "we are calling twice", force="talk")
        p = C.as_prompt_for_test("Fixer") if hasattr(C, "as_prompt_for_test") else None
        second = gemini.text
        # the second turn's prompt must contain the first turn's content
        seen = {}

        def spy(contents, **kw):
            blob = " ".join(str(x.get("text", "")) for t in contents or []
                            for x in (t.get("parts") or []) if isinstance(x, dict))
            if "Decide what the message is" not in blob:
                seen["blob"] = blob
            return "ack"
        gemini.text = spy
        C.say("Fixer", "again", force="talk")
        check("crew.remembers", "we are calling twice" in seen.get("blob", ""),
              seen.get("blob", "")[-160:])
    finally:
        gemini.text = orig
    check("crew.transcript_grew", len(C.transcript("Fixer")) == 4,
          str(len(C.transcript("Fixer"))))


def test_talk_does_not_run_work():
    from core import crew as C, gemini
    setup()
    orig = gemini.text
    gemini.text = fake_text(None)
    try:
        r = C.say("Fixer", "how are you?", force="talk")
        check("crew.talk.mode", r["mode"] == "talk", str(r)[:90])
        check("crew.talk.no_steps", not r.get("steps"), str(r.get("steps")))
    finally:
        gemini.text = orig


def test_work_uses_the_bot_brief():
    """The bot's standing instructions must reach the work, not the generic ones."""
    from core import crew as C, coder as K
    setup()
    seen = {}

    def fake_run(goal, path, **kw):
        seen["goal"] = goal
        return {"ok": True, "stopped": "done", "steps": 2, "seconds": 1.0,
                "changed": ["x.py"], "summary": "did it", "transcript": []}
    orig = K.run
    K.run = fake_run
    try:
        r = C.say("Fixer", "fix the thing", force="work")
    finally:
        K.run = orig
    check("crew.work.mode", r["mode"] == "work", str(r)[:90])
    check("crew.work.brief_included", "Never claim success you did not see" in seen.get("goal", ""),
          seen.get("goal", "")[:200])
    check("crew.work.ok", r["ok"] is True)


def test_named_readonly_skill_runs():
    from core import crew as C, skills as S
    setup()
    ran = {}
    orig = S.run
    S.run = lambda name, task, **kw: (ran.update({"n": name, "t": task}) or
                                       {"ok": True, "steps": 1, "seconds": 0.1,
                                        "changed": [], "summary": "all clear",
                                        "stopped": "done"})
    try:
        r = C.say("Fixer", "run check-deploys please", force="work")
    finally:
        S.run = orig
    check("crew.skill.runs", ran.get("n") == "check-deploys", str(ran))
    check("crew.skill.reported", r.get("skill") == "check-deploys", str(r)[:110])


def test_skill_needing_approval_is_not_run_silently():
    from core import crew as C, skills as S
    setup()
    S.save("send-it", when="you are asked to email a customer",
           inputs="the email tool", steps="send", validate="it sent",
           returns="a message id", approval="sending always needs approval",
           bot="Fixer")
    called = []
    orig = S.run
    S.run = lambda *a, **k: (called.append(a) or {"ok": True, "steps": 1,
                                                  "summary": "sent", "stopped": "done"})
    try:
        r = C.say("Fixer", "send-it", force="work")
    finally:
        S.run = orig
    check("crew.approval_skill_not_auto_ran", called == [], str(called))
    check("crew.approval_skill_explained",
          "could not run" in str(r.get("reply", "")).lower()
          or r.get("ok") is False, str(r)[:140])


def test_transcript_bounded():
    from core import crew as C
    setup()
    for i in range(500):
        C._append("Fixer", {"role": "user", "text": f"turn {i}"})
    check("crew.transcript_bounded", len(C.transcript("Fixer", limit=1000)) <= 400,
          str(len(C.transcript("Fixer", limit=1000))))
    check("crew.transcript_clear", C.clear("Fixer") is True)
    check("crew.transcript_empty", C.transcript("Fixer") == [])


def test_missing_bot():
    from core import crew as C
    setup()
    r = C.say("Ghost", "hello", force="talk")
    check("crew.missing_bot", r.get("ok") is False, str(r)[:80])
    out = C.tool("say", bot="Ghost", message="hi")
    check("crew.missing_tool_prose", "no bot named" in out, out[:80])


def test_tool_surface():
    from core import crew as C
    setup()
    out = C.tool("list")
    check("crew.tool.list", "Fixer" in out and "check-deploys" in out, out[:140])
    out = C.tool("brief", bot="Fixer")
    check("crew.tool.brief", "Fixer" in out, out[:80])
    out = C.tool("wat")
    check("crew.tool.unknown", "Unknown action" in out, out[:80])


if __name__ == "__main__":
    for fn in (test_brief, test_memory_is_the_point, test_talk_does_not_run_work,
               test_work_uses_the_bot_brief, test_named_readonly_skill_runs,
               test_skill_needing_approval_is_not_run_silently,
               test_transcript_bounded, test_missing_bot, test_tool_surface):
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
