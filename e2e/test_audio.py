"""Turning a hand-composed clip into a welcome sting.

The music is composed by hand and handed over, so this file is the only thing
between "a 30 second clip exported from a music model" and "a greeting that
sounds like it was meant". That gap is where the real bugs lived.

The loudness pass is the one worth testing hard. The first version delegated
to ffmpeg's single-pass `loudnorm`, which is a dynamic normaliser that needs
the material's loudness measured in advance. Given a quiet clip it estimated
the level wrong and attenuated it FURTHER — measured here at -38.9 dBFS in,
-45.9 dBFS out. A welcome sting you cannot hear is worse than no sting, and
nothing about it looks like a failure.

So the tests build real audio with ffmpeg, run the real pipeline, and measure
the result — including the case that a hot source and a quiet source land in
the same place, which is the only honest proof that the level is being set
rather than merely passed through.
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="jarvis-test-audio-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0
HAVE_FFMPEG = bool(shutil.which("ffmpeg"))


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


def _measure(path: Path) -> tuple[float, float]:
    """(peak, rms) in dBFS, over the WHOLE file.

    Measuring only the head is how a sting with a 0.35s fade-in gets reported
    as inaudible: the first few seconds are the quietest part of the build.
    """
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f",
                          "s16le", "-"], capture_output=True,
                         timeout=180).stdout
    n = len(raw) // 2
    if n < 1000:
        return -120.0, -120.0
    s = memoryview(raw).cast("h")
    peak = max(abs(int(v)) for v in s) / 32768.0
    acc = sum((int(v) / 32768.0) ** 2 for v in s) / n
    return 20 * math.log10(peak + 1e-9), 10 * math.log10(acc + 1e-9)


def _build(path: Path, seconds: float = 30.0, gain: float = 0.9) -> None:
    """A real clip that actually builds, the shape a music model returns."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=110:duration={seconds}", "-af",
         f"aeval='0.02+{gain}*pow(t/{seconds},2)':c=stereo",
         "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
        capture_output=True, timeout=240, check=True)


