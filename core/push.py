"""
core/push.py — reach the phone, and let the phone approve things.

WHY THIS EXISTS
    The dashboard works, but a phone in a pocket does not. Two things make
    the phone a real control surface instead of a small screen:

      1. A push alert when something needs a human — a task finished, an
         approval is waiting, a scheduled job failed. The dashboard cannot
         shout; a notification can, and it survives the tab being closed,
         which matters because the interesting events are exactly the ones
         that happen while the tab is not open.

      2. One-tap approve/reject. Tapping a notification action resolves the
         same core/confirm.py gate the on-screen card resolves — the run
         callable only ever comes from the interface, so a phone tap is a
         human decision with the same standing as a button press.

THE AUTH PROBLEM, AND WHY IT IS SOLVED THIS WAY
    A service worker cannot hold the dashboard's bearer token: the token
    lives in sessionStorage on a page, and the worker may outlive it. So
    every push action carries its own short-lived, signed token instead:

        token = b64url(payload_json) + "." + b64url(HMAC-SHA256(payload, secret))

    The secret is generated once per install and stored outside the repo.
    /api/push/respond accepts *no* other credential and validates only this
    signature plus a 15-minute age limit. A stolen token therefore buys one
    decision on one action, and expires on its own.

    Without VAPID, browsers reject a subscription outright, so keys are
    generated here once (P-256, via `cryptography`) and reused. pywebpush is
    an optional dependency: without it, every function here degrades to
    "no subscribers notified" instead of raising, so a Space that failed to
    install it still boots and the dashboard still works.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

MAX_SUBS = 40
ACTION_TTL_S = 15 * 60.0

# Populated on first send so `notify()` can report honestly in the UI.
_STATE: dict[str, Any] = {
    "enabled": False,
    "reason": "not initialised",
    "sent": 0,
    "failed": 0,
    "pruned": 0,
    "last_error": "",
    "last_at": 0.0,
}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


# ── Key material ─────────────────────────────────────────────────────────────

def _vapid_path() -> Path:
    return data_root() / "push_vapid.json"


def _secret_path() -> Path:
    return data_root() / "push_action_secret"


def _subs_path() -> Path:
    return data_root() / "push_subscriptions.json"


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, p)


def get_vapid_keys() -> dict:
    """Return {'publicKey': url-b64, 'privateKey': pem-or-pem-file-marker}.

    Generated once per install. The private key never leaves the server.
    """
    data = _read_json(_vapid_path(), {})
    if data.get("publicKey") and data.get("privatePKCS8"):
        return data
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        key = ec.generate_private_key(ec.SECP256R1())
        private_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption())
        public_b64 = _b64e(key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint))
        data = {"publicKey": public_b64,
                "privatePKCS8": private_pem.decode("ascii"),
                "created": time.time()}
        _write_json(_vapid_path(), data)
        return data
    except Exception as e:
        _STATE["reason"] = f"key generation failed: {type(e).__name__}"
        return {"publicKey": "", "privateKey": "", "error": str(e)[:200]}


def _action_secret() -> bytes:
    p = _secret_path()
    try:
        if p.exists():
            b = p.read_bytes()
            if len(b) >= 32:
                return b
        b = os.urandom(32)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
        os.chmod(p, 0o600)
        return b
    except Exception:
        return b"jarvis-fallback-action-secret-do-not-reuse"


# ── Action tokens ────────────────────────────────────────────────────────────

def sign_action(payload: dict) -> str:
    """Sign a short-lived action token the phone can present unauthenticated."""
    body = dict(payload)
    body.setdefault("iat", int(time.time()))
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(_action_secret(), raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(sig)}"


def verify_action(token: str, max_age: float = ACTION_TTL_S) -> Optional[dict]:
    """Return the payload when the signature is ours and the token is fresh."""
    try:
        body_b64, _, sig_b64 = str(token or "").partition(".")
        if not body_b64 or not sig_b64:
            return None
        raw = _b64d(body_b64)
        sig = _b64d(sig_b64)
        expect = hmac.new(_action_secret(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(raw.decode())
        iat = float(payload.get("iat", 0))
        if not iat or time.time() - iat > max_age or iat - time.time() > 60:
            return None
        return payload
    except Exception:
        return None


# ── Subscriptions ────────────────────────────────────────────────────────────

def list_subs() -> list[dict]:
    data = _read_json(_subs_path(), {})
    subs = data.get("subs") if isinstance(data, dict) else None
    return [s for s in (subs or []) if isinstance(s, dict) and s.get("endpoint")]


def add_sub(sub: dict, ua: str = "") -> dict:
    sub = {k: v for k, v in (sub or {}).items() if k in ("endpoint", "keys")}
    if not sub.get("endpoint") or not isinstance(sub.get("keys"), dict):
        raise ValueError("subscription needs endpoint + keys.p256dh + keys.auth")
    subs = list_subs()
    rec = {
        "id": "s-" + uuid.uuid4().hex[:8],
        "endpoint": sub["endpoint"],
        "keys": sub["keys"],
        "ua": str(ua or "")[:120],
        "created": time.time(),
        "ok": 0,
        "fails": 0,
        "last_error": "",
    }
    for i, old in enumerate(subs):
        if old.get("endpoint") == rec["endpoint"]:
            rec["id"] = old.get("id", rec["id"])
            rec["ok"] = int(old.get("ok", 0))
            subs[i] = rec
            break
    else:
        subs.append(rec)
    subs = sorted(subs, key=lambda s: s.get("created", 0), reverse=True)[:MAX_SUBS]
    _write_json(_subs_path(), {"subs": subs})
    return {"id": rec["id"], "subs": len(subs)}


def remove_sub(endpoint: str) -> dict:
    subs = list_subs()
    keep = [s for s in subs if s.get("endpoint") != endpoint]
    _write_json(_subs_path(), {"subs": keep})
    return {"removed": len(subs) - len(keep), "subs": len(keep)}


def sub_count() -> int:
    return len(list_subs())


# ── Sending ──────────────────────────────────────────────────────────────────

def _pywebpush():
    try:
        import pywebpush  # type: ignore
        return pywebpush
    except Exception:
        return None


def status() -> dict:
    st = dict(_STATE)
    st["subs"] = sub_count()
    st["has_vapid"] = bool(get_vapid_keys().get("publicKey"))
    st["pywebpush"] = _pywebpush() is not None
    return st


def _send_one(pyw, vapid: dict, sub: dict, payload: dict) -> tuple[bool, str]:
    try:
        pyw.webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
            vapid_private_key=vapid["privatePKCS8"],
            vapid_claims={"sub": f"mailto:jarvis@{_host_from(sub.get('endpoint'))}"},
            data=json.dumps(payload),
            ttl=3600,
        )
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]


def _host_from(endpoint: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(endpoint).hostname or "localhost")
    except Exception:
        return "localhost"


def notify(title: str, body: str = "", *, data: Optional[dict] = None,
           tag: str = "", url: str = "/", actions: Optional[list] = None,
           require_interaction: bool = False) -> dict:
    """Send one notification to every live subscription.

    Dead endpoints (404/410) are pruned: browsers retire subscriptions when
    an app is uninstalled, and a growing tombstone list would slow every
    send down for no benefit.
    """
    subs = list_subs()
    result = {"sent": 0, "failed": 0, "pruned": 0, "subs": len(subs)}
    if not subs:
        _STATE.update(sent=0, failed=0, last_at=time.time(),
                      last_error="", reason="no subscribers")
        return result
    pyw = _pywebpush()
    if pyw is None:
        _STATE.update(reason="pywebpush not installed",
                      last_error="pip install pywebpush", last_at=time.time())
        result["error"] = "pywebpush not installed"
        return result
    vapid = get_vapid_keys()
    if not vapid.get("privatePKCS8"):
        result["error"] = "no VAPID keys"
        return result

    payload = {
        "title": str(title)[:120],
        "body": str(body)[:300],
        "tag": tag or (f"jarvis-{uuid.uuid4().hex[:6]}"),
        "url": url or "/",
        "at": time.time(),
    }
    if data:
        payload["data"] = data
    if actions:
        payload["actions"] = actions

    alive: list[dict] = []
    for sub in subs:
        ok, err = _send_one(pyw, vapid, sub, payload)
        sub["ok"] = int(sub.get("ok", 0)) + (1 if ok else 0)
        if ok:
            result["sent"] += 1
            sub["fails"] = 0
            sub["last_error"] = ""
            alive.append(sub)
            continue
        sub["fails"] = int(sub.get("fails", 0)) + 1
        sub["last_error"] = err
        dead = ("404" in err) or ("410" in err) or ("Gone" in err)
        if dead or sub["fails"] >= 5:
            result["pruned"] += 1
            continue
        alive.append(sub)
        result["failed"] += 1
    _write_json(_subs_path(), {"subs": alive})
    _STATE.update(enabled=True, reason="ok", sent=result["sent"],
                  failed=result["failed"], pruned=result["pruned"],
                  last_error="", last_at=time.time())
    return result


# ── The two alerts JARVIS actually sends ─────────────────────────────────────

def notify_approval(title: str, detail: str, *, url: str = "/",
                    kind: str = "confirm", extra: Optional[dict] = None) -> dict:
    """Push an approval with ACCEPT / REJECT buttons that resolve the gate.

    Both tokens are minted now: the phone may tap ACCEPT twenty seconds
    later, and a token minted at tap time could not be trusted by the
    worker that has no secret.
    """
    data = {
        "url": url,
        "kind": kind,
        "accept_token": sign_action({"kind": kind, "accept": True}),
        "reject_token": sign_action({"kind": kind, "accept": False}),
    }
    if extra:
        data.update(extra)
    return notify(
        f"Approval needed — {title}"[:120],
        (detail or "")[:200],
        data=data,
        tag="jarvis-approval",
        url=url,
        actions=[{"action": "accept", "title": "Accept"},
                 {"action": "reject", "title": "Reject"}],
        require_interaction=True,
    )


def notify_task(title: str, tid: str = "", ok: bool = True) -> dict:
    icon = "done" if ok else "failed"
    return notify(f"Task {icon} — {title}"[:120],
                  f"{tid} · open the Tasks panel for the diff"
                  if ok else f"{tid} · see the log",
                  data={"url": f"/?task={tid}"},
                  tag=f"jarvis-task-{tid}" if tid else "jarvis-task",
                  url=f"/?task={tid}",
                  require_interaction=not ok)
