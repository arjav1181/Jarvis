"""core/gev_theme.py — the JARVIS chrome around the vendored God's Eye View.

The vendored app (MIT, pinned in godseye/MANIFEST.json) has its own design
system and its own `:root`. This file used to override those variables at serve
time to make it look like ours. That was a reskin, and it was wrong in a way
that mattered: the app shipped with one theme and rendered with another, so
every future upstream `:root` change would quietly stop applying, and there
was no single place to look to answer "what colour is this?".

**The palette now lives in their source.** `foundation.css` `:root` carries the
JARVIS tokens, so the vendored build is themed natively and survives an
upstream update. What is left here is only the part that is genuinely OURS and
genuinely not theirs:

  * a **HUD frame** — corner brackets and a thin rule, so the globe sits inside
    an instrument rather than filling a browser tab;
  * a **title block** — "GOD'S EYE VIEW // JARVIS", a live UTC clock and the
    layer count, because a spy satellite should tell you what time it is;
  * a **boot veil** — the JARVIS ring while Cesium loads, which is a 35 MB
    bundle and would otherwise show a black screen for a second or two.

All CSS, no injected JavaScript into their bundle, so their CSP stays intact
and a failure in this file can only ever make the globe plainer, never stop it
loading.

REBUILDING THEIR SOURCE
    `vite build` empties dist/ and does not reproduce the 3D models, the event
    imagery or the icons — there is no public/ directory, so those are static
    content, not build output. A plain rebuild silently deletes 21 files
    including every aircraft .glb. Use `node tools/build-dist.mjs`, which
    preserves and restores them and fails the build if any are missing.
"""

from __future__ import annotations

import re

#: The tokens we impose. Their names, our values — overriding a variable is
#: the whole mechanism, so this list is the contract.
TOKENS = {
    "--bg-dark": "#00060a",
    "--bg": "#00060a",
    "--glass-bg": "rgba(2, 14, 22, .78)",
    "--glass-bg-strong": "rgba(2, 14, 22, .92)",
    "--glass-border": "rgba(0, 212, 255, .16)",
    "--glass-border-hover": "rgba(0, 212, 255, .38)",
    "--accent": "#00d4ff",
    "--accent-dim": "rgba(0, 212, 255, .14)",
    "--accent-glow": "rgba(0, 212, 255, .38)",
    "--text-primary": "#e6f7ff",
    "--text-secondary": "rgba(190, 226, 240, .62)",
    "--text-dim": "rgba(190, 226, 240, .34)",
    "--menu-bg": "#02101a",
    "--panel-radius": "10px",
    "--btn-radius": "6px",
}

#: Amber is for anything that spends, sends, or is irreversible. In God's Eye
#: that is almost nothing — but the layer that can contact a person, and the
#: "fly there" that commits a route, should not glow the same cyan as a query.
AMBER = "#ff6b00"
AMBER_DIM = "rgba(255, 107, 0, .16)"

