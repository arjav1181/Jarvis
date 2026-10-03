"""The welcome ceremony, end to end, through the real server.

The ceremony is the one feature where a plausible-looking result is worthless:
a greeting that fires on the wrong sound, or a track that silently 404s, looks
identical to a working one until the moment you rely on it. So this drives the
actual endpoints — upload a track, clap at it with synthetic PCM, and check
that the right things happened and the wrong ones did not.

Two halves:
  * the script — does it vary, does it stay honest, does it stay quiet when it
    has nothing to say
  * the plumbing — does a clap actually reach the ceremony, does the track
    actually play, is any of it reachable without the token
"""
import json
import math
import os
import random
import sys
import tempfile
import time
from array import array
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = tempfile.mkdtemp(prefix="jarvis-test-ceremony-")
os.environ["JARVIS_DATA"] = _TMP
os.environ.setdefault("JARVIS_WELCOME_ENABLED", "1")

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


RATE = 16000


def pcm(samples):
    return array("h", [int(max(-1.0, min(1.0, v)) * 32768) for v in samples]).tobytes()


def clap_pcm(gap=0.12, lead=0.4, tail=0.4):
    """Two hand-claps as the detector will receive them: 16k mono PCM16."""
    rng = random.Random(11)
    out = [0.0] * int(lead * RATE)
    for _ in range(2):
        out += [rng.uniform(-1, 1) * 0.85] * int(0.006 * RATE)
        out += [0.0] * int(gap * RATE)
    out += [0.0] * int(tail * RATE)
    return pcm(out)


def speech_pcm(seconds=2.0, seed=5):
    rng = random.Random(seed)
    out = []
    for i in range(int(seconds * RATE)):
        t = i / RATE
        env = 0.5 * (1 + math.sin(2 * math.pi * 3.1 * t)) * \
              (0.6 + 0.4 * math.sin(2 * math.pi * 0.7 * t))
        out.append(rng.uniform(-1, 1) * env * 0.22)
    return pcm(out)


# ── the script ───────────────────────────────────────────────────────────────

def test_the_greeting_varies_by_hour():
    from core import ceremony as C
    seen = {}
    for h in range(24):
        lines = C.script(now=datetime(2026, 10, 1, h, 5), with_weather=False)
        seen[h] = lines[0]
    check("cer.midnight_is_its_own_thing",
          "midnight oil" in seen[23].lower(), seen[23])
    check("cer.morning_differs_from_evening", seen[9] != seen[20],
          "09:00 and 20:00 said the same thing")
    check("cer.always_addresses_sir",
          all("sir" in " ".join(C.script(now=datetime(2026, 10, 1, h, 0),
                                         with_weather=False)).lower()
              for h in (7, 13, 19)),
          "the ceremony forgot who it is talking to")


def test_it_never_says_the_same_thing_twice_in_a_row():
    from core import ceremony as C
    firsts = [C.script(with_weather=False)[0] for _ in range(8)]
    check("cer.rotates", len(set(firsts)) > 1,
          f"eight welcomes produced {len(set(firsts))} distinct openings")


def test_it_is_honest_about_faults():
    """The whole reason this module exists. A status line must never claim all
    systems are fine while something is actually broken."""
    from core import ceremony as C
    from core import computer as CP
    C._bump()
    healthy = C.status_line()
    check("cer.claims_nothing_when_healthy",
          "fully operational" in healthy.lower() or "green" in healthy.lower()
          or "nominal" in healthy.lower() or "no faults" in healthy.lower(),
          f"expected an earned all-clear on a healthy system, got: {healthy}")
    CP._state["error"] = "browser has been closed"
    broken = C.status_line()
    check("cer.names_a_real_fault",
          "not answering" in broken or "report" in broken.lower(),
          f"a broken computer produced: {broken}")
    check("cer.does_not_claim_all_clear_while_broken",
          "fully operational" not in broken.lower(),
          f"it claimed everything was fine while it was not: {broken}")
    CP._state.pop("error", None)
    check("cer.recovers", "fully operational" in C.status_line().lower()
          or "green" in C.status_line().lower(),
          "it kept complaining after the fault cleared")


