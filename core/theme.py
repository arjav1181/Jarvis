"""core/theme.py — the dashboard's own design tokens, for everything JARVIS draws.

Why this exists: Phase 4e lets the model write the page itself, which means
the theme is only guaranteed if we *hand it over*. A model told to "make it
look nice" will invent its own palette, and a week later the display shows a
purple gradient next to a cyan HUD and the product looks unfinished.

So the tokens live here once, in Python, and are injected into every artifact
JARVIS produces: model-written pages (4e), the globe (4f), the maps (4f).
Values are lifted verbatim from `dashboard/static/app.html`'s `:root` — if the
dashboard's palette changes, change TOKENS here in the same commit and every
page follows. `e2e/test_theme.py` fails if the two ever drift apart.

`THEME_CSS` is safe to inline in a page the model wrote: it is a static string
with no interpolation, so it cannot smuggle anything into the artifact.
"""

from __future__ import annotations

import re

# ── the dashboard's tokens, verbatim ─────────────────────────────────────────
TOKENS: dict[str, str] = {
    "bg": "#00060a",
    "panel": "#010d14",
    "panel2": "#010f18",
    "dark": "#000d14",
    "border": "#0d3347",
    "border-b": "#1a5c7a",
    "border-a": "#0f4060",
    "pri": "#00d4ff",
    "pri-dim": "#007a99",
    "pri-gho": "#001f2e",
    "acc": "#ff6b00",
    "acc2": "#ffcc00",
    "green": "#00ff88",
    "green-d": "#00aa55",
    "red": "#ff3355",
    "text": "#8ffcff",
    "text-dim": "#3a8a9a",
    "text-med": "#5ab8cc",
    "white": "#d8f8ff",
    "bar-bg": "#011520",
}

FONT = "'Courier New', Courier, monospace"

#: Injected into every page JARVIS writes. Kept small on purpose: it is a
#: vocabulary (colors, type, one button, one card, one table), not a framework.
THEME_CSS = """
:root{
  --bg:%(bg)s;--panel:%(panel)s;--panel2:%(panel2)s;--dark:%(dark)s;
  --border:%(border)s;--border-b:%(border-b)s;--border-a:%(border-a)s;
  --pri:%(pri)s;--pri-dim:%(pri-dim)s;--pri-gho:%(pri-gho)s;
  --acc:%(acc)s;--acc2:%(acc2)s;--green:%(green)s;--green-d:%(green-d)s;
  --red:%(red)s;--text:%(text)s;--text-dim:%(text-dim)s;
  --text-med:%(text-med)s;--white:%(white)s;--bar-bg:%(bar-bg)s;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%%;background:var(--bg);color:var(--text);font-family:%(font)s;
  overflow:auto;-webkit-font-smoothing:antialiased}
body{padding:18px}
h1,h2,h3{color:var(--pri);font-weight:600;letter-spacing:.04em}
h1{font-size:clamp(18px,3.2vw,30px);text-shadow:0 0 18px rgba(0,212,255,.35)}
h2{font-size:clamp(14px,2.2vw,19px);margin:18px 0 8px}
h3{font-size:clamp(12px,1.8vw,15px);margin:14px 0 6px;color:var(--text-med)}
p,li,td,th,label,div{font-size:clamp(12px,1.7vw,15px);line-height:1.55}
a{color:var(--pri);text-decoration:none;border-bottom:1px solid var(--pri-dim)}
a:hover{color:var(--green);border-bottom-color:var(--green)}
small,.dim{color:var(--text-dim);font-size:11px}
code,kbd{background:var(--pri-gho);color:var(--text);padding:1px 5px;border:1px solid var(--border);
  border-radius:3px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin:10px 0}
.card:hover{border-color:var(--border-b)}
.btn{background:var(--pri-gho);color:var(--pri);border:1px solid var(--border-b);
  border-radius:5px;padding:5px 10px;font-family:%(font)s;font-size:12px;cursor:pointer;
  letter-spacing:.05em}
.btn:hover{background:var(--pri-dim);color:var(--white)}
.btn.on{background:var(--pri);color:var(--bg)}
table{width:100%%;border-collapse:collapse;margin:8px 0}
th{color:var(--pri);text-align:left;font-size:11px;letter-spacing:.08em;
  border-bottom:1px solid var(--border-b);padding:5px 6px}
td{border-bottom:1px solid var(--border);padding:5px 6px;color:var(--text-med)}
tr:hover td{background:var(--pri-gho);color:var(--text)}
.grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%%),1fr))}
.hud{position:fixed;top:0;left:0;right:0;display:flex;gap:10px;align-items:center;
  padding:7px 12px;background:rgba(0,13,20,.94);border-bottom:1px solid var(--border-b);
  font-size:11px;letter-spacing:.1em;color:var(--pri);z-index:50}
.hud .sp{margin-left:auto;color:var(--text-dim);letter-spacing:.04em}
.hud .dot{width:6px;height:6px;border-radius:50%%;background:var(--green);
  box-shadow:0 0 8px var(--green);display:inline-block;margin-right:6px}
.warn{background:rgba(255,204,0,.08);border:1px solid var(--acc2);color:var(--acc2);
  border-radius:6px;padding:8px 10px;font-size:12px;margin:10px 0}
.bar{height:6px;background:var(--bar-bg);border:1px solid var(--border);border-radius:3px;
  overflow:hidden}
.bar>i{display:block;height:100%%;background:var(--pri);box-shadow:0 0 10px var(--pri)}
.ok{color:var(--green)}.bad{color:var(--red)}.warnc{color:var(--acc2)}
ul{padding-left:18px}
::selection{background:var(--pri);color:var(--bg)}
""" % {**TOKENS, "font": FONT}

#: Appended to the model tool description so it knows the vocabulary exists.
GUIDANCE = (
    "Every page you write is automatically themed: the CSS variables --pri "
    "(cyan), --acc (orange), --acc2 (yellow), --bg, --panel, --border, --text, "
    "--white, plus .card/.btn/.table/.grid/.hud/.bar classes are ALREADY "
    "defined on the page. Use them instead of inventing colors or a font. "
    "Never hardcode a hex background for the page or body: use var(--bg) and "
    "var(--panel) so the artifact matches the JARVIS dashboard. Use the "
    "Courier/monospace look, cyan headings, and keep it dark."
)


def inject(css: str = "", body: str = "", *, font: str | None = None) -> str:
    """Return a complete, on-theme HTML document for `body`."""
    link = ""
    if font:
        link = f'<link rel="preconnect" href="https://fonts.googleapis.com">'
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"{link}<style>{css or THEME_CSS}</style></head><body>{body}</body></html>"
    )


def apply_to_document(html: str) -> str:
    """Inject the theme into a *full document* the model wrote.

    Our <style> goes in first so the model's own rules still win — the point is
    that `var(--pri)` always exists, not that we overrule the page. A page that
    deliberately wants a different look can still have one; a page that forgot
    to have a look at all now gets ours.
    """
    out = str(html or "")
    if not out.strip():
        return ""
    style = f"<style>{THEME_CSS}</style>"
    m = re.search(r"<head[^>]*>", out, re.I)
    if m:
        return out[:m.end()] + style + out[m.end():]
    m = re.search(r"<html[^>]*>", out, re.I)
    if m:
        return out[:m.end()] + f"<head>{style}</head>" + out[m.end():]
    m = re.search(r"<body[^>]*>", out, re.I)
    if m:
        return out[:m.end()] + style + out[m.end():]
    return inject(body=out)