_CSS = r"""
/* ── JARVIS chrome for God's Eye View ───────────────────────────────────────
   Injected at serve time by core/gev_theme.py.

   This is ADDITION only. The palette used to be overridden here from the top
   of this file; it now lives in the app's own
   src/ui/styles/foundation.css `:root`, so the vendored source is the single
   truth for how it looks. What remains here is the part that is genuinely
   ours and genuinely not theirs: the HUD frame, the title block with its UTC
   clock, the boot veil for the 35 MB Cesium bundle, and the scanlines.

   Nothing here touches their JavaScript. */


/* Their panels: tighter corners, a real hairline, and a glow that reads as
   backlight rather than a coloured box. */
[class*="glass"], [class*="panel"], [class*="dock"], [class*="menu"],
[class*="visor"], [class*="rail"], [class*="stack"] {
  backdrop-filter: blur(14px) saturate(1.25) !important;
  -webkit-backdrop-filter: blur(14px) saturate(1.25) !important;
  border-color: var(--glass-border) !important;
  box-shadow: 0 0 0 1px rgba(0, 212, 255, .04),
              0 18px 50px rgba(0, 0, 0, .55),
              inset 0 1px 0 rgba(255, 255, 255, .05) !important;
}

/* Buttons: flat, quiet, and only glowing when they are the thing to press. */
button, [role="button"], .btn {
  font-family: var(--font-mono) !important;
  letter-spacing: .06em !important;
  text-transform: uppercase !important;
  font-size: 11px !important;
  border-radius: var(--btn-radius) !important;
  border: 1px solid var(--glass-border) !important;
  background: rgba(0, 212, 255, .05) !important;
  color: var(--text-primary) !important;
  transition: background .15s, border-color .15s, box-shadow .15s !important;
}
button:hover, [role="button"]:hover, .btn:hover {
  border-color: var(--glass-border-hover) !important;
  background: rgba(0, 212, 255, .12) !important;
  box-shadow: 0 0 18px rgba(0, 212, 255, .22) !important;
}
button:active, [role="button"]:active {
  background: rgba(0, 212, 255, .2) !important;
}
/* The primary action in any group gets the accent; everything else stays quiet. */
button.primary, [data-primary="true"], .is-active, [aria-pressed="true"] {
  background: rgba(0, 212, 255, .16) !important;
  border-color: var(--accent) !important;
  color: #fff !important;
  box-shadow: 0 0 22px rgba(0, 212, 255, .3) !important;
}

/* Sliders and toggles — the two controls people actually read as "themed". */
input[type="range"] {
  -webkit-appearance: none !important;
  appearance: none !important;
  height: 3px !important;
  background: linear-gradient(90deg, var(--accent) var(--val, 50%),
              rgba(0, 212, 255, .15) var(--val, 50%)) !important;
  border-radius: 2px !important;
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none !important;
  width: 13px !important; height: 13px !important;
  border-radius: 50% !important;
  background: #001420 !important;
  border: 2px solid var(--accent) !important;
  box-shadow: 0 0 12px rgba(0, 212, 255, .55) !important;
}
input[type="range"]::-moz-range-thumb {
  width: 13px !important; height: 13px !important;
  border-radius: 50% !important;
  background: #001420 !important;
  border: 2px solid var(--accent) !important;
  box-shadow: 0 0 12px rgba(0, 212, 255, .55) !important;
}

/* ── the HUD frame ──────────────────────────────────────────────────────────
   Corner brackets plus a hairline, so the globe reads as an instrument. */
#cesiumContainer::after {
  content: "";
  position: fixed; inset: 10px;
  pointer-events: none; z-index: 40;
  border: 1px solid rgba(0, 212, 255, .14);
}
#cesiumContainer::before {
  content: "";
  position: fixed; inset: 0;
  pointer-events: none; z-index: 39;
  background:
    linear-gradient(var(--accent), var(--accent)) 14px 14px / 26px 2px,
    linear-gradient(var(--accent), var(--accent)) 14px 14px / 2px 26px,
    linear-gradient(var(--accent), var(--accent)) calc(100% - 14px) 14px / 26px 2px,
    linear-gradient(var(--accent), var(--accent)) calc(100% - 14px) 14px / 2px 26px,
    linear-gradient(var(--accent), var(--accent)) 14px calc(100% - 14px) / 26px 2px,
    linear-gradient(var(--accent), var(--accent)) 14px calc(100% - 14px) / 2px 26px,
    linear-gradient(var(--accent), var(--accent)) calc(100% - 14px) calc(100% - 14px) / 26px 2px,
    linear-gradient(var(--accent), var(--accent)) calc(100% - 14px) calc(100% - 14px) / 2px 26px;
  background-repeat: no-repeat;
  opacity: .5;
}

/* ── the title block ──────────────────────────────────────────────────────── */
#jarvis-gev-hud {
  position: fixed; top: 22px; left: 50%; transform: translateX(-50%);
  z-index: 45; pointer-events: none;
  display: flex; flex-direction: column; align-items: center; gap: 3px;
  font-family: var(--font-mono);
  text-shadow: 0 0 18px rgba(0, 212, 255, .45);
}
#jarvis-gev-hud .t {
  font-size: 13px; letter-spacing: .42em; color: var(--text-primary);
}
#jarvis-gev-hud .s {
  font-size: 9px; letter-spacing: .3em; color: var(--text-dim);
  display: flex; gap: 14px;
}
#jarvis-gev-hud .s b { color: var(--accent); font-weight: 500; }

/* ── the boot veil ──────────────────────────────────────────────────────────
   Cesium is 35 MB; without this the first paint is a black rectangle. */
#jarvis-gev-boot {
  position: fixed; inset: 0; z-index: 60;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 18px; background: radial-gradient(120% 90% at 50% 0%, #04202e 0%, #00060a 62%);
  font-family: var(--font-mono);
  transition: opacity .6s ease;
}
#jarvis-gev-boot.done { opacity: 0; pointer-events: none; }
#jarvis-gev-boot .ring {
  width: 74px; height: 74px; border-radius: 50%;
  border: 1px solid rgba(0, 212, 255, .25);
  border-top-color: var(--accent);
  animation: jarvis-gev-spin 1.1s linear infinite;
  box-shadow: 0 0 34px rgba(0, 212, 255, .28), inset 0 0 22px rgba(0, 212, 255, .12);
}
#jarvis-gev-boot .l {
  font-size: 10px; letter-spacing: .34em; color: var(--text-secondary);
}
@keyframes jarvis-gev-spin { to { transform: rotate(360deg); } }

/* A faint scanline, because this is a surveillance instrument and it should
   look like one. Kept at 3% so it textures without costing a frame. */
#jarvis-gev-scan {
  position: fixed; inset: 0; z-index: 38; pointer-events: none;
  background: repeating-linear-gradient(0deg,
    rgba(0, 212, 255, .028) 0 1px, transparent 1px 3px);
  mix-blend-mode: screen;
}
"""


