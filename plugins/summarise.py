"""
Summarise anything, properly.

Give it a URL or a file and it returns what the thing actually says, in
prose, sized to what you asked for. The point is not shorter — it is
stripping: most of the web is navigation, cookie banners, "related articles"
and ad slots, and a summariser that keeps those is worse than useless because
it looks like it worked.

So: extract the readable part, drop the furniture, and say how much of the
original survived. If the extraction is poor, it says so and gives you the
raw text rather than a confident summary of nothing.

    action=page:   summarise a URL.
    action=file:   summarise a file in the workspace.
    action=key:    just the extraction, no summary — for when you want to
                   check whether the extraction was any good first.

Read-only. It fetches and it reads; it never writes, sends or deletes, so the
policy gate leaves it free.

The honesty rules in here are the whole design. A summary that does not say
what it left out is a way of being lied to politely.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

#: Things that are never content. Ordered so the greedy ones run first.
_JUNK = (
    (re.compile(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>"), " "),
    (re.compile(r"(?is)<!--.*?-->"), " "),
    (re.compile(r"(?is)<nav\b.*?</nav>"), " "),
    (re.compile(r"(?is)<(header|footer|aside)\b.*?</\1>"), " "),
    (re.compile(r"(?is)<form\b.*?</form>"), " "),
    (re.compile(r"(?is)<div[^>]*(class|id)=[^>]*(cookie|consent|advert|"
                r"promo|social|share|related|recommend|newsletter|"
                r"sidebar|comment)[^>]*>.*?</div>"), " "),
    (re.compile(r"(?is)<!--.*?-->"), " "),
)

_SKIP_PARA = re.compile(
    r"^(cookie|accept all|we use cookies|subscribe|sign in|log in|"
    r"advertisement|share this|read more|related|menu|home|contact|"
    r"privacy policy|terms of service|all rights reserved|©|copyright|"
    r"skip to content|toggle navigation|search\s*$)",
    re.I)


def _strip_html(html: str) -> str:
    out = html
    for pattern, repl in _JUNK:
        out = pattern.sub(repl, out)
    # keep the title and the meta description, which are often the best
    # summary a page has of itself
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if m:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    desc = ""
    m = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+'
                  r'content=["\'](.*?)["\']', html)
    if m:
        desc = re.sub(r"\s+", " ", m.group(1)).strip()
    out = re.sub(r"(?is)<br\s*/?>", "\n", out)
    out = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", out)
    out = re.sub(r"(?is)<[^>]+>", " ", out)
    out = (out.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">")
              .replace("&quot;", '"').replace("&#39;", "'"))
    lines = []
    for ln in out.splitlines():
        ln = re.sub(r"[ \t ]+", " ", ln).strip()
        if not ln or _SKIP_PARA.match(ln):
            continue
        lines.append(ln)
    body = "\n".join(lines)
    head = (f"{title}\n{desc}\n" if title or desc else "")
    return head + body


def _fetch(url: str, timeout: float = 20.0) -> tuple[str, str]:
    import httpx
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "JARVIS/1.0 (summarise)"} )
    ctype = str(r.headers.get("content-type") or "").lower()
    return (r.text if ("html" in ctype or not ctype)
            else r.text), ctype


def _squeeze(text: str, sentences: int) -> str:
    """A rough but honest shortener: the opening, which in an article is
    usually the lede, plus the longest few sentences after it."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    if len(parts) <= sentences:
        return text
    lead = parts[0]
    rest = sorted(parts[1:sentences * 3], key=len, reverse=True)[:max(0, sentences - 1)]
    keep = [lead] + sorted(rest)
    return " ".join(k for k in keep if k)[:4000]


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action") or "page").strip().lower()
    url = str(parameters.get("url") or "").strip()
    path = str(parameters.get("path") or "").strip()
    sentences = int(parameters.get("sentences") or 5)
    try:
        return _run(action, url, path, max(1, min(40, sentences)))
    except Exception as e:
        return f"I could not summarise that: {type(e).__name__}: {e}"[:200]


def _run(action: str, url: str, path: str, sentences: int) -> str:
    if action == "file":
        if not path:
            return "Which file?"
        from core import coder as _c
        ws = _c.ensure_ws(path)
        rel = str(path).lstrip("/")
        target = (ws / rel) if not Path(path).is_absolute() else Path(path)
        if not target.is_file():
            return f"No such file: {path}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Could not read it: {type(e).__name__}"
        return _wrap(f"{target.name}", text, text, sentences)

    if not url:
        return "Give me a URL."
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    raw, _ctype = _fetch(url)
    if not raw:
        return f"{url} returned nothing I could read."
    if "<" in raw[:2000]:
        text = _strip_html(raw)
        kind = "page"
    else:
        text = raw
        kind = "document"

    if action == "key":
        return (f"{url}\n{len(text)} characters survived extraction."
                f"\n\n{text[:1200]}")
    return _wrap(url, text, raw, sentences, kind=kind, original_len=len(raw))


def _wrap(title: str, text: str, original: str, sentences: int,
          kind: str = "page", original_len: Optional[int] = None) -> str:
    if not text.strip():
        return (f"{title}: I could not pull any readable text out of it. "
                f"That is usually a page that needs JavaScript, or a PDF.")
    kept = len(text)
    orig = original_len if original_len is not None else len(original)
    pct = int(kept * 100 / orig) if orig else 100
    summary = _squeeze(text, sentences)
    # A low survival rate is the signal that the extraction failed, and saying
    # so is more useful than a confident summary of the navigation menu.
    warning = ""
    if kind == "page" and pct < 4:
        warning = (f"\n\nOnly {pct}% of the page was real text, so treat this "
                   f"warily — it may be serving content to JavaScript.")
    return (f"{title} — {kept:,} characters of readable text "
            f"({pct}% of the {orig:,} fetched).\n\n{summary}{warning}")


PLUGIN = {
    "name": "summarise",
    "description": (
        "Strip a URL or a file down to what it actually says, in prose. Use "
        "this for 'summarise this link', 'what does this page say', 'TL;DR "
        "this', or any article, doc or long text the user pastes in. Strips "
        "navigation, cookie banners and ad slots, and reports how much "
        "survived — do NOT use it for a simple web_fetch, which is faster."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "page | file | key"},
            "url": {"type": "STRING", "description": "for page"},
            "path": {"type": "STRING", "description": "for file"},
            "sentences": {"type": "INTEGER",
                           "description": "how short; default 5"},
        },
        "required": [],
    },
}
