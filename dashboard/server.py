"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 3000 by default (override with JARVIS_PORT; no SSL
warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import httpx
import hmac
import json
import os
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import (HTMLResponse, JSONResponse, FileResponse,
                                Response, StreamingResponse)
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

# Server mode is decided in one place (core/mode.py); fall back to the env
# var so this module still behaves correctly if imported standalone.
try:
    from core.mode import SERVER_MODE
except Exception:
    SERVER_MODE = (os.environ.get("JARVIS_MODE") or "").strip().lower() in ("server", "headless")

def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    try:
        from core.data_paths import data_root
        return data_root()
    except Exception:
        return _repo_root()


BASE_DIR    = _repo_root()          # code / static assets live with the repo
DATA_ROOT   = _data_root()          # keys, memory, uploads — /data on HF Spaces
STATIC_DIR  = Path(__file__).parent / "static"
if os.environ.get("JARVIS_PORT"):
    PORT = int(os.environ["JARVIS_PORT"])
elif Path("/data").is_dir():
    PORT = 7860                     # HF Spaces default app port
else:
    PORT = 3000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        DATA_ROOT / "uploads",
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        _repo_root() / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return _repo_root() / "uploads"


UPLOADS_DIR = _make_uploads_dir()

# Shared state for /api/metrics (net rate needs a previous sample).
_metrics_cache: dict = {"primed": False, "net": None, "net_t": 0.0}

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(DATA_ROOT / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


_last_login_stamp = 0.0


def _last_login_at() -> float:
    """When someone last actually logged in. Module-level so it survives the
    request closure and can be read before the server object exists."""
    return _last_login_stamp


def note_login() -> None:
    global _last_login_stamp
    _last_login_stamp = time.time()


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = DATA_ROOT / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

def inject_map_providers(html: str, google_key: str | None = None,
                        ion_token: str | None = None) -> str:
    """Hand the browser its map/space keys at serve time.

    The Google key goes in as window.__GOOGLE_MAPS_API_KEY__ -- the exact global
    the vendored God's Eye bundle reads, and the one its
    _hasPhotorealCredentials() checks before offering photorealistic tiles. The
    Cesium ion token is set immediately after their Cesium script so Ion assets
    and terrain resolve before their viewer is constructed.

    Module-level rather than a closure so e2e can assert the contract against
    the real vendored file, instead of inferring it over HTTP.
    """
    if google_key is None or ion_token is None:
        try:
            from core import godseye as _gev
            prov = _gev.providers()
        except Exception:
            prov = {}
        google_key = prov.get("google_maps_key") if google_key is None else google_key
        ion_token = prov.get("cesium_ion_token") if ion_token is None else ion_token
    gk = str(google_key or "").strip()
    ion = str(ion_token or "").strip()
    # json.dumps escapes quotes but NOT "<", and the HTML parser ends a script
    # at the first "</script>" regardless of JS string context — so a value
    # containing one would break out of the tag. Escape "<" the usual way.
    def _js(v: str) -> str:
        return json.dumps(v).replace("<", "\\u003c")
    if gk:
        tag = ("<script>window.__GOOGLE_MAPS_API_KEY__ = "
               + _js(gk) + ";</script>")
        html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + tag,
                      html, count=1, flags=re.I)
    if ion:
        tag = ("<script>try{ if(window.Cesium && Cesium.Ion) "
               "Cesium.Ion.defaultAccessToken = " + _js(ion)
               + "; }catch(e){}</script>")
        html = re.sub(
            r"(<script[^>]*cesium/Cesium\.js[^>]*>\s*</script>)",
            lambda m: m.group(1) + tag, html, count=1, flags=re.I)
    return html


def _welcome_panels() -> list:
    """Which panels the welcome should bring up.

    Module level on purpose. It used to be defined inside _build_app, next to
    the routes, and called from _on_clap — which is a method, outside that
    function. A nested function is not in scope for a method, so every single
    clap raised NameError: name '_welcome_panels' is not defined.

    The try/except around the ceremony swallowed it into a chat line reading
    "The welcome did not run: ...", which is why it looked like the ceremony
    was merely unreliable rather than impossible. It had never once run from a
    clap.
    """
    return [p for p in str(os.environ.get("JARVIS_WELCOME_PANELS") or
                          "").split() if p.strip()]


def _disp():
    """core.display, reached through one name.

    Was a nested helper inside _build_app and was deleted along with a block of
    routes, which silently killed every /api/display route with a NameError.
    Module level so a route and a method can both reach it.
    """
    from core import display as _d
    return _d


async def _gev_send(path: str, query: str, method: str = "GET",
                    content: bytes | None = None, prefix: str = ""):
    """prefix is what the upstream wants in front of `path` — the app is served
    at the server root, but their data routes live under /api/."""
    from core import godseye as _gev
    url = f"{_gev.BASE}/{prefix}{path.lstrip('/')}"
    if query:
        url += "?" + query
    client = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=4.0),
                               follow_redirects=True)
    resp = await client.send(client.build_request(method, url, content=content),
                             stream=True)
    return client, resp


