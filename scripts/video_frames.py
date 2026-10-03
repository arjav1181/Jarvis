"""
scripts/video_frames.py — pull frames (and audio) out of a dropped video.

Used when a video is handed over for review: the point is to SEE it, not to
skim a transcript. Frames go to .inbox/frames/ at three densities so nothing
important is missed:

  * evenly spaced  — the shape of the whole thing, so the plan matches the
    narrative rather than the loudest ten seconds;
  * scene cuts     — where the content actually changes;
  * dense in the opening and around scene changes — where a demo explains
    itself.

    python3 scripts/video_frames.py .inbox/v.mp4 --every 12 --scenes 0.3
    python3 scripts/video_frames.py .inbox/v.mp4 --transcribe
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def grab(path: Path, out_dir: Path, *, every: int = 12, scenes: float = 0.3,
         dense: int = 30) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[str] = []
    runs = [
        (["-vf", f"fps=1/{max(1, every)}", "-q:v", "3"], "even"),
        (["-vf", f"select='gt(scene,{scenes})',scale=960:-1", "-vsync", "vfr",
          "-q:v", "3"], "scene"),
        (["-t", str(max(20, dense * 2)), "-vf", f"fps=1/{max(1, dense)}",
          "-q:v", "3"], "dense"),
    ]
    for args, kind in runs:
        d = out_dir / kind
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(path), *args,
             "-q:v", "3", str(d / f"{kind}_%04d.jpg")], check=False)
        made += sorted(str(p) for p in d.glob("*.jpg"))
    return len(made)


def audio(path: Path, out_dir: Path) -> str | None:
    wav = out_dir / "audio.wav"
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn",
         "-ac", "1", "-ar", "16000", str(wav)], check=False)
    return str(wav) if wav.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--every", type=int, default=12, help="seconds between even frames")
    ap.add_argument("--scenes", type=float, default=0.3, help="scene-change threshold")
    ap.add_argument("--dense", type=int, default=30,
                    help="seconds sampled at a high rate (the opening/demo bits)")
    ap.add_argument("--transcribe", action="store_true", help="run faster-whisper on the audio")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    path = Path(a.video)
    if not path.exists():
        print(f"no such file: {path}")
        return 1
    info = probe(path)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    dur = float(info["format"].get("duration") or 0)
    print(f"{path.name}: {dur:.0f}s, {v.get('width')}x{v.get('height')}, "
          f"has_audio={any(s['codec_type'] == 'audio' for s in info['streams'])}")
    out = Path(a.out) if a.out else path.parent / "frames"
    n = grab(path, out, every=a.every, scenes=a.scenes, dense=a.dense)
    print(f"frames: {n} written to {out}")
    if a.transcribe:
        wav = audio(path, out)
        if not wav:
            print("no audio track")
            return 0
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segs, _ = model.transcribe(wav)
            text = " ".join(s.text.strip() for s in segs)
            (out / "transcript.txt").write_text(text, encoding="utf-8")
            print(f"transcript: {len(text)} chars -> {out / 'transcript.txt'}")
        except Exception as e:
            print(f"transcription failed: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
