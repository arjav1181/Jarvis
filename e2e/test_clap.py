"""Clap detection, tested with synthetic audio rather than a microphone.

A detector you cannot test is a detector you cannot trust, and a microphone in
CI is a lie waiting to happen. So every case here is generated: impulses with a
known shape and a known spacing, steady noise at a known level, silence. The
detector is not told the answer anywhere.

The cases that matter most are the ones it must NOT fire on, because a welcome
that triggers on the wrong sound is worse than no welcome at all.
"""
import math
import os
import random
import sys
import tempfile
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="jarvis-test-clap-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


RATE = 16000


def silence(seconds, rate=RATE):
    return [0] * int(seconds * rate)


def impulse(seconds, rate=RATE, peak=0.85):
    """A clap: a very short, very loud burst. 6ms of noise, not a sine —
    a sine is a tone, and a tone is what a speaker makes, not a hand."""
    n = int(seconds * rate)
    rng = random.Random(7)
    return [int(max(-1.0, min(1.0, rng.uniform(-1, 1))) * peak * 32768)
            for _ in range(n)]


def steady_noise(seconds, level=0.05, rate=RATE, seed=3):
    """A fan, a stereo, rain. Loud enough to be interesting, not transient."""
    rng = random.Random(seed)
    return [int(rng.uniform(-1, 1) * level * 32768)
            for _ in range(int(seconds * rate))]


def speechy(seconds, rate=RATE, seed=11):
    """Loud, sustained, and modulated — the way a voice actually is. This is
    the false positive that matters most: someone talking near the mic."""
    rng = random.Random(seed)
    out = []
    n = int(seconds * rate)
    for i in range(n):
        t = i / rate
        env = 0.5 * (1 + math.sin(2 * math.pi * 3.1 * t))   # syllable rate
        env *= 0.6 + 0.4 * math.sin(2 * math.pi * 0.7 * t)   # phrase swell
        out.append(int(rng.uniform(-1, 1) * env * 0.22 * 32768))
    return out


def feed_all(det, samples):
    """Feed in realistic websocket-ish chunks, not one tidy array."""
    fired = 0
    chunk = 1024
    for i in range(0, len(samples), chunk):
        if det.feed(array("h", samples[i:i + chunk])):
            fired += 1
    return fired


# ── it must fire on a clap ───────────────────────────────────────────────────

def test_fires_on_a_double_clap():
    from core.clap import ClapDetector
    for gap in (0.08, 0.12, 0.18, 0.25, 0.30):
        d = ClapDetector(RATE)
        audio = (silence(0.4) + impulse(0.006) + silence(gap)
                 + impulse(0.006) + silence(0.5))
        hits = feed_all(d, audio)
        check(f"clap.fires_at_{int(gap*1000)}ms", hits == 1,
              f"{hits} detections for a {int(gap*1000)}ms double clap "
              f"({d.last_reason})")


def test_a_single_clap_is_not_a_welcome():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    hits = feed_all(d, silence(0.4) + impulse(0.006) + silence(1.2))
    check("clap.single_is_silent", hits == 0,
          f"one clap must not greet you: {hits} ({d.last_reason})")


def test_a_long_gap_is_two_noises():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    # 0.8s apart is two separate things happening, not a double clap
    audio = silence(0.4) + impulse(0.006) + silence(0.8) + impulse(0.006) \
        + silence(0.5)
    hits = feed_all(d, audio)
    check("clap.long_gap_rejected", hits == 0,
          f"claps 800ms apart are not a clap: {hits} ({d.last_reason})")


def test_too_close_is_one_clap():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    # under MIN_GAP: an echo or a single hand, not two hands
    audio = silence(0.4) + impulse(0.006) + silence(0.02) + impulse(0.006) \
        + silence(0.5)
    hits = feed_all(d, audio)
    check("clap.echo_rejected", hits == 0,
          f"20ms apart is one clap: {hits} ({d.last_reason})")


# ── it must stay quiet ───────────────────────────────────────────────────────

def test_silence_never_fires():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    hits = feed_all(d, silence(4.0))
    check("clap.silence", hits == 0, f"{hits} detections in 4s of silence")


def test_speech_does_not_fire():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    hits = feed_all(d, speechy(6.0))
    check("clap.speech", hits == 0,
          f"someone talking must not trigger a welcome: {hits} "
          f"({d.last_reason})")


def test_steady_noise_does_not_fire():
    from core.clap import ClapDetector
    for level in (0.05, 0.15, 0.30):
        d = ClapDetector(RATE)
        hits = feed_all(d, steady_noise(5.0, level=level))
        check(f"clap.steady_noise_{level}", hits == 0,
              f"{hits} detections at level {level} ({d.last_reason})")


def test_a_door_slam_does_not_fire():
    """One loud transient is a door. Two seconds later another door is two
    doors, not a clap — and the refractory period must not help here, the
    spacing rule has to."""
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    audio = silence(0.4) + impulse(0.02) + silence(2.0) + impulse(0.02) \
        + silence(0.5)
    hits = feed_all(d, audio)
    check("clap.door", hits == 0, f"two slams 2s apart: {hits}")


