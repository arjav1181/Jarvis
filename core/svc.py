"""Shared REST plumbing for the token-authenticated services (GitHub, Vercel,
Hugging Face).

This exists so that the three connectors are thin and so that every outbound
call inherits the same three properties:

  1. **No secret ever reaches a log line or a model-visible string.** Tokens come
     from `api_keys.json` and are attached to a header, never returned.
  2. **Every call has a timeout.** A connector that can hang forever is a
     connector that can hang the voice loop, and a hung voice loop is an
     assistant that stops answering.
  3. **Every call is injectable.** `fetcher` is the seam the tests use, so the
     connector suites never touch the network — the same convention
     `core/mail.py` uses for SMTP and IMAP.

`urllib` only, matching `core/maps.py` and `core/gcal.py`. No new dependency.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

UA = "JARVIS/1.0"
DEFAULT_TIMEOUT = 15


class ServiceError(RuntimeError):
    """A remote call failed in a way the caller should explain to a human.

    `status` is the HTTP code, or 0 for a network-level failure. The message is
    written to be shown to the user directly — it names the fix, not the stack.
    """

    def __init__(self, message: str, *, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def _keys() -> dict:
    from core.data_paths import config_dir
    p = config_dir() / "api_keys.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_token(*names: str) -> str:
    """First non-empty secret among `names`. Never logged, never returned."""
    k = _keys()
    for n in names:
        v = str(k.get(n) or "").strip()
        if v:
            return v
    return ""


def set_token(name: str, value: str) -> None:
    k = _keys()
    if value:
        k[name] = str(value).strip()
    else:
        k.pop(name, None)
    from core.data_paths import config_dir
    p = config_dir() / "api_keys.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(k, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        import os
        os.chmod(p, 0o600)
    except OSError:
        pass


def request(method: str, url: str, *, headers: Optional[dict] = None,
            body: Any = None, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """One HTTPS call. Returns decoded JSON, or the raw text when it is not JSON.

    Raises `ServiceError` with a message written for the user. A 401 in
    particular says which credential is wrong, because "unauthorized" with no
    pointer wastes the user's time.
    """
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code in (401, 403):
            hint = "The token is missing, expired, or lacks that scope."
        elif e.code == 404:
            hint = "Not found, or the token cannot see it."
        elif e.code == 429:
            hint = "Rate limited by the provider — wait a moment."
        else:
            hint = f"The provider returned {e.code}."
        raise ServiceError(hint, status=e.code, detail=detail) from None
    except urllib.error.URLError as e:
        raise ServiceError(f"Could not reach the service: {e.reason}",
                           detail=type(e).__name__) from None
    except Exception as e:
        raise ServiceError(f"{type(e).__name__}: {e}"[:180]) from None
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"_raw": raw}


def qs(params: dict) -> str:
    clean = {k: v for k, v in (params or {}).items()
             if v is not None and v != ""}
    return ("?" + urllib.parse.urlencode(clean)) if clean else ""
