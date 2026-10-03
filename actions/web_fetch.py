#web_fetch.py
"""Fetch a URL and return readable page text (for reviewing sites, GitHub, docs)."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

_MAX_CHARS = 4000
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "iframe", "object", "embed"}
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            t = data.strip()
            if t:
                self._parts.append(t)


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    return url


def web_fetch(url: str, player=None) -> str:
    """GET a URL, extract title + body text, return a readable excerpt."""
    target = _normalize_url(url)
    if not target:
        return "No URL provided."
    try:
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https"):
            return f"Unsupported scheme: {parsed.scheme}"
    except Exception:
        return f"Invalid URL: {url}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; JARVIS/1.0; +https://github.com/abc1181/Jaarvis)"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(target, headers=headers, timeout=15, allow_redirects=True)
    except Exception as e:
        return f"Fetch failed for {target}: {e}"

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code} for {target}"
    if "html" not in ctype and "xhtml" not in ctype and "text/" not in ctype:
        return (
            f"Fetched {target}: status {resp.status_code}, "
            f"content-type {ctype or 'unknown'} (non-text)."
        )

    html = resp.text or ""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = title_m.group(1).strip() if title_m else ""
    title = re.sub(r"\s+", " ", title)[:200]

    try:
        parser = _TextExtractor()
        parser.feed(html)
        body = " ".join(parser._parts)
    except Exception:
        body = re.sub(r"<[^>]+>", " ", html)
    body = re.sub(r"\s+", " ", body).strip()

    if not body:
        body = "(no readable text on page)"

    excerpt = body[:_MAX_CHARS]
    if len(body) > _MAX_CHARS:
        excerpt += " …[truncated]"

    head = f"URL: {resp.url}\nStatus: {resp.status_code}"
    if title:
        head += f"\nTitle: {title}"
    return f"{head}\n\n{excerpt}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "web_fetch",
    "description": (
        "Fetches a specific URL and returns its readable page text (title + body). "
        "Use for reviewing a website, GitHub profile/repo page, blog post, docs, "
        "or any 'look at this URL' task. Prefer this over browser_control go_to "
        "when you only need page content, not to click or interact."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {
                "type": "STRING",
                "description": "Full URL to fetch (https://… or bare domain)",
            }
        },
        "required": ["url"],
    },
    "handler": web_fetch,
}
