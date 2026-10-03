"""core/globe.py — a live Earth you can talk to, and the data behind it.

Phase 4f. Modelled on the God's Eye View project (github.com/bilawalsidhu,
MIT for the code) but built the way JARVIS needs it: **every third-party call
happens here, on the Space**, and the page we generate gets the JSON inlined.

That split is not cosmetic:
  * rate limits and caching live in one place we control, instead of every
    browser hitting USGS/CelesTrak/OpenSky directly and getting 429'd;
  * attribution and licensing are printed into the page, because several of
    these sources *require* a credit line (ODbL, CC BY, NOAA terms);
  * a page rendered in a sandboxed frame is not subject to the provider's CORS
    policy at all, so nothing breaks in a browser or in a test runner;
  * one dead provider degrades to "layer unavailable" instead of a blank globe.

Every source below is keyless and official. Licences differ and are recorded in
CREDITS, which the page renders; see PLAN.md 4f for the commercial-use notes
(OpenSky is non-commercial, which is why flights default to adsb.lol).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from typing import Any, Optional

UA = "Jaarvis/1.0 (self-hosted JARVIS display; +https://github.com/FatihMakes/Mark-LIV)"

# ── sources ──────────────────────────────────────────────────────────────────
# credit text is mandatory on-page for ODbL / CC BY / NOAA terms; keep it short
# but keep the names. Anything added here must be keyless and have a licence we
# can actually honour.
LAYERS: dict[str, dict[str, Any]] = {
    "quakes": {
        "label": "QUAKES 24H", "credit": "USGS", "licence": "public domain",
        "ttl": 300,
        "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
               "all_day.geojson",
    },
    "quakes_week": {
        "label": "QUAKES 7D", "credit": "USGS", "licence": "public domain",
        "ttl": 1800,
        "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
               "all_week.geojson",
    },
    "sats": {
        "label": "SATELLITES", "credit": "CelesTrak", "licence": "no licence, cite",
        "ttl": 3600,
        "url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=json",
    },
    "sats_all": {
        "label": "ALL SATELLITES", "credit": "CelesTrak", "licence": "no licence, cite",
        "ttl": 3600,
        "url": "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json",
    },
    "flights": {
        "label": "FLIGHTS", "credit": "adsb.lol (ODbL)", "licence": "ODbL 1.0",
        "ttl": 60, "needs_view": True,
        "url": "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{km}",
    },
    "flights_mil": {
        "label": "MILITARY FLIGHTS", "credit": "adsb.lol (ODbL)", "licence": "ODbL 1.0",
        "ttl": 60, "needs_view": True,
        "url": "https://api.adsb.lol/v2/mil",
    },
    "storms": {
        "label": "CYCLONES", "credit": "NOAA NHC", "licence": "NWS public terms",
        "ttl": 900,
        "url": "https://www.nhc.noaa.gov/CurrentStorms.json",
    },
}

#: Esri's classic World Imagery endpoint: no key, attribution carried by us.
BASEMAP = {
    "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
           "MapServer/tile/{z}/{y}/{x}",
    "credit": "Esri, Maxar, Earthstar Geographics",
    "licence": "Esri master agreement (public basemap, attribution required)",
}

#: Shown in the page's credit line, in this order.
CREDITS: list[str] = [
    "Basemap: Esri World Imagery",
    "Earthquakes: USGS (public domain)",
    "Satellites: CelesTrak",
    "Flights: adsb.lol (ODbL 1.0)",
    "Cyclones: NOAA NHC",
    "Weather: Open-Meteo (CC BY 4.0)",
    "Rendered after the style of God's Eye View (MIT)",
]

_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}
_last_hit: dict[str, float] = {}

DEFAULT_LAYERS = ["quakes", "sats", "storms"]
UA_OPEN_METEO = "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}" \
                "&current=temperature_2m,relative_humidity_2m,apparent_temperature," \
                "weather_code,wind_speed_10m&timezone=auto"


def _fetch(url: str, ttl: float, *, timeout: float = 12.0) -> Optional[Any]:
    """Cached, throttled GET. Returns None rather than raising: a dead
    provider must degrade one layer, never the page."""
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
        last = _last_hit.get(url, 0.0)
        if now - last < 0.25:          # be polite even across different layers
            time.sleep(0.25 - (now - last))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    with _lock:
        _cache[url] = (time.time(), data)
        _last_hit[url] = time.time()
    return data


def weather(lat: float, lon: float) -> Optional[dict]:
    """Current conditions at a point (Open-Meteo, CC BY 4.0).

    One retry: a public forecast API occasionally answers 429 or times out, and
    a globe that loses its temperature readout for a minute is worse than one
    that waited a second.
    """
    url = UA_OPEN_METEO.format(lat=round(float(lat), 3), lon=round(float(lon), 3))
    d = _fetch(url, 600)
    if d is None:
        time.sleep(1.0)
        d = _fetch(url, 600)
    cur = (d or {}).get("current") or {}
    if not cur:
        return None
    return {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
            "code": cur.get("weather_code"), "time": cur.get("time")}


def _coord(v: Any) -> Optional[float]:
    """NOAA storm fixes come as "29.6N" / "75.4W"; USGS as floats."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"^\s*([0-9.]+)\s*([NSEWnsew])\s*$", str(v))
    if not m:
        try:
            return float(v)
        except ValueError:
            return None
    num = float(m.group(1))
    return -num if m.group(2).upper() in ("S", "W") else num


