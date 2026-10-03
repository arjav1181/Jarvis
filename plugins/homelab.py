"""Homelab — status and control for your own machines.

Ping hosts, list Docker containers, start/stop a container, report load and
disk. Hosts and defaults come from parameters (nothing hardcoded about YOUR
lab). Uses system `ping` and the `docker` CLI — both spoken-graceful when
absent. Never raises.
"""

import shutil
import subprocess

_DOCKER = shutil.which("docker")
_PING = shutil.which("ping")


def _sh(cmd: list, timeout: int = 30) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "command failed").strip()[:200])
    return p.stdout.strip()


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "status").strip().lower()
    target = (params.get("target") or "").strip()[:200]
    try:
        if action == "ping":
            if not _PING:
                return "No ping tool on this machine, sir."
            host = target or "8.8.8.8"
            out = _sh([_PING, "-c", "3", "-W", "2", host])
            tail = out.strip().splitlines()[-2:]
            return f"{host}: " + " / ".join(t.strip() for t in tail)
        if action == "docker":
            if not _DOCKER:
                return "Docker is not installed here, sir."
            out = _sh([_DOCKER, "ps", "--format",
                       "{{.Names}} ({{.Status}})"])
            return "Running containers:\n" + (out or "none running.")
        if action == "docker_start":
            if not _DOCKER or not target:
                return "Name the container to start, sir."
            _sh([_DOCKER, "start", target])
            return f"{target} started, sir."
        if action == "docker_stop":
            if not _DOCKER or not target:
                return "Name the container to stop, sir."
            _sh([_DOCKER, "stop", target])
            return f"{target} stopped, sir."
        if action == "status":
            load = open("/proc/loadavg").read().split()[:3]
            df = _sh(["df", "-h", "/"]).splitlines()[-1].split()
            return (f"Load {' '.join(load)}, disk {df[3]} free of {df[1]} "
                    f"({df[4]} used), sir.")
        return f"Unknown homelab action '{action}' — try ping, docker, docker_start, docker_stop, status."
    except Exception as e:
        return f"Homelab failed, sir: {e}"


PLUGIN = {
    "server": True,  # acts on the server body (see limits)
    "name": "homelab",
    "description": ("Your homelab by voice: ping hosts, list/start/stop Docker "
                    "containers, report machine load and disk. Trigger on "
                    "'homelab', 'ping', 'docker', 'server status', 'is X up'. "
                    "Do NOT use for GitHub or general web questions."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "ping, docker, docker_start, docker_stop, status"},
            "target": {"type": "STRING",
                       "description": "hostname for ping, container name for start/stop"},
        },
        "required": [],
    },
}
