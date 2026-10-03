#!/usr/bin/env python3
"""Build the welcome sting that ships in the repository.

The track is a binary file in git, which is exactly the kind of thing nobody
can review six months later: nobody knows where it came from, what was done to
it, or whether it still matches the code. This script is the receipt.

    python tools/prepare_welcome_music.py <source.mp3> [--out PATH]

The source is the unmodified upstream file. Everything this does to it — which
window, the fades, the level — is the same `core.audio.condition()` an upload
goes through, so the track in the repo and the track a user uploads are made by
identical code. That is deliberate: if the pipeline ever changes, the shipped
track can be rebuilt to match instead of quietly diverging from it.

The source is never modified, and the licence travels with the output in
CREDITS.md rather than being assumed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import audio as _audio  # noqa: E402

DEFAULT_OUT = ROOT / "dashboard" / "static" / "welcome.mp3"
SAMPLE = 22


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", help="the unmodified upstream audio file")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--seconds", type=int, default=SAMPLE)
    args = ap.parse_args(argv)

    src = Path(args.source)
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        return 1
    if not _audio.have_ffmpeg():
        print("ffmpeg is not installed, so this cannot run", file=sys.stderr)
        return 1

    before = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
        capture_output=True, text=True, timeout=90).stdout.strip()
    print(f"  source : {src.name}  ({int(src.stat().st_size)} bytes, "
          f"{before or '?'}s)")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    r = _audio.condition(src, out, args.seconds)
    if not r.get("ok"):
        print(f"  FAILED : {r.get('error')}", file=sys.stderr)
        return 1
    after = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
        capture_output=True, text=True, timeout=90).stdout.strip()
    print(f"  window : {r['start']}s -> {r['start'] + args.seconds}s "
          f"(the cut lands on the loudest moment)")
    print(f"  level  : first pass measured {r.get('measured')} dBFS, "
          f"{r['gain_db']} dB correction applied")
    print(f"           final file measures {r.get('final_rms')} dBFS "
          f"(target {_audio.TARGET_RMS_DB})")
    print(f"  output : {out.relative_to(ROOT)}  ({r['bytes']} bytes, "
          f"{after or args.seconds}s)")
    print("\n  Reminder: the source is CC BY 4.0 and the credit in CREDITS.md "
          "is an obligation, not a courtesy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