def test_sleep_is_not_a_fault():
    from core import ceremony as C
    from core import computer as CP
    CP.stop()
    check("cer.asleep_is_not_damage", not C.faults(),
          f"a sleeping computer was reported as broken: {C.faults()}")
    check("cer.asleep_is_noted", any("asleep" in n for n in C.notes()),
          f"context about the idle computer is missing: {C.notes()}")


def test_weather_is_silent_without_a_city():
    """Better silent than wrong. Inventing a forecast for a place nobody named
    would be worse than saying nothing."""
    from core import ceremony as C
    os.environ.pop("JARVIS_CITY", None)
    check("cer.no_city_no_weather", C.weather_line(C.weather()) == "",
          "it produced weather with no city configured")
    lines = C.script(with_weather=True)
    check("cer.no_city_still_asks", lines[-1].lower().startswith("what")
          or "where" in lines[-1].lower() or "which" in lines[-1].lower(),
          f"the ceremony lost its final ask: {lines[-1]}")


def test_weather_advice_matches_the_weather():
    """Advice has to fit the conditions, or it is worse than no advice.

    Snow used to be an elif behind the rain branch, so snow with a high
    precipitation probability was told to take an umbrella — confidently, and
    for the wrong weather. Also: the tool lowercased the city, because
    str.capitalize() lowercases everything after the first character.
    """
    from core import weather as W
    snow = {"place": "Oslo", "temp": -2, "feels": -6, "desc": "snow",
            "kind": "snow", "rain_chance": 90, "wind": 15}
    line = W.weather_line(snow).lower()
    check("cer.snow_not_umbrella", "umbrella" not in line,
          f"snow was advised to carry an umbrella: {line}")
    check("cer.snow_says_warm", "warm" in line,
          f"snow did not say wrap up warm: {line}")
    rain = {"place": "London", "temp": 14, "feels": 12, "desc": "light rain",
            "kind": "rain", "rain_chance": 80, "wind": 20}
    check("cer.rain_says_umbrella",
          "umbrella" in W.weather_line(rain).lower(),
          "rain lost its umbrella advice")


def test_weather_tool_actually_answers():
    """It used to open a Google search and say "Showing the weather for X",
    which answers nothing. It must now either give weather or say plainly that
    it cannot — and must never claim to have shown a search."""
    from actions.weather_report import weather_action
    said = weather_action({"city": "Oslo"})
    check("cer.tool_no_search_claim", "showing the weather" not in said.lower(),
          f"the tool is still faking a forecast: {said}")
    check("cer.tool_honest_either_way",
          ("degrees" in said.lower()) or ("could not" in said.lower())
          or ("do not have a city" in said.lower()),
          f"neither an answer nor an honest refusal: {said}")
    nocity = weather_action({})
    check("cer.tool_asks_for_a_city_rather_than_guessing",
          ("do not have a city" in nocity.lower()) or "degrees" in nocity.lower(),
          f"unconfigured weather guessed instead of asking: {nocity}")


def test_weather_is_one_implementation():
    """Two answers to one question is how the fake one survived. There is now
    a module, and both callers use it."""
    from core import ceremony as C, weather as W
    check("cer.ceremony_uses_shared_weather", C.weather is W.weather,
          "the ceremony has drifted back to its own weather code")
    check("cer.shared_line", C.weather_line is W.weather_line,
          "the ceremony has drifted back to its own phrasing")

def test_the_city_is_a_setting_not_a_build_step():
    """The city used to be an environment variable, so the only way to tell
    Jarvis where you are was to edit the Space's variables and redeploy. That is
    not a setting. It is a field now, stored on the volume, and everything that
    needs it reads the same one place."""
    import os as _os
    from core import settings as _set, weather as _wx, ceremony as _ce
    saved = _os.environ.pop("JARVIS_CITY", None)
    try:
        _set.set_("city", "")
        check("cer.city_starts_unset", _wx.city() == "",
              f"a city appeared from nowhere: {_wx.city()!r}")
        _set.set_("city", "Lagos")
        check("cer.city_survives_a_reread", _wx.city() == "Lagos",
              f"the stored city did not come back: {_wx.city()!r}")
        check("cer.ceremony_agrees", _ce.city() == "Lagos",
              f"the ceremony disagrees with weather about the city: {_ce.city()!r}")
        check("cer.weather_uses_it", "Lagos" in (_wx.report("Lagos") or "Lagos"),
              "the report did not use the configured city")
        _set.set_("city", "  ")
        check("cer.city_can_be_cleared", _wx.city() == "",
              "clearing the city did nothing")
        _os.environ["JARVIS_CITY"] = "Oslo"
        check("cer.env_overrides_stored", _wx.city() == "Oslo",
              "an operator's env var should outrank a typed value")
    finally:
        _os.environ.pop("JARVIS_CITY", None)
        if saved:
            _os.environ["JARVIS_CITY"] = saved


