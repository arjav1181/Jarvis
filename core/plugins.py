"""Adding a capability without a release.

The plugin contract is deliberately tiny — a `PLUGIN` dict and a `run()`
function, nothing else — and that is what makes it the right answer to "I want
a new feature". The thing that was missing was not the contract, it was the
route there.

    A plugin used to mean: commit it, push it, wait out a rebuild. On a hosted
    Space that is ten minutes for a ten-line tool, so editing core looked like
    the faster path and plugins stayed at three shipped examples. The mechanism
    was right and the workflow was wrong.

So there are two directories. `plugins/` in the repository is the reviewed,
version-controlled place, and it is what ships. `<data>/user_plugins` is the
persistent volume: written at runtime, surviving rebuilds because it outlives
    the image, and the place a capability comes from when you do not want a
    release. Repository is scanned first, so a runtime plugin can never quietly
    shadow a reviewed one.

Two properties make this safe enough to leave switched on:

    Installing is validated BEFORE it is kept. A plugin that does not import,
    or has no PLUGIN dict, or is missing run(), is reported with its error and
    never written. You find out in a second with the reason, instead of ten
    minutes later in a container log.

    A broken plugin cannot take the assistant with it. Discovery already
    swallows import errors per file; this only decides what gets written.

One thing to be plain about, because it is not a bug and not an oversight:
installing a plugin is remote code execution by design. The file runs in this
process with this process's access. Every route here is behind the dashboard
token, and the only person who should hold that token is you — the same
position every other authenticated surface in the app is in. It is a door, and
this is where the key goes.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

#: 256 KB is a very large plugin. Anything bigger is a mistake, and a plugin
#: that size is not something you hand-write.
MAX_PLUGIN_BYTES = 256 * 1024

#: Only .py. A plugin that needs data files is a package, not a plugin, and
#: silently accepting other extensions is how a .pyi or a stray upload turns
#: into a confusing failure later.
ALLOWED_SUFFIXES = (".py",)


def repo_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins"


def user_dir() -> Optional[Path]:
    """The persistent, runtime-written directory.

    None when there is no data root at all — a bare library import with no
    writable state, in which case only the repository's plugins exist and
    installing is refused rather than writing somewhere that will vanish.
    """
    try:
        from core.data_paths import data_root
        d = data_root() / "user_plugins"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def dirs() -> list[Path]:
    out = [repo_dir()]
    u = user_dir()
    if u is not None:
        out.append(u)
    return out


def safe_name(name: str) -> str:
    """A filename that can only ever be a file inside the plugins dir.

    Not decoration. The name comes from an upload, and `../../etc/x` is a
    perfectly ordinary string to receive from a web form.
    """
    raw = str(name or "").strip().replace("\\", "/").split("/")[-1]
    raw = raw.split(".")[0]
    keep = [c if (c.isalnum() or c == "_") else "_" for c in raw]
    out = "".join(keep).strip("_")[:48]
    if not out or not out[0].isalpha() and out[0] != "_":
        out = f"p_{out}" if out else "plugin"
    return out


def validate_source(source: str, filename: str = "plugin.py") -> dict:
    """Import a plugin from source in isolation and report what came back.

    Compiles and executes it, deliberately — that is the only way to know a
    plugin works, and executing it here is the same execution it would get
    later. Written to a temporary file so the real plugins directories are
    never touched by something that is about to be rejected.
    """
    import tempfile
    from core.plugin_loader import _validate

    tmp_dir = Path(tempfile.mkdtemp(prefix="jarvis-plugin-check-"))
    path = tmp_dir / (safe_name(filename) + ".py")
    try:
        path.write_text(source, encoding="utf-8")
        compile(source, str(path), "exec")     # syntax first: a clear message
        spec = importlib.util.spec_from_file_location(
            f"jarvis_plugin_check_{int(time.time()*1000)}", path)
        if spec is None or spec.loader is None:
            return {"ok": False, "error": "could not build an import spec"}
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            return {"ok": False,
                    "error": f"it does not import: {type(e).__name__}: {e}"[:200],
                    "where": _where(e)}
        rec = _validate(module, path.name)
        if not rec.valid:
            return {"ok": False, "error": rec.error, "name": rec.name}
        return {"ok": True, "name": rec.name,
                "description": rec.description,
                "parameters": rec.parameters}
    except SyntaxError as e:
        return {"ok": False,
                "error": f"line {e.lineno}: {e.msg}"[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _where(exc: Exception) -> str:
    """Where in the user's file the failure is, not in ours."""
    tb = getattr(exc, "__traceback__", None)
    path = getattr(exc, "__file__", "") or ""
    best = None
    while tb is not None:
        f = tb.tb_frame
        if "jarvis_plugin_check" in str(f.f_code.co_filename) or \
                "plugin_check" in str(f.f_code.co_filename):
            best = (f.f_code.co_filename, tb.tb_lineno)
        tb = tb.tb_next
    if best and "jarvis-plugin-check" in best[0]:
        return f"line {best[1]} of your plugin"
    if path:
        return f"in {Path(path).name}"
    return ""


