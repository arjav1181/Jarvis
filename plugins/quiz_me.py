"""
Quiz me, and actually keep score.

You say a topic, it asks questions, you answer, it marks them and adapts:
get two right and it gets harder, get two wrong and it backs off. State lives
on the volume, so the quiz survives a restart and you can walk away and come
back to the same run.

    action=start:  begin on a topic. difficulty=1 easy … 5 brutal.
    action=answer: your answer to the question it just asked.
    action=score:  the run so far.
    action=stop:   end the quiz and say what to revise.

The questions are written by the assistant itself, not by a bank. That is the
honest trade: it can quiz you on anything you have actually been working on,
including the pages you asked it to read, and it can go from easy to hard in
one step. It is also not a fixed syllabus, and it will occasionally ask a poor
question — which is why it is explicit about grading on a keyword it tells you
it is using, rather than pretending to understand a nuanced answer.

Marking is by keyword, deliberately, and it says so: it can tell you that you
had the right idea, but it will not pretend to read an essay. When it is
unsure it says unsure rather than guessing your score.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

QUESTIONS_PER_RUN = 0        # unlimited; the difficulty is the limit
MAX_STREAK_BONUS = 3
DIFFICULTIES = {1: "easy", 2: "gentle", 3: "medium", 4: "hard", 5: "brutal"}


def _state_path() -> Path:
    from core.data_paths import data_root
    d = data_root() / "quiz"
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict) -> None:
    _state_path().write_text(json.dumps(st, indent=2), encoding="utf-8")


def _difficulty_from_correct(streak: int, wrong: int, base: int) -> int:
    """Move up on a run of correct answers, down on a run of wrong ones."""
    d = int(base or 3)
    if streak >= 3:
        d += 1
    elif wrong >= 2:
        d -= 1
    return max(1, min(5, d))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(s or "").lower()).strip()


def _mark(answer: str, keys) -> tuple[str, str]:
    """(verdict, why). verdict is right | close | wrong | unsure."""
    a = _norm(answer)
    if not a:
        return "unsure", "you did not answer"
    if not keys:
        return "unsure", "no answer key was given for this one"
    if len(a) <= 2 and a in ("y", "n", "1", "0"):
        return ("right", "you answered yes/no and so did the key") \
            if a in _norm(keys) else ("wrong", "yes/no, and the other way")
    for k in keys:
        k = _norm(k)
        if not k:
            continue
        if k in a or a in k:
            return "right", f"'{k}' is in there"
    # partial credit: share a long word with the key
    a_words = set(a.split())
    for k in keys:
        k_words = set(_norm(k).split())
        if not k_words or not a_words:
            continue
        overlap = a_words & k_words
        if overlap and max(len(w) for w in overlap) >= 5:
            return "close", f"you had part of it — '{overlap.pop()}'"
    return "wrong", "not close to the key"


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "score").strip().lower()
    topic = str(parameters.get("topic") or "").strip()
    answer = str(parameters.get("answer") or parameters.get("text") or "").strip()
    difficulty = parameters.get("difficulty")
    try:
        return _run(parameters, action, topic, answer, difficulty, player)
    except Exception as e:
        return f"The quiz broke: {type(e).__name__}: {e}"[:200]


def _run(parameters: dict, action: str, topic: str, answer: str,
         difficulty, player) -> str:
    # read the question-registration fields once, here, so _run does not have
    # to reach for the dict it was not given
    keys = [k.strip() for k in str(parameters.get("keys") or "").split(",")
            if k.strip()]
    q_text = str(parameters.get("q") or "").strip()
    answer_key = str(parameters.get("a") or "").strip()
    label = str(parameters.get("label") or "").strip()
    st = _load()

    if action in ("stop", "end", "quit"):
        if not st:
            return "There is no quiz running."
        asked, right, close, wrong = (st.get("asked", 0), st.get("right", 0),
                                     st.get("close", 0), st.get("wrong", 0))
        pct = int((right + close * 0.5) * 100 / max(1, asked))
        weak = st.get("weak") or []
        out = [f"Quiz on '{st.get('topic', '?')}' over. {asked} asked, "
               f"{right} right, {close} close, {wrong} wrong — about {pct}%."]
        if weak:
            out.append("Worth going over again: " + ", ".join(weak[:5]) + ".")
        out.append("Start another with a topic.")
        _state_path().unlink(missing_ok=True)
        return " ".join(out)

    if action in ("score", "status", ""):
        if not st:
            return ("No quiz running. Give me a topic and say start — "
                    "for example: quiz me on async Python, difficulty 4.")
        asked, right = st.get("asked", 0), st.get("right", 0)
        pct = int(right * 100 / max(1, asked))
        pending = st.get("current")
        out = [f"'{st.get('topic')}' — {asked} asked, {right} right ({pct}%), "
               f"difficulty {st.get('difficulty', 3)}."]
        if pending:
            out.append(f"Waiting on your answer to: {pending['q'][:90]}")
        return " ".join(out)

    if action in ("start", "begin", "new"):
        if not topic:
            return "What shall I quiz you on?"
        st = {"topic": topic, "difficulty": max(1, min(5, int(difficulty or 3))),
              "asked": 0, "right": 0, "close": 0, "wrong": 0,
              "streak": 0, "wrong_run": 0, "weak": [],
              "current": {"q": "", "keys": [], "a": "", "label": ""},
              "started": time.time()}
        _save(st)
        return _ask(st, player)

    if action in ("ask", "register", "next"):
        """The assistant hands over a question plus the key it will mark with.

        This step exists because the questions are written by the assistant,
        and a quiz that cannot be marked is not a quiz. Registering the key at
        the same moment as the question is what makes grading honest instead
        of a guess made afterwards.
        """
        if not st:
            return ("No quiz running. Say start with a topic first.")
        if not keys:
            return ("Give me the answer keys for that question — without them "
                    "I cannot mark it, and I will not guess your score.")
        st["current"] = {"q": q_text, "keys": keys,
                         "a": answer_key, "label": label}
        _save(st)
        return (f"Question {st.get('asked', 0) + 1} registered "
                f"({DIFFICULTIES[st.get('difficulty', 3)]}). "
                f"Ask it: {st['current']['q']}" if st["current"]["q"]
                else "Registered. Ask it, and I will mark the answer.")

    if action in ("answer", "reply"):
        cur = st.get("current") or {}
        if not cur.get("keys"):
            return ("Nothing is waiting with an answer key. Ask me a question "
                    "first — action=ask with keys, a and the question text.")
        if not answer:
            return "Say the answer, and I will mark it."
        verdict, why = _mark(answer, cur.get("keys") or [])
        st["asked"] = int(st.get("asked", 0)) + 1
        if verdict == "right":
            st["right"] = int(st.get("right", 0)) + 1
            st["streak"] = int(st.get("streak", 0)) + 1
            st["wrong_run"] = 0
            praise = ["Correct.", "That's it.", "Right.", "Good — that is the one."][
                st["streak"] % 4]
        elif verdict == "close":
            st["close"] = int(st.get("close", 0)) + 1
            st["streak"] = 0
            praise = f"Partly — {why}."
        elif verdict == "wrong":
            st["wrong"] = int(st.get("wrong", 0)) + 1
            st["wrong_run"] = int(st.get("wrong_run", 0)) + 1
            st["streak"] = 0
            weak = st.setdefault("weak", [])
            if cur.get("label") and cur["label"] not in weak:
                weak.append(cur["label"])
            praise = f"No — {why}. The answer was {cur.get('a')}."
        else:
            st["streak"] = 0
            praise = f"I cannot grade that ({why}). The answer was {cur.get('a')}."
        st["difficulty"] = _difficulty_from_correct(
            st.get("streak", 0), st.get("wrong_run", 0), st.get("difficulty", 3))
        st["current"] = None
        _save(st)
        return (f"{praise} [{st['right']}/{st['asked']} right, now "
                f"{DIFFICULTIES[st['difficulty']]}]\n\n" + _ask(st, player))

    return ("Actions: start (needs a topic), answer, score, stop.")


def _ask(st: dict, player) -> str:
    """Ask for the next question, and put it on the HUD if we can.

    The question text is written by the assistant rather than kept in a bank,
    because the whole value is that it can quiz on what the user has actually
    been working on — including pages they asked it to read — and step from
    easy to hard in one go. The trade is that it can ask a poor question, so
    the instruction below insists on an answer key; without one it cannot
    mark, and it says so rather than inventing a grade.
    """
    d = st.get("difficulty", 3)
    topic = st.get("topic", "")
    if player is not None and hasattr(player, "show_quiz"):
        try:
            player.show_quiz(topic, DIFFICULTIES[d])
        except Exception:
            pass
    return (f"Next: a {DIFFICULTIES[d]} question on {topic} "
            f"({st.get('right', 0)}/{st.get('asked', 0)} so far). Ask it, then "
            f"grade the user's next answer with action=answer, giving also "
            f"keys=<comma-separated keywords that count as correct>, "
            f"a=<the short correct answer> and label=<sub-topic to revise>. "
            f"Keep the question under 40 words. If the topic is too vague to "
            f"quiz, say so and ask for one.")


PLUGIN = {
    "name": "quiz_me",
    "description": (
        "Quiz the user on any topic and keep score, getting harder while they "
        "get answers right. Use this for 'quiz me', 'test me on X', 'am I "
        "ready for the exam', or 'drill me on this'. It tracks the run and "
        "tells them what to revise. It grades on keywords and says so."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "start | answer | score | stop"},
            "topic": {"type": "STRING", "description": "for start"},
            "difficulty": {"type": "INTEGER",
                           "description": "1 easy to 5 brutal"},
            "answer": {"type": "STRING", "description": "for answer"},
            "q": {"type": "STRING",
                  "description": "for ask: the question text"},
            "keys": {"type": "STRING",
                     "description": "for ask: comma-separated keywords that "
                                    "count as correct"},
            "a": {"type": "STRING",
                  "description": "for ask: the short correct answer"},
            "label": {"type": "STRING",
                      "description": "for ask: the sub-topic, so revision "
                                     "can point at it later"},
        },
        "required": [],
    },
}