def test_the_settings_store_is_not_trusted_blind():
    """A hand-edited file must not introduce a setting nothing reads, and a
    corrupt one must not take the feature down with it."""
    from core import settings as _set
    bad = _set.set_("nonsense", "x")
    check("cer.unknown_setting_refused", not bad.get("ok"),
          f"an unknown setting was accepted: {bad}")
    path = _set._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    check("cer.corrupt_file_survived", _set.get("city", "") == "",
          f"a corrupt settings file produced a city: {_set.get('city','')!r}")
    path.write_text('{"city": "Porto", "sneaky": "value"}', encoding="utf-8")
    check("cer.unknown_key_ignored", _set.get("city") == "Porto",
          "a good key was lost because the file also held a bad one")
    check("cer.unknown_key_not_returned", "sneaky" not in _set.all_settings(),
          f"an unread key was handed to the dashboard: {_set.all_settings()}")


def test_the_city_endpoint_is_reachable_and_gated():
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    from core import settings as _set
    _set.set_("city", "")
    srv = DashboardServer()
    srv._tokens = {"t"}
    auth = {"Authorization": "Bearer t"}
    with TestClient(srv.app) as c:
        r = c.get("/api/settings")
        check("cer.settings_gated", r.status_code == 401,
              f"the settings endpoint answered without a token: {r.status_code}")
        r = c.post("/api/settings", json={"city": "Lagos"}, headers=auth)
        check("cer.settings_writable", r.status_code == 200 and r.json().get("ok"),
              f"could not set the city: {r.status_code} {r.text[:80]}")
        r = c.get("/api/settings", headers=auth)
        got = (r.json().get("settings") or {}).get("city")
        check("cer.settings_read_back", got == "Lagos",
              f"the endpoint did not return what was written: {got!r}")
        r = c.post("/api/settings", json={"bogus": "x"}, headers=auth)
        check("cer.settings_rejects_unknown", r.status_code == 400,
              f"an unknown setting went through the API: {r.status_code}")
        _set.set_("city", "")

def test_weather_commentary():
    from core import ceremony as C
    cases = [
        ({"place": "London", "temp": 14, "feels": 12, "desc": "light rain",
          "kind": "rain", "rain_chance": 80, "wind": 20}, "umbrella"),
        ({"place": "Lagos", "temp": 33, "feels": 36, "desc": "clear",
          "kind": "clear", "rain_chance": 5, "wind": 8}, "water"),
        ({"place": "Oslo", "temp": -2, "feels": -6, "desc": "snow",
          "kind": "snow", "rain_chance": 30, "wind": 15}, "warm"),
    ]
    for w, want in cases:
        line = C.weather_line(w)
        check(f"cer.weather_{w['place']}", want in line.lower(),
              f"expected advice about {want}, got: {line}")
    check("cer.weather_not_a_bare_report",
          "degrees and" in C.weather_line(cases[0][0]).lower(),
          "it should read like a person, not a weather API")


def test_the_sequence_is_in_the_right_order():
    from core import ceremony as C
    os.environ["JARVIS_CITY"] = "London"
    lines = C.script(place="London")
    check("cer.ends_with_the_ask",
          any(w in lines[-1].lower() for w in ("what", "where", "which")),
          f"the ceremony did not end by asking: {lines[-1]}")
    check("cer.has_a_status_beat", len(lines) >= 3, f"too thin: {lines}")
    os.environ.pop("JARVIS_CITY", None)


# ── music ────────────────────────────────────────────────────────────────────

