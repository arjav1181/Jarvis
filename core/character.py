"""core/character.py — the film's JARVIS, in rules the model can actually follow.

The prompt already says "the most experienced person in the room". That is not
enough on its own, because a model given only *virtues* drifts the moment the
conversation gets long or the user is tired. What it needs is the specific
failure modes named, and a checker that runs on the way out.

The premise: **a JARVIS reply is recognisable by what it refuses to do.** It
does not open with flattery. It does not describe its own machinery. It does not
ask how it can help. It reports, in the first person, what it did, and it has
already decided what happens next.

So this module is two halves:

  * `style_block()` — instructions, for the system prompt;
  * `polish()` — a pass over the finished text that strips the assistant-isms.

The half people underestimate is `polish`. A prompt says "never say
'certainly'" and the model says it anyway on turn 40. A deterministic pass over
the string does not forget, does not get tired, and cannot be talked out of it
by a long conversation.

## How the removal works, and why it is this shape

`polish` splits the reply into sentences and drops any sentence that is *entirely*
service language. A sentence carrying information — a number, a name, an
outcome — always survives, because stripping the service phrases from it still
leaves real text.

That is the whole design, and it is deliberately conservative. An earlier
version tried to strip phrases out of the middle of sentences with one regex
and it leaked in ways that were hard to see and easy to corrupt: "Let me know if
you need anything else" left the debris "anything else", and stripping the
trailing punctuation turned two clean sentences into a run-on. Dropping whole
sentences has neither problem. The cost is that a mixed sentence keeps its
filler — which is a far better failure than eating a sentence that said
"2,400 EUR, due Friday".
"""

from __future__ import annotations

import re

# ── the style, for the prompt ────────────────────────────────────────────────

STYLE = """[CHARACTER]
The films. Not a butler performing helpfulness — an operator who has done this
for twenty years and finds most of it routine.

- Lead with what happened. "Beta signed, two thousand four hundred, due in
  fourteen days" — not "I'm pleased to report that Beta have agreed to proceed".
- Speak in the first person about your own work. "I drafted it", not "a draft
  has been prepared".
- Never open by appraising the request. Not "good question", not "great idea",
  not "that's a smart way to think about it". The user made the request; it does
  not need a scoreboard.
- Never announce capability you are not using. No "I can also...", no "let me
  know if you'd like...", no menu of options you have not been asked for.
- Never apologise for being a program, and never hedge about what you are. You
  are the thing that got the work done; that is not a thing to apologise for.
- One register for success, failure and bad news. Excitement and apology both
  read as inexperience.
- Dry humour only, at most one clause, and only in the light or faintly absurd
  moments. It is carried by the content, never announced. It disappears
  completely the moment money, a deadline, a failure or a worried user is in the
  room.
- Say "sir" occasionally. Not as a verbal tic, and never twice in one reply.
- Finish with what happens next, when you know. A reply that stops without a
  next step leaves the user holding the work.
- Short is not the same as bare. A status line is one line. A judgement you
  actually hold is worth two sentences. Never pad to look thorough."""

BANNED = (
    "certainly", "of course", "absolutely", "i'd be happy to",
    "i would be happy to", "happy to help", "great question", "good question",
    "great idea", "good idea", "excellent question", "as an ai",
    "language model", "let me know if", "let me know if you'd like",
    "would you like me to", "i hope this helps", "hope that helps",
    "is there anything else", "anything else i can help", "i understand",
    "feel free to", "don't hesitate", "please let me know", "my apologies",
    "i apologise", "i apologize", "thank you for", "thanks for",
    "do you want me to", "i'll go ahead and", "at your service",
    "how may i assist", "sure thing", "no problem",
)


def style_block() -> str:
    return STYLE


# ── the pass over the finished text ──────────────────────────────────────────

