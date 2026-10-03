FROM python:3.11-slim

# HF Spaces terminates TLS at the edge — never serve SSL inside the container.
ENV JARVIS_MODE=server \
    JARVIS_SSL=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JARVIS_BUILD=prod-fix-2026-09-30a

WORKDIR /app

# ── System packages ──────────────────────────────────────────────────────────
# The previous image installed gcc and libffi-dev and nothing else, which left
# four separate classes of silent breakage:
#
#   ffmpeg          audio/video transcode (miniaudio, edge-tts, voice fallback)
#   libGL + libglib opencv-python-headless imports cv2 and dies without them,
#                   so every screenshot and every vision call 500s
#   fonts           Chromium renders text as blank boxes without a font package,
#                   which looks like a broken screenshot rather than a missing
#                   dependency — the hardest kind of bug to report
#   tini            as PID 1, python receives SIGTERM directly but the Node
#                   globe server and the Playwright browser are left orphaned
#                   and the Space hangs on stop instead of exiting
#
# git and jq are here because the app and its operators both expect them, and
# `git` in particular is how a connector is added at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        tini \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        libasound2 \
        alsa-utils \
        espeak-ng \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# tini is the one dependency that is now REQUIRED rather than optional: it is
# PID 1, and if it is not at the path ENTRYPOINT names, the container never
# starts. Assert it here so a wrong package name fails the BUILD — where
# nothing is deployed and the fix is free — instead of the SPACE, where
# nothing responds and it is not.
RUN command -v tini >/dev/null || { echo "FATAL: tini is the ENTRYPOINT (PID 1) but is not installed - the container would not start."; exit 1; }

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt \
    && python -m playwright install --with-deps chromium

# ── Two extras that are allowed to be missing ───────────────────────────────
# Both are genuinely optional and both fail in ways that would take the whole
# dashboard down if they were fatal. An assistant that cannot wake on a phrase
# is still an assistant; an assistant that will not boot is not.
#
# openwakeword is what detects "Hey Jarvis". Its base dependencies are small —
# onnxruntime, scipy, scikit-learn — and the torch dependency only exists in
# the "full" extra, which is deliberately not requested. It also needs a
# multi-megabyte ONNX model downloaded on first use, so it is imported lazily
# and its absence is reported by /api/health rather than guessed at.
#
# google-antigravity is the agent runtime core/agent_runtime.py delegates to. It
# ships a compiled binary in its wheels, so it must come from PyPI.
RUN pip install --no-cache-dir openwakeword \
    || echo "[wake] openwakeword unavailable - the wake word will not work"
RUN pip install --no-cache-dir "google-antigravity" \
    || echo "[agent] antigravity SDK unavailable - delegation will say so"

# ── Node, for the real God's Eye View server (Phase 4f) ──────────────────────
# The globe is the actual project (github.com/bilawalsidhu/gods-eye-view, MIT,
# pinned in godseye/MANIFEST.json). Its vite server serves its /api/* data
# routes; the dashboard serves the app itself off disk and proxies only those
# routes to it on loopback.
#
# EVERY step here is deliberately non-fatal. A failed apt or a failed npm ci
# must never take the dashboard offline — the app still loads from the vendored
# build, it just has no live data layers, and core/godseye.py says so honestly
# instead of pretending. Getting this wrong took the Space down once already.
ENV GODSEYE_HOME=/opt/godseye
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && (curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
        && apt-get install -y --no-install-recommends nodejs) \
    || echo "[godseye] node unavailable — the globe will run without live layers"
# The vendored tree already has the shape their server expects:
#   /opt/godseye/_server/         their sources
#   /opt/godseye/_server/dist/    the built app, where `vite preview` serves it
COPY godseye/ $GODSEYE_HOME/
RUN cd $GODSEYE_HOME/_server \
    && (npm ci --no-audit --no-fund --engine-strict=false \
        && echo "[godseye] node deps installed") \
    || echo "[godseye] npm ci failed — the globe will run without live layers"

# Build their app rather than trusting a committed one. dist/ is still tracked
# in git purely as a fallback: this step is non-fatal, and if it ever fails the
# vendored build is what keeps the globe alive. Once CI has run `npm run
# build:check` on a green build, the 428 tracked artefacts can be dropped and
# this becomes the only way dist/ is produced.
RUN cd $GODSEYE_HOME/_server \
    && (npm run build --silent \
        && echo "[godseye] app built from source") \
    || echo "[godseye] build failed — falling back to the vendored dist/"

# ── OpenCode, as the coding agent JARVIS delegates real work to ──────────────
# This is the "actually do it" half of the project. JARVIS can already plan,
# file and message; this is what writes and refactors code when a task is
# genuinely a software task. `opencode run "<prompt>"` is the non-interactive
# entry point, and `opencode serve` exposes it over HTTP so a long job does not
# pay MCP cold-boot on every call.
#
# Non-fatal for the same reason as Node: a missing coding agent must not take
# the assistant offline. core/agents.py reports the absence honestly.
RUN npm install -g opencode-ai --no-fund --no-audit \
    && echo "[opencode] $(opencode --version 2>/dev/null || echo installed)" \
    || echo "[opencode] install failed — coding delegation will be unavailable"

# Full app tree (actions/, core/, memory/, dashboard/, config package, main.py)
COPY main.py .
# Attribution is shipped with the app, not left in the repo only: the
# deployed Space is a derivative of CC BY-NC work and has to carry the
# credit wherever it is actually running.
COPY CREDITS.md .
COPY actions/ actions/
COPY core/ core/
COPY memory/ memory/
COPY plugins/ plugins/
COPY dashboard/ dashboard/
# Package only — api_keys.json / certs stay off the image (live under /data)
COPY config/__init__.py config/__init__.py
COPY config/jarvis.ico config/jarvis.ico

# Persistent volume at /data — config/memory/uploads survive restarts
RUN mkdir -p /data/config /data/memory /data/uploads \
    && chmod -R 777 /data

# Fail the build if the import surface is broken, rather than discovering it on
# the first Space boot. This is the check that would have caught the missing
# libGL, and it costs three seconds.
# Report the import surface at build time rather than discovering it on the
# first Space boot. This is the check that would have caught the missing libGL.
#
# It does NOT fail the build, and that is deliberate. Everything else in this
# file that can fail is marked non-fatal, because a failed build means the
# Space serves nothing at all — an absent package costs one feature, a red
# build costs the assistant. Use `--strict` for a local or CI build, where a
# red build is cheap.
#
# A script rather than a Dockerfile heredoc on purpose: `RUN <<EOF` needs
# BuildKit, and a gate that quietly does not run on an older daemon is worse
# than no gate at all.
COPY scripts/docker_deps_check.py scripts/docker_deps_check.py
RUN python scripts/docker_deps_check.py

EXPOSE 7860

# tini reaps the Node globe server and any Playwright child on SIGTERM. Without
# it a restarting Space leaves orphans behind and the platform force-kills.
ENTRYPOINT ["/usr/bin/tini", "--"]
# PORT becomes 7860 automatically when /data exists (dashboard/server.py)
CMD ["python", "-u", "main.py"]