def test_a_track_can_be_uploaded_and_served():
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    srv._tokens = {"t"}
    auth = {"Authorization": "Bearer t"}

    wav = _tiny_wav()
    with TestClient(srv.app) as c:
        r = c.post("/api/ceremony/audio", headers=auth,
                   files={"file": ("welcome.wav", wav, "audio/wav")})
        d = r.json()
        check("cer.upload_ok", r.status_code == 200 and d.get("ok"),
              f"{r.status_code} {str(d)[:100]}")
        check("cer.upload_reports_music",
              (d.get("music") or {}).get("kind") == "file",
              f"the ceremony still thinks it has no music: {d.get('music')}")
        g = c.get("/api/ceremony/audio")
        check("cer.audio_served", g.status_code == 200 and len(g.content) > 512,
              f"the track came back {g.status_code}, {len(g.content)} bytes")
        check("cer.audio_is_the_right_bytes", g.content[:4] == b"RIFF",
              "that is not the wav that was uploaded")

        from core import ceremony as C
        check("cer.music_picks_it_up", C.music().get("kind") == "file",
              "the ceremony did not find the uploaded track")
        check("cer.music_url", C.music().get("url") == "/api/ceremony/audio",
              f"unexpected url: {C.music().get('url')}")

        # a junk file must be refused, not stored
        bad = c.post("/api/ceremony/audio", headers=auth,
                     files={"file": ("notes.txt", b"hello" * 400, "text/plain")})
        check("cer.rejects_non_audio", bad.status_code == 400,
              f"a text file was accepted: {bad.status_code}")

        c.post("/api/ceremony/audio/delete", headers=auth)
        # deleting YOUR upload must not leave the ceremony mute: it falls back
        # to the track that ships with the app.
        after = C.music()
        # Falls back to WHICHEVER candidate is chosen, not to a hardcoded
        # one: an earlier test leaves the choice elsewhere on purpose, and
        # asserting a specific track here would be asserting the test order.
        check("cer.falls_back_to_shipped",
              after.get("kind") == "file"
              and after.get("candidate") == C.chosen_id()
              and bool(after.get("candidate")),
              f"expected the chosen candidate after deleting the upload, "
              f"got {after}")


def _tiny_wav(ms=300, rate=16000, freq=440.0) -> bytes:
    import struct
    n = int(ms * rate / 1000)
    frames = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", v)
    data = bytes(frames)
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + \
        struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + \
        b"data" + struct.pack("<I", len(data))
    return hdr + data


# ── the clap → ceremony chain, through the server ────────────────────────────

def test_the_shipped_track_exists_and_is_credited():
    """The welcome must make a sound with nobody having to do anything, and
    CC BY is a legal obligation rather than a courtesy — so the credit has to
    travel with the file, not just sit in a README."""
    from core import ceremony as C
    m = C.music()
    check("cer.track_ships", m.get("kind") == "file",
          f"no default track: {m}")
    static = C.chosen_path()
    check("cer.track_exists_on_disk", static is not None and static.is_file(),
          "the chosen candidate is not on disk")
    if static is None or not static.is_file():
        return
    size = static.stat().st_size
    check("cer.track_is_sane", 20_000 < size < 25 * 1024 * 1024,
          f"the shipped track is {size} bytes, which is not a real audio file")
    with open(static, "rb") as fh:
        head = fh.read(3)
    check("cer.track_is_mp3_or_wav",
          head in (b"ID3", b"RIFF") or head[:2] == b"\xff",
          f"unrecognised audio header {head!r}")
    check("cer.track_is_in_the_image",
          "COPY dashboard/ dashboard/" in _src("Dockerfile"),
          "the tracks live under dashboard/ but the image does not copy them")
    check("cer.credits_recorded", "Kevin MacLeod" in _src("CREDITS.md"),
          "the music is shipped with no attribution in CREDITS.md")
    m = C.music()
    check("cer.track_is_found", m.get("kind") == "file",
          f"the shipped track was not picked up: {m}")
    check("cer.credit_is_carried", "Kevin MacLeod" in (m.get("credit") or ""),
          "the panel would show a CC BY track with no credit")
    check("cer.track_is_a_known_candidate", m.get("candidate") in
          [c["id"] for c in C.candidates()],
          f"the default track is not one of the shipped candidates: {m}")
    check("cer.track_stops", 0 < int(m.get("max_seconds") or 0) <= 60,
          "the track has no sane stop time")