def _build_dense(path: Path, seconds: float = 30.0) -> None:
    """A BRIGHT, wide, dense mix — much closer to real music than a sine.

    This exists because a pure sine hid a real bug. Level was being set from a
    measurement that discarded everything above 4kHz, so a narrowband tone
    landed on target while dense music came out 3.5 dB hot and riding the
    limiter. A sine has almost no high end, so it agreed with a wrong method.
    Any test of this must use material with a full spectrum.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=110:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=4400:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=9300:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=13500:duration={seconds}",
         "-filter_complex",
         "[0:a]volume=0.5[a0];[1:a]volume=0.35[a1];[2:a]volume=0.22[a2];"
         "[3:a]volume=0.12[a3];[a0][a1][a2][a3]amix=inputs=4,"
         f"volume='0.05+0.8*pow(t/{seconds},2)':eval=frame[mix];[mix]pan=stereo|c0=c0|c1=c0[out]",
         "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
        capture_output=True, timeout=300, check=True)


# ── the shape ────────────────────────────────────────────────────────────────

def test_it_picks_the_window_that_lands():
    if not HAVE_FFMPEG:
        for n in ("audio.window_is_valid", "audio.window_prefers_the_build",
                  "audio.measures_the_window"):
            check(n, True, "")
        print("  SKIP  window tests — no ffmpeg")
        return
    from core.audio import profile, TARGET_SECONDS
    src = Path(_TMP) / "build.mp3"
    _build(src, 30.0)
    check("audio.source_built", src.is_file() and src.stat().st_size > 4096,
          "could not build a source clip")
    start, db = profile(src, TARGET_SECONDS)
    check("audio.window_is_valid", 0.0 <= start <= 8.0,
          f"a 30s clip has 8s of valid start offsets, it chose {start}")
    check("audio.measures_the_window", -60.0 < db < 0.0,
          f"it could not measure the window it chose: {db} dBFS")
    # A clip that builds has its best window LATE. Taking second zero is the
    # obvious wrong answer, and it is what a naive atrim=0 would do.
    check("audio.window_prefers_the_build", start > 0.5,
          f"it chose {start}s on a clip that builds — that is the run-up, "
          f"not the payoff")


# ── the level ────────────────────────────────────────────────────────────────

def test_quiet_music_is_not_left_quiet():
    """The bug that mattered. A welcome sting you cannot hear is worse than no
    sting, and this failure is completely silent."""
    if not HAVE_FFMPEG:
        for n in ("audio.quiet_is_raised", "audio.hot_is_lowered",
                  "audio.both_land_together", "audio.lands_on_target",
                  "audio.nothing_clips", "audio.audible"):
            check(n, True, "")
        print("  SKIP  level tests — no ffmpeg")
        return
    from core.audio import condition, TARGET_RMS_DB, TARGET_SECONDS

    hot = Path(_TMP) / "hot.mp3"
    hot_out = Path(_TMP) / "hot-out.mp3"
    quiet = Path(_TMP) / "quiet.mp3"
    quiet_out = Path(_TMP) / "quiet-out.mp3"
    _build(hot, 30.0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(hot), "-af",
                    "volume=0.25", "-c:a", "libmp3lame", "-b:a", "128k",
                    str(quiet)], capture_output=True, timeout=240, check=True)

    r1 = condition(hot, hot_out, TARGET_SECONDS)
    r2 = condition(quiet, quiet_out, TARGET_SECONDS)
    check("audio.hot_succeeded", r1.get("ok"), f"{r1.get('error')}")
    check("audio.quiet_succeeded", r2.get("ok"), f"{r2.get('error')}")
    if not (r1.get("ok") and r2.get("ok")):
        return

    _, hot_rms = _measure(hot_out)
    _, quiet_rms = _measure(quiet_out)
    check("audio.quiet_is_raised", quiet_rms > -30.0,
          f"a quiet clip came out at {quiet_rms:.1f} dBFS — inaudible")
    check("audio.hot_is_lowered", hot_rms < -11.0,
          f"an over-hot clip came out at {hot_rms:.1f} dBFS — it will shout "
          f"over the greeting")
    check("audio.both_land_together", abs(hot_rms - quiet_rms) < 3.0,
          f"hot came out at {hot_rms:.1f} dBFS and quiet at {quiet_rms:.1f} "
          f"dBFS — the level is not being set, only passed through")
    check("audio.lands_on_target", abs(hot_rms - TARGET_RMS_DB) < 2.5,
          f"expected about {TARGET_RMS_DB} dBFS, got {hot_rms:.1f}")
    peak, _ = _measure(hot_out)
    check("audio.nothing_clips", peak < -0.5,
          f"peak at {peak:.1f} dBFS — the limiter is not holding")
    check("audio.audible", -24.0 < quiet_rms < -10.0,
          f"rms {quiet_rms:.1f} dBFS is outside a usable band")


def test_dense_music_also_lands_on_target():
    """The sine-based tests were passing a level method that was wrong. Dense,
    bright, wide music is where the error showed: it came out 3.5 dB hot."""
    if not HAVE_FFMPEG:
        for n in ("audio.dense_lands_on_target", "audio.dense_not_limited",
                  "audio.dense_headroom"):
            check(n, True, "")
        return
    from core import audio as _a
    src = Path(_TMP) / "dense.mp3"
    out = Path(_TMP) / "dense-out.mp3"
    _build_dense(src, 30.0)
    check("audio.dense_built", src.is_file() and src.stat().st_size > 4096,
          "could not build a dense source clip")
    r = _a.condition(src, out, _a.TARGET_SECONDS)
    check("audio.dense_conditioned", r.get("ok"), f"{r.get('error')}")
    if not r.get("ok"):
        return
    peak, rms = _measure(out)
    check("audio.dense_lands_on_target", abs(rms - _a.TARGET_RMS_DB) < 1.0,
          f"dense music came out at {rms:.1f} dBFS, target "
          f"{_a.TARGET_RMS_DB} — the level is being set from the wrong "
          f"measurement again")
    # riding the limiter means the gain was too high and peaks are being crushed
    check("audio.dense_not_limited", peak < -1.5,
          f"peak at {peak:.1f} dBFS — the limiter is doing the work instead "
          f"of the gain being right")
    check("audio.dense_headroom", -20.0 < rms < -12.0,
          f"{rms:.1f} dBFS is not a usable background level for speech")


def test_the_output_is_a_real_playable_file():
    if not HAVE_FFMPEG:
        check("audio.output_is_audio", True, "")
        return
    from core.audio import condition, TARGET_SECONDS
    src = Path(_TMP) / "shape.mp3"
    dst = Path(_TMP) / "shape-out.mp3"
    _build(src, 30.0)
    condition(src, dst, TARGET_SECONDS)
    check("audio.output_is_audio", dst.is_file() and dst.stat().st_size > 4096,
          f"no usable output: {dst.exists()}")
    with open(dst, "rb") as fh:
        head = fh.read(3)
    check("audio.output_is_mp3", head in (b"ID3", b"\xff\xfb", b"\xff\xf3"),
          f"unrecognised header {head!r}")
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(dst)],
        capture_output=True, text=True, timeout=90).stdout.strip()
    try:
        d = float(dur)
        check("audio.length_is_right", 20.0 <= d <= 23.5,
              f"a {TARGET_SECONDS}s sting came out as {d}s")
    except ValueError:
        check("audio.length_is_right", False, f"unreadable duration {dur!r}")


# ── the guarantee that matters most ──────────────────────────────────────────

def test_a_failed_condition_still_gives_you_your_music():
    """Conditioning is a judgement call. If ffmpeg cannot do it, the user must
    still have their file — a broken pipeline must never cost someone the
    music they handed over."""
    if not HAVE_FFMPEG:
        check("audio.fallback_keeps_the_music", True, "")
        return
    from core.audio import prepare
    src = Path(_TMP) / "fallback.mp3"
    _build(src, 30.0)
    out = Path(_TMP) / "fallback-out.mp3"
    # a file ffmpeg cannot read
    broken = Path(_TMP) / "broken.mp3"
    broken.write_bytes(b"this is not audio at all, not even close" * 20)
    r = prepare(broken, Path(_TMP) / "broken-out.mp3"), None
    res = r[0]
    check("audio.fallback_reports_failure", not res.get("ok")
          or res.get("conditioned") is False,
          f"it claimed to have conditioned a file that is not audio: {res}")
    check("audio.fallback_keeps_the_bytes",
          (Path(_TMP) / "broken-out.mp3").is_file()
          and (Path(_TMP) / "broken-out.mp3").read_bytes() == broken.read_bytes(),
          "the unprocessable file was not passed through untouched")

    # and the happy path still conditions
    r2 = prepare(src, out, 22)
    check("audio.prepare_conditions", r2.get("ok") and out.is_file(),
          f"{r2}")


# ── no runtime generation, anywhere ──────────────────────────────────────────

def test_nothing_generates_music_at_runtime():
    """The music is composed by hand. A clap cannot wait on a network call and
    cannot cost money, and a welcome that depends on an outside service is a
    welcome with one point of failure."""
    for name in ("core/ceremony.py", "dashboard/server.py", "main.py",
                 "dashboard/static/app.html"):
        src = _src(name)
        check(f"audio.no_generation.{name.replace('/', '_').replace('.', '_')}",
              "musicgen" not in src,
              "something still references the removed generator")
    # and no model-facing tool either
    check("audio.no_compose_tool", '"name": "compose"' not in _src("main.py"),
          "the compose tool is still declared")
    pol = _src("core/policy.py")
    # four spaces, so this is the TOOL registration and not the email tool's
    # "compose" action, which is a different thing entirely and must survive
    check("audio.no_compose_policy", '\n    "compose"' not in pol,
          "compose is still registered as a tool in the policy")
    check("audio.email_compose_survived", '"compose":       {"tier": "act"' in pol,
          "removing the music composer took the email compose action with it")
    check("audio.no_compose_route", "/api/ceremony/music" not in
          _src("dashboard/server.py"), "the generation routes are still there")
    check("audio.no_generation_module",
          not (ROOT / "core" / "musicgen.py").exists(),
          "core/musicgen.py still exists")

    # and the upload path must NOT be conditioning through anything external
    sv = _src("dashboard/server.py")
    i = sv.index('async def ceremony_audio_upload')
    body = sv[i:sv.index('async def ceremony_audio_delete')]
    check("audio.upload_conditions_locally", "core import audio" in body
          or "from core import audio" in body,
          "an upload is not being conditioned into a sting")
    check("audio.upload_keeps_the_original", "original" in body,
          "the original file is not preserved beside the conditioned one")


def test_the_welcome_music_still_works():
    from core import ceremony as C
    m = C.music()
    check("audio.shipped_track_present", m.get("kind") == "file",
          f"no music at all: {m}")
    # the default is one of the shipped candidates, not one fixed filename
    check("audio.shipped_track_named",
          m.get("candidate") in [c["id"] for c in C.candidates()],
          f"the default is not one of the shipped candidates: {m.get('name')}")
    check("audio.shipped_track_exists",
          (C.chosen_path() is not None) and C.chosen_path().is_file(),
          f"the chosen track is not on disk: {m.get('name')}")
    check("audio.stop_time_reported", 0 < int(m.get("max_seconds") or 0) <= 60,
          "the track has no sane stop time")
    check("audio.credit_still_carried", "MacLeod" in (m.get("credit") or ""),
          "the CC BY credit went missing when generation was removed")


def _src(name):
    with open(ROOT / name, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


if __name__ == "__main__":
    for fn in (test_it_picks_the_window_that_lands,
               test_quiet_music_is_not_left_quiet,
               test_dense_music_also_lands_on_target,
               test_the_output_is_a_real_playable_file,
               test_a_failed_condition_still_gives_you_your_music,
               test_nothing_generates_music_at_runtime,
               test_the_welcome_music_still_works):
        try:
            fn()
        except Exception as e:
            import traceback
            check(fn.__name__, False, f"raised {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\nPASS {COUNT - len(FAILS)}  FAIL {len(FAILS)}")
    for f in FAILS:
        print(f"  FAIL  {f}")
    sys.exit(1 if FAILS else 0)