#!/usr/bin/env python3
"""Vendor the real God's Eye View into this repo, reproducibly.

    python3 scripts/build_godseye.py                # build the pinned upstream
    python3 scripts/build_godseye.py --check        # verify only, no network

Upstream: github.com/bilawalsidhu/gods-eye-view (MIT for the code; see their
DATA_SOURCES.md for per-dataset terms). We pin one commit and record it in
godseye/MANIFEST.json, so the Space never has to reach GitHub to boot.

What this does beyond `vite build`:
  * flattens dist/godseye/* (their bundled Cesium) up one level, because
    index.html references /godseye/cesium/... — a nesting quirk of their build;
  * rewrites their own /api/* calls to /api/gev/* so they cannot collide with
    this project's dashboard API on the same origin. The rewrite is guarded by
    a negative lookbehind so external provider URLs that merely contain
    "/api/" (NASA FIRMS, Photon, translink GTFS, NASA earthdata) are untouched;
  * writes the manifest, including the action + layer inventory that JARVIS
    drives the app with.

Layout produced here:
  godseye/                 the built browser application (served at /godseye/)
  godseye/_server/         their vite config + API server sources, so the Space
                           image never has to reach GitHub — only npm
  godseye/MANIFEST.json    upstream commit, licence, action/layer inventory

The Space image installs Node, runs `npm ci` against _server/package-lock.json,
and `vite preview` serves both the static app and their /api/* routes; this
project proxies /api/gev/* through to it.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "godseye"

UPSTREAM = "https://github.com/bilawalsidhu/gods-eye-view.git"
# The exact commit this was built and verified against. Bump deliberately.
PIN = "b210ab0fe4d71c7faa0268134e0aa5f3c53fc7fe"
PIN_TAG = "main@b210ab0"

#: Verified by introspecting the built bundle's actionExecutor/runner at PIN.
#: JARVIS speaks this vocabulary; core/godseye.py maps intents onto it and
#: e2e/test_godseye.py asserts every name here still exists in the bundle.
ACTIONS = [
    "adjust_camera_zoom", "analyst_query", "annotate_map", "clear_annotations",
    "control_cctv", "control_cockpit", "control_radio", "control_scene",
    "fly_route", "fly_to_location", "frame_overhead", "get_current_view_state",
    "get_entity_context", "move_camera", "next_iss_pass", "next_satellite_pass",
    "select_nearest_aircraft", "set_context_mode", "set_cyber_sonar",
    "set_detection", "set_hud", "set_layer_visibility", "set_map_stack",
    "set_panel_open", "set_post_processing", "set_visual_style",
    "show_data_layers_menu", "stop_tracking", "track_entity", "zoom_to_globe",
]

#: Layer ids as they appear in dataManager.layers (a Map, so not enumerable
#: from Object.keys — captured at runtime at PIN).
LAYERS = [
    "bhote-koshi-2026", "bhote-koshi-locator", "flights", "military",
    "local-adsb", "earthquakes", "fire-perimeters", "alpr-cameras", "satellites",
    "rocket-launches", "traffic", "cctv", "radio", "transit", "bikeshare",
    "directions", "recent-imagery", "ais-live-vessels", "military-installations",
    "military-awareness", "wind", "weather-radar", "weather-satellite",
    "weather-lightning", "weather-cyclones", "local-datacenters", "local-dams",
    "telegeography-submarine-cables", "local-firms",
]

#: Their first-launch menu, in order. We default to a clean globe (the user
#: chose EXPLORE MANUALLY), so the others are listed for completeness.
FIRST_LAUNCH = ["live-contacts", "space-missions", "environmental",
                "explore-manually"]

#: /api/ paths that are NOT ours and must survive the rewrite untouched.
PROTECTED_HOSTS = ["firms.modaps.eosdis.nasa.gov", "gtfsrt.api.translink.com.au",
                   "photon.komoot.io", "wvs.earthdata.nasa.gov"]

# A /api/ directly preceded by a hostname character belongs to a third party.
REWRITE = re.compile(r'(?<![A-Za-z0-9.\-])/api/')


def run(cmd: list[str], cwd: Path, timeout: int = 1800) -> str:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode != 0:
        sys.stderr.write(p.stdout[-3000:] + "\n" + p.stderr[-3000:] + "\n")
        raise SystemExit(f"FAILED: {' '.join(cmd)}")
    return p.stdout


def flatten(dist: Path) -> None:
    """dist/godseye/* holds their bundled Cesium; index.html expects it at
    /godseye/cesium/*. Merge it up a level so one directory serves both."""
    nested = dist / "godseye"
    if nested.is_dir():
        for item in nested.iterdir():
            shutil.move(str(item), str(dist / item.name))
        nested.rmdir()


def rewrite_api(dist: Path) -> dict:
    """Move their /api/* calls under /api/gev/* so they cannot collide with
    the dashboard API on the same origin. External provider URLs are exempt."""
    touched, skipped_external = 0, 0
    for js in [p for p in dist.rglob("*.js") if "_server" not in p.parts]:
        text = original = js.read_text(encoding="utf-8", errors="replace")
        # count protected URLs before rewriting
        for host in PROTECTED_HOSTS:
            skipped_external += original.count(f"{host}/api/")
        new = REWRITE.sub("/api/gev/", text)
        if new != text:
            touched += 1
            js.write_text(new, encoding="utf-8")
    return {"files_rewritten": touched, "external_urls_protected": skipped_external}


def _browser_js(root: Path):
    """Only the built browser app. _server/ is their Node source, which defines
    /api/* routes on purpose — the proxy strips the /gev prefix, so those paths
    must stay exactly as upstream ships them."""
    return [p for p in root.rglob("*.js")
            if "_server" in p.parts and "dist" in p.parts
            and "node_modules" not in p.parts]


def app_dir(root: Path) -> Path:
    """The built browser app. It lives under _server/dist because that is where
    `vite preview` serves from — putting it anywhere else means their server
    answers /api/* perfectly while 404ing every asset."""
    return root / "_server" / "dist"


def verify(root: Path) -> list[str]:
    dist = app_dir(root)
    problems = []
    if not (dist / "index.html").exists():
        problems.append("index.html missing")
    if not (dist / "cesium" / "Cesium.js").exists():
        problems.append("bundled Cesium missing at dist/cesium/Cesium.js")
    blob = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in _browser_js(dist))
    for host in PROTECTED_HOSTS:
        if f"{host}/api/" not in blob:
            problems.append(f"external provider URL clobbered: {host}")
    leftover = re.findall(r'(?<![A-Za-z0-9.\-])/api/(?!gev/)', blob)
    if leftover:
        problems.append(f"{len(leftover)} un-namespaced /api/ call(s) remain")
    # their index.html points two icons at the site root; ours is mounted at
    # /godseye/, so rewrite them onto the mount
    idx = dist / "index.html"
    if idx.exists():
        html = idx.read_text(encoding="utf-8", errors="replace")
        fixed = re.sub(r'((?:src|href)=")/(logo\.svg|mic\.svg)', r'\1/godseye/\2', html)
        if fixed != html:
            idx.write_text(fixed, encoding="utf-8")
    missing = [a for a in ACTIONS if f'"{a}"' not in blob]
    if missing:
        problems.append(f"actions missing from bundle: {missing[:6]}")
    # the Node side must be vendored too, or the Space cannot run their API
    srv = root / "_server"
    for need in ("package.json", "package-lock.json", "vite.config.js",
                 "server/standalone/vite.config.js", "dist/index.html"):
        if not (srv / need).exists():
            problems.append(f"_server missing {need}")
    if (root / "MANIFEST.json").exists():
        mf = json.loads((root / "MANIFEST.json").read_text())
        if mf.get("commit") != PIN:
            problems.append(f"MANIFEST commit {mf.get('commit')} != pinned {PIN}")
    return problems


def build(dest: Path) -> dict:
    tmp = Path("/tmp") / f"gev_build_{int(time.time())}"
    tmp.mkdir(parents=True)
    try:
        run(["git", "clone", "--depth", "1", UPSTREAM, "src"], tmp, timeout=600)
        work = tmp / "src"
        got = run(["git", "rev-parse", "HEAD"], work).strip()
        if got != PIN:
            raise SystemExit(f"upstream moved: got {got}, pinned {PIN}")
        run(["npm", "ci", "--no-audit", "--no-fund", "--engine-strict=false"],
            work, timeout=1800)
        # --base is the mount point: every asset ref becomes /godseye/...
        # so the dashboard can serve the app itself and never has to
        # proxy HTML to a server whose own base disagrees.
        run(["npx", "vite", "build", "--base=/godseye/"], work, timeout=1800)
        dist = work / "dist"
        flatten(dist)
        out_dist = dest / "_server" / "dist"
        stats = rewrite_api(dist)
        problems = verify(dist)
        if problems:
            raise SystemExit("verification failed:\n  - " + "\n  - ".join(problems))
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.mkdir()
        shutil.copytree(dist, out_dist)
        # The Node side: their vite config resolves paths relative to their repo
        # root, so these have to sit together. Node itself and node_modules are
        # NOT vendored (312MB, and npm is a normal build dependency) — the image
        # runs `npm ci` at the pinned lockfile.
        server_root = out_dist.parent
        server_root.mkdir(exist_ok=True)
        for item in ("package.json", "package-lock.json", "build", "server",
                     "config", "src", "scripts", "tools", "index.html",
                     "style.css", "vite.config.js", ".env.example"):
            srcp = work / item
            if srcp.is_dir():
                shutil.copytree(srcp, server_root / item,
                                ignore=shutil.ignore_patterns("node_modules", ".git"))
            elif srcp.is_file():
                shutil.copy2(srcp, server_root / item)
        size = sum(f.stat().st_size for f in dest.rglob("*")
                   if f.is_file() and "node_modules" not in f.parts)
        return {"commit": got, "bytes": size, **stats,
                "actions": len(ACTIONS), "layers": len(LAYERS)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="verify the committed build without touching the network")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    dest = Path(args.out)

    if args.check:
        if not dest.exists():
            print(f"x nothing vendored at {dest}")
            return 1
        problems = verify(dest)
        print(f"vendored: {dest}  "
              f"({sum(f.stat().st_size for f in dest.rglob('*') if f.is_file() and 'node_modules' not in f.parts)/1e6:.1f} MB)")
        for p in problems:
            print(f"  x {p}")
        print("VERIFY OK" if not problems else "VERIFY FAILED")
        return 1 if problems else 0

    print(f"building God's Eye View @ {PIN_TAG} …")
    info = build(dest)
    manifest = {
        "project": "God's Eye View",
        "upstream": "https://github.com/bilawalsidhu/gods-eye-view",
        "commit": info["commit"],
        "pinned_as": PIN_TAG,
        "code_license": "MIT (Copyright (c) 2026 Bilawal Sidhu)",
        "data_terms": "Per-dataset; see upstream DATA_SOURCES.md. "
                      "telegeography-submarine-cables is CC BY-NC-SA "
                      "(non-commercial) — this build is for personal use only.",
        "api_namespace": "/api/gev/* -> proxied to the vendored Node server",
        "actions": ACTIONS,
        "layers": LAYERS,
        "first_launch": FIRST_LAUNCH,
        "bytes": info["bytes"],
        "files_rewritten": info["files_rewritten"],
        "external_urls_protected": info["external_urls_protected"],
        "built_by": "scripts/build_godseye.py",
    }
    (dest / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                        encoding="utf-8")
    print(f"vendored {info['bytes']/1e6:.1f} MB -> {dest}/_server/dist")
    print(f"  commit           {info['commit']}")
    print(f"  files rewritten  {info['files_rewritten']}")
    print(f"  external /api/   {info['external_urls_protected']} protected")
    print(f"  actions / layers {info['actions']} / {info['layers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
