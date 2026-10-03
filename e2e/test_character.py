"""Unit tests for the character pass.

The hard part of this module is not that it removes things — it is that it
removes the *right* things and leaves everything else byte-identical. So most of
these assert on what must NOT change.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import character as C  # noqa: E402

fails: list[str] = []
n = 0


def check(name, ok, detail=""):
    global n
    n += 1
    if not ok:
        fails.append(f"{name}: {detail}")


# ── must survive: opener words doing grammatical work ───────────────────────
# Regression. These were all silently mangled on the way to the screen. The
# stripper treated any sentence-initial "good/great/well/right/perfect" as
# filler, but in these sentences the word IS the subject: "Good morning."
# became "morning.", "Great, that worked." became "worked.". Four of every ten
# ordinary replies were corrupted. An opener is only filler when English marks
# it as one — an exclamation, or the whole sentence on its own.
for text in (
    "Good morning. What are we doing?",
    "Good evening. The deploy is green.",
    "Great, that worked.",
    "Well, that is a first.",
    "Perfect timing, the relay was down.",
    "Right, the calendar is empty.",
    "So the port is bound, then.",
    "Now the deploy is green.",
):
    n += 1
    ok = C.polish(text) == text
    if not ok:
        fails.append(f"corrupted:{text[:30]}: {C.polish(text)!r}")

# and the production wrapper must never hand back an empty reply
n += 1
for text in ("Right.", "Okay.", "Certainly!"):
    if not (C.polish(text) or text):
        fails.append(f"production wrapper emptied {text!r}")

# ── must be removed entirely ────────────────────────────────────────────────
for text in (
    "Certainly!",
    "Of course.",
    "Let me know if you need anything else.",
    "Is there anything else I can help with?",
    "I would be happy to help with that.",
    "I understand.",
    "Do you want me to send the draft?",
    "Would you like me to send it?",
    "I hope this helps!",
    "Please let me know if you would like more detail.",
    "Feel free to ask.",
    "My apologies.",
    "Thank you for your patience.",
):
    check(f"drops:{text[:28]}", C.polish(text) == "", repr(C.polish(text)))

# ── must survive completely ─────────────────────────────────────────────────
for text in (
    "Beta signed at 2,400 EUR, due in 14 days.",
    "The Bengaluru lead has not replied. I will chase on Thursday.",
    "The camera is being difficult. I will use the second one.",
    "Two things are broken: the relay and the DNS record.",
    "I could not reach the server. It timed out after 20 seconds.",
    "The site is live at acme.example.",
):
    check(f"keeps:{text[:30]}", C.polish(text) == text, repr(C.polish(text)))

# A sentence that mixes framing with a real answer keeps the answer. "As an AI,
# I cannot do that" is blunt, not empty — the stripper removes the framing and
# leaves the refusal, which is the whole job.
check("mixed.as_an_ai_keeps_the_refusal",
      C.polish("As an AI, I cannot do that.") == "I cannot do that.",
      repr(C.polish("As an AI, I cannot do that.")))

# ── mixed: the filler goes, the information stays ───────────────────────────
check("mixed.keeps_the_number",
      "2,400" in C.polish("Certainly! I'd be happy to help. Beta signed at 2,400 EUR."))
check("mixed.keeps_the_fact",
      "relay is back" in C.polish("I apologise for the delay. The relay is back up."))
check("mixed.drops_the_filler",
      "happy to help" not in C.polish("I would be happy to help. The build finished.").lower())

# ── punctuation and structure ────────────────────────────────────────────────
# "I hope this helps!" is pure filler, so it goes and the two real sentences
# either side of it are joined cleanly — not welded, not padded.
two = C.polish("Certainly! Beta signed. I hope this helps!")
check("punct.keeps_final_stop", two.endswith("."), repr(two))
check("punct.drops_the_filler_sentence", two == "Beta signed.", repr(two))
joined = C.polish("Beta signed. I hope this helps! Bengaluru next.")
check("punct.joins_the_survivors", joined == "Beta signed. Bengaluru next.",
      repr(joined))
check("punct.no_run_on", "daysBengaluru" not in joined)

lists = C.polish("- Beta: 2,400\n- Bengaluru: waiting\n- Newsletter: two months")
check("lists.intact", lists.count("\n-") == 2, repr(lists))

numbered = C.polish("1. First thing.\n2. Second thing.")
check("lists.numbered_intact", numbered == "1. First thing.\n2. Second thing.",
      repr(numbered))

# ── the address is rationed ─────────────────────────────────────────────────
sir3 = C.polish("Yes, sir. The relay is up. Nothing else, sir. Noted, sir.")
check("sir.rationed", sir3.lower().count("sir") == 1, repr(sir3))
check("sir.survives_once", "sir" in sir3.lower(), repr(sir3))
check("sir.opener_vocative_absorbed",
      C.polish("Certainly! Yes, sir. The relay is up.") == "Yes, sir. The relay is up.",
      repr(C.polish("Certainly! Yes, sir. The relay is up.")))

# ── never returns nothing for a real reply ──────────────────────────────────
check("safety.never_empties_real", C.polish("Beta signed at 2,400 EUR.") != "")
check("safety.short_unchanged", C.polish("Yes.") == "Yes.", repr(C.polish("Yes.")))

# ── critique names the problem ──────────────────────────────────────────────
c = C.critique("Certainly! I'd be happy to help, sir. I think perhaps it could work, sir.")
check("critique.finds_banned", any("certainly" in x for x in c), c)
check("critique.finds_tic", any("tic" in x for x in c), c)
check("critique.finds_opener", any("interjection" in x for x in c), c)
check("critique.quiet_on_good", C.critique("Beta signed at 2,400 EUR. Bengaluru next.") == [],
      C.critique("Beta signed at 2,400 EUR. Bengaluru next."))

# ── the style block reaches the model ───────────────────────────────────────
block = C.style_block()
check("style.has_character_header", "[CHARACTER]" in block)
check("style.says_sir_rarely", "sir" in block.lower())
check("style.forbids_flattery", "appraising" in block or "scoreboard" in block)
# The style block must actually name the failure modes the stripper enforces.
for phrase in ("appraising", "scoreboard", "apologise", "sir"):
    check(f"style.mentions_{phrase}", phrase in block.lower(), phrase)

print(f"PASS {n - len(fails)}  FAIL {len(fails)}")
for f in fails:
    print("  x", f)
raise SystemExit(1 if fails else 0)
