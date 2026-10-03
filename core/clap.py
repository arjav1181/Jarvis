"""Clap detection: two sharp transients, close together, and nothing else.

A welcome you trigger by clapping is only worth having if it fires when you
mean it and stays quiet the rest of the time. An assistant that claps at a
door slam, at a dog, or at your own typing is worse than one that never
listens, because you stop trusting it and then it misses the one you meant.

So the test is deliberately narrow. It is looking for the SHAPE of a clap —
a very short, very loud transient against a quiet background, twice, close
together — and not for loudness. Speech is loud and sustained; a door is loud
and singular; music is loud and continuous. None of them are two sharp spikes
a fraction of a second apart.

The method is an adaptive energy gate rather than a fixed threshold, because a
fixed number that works in a quiet room either misses every clap in a noisy
one or fires constantly in a quiet one:

    level      RMS of a 40ms block — the unit of "how loud is right now"
    floor      a slow EMA of the level, updated ONLY while the room is quiet,
               so a sustained loud sound cannot drag the floor up and deafen
               the detector for the rest of the session
    threshold  floor x SPIKE_RATIO, with an absolute floor so that silence
               does not make it hypersensitive

Two crossings of that threshold within MIN_GAP..MAX_GAP is a clap. Anything
else is logged and discarded. After firing, a refractory period stops one
physical clap from being counted as four.

Reimplemented from first principles rather than ported: the technique is
common, the specific repo it came from carries no licence, and this project
does not vendor unlicensed code. See CREDITS.md.
"""
from __future__ import annotations

import math
import os
import time
from array import array
from typing import Any, Optional, Union

#: Both the phone socket and the browser's AudioWorklet already deliver 16kHz
#: mono PCM16, because that is what the live voice session wants. Re-using
#: that stream means no resampling and no second capture path.
SAMPLE_RATE = 16000

BLOCK_MS = 40
#: A transient must be this many times the quiet floor. 7 is forgiving of a
#: clap made at a distance and still far above speech, which sits near 2-3x.
SPIKE_RATIO = 7.0
#: Absolute minimum, so that in genuine silence the gate is not microscopic
#: and a mouse click does not read as a hand clap.
MIN_RMS = 0.012
#: Slow enough to ignore a single clap, fast enough to follow a room changing.
NOISE_FLOOR_ALPHA = 0.992
#: The floor only moves when the level is under this multiple of it. This is
#: the part that stops a fan or a stereo from blinding the detector.
QUIET_GATE_MULT = 2.2
#: Two claps closer than this are one clap with an echo.
MIN_DOUBLE_GAP_S = 0.05
#: Further apart than this and it is two separate noises, not a clap.
MAX_DOUBLE_GAP_S = 0.35
#: Refractory period after firing, so one clap is one welcome.
COOLDOWN_S = 0.45
#: A hand clap is a burst: it is above the gate for one 40ms block and gone. A
#: spoken syllable stays up for 150-460ms. This is the single most important
#: number here — without it, speech whose syllables happen to fall in rhythm
#: reads as a steady stream of claps, and the welcome fires at people talking.
MAX_TRANSIENT_S = 0.12
#: Below this the block is not even worth measuring.
SILENCE_RMS = 0.0015

PCM = Union[bytes, bytearray, memoryview, "array"]


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default).strip())
    except Exception:
        return default