def _storm_name(s: dict) -> str:
    return str(s.get("name") or s.get("id") or "storm")


def _quakes(geo: dict, limit: int = 120) -> list[dict]:
    out = []
    for f in (geo or {}).get("features", [])[:limit]:
        p = (f.get("properties") or {})
        c = (f.get("geometry") or {}).get("coordinates") or []
        if len(c) < 2:
            continue
        out.append({"lon": c[0], "lat": c[1], "depth": c[2] if len(c) > 2 else 0,
                    "mag": p.get("mag"), "place": (p.get("place") or "")[:70],
                    "alert": p.get("alert"), "ts": p.get("time")})
    return out


def _sats(els: list, limit: int = 140) -> list[dict]:
    out = []
    for e in (els or [])[:limit]:
        out.append({"name": (e.get("OBJECT_NAME") or "")[:24],
                    "line1": e.get("LINE1"), "line2": e.get("LINE2")})
    return out


def _flights(ac: Any, limit: int = 250) -> list[dict]:
    """adsb.lol answers with a {"ac": [...]} envelope on the radius endpoint and a
    bare list on /mil — so accept either rather than making every caller know."""
    if isinstance(ac, dict):
        ac = ac.get("ac") or []
    out = []
    for a in (ac or [])[:limit]:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        out.append({"lat": a["lat"], "lon": a["lon"],
                    "alt": a.get("alt_baro") or a.get("alt_geom"),
                    "spd": a.get("gs"), "track": a.get("track"),
                    "call": (a.get("flight") or a.get("r") or a.get("hex") or "?").strip(),
                    "type": a.get("t") or "", "mil": a.get("category") == "A3"})
    return out


def snapshot(*, lat: Optional[float] = None, lon: Optional[float] = None,
             radius_km: int = 250, layers: Optional[list[str]] = None,
             with_weather: bool = True) -> dict:
    """Fetch the requested layers. Returns data + counts + credit lines."""
    want = [x for x in (layers or DEFAULT_LAYERS) if x in LAYERS]
    data: dict[str, Any] = {}
    counts: dict[str, int] = {}
    stale: list[str] = []

    for name in want:
        spec = LAYERS[name]
        url = spec["url"]
        if spec.get("needs_view") and (lat is None or lon is None):
            continue                      # flights need somewhere to look from
        if spec.get("needs_view"):
            url = url.format(lat=lat, lon=lon, km=int(max(1, min(radius_km, 400))))
        raw = _fetch(url, spec["ttl"])
        if raw is None:
            stale.append(name)
            continue
        if name.startswith("quakes"):
            data[name] = _quakes(raw)
        elif name.startswith("sats"):
            data[name] = _sats(raw)
        elif name == "storms":
            rows = []
            for st in ((raw or {}).get("activeStorms") or []):
                slat, slon = _coord(st.get("latitude")), _coord(st.get("longitude"))
                if slat is None or slon is None:
                    continue
                rows.append({"id": st.get("id"), "name": _storm_name(st),
                             "type": st.get("type") or "", "lat": slat, "lon": slon,
                             "wind": st.get("windspeed"), "press": st.get("pressure")})
            data[name] = rows
        else:
            data[name] = _flights(raw)
        counts[name] = len(data[name])

    w = weather(lat, lon) if (with_weather and lat is not None and lon is not None) else None
    return {"data": data, "counts": counts, "unavailable": stale, "weather": w,
            "basemap": BASEMAP, "credits": CREDITS, "fetched": time.time(),
            "view": {"lat": lat, "lon": lon, "radius_km": radius_km}}