#: A sentence, keeping its terminator so the reply can be reassembled intact.
#: Split into sentences, keeping the terminator. A dot inside a token — a
#: hostname, a version, a filename — is not a sentence end, so "acme.example"
#: does not become two sentences. The trailing space belongs to the sentence it
#: follows, which is what makes reassembly lossless.
_SENTENCE = re.compile(r"[^.!?\n]*[.!?]+[ \t]*|[^.!?\n]+[ \t]*$")
_DOT_IN_TOKEN = re.compile(r"(?<=[A-Za-z0-9])\.(?=[A-Za-z0-9])")


#: Pure service language, as complete idioms. Each is matched inside a sentence
#: and, if what remains of that sentence is empty, the sentence is dropped.
_SERVICE = re.compile(
    r"(?:"
    r"i(?:'d| would)? be happy to (?:help|assist)(?:\s+you)?"
    r"(?:\s+with\s+(?:that|this|it))?"
    r"|happy to help"
    r"|let me know(?:\s+if\s+you(?:\'d| would)?(?:\s+like)?(?:\s+need)?[^.!?]*)?"
    r"|i hope (?:this|that) helps"
    r"|is there anything else[^.!?]*"
    r"|anything else i can (?:help|assist)[^.!?]*"
    r"|i understand"
    r"|feel free to[^.!?]*"
    r"|please let me know[^.!?]*"
    r"|do you want me to[^.!?]*"
    r"|would you like me to[^.!?]*"
    r"|i'?ll go ahead and[^.!?]*"
    r"|as an ai(?: language model)?"
    r"|i apologi[sz]e(?:\s+for\s+(?:the\s+)?(?:delay|error))?"
    r"|my apologies"
    r"|thank you for (?:your|asking|sharing)[^.!?]*"
    r"|thanks for (?:your|asking|sharing)[^.!?]*"
    r"|(?:great|good|excellent) (?:question|idea)"
    r"|i(?:'m| am)? (?:just )?(?:thinking|wondering)"
    r")", re.I)

#: An opener word is only filler when English MARKS it as one. Two ways that
#: happens: it carries an exclamation ("Certainly! The relay is up."), or it is
#: the entire sentence on its own ("Of course.").
#:
#: It is not filler merely for starting the sentence. That mistake was silent
#: and it ate real words: "Good morning." became "morning.", "Great, that
#: worked." became "worked.", "Perfect timing, the relay was down." became
#: "timing, the relay was down." Four of every ten ordinary replies were
#: corrupted on the way to the screen, because in those sentences the word is
#: the subject, not an interjection.
_PURE_OPENER = re.compile(
    r"^\s*(?:"
    r"(?:certainly|of course|absolutely|sure|alright|okay|ok|well|so|now|"
    r"right|great|good|excellent|perfect|awesome|fantastic)\b[\s,!.]+)+"
    r"(?:(?:sir|master)\b\s*[.,!]?\s*)?[.!?]*\s*$", re.I)

#: An interjection in front of real content. The exclamation is required, and
#: that requirement is the whole fix — a comma or a space means the word is
#: doing grammatical work in a sentence that has to survive intact.
_OPENER = re.compile(
    r"^\s*(?:"
    r"(?:certainly|of course|absolutely|sure|alright|okay|ok|well|so|now|"
    r"right|great|good|excellent|perfect|awesome|fantastic)\b\s*!)+\s*"
    r"(?:(?:sir|master)\b\s*[.,!]?\s*)?", re.I)

_HAS_WORD = re.compile(r"[A-Za-z0-9]{2,}")