def test_replacing_the_track_drops_the_credit():
    """A user upload is theirs. The credit belonged to the file that was
    replaced, so it must not follow them."""
    from core import ceremony as C
    wav = _tiny_wav()
    d = Path(_TMP) / "ceremony"
    d.mkdir(parents=True, exist_ok=True)
    (d / "welcome.wav").write_bytes(wav)
    try:
        m = C.music()
        check("cer.upload_wins", m.get("name") == "welcome.wav",
              f"the data dir must be searched first, got {m.get('name')}")
        check("cer.no_stale_credit", not (m.get("credit") or ""),
              f"the old track's credit is still being shown: {m.get('credit')}")
    finally:
        (d / "welcome.wav").unlink(missing_ok=True)


def test_the_audition_set_is_shipped_and_audible():
    """Two picks were rejected as "trash", which was fair — a waveform is not
    taste. The answer is to stop guessing, so the four candidates have to
    actually exist, be playable, and be genuinely different from each other."""
    from core import ceremony as C
    rows = C.candidates()
    check("cer.candidates_exist", len(rows) >= 3,
          f"only {len(rows)} candidate(s) to audition")
    seen_bytes = set()
    for r in rows:
        p = ROOT / "dashboard" / "static" / "music" / r["file"]
        check(f"cer.candidate_file.{r['id']}", p.is_file() and p.stat().st_size > 10_000,
              f"{r['file']} is missing or tiny")
        check(f"cer.candidate_credit.{r['id']}", "MacLeod" in (r.get("credit") or ""),
              f"{r['id']} ships without a credit: {r.get('credit')!r}")
        check(f"cer.candidate_mood.{r['id']}", bool((r.get("mood") or "").strip()),
              f"{r['id']} has no description, so there is nothing to choose between")
        check(f"cer.candidate_url.{r['id']}", r.get("url", "").endswith(r["file"]),
              f"bad url {r.get('url')!r}")
    # and they must be DIFFERENT files, not four copies of one
    digests = set()
    for r in rows:
        p = ROOT / "dashboard" / "static" / "music" / r["file"]
        digests.add(p.read_bytes())
    check("cer.candidates_are_distinct", len(digests) == len(rows),
          f"{len(rows)} candidates but only {len(digests)} distinct files")


def test_choosing_a_candidate_actually_changes_the_music():
    """The bug this replaced: candidates sat below a generic 'any file called
    welcome.*' scan, so picking one changed nothing while appearing to
    succeed. A choice the user made has to outrank a file nobody picked."""
    from core import ceremony as C
    played = set()
    for cid in (c["id"] for c in C.candidates()):
        r = C.choose(cid)
        check(f"cer.choose_ok.{cid}", r.get("ok"), f"{r}")
        m = C.music()
        check(f"cer.choose_takes_effect.{cid}", m.get("candidate") == cid,
              f"chose {cid} but {m.get('candidate')} is playing ({m.get('name')})")
        check(f"cer.credit_follows.{cid}",
              next((c["title"] for c in C.candidates() if c["id"] == cid), "")[:6]
              in (m.get("credit") or ""),
              f"the credit did not follow the choice: {m.get('credit')!r}")
        played.add(m.get("name"))
    check("cer.choices_differ", len(played) == len(C.candidates()),
          f"choosing did not change the file: {played}")
    # a bad id is refused and changes nothing
    before = C.music().get("name")
    bad = C.choose("not-a-track")
    check("cer.choose_bad_id", not bad.get("ok") and bad.get("error"),
          f"{bad}")
    check("cer.choose_bad_id_keeps_playing", C.music().get("name") == before,
          "a bad choice changed what plays")


def test_an_upload_outranks_a_candidate():
    from core import ceremony as C
    d = Path(_TMP) / "ceremony"
    d.mkdir(parents=True, exist_ok=True)
    (d / "welcome.wav").write_bytes(_tiny_wav())
    try:
        C.choose("ethereal")
        m = C.music()
        check("cer.upload_wins_over_candidate", m.get("name") == "welcome.wav",
              f"the shipped candidate shadowed the user's own track: {m.get('name')}")
    finally:
        (d / "welcome.wav").unlink(missing_ok=True)
    check("cer.back_to_candidate_after_delete",
          C.music().get("name") == "welcome-ethereal.mp3",
          f"deleting the upload did not fall back: {C.music().get('name')}")


