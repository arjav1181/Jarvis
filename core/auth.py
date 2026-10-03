"""core/auth.py — one user, one door, properly locked.

The dashboard has been behind a six-digit PIN from the start, which is fine until
you put an AI on it that can send email, spend money and open your front door.
Then the login is the weakest part of the system, so this makes it the strongest.

Four things, in order of how much they matter:

  * **TOTP, no new dependency.** RFC 6238 is HMAC-SHA1 over a 30-second counter.
    `hmac` and `hashlib` are in the standard library, so there is nothing to
    install, nothing to audit, and nothing to break on a Space rebuild. The app
    shows a secret, the user's authenticator app shows six digits.
  * **Constant-time comparison everywhere.** A PIN check that returns early on
    the first wrong digit leaks the prefix one request at a time. `secrets.compare_digest`
    removes the whole class of bug.
  * **PIN strength as advice, not a wall.** Refusing a weak PIN locks people out
    of their own dashboard, which is worse. Instead: `strength()` returns a
    verdict, the dashboard shows it, and a weak PIN is allowed but marked.
  * **Sessions that actually expire.** Absolute lifetime and idle timeout, with
    the idle window sliding on activity. A token that lives forever on a phone
    that gets stolen is a session that outlives the threat.

Everything here is file-backed under `data/`, mode 0600, because the Space
filesystem is the only place this can live.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import threading
import time
from typing import Any, Optional

from core.data_paths import data_root

SESSION_IDLE = 12 * 3600          # 12h idle
SESSION_ABSOLUTE = 7 * 24 * 3600  # a week, no matter how active
TOTP_STEP = 30
TOTP_DIGITS = 6
RECOVERY_CODES = 10

#: PINs that are technically PINs and are not secrets
WEAK_PINS = {
    "000000", "111111", "222222", "333333", "444444", "555555", "666666",
    "777777", "888888", "999999", "123456", "654321", "112233", "121212",
    "123123", "123321", "000111", "696969", "100000", "101010", "258000",
    "159753", "085200", "1234", "0000", "1111", "9999", "admin", "jarvis",
}

_lock = threading.RLock()
_path = None
_cache: Optional[dict] = None


def _file():
    global _path
    if _path is None:
        _path = data_root() / "auth.json"
    return _path


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            d = json.loads(_file().read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d.setdefault("pin_hash", "")
        d.setdefault("totp_secret", "")
        d.setdefault("totp_on", False)
        d.setdefault("recovery", [])       # [{code_hash, used_at}]
        d.setdefault("events", [])
        _cache = d
        return d


def _save() -> None:
    with _lock:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass


def reset() -> None:
    global _cache, _path
    with _lock:
        _cache = None
        _path = None


# ── PIN ──────────────────────────────────────────────────────────────────────

def _hash(pin: str, salt: str) -> str:
    """PBKDF2 rather than a plain hash: a stolen file must not be crackable with
    a GPU, and the PIN space is small enough that a fast hash would fall."""
    return hashlib.pbkdf2_hmac(
        "sha256", str(pin).encode(), bytes.fromhex(salt), 200_000).hex()


def _salt() -> str:
    return secrets.token_hex(16)


def strength(pin: str) -> dict:
    """A verdict, not a wall. 6 digits is the floor because the UI types 6."""
    p = str(pin or "").strip()
    reasons = []
    if len(p) < 6:
        reasons.append("shorter than 6 characters")
    if p.lower() in WEAK_PINS:
        reasons.append("one of the most-guessed PINs in the world")
    if p.isdigit() and len(set(p)) <= 3:
        reasons.append("too few distinct digits")
    if re_seq(p):
        reasons.append("a run, like 123456")
    bits = _entropy(p)
    if bits < 14:
        reasons.append("too easy to guess")
    score = min(4, max(0, int(bits / 8)))
    label = ("too weak" if reasons else
             "weak" if score < 2 else "okay" if score < 3 else "strong")
    return {"bits": round(bits, 1), "score": score, "label": label,
            "ok": not reasons, "reasons": reasons}


def re_seq(p: str) -> bool:
    s = str(p or "")
    if len(s) < 3:
        return False
    step = 1 if len(s) > 1 and s[1] >= s[0] else -1
    return all(int(s[i + 1]) - int(s[i]) == step for i in range(len(s) - 1)) \
        if s.isdigit() else False


def _entropy(p: str) -> float:
    """Rough Shannon-ish estimate over the character classes actually used,
    discounted for repetition. Not cryptography, just advice."""
    p = str(p or "")
    if not p:
        return 0.0
    pool = 0
    if any(c.isdigit() for c in p):
        pool += 10
    if any(c.islower() for c in p):
        pool += 26
    if any(c.isupper() for c in p):
        pool += 26
    if any(not c.isalnum() for c in p):
        pool += 33
    uniq = len(set(p))
    # a PIN typed on a keypad is not a passphrase
    return round(min(len(p) * (pool ** 0.5 if pool else 1), len(p) * 4.0)
                 * (0.5 + 0.5 * uniq / max(1, len(p))), 1)


def set_pin(pin: str) -> dict:
    p = str(pin or "").strip()
    if not p:
        raise ValueError("the PIN cannot be empty")
    v = strength(p)
    with _lock:
        d = _load()
        s = _salt()
        d["pin_hash"] = _hash(p, s)
        d["pin_salt"] = s
        d.setdefault("sessions", {})
        _save()
        _event("pin_changed", ok=True,
               detail=f"strength={v['label']}" + ("" if v["ok"] else
                                                  " (weak, allowed)"))
    return {"ok": True, "strength": v}


def pin_set() -> bool:
    return bool(_load().get("pin_hash"))


def check_pin(pin: str) -> bool:
    d = _load()
    if not d.get("pin_hash"):
        return False
    got = _hash(str(pin or "").strip(), d.get("pin_salt", ""))
    ok = hmac.compare_digest(got, d["pin_hash"])
    if not ok:
        _event("pin_failed", ok=False)
    return ok


# ── TOTP ─────────────────────────────────────────────────────────────────────

def _b32(secret: str) -> bytes:
    pad = "=" * (-len(secret) % 8)
    return base64.b32decode((secret + pad).upper(), casefold=True)


def totp_code(secret: str, at: float | None = None) -> str:
    """The six digits for this moment. Exposed so the tests can prove the
    algorithm against the RFC's own test vectors."""
    counter = int((at if at is not None else time.time()) // TOTP_STEP)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(_b32(secret), msg, hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def totp_enroll() -> dict:
    """Generate a secret and recovery codes. The secret is returned ONCE, in
    clear, because that is the only moment the user can write it down."""
    secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    with _lock:
        d = _load()
        d["totp_secret"] = secret
        d["totp_on"] = True
        stored, plain = [], []
        for _ in range(RECOVERY_CODES):
            raw = f"{secrets.token_hex(2)}-{secrets.token_hex(2)}"
            plain.append(raw)
            salt = _salt()          # one salt, used for the hash AND stored,
            stored.append({"code_hash": _hash(raw, salt),   # or it can never
                           "salt": salt, "used_at": None})  # verify again
        d["recovery"] = stored
        _save()
        _event("totp_enrolled", ok=True)
    return {"secret": secret, "recovery_codes": plain,
            "issuer": "JARVIS", "digits": TOTP_DIGITS, "step": TOTP_STEP,
            "url": f"otpauth://totp/JARVIS:Jarvis?secret={secret}"
                   f"&issuer=JARVIS&digits={TOTP_DIGITS}&period={TOTP_STEP}",
            "note": "store the recovery codes somewhere that is not this phone"}


def totp_on() -> bool:
    return bool(_load().get("totp_on") and _load().get("totp_secret"))


def totp_off() -> dict:
    with _lock:
        d = _load()
        d["totp_on"] = False
        d["totp_secret"] = ""
        d["recovery"] = []
        _save()
        _event("totp_disabled", ok=True)
    return {"totp_on": False}


def verify_totp(code: str, *, window: int = 1) -> bool:
    """±1 step of drift, which is the difference between 'my watch is 20s out'
    and 'two-factor is broken'."""
    d = _load()
    if not totp_on():
        return True
    raw = str(code or "").strip()
    c = re.sub(r"\D", "", raw)
    now = time.time()
    if len(c) == TOTP_DIGITS:
        for skew in range(-window, window + 1):
            if hmac.compare_digest(
                    totp_code(d["totp_secret"], now + skew * TOTP_STEP), c):
                _event("totp_ok", ok=True)
                return True
    # a recovery code, once — hashed exactly as it was typed, hyphens and all
    for r in d.get("recovery") or []:
        if r.get("used_at"):
            continue
        if hmac.compare_digest(_hash(raw, r.get("salt", "")),
                               r.get("code_hash", "")):
            r["used_at"] = time.time()
            _save()
            _event("recovery_used", ok=True)
            return True
    _event("totp_failed", ok=False)
    return False


# ── sessions ─────────────────────────────────────────────────────────────────

def new_session(*, label: str = "user", agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        d = _load()
        d.setdefault("sessions", {})
        now = time.time()
        d["sessions"][token] = {"born": now, "seen": now, "label": str(label)[:40],
                                "agent": str(agent)[:60]}
        # prune on create so the file cannot grow without bound
        for k, v in list(d["sessions"].items()):
            if now - v.get("seen", 0) > SESSION_IDLE or \
               now - v.get("born", 0) > SESSION_ABSOLUTE:
                d["sessions"].pop(k, None)
        _save()
    return token


def check_session(token: str, *, touch: bool = True) -> dict:
    """Validate a session and, by default, slide its idle window. Returns a
    verdict dict rather than a bool so the caller can say *why*."""
    t = str(token or "").strip()
    with _lock:
        d = _load()
        s = (d.get("sessions") or {}).get(t)
        if not s:
            return {"ok": False, "why": "unknown session"}
        now = time.time()
        if now - s.get("born", 0) > SESSION_ABSOLUTE:
            d["sessions"].pop(t, None)
            _save()
            return {"ok": False, "why": "expired — sign in again"}
        idle = now - s.get("seen", 0)
        if idle > SESSION_IDLE:
            d["sessions"].pop(t, None)
            _save()
            _event("session_idle_out", ok=False,
                   detail=f"{int(idle / 60)} min idle")
            return {"ok": False, "why": "expired after inactivity"}
        if touch:
            s["seen"] = now
            _save()
        return {"ok": True, "since": s.get("born", now), "label": s.get("label", "")}


def end_session(token: str) -> dict:
    with _lock:
        d = _load()
        had = (d.get("sessions") or {}).pop(str(token or ""), None) is not None
        _save()
    if had:
        _event("signed_out", ok=True)
    return {"ok": had}


def sessions() -> list[dict]:
    now = time.time()
    with _lock:
        rows = [{"token": k[:8] + "…", "label": v.get("label", ""),
                 "born": v.get("born", 0), "seen": v.get("seen", 0),
                 "idle_min": int((now - v.get("seen", now)) / 60)}
                for k, v in (load_sessions() or {}).items()]
    rows.sort(key=lambda r: r["seen"], reverse=True)
    return rows


def load_sessions() -> dict:
    return dict(_load().get("sessions") or {})


def sign_out_all() -> int:
    with _lock:
        d = _load()
        n = len(d.get("sessions") or {})
        d["sessions"] = {}
        _save()
    _event("signed_out_everywhere", ok=True, detail=f"{n} session(s)")
    return n


# ── the ledger ───────────────────────────────────────────────────────────────

def _event(kind: str, *, ok: bool = True, detail: str = "") -> None:
    with _lock:
        d = _load()
        d.setdefault("events", [])
        d["events"].append({"kind": kind, "ok": bool(ok),
                            "detail": str(detail or "")[:160], "at": time.time()})
        d["events"] = d["events"][-200:]
        _save()


def events(limit: int = 40) -> list[dict]:
    with _lock:
        return list(reversed(_load().get("events") or []))[:max(1, min(int(limit or 40), 200))]


def status() -> dict:
    d = _load()
    s = _load().get("sessions") or {}
    return {
        "pin_set": bool(d.get("pin_hash")),
        "totp_on": totp_on(),
        "recovery_left": sum(1 for r in d.get("recovery") or [] if not r.get("used_at")),
        "sessions": len(s),
        "idle_hours": SESSION_IDLE // 3600,
        "absolute_days": SESSION_ABSOLUTE // 86400,
        "recent_failures": sum(1 for e in (d.get("events") or [])[-200:]
                               if not e.get("ok")),
    }
