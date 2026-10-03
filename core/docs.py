"""core/docs.py — turn a record into a document worth sending.

Phase 7. Two outputs from one renderer:

  * a **themed HTML page**, which goes straight onto the display surface (Phase
    4e) so the user can look at it before it leaves the building;
  * a **PDF**, rendered with the Chromium that is already in the Space image.

The PDF part is the reason this is not a PDF library. We have no reportlab and
are not adding one: the same HTML that looks right on the phone is the PDF, so
there is one design instead of two that drift. It is also why the invoice looks
like the dashboard — `core.theme` supplies the tokens, same as every other page
JARVIS produces.
"""

from __future__ import annotations

import html
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from core import theme
from core.data_paths import data_root

MAX_PAGES = 1


def _e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def money(v: float, currency: str = "EUR") -> str:
    """Enough to be readable aloud and unambiguous on an invoice."""
    sym = {"EUR": "€", "USD": "$", "GBP": "£", "TRY": "₺"}.get(currency, "")
    n = f"{abs(v):,.2f}"
    return f"{'-' if v < 0 else ''}{sym}{n}" if sym else f"{v:,.2f} {currency}"


def _money(v: float, cur: str = "EUR") -> str:
    return money(v, cur)


# ── invoice ──────────────────────────────────────────────────────────────────

def invoice_html(inv: dict, *, from_name: str = "", from_detail: str = "",
                 payment_note: str = "") -> str:
    """A real invoice document, in our theme."""
    cur = inv.get("currency", "EUR")
    rows = []
    for i, it in enumerate(inv.get("items", [])):
        rows.append(
            f"<tr><td style='width:28px;color:var(--text-dim)'>{i + 1}</td>"
            f"<td>{_e(it.get('description'))}</td>"
            f"<td style='text-align:right'>{_e(it.get('qty'))}</td>"
            f"<td style='text-align:right'>{_e(money(it.get('amount'), cur))}</td></tr>")
    tax_pct = inv.get("tax_rate") or 0
    # 20.0 reads like a bug on an invoice; 20 reads like a rate
    tax_pct_s = str(int(tax_pct)) if float(tax_pct) == int(float(tax_pct)) else str(tax_pct)
    # built directly rather than rewritten out of a <tr> afterwards: string
    # surgery on generated HTML is how a document ends up subtly wrong
    row = ("display:flex;justify-content:space-between;padding:4px 0;"
           "border-bottom:1px solid var(--border)")
    tax_line = ("<div style='" + row + "'><span class='dim'>VAT ("
                + tax_pct_s + "%)</span><span>"
                + _e(money(inv.get("tax", 0), cur)) + "</span></div>") if tax_pct else ""

    stamp = ""
    st = (inv.get("status") or "draft").upper()
    colour = {"PAID": "var(--green)", "SENT": "var(--pri)",
              "OVERDUE": "var(--red)", "VOID": "var(--text-dim)"}.get(
        st, "var(--acc2)")
    if inv.get("overdue"):
        stamp = "OVERDUE"
        colour = "var(--red)"
    elif st:
        stamp = st

    body = f"""
<div style="max-width:820px;margin:0 auto">
  <div style="display:flex;gap:12px;align-items:flex-start;border-bottom:2px solid var(--pri);
       padding-bottom:12px;margin-bottom:18px">
    <div style="flex:1">
      <div style="font-size:24px;color:var(--pri);letter-spacing:.06em">INVOICE</div>
      <div style="font-size:15px;color:var(--white);margin-top:4px">{_e(inv.get('number'))}</div>
    </div>
    <div style="text-align:right">
      {'<div style="font-size:16px;color:' + colour + ';letter-spacing:.1em">' + stamp + '</div>' if stamp else ''}
      <div class="dim">issued {_e(inv.get('issued'))}</div>
      <div class="dim">due {_e(inv.get('due'))}</div>
    </div>
  </div>

  <div style="display:flex;gap:30px;flex-wrap:wrap;margin-bottom:18px">
    <div style="flex:1;min-width:220px">
      <div class="dim" style="letter-spacing:.12em">FROM</div>
      <div style="color:var(--white);margin-top:4px">{_e(from_name or 'Your name')}</div>
      {'<div class="dim">' + _e(from_detail).replace(chr(10), '<br>') + '</div>' if from_detail else ''}
    </div>
    <div style="flex:1;min-width:220px">
      <div class="dim" style="letter-spacing:.12em">BILL TO</div>
      <div style="color:var(--white);margin-top:4px">{_e(inv.get('client_name'))}</div>
      {'<div class="dim">' + _e(inv.get('company')) + '</div>' if inv.get('company') else ''}
      {'<div class="dim">' + _e(inv.get('client_email')) + '</div>' if inv.get('client_email') else ''}
      {'<div class="dim">' + _e(inv.get('address')).replace(chr(10), '<br>') + '</div>' if inv.get('address') else ''}
    </div>
  </div>

  <table>
    <tr><th>#</th><th>DESCRIPTION</th><th style="text-align:right">QTY</th>
        <th style="text-align:right">AMOUNT</th></tr>
    {''.join(rows)}
  </table>

  <div style="display:flex;justify-content:flex-end;margin-top:14px">
    <div style="min-width:260px">
      <div style='{row}'><span class="dim">SUBTOTAL</span>
        <span>{_e(money(inv.get('subtotal', 0), cur))}</span></div>
      {tax_line}
      <div style="display:flex;justify-content:space-between;padding:8px 0;font-size:20px;
           color:var(--pri);border-top:2px solid var(--pri);margin-top:4px">
        <span>TOTAL</span><span>{_e(money(inv.get('total', 0), cur))}</span></div>
    </div>
  </div>

  {('<div class="warn" style="margin-top:18px">PAYMENT TERMS<br>' + _e(inv.get('notes')).replace(chr(10), '<br>') + '</div>') if inv.get('notes') else ''}
  {('<div class="card" style="margin-top:14px;font-size:12px">' + _e(payment_note) + '</div>') if payment_note else ''}
  <div class="dim" style="margin-top:20px;font-size:10px">
    Generated by JARVIS · {_e(time.strftime('%Y-%m-%d %H:%M'))}
  </div>
</div>"""
    return theme.inject(body=body)