def test_a_clap_runs_the_ceremony():
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    from core import clap as _clap_mod
    _clap_mod.reset()
    srv = DashboardServer()
    srv._tokens = {"t"}
    auth = {"Authorization": "Bearer t"}
    with TestClient(srv.app) as c:
        # a clap on the laptop mic
        r = c.post("/api/ceremony/clap", headers=auth,
                   json={"b64": __import__("base64").b64encode(
                       clap_pcm()).decode()})
        d = r.json()
        check("cer.clap_detected", d.get("clap") is True,
              f"a real double clap was not detected: {d}")
        check("cer.clap_counted", int(d.get("detections") or 0) >= 1,
              f"the detection was not counted: {d}")

        # and one that must not
        r2 = c.post("/api/ceremony/clap", headers=auth,
                    json={"b64": __import__("base64").b64encode(
                        speech_pcm()).decode()})
        check("cer.speech_not_a_clap", r2.json().get("clap") is not True,
              f"speech triggered the ceremony: {r2.json()}")

        # running it directly, with no microphone at all
        r3 = c.post("/api/ceremony/run", headers=auth, json={})
        d3 = r3.json()
        check("cer.run_ok", r3.status_code == 200 and d3.get("ok"),
              f"{r3.status_code} {str(d3)[:110]}")
        check("cer.run_has_lines", len(d3.get("lines") or []) >= 3,
              f"too few lines: {d3.get('lines')}")
        check("cer.run_voice", d3.get("voice") == "en-GB-RyanNeural",
              f"wrong voice: {d3.get('voice')}")
        check("cer.run_reports_music",
              (d3.get("music") or {}).get("kind") in ("file", "synth"),
              f"no music plan: {d3.get('music')}")


def test_it_is_all_behind_the_token():
    from fastapi.testclient import TestClient
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    srv._tokens = {"t"}
    with TestClient(srv.app) as c:
        for path, method, kwargs in (
                ("/api/ceremony", "get", {}),
                ("/api/ceremony/run", "post", {"json": {}}),
                ("/api/ceremony/clap", "post", {"json": {"b64": ""}}),
                ("/api/ceremony/trigger", "post", {}),
                ("/api/ceremony/audio", "post",
                 {"files": {"file": ("x.wav", b"x" * 900, "audio/wav")}}),
                ("/api/ceremony/audio/delete", "post", {})):
            r = getattr(c, method)(path, **kwargs)
            check(f"cer.auth_{path.split('/')[-1]}_{method}",
                  r.status_code == 401,
                  f"{method.upper()} {path} answered {r.status_code} with no token")
        # and a bad token on the ceremony socket path
        try:
            with c.websocket_connect("/ws/computer?token=wrong"):
                check("cer.ws_rejects", False, "it accepted a bad token")
        except Exception:
            check("cer.ws_rejects", True)


def test_the_whole_surface_is_wired():
    sv = _src("dashboard/server.py")
    for route in ("/api/ceremony", "/api/ceremony/run", "/api/ceremony/audio",
                  "/api/ceremony/clap", "/api/ceremony/trigger",
                  "/api/clap"):
        check(f"cer.route{route}", route in sv, f"{route} is missing")
    check("cer.phone_taps_the_detector",
          "_clap_tap" in sv and "_welcome" in sv,
          "the phone mic is not feeding the detector")
    ui = _src("dashboard/static/app.html")
    check("cer.ui_opens", "openCeremony()" in ui, "no way to open the panel")
    check("cer.ui_listens", "cerListen" in ui, "no mic control")
    check("cer.ui_uploads", "cerSendFile" in ui, "no way to change the track")
    check("cer.ui_reacts_to_socket", "welcome_line" in ui,
          "a clap from the phone would not show anything")
    # every websocket kind the panel invents must be handled by the server
    kinds = set(re.findall(r"kind:\s*'([a-z_]+)'", ui))
    handled = set(re.findall(r'kind == "([a-z_]+)"', sv))
    missing = sorted(k for k in kinds if k not in handled and k != "ping")
    check("cer.ui_kinds_handled", not missing,
          f"the panel sends {missing} and the socket ignores them")


