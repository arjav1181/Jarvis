"""
core/tasks.py — persisted task store for Phase 2 (agentic coding).

A task = one unit of delegated coding work:

    {id, prompt, where, device, repo, model, status, diff, branch, ...}
    + a per-task log file (tasks/<id>.log)

States: queued → running → done | failed | cancelled
(where = "device" → jarvisd runs opencode (worktree-isolated on that
 machine); where = "space" → opencode runs here in the given repo.)

Every mutation broadcasts {"type":"task", ...} over the dashboard WS so the
Tasks panel streams live. Persisted at data_root()/tasks.json so HF /data
survives restarts.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.data_paths import config_dir, data_root

TERMINAL = {"done", "failed", "cancelled"}
STATUSES = ("queued", "running", "done", "failed", "cancelled")
WHERES = ("device", "space")


class TaskStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root else data_root()
        self._path = self._root / "tasks.json"
        self._logdir = self._root / "tasks"
        self._cfg_path = config_dir() / "tasks.json" if root is None \
            else Path(root) / "config" / "tasks.json"
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._broadcast: Callable[[dict], Any] | None = None   # async fn or None
        self._load()
        try:
            self._logdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ── persistence ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._tasks = {str(k): v for k, v in raw.items()
                               if isinstance(v, dict)}
        except (OSError, ValueError):
            self._tasks = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._tasks, indent=1, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    # ── wiring ──────────────────────────────────────────────────────────────

    def bind(self, broadcast) -> None:
        """broadcast: async (msg: dict) -> None — the dashboard WS fan-out."""
        self._broadcast = broadcast

    def _emit(self, msg: dict) -> None:
        if self._broadcast is None:
            return
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._broadcast(msg))

    # ── default target (dashboard settings) ─────────────────────────────────

    def default_where(self) -> str:
        try:
            cfg = json.loads(self._cfg_path.read_text(encoding="utf-8"))
            v = str(cfg.get("default_where") or "")
            if v in WHERES:
                return v
        except (OSError, ValueError):
            pass
        return "device"

    def set_default_where(self, where: str) -> str:
        if where not in WHERES:
            raise ValueError(f"where must be one of {WHERES}")
        try:
            self._cfg_path.parent.mkdir(parents=True, exist_ok=True)
            self._cfg_path.write_text(
                json.dumps({"default_where": where}, indent=1),
                encoding="utf-8")
        except OSError:
            pass
        return where

    # ── CRUD ────────────────────────────────────────────────────────────────

    def create(self, *, prompt: str, where: str = "device",
               repo: str = "", device: str = "", model: str = "",
               source: str = "voice") -> dict:
        prompt = str(prompt or "").strip()[:4000]
        if not prompt:
            raise ValueError("prompt is required")
        if where not in WHERES:
            raise ValueError(f"where must be one of {WHERES}")
        with self._lock:
            tid = "t-" + secrets.token_hex(3)
            while tid in self._tasks:
                tid = "t-" + secrets.token_hex(3)
            rec = {
                "id":      tid,
                "prompt":  prompt,
                "where":   where,
                "repo":    str(repo or "")[:600],
                "device":  str(device or "")[:60],
                "model":   str(model or "")[:80],
                "source":  source,
                "status":  "queued",
                "created": round(time.time()),
                "updated": round(time.time()),
                "diff":    "",
                "branch":  "",
                "wt":      "",
                "tail":    "",
                "pushed":  False,
            }
            self._tasks[tid] = rec
            self._save()
        self._emit({"type": "task", "event": "created", "task": dict(rec)})
        return dict(rec)

    def get(self, tid: str) -> dict | None:
        rec = self._tasks.get(str(tid))
        return dict(rec) if rec else None

    def list(self, status: str = "", limit: int = 100) -> list[dict]:
        recs = sorted(self._tasks.values(),
                      key=lambda r: r.get("created", 0), reverse=True)
        if status:
            recs = [r for r in recs if r.get("status") == status]
        return [dict(r) for r in recs[:max(1, min(limit, 500))]]

    def update(self, tid: str, **fields: Any) -> dict:
        with self._lock:
            rec = self._tasks.get(str(tid))
            if rec is None:
                raise KeyError(tid)
            for k, v in fields.items():
                if k in ("id",):
                    continue
                rec[k] = v
            rec["updated"] = round(time.time())
            out = dict(rec)
            self._save()
        self._emit({"type": "task", "event": "update", "task": out})
        return out

    def start(self, tid: str) -> dict:
        return self.update(tid, status="running",
                           started=round(time.time()))

    def finish(self, tid: str, status: str, **fields: Any) -> dict:
        if status not in TERMINAL:
            raise ValueError(f"status must be one of {sorted(TERMINAL)}")
        return self.update(tid, status=status,
                           finished=round(time.time()), **fields)

    # ── logs ────────────────────────────────────────────────────────────────

    def log_path(self, tid: str) -> Path:
        return self._logdir / f"{str(tid).replace('/', '_')}.log"

    def append_log(self, tid: str, line: str) -> None:
        line = str(line).rstrip("\n")[:4000]
        try:
            self._logdir.mkdir(parents=True, exist_ok=True)
            with self.log_path(tid).open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self._emit({"type": "task", "event": "log",
                    "id": str(tid), "line": line})

    def read_log(self, tid: str, tail: int = 500) -> str:
        try:
            txt = self.log_path(tid).read_text(encoding="utf-8",
                                               errors="replace")
        except OSError:
            return ""
        lines = txt.splitlines()
        return "\n".join(lines[-max(1, min(tail, 5000)):])

    # ── helpers for the runner ──────────────────────────────────────────────

    def resolve_where(self, where: str = "") -> str:
        where = (where or "").lower().strip()
        if where in ("auto", "default", ""):
            return self.default_where()
        if where == "pc":
            return "device"
        if where in WHERES:
            return where
        raise ValueError(f"unknown target {where!r} — use device or space")
