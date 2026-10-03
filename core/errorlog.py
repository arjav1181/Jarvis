"""The last few things that went wrong, with the secrets taken out.

This exists because of a specific failure: the Space was doing nothing, nothing
in the runtime said why, and I guessed wrong twice — blaming the image, then the
front end — before finding that nobody was logged in. The whole cost of that
was not having one place to look.

So the app keeps a short in-memory ring of recent errors and serves it on
/api/health. In memory on purpose: a log file on the persistent volume would
outlive the process, which is exactly the wrong property for "what just broke".

Everything is redacted on the way in, not on the way out, because the route that
reads this is unauthenticated. A traceback is the single most likely place in a
program for a key to appear by accident — httpx echoes request headers, and a
failed Gemini call puts the URL and sometimes the key right in the message. So
the patterns below run before a line is ever stored, and there is no code path
that stores an unredacted line.
"""

import logging
import re
import sys
import threading
from collections import deque
from typing import Optional

#: How much to keep. Enough to see a repeating fault, small enough that the
#: health route stays cheap and cannot be used to page through history.
MAX_LINES = 40
_MAX_CHARS = 400

_lock = threading.Lock()
_recent: deque = deque(maxlen=MAX_LINES)

#: Applied in order. First match wins per pattern; the whole set is applied to
#: every line, because one traceback can carry two different kinds of secret.
_PATTERNS: tuple[tuple["re.Pattern", str], ...] = (
    # Google API keys (AIza…), HuggingFace (hf_…), GitHub (ghp_/gho_/ghs_),
    # OpenAI (sk-…), Slack (xox…), Stripe, and long bearer-ish blobs.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{10,}"), "AIza[REDACTED]"),
    (re.compile(r"\bhf_[0-9A-Za-z]{10,}"), "hf_[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[0-9A-Za-z]{10,}"), "gh*_[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"), "xox*_[REDACTED]"),
    (re.compile(r"\bsk-[0-9A-Za-z_\-]{16,}"), "sk-[REDACTED]"),
    # JSON Web Tokens, in three dot-separated base64url parts.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"),
     "[REDACTED-JWT]"),
    # Anything labelled as a credential, however it was labelled. The value is
    # consumed whole (\S+) rather than up to a delimiter: the patterns above
    # have already rewritten some values to contain brackets, and a delimiter-
    # aware version stopped half way through one and printed the shape of the
    # redaction back at itself.
    (re.compile(r"(?i)\b(api[_\-]?key|apikey|secret|password|passwd|token|"
                r"authorization|auth|bearer)\b(\s*[:=]\s*|\s*[:=]\s*)(\S+)"),
     lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]"),
    # A Bearer header value.
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    # Bare high-entropy hex, which is what a session token looks like.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED-HEX]"),
)


def redact(text: str) -> str:
    """Scrub a line. Applied before storage, always."""
    out = str(text)
    for pat, rep in _PATTERNS:
        try:
            out = pat.sub(rep, out)
        except Exception:
            # A bad pattern must never take the logger down with it — losing
            # the log is the one failure this module cannot afford.
            continue
    out = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", out)
    if len(out) > _MAX_CHARS:
        out = out[:_MAX_CHARS] + "…"
    return out


def record(line: str) -> None:
    """Store one redacted line. Cheap, safe to call from anywhere."""
    try:
        with _lock:
            _recent.append(redact(line))
    except Exception:
        pass


def recent(limit: int = 12) -> list:
    """The newest last. Returns redacted lines only — there is no accessor for
    the unredacted buffer, on purpose."""
    try:
        with _lock:
            items = list(_recent)
    except Exception:
        return []
    n = max(1, min(int(limit or 1), MAX_LINES))
    return items[-n:]


def clear() -> None:
    with _lock:
        _recent.clear()


class _Capture(logging.Handler):
    """Pulls warnings and worse out of the `logging` module.

    Only `logging`, not `print` — this app narrates most of its life with
    print(), and intercepting that would mean rewriting call sites or shadowing
    builtins. What lands here is library warnings and anything that used the
    logging module properly, which is where systemic faults show up. Uncaught
    exceptions come in through the excepthook below.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.WARNING:
                return
            record.record(f"log {record.name}: {record.getMessage()}")
        except Exception:
            pass


_installed = False


def install() -> None:
    """Attach the handler and the excepthook. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    try:
        root = logging.getLogger()
        root.addHandler(_Capture())
        if root.level == logging.NOTSET or root.level > logging.WARNING:
            root.setLevel(logging.WARNING)
    except Exception:
        pass

    # This app reports its own failures with traceback.print_exc() far more
    # often than it lets them escape, and print_exc writes straight to stderr —
    # it never reaches the excepthook. That is the dominant error idiom in this
    # codebase, so without this the log would be nearly empty precisely when
    # something has gone wrong. Wrapping it captures the same text the operator
    # sees, redacted.
    try:
        import traceback as _tb
        _real_print_exc = _tb.print_exc

        def _print_exc(*a, **kw):
            try:
                import io
                buf = io.StringIO()
                _real_print_exc(*a, file=buf, **({"limit": kw["limit"]} if "limit" in kw else {}))
                for line in buf.getvalue().strip().split("\n")[-6:]:
                    record(line)
            except Exception:
                pass
            return _real_print_exc(*a, **kw)

        _tb.print_exc = _print_exc
    except Exception:
        pass

    previous = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            import traceback
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            for line in text.strip().split("\n")[-6:]:
                record(line)
        except Exception:
            pass
        if previous:
            previous(exc_type, exc, tb)

    sys.excepthook = _hook