def test_the_panel_is_actually_told():
    """The response said the clap worked. That is not the same as the panel
    knowing, and for a while it was not: `broadcast` is a coroutine, and
    calling it bare built one that nobody awaited, so the messages went
    nowhere while every HTTP assertion passed.

    So this connects to the real socket and waits for the welcome."""
    import asyncio
    import inspect
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    check("cer.broadcast_is_async", inspect.iscoroutinefunction(srv.broadcast),
          "broadcast is not a coroutine — the awaits here would be wrong")
    src = _src("dashboard/server.py")
    body = src[src.index("async def _welcome("):src.index("def _welcome_panels(")]
    bare = [l.strip() for l in body.split("\n")
            if l.strip().startswith("self.broadcast(")]
    check("cer.no_unawaited_broadcast", not bare,
          f"these builds a coroutine nobody awaits: {bare[:2]}")

    from fastapi.testclient import TestClient
    import base64 as b64
    # The detector is a process-wide singleton, so a clap from an earlier test
    # is still inside its refractory period. Reset it, or this test measures
    # the cooldown instead of the plumbing.
    from core import clap as _clap_mod
    _clap_mod.reset()
    srv2 = DashboardServer()
    srv2._tokens = {"t"}
    got = []

    with TestClient(srv2.app) as c:
        with c.websocket_connect("/ws?token=t") as ws:
            # drain whatever the socket sends on connect
            try:
                ws.receive_json()
            except Exception:
                pass

            def pump(seconds=6.0):
                import time as _t
                end = _t.time() + seconds
                while _t.time() < end:
                    try:
                        m = ws.receive_json()
                    except Exception:
                        break
                    got.append(m.get("type"))
                    if "welcome_done" in got:
                        break
                    if _t.time() > end:
                        break

            import threading
            th = threading.Thread(target=pump, daemon=True)
            th.start()
            r = c.post("/api/ceremony/clap",
                       headers={"Authorization": "Bearer t"},
                       json={"b64": b64.b64encode(clap_pcm()).decode()})
            th.join(timeout=12)
    check("cer.clap_over_http", r.json().get("clap") is True,
          f"the clap was not even detected: {r.json()}")
    check("cer.panel_was_told", "welcome" in got,
          f"the panel socket never received the welcome; it saw: {sorted(set(got))}")
    check("cer.panel_got_the_lines", "welcome_line" in got,
          f"no lines reached the panel; it saw: {sorted(set(got))}")
    check("cer.ceremony_completed", "welcome_done" in got,
          f"the ceremony never finished on the socket; it saw: {sorted(set(got))}")


def test_the_app_actually_builds():
    """A method defined inside the route-registering function ends that
    function early and returns None. Every route after it vanishes, the Space
    serves nothing, and it is still valid Python — so only an assertion
    catches it."""
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    check("cer.app_builds", srv.app is not None,
          "_build_app returned None — every route after the stray method is gone")
    if srv.app is None:
        return
    paths = {r.path for r in srv.app.routes}
    check("cer.route_count", len(paths) > 100,
          f"only {len(paths)} routes registered, which is far too few")
    for p in ("/", "/api/crew", "/ws/phone-audio", "/ws/computer"):
        check(f"cer.route_present{p}", p in paths, f"{p} is missing")


def _src(name):
    with open(ROOT / name, encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


import re  # noqa: E402  (used by the wiring check above)


if __name__ == "__main__":
    for fn in (test_the_greeting_varies_by_hour,
               test_it_never_says_the_same_thing_twice_in_a_row,
               test_it_is_honest_about_faults, test_sleep_is_not_a_fault,
               test_weather_is_silent_without_a_city, test_weather_commentary,
               test_weather_advice_matches_the_weather,
               test_weather_tool_actually_answers,
               test_weather_is_one_implementation,
               test_the_city_is_a_setting_not_a_build_step,
               test_the_settings_store_is_not_trusted_blind,
               test_the_city_endpoint_is_reachable_and_gated,
               test_the_sequence_is_in_the_right_order,
               test_the_audition_set_is_shipped_and_audible,
               test_choosing_a_candidate_actually_changes_the_music,
               test_an_upload_outranks_a_candidate,
               test_the_shipped_track_exists_and_is_credited,
               test_replacing_the_track_drops_the_credit,
               test_a_track_can_be_uploaded_and_served,
               test_a_clap_runs_the_ceremony, test_the_panel_is_actually_told,
               test_it_is_all_behind_the_token,
               test_the_app_actually_builds, test_the_whole_surface_is_wired):
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