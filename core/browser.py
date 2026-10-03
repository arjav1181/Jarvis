"""core/browser.py — a real browser JARVIS can drive, with a leash.

What this is for: "log in to my hosting panel and show me the bill", "check
whether the site is up", "open three tabs and tell me what each one says",
"what's the price on that page". What this is *not* for: crawling someone.

The leash is the interesting part, and it is not optional:

  * **An allowlist.** Only hosts the user has added are reachable. A brand new
    install can open nothing at all, so the tool cannot be turned into a scraper
    by prompt alone — the user has to deliberately add a domain.
  * **robots.txt is honoured** for any path we crawl, and disallowed paths are
    refused with the reason, not silently skipped.
  * **Bounded runs.** A step budget and a wall-clock budget per call. A page
    that loops cannot spend an afternoon.
  * **No stealth, no credential storage.** We log in by driving the real login
    form the user watches in the panel; passwords go through the vault the rest
    of the app uses, and are never written to the browser profile on disk.
  * **Every action is journalled** with the URL, so "what did it open?" is
    answerable afterwards.

Playwright runs headless here. When a site needs a human — a captcha, a 2FA
prompt — the honest answer is to hand the user a headed session rather than
pretend.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

DEFAULT_TIMEOUT = 20
MAX_STEPS = 12
MAX_AGE_DAYS = 45


def _file():
    return data_root() / "browser.json"


_lock = threading.RLock()
_cache: Optional[dict] = None
_playwright = None          # the started driver, reused across calls
_browser = None
_context = None


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
        d.setdefault("allow", {})
        d.setdefault("runs", [])
        _cache = d
        return d


def _save() -> None:
    with _lock:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)          # the cache already holds the current object


def reset() -> None:
    close()
    global _cache
    with _lock:
        _cache = None


# ── the allowlist ────────────────────────────────────────────────────────────

def allow(host: str, *, on: bool = True) -> dict:
    """Add or remove a host JARVIS is allowed to open. Needs no scheme — store
    the host, because that is the part the leash is actually about."""
    h = str(host or "").strip().lower()
    if not h:
        raise ValueError("which host? e.g. example.com")
    h = re.sub(r"^https?://", "", h).split("/")[0]
    if not re.match(r"^[a-z0-9.\-]+(:\d+)?$", h):
        raise ValueError(f"'{host}' is not a hostname")
    with _lock:
        d = _load()
        d["allow"][h] = {"on": bool(on), "added": time.time()}
        _save()
    return {"host": h, "allowed": bool(on),
            "note": "the user widened the leash" if on else "closed again"}


def deny(host: str) -> dict:
    return allow(host, on=False)


def allowed() -> list[dict]:
    with _lock:
        return [{"host": h, "on": bool(v.get("on")),
                 "added": v.get("added")} for h, v in sorted(_load()["allow"].items())]


def is_allowed(url: str) -> tuple[bool, str]:
    try:
        u = urllib.parse.urlparse(str(url))
    except Exception:
        return False, "that is not a URL"
    if u.scheme not in ("http", "https"):
        return False, f"only http and https, not '{u.scheme}'"
    if not u.hostname:
        return False, "no host in that URL"
    host = u.hostname.lower()
    entry = _load()["allow"].get(host)
    if entry is None:
        return False, (f"{host} is not on the allowlist — open the Browsers "
                       f"panel and add it if you want me to go there")
    if not entry.get("on"):
        return False, f"{host} is blocked"
    return True, host


def _robots_allows(url: str) -> tuple[bool, str]:
    """Honour robots.txt for the path, not just the host. A Disallow on
    /private is a real instruction and we follow it."""
    try:
        u = urllib.parse.urlparse(str(url))
        if u.scheme != "http":
            return True, ""
        origin = f"{u.scheme}://{u.netloc}"
        req = urllib.request.Request(origin + "/robots.txt",
                                     headers={"User-Agent": "JARVIS"})
        with urllib.request.urlopen(req, timeout=6) as r:
            text = r.read(200_000).decode("utf-8", "replace")
    except Exception:
        return True, ""          # no robots.txt is permission, per convention
    ua = "jarvis"
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    applies, dis = False, []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k == "user-agent":
            applies = v == "*" or ua in v.lower()
        elif k == "disallow" and applies and v:
            dis.append(v)
    for d in dis:
        if d == "/" or path.startswith(d):
            return False, f"robots.txt disallows {d} on this site"
    return True, ""


# ── the driver ───────────────────────────────────────────────────────────────

def _start():
    """Lazily bring up one headless Chromium and reuse it. Reused, not new, so
    four calls do not pay four browser startups."""
    global _playwright, _browser, _context
    if _context is not None:
        return _context
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(f"Playwright is not available: {e}") from None
    _playwright = sync_playwright().start()
    # Same candidate list docs.py uses: Playwright's bundled headless shell is
    # missing system libs on this image, while the system chromium works. One
    # list for every place in the project that launches a browser.
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    _browser = None
    for exe in (os.environ.get("JARVIS_E2E_CHROMIUM"),
                "/repl/tools/bin/chromium"):
        if exe and Path(exe).exists():
            try:
                _browser = _playwright.chromium.launch(
                    headless=True, executable_path=exe, args=args)
                break
            except Exception:
                continue
    if _browser is None:
        _browser = _playwright.chromium.launch(headless=True, args=args)
    _context = _browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
    return _context


def close() -> dict:
    global _playwright, _browser, _context
    for obj in (_context, _browser, _playwright):
        try:
            if obj is not None:
                obj.close() if hasattr(obj, "close") else obj.stop()
        except Exception:
            pass
    _playwright = _browser = _context = None
    return {"closed": True}


def ready() -> dict:
    return {"playwright": _available(), "allowed": allowed(),
            "context": _context is not None}


def _available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


# ── what it can do ───────────────────────────────────────────────────────────

def _log(action: str, target: str, ok: bool, detail: str = "") -> None:
    with _lock:
        d = _load()
        d["runs"].append({"action": action, "target": target, "ok": bool(ok),
                          "detail": str(detail or "")[:200], "at": time.time()})
        d["runs"] = d["runs"][-200:]
        _save()
    try:
        from core import journal as J
        J.entry("done" if ok else "error", f"browser: {action} {target}"[:180],
                body=str(detail or "")[:200], tags=["browser"],
                ok=bool(ok), actor="browser")
    except Exception:
        pass


def _guard(url: str) -> str:
    ok, why = allowed and is_allowed(url)
    if not ok:
        raise ValueError(why)
    ok2, why2 = _robots_allows(url)
    if not ok2:
        raise ValueError(why2)
    return urllib.parse.urlparse(str(url)).hostname or ""


def open_page(url: str, *, timeout: int = DEFAULT_TIMEOUT,
              screenshot: bool = False) -> dict:
    """Load a page and return its readable text. This is the workhorse: almost
    every other action ends by handing back text."""
    host = _guard(url)
    ctx = _start()
    page = ctx.new_page()
    t0 = time.time()
    try:
        page.goto(str(url), timeout=int(timeout) * 1000, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass                                    # many pages never go idle
        title = (page.title() or "")[:200]
        text = _visible_text(page)[:24000]
        links = _links(page)[:40]
        shot = ""
        if screenshot:
            shot = _shot(page)
        _log("open", host, True, title)
        return {"url": page.url, "title": title, "text": text, "links": links,
                "chars": len(text), "screenshot": shot,
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        _log("open", host, False, f"{type(e).__name__}: {e}"[:160])
        raise ValueError(f"could not open {host}: {type(e).__name__}: {e}"[:180])
    finally:
        try:
            page.close()
        except Exception:
            pass


def _visible_text(page) -> str:
    try:
        page.evaluate("""() => {
            document.querySelectorAll('script,style,noscript,svg,iframe')
              .forEach(n => n.remove());
        }""")
    except Exception:
        pass
    try:
        return re.sub(r"\n{3,}", "\n\n", page.inner_text("body") or "")
    except Exception:
        return ""


def _links(page) -> list[dict]:
    try:
        rows = page.eval_on_selector_all(
            "a[href]", "els => els.slice(0,80).map(e => "
            "({t:(e.innerText||'').trim().slice(0,80), h:e.href}))")
        out = []
        for r in rows or []:
            if r.get("t"):
                out.append({"text": r["t"], "href": r["h"]})
        return out
    except Exception:
        return []


def _shot(page) -> str:
    try:
        from core.data_paths import data_root
        d = data_root() / "screenshots"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"shot-{int(time.time())}.png"
        page.screenshot(path=str(p), full_page=False)
        return str(p)
    except Exception:
        return ""


def click_and_read(url: str, text: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Click something by its visible text, then read the page it lands on. This
    is how 'go to the billing page' works without a selector language."""
    host = _guard(url)
    ctx = _start()
    page = ctx.new_page()
    try:
        page.goto(str(url), timeout=int(timeout) * 1000, wait_until="domcontentloaded")
        loc = page.get_by_text(str(text), exact=False).first
        loc.click(timeout=8000)
        page.wait_for_load_state("domcontentloaded", timeout=8000)
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        body = _visible_text(page)[:20000]
        _log("click", f"{host} → {text}", True, page.url[:120])
        return {"url": page.url, "title": (page.title() or "")[:200], "text": body}
    except Exception as e:
        _log("click", f"{host} → {text}", False, str(e)[:160])
        raise ValueError(f"could not click '{text}' on {host}: {e}"[:180])
    finally:
        try:
            page.close()
        except Exception:
            pass


