"""
core/display.py — the surface JARVIS can put anything on.

THE IDEA
    Ask for a wiring diagram, a live chart, a dashboard mock, a game, a map, a
    web page, a photo — and the assistant puts it on the screen instead of
    describing it. The video that started this was a wiring diagram; the point
    is not wiring diagrams, it is that the display surface is general.

WHY THE MODEL WRITES THE MARKUP
    A hand-rolled renderer can only draw what its author imagined. Asking for
    an arbitrary thing means the markup has to come from the model, so the
    safety question is not "is model HTML dangerous" — it is "where does it
    run". The answer: inside a sandboxed iframe with no same-origin access, so
    the page can run scripts, draw on a canvas, fetch the internet, and play
    with the DOM, and still cannot read this dashboard's sessionStorage, send
    authenticated API calls, or navigate the window it lives in. Everything it
    needs from us it gets through one narrow, validated channel.

THE KINDS
    html     model-written markup + canvas/svg, sandboxed
    url      a real page, framed (or an image, sniffed)
    chart    structured data, drawn by US as SVG — no model layout
    image    a generated or fetched picture
    text     plain text, styled by us

    `chart` exists on purpose: when the model has DATA rather than a design,
    it is better off emitting values than pixels, and the difference is that a
    chart survives a dark theme, a phone screen, and a re-render.

WHAT IS STORED
    One JSON document per artifact under data_root()/display/, so the display
    survives a Space rebuild and the user can reopen yesterday's dashboard.
    History is capped and pruned by age — a display surface that grows
    forever is a disk leak with a user interface.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

KINDS = ("html", "url", "chart", "image", "text")
MAX_HTML = 400_000          # ~400 KB of markup; a page, not a payload
MAX_TEXT = 20_000
KEEP = 40
MAX_AGE_DAYS = 30

# The iframe the dashboard uses. Note what is NOT here: allow-same-origin.
# Without it the document gets an opaque origin, so it cannot touch our
# storage, cookies, or authenticated endpoints — that is the whole sandbox.
SANDBOX_ATTR = "allow-scripts allow-forms allow-modals allow-popups allow-downloads"

_SCRIPT_SRC = re.compile(r"<script[^>]+src=[\"'][^\"']+[\"'][^>]*>", re.I)


def _dir() -> Path:
    p = data_root() / "display"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(aid: str) -> Path:
    return _dir() / f"{_safe_id(aid)}.json"


def _safe_id(aid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(aid or ""))[:48] or "x"


# ── sandboxing ───────────────────────────────────────────────────────────────

def sanitize_html(html: str) -> str:
    """Keep the page's own scripts; refuse the two shapes that can escape.

    A sandboxed frame already blocks same-origin access, but a <base> tag or a
    top-navigation attempt is still worth refusing outright, and a document
    that opens with a meta refresh can be used to bounce the user somewhere
    else. Everything else is the model's own page and stays.
    """
    out = str(html or "")
    if not out.strip():
        return ""
    out = re.sub(r"<base[^>]*>", "", out, flags=re.I)
    out = re.sub(r"<meta[^>]+http-equiv=[\"']?refresh[^>]*>", "", out, flags=re.I)
    out = re.sub(r"""<a[^>]+target=[\"']_blank[\"'][^>]*>""",
                 lambda m: m.group(0).replace('target="_blank"',
                                              'target="_blank" rel="noopener"'),
                 out, flags=re.I)
    return out


def wrap_html(title: str, body: str) -> str:
    """A self-contained page, so the model never has to think about <head>."""
    from core import theme as _theme
    page = _theme.inject(body=body)
    return page.replace("<title>", f"<title>{_esc(title)}</title>", 1) \
        if "<title>" in page else page


def _esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ── store ────────────────────────────────────────────────────────────────────

