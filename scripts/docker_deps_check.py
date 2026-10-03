#!/usr/bin/env python3
"""Build-time gate for the JARVIS container.

The previous image shipped without one, which is how a missing `libGL` turned
into a dashboard where every screenshot 500'd — discovered by a user, in
production, rather than by the build. This turns that class of failure into a
red build instead.

It checks two things, and the split matters:

  * **python imports — reported by default, fatal only with `--strict`.**
    This was originally fatal, which was wrong for this image. The Dockerfile
    it lives in records that a fatal build step once took the Space down for
    everyone, and a Space that does not build is a Space that serves nothing:
    a missing `pdfplumber` costs one file reader, while a failed build costs
    the whole assistant. So the default run prints a loud, greppable summary
    and exits 0, and `--strict` is available for CI or a local build where a
    red build is cheap.
  * **binaries — always reported, never fatal.** node, opencode and ffmpeg are
    installed non-fatally by design, so failing on them would contradict that.

Deliberately not a shell script and not a Dockerfile heredoc: heredoc support
in `RUN` needs BuildKit, and a build gate that silently does not run on an
older daemon is worse than no gate at all.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from typing import Optional

#: Imported by main.py, core/, actions/, or dashboard/ at startup or at tool
#: discovery. Missing any of these is a container that boots and misbehaves.
FATAL = [
    "fastapi", "uvicorn", "httpx", "numpy", "psutil", "requests", "bs4",
    "ddgs", "playwright", "cv2", "PIL", "pdfplumber", "pypdf", "docx", "pptx",
    "openpyxl", "mss", "google.genai", "edge_tts", "miniaudio",
    "pywebpush", "cryptography", "tinytuya", "paho.mqtt",
]

#: Installed non-fatally. Reported so a degraded container is visible in the
#: build log rather than only in a support ticket.
SOFT = ["node", "opencode", "ffmpeg", "git"]


def _version(binary: str) -> str:
    exe = shutil.which(binary)
    if not exe:
        return "MISSING"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=25)
        return (out.stdout or out.stderr).strip().splitlines()[0][:60]
    except Exception as e:
        return f"present ({type(e).__name__} reading version)"


def main(argv: Optional[list] = None) -> int:
    strict = "--strict" in (argv if argv is not None else sys.argv[1:])
    broken: list[str] = []
    for mod in FATAL:
        try:
            importlib.import_module(mod)
        except Exception as e:
            broken.append(f"{mod} ({type(e).__name__}: {e})")

    for b in SOFT:
        print(f"[deps] {b:10} {_version(b)}")

    if broken:
        # stdout is block-buffered when piped into a build log, so without this
        # the warning block appears BELOW the lines that explain it.
        sys.stdout.flush()
        label = "FATAL" if strict else "WARNING"
        print(f"\n[deps] {label} — {len(broken)} module(s) do not import:",
              file=sys.stderr)
        for b in broken:
            print(f"  - {b}", file=sys.stderr)
        if strict:
            return 1
        print("[deps] continuing: the build is a production image, and an "
              "absent package disables one feature rather than the whole "
              "assistant. Re-run with --strict to fail instead.", file=sys.stderr)
        return 0

    print(f"[deps] all {len(FATAL)} critical modules import cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
