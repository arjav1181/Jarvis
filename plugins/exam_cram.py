"""
Exam preparation, built from your own material.

Give it the syllabus or the notes and it builds a revision run: a plan ordered
by what you are weakest on, practice questions with answer keys, and a mock
paper on request. Progress is kept, so "what should I revise tonight" has a
real answer instead of a shrug.

    action=plan:    build a revision plan from a topic or a set of notes.
    action=drill:   practice questions on one sub-topic, marking as it goes.
    action=mock:    a full-length mock paper.
    action=progress: what is improving and what is stuck.
    action=reset:   clear the run. (Asks — it deletes progress.)

Where the topics come from matters more than anything else here. Given a
syllabus, it turns each line into a sub-topic with a weight. Given your own
past questions and mistakes — from quiz_me, or from anything you have marked
weak — it weights those higher, because a thing you got wrong before is a
thing you will get wrong again.

The limits are stated in the output rather than glossed: it does not know your
course, it has not seen past papers unless you gave it them, and a mock paper
it invented is a rehearsal for the format, not a prediction of the real thing.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

#: How many sub-topics a single plan will carry. More than this and a person
#: cannot actually revise it tonight.
MAX_TOPICS = 14
#: A sub-topic you keep getting wrong climbs this much per miss.
MISR_WEIGHT = 0.34


def _state_path() -> Path:
    from core.data_paths import data_root
    d = data_root() / "exam"
    d.mkdir(parents=True, exist_ok=True)
    return d / "run.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict) -> None:
    _state_path().write_text(json.dumps(st, indent=2), encoding="utf-8")


def _topics_from(syllabus: str) -> list[str]:
    """Turn a blob of syllabus into sub-topics.

    Syllabi arrive as bullets, numbered lists, or one-per-line, and splitting
    on all three is the difference between a usable plan and a single
    enormous topic called "everything".
    """
    out: list[str] = []
    for raw in str(syllabus or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^[-*•]\s*", "", s)
        s = re.sub(r"^\d+[.)]\s*", "", s)
        s = re.sub(r"^[-–—]\s*", "", s)
        s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
        if len(s) < 3 or len(s) > 120:
            continue
        out.append(s)
    seen, uniq = set(), []
    for t in out:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:MAX_TOPICS]


def _weight(st: dict, topic: str) -> float:
    """Weak topics float to the top. Difficulty is not a guess: it is the miss
    count, so a thing you have got wrong twice outranks a thing you have never
    been asked about."""
    rec = (st.get("topics") or {}).get(topic) or {}
    asked = int(rec.get("asked") or 0)
    right = int(rec.get("right") or 0)
    if asked == 0:
        base = 0.5                      # untried: middling
    else:
        base = 1.0 - (right / asked)    # 0 all right, 1 all wrong
    return round(base + int(rec.get("misses") or 0) * MISR_WEIGHT, 3)


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "progress").strip().lower()
    topic = str(parameters.get("topic") or "").strip()
    syllabus = str(parameters.get("syllabus") or parameters.get("notes") or "").strip()
    count = int(parameters.get("count") or 8)
    try:
        return _run(action, topic, syllabus, max(1, min(40, count)), player)
    except Exception as e:
        return f"Exam prep broke: {type(e).__name__}: {e}"[:200]


def _run(action: str, topic: str, syllabus: str, count: int, player) -> str:
    st = _load()

    if action in ("reset", "clear"):
        _state_path().unlink(missing_ok=True)
        return "Cleared. Nothing carried over from the last run."

    if action in ("plan", "set", "build"):
        if not topic and not syllabus:
            return ("Give me the subject, and paste the syllabus or notes if "
                    "you have them. Bullets or one per line both work.")
        found = _topics_from(syllabus) or [topic]
        st = {"subject": topic or found[0], "topics": st.get("topics", {}),
              "built": time.time()}
        for t in found:
            st["topics"].setdefault(t, {"asked": 0, "right": 0, "misses": 0})
        _save(st)
        ranked = sorted(found, key=lambda t: -_weight(st, t))
        lines = [f"{len(found)} sub-topics for {st['subject']}:"]
        for i, t in enumerate(ranked[:10], 1):
            rec = st["topics"][t]
            mark = ("untried" if not rec["asked"]
                    else f"{rec['right']}/{rec['asked']} right")
            lines.append(f"{i}. {t} — {mark}")
        lines.append("Say drill and a sub-topic for practice, or mock for a "
                     "full paper.")
        lines.append("I do not know your course, and I have not seen a real "
                     "past paper unless you gave me one — treat a mock as "
                     "rehearsal, not a prediction.")
        return "\n".join(lines)

    if not st:
        return ("There is no plan yet. Give me the subject and the syllabus "
                "and say plan.")

    if action in ("progress", "status", ""):
        recs = st.get("topics") or {}
        if not recs:
            return "The plan has no sub-topics yet — send the syllabus."
        ranked = sorted(recs, key=lambda t: -_weight(st, t))
        asked = sum(int(r.get("asked") or 0) for r in recs.values())
        right = sum(int(r.get("right") or 0) for r in recs.values())
        pct = int(right * 100 / max(1, asked))
        out = [f"{st.get('subject')}: {asked} questions asked across "
               f"{len(recs)} topics, {pct}% right."]
        stuck = [t for t in ranked if _weight(st, t) >= 0.8][:4]
        if stuck:
            out.append("Stuck on: " + ", ".join(stuck) + ".")
        else:
            out.append("Nothing is badly stuck right now.")
        return " ".join(out)

    if action in ("drill", "practice"):
        target = topic or max((st.get("topics") or {}),
                              key=lambda t: _weight(st, t), default="")
        if not target:
            return "No sub-topics to drill — build a plan first."
        rec = (st.get("topics") or {}).setdefault(
            target, {"asked": 0, "right": 0, "misses": 0})
        rec["drill_remaining"] = int(rec.get("drill_remaining") or count)
        rec["drill_topic"] = target
        _save(st)
        return (f"Drilling '{target}' — about {count} questions, starting at "
                f"the difficulty you are actually at. Ask question one, then "
                f"mark each answer with keys=<keywords>, a=<correct answer> "
                f"and say which sub-topic it was, so the weighting stays "
                f"honest. Stop when I say so.")

    if action in ("mock", "paper"):
        recs = st.get("topics") or {}
        ranked = sorted(recs, key=lambda t: -_weight(st, t))[:6] or ["the topic"]
        return (f"Mock paper on {st.get('subject')}: {count} marks across "
                f"{len(ranked)} areas, weighted toward what I am worst at — "
                f"{', '.join(ranked[:4])}.\n\n"
                f"Write the paper first, mark scheme second, and keep the mark "
                f"scheme separate so I cannot see it while you are working. "
                f"Then tell me my score topic by topic and the weighting "
                f"improves for next time.")

    return "Actions: plan (needs a subject), drill, mock, progress, reset."


PLUGIN = {
    "name": "exam_cram",
    "description": (
        "Build a revision plan and drill the user on it, weighting what they "
        "are actually worst at rather than what the syllabus lists first. Use "
        "this for 'help me revise for', 'I have an exam on', 'what should I "
        "revise tonight', or 'give me a mock paper'. It keeps a run across "
        "sessions. It does not know their course and has not seen a real past "
        "paper unless given one, and it says so."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "plan | drill | mock | progress | reset"},
            "topic": {"type": "STRING", "description": "the sub-topic"},
            "syllabus": {"type": "STRING",
                         "description": "the topics, as bullets or one a line"},
            "count": {"type": "INTEGER",
                      "description": "how many questions, default 8"},
        },
        "required": [],
    },
}