def place(query: str) -> Optional[dict]:
    """Geocode through the maps module — one geocoder for the whole project.

    Normalised to {name, lat, lon, country} so the globe does not care whether
    the answer came from Nominatim, Photon or something added later.
    """
    from core import maps as _maps
    try:
        hits = _maps.search(query, limit=1)
    except Exception:
        return None
    if not hits:
        return None
    h = hits[0]
    try:
        lat, lon = float(h.get("lat")), float(h.get("lon"))
    except (TypeError, ValueError):
        return None
    return {"name": h.get("label") or h.get("full_name") or query,
            "lat": lat, "lon": lon,
            "country": h.get("country") or "",
            "full": h.get("full_name") or ""}


# ── the page ─────────────────────────────────────────────────────────────────

def _js(obj: Any) -> str:
    """JSON safe to sit inside a <script> block."""
    return json.dumps(obj, separators=(",", ":"), default=str).replace("<", "\\u003c")


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/cesium@1.132.0/Build/Cesium/Widgets/widgets.css">
<style>__THEME__
html,body{overflow:hidden;padding:0}
#gl{position:absolute;inset:0}
.hud{position:absolute;top:0;left:0;right:0;z-index:11;pointer-events:none;
  background:linear-gradient(180deg,rgba(0,13,20,.95),rgba(0,13,20,.72));
  border-bottom:1px solid var(--border-b);padding:6px 10px;gap:10px}
.hud>*{pointer-events:auto}
.hud .grp{display:flex;gap:5px;align-items:center;flex-wrap:wrap}
.credit{position:absolute;left:0;bottom:0;z-index:10;background:rgba(0,6,10,.86);
  border-top:1px solid var(--border);border-right:1px solid var(--border);
  padding:4px 8px;font-size:9px;color:var(--text-dim);letter-spacing:.03em;
  max-width:100vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.info{position:absolute;right:0;top:44px;z-index:10;width:min(300px,42vw);
  background:rgba(1,13,20,.9);border-left:1px solid var(--border);padding:9px 11px}
.info h3{margin:0 0 5px;font-size:11px}
.info .row{display:flex;justify-content:space-between;gap:8px;font-size:11px;
  padding:2px 0;border-bottom:1px solid var(--border)}
.info .row b{color:var(--pri);font-weight:400}
.modes .btn.on{background:var(--pri);color:var(--bg)}
#scan{position:absolute;inset:0;z-index:9;pointer-events:none;opacity:0;transition:opacity .25s}
#scan.nvg{opacity:1;background:
  repeating-linear-gradient(0deg,rgba(0,255,120,.055) 0 1px,transparent 1px 3px),
  radial-gradient(ellipse at center,rgba(0,255,110,.10),rgba(0,60,25,.30) 78%)}
#scan.flir{opacity:1;background:
  repeating-linear-gradient(0deg,rgba(255,0,0,.05) 0 1px,transparent 1px 3px),
  linear-gradient(180deg,rgba(0,0,255,.28),rgba(255,140,0,.22) 45%,rgba(255,0,60,.26))}
#scan.crt{opacity:1;background:
  repeating-linear-gradient(0deg,rgba(0,0,0,.35) 0 1px,transparent 1px 2px),
  radial-gradient(ellipse at center,transparent 55%,rgba(0,0,0,.75))}