async def _gev_relay(resp, client, *, cache: str | None = None):
    headers = {}
    if cache:
        headers["cache-control"] = cache
    # aiter_bytes, not aiter_raw: the upstream gzips, and we forward
    # content-type only — streaming the raw bytes would hand the browser a
    # compressed body with no content-encoding to explain it.
    return StreamingResponse(
        resp.aiter_bytes(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/octet-stream"),
        headers=headers, background=None)


class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        # Persistent dashboard password (survives restarts; file in DATA_ROOT
        # config/, never in the repo). Login attempts are rate-limited like
        # jarvisd pairing: 10 bad in 60s → 60s lock.
        self._pw_path    = DATA_ROOT / "config" / "dashboard_auth.json"
        self._pw_rec     = None
        self._pw_fails: list[float] = []
        self._pw_lock_until = 0.0
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._last_status: dict | None    = None
        self._command_queue               = asyncio.Queue()
        self._loop                        = None    # set in serve(); used off-loop
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        # ── jarvisd limbs (Phase 1): outbound device daemons ────────────
        # Devices dial IN to /ws/agent with a token minted by pairing; we
        # only ever persist the token's sha256, never the token itself.
        self._agents_path     = DATA_ROOT / "agents.json"
        self._agents: dict[str, dict] = {}          # token_hash → {name, os, caps, created}
        self._agent_socks: dict[str, WebSocket] = {}  # token_hash → live socket
        self._agent_seen: dict[str, float] = {}     # token_hash → last activity ts
        self._agent_futs: dict[str, asyncio.Future] = {}
        self._agent_fut_owner: dict[str, str] = {}  # req id → token_hash
        self._agent_seq       = 0
        self._agent_pair_code = ""
        self._agent_fails: list[float] = []         # bad pairing attempts (60s window)
        self._agent_lock_until = 0.0
        # ── Phase 2: task store (agentic coding) ─────────────────────────
        from core.tasks import TaskStore
        self._tasks_store = TaskStore()
        self._tasks_store.bind(self.broadcast)
        self._agent_chunk_cb  = self._chunk_to_task   # streamed task output sink
        self._task_req_map: dict[str, str] = {}       # agent req id → task id
        self._task_procs: dict[str, "asyncio.subprocess.Process"] = {}  # space runs
        self._agents_load()
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        # ceremony: a clap is one event, so a burst of them is one welcome
        self._welcome_until: float                = 0.0
        # the running assistant, for the few things only it can do —
        # reloading its plugin registry among them
        self._live                              = None
        # Browser playback sockets (server mode). PCM goes only to the newest
        # connection (_audio_out_primary) — broadcasting to every open tab made
        # each tab play the same stream a few ms apart → unintelligible overlap.
        self._audio_out_clients: set[WebSocket] = set()
        self._audio_out_primary: WebSocket | None = None
        self._audio_out_held: list[bytes] = []   # PCM buffered while no player
        self._ptt_callback                = None
        self._interrupt_callback          = None
        self._mute_callback               = None
        self._key_saved_callback          = None
        self._voice_callback              = None
        self._wake_state_provider         = None
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self._ws_heartbeat_task           = None
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    #: hours of no login after which pairing re-arms itself (see bootstrap_key)
    _PAIR_REARM_HOURS = float(os.environ.get("JARVIS_PAIR_REARM_H", "6"))
    _login_at: float = 0.0

    def note_login(self) -> None:
        """Called whenever a session is actually established, so 'in use' means
        someone is using it rather than something once was."""
        self._login_at = time.time()

    # ── persistent dashboard password ─────────────────────────────────────

    _PW_ITER = 240_000   # pbkdf2-sha256

    def _pw_load(self) -> dict | None:
        if self._pw_rec is None:
            try:
                self._pw_rec = json.loads(
                    self._pw_path.read_text(encoding="utf-8"))
            except Exception:
                self._pw_rec = {}
        return self._pw_rec or None

    def _pw_exists(self) -> bool:
        rec = self._pw_load()
        return bool(rec and rec.get("hash") and rec.get("salt"))

    def _pw_set(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, self._PW_ITER)
        now = round(time.time())
        old = self._pw_load() or {}
        self._pw_rec = {
            "algo":    "pbkdf2_sha256",
            "iter":    self._PW_ITER,
            "salt":    salt.hex(),
            "hash":    dk.hex(),
            "created": old.get("created", now),
            "changed": now,
        }
        self._pw_path.parent.mkdir(parents=True, exist_ok=True)
        self._pw_path.write_text(json.dumps(self._pw_rec, indent=2),
                                 encoding="utf-8")
        try:
            os.chmod(self._pw_path, 0o600)
        except Exception:
            pass

    def _pw_verify(self, password: str) -> bool:
        rec = self._pw_load()
        if not rec or not rec.get("hash") or not rec.get("salt"):
            return False
        try:
            salt = bytes.fromhex(rec["salt"])
            want = bytes.fromhex(rec["hash"])
            iters = int(rec.get("iter") or self._PW_ITER)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
            return hmac.compare_digest(dk, want)
        except Exception:
            return False

    # ── jarvisd pairing & device registry (Phase 1) ─────────────────────

    @staticmethod
    def _agent_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _agents_load(self) -> None:
        try:
            data = json.loads(self._agents_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        devs = data.get("devices")
        self._agents = devs if isinstance(devs, dict) else {}
        code = data.get("pair_code")
        self._agent_pair_code = code if isinstance(code, str) and code else \
            ''.join(secrets.choice(_KEY_CHARS) for _ in range(8))
        self._agents_save()

    def _agents_save(self) -> None:
        try:
            self._agents_path.parent.mkdir(parents=True, exist_ok=True)
            self._agents_path.write_text(json.dumps(
                {"pair_code": self._agent_pair_code, "devices": self._agents},
                indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[Agent] registry save failed: {e}")

    def devices_public(self) -> list[dict]:
        """Dashboard-facing device list: identity + reachability, no secrets."""
        now = time.time()
        out = []
        for h, rec in self._agents.items():
            online = h in self._agent_socks
            seen = self._agent_seen.get(h)
            out.append({
                "name":     rec.get("name", "device"),
                "os":       rec.get("os", ""),
                "caps":     rec.get("caps") or {},
                "online":   online,
                "last_seen": seen,
                "age_s":    round(now - seen) if seen else None,
            })
        out.sort(key=lambda d: (not d["online"], d["name"]))
        return out

    def _approved_exec(self, device: str, cmd: str) -> str:
        """Re-send a human-approved command to a limb.

        Called by core/confirm.py's resolve() on a WORKER THREAD, so the
        coroutine is marshalled back onto the server loop instead of being
        awaited here. `approved: True` is set here and nowhere else — the
        client never gets a say, which is the whole point of the gate.
        """
        loop = self._loop
        if not loop or loop.is_closed():
            return "The server loop is gone — nothing ran."
        fut = asyncio.run_coroutine_threadsafe(
            self.agent_request(device, "exec",
                               {"cmd": cmd, "approved": True}, timeout=60),
            loop)
        try:
            r = fut.result(timeout=75)
        except Exception as e:
            return f"Approved command failed: {type(e).__name__}: {e}"
        if r.get("ok"):
            out = (r.get("out") or "").strip()
            return f"Ran `{cmd[:60]}` on {device or 'the device'}: {out[:200] or 'ok'}"
        return f"The device refused: {(r.get('error') or r.get('out') or '')[:200]}"

    def agent_target(self, want: str = "") -> tuple[str, dict] | None:
        """Pick a device: exact+online → exact → substring+online →
        substring → any online. Online wins over an exact-name stale twin."""
        if want:
            wl = want.strip().lower()
            for want_online in (True, False):
                for h, rec in self._agents.items():
                    if (rec.get("name", "").lower() == wl
                            and (not want_online or h in self._agent_socks)):
                        return h, rec
            for want_online in (True, False):
                for h, rec in self._agents.items():
                    if (wl in rec.get("name", "").lower()
                            and (not want_online or h in self._agent_socks)):
                        return h, rec
        for h in self._agent_socks:
            return h, self._agents.get(h, {})
        return None

    async def agent_request(self, device: str, kind: str,
                            payload: dict | None = None,
                            timeout: float = 30.0,
                            task_id: str = "") -> dict:
        """Send one request to a connected jarvisd and await its reply.

        task_id: when set, streamed chunks for this request are appended to
        that task's log (routed by request id — see _chunk_to_task).
        """
        tgt = self.agent_target(device or "")
        if not tgt:
            return {"ok": False, "error": "no_device",
                    "hint": "no jarvisd daemon is paired/connected"}
        h, rec = tgt
        ws = self._agent_socks.get(h)
        name = rec.get("name", "device")
        if ws is None:
            return {"ok": False, "error": "offline", "device": name}
        self._agent_seq += 1
        rid = f"a{self._agent_seq}"
        fut = asyncio.get_running_loop().create_future()
        self._agent_futs[rid] = fut
        self._agent_fut_owner[rid] = h
        if task_id:
            self._task_req_map[rid] = str(task_id)
        try:
            await ws.send_json({"id": rid, "type": kind, "payload": payload or {}})
            res = await asyncio.wait_for(fut, timeout)
            if isinstance(res, dict):
                res.setdefault("device", name)
                return res
            return {"ok": False, "error": "bad_reply", "device": name}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout", "device": name,
                    "hint": f"no reply in {timeout:.0f}s"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200], "device": name}
        finally:
            self._agent_futs.pop(rid, None)
            self._agent_fut_owner.pop(rid, None)
            self._task_req_map.pop(rid, None)

    # ── Phase 2: task runner (agentic coding) ───────────────────────────────

    def _chunk_to_task(self, msg: dict) -> None:
        """A streamed chunk for a task request → that task's log file."""
        tid = self._task_req_map.get(str(msg.get("id") or ""))
        if tid:
            try:
                self._tasks_store.append_log(tid, str(msg.get("text") or ""))
            except Exception:
                pass

    async def start_task(self, *, prompt: str, where: str = "",
                         repo: str = "", device: str = "",
                         model: str = "", source: str = "voice") -> dict:
        """Create a task and kick its runner. Returns the task or {"error"}."""
        try:
            w = self._tasks_store.resolve_where(where)
        except ValueError as e:
            return {"error": str(e)}
        if w == "device":
            tgt = self.agent_target(device or "")
            if not tgt:
                return {"error": "No jarvisd device is online — start the "
                                 "daemon on your PC, or run this on the Space "
                                 "instead (where='space')."}
            h, rec = tgt
            if h not in self._agent_socks:
                return {"error": f"Device '{rec.get('name')}' is offline."}
            device = rec.get("name", "")
        else:
            device = ""
        try:
            task = self._tasks_store.create(
                prompt=prompt, where=w, repo=repo, device=device,
                model=model, source=source)
        except ValueError as e:
            return {"error": str(e)}
        asyncio.create_task(self._task_run(task["id"]))
        return task

    def _sys(self, text: str) -> None:
        asyncio.create_task(self.broadcast({"type": "sys", "text": text}))

    def _finish_if_active(self, tid: str, status: str, **fields) -> None:
        # A cancel may have landed while the run was still awaited — never
        # overwrite a terminal state (cancel wins).
        cur = self._tasks_store.get(tid)
        if cur and cur.get("status") not in ("done", "failed", "cancelled"):
            self._tasks_store.finish(tid, status, **fields)
            self._push_task_alert(cur, status)

    def _push_task_alert(self, rec: dict, status: str) -> None:
        """Phone alert when a task reaches a terminal state.

        Fire-and-forget: a push service that is down, slow or misconfigured
        must never affect the task it is reporting on.
        """
        if rec.get("status") == "cancelled":
            return
        try:
            from core import push as _push
            if _push.sub_count() == 0:
                return
            label = (rec.get("prompt") or "coding task").strip()[:60]
            asyncio.create_task(asyncio.to_thread(
                _push.notify_task, label, rec.get("id", ""),
                status == "done"))
        except Exception:
            pass

    async def _task_run(self, tid: str) -> None:
        rec = self._tasks_store.get(tid)
        if not rec or rec.get("status") not in ("queued", "running"):
            return
        if rec.get("status") == "running":
            return   # already picked up (double-dispatch guard)
        rec = self._tasks_store.start(tid)
        try:
            if rec.get("where") == "device":
                await self._task_run_device(rec)
            else:
                await self._task_run_space(rec)
        except Exception as e:                       # never leave it hanging
            self._tasks_store.append_log(tid, f"[jarvis] internal error: {e}")
            self._finish_if_active(tid, "failed", tail=str(e)[:500])
            self._sys(f"Task {tid} failed: {str(e)[:120]}")

    async def _task_run_device(self, rec: dict) -> None:
        tid = rec["id"]
        tgt = self.agent_target(rec.get("device") or "")
        if not tgt or tgt[0] not in self._agent_socks:
            self._finish_if_active(tid, "failed",
                                   tail="device went offline before start")
            self._sys(f"Task {tid} failed: device offline.")
            return
        dev = tgt[1].get("name", "device")
        self._tasks_store.update(tid, device=dev)
        self._tasks_store.append_log(
            tid, f"[jarvis] running on device '{dev}'"
            + (f" — repo {rec['repo']} (worktree-isolated)"
               if rec.get("repo") else ""))
        r = await self.agent_request(
            dev, "task.run",
            {"prompt": rec["prompt"], "cwd": rec.get("repo") or "",
             "model": rec.get("model") or "", "task_id": tid},
            timeout=1800, task_id=tid)
        if r.get("rc") is None and not r.get("diff"):
            # protocol-level failure: offline / device_gone / old daemon
            err = r.get("error") or r.get("reason") or "no reply"
            self._tasks_store.append_log(tid, f"[jarvis] {err}")
            hint = r.get("hint") or ""
            if "task.run" in str(err):
                hint = hint or ("update jarvisd.py on that device and "
                                "restart the daemon")
            if hint:
                self._tasks_store.append_log(tid, f"[jarvis] {hint}")
            self._finish_if_active(tid, "failed", tail=str(err)[:500])
            self._sys(f"Task {tid} failed: {str(err)[:120]}")
            return
        rc = r.get("rc")
        tail = str(r.get("tail") or "")[-4000:]
        if rc == 0:
            changed = r.get("changed") or []
            self._finish_if_active(
                tid, "done", diff=str(r.get("diff") or ""),
                branch=str(r.get("branch") or ""),
                wt=str(r.get("wt") or ""), tail=tail,
                rc=0, changed=changed)
            n = len(changed) if isinstance(changed, list) else 0
            self._sys(f"Task {tid} done on {dev}"
                      + (f" — {n} file{'s' if n != 1 else ''} changed."
                         " Review + push in the Tasks panel." if n else
                         " — no changes made."))
        else:
            self._finish_if_active(tid, "failed", tail=tail, rc=rc)
            self._sys(f"Task {tid} failed on {dev} (rc={rc}) — see log.")

    async def _git(self, cwd: str, *args: str) -> str:
        try:
            p = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await p.communicate()
            return out.decode(errors="replace") if p.returncode == 0 else ""
        except Exception:
            return ""

    async def _task_run_space(self, rec: dict) -> None:
        import shutil as _shutil
        tid = rec["id"]
        which = _shutil.which("opencode")
        if not which:
            msg = ("opencode is not installed on the Space — run this task "
                   "on your PC instead (where='device'), or ask to install "
                   "opencode here.")
            self._tasks_store.append_log(tid, f"[jarvis] {msg}")
            self._finish_if_active(tid, "failed", tail=msg)
            self._sys(f"Task {tid} failed: {msg}")
            return
        cwd = (rec.get("repo") or "").strip()
        if cwd and not Path(cwd).is_dir():
            msg = f"repo path does not exist on the Space: {cwd}"
            self._tasks_store.append_log(tid, f"[jarvis] {msg}")
            self._finish_if_active(tid, "failed", tail=msg)
            return
        if not cwd:
            cwd = str(DATA_ROOT / "work" / tid)
            Path(cwd).mkdir(parents=True, exist_ok=True)
        argv = [which, "run"]
        env = {**os.environ, "PWD": cwd}
        model = str(rec.get("model") or "").strip()

        # Delegation runs the OpenAI-compatible gateway by default, for the
        # same reason core/gemini.py and core/coder.py prefer it: it is a
        # different quota pool from the Gemini free tier that runs dry, and a
        # delegated task that stops at step two is a task that mostly does not
        # happen.
        #
        # opencode is a separate process with its own provider config, so
        # preferring the gateway meant actually handing it one — a base URL, a
        # key and a model. Without this the preference was real everywhere
        # except here, which is the one place a user notices, because a
        # delegation either runs or quietly does not.
        if not model:
            try:
                from core import gateway as _gw
                if _gw.enabled():
                    gmodel = _gw.chat_model()
                    if gmodel:
                        model = gmodel
                    gs = _gw.settings()
                    base = str(gs.get("openai_base_url") or "").strip().rstrip("/")
                    key = str(gs.get("openai_api_key") or "").strip()
                    if base:
                        env["OPENAI_BASE_URL"] = base
                    if key:
                        env["OPENAI_API_KEY"] = key
                    if model:
                        # The gateway's models are addressed provider/model.
                        env["OPENCODE_MODEL"] = model
                    self._tasks_store.append_log(
                        tid, f"[jarvis] delegation via gateway"
                             + (f" ({model})" if model else ""))
            except Exception as e:
                self._tasks_store.append_log(
                    tid, f"[jarvis] gateway unavailable, using the default "
                         f"provider ({type(e).__name__})")
        if model:
            argv += ["--model", model]
        argv.append(rec["prompt"])
        self._tasks_store.append_log(
            tid, f"[jarvis] running on the Space (in-place: {cwd})")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
        except Exception as e:
            self._finish_if_active(tid, "failed", tail=str(e)[:500])
            return
        self._task_procs[tid] = proc
        tail: list[str] = []

        async def _pump() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if not text:
                    continue
                tail.append(text)
                if len(tail) > 400:
                    tail.pop(0)
                self._tasks_store.append_log(tid, text)

        pump = asyncio.create_task(_pump())
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=1800)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            pump.cancel()
            self._task_procs.pop(tid, None)
            self._finish_if_active(tid, "failed", tail="timed out after 1800s")
            self._sys(f"Task {tid} timed out.")
            return
        await pump
        self._task_procs.pop(tid, None)
        tail_txt = "\n".join(tail)[-4000:]
        if rc != 0:
            self._finish_if_active(tid, "failed", tail=tail_txt, rc=rc)
            self._sys(f"Task {tid} failed on the Space (rc={rc}).")
            return
        diff = changed = ""
        branch = ""
        if (rec.get("repo") or "") and Path(cwd, ".git").exists():
            await self._git(cwd, "add", "-N", ".")
            diff = await self._git(cwd, "diff")
            st = await self._git(cwd, "status", "--porcelain")
            changed = [l[3:] for l in st.splitlines() if l.strip()]
            branch = (await self._git(cwd, "rev-parse",
                                      "--abbrev-ref", "HEAD")).strip()
        self._finish_if_active(tid, "done", diff=diff, branch=branch,
                               wt="", tail=tail_txt, rc=0, changed=changed)
        n = len(changed) if isinstance(changed, list) else 0
        self._sys(f"Task {tid} done on the Space"
                  + (f" — {n} file{'s' if n != 1 else ''} changed."
                     " Review + push in the Tasks panel." if n else "."))

    async def push_task(self, tid: str) -> dict:
        """Approval-gated push. Returns {"pending": sentence} | {"error"} |
        {"ok": True, ...} when a push completes synchronously (it doesn't)."""
        rec = self._tasks_store.get(str(tid))
        if not rec:
            return {"error": "unknown task"}
        if rec.get("status") != "done":
            return {"error": f"task is {rec.get('status')} — only a done "
                             "task can be pushed"}
        if rec.get("pushed"):
            return {"error": "already pushed"}
        if rec.get("where") == "device" and not (rec.get("wt") and rec.get("branch")):
            return {"error": "this task ran without a git worktree (no repo "
                             "given) — nothing to push"}
        if rec.get("where") == "space" and not rec.get("repo"):
            return {"error": "space tasks need a repo path to push"}
        from core import confirm as confirm_gate
        if confirm_gate.pending_title():
            return {"error": "A confirmation is already waiting on the "
                             "dashboard — resolve it first."}
        diff_head = "\n".join((rec.get("diff") or "").splitlines()[:36])
        if not diff_head:
            diff_head = "(no textual diff — files were created)"
        detail = (f"{rec.get('where')} · {rec.get('branch') or rec.get('repo')}"
                  f"\n{diff_head}")[:290]
        loop = asyncio.get_running_loop()

        def _run() -> str:
            res = asyncio.run_coroutine_threadsafe(
                self._do_push(str(tid)), loop).result(timeout=150)
            return res

        sentence = confirm_gate.request(
            key=f"task-push-{tid}",
            title=f"Push {tid} to origin?",
            detail=detail, run=_run)
        return {"pending": sentence}

    async def _do_push(self, tid: str) -> None:
        rec = self._tasks_store.get(tid)
        if not rec:
            return
        try:
            if rec.get("where") == "device":
                r = await self.agent_request(
                    rec.get("device") or "", "task.push",
                    {"wt": rec.get("wt"), "branch": rec.get("branch"),
                     "approved": True, "task_id": tid},
                    timeout=120, task_id=tid)
                ok = bool(r.get("ok"))
                err = str(r.get("error") or r.get("reason") or "")
            else:
                ok, err = await self._space_push(rec)
            if ok:
                self._tasks_store.update(tid, pushed=True)
                self._tasks_store.append_log(tid, "[jarvis] pushed to origin ✓")
                self._sys(f"Task {tid} pushed to origin.")
            else:
                err = err or "push failed"
                self._tasks_store.append_log(tid, f"[jarvis] push failed: {err}")
                self._sys(f"Task {tid} push failed: {err[:120]}")
        except Exception as e:
            self._tasks_store.append_log(tid, f"[jarvis] push error: {e}")
            self._sys(f"Task {tid} push error: {str(e)[:120]}")

    async def _space_push(self, rec: dict) -> tuple[bool, str]:
        cwd = rec.get("repo") or ""
        if not cwd or not Path(cwd, ".git").exists():
            return False, "not a git repository on the Space"
        await self._git(cwd, "add", "-A")
        msg = f"jarvis {rec['id']}: " + rec.get("prompt", "")[:60]
        await self._git(cwd, "-c", "user.name=JARVIS",
                        "-c", "user.email=jarvis@local",
                        "commit", "-m", msg)
        try:
            p = await asyncio.create_subprocess_exec(
                "git", "-C", cwd, "push", "-u", "origin", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await p.communicate()
            text = out.decode(errors="replace")
            if p.returncode == 0:
                return True, ""
            return False, text.strip()[-300:] or "git push failed"
        except Exception as e:
            return False, str(e)[:300]

    async def cancel_task(self, tid: str) -> dict:
        rec = self._tasks_store.get(str(tid))
        if not rec:
            return {"error": "unknown task"}
        if rec.get("status") in ("done", "failed", "cancelled"):
            return {"error": f"task already {rec.get('status')}"}
        if rec.get("status") == "running":
            if rec.get("where") == "device" and rec.get("device"):
                try:
                    await self.agent_request(rec["device"], "task.cancel",
                                             {"task_id": rec["id"]},
                                             timeout=8)
                except Exception:
                    pass
            else:
                proc = self._task_procs.get(str(tid))
                if proc:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._tasks_store.finish(str(tid), "cancelled")
        self._tasks_store.append_log(str(tid), "[jarvis] cancelled by user")
        self._sys(f"Task {tid} cancelled.")
        return {"ok": True, "task": self._tasks_store.get(str(tid))}

    @staticmethod
    def _ssl_enabled() -> bool:
        # Replit's edge already terminates TLS and forwards plain HTTP to this
        # port (.replit maps localPort 3000). Serving TLS here makes the proxy
        # fail the handshake and return 502 to every public request.
        if DashboardServer._on_replit():
            return False
        # HF Spaces also terminate TLS at the proxy — same failure mode.
        if DashboardServer._on_hf_spaces():
            return False
        if (os.environ.get("JARVIS_SSL") or "").strip().lower() in ("0", "off", "false"):
            return False
        certs = DATA_ROOT / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    @staticmethod
    def _on_replit() -> bool:
        return bool(os.environ.get("REPLIT_DEV_DOMAIN") or os.environ.get("REPL_ID"))

    @staticmethod
    def _on_hf_spaces() -> bool:
        # Hugging Face injects SPACE_ID / SYSTEM=spaces on Docker Spaces.
        if (os.environ.get("SYSTEM") or "").strip().lower() == "spaces":
            return True
        return bool(os.environ.get("SPACE_ID") or os.environ.get("SPACE_HOST"))

    @staticmethod
    def public_base_url() -> str | None:
        """Public HTTPS origin when running under Replit (edge TLS), else None."""
        if not DashboardServer._on_replit():
            return None
        domain = (os.environ.get("REPLIT_DEV_DOMAIN")
                  or os.environ.get("REPLIT_DOMAINS") or "").split(",")[0].strip()
        return f"https://{domain}" if domain else None

    def get_url(self) -> str:
        pub = self.public_base_url()
        if pub:
            return pub
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._on_replit():
            return self.get_url().removeprefix("https://")
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    def set_ptt_callback(self, fn) -> None:
        self._ptt_callback = fn

    def set_interrupt_callback(self, fn) -> None:
        self._interrupt_callback = fn

    def set_mute_callback(self, fn) -> None:
        self._mute_callback = fn

    def set_key_saved_callback(self, fn) -> None:
        self._key_saved_callback = fn

    def set_voice_callback(self, fn) -> None:
        self._voice_callback = fn

    def set_wake_state_provider(self, fn) -> None:
        self._wake_state_provider = fn

    # ── browser audio out (server mode) ────────────────────────────────────

    async def send_audio(self, data: bytes) -> None:
        """Push one PCM batch to the active /ws/audio-out player only.

        If no player is connected (page still opening, WS flap), hold a short
        ring instead of dropping — silence-on-reconnect was "voice never
        starts" until the next turn.
        """
        ws = self._audio_out_primary
        if ws is None:
            self._audio_out_held.append(data)
            if len(self._audio_out_held) > 40:   # ~4 s at 100 ms/batch
                self._audio_out_held.pop(0)
            return
        if self._audio_out_held:
            held, self._audio_out_held = self._audio_out_held, []
            for h in held:
                try:
                    await ws.send_bytes(h)
                except Exception:
                    if self._audio_out_primary is ws:
                        self._audio_out_primary = None
                    self._audio_out_clients.discard(ws)
                    self._audio_out_held = held  # keep remainder for next client
                    return
        try:
            await ws.send_bytes(data)
        except Exception:
            if self._audio_out_primary is ws:
                self._audio_out_primary = None
            self._audio_out_clients.discard(ws)

    async def send_audio_control(self, msg: dict) -> None:
        """Send a control JSON frame (e.g. {"type":"flush"}) to the players."""
        if msg.get("type") == "flush":
            # Interrupt / session restart — stale held PCM must not replay.
            self._audio_out_held.clear()
        dead: set[WebSocket] = set()
        for ws in list(self._audio_out_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._audio_out_clients -= dead
        if self._audio_out_primary in dead:
            self._audio_out_primary = None

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        if msg.get("type") == "status":
            self._last_status = msg
        # Only durable lines go to history — audio_level / partial_user / pong
        # would flood the 300-entry ring and pollute reconnect replays.
        if msg.get("type") not in ("audio_level", "partial_user", "ping", "pong"):
            self._history.append(msg)
            if len(self._history) > 300:
                self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def _ws_heartbeat(self) -> None:
        """Server ping every 20s — stops Replit/edge idle timeouts from
        silently dropping the dashboard socket."""
        while True:
            await asyncio.sleep(20)
            if not self._clients:
                continue
            dead: set[WebSocket] = set()
            for ws in list(self._clients):
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    dead.add(ws)
            if dead:
                self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    @staticmethod
    def _recent_errors(limit: int = 12) -> list:
        """Recent errors, already redacted before they were stored.

        core/errorlog.py has no accessor for the unredacted buffer, so this
        cannot leak by accident — there is nothing to leak.
        """
        try:
            from core import errorlog as _el
            return _el.recent(limit)
        except Exception as e:
            return [f"error log unavailable: {type(e).__name__}"]

    def bind_live(self, live) -> None:
        """Give the dashboard the running assistant.

        Almost everything here is self-contained, but reloading the plugin
        registry is not: the registry is read per turn to build the tool
        declarations, so only the live object can swap it and make a new plugin
        callable on the next message.
        """
        self._live = live

    def _reload_plugins(self) -> dict:
        live = getattr(self, "_live", None)
        if live is None or not hasattr(live, "reload_plugins"):
            return {"ok": False,
                    "error": "the assistant is not running in this process, so "
                          "plugins cannot be reloaded. Restart the app."}
        try:
            return live.reload_plugins()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}

    def _clap_tap(self, data: bytes) -> bool:
        """Feed the shared clap detector. True when a clap just happened."""
        from core import clap as _clap
        return bool(_clap.feed(data))

    async def _welcome(self, *, source: str = "clap") -> None:
        """The ceremony, once a clap lands.

        Guarded so a burst of claps cannot stack up ceremonies, and so the
        panels open on the computer rather than blocking whoever clapped.
        """
        import time as _time
        if self._welcome_until > _time.monotonic():
            return
        self._welcome_until = _time.monotonic() + 20.0
        try:
            from core import ceremony as _ce
            if not _ce.enabled():
                return
            await self.broadcast({"type": "sys", "text": "Clap heard."})
            # tell every open panel to play the music and show the lines
            await self.broadcast(
                {"type": "welcome", "source": source,
                 "url": f"/api/ceremony/audio?v={int(_time.time())}"})
            # Speak first, open second. bundle() returns immediately; the
            # browser is a separate, slower job that must not hold up the line.
            r = await asyncio.wait_for(
                asyncio.to_thread(_ce.bundle), timeout=60)
            for line in (r.get("lines") or []):
                await self.broadcast({"type": "welcome_line", "text": line})
            await self.broadcast({"type": "welcome_done",
                                  "faults": r.get("faults") or [],
                                  "opened": []})
            panels = _welcome_panels()
            if panels:
                # fire and forget: the panels are already up by the time the
                # browser is, and nothing waits on this
                asyncio.create_task(asyncio.to_thread(_ce.open_panels, panels))
        except Exception as e:
            try:
                await self.broadcast(
                    {"type": "sys",
                     "text": f"The welcome did not run: {e}"[:120]})
            except Exception:
                pass
        finally:
            import time as _time2
            self._welcome_until = _time2.monotonic() + 2.0

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and tok in self._tokens

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        # ── PWA assets ──────────────────────────────────────────────────────
        # The manifest and the worker must be reachable from the site root:
        # scope "/" is only allowed for a worker served at the origin, and
        # Android/iOS refuse an install prompt when either 404s.
        _STATIC_DIR = Path(__file__).resolve().parent / "static"

        @app.get("/manifest.webmanifest")
        async def serve_manifest():
            p = _STATIC_DIR / "manifest.webmanifest"
            if not p.exists():
                return JSONResponse({"error": "manifest missing"}, status_code=404)
            return FileResponse(str(p), media_type="application/manifest+json")

        @app.get("/sw.js")
        async def serve_sw():
            p = _STATIC_DIR / "sw.js"
            if not p.exists():
                return JSONResponse({"error": "sw missing"}, status_code=404)
            return FileResponse(
                str(p), media_type="application/javascript",
                headers={"Cache-Control": "no-cache",
                         "Service-Worker-Allowed": "/"})

        @app.get("/static/icon-{name}.png")
        async def serve_icon(name: str):
            # One route for the icon family keeps the manifest honest without
            # four near-identical handlers; anything not on disk is a 404.
            if name not in ("192", "512", "maskable-512"):
                return JSONResponse({"error": "no such icon"}, status_code=404)
            p = _STATIC_DIR / f"icon-{name}.png"
            if not p.exists():
                return JSONResponse({"error": "icon missing"}, status_code=404)
            return FileResponse(str(p), media_type="image/png",
                                headers={"Cache-Control": "public, max-age=604800"})

        # The PWA's own pages. Same shape as the icon route: an explicit
        # allowlist, so this cannot be turned into "serve any file on disk".
        # Without these, the manifest shortcut, the widget and the offline
        # fallback all 404 and the install quietly does nothing.
        @app.get("/static/music/{name}")
        async def serve_welcome_music(name: str):
            """The audition stings. Named files only — a path is not a
            parameter here on purpose, so this cannot become a way to read
            arbitrary files off the image."""
            from core import ceremony as _ce
            row = next((c for c in _ce.CANDIDATES if c["file"] == name), None)
            if row is None:
                return JSONResponse({"error": "no such track"}, status_code=404)
            p = _ce._music_dir() / name
            if not p.is_file():
                return JSONResponse({"error": "no such track"}, status_code=404)
            return FileResponse(str(p), media_type="audio/mpeg",
                                headers={"Cache-Control": "no-cache"})

        @app.get("/static/{page}.html")
        @app.get("/{page}.html")
        async def serve_pwa_page(page: str):
            if page not in ("widget", "offline", "template.widget", "login"):
                return JSONResponse({"error": "no such page"}, status_code=404)
            p = _STATIC_DIR / f"{page}.html"
            if not p.exists():
                return JSONResponse({"error": "page missing"}, status_code=404)
            # The widget reads a session token, so it must never be cached by a
            # proxy or a service worker in a way that could outlive the session.
            cache = ("no-store" if page in ("widget", "login")
                     else "public, max-age=3600")
            return FileResponse(str(p), media_type="text/html; charset=utf-8",
                                headers={"Cache-Control": cache})

        @app.get("/apple-touch-icon.png")
        async def serve_apple_icon():
            # iOS fetches this exact path when the page is added to the home
            # screen — it never reads the manifest for the home-screen icon.
            p = _STATIC_DIR / "apple-touch-icon.png"
            if not p.exists():
                return JSONResponse({"error": "icon missing"}, status_code=404)
            return FileResponse(str(p), media_type="image/png")

        @app.get("/favicon.ico")
        async def favicon():
            p = Path(__file__).resolve().parent / "static" / "favicon.ico"
            if p.exists():
                return FileResponse(str(p), media_type="image/x-icon")
            return Response(status_code=204)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            # app.html carries the audio-out player — a cached copy keeps the
            # old jitter/prefill logic and the voice stays choppy after deploy.
            return HTMLResponse(
                html,
                headers={"Cache-Control": "no-store, must-revalidate",
                         "Pragma": "no-cache"},
            )

        @app.post("/login")
        async def login(req: Request):
            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                note_login()
                self._token_keys[tok] = entered
                self._aes_key(entered)                   # pre-derive & cache
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            note_login()
            self._token_keys[tok] = key
            self._aes_key(key)
            self._device_sessions[dev_tok] = {"session_key": key}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            note_login()
            self._token_keys[tok] = session_key
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        # ── jarvisd device limbs (Phase 1) ──────────────────────────────

        @app.post("/api/agent/pair")
        async def agent_pair(req: Request):
            """Mint a device token for a jarvisd daemon. Auth = pair code.

            The code is printed at boot and shown (authed) on the dashboard
            Devices panel. Wrong guesses are rate-limited: 10 in 60s → 60s lock.
            """
            now = time.time()
            if now < self._agent_lock_until:
                return JSONResponse({"error": "locked"},
                                    status_code=429)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "bad_request"}, status_code=400)
            code = str(body.get("code") or "").strip()
            if not code or code.lower() != self._agent_pair_code.lower():
                self._agent_fails = [t for t in self._agent_fails if t > now - 60]
                self._agent_fails.append(now)
                if len(self._agent_fails) >= 10:
                    self._agent_lock_until = now + 60
                    self._agent_fails = []
                return JSONResponse({"error": "bad code"}, status_code=403)
            self._agent_fails = []
            token = secrets.token_urlsafe(32)
            h = self._agent_hash(token)
            rec = {
                "name":    str(body.get("name") or "device")[:40],
                "os":      str(body.get("os") or "")[:80],
                "caps":    body.get("caps") if isinstance(body.get("caps"), dict) else {},
                "created": round(now),
            }
            # The name is the routing key: re-pairing the same device name
            # retires every older credential instead of piling up duplicate
            # rows (stale twins made exec target an offline record).
            for old_h in [h2 for h2, r2 in self._agents.items()
                          if r2.get("name") == rec["name"]]:
                self._agents.pop(old_h, None)
                self._agent_socks.pop(old_h, None)
                self._agent_seen.pop(old_h, None)
                print(f"[Agent] retired stale credential for {rec['name']}")
            self._agents[h] = rec
            self._agents_save()
            print(f"[Agent] paired: {rec['name']} ({rec['os']})")
            asyncio.create_task(self.broadcast(
                {"type": "device", "event": "paired", "name": rec["name"],
                 "devices": self.devices_public()}))
            return JSONResponse({"ok": True, "token": token})

        @app.get("/api/devices")
        async def devices_list(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({"devices": self.devices_public(),
                                 "pair_code": self._agent_pair_code})

        @app.post("/api/devices/pair-code")
        async def devices_rotate(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            self._agent_pair_code = ''.join(secrets.choice(_KEY_CHARS)
                                            for _ in range(8))
            self._agents_save()
            print(f"[Dashboard] New agent pair code: {self._agent_pair_code}")
            return JSONResponse({"ok": True, "pair_code": self._agent_pair_code})

        @app.post("/api/devices/ping")
        async def devices_ping(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            t0 = time.time()
            r = await self.agent_request(str(body.get("device") or ""),
                                         "status", {}, timeout=8)
            r["ms"] = round((time.time() - t0) * 1000)
            return JSONResponse(r)

        @app.post("/api/devices/exec")
        async def devices_exec(req: Request):
            """Run a shell command on a limb (allowlist enforced daemon-side)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            cmd = str(body.get("cmd") or "").strip()
            if not cmd:
                return JSONResponse({"error": "no command"}, status_code=400)
            # NOTE: only {"cmd"} is forwarded — never a client-supplied
            # "approved" flag. The daemon-side approval is set exclusively by
            # this server, from a core/confirm.py resolve (a human pressing
            # CONFIRM on the dashboard card, or tapping Accept on the phone).
            r = await self.agent_request(str(body.get("device") or ""),
                                         "exec", {"cmd": cmd}, timeout=30)
            if r.get("needs_approval"):
                # The daemon refused because the command is outside the
                # allowlist. Typing it in the panel is not consent, so the
                # same gate the voice path uses opens here — a human decides,
                # wherever they are.
                from core import confirm as gate
                if gate.pending_title():
                    return JSONResponse({
                        "ok": False, "needs_approval": True,
                        "error": "another confirmation is already waiting — "
                                 "answer that one first"})
                device = str(body.get("device") or "") or self.agent_target("")[0]
                token = gate.request(
                    key=f"device-exec-{int(time.time() * 1000) % 10**9}",
                    title=f"Run on {device or 'the device'}: {cmd[:60]}",
                    detail=cmd[:300],
                    run=lambda: self._approved_exec(device, cmd))
                return JSONResponse({"ok": False, "needs_approval": True,
                                     "pending": token})
            return JSONResponse(r)

        # ── Phase 2: tasks (agentic coding) ─────────────────────────────────

        @app.get("/api/tasks")
        async def tasks_list(req: Request, status: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({"tasks": self._tasks_store.list(status=status),
                                 "default_where": self._tasks_store.default_where()})

        @app.post("/api/tasks")
        async def tasks_create(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            r = await self.start_task(
                prompt=str(body.get("prompt") or ""),
                where=str(body.get("where") or ""),
                repo=str(body.get("repo") or ""),
                device=str(body.get("device") or ""),
                model=str(body.get("model") or ""),
                source="panel")
            if "error" in r:
                return JSONResponse(r, status_code=400)
            return JSONResponse(r)

        @app.post("/api/tasks/config")
        async def tasks_config(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            try:
                w = self._tasks_store.set_default_where(
                    str(body.get("default_where") or ""))
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"ok": True, "default_where": w})

        @app.get("/api/tasks/{tid}")
        async def tasks_get(tid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            rec = self._tasks_store.get(tid)
            if not rec:
                return JSONResponse({"error": "unknown task"}, status_code=404)
            return JSONResponse(rec)

        @app.get("/api/tasks/{tid}/log")
        async def tasks_log(tid: str, req: Request, tail: int = 500):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if not self._tasks_store.get(tid):
                return JSONResponse({"error": "unknown task"}, status_code=404)
            return JSONResponse({"id": tid,
                                 "log": self._tasks_store.read_log(tid, tail)})

        @app.post("/api/tasks/{tid}/cancel")
        async def tasks_cancel(tid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            r = await self.cancel_task(tid)
            if "error" in r:
                return JSONResponse(r, status_code=400)
            return JSONResponse(r)

        @app.post("/api/tasks/{tid}/retry")
        async def tasks_retry(tid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            old = self._tasks_store.get(tid)
            if not old:
                return JSONResponse({"error": "unknown task"}, status_code=404)
            r = await self.start_task(prompt=old["prompt"],
                                      where=old.get("where", ""),
                                      repo=old.get("repo", ""),
                                      device=old.get("device", ""),
                                      model=old.get("model", ""),
                                      source="retry")
            if "error" in r:
                return JSONResponse(r, status_code=400)
            return JSONResponse(r)

        @app.post("/api/tasks/{tid}/push")
        async def tasks_push(tid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            r = await self.push_task(tid)
            if "error" in r:
                return JSONResponse(r, status_code=400)
            return JSONResponse(r)

        # ── Phase 3: persistent scheduler + phone push ─────────────────────────
        #
        # The scheduler is process state, not per-connection state, so these
        # endpoints are thin wrappers over core/scheduler.py; the store is
        # shared with the model-callable manage_schedule tool.

        def _sched():
            from core.scheduler import get_scheduler, to_public
            return get_scheduler(), to_public

        @app.get("/api/schedule")
        async def schedule_list(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            s, to_public = _sched()
            return JSONResponse(
                {"jobs": [to_public(j) for j in s.list()]})

        @app.post("/api/schedule")
        async def schedule_add(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            s, to_public = _sched()
            try:
                job = s.add(
                    str(body.get("name") or "")[:80],
                    str(body.get("kind") or "interval").strip().lower(),
                    str(body.get("spec") or ""),
                    prompt=str(body.get("prompt") or ""),
                    handler_name=str(body.get("handler") or ""),
                    enabled=bool(body.get("enabled", True)),
                    notify=bool(body.get("notify", False)),
                    source="panel")
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse(to_public(job), status_code=201)

        @app.get("/api/schedule/{jid}")
        async def schedule_one(jid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            s, to_public = _sched()
            job = s.get(jid)
            if not job:
                return JSONResponse({"error": "unknown job"}, status_code=404)
            return JSONResponse({"job": to_public(job),
                                 "log": s.read_log(jid, 120)})

        @app.post("/api/schedule/{jid}")
        async def schedule_update(jid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            s, to_public = _sched()
            fields = {k: body[k] for k in
                      ("name", "kind", "spec", "prompt", "handler",
                       "notify", "requires_awake")
                      if k in body}
            if "enabled" in body:
                job = s.set_enabled(jid, bool(body["enabled"]))
                if not job:
                    return JSONResponse({"error": "unknown job"}, status_code=404)
                return JSONResponse(to_public(job))
            try:
                job = s.update(jid, **fields)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            if not job:
                return JSONResponse({"error": "unknown job"}, status_code=404)
            return JSONResponse(to_public(job))

        @app.post("/api/schedule/{jid}/remove")
        async def schedule_remove(jid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            s, _ = _sched()
            if not s.remove(jid):
                return JSONResponse({"error": "unknown job"}, status_code=404)
            return JSONResponse({"ok": True, "removed": jid})

        @app.post("/api/schedule/{jid}/run")
        async def schedule_run(jid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            s, _ = _sched()
            r = s.run_now(jid)
            if "error" in r:
                return JSONResponse(r, status_code=400)
            return JSONResponse(r)

        # ── Web Push (phone alerts + one-tap approval) ──────────────────────
        #
        # Everything except /api/push/respond requires the dashboard token.
        # respond is different on purpose: a service worker cannot read
        # sessionStorage, so the signed action token minted when the approval
        # was created IS the credential — 15-minute, single-purpose, HMAC.

        # ── the phone, as a web app ───────────────────────────────────────────
        # The Space already ships a manifest and a service worker, so the phone
        # installs it with no APK. These are the two calls that app makes: one
        # to register itself for push, one to report what it can see.

        # ── the crew: message a bot, it acts as itself ────────────────────────
        #
        # This is the surface that makes a bot a colleague rather than a row in
        # a table. Each bot has its own transcript, its own voice, and its own
        # saved methods; you message one and it answers as that one.

        @app.get("/api/crew")
        async def crew_list(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import agents as _ag, skills as _sk
            rows = []
            for a in _ag.roster():
                mine = [s for s in _sk.all_skills()
                        if (s.get("bot") or "").lower() == a["name"].lower()]
                rows.append({"name": a["name"], "role": a.get("role") or "",
                             "persona": a.get("persona") or "",
                             "tools": a.get("tools") or [],
                             "enabled": a.get("enabled", True),
                             "status": a.get("status") or "idle",
                             "runs": a.get("runs", 0),
                             "skills": [{"name": s["name"],
                                         "readonly": _sk.readonly(s),
                                         "runs": s.get("runs", 0),
                                         "last_run": s.get("last_run")}
                                        for s in mine]})
            return JSONResponse({"bots": rows, "skills": _sk.all_skills()})

        @app.get("/api/crew/{name}/chat")
        async def crew_chat(name: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import crew as _cr
            b = _cr.brief(name)
            if not b.get("ok"):
                return JSONResponse({"error": b.get("error")}, status_code=404)
            return JSONResponse({"bot": name, "brief": b["brief"],
                                 "turns": _cr.transcript(name)})

        @app.post("/api/crew/{name}/say")
        async def crew_say(name: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            from core import crew as _cr
            r = _cr.say(name, str(body.get("message") or ""),
                        path=str(body.get("path") or ""),
                        force=str(body.get("mode") or ""))
            if "error" in r and r.get("ok") is False:
                return JSONResponse(r, status_code=404)
            return JSONResponse(r)

        @app.post("/api/crew/{name}/clear")
        async def crew_clear(name: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import crew as _cr
            return JSONResponse({"ok": _cr.clear(name)})

        @app.post("/api/crew/skill")
        async def crew_skill(req: Request):
            """Save a method from the crew UI."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            from core import skills as _sk
            try:
                rec = _sk.save(str(body.get("name") or ""),
                               when=str(body.get("when") or ""),
                               inputs=str(body.get("inputs") or ""),
                               steps=str(body.get("steps") or ""),
                               validate=str(body.get("validate") or ""),
                               returns=str(body.get("returns") or ""),
                               approval=str(body.get("approval") or ""),
                               bot=str(body.get("bot") or ""),
                               overwrite=bool(body.get("overwrite")))
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"ok": True, "skill": rec["name"]})

        @app.post("/api/phone/report")
        async def phone_report(req: Request):
            """The installed app telling us its battery, where it is, or that a
            camera frame is available.

            Auth is a bearer token rather than the dashboard session, because a
            phone has no session and no login. The token is the same signed
            action credential used elsewhere, so it is short-lived and cannot be
            replayed forever.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            from core import phone as _ph
            return JSONResponse(_ph.report(
                device=str(body.get("device") or "phone"),
                kind=str(body.get("kind") or "unknown"),
                data=body.get("data") or {}))

        @app.get("/api/phone/status")
        async def phone_status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import phone as _ph, push as _push
            return JSONResponse({
                "installed": _push.sub_count() > 0,
                "subs": _push.sub_count(),
                "devices": _ph.devices(),
                "can_notify": bool(_push.get_vapid_keys().get("publicKey")),
                "vapid": _push.get_vapid_keys().get("publicKey", ""),
            })

        # ── the shared computer, live ──────────────────────────────────────────
        # One socket carries both directions: frames and the step log go out,
        # clicks, typing and take-over come back in. This is the panel that
        # makes it a computer rather than an API client — you can watch a bot
        # work, and take the keyboard when a page asks for a human.

        # Browser work blocks — a click is up to 25s, a launch is 90s, and a
        # screenshot is a real capture. Calling any of it inline from an
        # `async def` freezes THIS loop, and this loop carries the chat, the
        # voice socket and every panel in the app. Measured: a 3s page script
        # stalled every other request for 3119ms.
        #
        # So nothing below calls the computer directly. `to_thread` moves the
        # blocking wait onto a worker, the browser keeps its own loop, and the
        # dashboard stays answerable while a bot works.
        async def _cp_run(fn, *a, timeout: float = 120.0, **kw):
            return await asyncio.wait_for(asyncio.to_thread(fn, *a, **kw),
                                          timeout=timeout)

        async def _cp_frame(mod):
            return await _cp_run(mod.frame, timeout=40)

        @app.websocket("/ws/computer")
        async def computer_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            from core import computer as _cp

            loop = asyncio.get_running_loop()

            def push(kind: str, payload: dict) -> None:
                """Called from the computer's thread — hand the frame to the
                server loop, never touch the socket from this thread."""
                try:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": kind, **payload}), loop)
                except Exception:
                    pass

            unsubscribe = _cp.subscribe(push)
            try:
                await websocket.send_json({"type": "hello", **_cp.status()})
                if _cp.status().get("up"):
                    await websocket.send_json({"type": "frame", "up": True,
                                               "shot": (await _cp_frame(_cp)).get("shot"),
                                               "url": _cp.status().get("url"),
                                               "title": _cp.status().get("title"),
                                               "handover": _cp.status().get("handover")})
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    raw_msg = (msg.get("text") or "").strip()
                    if not raw_msg:
                        continue
                    try:
                        m = json.loads(raw_msg)
                    except Exception:
                        continue
                    kind = str(m.get("kind") or "")
                    bot = str(m.get("bot") or "you")
                    if kind == "ping":
                        continue
                    elif kind == "frame":
                        pass
                    elif kind == "click":
                        await _cp_run(_cp.click, str(m.get("selector") or ""),
                                      int(m.get("x") or 0), int(m.get("y") or 0),
                                      timeout=40, bot=bot)
                    elif kind == "type":
                        await _cp_run(_cp.type_text, str(m.get("text") or ""),
                                      timeout=70, bot=bot)
                    elif kind == "fill":
                        await _cp_run(_cp.fill, str(m.get("selector") or ""),
                                      str(m.get("text") or ""), timeout=40, bot=bot)
                    elif kind == "press":
                        await _cp_run(_cp.press, str(m.get("key") or "Enter"),
                                      timeout=40, bot=bot)
                    elif kind == "scroll":
                        await _cp_run(_cp.scroll, int(m.get("amount") or 400),
                                      timeout=40, bot=bot)
                    elif kind == "handover":
                        _cp.handover(str(m.get("mode") or "user"),
                                     str(m.get("note") or ""))
                    elif kind == "start":
                        # the one genuinely slow path: a cold launch is a real
                        # Chromium start, so it gets the long leash
                        await _cp_run(_cp.start, bot, timeout=150)
                    elif kind == "stop":
                        await _cp_run(_cp.stop, timeout=40)
                    elif kind == "forget":
                        _cp.forget_secret(str(m.get("key") or ""))
                    elif kind == "secret":
                        # written straight into the in-memory vault: never
                        # echoed, never logged, never put in a transcript
                        _cp.put_secret(str(m.get("key") or ""),
                                       str(m.get("value") or ""))
                    else:
                        continue
                    if kind != "secret":          # nothing to redraw for a secret
                        await websocket.send_json({"type": "frame",
                                                   **(await _cp_frame(_cp))})
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                try:
                    unsubscribe()
                except Exception:
                    pass

        @app.get("/api/computer")
        async def computer_status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import computer as _cp
            return JSONResponse({**_cp.status(), "secrets": _cp.secret_keys(),
                                 "steps": _cp.steps(40)})

        @app.post("/api/computer")
        async def computer_act(req: Request):
            """The same surface without the socket, so the assistant can drive
            this computer from a tool call.

            Off the loop for the same reason the socket is: a `go` is a real
            navigation and a `start` is a cold Chromium. Called inline, one
            request would stall the chat and the voice socket for as long as
            the page took."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            from core import computer as _cp
            said = await asyncio.wait_for(
                asyncio.to_thread(_cp.tool,
                                  str(body.get("action") or ""),
                                  url=str(body.get("url") or ""),
                                  selector=str(body.get("selector") or ""),
                                  text=str(body.get("text") or ""),
                                  key=str(body.get("key") or ""),
                                  amount=int(body.get("amount") or 400),
                                  x=int(body.get("x") or 0),
                                  y=int(body.get("y") or 0),
                                  mode=str(body.get("mode") or ""),
                                  bot=str(body.get("bot") or "assistant")),
                timeout=180)
            # the browser moved, so the panel needs the new picture
            frame = await asyncio.to_thread(_cp.frame)
            return JSONResponse({"said": said, "frame": frame,
                                 "status": _cp.status()})

        # ── the welcome ceremony ───────────────────────────────────────────────
        # Clap -> music -> greeting -> weather -> the ask. The audio is a file
        # the user supplies, uploaded from the panel onto the persistent volume,
        # so changing the track never needs a rebuild.

        _AUDIO_TYPES = {"audio/mpeg": ".mp3", "audio/mp3": ".mp3",
                        "audio/wav": ".wav", "audio/x-wav": ".wav",
                        "audio/wave": ".wav", "audio/ogg": ".ogg",
                        "audio/mp4": ".m4a", "audio/aac": ".m4a",
                        "audio/webm": ".webm"}

        @app.get("/api/health")
        async def health(req: Request):
            """Is anything actually alive? Deliberately unauthenticated.

            The dashboard page itself is static, so it loads whether or not the
            assistant is running and whether or not the browser is logged in.
            That makes "the page is up but Jarvis does nothing" impossible to
            diagnose from outside — and it is the exact failure this endpoint
            exists for. Three different causes look identical on screen:

                the assistant is asleep and waiting for the wake word
                the assistant never started
                the browser is holding a token the Space no longer has

            Sessions live in memory, so every restart drops them. Without this,
            a stale token looks identical to a broken bot.

            Returns no secrets, no data and no tokens — only booleans and state
            strings. `sessions` is a count, so "logged in?" is answerable
            without being able to log in.
            """
            live = getattr(self, "_live", None)
            snap: dict = {}
            if live is not None:
                try:
                    snap = live.ui.snapshot()
                except Exception as e:
                    snap = {"error": str(e)[:120]}
            try:
                from core import agent_runtime as _ar
                agent_ok, agent_why = _ar.available()
            except Exception as e:
                agent_ok, agent_why = False, f"agent runtime unavailable: {e}"
            try:
                from core import wake_word as _ww
                wake_ok = bool(_ww.is_installed())
            except Exception:
                wake_ok = False
            return JSONResponse({
                "ok": True,
                "assistant": "bound" if live is not None else "not started",
                "state": (snap.get("state") or "unknown").lower(),
                "listening": bool(snap.get("listening")),
                "ready": bool(snap.get("ready")),
                "sessions": len(self._tokens) + len(self._device_sessions),
                "gemini_configured": bool(
                    (os.environ.get("JARVIS_GEMINI_API_KEY") or "").strip()
                    or (os.environ.get("GEMINI_API_KEY") or "").strip()),
                "agent_runtime": {"ready": agent_ok, "detail": agent_why},
                "wake_word_available": wake_ok,
                "errors": self._recent_errors(),
            })

        @app.get("/api/settings")
        async def settings_get(req: Request):
            """What a person has set. Environment overrides are applied, so this
            is what is actually in force rather than what was typed."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import settings as _set
            return JSONResponse({"settings": _set.all_settings()})

        @app.post("/api/settings")
        async def settings_set(req: Request):
            """Set one of the known keys. Empty clears it, which is a real
            setting rather than a way to blank the box."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            from core import settings as _set
            out = {"ok": True, "applied": {}}
            for key, value in (body or {}).items():
                r = _set.set_(key, str(value if value is not None else ""))
                if not r.get("ok"):
                    return JSONResponse({"ok": False, "error": r.get("error")},
                                        status_code=400)
                out["applied"][key] = r.get("value", "")
            out["settings"] = _set.all_settings()
            return JSONResponse(out)

        @app.get("/api/ceremony/tracks")
        async def ceremony_tracks(req: Request):
            """The audition set. Four shipped stings, the active one marked."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import ceremony as _ce
            return JSONResponse({"candidates": _ce.candidates(),
                                 "active": _ce.chosen_id()})

        @app.post("/api/ceremony/tracks")
        async def ceremony_choose(req: Request):
            """Choose which one plays. Writes a pointer on the volume, so trying
            all four is a click rather than a deploy."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import ceremony as _ce
            r = _ce.choose(str(body.get("id") or ""))
            r["candidates"] = _ce.candidates()
            return JSONResponse(r)

        @app.get("/api/ceremony")
        async def ceremony_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import ceremony as _ce, clap as _clap
            return JSONResponse({
                "enabled": _ce.enabled(), "voice": _ce.VOICE,
                "city": _ce.city(), "address": _ce.address(),
                "music": _ce.music(), "faults": _ce.faults(),
                "notes": _ce.notes(), "clap": _clap.status()})

        @app.post("/api/ceremony/run")
        async def ceremony_run(req: Request):
            """Run it now — the clap calls this, and so does the panel's own
            button, so the ceremony is testable without a microphone."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import ceremony as _ce
            try:
                body = await req.json()
            except Exception:
                body = {}
            panels = body.get("panels")
            if not isinstance(panels, list):
                panels = [p for p in str(body.get("panels") or "").split() if p]
            # off the loop: this wakes a real browser
            return JSONResponse(await asyncio.wait_for(
                asyncio.to_thread(_ce.run, place=str(body.get("city") or ""),
                                  panels=panels), timeout=150))

        @app.get("/api/ceremony/audio")
        async def ceremony_audio():
            """The track, or 404 — and never a directory listing or a guess."""
            from core import ceremony as _ce
            f = _ce.music_file()
            if f is None:
                return JSONResponse({"error": "no welcome audio yet"}, status_code=404)
            media = {".mp3": "audio/mpeg", ".wav": "audio/wav",
                     ".ogg": "audio/ogg", ".m4a": "audio/mp4",
                     ".webm": "audio/webm"}.get(f.suffix.lower(),
                                                "application/octet-stream")
            return FileResponse(str(f), media_type=media,
                                headers={"Cache-Control": "no-cache"})

        @app.post("/api/ceremony/audio")
        async def ceremony_audio_upload(req: Request):
            """Take a wav or mp3 from the panel and keep it on the volume."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            form = await req.form()
            up = form.get("file")
            if up is None or not hasattr(up, "read"):
                return JSONResponse({"error": "no file"}, status_code=400)
            data = await up.read()
            ctype = str(getattr(up, "content_type", "") or "").lower()
            ext = _AUDIO_TYPES.get(ctype, "")
            if not ext:
                name = str(getattr(up, "filename", "") or "")
                ext = ("." + name.rsplit(".", 1)[-1].lower()
                       if "." in name else "")
                if ext not in (".mp3", ".wav", ".ogg", ".m4a", ".webm"):
                    return JSONResponse(
                        {"error": "that is not audio I can play "
                                  "(mp3, wav, ogg, m4a)"}, status_code=400)
            if not data or len(data) < 512:
                return JSONResponse({"error": "that file is empty or too small"},
                                    status_code=400)
            # 25 MB, not 12. The cap was originally set for a hand-made sting
            # and then rejected a perfectly ordinary 14 MB track — the most
            # likely thing anyone would actually upload. Playback stops at
            # MAX_MUSIC_SECONDS regardless of length, so a long file is fine.
            if len(data) > 25 * 1024 * 1024:
                return JSONResponse({"error": "too large — 25 MB is the limit"},
                                    status_code=400)
            from core.data_paths import data_root
            d = data_root() / "ceremony"
            d.mkdir(parents=True, exist_ok=True)
            raw_file = d / f".upload{ext}"
            target = d / f"welcome{ext}"
            keep = d / f"original{ext}"
            try:
                raw_file.write_bytes(data)
            except Exception as e:
                return JSONResponse({"error": f"could not save: {e}"[:120]},
                                    status_code=500)

            # A composed clip is raw material: 30 seconds written to build to a
            # payoff, and not mixed for playing quietly under a voice. So it
            # gets trimmed, faded and levelled on the way in — which means a
            # hand-composed track works with no further effort.
            #
            # The ORIGINAL is kept beside it. Conditioning is a judgement, and
            # if the user disagrees with it their file must still be there.
            detail: dict = {}
            try:
                from core import audio as _au
                staged = d / f".cond{ext}"
                detail = await asyncio.wait_for(
                    asyncio.to_thread(_au.prepare, raw_file, staged), timeout=240)
                if not detail.get("ok"):
                    staged.unlink(missing_ok=True)
                else:
                    try:
                        keep.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raw_file.replace(keep)
                    staged.replace(target)   # atomic: never a half-written file
            except Exception as e:
                detail = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
                raw_file.replace(target)     # at worst, use it untouched
            finally:
                try:
                    raw_file.unlink(missing_ok=True)
                except Exception:
                    pass
            from core import ceremony as _ce
            return JSONResponse({"ok": True, "saved": target.name,
                                 "bytes": target.stat().st_size,
                                 "original": keep.name if keep.is_file() else "",
                                 "prepared": detail, "music": _ce.music()})

        @app.post("/api/ceremony/audio/delete")
        async def ceremony_audio_delete(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import ceremony as _ce
            from core.data_paths import data_root
            d = data_root() / "ceremony"
            removed = []
            for p in list(d.glob("welcome.*")):
                try:
                    p.unlink(); removed.append(p.name)
                except Exception:
                    pass
            return JSONResponse({"ok": True, "removed": removed,
                                 "music": _ce.music()})

        # ── plugins: add a capability, not a release ───────────────────────────
        # A plugin is a PLUGIN dict and a run() function. Adding one used to
        # mean committing it and waiting out a rebuild, which is why editing
        # core kept looking like the faster path. These write to the persistent
        # volume and reload the registry in place, so the tool is live on the
        # next message.
        #
        # Installing a plugin is remote code execution by design — the file
        # runs in this process. Every route here is behind the dashboard token.

        @app.get("/api/plugins")
        async def plugins_list(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import plugins as _pl
            inv = _pl.inventory()
            live = getattr(self, "_live", None)
            reg = getattr(live, "_plugin_registry", None)
            active, rejected = [], []
            if reg is not None:
                for d in reg.list_for_ui():
                    row = {"name": d.get("name"), "file": d.get("file"),
                           "description": (d.get("description") or "")[:400],
                           "error": d.get("error") or ""}
                    (rejected if row["error"] else active).append(row)
            return JSONResponse({**inv, "active": active, "rejected": rejected,
                                 "installable": _pl.installable()})

        @app.post("/api/plugins")
        async def plugins_install(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            form = await req.form()
            up = form.get("file")
            if up is not None and hasattr(up, "read"):
                source = (await up.read()).decode("utf-8", "replace")
                filename = str(getattr(up, "filename", "") or "plugin.py")
            else:
                try:
                    body = await req.json()
                except Exception:
                    return JSONResponse({"error": "no file"}, status_code=400)
                source = str(body.get("source") or "")
                filename = str(body.get("filename") or "plugin.py")
            from core import plugins as _pl
            # validate first: a plugin that does not work is reported with its
            # reason and never written
            r = await asyncio.to_thread(_pl.install, source, filename)
            if r.get("ok"):
                r["reload"] = self._reload_plugins()
            return JSONResponse(r)

        @app.post("/api/plugins/validate")
        async def plugins_validate(req: Request):
            """Check a plugin without keeping it — for iterating in the panel."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            from core import plugins as _pl
            return JSONResponse(await asyncio.to_thread(
                _pl.validate_source, str(body.get("source") or ""),
                str(body.get("filename") or "plugin.py")))

        @app.post("/api/plugins/remove")
        async def plugins_remove(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import plugins as _pl
            r = _pl.remove(str(body.get("file") or body.get("name") or ""))
            if r.get("ok"):
                r["reload"] = self._reload_plugins()
                r["plugins"] = _pl.inventory()
            return JSONResponse(r)

        @app.post("/api/plugins/reload")
        async def plugins_reload(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            r = self._reload_plugins()
            from core import plugins as _pl
            r["plugins"] = _pl.inventory()
            return JSONResponse(r)

        @app.post("/api/ceremony/clap")
        async def ceremony_clap(req: Request):
            """The laptop's mic. Posts raw PCM16 like the phone does, and lands
            on exactly the same detector and the same ceremony — one event, one
            welcome, whichever ear heard it."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            import base64 as _b64
            # b64 is what the panel sends, because raw PCM16 is not JSON-safe;
            # plain latin-1 text is kept for curl and for the phone.
            #
            # These were the wrong way round once: the decode was gated on the
            # OTHER field being non-empty, so every browser clap arrived as
            # zero bytes and the detector reported "no audio at all" while the
            # mic meter in the panel was visibly moving.
            raw_audio = b""
            if body.get("b64"):
                try:
                    raw_audio = _b64.b64decode(str(body["b64"]), validate=False)
                except Exception:
                    raw_audio = b""
            else:
                sent = body.get("pcm") or ""
                if isinstance(sent, str):
                    raw_audio = sent.encode("latin-1", "ignore")
                elif isinstance(sent, (bytes, bytearray)):
                    raw_audio = bytes(sent)
            fired = False
            if raw_audio:
                fired = await asyncio.to_thread(self._clap_tap, raw_audio)
            if fired:
                await self._welcome(source="browser")
            from core import clap as _clap
            return JSONResponse({"clap": fired, "bytes": len(raw_audio),
                                 **_clap.status()})

        @app.post("/api/ceremony/trigger")
        async def ceremony_trigger(req: Request):
            """Run it without clapping — for testing, and for when you just
            want the ceremony."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            await self._welcome(source="manual")
            return JSONResponse({"ok": True})

        @app.post("/api/clap")
        async def clap_test(req: Request):
            """Feed the detector from a file, so the whole chain can be proven
            without a microphone in the room."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            form = await req.form()
            up = form.get("file")
            if up is None:
                return JSONResponse({"error": "no file"}, status_code=400)
            data = await up.read()
            from core import clap as _clap
            fired = await asyncio.to_thread(_clap.feed, data)
            return JSONResponse({"clap": fired, **_clap.status()})

        @app.get("/api/push/key")
        async def push_key(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import push as _push
            keys = _push.get_vapid_keys()
            return JSONResponse({"publicKey": keys.get("publicKey", ""),
                                 "ready": bool(keys.get("publicKey"))})

        @app.get("/api/push/status")
        async def push_status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import push as _push
            return JSONResponse(_push.status())

        @app.post("/api/push/subscribe")
        async def push_subscribe(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            from core import push as _push
            try:
                r = _push.add_sub(body.get("subscription") or body,
                                  req.headers.get("user-agent", ""))
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse(r, status_code=201)

        @app.post("/api/push/unsubscribe")
        async def push_unsubscribe(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            from core import push as _push
            return JSONResponse(_push.remove_sub(body.get("endpoint") or ""))

        @app.post("/api/push/test")
        async def push_test(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import push as _push
            if not _push.sub_count():
                return JSONResponse(
                    {"error": "No phone is subscribed yet — enable alerts "
                              "on your phone first."}, status_code=400)
            r = await asyncio.to_thread(
                _push.notify, "JARVIS alerts are live",
                "This is what your phone will say when a task finishes "
                "or something needs approval.")
            return JSONResponse(r)

        @app.post("/api/push/respond")
        async def push_respond(req: Request):
            body = await req.json()
            from core import push as _push
            payload = _push.verify_action(body.get("token", ""))
            if not payload:
                return JSONResponse(
                    {"error": "invalid or expired action token"},
                    status_code=401)
            kind = str(payload.get("kind") or "confirm")
            accept = bool(payload.get("accept"))
            if kind != "confirm":
                return JSONResponse({"error": f"unknown action {kind}"},
                                    status_code=400)
            from core import confirm as _gate
            title = _gate.pending_title()
            _gate.resolve(accept)          # same gate the HUD button resolves
            verb = "approved" if accept else "rejected"
            line = (f"SYS: Phone {verb} — {title}" if title
                    else f"SYS: Phone {verb} a confirmation that already expired")
            await self.broadcast({"type": "sys", "text": line})
            return JSONResponse({"ok": True, "acted": verb,
                                 "had_pending": bool(title)})

        # ── Phase 4a: lead engine (discovery + CRM + inbox) ───────────────────
        #
        # Discovery is network-bound, so it always runs in a thread: an HTTP
        # request must never wait on somebody's RSS feed, and the assistant
        # must keep talking while the run happens.

        # ── Phase 4b: scoring + kanban ───────────────────────────────────────

        # ── Phase 4c: enrich, draft, and one-tap approval ─────────────────────
        #
        # Approval deliberately reuses core/confirm.py — the same gate the
        # dashboard card and the Phase 3 push notification resolve. The draft
        # is the run() closure's payload, and nothing marks a lead "approved"
        # except resolve(True). 4d's sender is the only thing that may move it
        # to contacted.

        # ── Maps (places, links, distances) ──────────────────────────────────
        @app.get("/api/maps/search")
        async def maps_search(req: Request, q: str = "", provider: str = "google",
                              limit: int = 5):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import maps as _maps
            if not q.strip():
                return JSONResponse({"error": "no query"}, status_code=400)
            found = await asyncio.to_thread(
                _maps.search, q, limit=max(1, min(int(limit or 5), 10)))
            p = (provider or "google").lower()
            if p not in _maps.PROVIDERS:
                p = "google"
            for row in found:
                row["provider"] = p
                row["provider_label"] = _maps.LABELS[p]
                row["embed"] = _maps.embed_url(row["lat"], row["lon"])
                row["tile"] = _maps.tiles_url(row["lat"], row["lon"])
            return JSONResponse({"query": q, "provider": p, "results": found,
                                 "pins": await asyncio.to_thread(_maps.load_pins)})

        @app.get("/api/maps/pins")
        async def maps_pins(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import maps as _maps
            return JSONResponse({"pins": await asyncio.to_thread(_maps.load_pins)})

        # ── Phase 4d: Gmail sending + reply watch ─────────────────────────────
        #
        # /api/mail/config takes the app password. It is written straight to
        # the gitignored config file and never echoed back — the response
        # reports configured/not, never the secret.

        @app.get("/api/mail/status")
        async def mail_status(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import mail as _mail
            cfg = _mail.config()
            # The panel shows "N of M used today", so the budget has to be in
            # the same response as the configuration.
            return JSONResponse({**cfg, "budget": _mail.budget()})

        @app.post("/api/mail/config")
        async def mail_config(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            from core import mail as _mail
            addr = str(body.get("address") or "")
            pw = str(body.get("app_password") or "")
            if addr and not pw and _mail.config().get("configured"):
                pass                       # changing address only: keep the pw
            elif not addr:
                return JSONResponse({"error": "an address is required"},
                                    status_code=400)
            cfg = await asyncio.to_thread(_mail.set_config, addr, pw)
            if cfg.get("configured"):
                # A wrong password shows up here rather than at 3am on the
                # first real send: verify once, now.
                probe = await asyncio.to_thread(_mail.verify_login)
                if not probe.get("ok"):
                    return JSONResponse(
                        {"error": "Gmail rejected that login",
                         "detail": probe.get("error", "")[:160],
                         "configured": True}, status_code=400)
            return JSONResponse(cfg)

        @app.get("/godseye")
        @app.get("/godseye/{path:path}")
        async def godseye_app(path: str = "", request: Request = None):
            """The app itself, served straight from the vendored build.

            Deliberately NOT proxied: their build bakes /godseye/ into every
            asset reference, so the mount point has to be ours. Their Node
            server is used for one thing only — the /api/* data routes below.
            A side benefit: the globe still opens if that process is down, it
            just has no live layers.
            """
            from core import godseye as _gev
            root = _gev.GODSEYE_DIR
            rel = (path or "index.html").lstrip("/")
            try:
                target = (root / rel).resolve()
                target.relative_to(root.resolve())          # no climbing out
            except Exception:
                return JSONResponse({"error": "nope"}, status_code=400)
            if target.is_dir():
                target = target / "index.html"
            if not target.is_file():
                return JSONResponse({"error": "not found", "path": rel},
                                    status_code=404)
            if target.name == "index.html":
                # Their build reads window.__GOOGLE_MAPS_API_KEY__ and gates the
                # photorealistic path on it, so injecting here means a key is a
                # settings save — no rebuild of a 35 MB vendored app, and no
                # key baked into a file in the image. no-store, because the
                # response now depends on a secret.
                #
                # The JARVIS skin goes in through the same door, for the same
                # reason: one injection point, no rebuild, and a failure in the
                # skin can only make the globe plainer — never stop it loading.
                from core import gev_theme
                html = gev_theme.inject(inject_map_providers(
                    target.read_text(encoding="utf-8", errors="replace")))
                return HTMLResponse(html, headers={"cache-control": "no-store"})
            return FileResponse(str(target), headers={
                "cache-control": "public, max-age=600"})

        @app.api_route("/api/gev/{path:path}", methods=["GET", "POST", "OPTIONS"])
        async def godseye_api(path: str, request: Request):
            """Their /api/* routes, namespaced under /api/gev. A read-mostly data
            proxy over one fixed loopback upstream: no user-controlled URL, so
            there is nothing here to turn into a request forger."""
            from core import godseye as _gev
            if not _gev.api_alive():
                return JSONResponse({"error": "godseye data server is not running"},
                                    status_code=503)
            body = await request.body() if request.method != "GET" else None
            client, resp = await _gev_send(path, request.url.query,
                                           request.method, body or None,
                                           prefix="api/")
            return await _gev_relay(resp, client)

        #: how long any single model-backed endpoint may hold a request open
        _MODEL_CALL_BUDGET = float(os.environ.get("JARVIS_MODEL_BUDGET", "120"))

        # ── Phase 7: business (invoices, proposals, clients, documents) ────────
        # ── Phase 8: the org ─────────────────────────────────────────────────
        @app.get("/api/agents")
        async def agents_get(req: Request, view: str = ""):
            """The roster, the org summary, or an agent's own history. Config
            lives here rather than in code, so a new role is a form, not a PR."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import agents as A
            await asyncio.to_thread(A.seed)
            if view == "org":
                return JSONResponse(A.org())
            if view == "runs":
                return JSONResponse({"runs": A.recent_runs(40)})
            if view == "catalog":
                return JSONResponse({"roles": list(A.ROLES)})
            return JSONResponse({"agents": A.roster(), "org": A.org(),
                                 "roles": list(A.ROLES)})

        @app.post("/api/agents")
        async def agents_post(req: Request):
            """Hire, change, retire or fire an agent.

            The gate is on the money, not on the hiring: creating an agent is
            cheap and reversible, giving it a daily budget is neither."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import agents as A
            from core import policy as P
            op = str(body.get("op") or "hire").strip().lower()
            name = str(body.get("name") or "").strip()
            budget = float(body.get("budget_usd_day") or 0)
            try:
                if op in ("hire", "add", "save", "update"):
                    action = "hire" if budget > 0 else "hire_no_budget"
                    d = P.gate("agents", {"action": action}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    a = await asyncio.to_thread(
                        A.hire, name,
                        role=str(body.get("role") or ""),
                        persona=str(body.get("persona") or ""),
                        tools=body.get("tools") or "",
                        model=str(body.get("model") or "default"),
                        budget_usd_day=budget,
                        schedule=str(body.get("schedule") or ""),
                        enabled=bool(body.get("enabled", True)))
                    P.audit(f"agents.{op}", "act", actor="user", target=a["name"],
                            result=f"budget ${budget:.2f}/day"
                            if budget else "no budget")
                    return JSONResponse(a, status_code=201)
                if op in ("retire", "disable"):
                    a = await asyncio.to_thread(A.retire, name, delete=False)
                    P.audit("agents.retire", "act", actor="user", target=a["name"],
                            result="retired")
                    return JSONResponse(a)
                if op in ("delete", "fire", "remove"):
                    d = P.gate("agents", {"action": "delete"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    r = await asyncio.to_thread(A.retire, name, delete=True)
                    P.audit("agents.delete", "delete", actor="user",
                            target=r.get("deleted", name), result="deleted")
                    return JSONResponse(r)
                if op == "shift":
                    order = await asyncio.to_thread(A.shift, name,
                                                    str(body.get("job") or ""))
                    return JSONResponse({"order": order})
                if op == "brief":
                    w = await asyncio.to_thread(
                        A.brief, name, str(body.get("job") or ""),
                        deliverable_kind=str(body.get("kind") or "note"),
                        extra=str(body.get("context") or ""))
                    return JSONResponse(w)
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/deliverables")
        async def deliverables_get(req: Request, kind: str = "",
                                   agent: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import agents as A
            rows = await asyncio.to_thread(A.deliverables, kind, agent)
            return JSONResponse({"deliverables": rows, "org": A.org()})

        @app.post("/api/deliverables")
        async def deliverables_post(req: Request):
            """File work, or have one agent grade another's work."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import agents as A
            from core import policy as P
            op = str(body.get("op") or "record").strip().lower()
            try:
                if op in ("record", "add", "new"):
                    d = P.gate("deliverable", {"action": "record"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    rec = await asyncio.to_thread(
                        A.deliver, str(body.get("agent") or "JARVIS"),
                        str(body.get("kind") or "note"),
                        str(body.get("title") or ""),
                        str(body.get("body") or ""),
                        summary=str(body.get("summary") or ""))
                    return JSONResponse(rec, status_code=201)
                if op in ("review", "grade"):
                    rec = await asyncio.to_thread(
                        A.review, str(body.get("ref") or ""),
                        str(body.get("reviewer") or "JARVIS"),
                        str(body.get("verdict") or "pass"),
                        str(body.get("note") or ""))
                    return JSONResponse(rec)
                if op == "delete":
                    d = P.gate("deliverable", {"action": "delete"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    gone = await asyncio.to_thread(
                        A.delete_deliverable, str(body.get("ref") or ""))
                    return JSONResponse({"deleted": gone} if gone else
                                        {"error": "no deliverable matched"},
                                        status_code=200 if gone else 404)
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        # ── Phase 9: the control centre surface ───────────────────────────────
        # One panel, many subsystems. The rule the rest of this app already
        # follows: every one of these answers even when nothing is configured,
        # because "not set up yet" is a status the user needs to see, not an
        # error they have to decode.
        @app.get("/api/journal")
        async def journal_get(req: Request, q: str = "", kind: str = "",
                              days: int = 30):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import journal as J
            rows = (await asyncio.to_thread(J.search, q, days=days, limit=40)
                    if q else await asyncio.to_thread(J.recent, days,
                                                      kind=kind, limit=60))
            return JSONResponse({"entries": rows, "stats": J.stats(),
                                 "timeline": J.timeline(21)})

        @app.post("/api/journal")
        async def journal_post(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import journal as J
            from core import policy as P
            op = str(body.get("op") or "add").lower()
            try:
                if op in ("add", "note", "decide"):
                    d = P.gate("save_memory", {"action": "note"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    rec = await asyncio.to_thread(
                        J.entry, "decision" if op == "decide" else "note",
                        str(body.get("title") or ""), str(body.get("body") or ""),
                        tags=body.get("tags") or [], actor="user")
                    return JSONResponse(rec, status_code=201)
                if op == "delete":
                    return JSONResponse({"note": "journal entries are append-only; "
                                                  "they are only ever pruned by age"})
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/calendar")
        async def calendar_get(req: Request, days: int = 7):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import gcal as G
            return JSONResponse({"events": await asyncio.to_thread(G.events, days),
                                 "status": G.status()})

        @app.post("/api/calendar")
        async def calendar_post(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import gcal as G
            from core import policy as P
            op = str(body.get("op") or "add").lower()
            try:
                if op in ("add", "create"):
                    d = P.gate("calendar", {"action": "add"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    ev = await asyncio.to_thread(
                        G.create, str(body.get("title") or ""),
                        body.get("start"),
                        end=str(body.get("end") or ""),
                        minutes=int(body.get("minutes") or 60),
                        where=str(body.get("where") or ""),
                        notes=str(body.get("notes") or ""))
                    return JSONResponse(ev, status_code=201)
                if op == "delete":
                    d = P.gate("calendar", {"action": "delete"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    return JSONResponse(await asyncio.to_thread(
                        G.delete, str(body.get("ref") or "")))
                if op == "agenda":
                    return JSONResponse({"agenda": G.agenda()})
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/gcal/connect")
        async def gcal_connect(req: Request):
            """Hand back the consent link. The secret itself is never returned —
            the user pastes it into settings."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import gcal as G
            try:
                return JSONResponse({"url": G.auth_url(),
                                     "redirect": G._redirect_uri()})
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200],
                                     "status": G.status()}, status_code=400)

        @app.get("/api/gcal/callback")
        async def gcal_callback(code: str = "", state: str = ""):
            """Google redirects here. No auth header: the state token is the
            proof, and it only exists for ten minutes."""
            from core import gcal as G
            try:
                out = G.callback(code, state)
                return JSONResponse(out)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)

        @app.get("/api/gcal/calendar.ics")
        async def gcal_ics():
            from core import gcal as G
            body, ctype = G.ics_text()
            return Response(body, media_type=ctype,
                            headers={"Content-Disposition":
                                     'attachment; filename="jarvis.ics"'})

        @app.get("/api/files")
        async def files_get(req: Request, q: str = "", tag: str = "",
                            client: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import files as F
            return JSONResponse({"files": await asyncio.to_thread(
                F.find, q, tag=tag, client=client), "stats": F.stats()})

        @app.post("/api/files")
        async def files_post(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import files as F
            from core import policy as P
            op = str((req.query_params.get("op")) or "").lower()
            try:
                if op == "upload":
                    form = await req.form()
                    up = form.get("file")
                    raw = await up.read() if hasattr(up, "read") else bytes(up or b"")
                    d = P.gate("files", {"action": "upload"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    rec = await asyncio.to_thread(
                        F.store, raw, getattr(up, "filename", "file"),
                        title=str(form.get("title") or ""),
                        tags=[t for t in str(form.get("tags") or "").split(",")
                              if t.strip()],
                        client=str(form.get("client") or ""), actor="user")
                    return JSONResponse(rec, status_code=201)
                body = await req.json()
            except Exception:
                body = {}
            op = str(body.get("op") or "").lower()
            try:
                if op == "attach":
                    return JSONResponse(await asyncio.to_thread(
                        F.attach, str(body.get("id") or ""),
                        client=str(body.get("client") or ""),
                        deliverable=str(body.get("deliverable") or ""),
                        tags=body.get("tags") or []))
                if op == "delete":
                    d = P.gate("files", {"action": "delete"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    return JSONResponse(await asyncio.to_thread(
                        F.delete, str(body.get("id") or "")))
                if op == "text":
                    return JSONResponse({"text": await asyncio.to_thread(
                        F.text_of, str(body.get("id") or ""))})
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/files/{fid}")
        async def file_download(fid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import files as F
            p = await asyncio.to_thread(F.resolve, fid)
            if not p:
                return JSONResponse({"error": "not found"}, status_code=404)
            import mimetypes
            rec = F.get(fid) or {}
            return FileResponse(p, filename=rec.get("name") or p.name,
                                media_type=rec.get("mime")
                                or mimetypes.guess_type(p.name)[0]
                                or "application/octet-stream")

        @app.get("/api/home")
        async def home_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import home as H
            await asyncio.to_thread(H.seed)
            return JSONResponse(H.state())

        @app.post("/api/home")
        async def home_post(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import home as H
            from core import policy as P
            op = str(body.get("op") or "").lower()
            try:
                if op == "device":
                    d = P.gate("home", {"action": "device"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    return JSONResponse(await asyncio.to_thread(
                        H.device, str(body.get("name") or ""),
                        str(body.get("kind") or "virtual"),
                        op=str(body.get("dev_op") or ""),
                        args=body.get("args") or {},
                        where=str(body.get("where") or ""),
                        note=str(body.get("note") or "")), status_code=201)
                if op == "scene":
                    d = P.gate("home", {"action": "scene"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    return JSONResponse(await asyncio.to_thread(
                        H.scene, str(body.get("name") or ""),
                        body.get("steps") or []), status_code=201)
                if op == "delete_scene":
                    return JSONResponse(await asyncio.to_thread(
                        H.delete_scene, str(body.get("name") or "")))
                if op in ("run", "run_device"):
                    d = P.gate("home", {"action": "act"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    return JSONResponse(await asyncio.to_thread(
                        H.run_device, str(body.get("name") or ""),
                        str(body.get("op2") or ""),
                        dry_run=bool(body.get("dry_run"))))
                if op in ("run_scene", "scene_run"):
                    d = P.gate("home", {"action": "act"}, actor="user")
                    if d.needs_approval:
                        return JSONResponse({"needs_approval": True,
                                             "reason": d.reason}, status_code=202)
                    return JSONResponse(await asyncio.to_thread(
                        H.run_scene, str(body.get("name") or ""),
                        dry_run=bool(body.get("dry_run"))))
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/browser")
        async def browser_get(req: Request, view: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import browser as B
            if view == "history":
                return JSONResponse({"history": B.history(40)})
            return JSONResponse({"allowed": B.allowed(), "stats": B.stats()})

        @app.post("/api/browser")
        async def browser_post(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import browser as B
            from core import policy as P
            op = str(body.get("op") or "").lower()
            try:
                if op in ("allow", "deny"):
                    if op == "allow":
                        d = P.gate("browser", {"action": "allow"},
                                   actor="user")
                        if d.needs_approval:
                            return JSONResponse({"needs_approval": True,
                                                 "reason": d.reason},
                                                status_code=202)
                        return JSONResponse(B.allow(str(body.get("host") or "")))
                    return JSONResponse(B.deny(str(body.get("host") or "")))
                if op == "open":
                    return JSONResponse(await asyncio.to_thread(
                        B.open_page, str(body.get("url") or ""),
                        screenshot=bool(body.get("screenshot"))))
                if op == "click":
                    return JSONResponse(await asyncio.to_thread(
                        B.click_and_read, str(body.get("url") or ""),
                        str(body.get("text") or "")))
                if op == "status":
                    return JSONResponse({"results": await asyncio.to_thread(
                        B.status_of, body.get("urls") or [])})
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/voice")
        async def voice_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import voice as V
            return JSONResponse(V.status())

        @app.post("/api/voice")
        async def voice_post(req: Request):
            """The voice loop's transitions. The client holds the microphone and
            does the recognition; the server owns the state machine so a dropped
            websocket cannot leave the assistant stuck talking to nobody."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import voice as V
            op = str(body.get("op") or "").lower()
            try:
                if op == "settings":
                    return JSONResponse(V.set_settings(
                        **{k: v for k, v in body.items() if k != "op"}))
                if op == "commit":
                    return JSONResponse(V.history(1)[0] if V.history(1)
                                        else {"error": "no turn"})
                return JSONResponse(V.status())
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/auth")
        async def auth_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import auth as A
            return JSONResponse({"status": A.status(), "sessions": A.sessions(),
                                 "events": A.events(20)})

        @app.post("/api/auth")
        async def auth_post(req: Request):
            """Set the PIN, enrol 2FA, sign everything out. Note what is NOT
            here: a way to disable the approval gate, or to turn off 2FA without
            the current session — those are not actions this endpoint has."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import auth as A
            op = str(body.get("op") or "").lower()
            try:
                if op == "set_pin":
                    return JSONResponse(A.set_pin(str(body.get("pin") or "")))
                if op == "strength":
                    return JSONResponse(A.strength(str(body.get("pin") or "")))
                if op == "totp_enroll":
                    return JSONResponse(A.totp_enroll())
                if op == "sign_out_all":
                    return JSONResponse({"signed_out": A.sign_out_all()})
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/proactive")
        async def proactive_get(req: Request, view: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import proactive as P
            if view == "briefing":
                from core import journal as J
                return JSONResponse({"text": P.briefing(force=True),
                                     "digest": J.digest()})
            if view == "ledger":
                return JSONResponse({"ledger": P.ledger(40)})
            if view == "boundary":
                return JSONResponse({"rows": P.boundary_rows()})
            return JSONResponse({"status": P.status(), "watches": P.watches()})

        @app.post("/api/proactive")
        async def proactive_post(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import proactive as P
            op = str(body.get("op") or "").lower()
            try:
                if op == "briefing":
                    return JSONResponse({"text": P.briefing(force=True)})
                if op == "check":
                    return JSONResponse(P.classify(str(body.get("text") or "")))
                if op == "watch":
                    return JSONResponse(P.watch(str(body.get("name") or ""),
                                                str(body.get("prompt") or ""),
                                                every=str(body.get("every") or "6h"),
                                                kind=str(body.get("kind") or "check")),
                                        status_code=201)
                if op == "unwatch":
                    return JSONResponse(P.unwatch(str(body.get("name") or "")))
                return JSONResponse({"error": f"unknown op '{op}'"},
                                    status_code=400)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/control")
        async def control_get(req: Request):
            """The config console in one call: every knob, every status, one
            round trip. Opening the Control Center should not cost twelve
            requests, and a dashboard that fires twelve requests to draw a panel
            is a dashboard that feels slow."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out: dict = {}
            for name, fn in (
                ("org", lambda: __import__("core.agents", fromlist=["x"]).org()),
                ("policy", lambda: {"tools": __import__(
                    "core.policy", fromlist=["x"]).catalogue()}),
                ("journal", lambda: __import__("core.journal", fromlist=["x"])
                 .stats()),
                ("knowledge", lambda: __import__("core.knowledge", fromlist=["x"])
                 .stats()),
                ("files", lambda: __import__("core.files", fromlist=["x"]).stats()),
                ("calendar", lambda: __import__("core.gcal", fromlist=["x"])
                 .status()),
                ("home", lambda: __import__("core.home", fromlist=["x"]).state()),
                ("browser", lambda: __import__("core.browser", fromlist=["x"])
                 .stats()),
                ("voice", lambda: __import__("core.voice", fromlist=["x"]).status()),
                ("auth", lambda: __import__("core.auth", fromlist=["x"]).status()),
                ("proactive", lambda: __import__("core.proactive", fromlist=["x"])
                 .status()),
            ):
                try:
                    out[name] = await asyncio.to_thread(fn)
                except Exception as e:
                    out[name] = {"error": f"{type(e).__name__}: {e}"[:120]}
            try:
                from core import scheduler as S
                out["scheduler"] = {"jobs": len(S.get_scheduler().list())}
            except Exception as e:
                out["scheduler"] = {"error": str(e)[:80]}
            try:
                from core import confirm as C
                out["approvals"] = {"waiting": C.count()}
            except Exception:
                out["approvals"] = {"waiting": 0}
            return JSONResponse(out)

        @app.get("/api/widget")
        async def widget_get(req: Request):
            """The home-screen widget's whole world: what is waiting, who is
            working, and one line of briefing. Deliberately three numbers — a
            widget that shows everything shows nothing, and this one is on a
            lock screen where nobody will read a paragraph."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            out: dict = {}
            try:
                from core import confirm as C
                out["approvals"] = C.count()
            except Exception:
                out["approvals"] = 0
            try:
                from core import agents as A
                o = A.org()
                out["agents"] = o["active"]
                out["working"] = o["working"]
            except Exception:
                out["agents"] = 0
                out["working"] = 0
            try:
                from core import journal as J
                d = J.digest()
                out["headline"] = "" if d.startswith("Nothing recorded") else d[:160]
            except Exception:
                out["headline"] = ""
            return JSONResponse(out)

        @app.get("/api/search")
        async def search_get(req: Request, q: str = ""):
            """One search box over everything: memory, the journal, files,
            agents, deliverables, leads and invoices. Searching in six places
            is the same as not searching."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            needle = str(q or "").strip()
            if len(needle) < 3:
                # two characters match everything in a corpus this size, and a
                # search box that returns noise is a search box nobody uses
                return JSONResponse({"results": [], "q": needle,
                                     "hint": "three characters or more"})
            out: list[dict] = []

            def add(kind: str, title: str, where: str, detail: str = "") -> None:
                if len(out) < 60:
                    out.append({"kind": kind, "title": str(title)[:120],
                                "where": where, "detail": str(detail)[:160]})

            try:
                from core import knowledge as K
                for r in K.search(needle, k=6):
                    add("memory", r.get("title") or r.get("text", "")[:80],
                        "📚 Knowledge", r.get("snippet", ""))
            except Exception:
                pass
            try:
                from core import journal as J
                for r in J.search(needle, limit=6):
                    add("journal", r["title"], "📓 Journal", r.get("body", ""))
            except Exception:
                pass
            try:
                from core import files as F
                for r in F.find(needle)[:6]:
                    add("file", r["name"], "📁 Files", r.get("client", ""))
            except Exception:
                pass
            try:
                from core import agents as A
                for r in A.deliverables(limit=200)[:200]:
                    if needle.lower() in r["title"].lower():
                        add("work", r["title"], f"📦 {r['agent']}",
                            f"v{r['version']} {r['kind']}")
            except Exception:
                pass
            return JSONResponse({"results": out, "q": needle,
                                 "count": len(out)})

        @app.get("/api/mcp")
        async def mcp_list(req: Request):
            """Configured MCP servers and their live health.

            Health is computed by actually starting each server, because a
            server that is configured but broken is the case that matters and
            it is invisible otherwise.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import mcp as _mcp
            return JSONResponse({"servers": _mcp.servers(),
                                 "health": _mcp.POOL.health()})

        @app.post("/api/mcp")
        async def mcp_save(req: Request):
            """Add, update or remove an MCP server."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            from core import mcp as _mcp
            op = str(body.get("op") or "add").strip().lower()
            name = str(body.get("name") or "").strip()
            if op in ("remove", "delete"):
                ok = _mcp.remove_server(name)
                if ok:
                    _mcp.POOL.invalidate(name)
                return JSONResponse({"ok": ok, "servers": _mcp.servers()})
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)
            rows = [s for s in _mcp.servers() if s["name"] != name]
            rows.append({
                "name": name,
                "command": str(body.get("command") or "").strip(),
                "args": [str(a) for a in (body.get("args") or [])],
                "env": {str(k): str(v) for k, v in (body.get("env") or {}).items()},
                "url": str(body.get("url") or "").strip(),
                "trusted": bool(body.get("trusted")),
            })
            saved = _mcp.save_servers(rows)
            _mcp.POOL.invalidate(name)
            # report what actually happened rather than assuming success
            verdict = "saved"
            try:
                verdict = f"up, {len(_mcp.POOL.get(name).tools())} tool(s)"
            except Exception as e:
                verdict = f"saved but DOWN — {e}"
            return JSONResponse({"ok": True, "name": name, "status": verdict,
                                 "servers": saved})

        @app.get("/api/credits")
        async def credits(req: Request):
            """Attribution, served from CREDITS.md.

            Deliberately reads the file rather than keeping its own copy of the
            list. A credits panel that can drift from the credits file is a
            credits panel that lies, and the whole point of attribution is that
            it is true even when nobody is maintaining it.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core.data_paths import repo_root
            for cand in (repo_root() / "CREDITS.md",
                         Path(__file__).resolve().parent.parent / "CREDITS.md"):
                try:
                    if cand.exists():
                        return JSONResponse({
                            "markdown": cand.read_text(encoding="utf-8"),
                            "path": cand.name})
                except Exception:
                    continue
            return JSONResponse({"markdown": "", "missing": True,
                                 "error": "CREDITS.md was not included in "
                                          "this build."})

        @app.get("/api/connectors")
        async def connectors_get(req: Request):
            """What is connected, what is optional, and what each one would buy.
            The point is that a missing key is a status, not an error."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            rows = []
            try:
                from core import mail as M
                mcfg = M.config()
                rows.append({"id": "gmail", "name": "Gmail (SMTP + IMAP)",
                             "configured": bool(mcfg.get("address")
                                                 and mcfg.get("app_password")),
                             "detail": mcfg.get("address") or "not set",
                             "buys": "sending approved leads, reading replies",
                             "where": "📡 Leads → Email sending",
                             "cost": "free with an app password"})
            except Exception:
                pass
            try:
                from core import godseye as G
                st = G.provider_state()
                rows.append({"id": "maps", "name": "Google Maps (3D tiles)",
                             "configured": bool(st.get("has_google")),
                             "detail": st.get("google_masked") or "not set",
                             "buys": "photorealistic 3D buildings on the globe",
                             "where": "🛰 → KEYS",
                             "cost": "free tier, metered"})
            except Exception:
                pass
            try:
                from core import godseye as G2
                st2 = G2.provider_state()
                rows.append({"id": "ion", "name": "Cesium ion (terrain)",
                             "configured": bool(st2.get("has_ion")),
                             "detail": st2.get("ion_masked") or "not set",
                             "buys": "real terrain elevation",
                             "where": "🛰 → KEYS",
                             "cost": "free community plan"})
            except Exception:
                pass
            rows.append({"id": "stripe", "name": "Stripe (payment links)",
                         "configured": False, "detail": "not connected",
                         "buys": "a pay button on an invoice",
                         "where": "not needed — invoices work without it",
                         "cost": "per-transaction"})
            # token_key is what tells the panel to render an input. It is the
            # same name POST /api/connectors/token whitelists, so the UI cannot
            # offer a field the API would refuse.
            for cid, mod, label, buys, where, tkey, hint in (
                ("github", "github", "GitHub",
                 "watching CI, reading PRs and issues, commenting on request",
                 "PAT with scope `repo` for private repositories",
                 "github_token", "ghp_… — github.com → Settings → Developer "
                                 "settings → Personal access tokens"),
                ("vercel", "vercel", "Vercel",
                 "seeing which production deploys broke, before you do",
                 "account settings → tokens",
                 "vercel_token", "vercel.com → Account Settings → Tokens"),
                ("hf", "hf", "Hugging Face",
                 "looking up models and restarting your crashed Spaces",
                 "optional — public repos work with no token at all",
                 "hf_token", "hf_… — huggingface.co → Settings → Access Tokens"),
            ):
                try:
                    import importlib
                    m = importlib.import_module(f"core.{mod}")
                    ok = bool(m.configured())
                except Exception:
                    ok = False
                rows.append({"id": cid, "name": label, "configured": ok,
                             "detail": "connected" if ok else "not set",
                             "buys": buys, "where": where,
                             "cost": "free tier, token only",
                             "token_key": tkey, "token_hint": hint})
            rows.append({"id": "calendar", "name": "Calendar (.ics)",
                         "configured": True,
                         "detail": "due dates exported as a real calendar file",
                         "buys": "deadlines in the calendar you actually use",
                         "where": "ask: 'give me the due dates'",
                         "cost": "free"})
            return JSONResponse({"connectors": rows})

        @app.post("/api/connectors/token")
        async def connectors_token(req: Request):
            """Store a connector token under a whitelisted name.

            Whitelisted by name, not by free-form key, so a crafted request
            cannot write an arbitrary field into api_keys.json. The response
            never echoes the value back.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            name = str(body.get("name") or "").strip().lower()
            value = str(body.get("value") or "").strip()
            allowed = {"github_token", "vercel_token", "hf_token"}
            if name not in allowed:
                return JSONResponse(
                    {"error": f"Unknown connector. Use one of: "
                              + ", ".join(sorted(allowed))}, status_code=400)
            if not value:
                return JSONResponse({"error": "Paste the token first."},
                                    status_code=400)
            from core import svc as _svc
            _svc.set_token(name, value)
            if self._key_saved_callback:
                try:
                    self._key_saved_callback()
                except Exception:
                    pass
            return JSONResponse({"ok": True, "name": name})

        # ── Phase 6: knowledge ─────────────────────────────────────────────────
        @app.get("/api/knowledge")
        async def knowledge_search(req: Request, q: str = "", k: int = 5,
                                   view: str = ""):
            """Search documents AND past turns. `view` picks the panel's tab:
            search | docs | recent | stats."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import knowledge as K
            try:
                if view == "docs":
                    return JSONResponse({"docs": K.docs(100), "stats": K.stats()})
                if view == "recent":
                    return JSONResponse({"turns": K.recent_turns(int(k) or 20),
                                         "stats": K.stats()})
                if view == "stats":
                    return JSONResponse({"stats": K.stats()})
                hits = K.search(q, k=int(k) or 5) if q.strip() else []
                return JSONResponse({"hits": hits, "stats": K.stats(), "query": q})
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.post("/api/knowledge")
        async def knowledge_add(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import knowledge as K
            title = str(body.get("title") or "").strip()
            text = str(body.get("text") or "").strip()
            url = str(body.get("url") or "").strip()
            try:
                if url and not text:
                    t, text = await asyncio.to_thread(K.fetch_url, url)
                    title = title or t
                rec = await asyncio.to_thread(
                    K.ingest, title or "note", text,
                    source=str(body.get("source") or "note"), uri=url)
                return JSONResponse(rec, status_code=201)
            except ValueError as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.post("/api/knowledge/forget")
        async def knowledge_forget(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
                doc_id = int(body.get("id"))
            except Exception:
                return JSONResponse({"error": "id required"}, status_code=400)
            from core import knowledge as K
            from core import policy as P
            ok = await asyncio.to_thread(K.forget, doc_id)
            P.audit("knowledge.forget", "delete", actor="user",
                    target=str(doc_id), result="forgotten" if ok else "missing")
            return JSONResponse({"ok": ok})

        # ── Phase 5: policy, audit, approvals ──────────────────────────────────
        # The dashboard is where a policy is actually changed, so this is the
        # config console for it: every tool's tier and approval flag, the daily
        # spend cap, the rate limit, and the audit trail that says what happened.
        @app.get("/api/policy")
        async def policy_get(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import policy as P
            from core import confirm as C
            return JSONResponse({
                "config": P.config(), "tools": P.catalogue(), "budget": P.budget(),
                "inbox": C.inbox(), "tiers": list(P.TIERS),
                "stats": P.audit_stats(),
            })

        @app.post("/api/policy")
        async def policy_set(req: Request):
            """Change the policy. Tiers and caps are the knobs; a tool's tier can
            be moved in the dashboard, which is the point \u2014 no file edits."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import policy as P
            try:
                cfg = P.save_config(body or {})
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)
            try:
                asyncio.create_task(self.broadcast({
                    "type": "policy", "event": "changed"}))
            except Exception:
                pass
            return JSONResponse({"config": cfg, "tools": P.catalogue(),
                                 "budget": P.budget()})

        @app.get("/api/audit")
        async def audit_get(req: Request, limit: int = 120, action: str = "",
                            tier: str = "", actor: str = ""):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import policy as P
            try:
                rows = P.audit_tail(limit, action=action, tier=tier, actor=actor)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)
            return JSONResponse({"rows": rows, "stats": P.audit_stats()})

        @app.get("/api/approvals")
        async def approvals_get(req: Request):
            """The inbox: what is waiting on a human, and the panic button."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import confirm as C
            return JSONResponse({"items": C.inbox()})

        @app.post("/api/approvals/resolve")
        async def approvals_resolve(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import confirm as C
            accepted = bool(body.get("accepted"))
            title = C.pending_title()
            C.resolve(accepted)
            from core import policy as P
            if title:
                P.audit("approval", "act", actor="user", target=title,
                        result="approved" if accepted else "declined",
                        detail="resolved from the dashboard")
            return JSONResponse({"ok": True, "remaining": C.inbox()})

        @app.post("/api/approvals/reject_all")
        async def approvals_reject_all(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import confirm as C
            from core import policy as P
            n = C.reject_all()
            if n:
                P.audit("approval", "act", actor="user", target=f"{n} item(s)",
                        result="rejected_all", detail="panic button")
            return JSONResponse({"ok": True, "rejected": n})

        @app.get("/api/godseye/providers")
        async def godseye_providers_get(req: Request):
            """Masked view of the map/space keys. Never returns the key."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import godseye as _gev
            try:
                return JSONResponse(_gev.provider_state())
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.post("/api/godseye/providers")
        async def godseye_providers_post(req: Request):
            """Save a key, then bounce their Node server so its server-side
            routes pick it up. A masked value (dots) means "leave it alone"."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            from core import godseye as _gev
            patch = {}
            for key in ("google_maps_key", "cesium_ion_token"):
                val = body.get(key)
                if val is None:
                    continue
                val = str(val)
                if set(val) <= {"\u2022", "."} or val.strip() in ("", "***"):
                    continue                      # masked echo, not a real change
                patch[key] = val.strip()
            if not patch:
                return JSONResponse(_gev.provider_state())
            try:
                return JSONResponse({**_gev.save_providers(**patch),
                                     "restarted": True})
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/godseye")
        async def godseye_state(req: Request):
            """What JARVIS can drive, and whether its data server is up.

            Auth-gated like every other /api route: it names the internal port
            the Node server listens on, which is none of the public's business.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            from core import godseye as _gev
            try:
                st = _gev.state()
                st.pop("port", None)
                return JSONResponse(st)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/display")
        async def display_list(req: Request, limit: int = 20):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            return JSONResponse({"items": await asyncio.to_thread(
                _disp().listing, limit)})

        @app.get("/api/display/{aid}", response_class=HTMLResponse)
        async def display_page(aid: str, req: Request):
            """The artifact page itself, for a sandboxed frame to load."""
            rec = await asyncio.to_thread(_disp().get, aid)
            if not rec:
                return HTMLResponse("<h1>gone</h1>", status_code=404)
            if rec.get("kind") == "html":
                return HTMLResponse(rec.get("html") or "", headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy":
                        "default-src 'self' https: data: blob:; "
                        "style-src 'unsafe-inline' https:; "
                        "script-src 'unsafe-inline' 'unsafe-eval' https:; "
                        "img-src data: blob: https:; frame-src https:;",
                })
            return HTMLResponse(
                f"<!DOCTYPE html><meta charset=utf-8><body style='font:14px monospace;"
                f"background:#050a12;color:#d8f8ff;padding:16px'>"
                f"<b>{rec.get('title')}</b><br>{rec.get('kind')} artifact — "
                f"nothing to render here.</body>")

        @app.get("/api/display/{aid}/data")
        async def display_data(aid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            rec = await asyncio.to_thread(_disp().get, aid)
            if not rec:
                return JSONResponse({"error": "gone"}, status_code=404)
            return JSONResponse(rec)

        @app.post("/api/display")
        async def display_save(req: Request):
            """Save an artifact from the panel or a script (not the model)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            d = _disp()
            try:
                rec = await asyncio.to_thread(
                    d.save, str(body.get("kind") or "text"),
                    title=str(body.get("title") or ""),
                    html=str(body.get("html") or ""),
                    url=str(body.get("url") or ""),
                    text=str(body.get("text") or ""),
                    spec=body.get("spec") if isinstance(body.get("spec"), dict) else None,
                    warning=str(body.get("warning") or ""),
                    source=("scheduler"
                            if str(body.get("source") or "").lower() == "scheduler"
                            else "user"))
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            await self.broadcast({"type": "display", "event": "shown",
                                  "id": rec["id"], "kind": rec["kind"],
                                  "source": rec.get("source", "user")})
            return JSONResponse(rec, status_code=201)

        @app.post("/api/display/{aid}/pin")
        async def display_pin(aid: str, req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            rec = await asyncio.to_thread(
                _disp().set_pinned, aid, bool(body.get("pinned", True)))
            if not rec:
                return JSONResponse({"error": "gone"}, status_code=404)
            return JSONResponse(rec)

        @app.post("/api/display/close")
        async def display_close(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            d = _disp()
            aid = str(body.get("id") or "")
            if aid:
                ok = await asyncio.to_thread(d.delete, aid)
                if not ok:
                    return JSONResponse({"error": "gone"}, status_code=404)
            else:
                for r in await asyncio.to_thread(d.listing, 50):
                    await asyncio.to_thread(d.delete, r["id"])
            await self.broadcast({"type": "display", "event": "cleared"})
            return JSONResponse({"ok": True})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            """The WAKE button, and what the browser's phrase check calls when
            it hears the name.

            This used to be a no-op that answered `{"ok": true}`: it called
            `_wake_callback`, which nothing on a Space ever sets, and it never
            told the browser anything — so the client sat on "Sending wake
            signal…" waiting for a `type: "wake"` message that was never sent.
            The button looked broken and the endpoint lied about succeeding.

            Now it does what the endpoint always implied: run the callback if
            the desktop attached one, tell every tab the wake happened, and
            report honestly whether a local detector was even available.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            # Is the *local* detector actually enabled? The flag gates the
            # passive listener on a desktop, not a deliberate button press — so
            # asking, rather than assuming, is the difference between a wake
            # that does nothing and one that works.
            state: dict = {}
            if self._wake_state_provider:
                try:
                    state = dict(self._wake_state_provider() or {})
                except Exception:
                    state = {}
            local_enabled = bool(state.get("enabled")) and bool(state.get("ready"))

            ran = False
            if self._wake_callback and local_enabled:
                try:
                    self._wake_callback()
                    ran = True
                except Exception as e:
                    print(f"[Wake] callback raised: {type(e).__name__}: {e}")
            await self.broadcast({"type": "wake", "at": time.time(),
                                  "source": "button", "local": ran,
                                  "local_enabled": local_enabled,
                                  "awake": bool(state.get("awake", True))})
            note_login()
            if ran:
                note = "the local wake detector woke JARVIS"
            elif local_enabled:
                note = "local detector is on but was already awake"
            else:
                note = ("no local audio device here (this is a server, so the "
                        "wake_word setting cannot do anything) — the signal has "
                        "been broadcast to the open tabs, which is what starts "
                        "the voice session")
            return JSONResponse({"ok": True, "local_detector": ran,
                                 "local_enabled": local_enabled,
                                 "awake": bool(state.get("awake", True)),
                                 "note": note})

        # ── Music mini-player: buttons + track-end advance ───────────────────
        # Voice commands broadcast directly from actions/play_music.py; these
        # two endpoints carry the browser-driven half (player buttons in any
        # tab, and "the song finished") back through the same broadcast so the
        # tab that owns /ws/audio-out executes playback — never two tabs.

        @app.post("/api/music/control")
        async def music_control(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            try:
                from actions import play_music as _pm
                msg = _pm.client_op(body.get("action"), body.get("value"))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            if msg:
                await self.broadcast(msg)
            return JSONResponse({"ok": True})

        @app.post("/api/music/advance")
        async def music_advance(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from actions import play_music as _pm
                msg = _pm.client_advance()
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            await self.broadcast(msg)
            return JSONResponse({"ok": True})

        # ── Music library: manual browse / search / play (no voice needed) ───

        @app.get("/api/music/search")
        async def music_search(req: Request, q: str = "", source: str = "auto"):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from actions import play_music as _pm
                return JSONResponse(_pm.search(q, source))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/music/browse")
        async def music_browse(req: Request, kind: str = "trending"):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from actions import play_music as _pm
                return JSONResponse(_pm.browse(kind))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/music/play")
        async def music_play_queue(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            try:
                from actions import play_music as _pm
                msg = _pm.set_queue(body.get("items"), body.get("index", 0))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            await self.broadcast(msg)
            return JSONResponse({"ok": True, "index": msg["index"],
                                 "count": len(msg["queue"])})

        @app.post("/api/music/jump")
        async def music_jump(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body = await req.json()
            try:
                from actions import play_music as _pm
                msg = _pm.client_jump(body.get("index"))
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            if msg:
                await self.broadcast(msg)
            return JSONResponse({"ok": msg is not None})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    # receive() accepts bytes (PCM) and text (keepalive ping).
                    # During JARVIS speech the mic is gated and sends nothing —
                    # an idle WS was being dropped by the HF proxy, which showed
                    # up as "Voice dropped — reconnecting mic…" mid-conversation.
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    data = msg.get("bytes")
                    if not data:
                        continue
                    # The clap rides the stream the phone is already sending,
                    # so there is no second capture path and no second
                    # permission prompt. Feeding it on a thread keeps a slow
                    # block off this loop.
                    try:
                        if await asyncio.to_thread(self._clap_tap, data):
                            asyncio.create_task(self._welcome())
                    except Exception:
                        pass
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        @app.websocket("/ws/agent")
        async def agent_ws(websocket: WebSocket, token: str = ""):
            """jarvisd limbs dial in here (outbound from their side).

            Protocol: server→daemon {id, type, payload}; daemon→server either
            {id, ok, ...} (reply, resolves the pending future) or
            {type: "chunk", id, text} (streamed output) / {type:"event"}.
            """
            tok = token.strip()
            h = self._agent_hash(tok) if tok else ""
            if not tok or h not in self._agents:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._agent_socks[h] = websocket
            self._agent_seen[h] = time.time()
            name = self._agents.get(h, {}).get("name", "device")
            print(f"[Agent] {name} online")
            await self.broadcast({"type": "device", "event": "online",
                                  "name": name, "devices": self.devices_public()})
            try:
                while True:
                    msg = await websocket.receive_json()
                    self._agent_seen[h] = time.time()
                    mtype = msg.get("type")
                    # Typed messages FIRST: a streamed chunk carries the same
                    # id as its pending request — checking futures first would
                    # resolve the future on chunk #1 and truncate the stream.
                    if mtype == "chunk":
                        cb = self._agent_chunk_cb
                        if cb:
                            try:
                                cb(msg)
                            except Exception:
                                pass
                        continue
                    if mtype == "event":
                        await self.broadcast(
                            {"type": "sys",
                             "text": f"{name}: {msg.get('text', '')}"})
                        continue
                    mid = msg.get("id")
                    if mid and mid in self._agent_futs:
                        fut = self._agent_futs[mid]
                        if not fut.done():
                            fut.set_result(msg)
                        continue
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                self._agent_socks.pop(h, None)
                # Fail only THIS limb's in-flight requests so callers don't
                # sit through their full timeout (other limbs are untouched).
                for rid, owner in list(self._agent_fut_owner.items()):
                    if owner == h:
                        fut = self._agent_futs.get(rid)
                        if fut and not fut.done():
                            fut.set_result({"ok": False, "error": "device_gone"})
                print(f"[Agent] {name} offline")
                await self.broadcast({"type": "device", "event": "offline",
                                      "name": name, "devices": self.devices_public()})

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            # Reconnect recovery: send history as ONE batch so the client can
            # clear/re-render the feed instead of appending duplicates.
            try:
                await websocket.send_json({
                    "type": "history",
                    "entries": list(self._history[-50:]),
                })
            except Exception:
                pass
            # Always leave "Connecting" — pill needs a status on first join.
            try:
                await websocket.send_json(
                    self._last_status
                    or {"type": "status", "state": "sleeping", "raw": "SLEEPING"}
                )
            except Exception:
                pass
            try:
                while True:
                    data = await websocket.receive_json()
                    typ = data.get("type")
                    if typ == "ping":
                        # Client keepalive — answer so half-open proxies close.
                        try:
                            await websocket.send_json({"type": "pong"})
                        except Exception:
                            break
                        continue
                    if typ == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                self._clients.discard(websocket)

        # ── Server-mode control API + browser audio out ──────────────────────

        @app.get("/api/setup/state")
        async def setup_state():
            """Unauth: is there a Gemini key, and are we in server mode?
            Both the app overlay and the login hint read this before anything
            else, so it must never require a token."""
            configured = False
            try:
                from memory.config_manager import is_configured
                configured = is_configured()
            except Exception:
                pass
            return JSONResponse({"configured": configured,
                                 "server_mode": SERVER_MODE})

        @app.post("/api/save-key")
        async def save_key(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad request"}, status_code=400)
            key = str(body.get("key") or "").strip()
            if len(key) < 15:
                return JSONResponse({"error": "Key looks too short"},
                                    status_code=400)
            try:
                from memory.config_manager import save_api_keys
                save_api_keys(key)   # merges into any existing config fields
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            if self._key_saved_callback:
                try:
                    self._key_saved_callback()
                except Exception:
                    pass
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "API key saved."}
            ))
            return JSONResponse({"ok": True})

        @app.post("/api/confirm")
        async def confirm_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            accept = bool(body.get("accept"))
            try:
                from core import confirm as _confirm_gate
                _confirm_gate.resolve(accept)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            return JSONResponse({"ok": True})

        @app.post("/api/mute")
        async def mute_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            muted = False
            if self._mute_callback:
                try:
                    muted = bool(self._mute_callback())
                except Exception:
                    pass
            return JSONResponse({"ok": True, "muted": muted})

        @app.post("/api/ptt")
        async def ptt_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            held = bool(body.get("held"))
            if self._ptt_callback:
                try:
                    self._ptt_callback(held)
                except Exception:
                    pass
            return JSONResponse({"ok": True})

        @app.post("/api/interrupt")
        async def interrupt_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._interrupt_callback:
                try:
                    self._interrupt_callback()
                except Exception:
                    pass
            return JSONResponse({"ok": True})

        @app.get("/api/wake/state")
        async def wake_state_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_state_provider:
                try:
                    return JSONResponse(dict(self._wake_state_provider()))
                except Exception:
                    pass
            return JSONResponse({"enabled": False, "awake": True, "ready": False})

        @app.get("/api/voice")
        async def get_voice_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from memory.config_manager import AVAILABLE_VOICES, get_voice
                return JSONResponse({"voice": get_voice(),
                                     "available": list(AVAILABLE_VOICES)})
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/voice")
        async def set_voice_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            voice = str(body.get("voice") or "").strip()
            try:
                from memory.config_manager import AVAILABLE_VOICES, save_voice
                if voice not in AVAILABLE_VOICES:
                    return JSONResponse(
                        {"error": f"Unknown voice — choose one of {AVAILABLE_VOICES}"},
                        status_code=400)
                save_voice(voice)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
            if self._voice_callback:
                try:
                    self._voice_callback(voice)
                except Exception:
                    pass
            return JSONResponse({"ok": True, "voice": voice})

        @app.get("/api/config")
        async def get_config_ep(req: Request):
            """Gateway/voice settings for the settings overlay.

            Values are the resolved view (env wins over file); `env_locked`
            marks keys an env var is overriding (dash shows ENV-LOCKED, save
            still writes the file). The API key is masked to last-4."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from memory.config_manager import get_openai_settings
                from core import gateway as gw
                s = get_openai_settings()
                vals = dict(s["values"])
                k = vals.get("openai_api_key") or ""
                vals["openai_api_key"] = (
                    f"••••{k[-4:]}" if k else "")
                return JSONResponse({
                    "values": vals,
                    "env_locked": s["env_locked"],
                    "engines": gw.engine_availability(),
                })
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/config")
        async def set_config_ep(req: Request):
            """Merge gateway/voice settings into api_keys.json (whitelist in
            save_openai_settings). Masked keys (••••xxxx) and env-locked
            fields are left unchanged so a save never clobbers a real key or
            pretends to beat the environment."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            try:
                from memory.config_manager import (
                    get_openai_settings, save_openai_settings)
                from core import gateway as gw
                cur = get_openai_settings()
                env_locked = cur["env_locked"]
                raw_file = cur["raw_file"]
                payload = {}
                for key, val in (body or {}).items():
                    if env_locked.get(key):
                        continue   # env wins at read time — don't fake-write
                    if key == "openai_api_key":
                        s = str(val or "")
                        if not s or s.startswith("••••"):
                            continue   # blank/masked = keep existing
                        payload[key] = s.strip()
                        continue
                    if key == "voice_fallback":
                        payload[key] = bool(val)
                        continue
                    if key in ("stt_mode", "tts_mode"):
                        payload[key] = str(val or "auto")
                        continue
                    if key in ("openai_base_url", "openai_model",
                               "voice_model", "openai_voice",
                               "stt_model", "tts_model"):
                        payload[key] = str(val or "").strip()
                if payload:
                    save_openai_settings(**payload)
                # Re-read so the response reflects what is actually live.
                s2 = get_openai_settings()
                vals = dict(s2["values"])
                k = vals.get("openai_api_key") or ""
                vals["openai_api_key"] = f"••••{k[-4:]}" if k else ""
                if self.broadcast:
                    try:
                        await self.broadcast({
                            "type": "sys",
                            "text": "Settings saved — new values apply to "
                                    "the next call/turn.",
                        })
                    except Exception:
                        pass
                return JSONResponse({
                    "ok": True,
                    "values": vals,
                    "env_locked": s2["env_locked"],
                    "engines": gw.engine_availability(),
                })
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.get("/api/metrics")
        async def metrics_ep(req: Request):
            """Live sysmon for the HUD left panel.

            CPU/RAM/uptime/temp come from core.sysmetrics — cgroup-aware, so
            inside the HF container these are the Space's own numbers, not the
            host's. net stays psutil (/proc/net/dev is namespace-correct) and
            is kept as a cached delta sample.
            """
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from core import sysmetrics
                cpu = float(sysmetrics.cpu_percent(interval=None))
                mem = float(sysmetrics.memory_stats()["percent"])
                import psutil
                nc = psutil.net_io_counters()
                now = time.time()
                net = 0.0
                prev = _metrics_cache["net"]
                if prev is not None:
                    dt = now - _metrics_cache["net_t"]
                    if dt > 0.15:
                        net = ((nc.bytes_sent - prev.bytes_sent)
                               + (nc.bytes_recv - prev.bytes_recv)) / dt
                        net /= 1024 * 1024
                if prev is None or now - _metrics_cache["net_t"] > 1.2:
                    _metrics_cache["net"] = nc
                    _metrics_cache["net_t"] = now
                tmp = float(sysmetrics.cpu_temperature())
                up = float(sysmetrics.uptime_seconds())
                procs = sysmetrics.process_count()
                return JSONResponse({
                    "cpu": cpu, "mem": mem, "net": round(net, 1),
                    "gpu": -1.0, "tmp": round(tmp, 1),
                    "uptime": int(up), "procs": procs,
                })
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @app.post("/api/bootstrap-key")
        async def bootstrap_key():
            """Server-mode pairing: mint a one-time PIN for the login form.

            Locked while the dashboard is in use, so a stranger on the LAN
            cannot mint themselves a login. **But it has to expire.** These
            sessions live in memory, so a Space can end up permanently wedged:
            something holds a session, nobody can log in, and redeploying does
            not clear it. That is a lock with no key, which is how this was
            found — an automated smoke test tripped it and the owner was locked
            out of his own dashboard.

            So: after _PAIR_REARM_HOURS with no *login*, pairing re-arms. This
            is not a hole. The key is still delivered only through the owner's
            own console, which is the thing an attacker on the LAN does not
            have. A live dashboard still refuses immediately.
            """
            if not SERVER_MODE:
                return JSONResponse({"error": "Not available"},
                                    status_code=404)
            # Defensive on purpose. This endpoint is the only way back into a
            # wedged Space, so it must never be the thing that 500s. If anything
            # about the session state is missing or half-built, say so and still
            # answer, rather than taking the login page down with it.
            try:
                paired = bool(self._tokens or self._device_sessions)
            except Exception as e:
                print(f"[Pair] could not read session state: {type(e).__name__}: {e}")
                paired = False
            try:
                idle = time.time() - _last_login_at()
            except Exception as e:
                print(f"[Pair] no login clock: {type(e).__name__}: {e}")
                idle = 0.0
            try:
                hours = float(self._PAIR_REARM_HOURS) * 3600
            except Exception:
                hours = 6 * 3600
            if paired and idle < hours:
                # NB: use the class attribute, never a bare name. `_PAIR_REARM_HOURS`
                # unbound is a NameError, and it fires on exactly this branch —
                # the one taken once the Space is paired. That is what made
                # /api/bootstrap-key 500 *after* a successful login while
                # working perfectly before one, which read like a mysterious
                # "intermittent" outage.
                return JSONResponse(
                    {"error": "Already paired — use the console pairing "
                              "link, or log in with your password",
                     "paired": True,
                     "idle_hours": round(idle / 3600, 1),
                     "rearms_in_hours": round(
                         max(0.0, hours / 3600 - idle / 3600), 1)},
                    status_code=403)
                print(f"[Dashboard] Pairing re-armed after "
                      f"{round(idle / 3600, 1)}h with no login.")
            try:
                key = self.new_key(expiry_secs=900)
            except Exception as e:
                return JSONResponse(
                    {"error": f"could not mint a key: {type(e).__name__}: {e}"},
                    status_code=500)
            return JSONResponse({"key": key, "rearmed": bool(paired)})

        # ── persistent password login ────────────────────────────────────
        # Pairing keys stay one-time; the password is the "log in anywhere"
        # credential. pbkdf2-sha256 hash lives in DATA_ROOT config/, and
        # login attempts are rate-limited (10 bad/60s → 60s lock).

        @app.get("/api/auth/state")
        async def auth_state():
            """Public: the login page only needs to know if a password exists."""
            return JSONResponse({"password_set": self._pw_exists()})

        @app.post("/api/auth/set-password")
        async def auth_set_password(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "bad_request"}, status_code=400)
            pw = str(body.get("password") or "")
            if len(pw) < 8:
                return JSONResponse(
                    {"error": "Password must be at least 8 characters"},
                    status_code=400)
            if self._pw_exists():
                # Changing an existing password needs the current one, so a
                # stolen session token can't silently lock the owner out.
                if not self._pw_verify(str(body.get("current") or "")):
                    return JSONResponse(
                        {"error": "Wrong current password"}, status_code=401)
            self._pw_set(pw)
            print("[Dashboard] Dashboard password " +
                  ("changed" if body.get("current") else "set") + ".")
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Dashboard password updated."}))
            return JSONResponse({"ok": True, "password_set": True})

        @app.post("/api/auth/login")
        async def auth_login(req: Request):
            now = time.time()
            if now < self._pw_lock_until:
                return JSONResponse({"error": "locked"}, status_code=429)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "bad_request"}, status_code=400)
            if not self._pw_exists():
                return JSONResponse({"error": "password_not_set"},
                                    status_code=403)
            if not self._pw_verify(str(body.get("password") or "")):
                self._pw_fails = [t for t in self._pw_fails if t > now - 60]
                self._pw_fails.append(now)
                if len(self._pw_fails) >= 10:
                    self._pw_lock_until = now + 60
                    self._pw_fails = []
                    print("[Dashboard] Password login locked for 60s "
                          "(10 bad attempts).")
                return JSONResponse({"error": "bad_password"},
                                    status_code=401)
            self._pw_fails = []
            tok         = secrets.token_urlsafe(32)
            session_key = secrets.token_urlsafe(32)
            dev_tok     = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            note_login()
            self._token_keys[tok] = session_key
            self._device_sessions[dev_tok] = {"session_key": session_key}
            self._aes_key(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys",
                 "text": "Remote connection established (password)."}))
            # device_token: next visit on this browser reconnects silently
            # (same flow as the phone QR pairing).
            return JSONResponse({"ok": True, "token": tok,
                                 "key": session_key,
                                 "device_token": dev_tok})

        @app.websocket("/ws/audio-out")
        async def audio_out_ws(websocket: WebSocket, token: str = ""):
            """Binary PCM from JARVIS → browser speakers (server mode)."""
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            # Newest tab becomes the sole player. Tell the previous one to
            # stop so two open dashboards cannot speak over each other.
            prev = self._audio_out_primary
            if prev is not None and prev is not websocket:
                try:
                    await prev.send_json({"type": "flush"})
                    await prev.close(code=4002)
                except Exception:
                    pass
                self._audio_out_clients.discard(prev)
            self._audio_out_clients.add(websocket)
            self._audio_out_primary = websocket
            # Deliver anything held while the tab was opening / reconnecting.
            if self._audio_out_held:
                held, self._audio_out_held = self._audio_out_held, []
                for h in held:
                    try:
                        await websocket.send_bytes(h)
                    except Exception:
                        break
            try:
                # Client keeps the socket warm with text pings during silence
                # (no PCM while JARVIS speaks). receive() returns those without
                # treating them as close; only disconnect ends the loop.
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
            except WebSocketDisconnect:
                pass
            finally:
                self._audio_out_clients.discard(websocket)
                if self._audio_out_primary is websocket:
                    self._audio_out_primary = None

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = DATA_ROOT / "config" / "certs" / "jarvis.key"
        ssl_cert = DATA_ROOT / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Kept for anything the confirm gate runs on a worker thread (the
        # approved-exec re-send) — it has to get back onto THIS loop.
        self._loop = asyncio.get_running_loop()

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        if not (self._on_replit() or self._on_hf_spaces()):
            asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)

        use_ssl  = self._ssl_enabled()
        ssl_key  = DATA_ROOT / "config" / "certs" / "jarvis.key"
        ssl_cert = DATA_ROOT / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            # Generate the TLS pair on first run so no private key ships in the repo.
            _ensure_certs()
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        base = self.get_url()
        print(f"[Dashboard] {base}")
        if self._on_replit():
            print("[Dashboard] Replit edge: plain HTTP on "
                  f":{PORT}, TLS terminated at {self.public_base_url()}")
        if SERVER_MODE:
            # Server mode has no desktop UI to press Remote Control — mint the
            # pairing key here so the browser can get in on first boot.
            key = self.new_key(expiry_secs=900)
            print("[Dashboard] Server mode — open one of these:")
            print(f"[Dashboard]   Auto-login (15 min): {base}/auto-login?key={key}")
            print(f"[Dashboard]   Or /login with PIN:  {key}")
            print(f"[Dashboard]   Agent pair code:     {self._agent_pair_code}  "
                  "(pair a limb: python jarvisd.py --server <url> --pair <code>)")
        else:
            print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        if self._ws_heartbeat_task is None:
            self._ws_heartbeat_task = asyncio.create_task(self._ws_heartbeat())
        await uvicorn.Server(cfg).serve()