def install(source: str, filename: str = "plugin.py",
            *, validate_only: bool = False) -> dict:
    """Validate, then write. A plugin that does not work is never kept."""
    if not str(source or "").strip():
        return {"ok": False, "error": "that file is empty"}
    size = len(source.encode("utf-8", "ignore"))
    if size > MAX_PLUGIN_BYTES:
        return {"ok": False,
                "error": f"that is {size // 1024} KB — a plugin should be a "
                          f"few hundred lines, and the limit is "
                          f"{MAX_PLUGIN_BYTES // 1024} KB"}
    name = safe_name(filename)
    if Path(str(filename)).suffix.lower() not in ALLOWED_SUFFIXES:
        return {"ok": False, "error": "a plugin is a .py file"}

    v = validate_source(source, name + ".py")
    if not v.get("ok"):
        return v
    if validate_only:
        return v

    target_dir = user_dir()
    if target_dir is None:
        return {"ok": False,
                "error": "there is no writable data directory here, so a "
                          "runtime plugin cannot be kept. In the repository, "
                          "plugins/ still works."}
    # A repository plugin with the same PLUGIN['name'] would win the scan
    # anyway, and the runtime copy would then be rejected at every discovery
    # with a collision message the user has to decode. Refuse here, by name,
    # where the reason can actually be explained.
    shipped = _name_taken_in_repo(v["name"])
    if shipped:
        return {"ok": False,
                "error": f"'{v['name']}' already ships in the repository as "
                          f"plugins/{shipped}. Edit that one — a copy here "
                          f"would lose to it every time the plugins are "
                          f"scanned."}
    target = target_dir / f"{name}.py"
    if target.exists():
        return {"ok": False,
                "error": f"'{name}.py' is already installed. Remove it first "
                          f"if you meant to replace it."}
    tmp = target_dir / f".{name}.part"
    try:
        tmp.write_text(source, encoding="utf-8")
        tmp.replace(target)            # atomic: never a half-written plugin
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return {"ok": False, "error": f"could not save: {e}"[:160]}
    return {"ok": True, "name": v["name"], "file": target.name,
            "description": v.get("description", ""), "bytes": size,
            "installed": True}


def remove(filename: str) -> dict:
    """Remove a runtime plugin. Repository plugins are not deletable from
    here — they belong to a rebuild, and pretending otherwise would make the
    tool vanish at the next restart and look like a bug."""
    target_dir = user_dir()
    if target_dir is None:
        return {"ok": False, "error": "no writable data directory"}
    name = safe_name(filename)
    path = target_dir / f"{name}.py"
    if not path.is_file():
        return {"ok": False,
                "error": f"'{name}.py' is not a runtime plugin — repository "
                          f"plugins are removed by changing the repository."}
    try:
        path.unlink()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    return {"ok": True, "removed": path.name}


def _name_taken_in_repo(name: str) -> str:
    """The repository file that already claims this plugin name, if any.

    Read as text rather than imported, so asking the question never has the
    side effect of running half a dozen plugins.
    """
    if not name:
        return ""
    try:
        for p in sorted(repo_dir().glob("*.py")):
            if p.name.startswith("_"):
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            m = re.search(r"""["']name["']\s*:\s*["']([A-Za-z_]\w*)["']""", txt)
            if m and m.group(1) == name:
                return p.name
    except Exception:
        pass
    return ""


def installable() -> dict:
    """Whether a plugin can be installed at all, and why not if it cannot."""
    d = user_dir()
    return {"ok": d is not None,
            "dir": str(d) if d is not None else "",
            "max_bytes": MAX_PLUGIN_BYTES,
            "why": "" if d is not None else
                   "there is no writable data directory in this process"}


def inventory() -> dict:
    """What is installed where, without importing anything.

    A listing must never be the thing that can crash: this only stats files
    and reads their PLUGIN name out of the source as text.
    """
    rows = []
    for d in dirs():
        try:
            files = sorted(d.glob("*.py"), key=lambda p: p.name)
        except Exception:
            continue
        for p in files:
            if p.name.startswith("_"):
                continue
            name, size = p.stem, 0
            try:
                size = p.stat().st_size
                txt = p.read_text(encoding="utf-8", errors="replace")
                for line in txt.splitlines():
                    ls = line.strip()
                    if ls.startswith('"name"') or ls.startswith("'name'"):
                        seg = ls.split(":", 1)[-1].strip().rstrip(",")
                        name = seg.strip("\"' ")
                        break
            except Exception:
                pass
            rows.append({"file": p.name, "name": name, "bytes": size,
                         "runtime": d != repo_dir()})
    return {"dirs": [str(d) for d in dirs()], "files": rows}