def save(kind: str, *, title: str = "", html: str = "", url: str = "",
         text: str = "", spec: Optional[dict] = None,
         source: str = "model", warning: str = "", pinned: bool = False,
         meta: Optional[dict] = None) -> dict:
    """Store one artifact and return its record. Raises ValueError on junk."""
    kind = str(kind or "text").lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    aid = "d-" + uuid.uuid4().hex[:8]
    now = time.time()
    rec: dict[str, Any] = {
        "id": aid, "kind": kind, "title": str(title or kind)[:120],
        "source": source, "created": now, "warning": str(warning or "")[:200],
        "pinned": bool(pinned), "meta": meta or {},
    }
    if kind == "html":
        from core import theme as _theme
        page = _theme.apply_to_document(sanitize_html(html or ""))
        if not page.strip():
            raise ValueError("no html to show")
        if len(page) > MAX_HTML:
            page = page[:MAX_HTML]
        rec["bytes"] = len(page)
        rec["html"] = page
    elif kind == "url":
        u = str(url or "").strip()
        if not re.match(r"^https?://", u):
            raise ValueError("url must start with http:// or https://")
        rec["url"] = u[:2000]
        rec["embed"] = bool(meta and meta.get("embed", True))
    elif kind == "chart":
        if not isinstance(spec, dict) or not spec:
            raise ValueError("a chart needs a spec")
        rec["spec"] = spec
    elif kind == "image":
        src = str(url or "").strip()
        if not (src.startswith("data:image/") or re.match(r"^https?://", src)):
            raise ValueError("image needs a data: or http(s) url")
        rec["url"] = src[:2_000_000]
    else:
        t = str(text or "")[:MAX_TEXT]
        if not t.strip():
            raise ValueError("nothing to show")
        rec["text"] = t

    _path(aid).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    _prune()
    return {k: v for k, v in rec.items() if k not in ("html", "spec", "url")} | {
        "has_html": bool(rec.get("html")), "has_spec": bool(rec.get("spec"))}


def get(aid: str) -> Optional[dict]:
    p = _path(aid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def html_of(aid: str) -> str:
    rec = get(aid) or {}
    if rec.get("kind") != "html":
        return ""
    return rec.get("html") or ""


def listing(limit: int = 20) -> list[dict]:
    out = []
    for p in sorted(_dir().glob("*.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({k: v for k, v in r.items()
                    if k not in ("html", "spec", "text", "url")})
    out.sort(key=lambda r: (not r.get("pinned"), -(r.get("created") or 0)))
    return out[:max(1, min(int(limit or 20), KEEP))]


def delete(aid: str) -> bool:
    p = _path(aid)
    if p.exists():
        p.unlink()
        return True
    return False


def set_pinned(aid: str, pinned: bool = True) -> Optional[dict]:
    rec = get(aid)
    if not rec:
        return None
    rec["pinned"] = bool(pinned)
    _path(aid).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return {k: v for k, v in rec.items() if k not in ("html", "spec", "text", "url")}


def _prune() -> None:
    """Bound the directory: pinned survive, then newest, then a 30-day cap."""
    rows = []
    for p in _dir().glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            rows.append((p, float(r.get("created") or 0), bool(r.get("pinned"))))
        except Exception:
            continue
    rows.sort(key=lambda t: t[1], reverse=True)
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    for i, (p, created, pinned) in enumerate(rows):
        # Pinned first, and unconditionally: "keep this on my screen" has to
        # mean it survives however many other things I show afterwards. The
        # order of these two checks is the whole point — with the index test
        # first, anything old-but-pinned was deleted at position KEEP.
        if pinned:
            continue
        if i < KEEP and created > cutoff:
            continue
        try:
            p.unlink()
        except OSError:
            pass


# ── describe (for the model) ─────────────────────────────────────────────────

def describe(rec: dict) -> str:
    """One honest sentence — the model speaks this, the screen shows the rest."""
    kind = rec.get("kind")
    bits = [f"Showing '{rec.get('title') or kind}'"]
    if rec.get("warning"):
        bits.append(f"with a warning: {rec['warning']}")
    if kind == "html":
        bits.append(f"({rec.get('bytes', 0) // 1000} KB of markup, interactive)")
    elif kind == "chart":
        bits.append("(drawn from the data you gave me)")
    elif kind == "url":
        bits.append(f"({rec.get('url', '')[:70]})")
    return " ".join(bits) + "."