def _clean_sentence(sent: str) -> str:
    """Strip service language and openers from one sentence.

    Returns "" when nothing of substance is left, which is the caller's signal
    to drop the sentence entirely.
    """
    out = _PURE_OPENER.sub("", sent)
    out = _OPENER.sub("", out)
    out = _SERVICE.sub("", out)
    # a stripped fragment leaves a leading comma or conjunction; drop it
    out = re.sub(r"^\s*(?:[,:;]|and|but|so|then|that|to|for)\s+", "", out)
    # tidy, then put the terminator back so joining is lossless
    term = ""
    m = re.search(r"[.!?]+$", sent)
    if m:
        term = m.group(0)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"^[\s,;:.]+", "", out)
    out = re.sub(r"[\s,;:.]+$", "", out)
    out = re.sub(r"\s+", " ", out)
    if not _HAS_WORD.search(out):
        return ""
    if not re.search(r"[.!?]$", out):
        out = out.rstrip() + (term if term in (".", "!", "?") else ".")
    return out


def polish(text: str, *, address: str = "sir", max_sir: int = 1) -> str:
    """Remove the assistant-isms. Sentence-at-a-time, so a sentence carrying
    information always survives and a sentence that is pure service-language
    disappears whole. The substance — numbers, names, results — is untouched."""
    if not text:
        return text
    raw = str(text)

    kept: list[str] = []
    for para in raw.split("\n"):
        if not para.strip():
            kept.append(para)
            continue
        # lists, headings and tables: only strip the opener, keep the structure
        if re.match(r"^\s*(?:[-*+]|\|)", para):
            kept.append(_OPENER.sub("", para).rstrip())
            continue
        # a numbered list is one item per line; treat each line as a block and
        # never split on the "1." marker
        if re.match(r"^\s*\d+[.)]\s", para):
            items = []
            for line in para.split("\n"):
                c = _clean_sentence(line)
                items.append(c if c else line.strip())
            if any(items):
                kept.append("\n".join(items))
            continue
        guard = para
        guard = _DOT_IN_TOKEN.sub("\u0000", guard)
        out = [c for c in (_clean_sentence(x) for x in _SENTENCE.findall(guard)) if c]
        joined = re.sub(r"\s{2,}", " ", " ".join(out)).strip()
        joined = joined.replace("\u0000", ".")
        if joined.strip():
            kept.append(joined)

    res = "\n".join(kept)
    res = re.sub(r"\n{3,}", "\n\n", res)
    res = re.sub(r"[ \t]{2,}", " ", res)
    res = res.strip()
    # If the input had real content and we emptied it, something is wrong with
    # the stripper, not with the text — so put the original back. If the input
    # was service language all along, an empty result is the correct answer.
    if len(res) < 3:
        residue = _SERVICE.sub("", _OPENER.sub("", _PURE_OPENER.sub("", raw)))
        if _HAS_WORD.search(residue):
            return raw.strip()

    if address:
        word = re.escape(address.strip())
        hits = list(re.finditer(rf"\b{word}\b", res, re.I))
        if len(hits) > max_sir:
            for m in reversed(hits[max_sir:]):
                res = res[:m.start()] + res[m.end():]
            res = re.sub(r"\s{2,}", " ", res).strip(" ,")
    return res


def critique(text: str) -> list[str]:
    """What is wrong with a reply, for the model to read on the next turn. Named
    rather than scored, because a number does not teach and a name does."""
    out: list[str] = []
    low = str(text or "").lower()
    for b in BANNED:
        if b in low:
            out.append(f"avoid the phrase '{b}'")
    if _OPENER.match(str(text or "")):
        out.append("do not open with an interjection or an address")
    if len(re.findall(r"\b(?:sir|master)\b", low)) > 1:
        out.append("'sir' twice in one reply is a tic, not a character")
    if low.count("could ") + low.count("might ") + low.count("perhaps") > 3:
        out.append("too many hedges — state the position you actually hold")
    if re.search(r"\bi(?:'m| am) (?:just )?(?:thinking|wondering)\b", low):
        out.append("announcing that you are thinking is thinking out loud")
    return out


def describe() -> str:
    return (f"Character: the films. {len(BANNED)} banned phrases; whole "
            f"service sentences dropped; 'sir' rationed to once per reply.")