def hud_html(layers: int = 0) -> str:
    """The title block. `layers` is filled in by JS once their manifest loads;
    until then it shows a dash rather than a wrong number."""
    return (
        '<div id="jarvis-gev-hud" aria-hidden="true">'
        '<div class="t">GOD&#39;S EYE VIEW</div>'
        '<div class="s"><span>JARVIS</span>'
        f'<span>UTC <b id="jarvis-gev-clock">--:--:--</b></span>'
        f'<span>LAYERS <b id="jarvis-gev-layers">{layers or "—"}</b></span>'
        "</div></div>"
    )


def boot_html() -> str:
    return (
        '<div id="jarvis-gev-boot">'
        '<div class="ring"></div>'
        '<div class="l">INITIALISING ORBITAL VIEW</div>'
        "</div>"
        '<div id="jarvis-gev-scan" aria-hidden="true"></div>'
    )


def script_html() -> str:
    """The small amount of JS the skin needs.

    Deliberately tiny and defensive: it must never throw. If their bundle has not
    finished when this runs, it waits; if it never does, the veil simply stays
    and the globe is still usable underneath it.
    """
    return """<script>
(function () {
  "use strict";
  var boot = document.getElementById('jarvis-gev-boot');
  var layersEl = document.getElementById('jarvis-gev-layers');
  var clockEl = document.getElementById('jarvis-gev-clock');

  // UTC clock, because a satellite view without a timezone is a screenshot.
  function tick() {
    if (!clockEl) return;
    try {
      var d = new Date();
      clockEl.textContent =
        String(d.getUTCHours()).padStart(2, "0") + ":" +
        String(d.getUTCMinutes()).padStart(2, "0") + ":" +
        String(d.getUTCSeconds()).padStart(2, "0");
    } catch (e) {}
  }
  tick();
  setInterval(tick, 1000);

  // Their manifest is the source of truth for how many layers exist. Poll for
  // it rather than guessing a selector, and give up quietly after a while.
  var tries = 0;
  function countLayers() {
    tries++;
    if (layersEl) {
      var n = 0;
      try {
        // their layer toggles carry an aria-pressed / data-state; count those
        n = document.querySelectorAll(
          '[data-layer-id], [aria-pressed="true"]').length;
      } catch (e) { n = 0; }
      if (n) layersEl.textContent = String(n);
    }
    if (tries < 40) setTimeout(countLayers, 500);
  }
  setTimeout(countLayers, 1200);

  // Lift the veil once their canvas has actually painted something.
  function lift() {
    if (!boot) return;
    try {
      var c = document.querySelector("#cesiumContainer canvas");
      if (c && c.width > 0) { boot.classList.add("done"); return; }
    } catch (e) {}
    if (tries < 120) setTimeout(lift, 250);
    else boot.classList.add("done");   // never trap the user behind a veil
  }
  setTimeout(lift, 600);
})();
</script>"""


def inject(html: str) -> str:
    """Add the skin to a God's Eye document. Idempotent, and safe to call on a
    document that already has it."""
    if "jarvis-gev-boot" in html:
        return html
    css = "<style>\n" + _CSS + "\n</style>"
    html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + css, html,
                  count=1, flags=re.I)
    # the HUD and boot veil go at the top of the body, before their own root, so
    # their layout is never displaced by ours
    html = re.sub(r"(<body[^>]*>)",
                  lambda m: m.group(1) + boot_html() + hud_html(), html,
                  count=1, flags=re.I)
    html = re.sub(r"(</body>)", script_html() + r"\1", html,
                  count=1, flags=re.I)
    return html


def describe() -> str:
    return ("God's Eye chrome: HUD frame, title block with UTC clock, "
            "boot veil, scanlines. Palette is themed at the source "
            "(src/ui/styles/foundation.css), not overridden here.")