class ClapDetector:
    """Feed it raw PCM16 mono; it tells you when a double clap happened.

    Stateless across instances, stateful within one: the noise floor and the
    pending first transient are the whole state, and both are inspectable so a
    detection can be explained after the fact rather than merely asserted.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, *,
                 block_ms: int = BLOCK_MS, spike_ratio: float = SPIKE_RATIO,
                 min_rms: float = MIN_RMS, alpha: float = NOISE_FLOOR_ALPHA,
                 quiet_gate_mult: float = QUIET_GATE_MULT,
                 min_gap: float = MIN_DOUBLE_GAP_S,
                 max_gap: float = MAX_DOUBLE_GAP_S,
                 cooldown: float = COOLDOWN_S,
                 max_transient: float = MAX_TRANSIENT_S) -> None:
        self.sample_rate = max(8000, int(sample_rate))
        self.block_ms = max(10, int(block_ms))
        self.spike_ratio = float(spike_ratio)
        self.min_rms = float(min_rms)
        self.alpha = float(alpha)
        self.quiet_gate_mult = float(quiet_gate_mult)
        self.min_gap = float(min_gap)
        self.max_gap = float(max_gap)
        self.cooldown = float(cooldown)
        self.max_transient = float(max_transient)
        self.block_samples = max(1, int(self.sample_rate * self.block_ms / 1000))
        self.reset()

    # ── state ────────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self.noise_floor = 0.004
        self.last_rms = 0.0
        self.threshold = max(self.noise_floor * self.spike_ratio, self.min_rms)
        self.detections = 0
        self.blocks = 0
        self.samples = 0
        self._carry = array("h")
        self._odd = b""
        self._candidate: Optional[tuple] = None
        self._pending_at: Optional[float] = None
        self._fired_at = -1e9
        self.last_reason = ""
        self.last_event_at = 0.0

    @property
    def armed(self) -> bool:
        """False while a recent clap is still inside its refractory period."""
        return (time.monotonic() - self._fired_at) >= self.cooldown

    # ── the measurement ──────────────────────────────────────────────────────

    @staticmethod
    def _rms(samples: "array") -> float:
        n = len(samples)
        if n == 0:
            return 0.0
        # int16 -> normalised float. No numpy: this runs on whatever thread the
        # audio arrives on, and a small pure-python loop is not the bottleneck.
        total = 0.0
        for v in samples:
            f = v / 32768.0
            total += f * f
        return math.sqrt(total / n)

    def _to_samples(self, pcm: PCM) -> "array":
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            buf = bytes(pcm)
            # A binary websocket frame is not guaranteed to be even-length, so a
            # sample can be split across two frames. Carrying the odd byte is
            # not pedantry: dropping it desynchronises every sample after it
            # and the detector quietly stops finding claps.
            if self._odd:
                buf = self._odd + buf
                self._odd = b""
            if len(buf) % 2:
                buf, self._odd = buf[:-1], buf[-1:]
            out = array("h")
            out.frombytes(buf)
            return out
        return pcm

    def _on_block(self, samples: "array") -> bool:
        rms = self._rms(samples)
        self.last_rms = rms
        self.blocks += 1

        # The floor moves only in quiet, so a sustained sound cannot raise it
        # and then mask every real clap for the rest of the session.
        if rms < self.noise_floor * self.quiet_gate_mult:
            self.noise_floor = self.alpha * self.noise_floor + \
                (1.0 - self.alpha) * rms
            self.noise_floor = max(self.noise_floor, 1e-6)
        self.threshold = max(self.noise_floor * self.spike_ratio, self.min_rms)

        now = self.samples / float(self.sample_rate)

        if rms < SILENCE_RMS:
            return self._settle(now)

        if rms <= self.threshold:
            return self._settle(now)

        # Above the gate. The first block of a crossing is only a CANDIDATE:
        # nothing is decided until the level comes back down, because that is
        # what tells a burst apart from a sound that simply stays loud.
        if self._candidate is None:
            self._candidate = (now, rms)
        # already above and still above: a sustained passage, not a transient.
        # Keep waiting rather than re-arming on every block of it.
        return False

    def _settle(self, now: float) -> bool:
        """The level fell back. Settle any candidate and try to pair it."""
        cand = self._candidate
        self._candidate = None
        if cand is None:
            # nothing outstanding, but an unpaired first transient may have
            # aged out while we were busy
            if self._pending_at is not None and \
                    (now - self._pending_at) > self.max_gap:
                self._pending_at = None
            return False

        at, level = cand
        width = now - at
        if width > self.max_transient:
            self.last_reason = (f"rejected: stayed up {width*1000:.0f}ms — a "
                                f"sound, not a clap")
            return False

        if (time.monotonic() - self._fired_at) < self.cooldown:
            self.last_reason = f"ignored: inside the {self.cooldown}s cooldown"
            return False

        if self._pending_at is None:
            self._pending_at = at
            self.last_reason = f"first transient at {at:.2f}s (rms {level:.3f})"
            return False

        gap = at - self._pending_at
        self._pending_at = None
        if gap < self.min_gap:
            self.last_reason = f"rejected: {gap*1000:.0f}ms apart, too close"
            return False
        if gap > self.max_gap:
            self.last_reason = f"first transient expired after {gap:.2f}s"
            self._pending_at = at
            return False

        self._fired_at = time.monotonic()
        self.detections += 1
        self.last_event_at = time.time()
        self.last_reason = (f"double clap: two transients {gap*1000:.0f}ms "
                            f"apart, {width*1000:.0f}ms wide, peak rms "
                            f"{level:.3f} over a floor of {self.noise_floor:.4f}")
        return True

    # ── the public door ──────────────────────────────────────────────────────

    def feed(self, pcm: PCM) -> bool:
        """Feed any amount of audio. Returns True exactly once per clap.

        Chunk boundaries do not matter: a partial block is carried over, so a
        160-sample websocket frame and a 4096-sample burst are both handled
        without the caller having to align anything.
        """
        samples = self._to_samples(pcm)
        if not samples:
            return False
        if self._carry:
            samples = array("h", self._carry) + samples
            self._carry = array("h")

        fired = False
        step = self.block_samples
        i = 0
        n = len(samples)
        while i + step <= n:
            block = samples[i:i + step]
            i += step
            self.samples += step
            if self._on_block(block) and not fired:
                fired = True
        if i < n:
            self._carry = array("h", samples[i:])
        return fired

    def reset_floor(self, rms: float) -> None:
        """Seed the floor from a known quiet measurement, e.g. the first
        half-second of a stream. Avoids the first few claps being missed
        while the floor settles."""
        self.noise_floor = max(1e-6, float(rms))

    # ── diagnostics ──────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {"detections": self.detections, "blocks": self.blocks,
                "noise_floor": round(self.noise_floor, 5),
                "threshold": round(self.threshold, 5),
                "last_rms": round(self.last_rms, 5),
                "armed": self.armed, "reason": self.last_reason}

    def __repr__(self) -> str:
        return (f"<ClapDetector {self.sample_rate}Hz blocks={self.blocks} "
                f"floor={self.noise_floor:.4f} hits={self.detections}>")


#: One process-wide detector. Two mic sources feed the same one, so a clap on
#: the phone and a clap at the laptop are the same event, and a clap heard by
#: both at once cannot produce two welcomes.
_DETECTOR: Optional[ClapDetector] = None


def detector() -> ClapDetector:
    global _DETECTOR
    if _DETECTOR is None:
        _DETECTOR = ClapDetector(_env_int("JARVIS_MIC_SAMPLE_RATE", SAMPLE_RATE))
    return _DETECTOR


def feed(pcm: PCM) -> bool:
    """Feed the shared detector. True means a clap just happened."""
    return detector().feed(pcm)


def status() -> dict:
    return detector().status()


def reset() -> None:
    detector().reset()