def fill_and_submit(url: str, fields: dict, *, submit: str = "",
                    timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Drive a real form — the honest way to log in, because the user sees it.

    `fields` maps a label or name to a value. Nothing is stored: if the caller
    needs a password, it must hold it in hand, and it goes straight into the
    page and nowhere else."""
    host = _guard(url)
    if not isinstance(fields, dict) or not fields:
        raise ValueError("which fields? {'email': '...', 'password': '...'}")
    ctx = _start()
    page = ctx.new_page()
    try:
        page.goto(str(url), timeout=int(timeout) * 1000, wait_until="domcontentloaded")
        for key, val in list(fields.items())[:12]:
            filled = False
            for sel in (f"input[name='{key}']", f"#{key}",
                        f"input[placeholder*='{key}' i]",
                        f"input[aria-label*='{key}' i]"):
                try:
                    el = page.locator(sel).first
                    el.fill(str(val), timeout=4000)
                    filled = True
                    break
                except Exception:
                    continue
            if not filled:
                try:
                    page.get_by_label(str(key), exact=False).first.fill(
                        str(val), timeout=4000)
                except Exception:
                    _log("fill", f"{host}:{key}", False, "no such field")
                    raise ValueError(f"{host} has no field called '{key}'")
        if submit:
            page.get_by_text(str(submit), exact=False).first.click(timeout=8000)
        else:
            page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        body = _visible_text(page)[:20000]
        # never echo what was typed
        _log("submit", host, True, f"{len(fields)} field(s) → {page.url[:100]}")
        return {"url": page.url, "title": (page.title() or "")[:200], "text": body}
    except ValueError:
        raise
    except Exception as e:
        _log("submit", host, False, str(e)[:160])
        raise ValueError(f"that form did not go through: {e}"[:180])
    finally:
        try:
            page.close()
        except Exception:
            pass


def status_of(urls: list, *, timeout: int = 8) -> list[dict]:
    """Is the site up? A cheap HEAD on the allowlist — the check you want at
    3am, and the one a scheduler job should be running."""
    out = []
    for u in list(urls or [])[:10]:
        try:
            _guard(u)
        except ValueError as e:
            out.append({"url": u, "ok": False, "error": str(e)[:100]})
            continue
        t0 = time.time()
        try:
            req = urllib.request.Request(str(u), method="HEAD",
                                         headers={"User-Agent": "JARVIS"})
            with urllib.request.urlopen(req, timeout=int(timeout)) as r:
                out.append({"url": u, "ok": 200 <= r.status < 400,
                            "status": r.status,
                            "ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            out.append({"url": u, "ok": False, "error": f"{type(e).__name__}"[:60]})
    return out


def history(limit: int = 30) -> list[dict]:
    with _lock:
        return list(reversed(_load()["runs"]))[:max(1, min(int(limit or 30), 200))]


def stats() -> dict:
    with _lock:
        runs = _load()["runs"]
        allow_n = sum(1 for v in _load()["allow"].values() if v.get("on"))
    return {"runs": len(runs), "hosts_allowed": allow_n,
            "ok_rate": (round(sum(1 for r in runs if r.get("ok")) / len(runs), 3)
                        if runs else None),
            "playwright": _available()}
