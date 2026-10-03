"""Make a hand-composed clip into a welcome sting.

The music is composed once, by hand, with Lyria or anything else — this
project deliberately does not generate audio at runtime. A clap cannot wait on
a network call and cannot cost money, and a welcome that depends on an outside
service is a welcome with a single point of failure. So the music arrives as a
file, and the only job here is to make that file behave.

A composed clip is raw material: usually 30 seconds of music written to build
to a payoff, which is wrong for a greeting in two specific ways.

    It is the wrong SHAPE. The payoff lands late, so a naive cut at the length
    the ceremony wants throws away the best part and keeps the run-up.

    It is the wrong LEVEL. Clip exports are not mixed for playing quietly
    under a voice, and the first version of this used ffmpeg's single-pass
    `loudnorm` — which is a dynamic normaliser that needs the material's
    loudness measured in advance. Given a quiet clip it estimated wrong and
    attenuated it FURTHER: -38.9 dBFS in, -45.9 dBFS out. A welcome sting you
    cannot hear is worse than no sting. `linear=true` does not help;
    `dynaudnorm` overshoots and pumps.

So the shape is chosen by measurement (RMS per half second, preferring the
window that lifts) and the level is applied as one constant gain with a
limiter after it. Predictable, no pumping, and testable against known input.

The original file is never modified. Conditioning writes a sibling, so an
upload is reversible and nothing anyone handed us is destroyed.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path
from typing import Optional

#: How long the welcome sting is. Also the cap the panel stops playback at.
TARGET_SECONDS = 22

#: The level it is set to, in plain RMS dBFS: loud enough to be felt, quiet
#: enough to sit under a voice without competing with it.
TARGET_RMS_DB = -16.0

#: Loudness-compatible formats the panel can play.
ACCEPTED = (".mp3", ".wav", ".ogg", ".m4a", ".webm", ".flac", ".aac", ".opus")


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def profile(path: Path, want: int = TARGET_SECONDS) -> tuple[float, float]:
    """One decode, two answers: where the build peaks, and how loud it is.

    Decodes to 8k mono and takes RMS per half second. Returns
    `(start_offset, rms_dbfs_of_that_window)`.

    Measured rather than assumed because both of these were wrong the first
    time. Taking the first N seconds put the payoff after the cut, and asking
    ffmpeg to normalise in one pass made a quiet track QUIETER — a welcome
    sting you cannot hear is worse than no sting.
    """
    empty = (0.0, -60.0)
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar",
             "8000", "-f", "s16le", "-"],
            capture_output=True, timeout=90).stdout
        if len(raw) < 4000:
            return empty
        win = 4000                      # half a second at 8k
        n = len(raw) // 2
        samples = memoryview(raw).cast("h")
        rms: list[float] = []
        for i in range(0, n - win, win):
            acc = 0.0
            for v in samples[i:i + win]:
                f = v / 32768.0
                acc += f * f
            rms.append(math.sqrt(acc / win))
        length = max(2, int(want / 0.5))
        if len(rms) < length:
            return empty

        # Score on how LOUD the window ENDS, with a mild bonus for lift.
        #
        # Ranking by lift alone was wrong and measurably so: on a clip that
        # builds, the window starting at zero has the biggest ratio (quiet
        # head, louder tail) and so always won — which threw away the payoff
        # and kept the run-up. A sting wants to ARRIVE. What matters is where
        # the cut lands, and it should land on the loudest moment there is.
        best_at, best_score = 0.0, None
        for i in range(0, len(rms) - length + 1):
            seg = rms[i:i + length]
            head = sum(seg[:10]) / 10 or 1e-6
            tail = sum(seg[-5:]) / 5
            score = tail * (1.0 + 0.25 * min(2.0, tail / head))
            if best_score is None or score > best_score:
                best_score, best_at = score, i * 0.5
        window = rms[int(best_at / 0.5):int(best_at / 0.5) + length]
        mean = sum(window) / len(window)
        return float(best_at), 20.0 * math.log10(mean + 1e-9)
    except Exception:
        return empty


def _rms_db(path: Path) -> float:
    """RMS dBFS of a file, decoded exactly as a player would receive it.

    No rate conversion and no downmix, on purpose. Every earlier measurement
    disagreed with the artefact by a couple of dB — 8k mono threw away
    everything above 4kHz, and a mono downmix under-read a wide stereo mix —
    and each fix moved the error somewhere else. Measuring the finished file
    the way it will be played has no such gap left to fall into.
    """
    try:
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-"],
            capture_output=True, timeout=120).stdout
        n = len(raw) // 2
        if n < 2000:
            return -60.0
        acc = 0.0
        for v in memoryview(raw).cast("h"):
            f = v / 32768.0
            acc += f * f
        return 20.0 * math.log10(math.sqrt(acc / n) + 1e-9)
    except Exception:
        return -60.0


def _render(src: Path, dst: Path, start: float, seconds: float,
            gain_db: float) -> None:
    # 1.2s, not 2.2: the cut is placed on the loudest moment in the window, so
    # a long fade would immediately take the edge off the very thing it is
    # there to deliver. Long enough to avoid a click, short enough to land.
    fade_out_start = max(0.0, seconds - 1.2)
    af = (f"atrim={start:.2f}:{start + seconds},asetpts=N/SR/TB,"
          f"afade=t=in:st=0:d=0.30,"
          f"afade=t=out:st={fade_out_start:.2f}:d=1.2,"
          f"volume={gain_db:.2f}dB,"
          f"alimiter=limit=0.95:level=disabled")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-af", af,
         "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "44100", str(dst)],
        capture_output=True, timeout=240, check=True)


def condition(src: Path, dst: Path, seconds: int = TARGET_SECONDS) -> dict:
    """Trim, fade and level a clip. Returns what it did and why.

    Two passes, and the second one exists because the first could not be
    trusted. The level has to be a constant gain rather than a normaliser —
    ffmpeg's single-pass `loudnorm` is dynamic, needs the material's loudness
    measured up front, and given a quiet clip attenuated it FURTHER: measured
    at -38.9 dBFS in, -45.9 dBFS out, which is a welcome you cannot hear.

    But a gain worked out from a MEASUREMENT is only as good as the
    measurement, and three different ways of measuring this file each
    disagreed by around 2 dB. So the level is settled empirically: render
    once, measure the file that came out, and re-render with the difference.
    A constant gain converges on the first correction, and the number reported
    is the one the finished file actually measures.
    """
    if not have_ffmpeg():
        return {"ok": False, "error": "ffmpeg is missing from the image"}
    start, _ = profile(src, seconds)
    try:
        _render(src, dst, start, seconds, 0.0)
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": "could not process the audio: "
                  + (e.stderr or b"").decode("utf-8", "ignore")[:140]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:140]}
    if not dst.is_file() or dst.stat().st_size < 4096:
        return {"ok": False, "error": "the processed file came out empty"}

    first = _rms_db(dst)
    correction = max(-24.0, min(36.0, TARGET_RMS_DB - first))
    if abs(correction) > 0.25:
        try:
            _render(src, dst, start, seconds, correction)
        except Exception as e:
            # the first render is already on disk and playable, so a failed
            # correction is not a failure of the whole thing
            return {"ok": True, "start": round(start, 2),
                    "gain_db": 0.0, "measured": round(first, 1),
                    "target": TARGET_RMS_DB, "corrected": False,
                    "note": f"level correction failed ({type(e).__name__}); "
                            f"the ungained cut is playable",
                    "bytes": dst.stat().st_size}
    final = _rms_db(dst)
    return {"ok": True, "start": round(start, 2),
            "gain_db": round(correction, 2), "measured": round(first, 1),
            "final_rms": round(final, 1), "target": TARGET_RMS_DB,
            "bytes": dst.stat().st_size}


def prepare(src: Path, dst: Path, seconds: int = TARGET_SECONDS) -> dict:
    """Condition a clip, falling back to using it untouched.

    A failed condition must never cost the user their music: if ffmpeg cannot
    do it, the original is still perfectly playable and the panel already
    stops playback at the right moment.
    """
    r = condition(src, dst, seconds)
    if r.get("ok"):
        return r
    try:
        shutil.copyfile(src, dst)
    except Exception as e:
        return {"ok": False, "error": f"{r.get('error')} — and the original "
                  f"could not be copied either: {e}"[:160]}
    return {"ok": True, "conditioned": False, "reason": r.get("error"),
            "bytes": dst.stat().st_size}