# ── proposal ─────────────────────────────────────────────────────────────────

def proposal_html(p: dict, *, from_name: str = "", from_detail: str = "") -> str:
    cur = p.get("currency", "EUR")
    items = "".join(
        f"<tr><td>{_e(i.get('description') or i.get('desc') or i.get('title'))}</td>"
        f"<td style='text-align:right'>{_e(money(i.get('amount', i.get('price', 0)), cur))}</td></tr>"
        for i in (p.get("items") or []))
    paras = "".join(f"<p>{_e(par)}</p>" for par in
                    re.split(r"\n\s*\n", str(p.get("body") or "")) if par.strip())
    body = f"""
<div style="max-width:820px;margin:0 auto">
  <div style="border-bottom:2px solid var(--pri);padding-bottom:12px;margin-bottom:18px">
    <div class="dim" style="letter-spacing:.14em">PROPOSAL</div>
    <div style="font-size:24px;color:var(--pri);margin-top:4px">{_e(p.get('title'))}</div>
    <div class="dim" style="margin-top:6px">
      for {_e(p.get('client_name') or 'you')} · prepared {_e(p.get('created'))}
      · valid until {_e(p.get('valid_until'))}</div>
  </div>
  {paras or '<p class="dim">No scope written yet.</p>'}
  {('<table style="margin-top:18px"><tr><th>ITEM</th>'
     '<th style="text-align:right">AMOUNT</th></tr>' + items + '</table>') if items else ''}
  <div class="card" style="margin-top:20px">
    <div class="dim" style="letter-spacing:.12em">NEXT STEP</div>
    <div style="margin-top:4px">Say yes and this becomes an invoice the same day.</div>
  </div>
  <div class="dim" style="margin-top:20px;font-size:10px">
    {'<div>' + _e(from_name) + '</div>' if from_name else ''}
    {'<div>' + _e(from_detail).replace(chr(10), '<br>') + '</div>' if from_detail else ''}
    Generated by JARVIS · {_e(time.strftime('%Y-%m-%d %H:%M'))}
  </div>
</div>"""
    return theme.inject(body=body)


# ── pdf ──────────────────────────────────────────────────────────────────────

def to_pdf(html_text: str, name: str = "document") -> Optional[Path]:
    """Render with the Chromium already in the image. Returns None when no
    browser is available (a desktop install), so the caller can fall back to
    showing the HTML rather than failing the request."""
    out_dir = data_root() / "documents"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(name))[:60] or "document"
    target = out_dir / f"{safe}-{int(time.time())}.pdf"
    tmp = out_dir / f".{safe}-{int(time.time())}.html"
    try:
        tmp.write_text(html_text, encoding="utf-8")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            for exe in (os.environ.get("JARVIS_E2E_CHROMIUM"),
                        "/repl/tools/bin/chromium",
                        "/root/.cache/ms-playwright"):
                if exe and Path(exe).exists():
                    browser = p.chromium.launch(headless=True, executable_path=exe,
                                                args=["--no-sandbox"])
                    break
            if browser is None:
                browser = p.chromium.launch(headless=True,
                                           args=["--no-sandbox"])
            try:
                page = browser.new_page()
                page.goto(tmp.as_uri(), wait_until="load", timeout=30000)
                page.emulate_media(media="print")
                page.pdf(path=str(target), format="A4", print_background=True,
                         margin={"top": "12mm", "bottom": "12mm",
                                 "left": "10mm", "right": "10mm"})
            finally:
                browser.close()
        return target if target.exists() else None
    except Exception:
        return None
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def store_html(html_text: str, name: str = "document") -> Path:
    """Keep the HTML too — it is what the display surface shows, and it is
    readable on a phone without a PDF viewer."""
    out_dir = data_root() / "documents"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(name))[:60] or "document"
    target = out_dir / f"{safe}-{int(time.time())}.html"
    target.write_text(html_text, encoding="utf-8")
    return target


def list_documents(limit: int = 40) -> list[dict]:
    out_dir = data_root() / "documents"
    if not out_dir.is_dir():
        return []
    rows = []
    for p in out_dir.iterdir():
        try:
            rows.append({"name": p.name, "bytes": p.stat().st_size,
                         "kind": p.suffix.lstrip(".").lower() or "file",
                         "age_s": round(time.time() - p.stat().st_mtime)})
        except OSError:
            continue
    rows.sort(key=lambda r: r["age_s"])
    return rows[:max(1, min(int(limit or 40), 200))]