#scan.crt::after{content:"";position:absolute;inset:0;background:rgba(120,220,255,.05);
  animation:flick 2.7s steps(2) infinite}
@keyframes flick{0%,100%{opacity:.05}50%{opacity:.14}}
#gl.nvg{filter:grayscale(1) sepia(1) hue-rotate(75deg) saturate(2.6) brightness(1.12) contrast(1.15)}
#gl.flir{filter:grayscale(1) contrast(1.5) brightness(1.05)}
#gl.crt{filter:saturate(1.25) contrast(1.1)}
#gl.crt #gl-wish{text-shadow:1px 0 rgba(255,0,0,.5),-1px 0 rgba(0,255,255,.5)}
.boot{position:absolute;inset:0;z-index:20;display:flex;align-items:center;
  justify-content:center;flex-direction:column;gap:8px;background:var(--bg);color:var(--text-dim);
  font-size:11px;letter-spacing:.12em;text-align:center;padding:20px}
</style></head>
<body>
<div id="gl"></div><div id="scan"></div>
<div class="hud">
  <div class="grp">
    <span class="dot"></span><span>JARVIS ORBITAL</span>
    <span id="utc" class="dim"></span>
  </div>
  <div class="grp" id="lay"></div>
  <div class="grp modes" style="margin-left:auto" id="modes">
    <button class="btn on" data-m="normal">NORMAL</button>
    <button class="btn" data-m="nvg">NVG</button>
    <button class="btn" data-m="flir">FLIR</button>
    <button class="btn" data-m="crt">CRT</button>
  </div>
</div>
<div class="info" id="info"></div>
<div class="credit">__CREDITS__</div>
<div class="boot" id="boot">INITIALISING ORBITAL VIEW<div class="bar" style="width:180px">
  <i style="width:30%"></i></div><span>fetching live layers</span></div>
<script>window.CESIUM_BASE_URL="https://cdn.jsdelivr.net/npm/cesium@1.132.0/Build/Cesium/";</script>
<script src="https://cdn.jsdelivr.net/npm/cesium@1.132.0/Build/Cesium/Cesium.js"></script>
<script src="https://cdn.jsdelivr.net/npm/satellite.js@5.0.0/dist/satellite.min.js"></script>
<script>
const SNAP = /*SNAP*/;
const HAS = { cesium: typeof Cesium !== 'undefined', sgp4: typeof satellite !== 'undefined' };
const $ = s => document.querySelector(s);
const boot = $('#boot');