def test_one_clap_makes_one_welcome():
    """Three claps in quick succession are one enthusiastic greeting."""
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    audio = silence(0.4)
    for _ in range(3):
        audio += impulse(0.006) + silence(0.10)
    audio += silence(1.0)
    hits = feed_all(d, audio)
    check("clap.triple_is_one", hits == 1,
          f"three claps 100ms apart should greet once, got {hits}")


# ── the adaptive part ────────────────────────────────────────────────────────

def test_a_loud_room_does_not_blind_it():
    """The whole reason the floor is adaptive. A fixed threshold tuned in a
    quiet room goes blind the moment a fan starts — or starts firing at
    everything. After a loud stretch, a clap must still be found."""
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    warm = steady_noise(3.0, level=0.25)          # a room gets loud
    feed_all(d, warm)
    after = silence(0.4) + impulse(0.006) + silence(0.12) + impulse(0.006) \
        + silence(0.5)
    hits = feed_all(d, after)
    check("clap.survives_a_loud_room", hits == 1,
          f"a clap was missed after 3s of noise: floor={d.noise_floor:.4f} "
          f"threshold={d.threshold:.4f} ({d.last_reason})")


def test_it_finds_a_clap_in_a_quiet_room_too():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    hits = feed_all(d, silence(0.5) + impulse(0.006) + silence(0.12)
                    + impulse(0.006) + silence(0.5))
    check("clap.quiet_room", hits == 1, f"missed in silence: {d.last_reason}")


def test_the_floor_actually_adapts():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    start = d.noise_floor
    # quiet, so the floor is allowed to move — it must NOT move toward a loud
    # room, which is the next test
    feed_all(d, steady_noise(6.0, level=0.0015))
    check("clap.floor_moves", d.noise_floor < start,
          f"the floor never moved: {start:.5f} -> {d.noise_floor:.5f}")
    # and a sustained LOUD sound must not drag it up into uselessness
    d2 = ClapDetector(RATE)
    before = d2.noise_floor
    feed_all(d2, steady_noise(8.0, level=0.45))
    check("clap.floor_not_poisoned", d2.noise_floor < before * 4,
          f"a loud room pushed the floor to {d2.noise_floor:.4f} "
          f"(was {before:.4f}) — it would now be deaf")


# ── plumbing ─────────────────────────────────────────────────────────────────

def test_chunking_is_irrelevant():
    """A websocket hands over whatever it has. A clap split across two frames
    must still be one clap."""
    from core.clap import ClapDetector
    audio = array("h", silence(0.4) + impulse(0.006) + silence(0.12)
                  + impulse(0.006) + silence(0.5))
    for chunk in (1, 7, 64, 333, 1024, 4096, len(audio)):
        d = ClapDetector(RATE)
        hits = 0
        for i in range(0, len(audio), chunk):
            if d.feed(audio[i:i + chunk]):
                hits += 1
        check(f"clap.chunk_{chunk}", hits == 1,
              f"a clap split into {chunk}-sample frames gave {hits}")


def test_bytes_and_arrays_agree():
    from core.clap import ClapDetector
    audio = silence(0.4) + impulse(0.006) + silence(0.12) + impulse(0.006) \
        + silence(0.4)
    arr = array("h", audio)
    raw = arr.tobytes()
    d1, d2 = ClapDetector(RATE), ClapDetector(RATE)
    h1 = feed_all(d1, arr)
    h2 = 0
    for i in range(0, len(raw), 999):     # deliberately ragged byte chunks
        if d2.feed(raw[i:i + 999]):
            h2 += 1
    check("clap.bytes_match_array", h1 == 1 and h2 == 1,
          f"array gave {h1}, ragged bytes gave {h2}")


def test_odd_length_bytes_do_not_crash():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    try:
        d.feed(b"\x01\x02\x03")          # a truncated frame
        d.feed(b"")
        d.feed(bytearray())
        check("clap.odd_bytes", True)
    except Exception as e:
        check("clap.odd_bytes", False, f"{type(e).__name__}: {e}")


def test_reset_clears_state():
    from core.clap import ClapDetector
    d = ClapDetector(RATE)
    feed_all(d, silence(0.4) + impulse(0.006) + silence(0.12) + impulse(0.006))
    d.reset()
    check("clap.reset", d.detections == 0 and not d._pending_at
          and d.armed, "reset must clear the counter and any pending transient")


def test_the_shared_detector_is_one():
    from core import clap
    clap.reset()
    check("clap.singleton", clap.detector() is clap.detector(),
          "two mic sources must share one detector, or one clap welcomes twice")
    st = clap.status()
    for k in ("detections", "noise_floor", "threshold", "armed"):
        check(f"clap.status_has_{k}", k in st, f"status() is missing {k}")


if __name__ == "__main__":
    for fn in (test_fires_on_a_double_clap, test_a_single_clap_is_not_a_welcome,
               test_a_long_gap_is_two_noises, test_too_close_is_one_clap,
               test_silence_never_fires, test_speech_does_not_fire,
               test_steady_noise_does_not_fire, test_a_door_slam_does_not_fire,
               test_one_clap_makes_one_welcome, test_a_loud_room_does_not_blind_it,
               test_it_finds_a_clap_in_a_quiet_room_too,
               test_the_floor_actually_adapts, test_chunking_is_irrelevant,
               test_bytes_and_arrays_agree, test_odd_length_bytes_do_not_crash,
               test_reset_clears_state, test_the_shared_detector_is_one):
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