if (!HAS.cesium) {
  boot.innerHTML = '<div style="color:var(--acc2);font-size:13px">CESIUM COULD NOT LOAD</div>'
    + '<div>The globe needs WebGL and the Cesium build from the CDN. The data below is '
    + 'still live &#8212; the map is what failed to render.</div>';
} else {
  const layers = [];
  const viewer = new Cesium.Viewer('gl', {
    animation:false, timeline:false, baseLayerPicker:false, geocoder:false,
    homeButton:false, sceneModePicker:false, navigationHelpButton:false,
    fullscreenButton:false, infoBox:false, selectionIndicator:false,
    requestRenderMode:false,
    // no Cesium Ion: we have no token and do not need one. Ellipsoid terrain
    // plus the Esri basemap below is the whole Earth, honestly drawn.
    baseLayer: false,
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    contextOptions: { webgl: { alpha: false } },
  });
  viewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
    url: SNAP.basemap.url, credit: SNAP.basemap.credit,
    maximumLevel: 17, enableLighting: true,
  }));
  viewer.scene.backgroundColor = Cesium.Color.BLACK;
  viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString('#00060a');
  viewer.scene.fog.enabled = true;
  viewer.scene.fog.density = 0.00022;
  viewer.scene.globe.showGroundAtmosphere = true;
  viewer.scene.skyAtmosphere.show = true;
  if (viewer.scene.postProcessStages && viewer.scene.postProcessStages.fxaa) {
    viewer.scene.postProcessStages.fxaa.enabled = true;
  }
  viewer.scene.globe.depthTestAgainstTerrain = false;

  const V = {
    pri: Cesium.Color.fromCssColorString('#00d4ff'),
    acc: Cesium.Color.fromCssColorString('#ff6b00'),
    acc2: Cesium.Color.fromCssColorString('#ffcc00'),
    green: Cesium.Color.fromCssColorString('#00ff88'),
    red: Cesium.Color.fromCssColorString('#ff3355'),
    white: Cesium.Color.fromCssColorString('#d8f8ff'),
    dim: Cesium.Color.fromCssColorString('#5ab8cc'),
  };
  const FONT = '11px Courier New, monospace';

  function add(pt, opt) {
    return viewer.entities.add({
      position: Cesium.Cartesian3.fromDegrees(pt.lon, pt.lat, pt.alt || 0),
      point: { pixelSize: opt.size || 6, color: opt.color || V.pri,
               outlineColor: Cesium.Color.BLACK, outlineWidth: 1,
               disableDepthTestDistance: Number.POSITIVE_INFINITY },
      label: opt.label ? {
        text: opt.label, font: FONT, fillColor: opt.labelColor || V.white,
        outlineColor: Cesium.Color.BLACK, outlineWidth: 2,
        style: Cesium.LabelStyle.FILL_AND_OUTLINE,
        pixelOffset: new Cesium.Cartesian2(9, -9),
        scale: 0.95, disableDepthTestDistance: Number.POSITIVE_INFINITY,
      } : undefined,
      polyline: opt.track ? { positions: opt.track, width: 1.5,
                              material: Cesium.Color.fromCssColorString('#007a99')
                                          .withAlpha(0.75),
                              arcType: Cesium.ArcType.GEODESIC } : undefined,
    });
  }

  const NAMES = { quakes:'QUAKES 24H', quakes_week:'QUAKES 7D', sats:'SATELLITES',
                  sats_all:'ALL SATELLITES', flights:'FLIGHTS', flights_mil:'MILITARY',
                  storms:'CYCLONES' };
  const objs = {};

  function build(name) {
    const arr = SNAP.data[name] || [];
    objs[name] = new Cesium.CustomDataSource(name);
    if (name.startsWith('quakes')) {
      arr.forEach(q => {
        const m = Number(q.mag) || 0;
        const c = m >= 6 ? V.red : m >= 4.5 ? V.acc : m >= 2.5 ? V.acc2 : V.green;
        add(q, { size: Math.max(4, Math.min(20, m * 2.2)), color: c,
                 label: m >= 4.5 ? 'M' + m.toFixed(1) : null, labelColor: c });
      });
    } else if (name.startsWith('sats')) {
      if (HAS.sgp4) {
        arr.forEach(s => {
          try {
            const rec = satellite.twoline2satrec(s.line1, s.line2);
            const pos = { lat: 0, lng: 0, alt: 0 };
            const p = satellite.propagate(rec, Date.now());
            const gmst = satellite.gstime(new Date());
            const pv = satellite.eciToGeodetic(p.position, gmst);
            s.lat = pv.latitude * 180 / Math.PI; s.lon = pv.longitude * 180 / Math.PI;
            s.alt = pv.height * 1000;
            const trk = [];
            for (let m = 0; m <= 100; m++) {
              const pp = satellite.propagate(rec, Date.now() + m * 60000);
              const gg = satellite.eciToGeodetic(pp.position, satellite.gstime(new Date(Date.now() + m * 60000)));
              trk.push([gg.longitude * 180 / Math.PI, gg.latitude * 180 / Math.PI, 0]);
            }
            add(s, { size: 5, color: V.pri, label: s.name, labelColor: V.dim, track: trk });
          } catch (e) { /* a bad TLE is not worth a blank layer */ }
        });
      }
    } else if (name === 'storms') {
      arr.forEach(s => add(s, { size: 11, color: V.acc,
        label: s.name + ' ' + (s.type || ''), labelColor: V.acc2 }));
    } else {
      arr.forEach(f => add(f, { size: 4, color: f.mil ? V.acc : V.green,
        label: f.call, labelColor: f.mil ? V.acc : V.dim }));
    }
    viewer.dataSources.add(objs[name]);
    objs[name].show = true;
  }

  const present = Object.keys(SNAP.data).filter(k => (SNAP.data[k] || []).length);
  present.forEach(build);

  // HUD wiring
  const lay = $('#lay');
  present.concat(SNAP.unavailable || []).forEach(k => {
    const b = document.createElement('button');
    b.className = 'btn' + (SNAP.unavailable.includes(k) ? '' : ' on');
    b.textContent = (NAMES[k] || k) + (SNAP.unavailable.includes(k) ? ' ✗' : '');
    if (SNAP.unavailable.includes(k)) { b.disabled = true; b.style.opacity = .45; }
    b.onclick = () => {
      if (!objs[k]) return;
      objs[k].show = !objs[k].show;
      b.classList.toggle('on', objs[k].show);
    };
    lay.appendChild(b);
  });

  function info() {
    const w = SNAP.weather;
    const rows = [
      ['UTC', new Date().toISOString().slice(11, 19)],
      ['LAYERS', Object.keys(SNAP.data).length + (SNAP.unavailable.length ? ' (' + SNAP.unavailable.length + ' down)' : '')],
    ];
    Object.keys(SNAP.counts || SNAP.data).forEach(k => {
      rows.push([NAMES[k] || k, (SNAP.data[k] || []).length]);
    });
    if (w) rows.push(['TEMP', w.temp + '°C (feels ' + w.feels + ')']);
    if (w) rows.push(['WIND', w.wind + ' km/h']);
    if (w) rows.push(['HUMIDITY', w.humidity + '%']);
    $('#info').innerHTML = '<h3>ORBITAL FEED</h3>' + rows.map(r =>
      '<div class="row"><span>' + r[0] + '</span><b>' + r[1] + '</b></div>').join('')
      + (SNAP.unavailable.length ? '<div class="row"><span>DOWN</span><b style="color:var(--acc2)">'
        + SNAP.unavailable.join(', ') + '</b></div>' : '');
  }
  info();
  setInterval(info, 1000);
  setInterval(() => { $('#utc').textContent = new Date().toISOString().slice(11, 19) + 'Z'; }, 1000);

  document.querySelectorAll('#modes .btn').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('#modes .btn').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
      const m = b.dataset.m;
      $('#gl').className = m === 'normal' ? '' : m;
      $('#scan').className = m === 'normal' ? '' : m;
    };
  });

  const v = SNAP.view || {};
  if (v.lat != null && v.lon != null) {
    viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(v.lon, v.lat, 260000),
      duration: 2.2 });
  } else {
    viewer.camera.flyTo({ destination: Cesium.Cartesian3.fromDegrees(20, 18, 26000000),
      duration: 1.6 });
  }
  boot.remove();

  // voice-ish escape hatches the parent frame can call
  window.jarvisFlyTo = (lat, lon, h) => viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(lon, lat, h || 200000), duration: 1.8 });
  window.jarvisReady = true;
}
</script></body></html>"""


def build_page(snap: dict, title: str = "JARVIS ORBITAL") -> str:
    """A complete, self-contained globe page from a snapshot."""
    from core import theme as _theme
    credits = " · ".join(snap.get("credits") or CREDITS)
    return (PAGE.replace("__TITLE__", str(title).replace("<", "&lt;")[:120])
                .replace("__THEME__", _theme.THEME_CSS)
                .replace("__CREDITS__", credits.replace("<", "&lt;"))
                .replace("/*SNAP*/", _js(snap)))


def describe(snap: dict) -> str:
    """One line for the model to say out loud, plus the honest caveats."""
    counts = snap.get("counts") or {}
    bits = [f"{k} {v}" for k, v in counts.items() if v]
    down = snap.get("unavailable") or []
    out = "Live on the globe: " + (", ".join(bits) if bits else "imagery only") + "."
    if down:
        out += f" Not answering right now: {', '.join(down)}."
    out += " Credit line is on the page, as those sources require."
    return out
