"""core/files.py — the filing cabinet, and the thing that makes JARVIS useful
with a client's paperwork.

The list of reasons a freelancer keeps documents is short: a contract, an
invoice, a brief, a spec, a photo of the whiteboard. So this is deliberately
plain — upload a file, get text out of it, find it later by meaning, and attach
it to a client or a deliverable so the paperwork lives next to the work instead
of in a Downloads folder.

Three decisions worth stating:

  * **Text is extracted and indexed at upload time, not on search.** The moment
    a document lands, `knowledge` gets it, so "what did the client say about
    the second payment?" works without anyone opening anything. Extraction is
    best-effort per format and never blocks the upload — a PDF we cannot read is
    still a PDF you can download.

  * **Attachments are references, not copies.** A deliverable points at a file
    id. Re-uploading a contract does not fork the history of who saw which
    version, and deleting a file tells you what it was attached to instead of
    silently orphaning a reference.

  * **The blast radius is small.** A size cap, a filename sanitiser, a deny-list
    for things that should never be stored and served back, and no path ever
    comes from the caller. `resolve()` is the only function that turns an id
    into a path, and it will only return paths inside the uploads directory.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.data_paths import uploads_dir

MAX_BYTES = 25 * 1024 * 1024          # a Space has 16GB; 25MB/file is plenty
INDEX_NAME = "files.json"

TEXTY = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
         ".xml", ".html", ".htm", ".py", ".js", ".ts", ".sql", ".ini", ".log",
         ".rst", ".srt", ".vtt", ".ics"}

_lock = threading.RLock()
_cache: Optional[dict] = None

#: never stored, never served: they are either dangerous or useless here
DENY_EXT = {".exe", ".dll", ".so", ".dylib", ".sh", ".bat", ".cmd", ".com",
            ".msi", ".apk", ".jar", ".pyc", ".scr", ".pif"}


def _dir() -> Path:
    return uploads_dir()


def _index_path() -> Path:
    return _dir() / INDEX_NAME


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            d = json.loads(_index_path().read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        d.setdefault("files", [])
        _cache = d
        return d


def _save() -> None:
    with _lock:
        _dir().mkdir(parents=True, exist_ok=True)
        p = _index_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_load(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)


def reset() -> None:
    global _cache
    with _lock:
        _cache = None


# ── safety ───────────────────────────────────────────────────────────────────

def safe_name(name: str) -> str:
    """A filename that cannot escape the directory and cannot confuse a shell."""
    n = os.path.basename(str(name or "").strip().replace("\\", "/"))
    n = re.sub(r"[\x00-\x1f\x7f]", "", n)
    n = re.sub(r"[^\w.\- ]+", "_", n, flags=re.UNICODE).strip(" .")
    return (n or "file")[:120]


def _ext(name: str) -> str:
    return Path(name).suffix.lower()


def resolve(file_id: str) -> Optional[Path]:
    """The only id → path conversion in the module, and it refuses to leave the
    uploads directory even if the index has been tampered with."""
    rec = get(file_id)
    if not rec:
        return None
    root = _dir().resolve()
    p = (root / rec["stored"]).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None


# ── writing ──────────────────────────────────────────────────────────────────

def _extract(path: Path, ext: str) -> tuple[str, str]:
    """(text, how). Best effort, per format, and never the reason an upload
    fails — a file we cannot read is still a file the user can open."""
    if ext in TEXTY:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:400_000], "text"
        except Exception:
            return "", "text"
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            r = PdfReader(str(path))
            return "\n".join((pg.extract_text() or "") for pg in r.pages[:200]), "pdf"
        except Exception:
            return "", "pdf"
    if ext in (".docx", ".pptx", ".xlsx"):
        try:
            import zipfile
            import re as _re
            with zipfile.ZipFile(path) as z:
                parts = [n for n in z.namelist() if n.endswith(".xml")]
                blob = "".join(z.read(n).decode("utf-8", "replace") for n in parts[:20])
            txt = _re.sub(r"<[^>]+>", " ", blob)
            return _re.sub(r"\s+", " ", txt)[:400_000], "ooxml"
        except Exception:
            return "", "ooxml"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        return "", "image"
    return "", "none"


def store(raw: bytes, filename: str, *, title: str = "", tags: Any = None,
          client: str = "", deliverable: str = "", actor: str = "user",
          index: bool = True) -> dict:
    """`raw` is the bytes; nothing about the caller's filesystem is trusted."""
    if not raw:
        raise ValueError("that file is empty")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"that file is {len(raw) // 1048576}MB — the cap is "
                         f"{MAX_BYTES // 1048576}MB")
    name = safe_name(filename or "file")
    ext = _ext(name)
    if ext in DENY_EXT:
        raise ValueError(f"I will not store a {ext} file — upload the source "
                         f"instead and I will keep that")
    fid = "f-" + uuid.uuid4().hex[:10]
    digest = hashlib.sha256(raw).hexdigest()
    dest_dir = _dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{fid}{ext}"
    (dest_dir / stored).write_bytes(raw)

    text, how = _extract(dest_dir / stored, ext) if index else ("", "skipped")
    rec = {
        "id": fid, "name": name, "stored": stored, "title": title or name,
        "ext": ext, "bytes": len(raw), "sha256": digest,
        "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "tags": sorted({str(t).strip().lower()[:32] for t in (tags or [])
                        if str(t).strip()}),
        "client": str(client or "")[:120],
        "deliverable": str(deliverable or "")[:60],
        "chars": len(text), "extracted": how,
        "doc_id": None, "uploaded": time.time(), "by": str(actor or "user")[:40],
    }
    if text.strip():
        try:
            from core import knowledge as K
            d = K.ingest(rec["title"], text, source="file", uri=f"file:{fid}")
            rec["doc_id"] = d.get("id")
        except Exception:
            rec["doc_id"] = None
    with _lock:
        _load()["files"].append(rec)
        _save()
    try:
        from core import journal as J
        J.entry("done", f"filed {name}"[:180],
                body=f"{len(raw)} bytes · {how or 'not indexed'}"
                     + (f" · {client}" if client else ""),
                tags=["files"], refs=[fid], actor=actor)
    except Exception:
        pass
    return dict(rec)


def store_path(path: str, *, title: str = "", tags: Any = None,
               client: str = "", actor: str = "user") -> dict:
    p = Path(str(path)).expanduser()
    if not p.is_file():
        raise ValueError(f"no file at {p}")
    return store(p.read_bytes(), p.name, title=title, tags=tags,
                 client=client, actor=actor)


def get(file_id: str) -> Optional[dict]:
    want = str(file_id or "").strip().lower()
    with _lock:
        for f in _load()["files"]:
            if want and want in (f["id"].lower(), f["stored"].lower()):
                return dict(f)
    return None


def find(q: str = "", *, tag: str = "", client: str = "",
         deliverable: str = "") -> list[dict]:
    needle = str(q or "").strip().lower()
    with _lock:
        rows = [dict(f) for f in _load()["files"]]
    out = []
    for f in rows:
        if tag and tag.lower() not in (f.get("tags") or []):
            continue
        if client and str(client).lower() not in str(f.get("client", "")).lower():
            continue
        if deliverable and f.get("deliverable") != deliverable:
            continue
        if needle:
            hay = " ".join([f.get("name", ""), f.get("title", ""),
                            " ".join(f.get("tags") or []),
                            str(f.get("client", ""))]).lower()
            if needle not in hay:
                continue
        out.append(f)
    out.sort(key=lambda r: r.get("uploaded", 0), reverse=True)
    return out


def attach(file_id: str, *, client: str = "", deliverable: str = "",
           tags: Any = None) -> dict:
    f = get(file_id)
    if not f:
        raise ValueError(f"no file '{file_id}'")
    with _lock:
        rec = next((x for x in _load()["files"] if x["id"] == f["id"]), None)
        if client:
            rec["client"] = str(client)[:120]
        if deliverable:
            rec["deliverable"] = str(deliverable)[:60]
        if tags:
            rec["tags"] = sorted(set(rec.get("tags") or [])
                                 | {str(t).strip().lower()[:32] for t in tags
                                    if str(t).strip()})
        _save()
    return dict(rec)


def text_of(file_id: str) -> str:
    """The extracted text, re-extracted if the index did not keep it. Used by
    the `files` tool so the model can read a document without downloading it."""
    p = resolve(file_id)
    if not p:
        raise ValueError(f"no file '{file_id}'")
    rec = get(file_id) or {}
    body, _ = _extract(p, rec.get("ext", _ext(p.name)))
    if body.strip():
        return body[:200_000]
    if rec.get("doc_id"):
        try:
            from core import knowledge as K
            d = K.get_doc(int(rec["doc_id"])) if hasattr(K, "get_doc") else None
            if d and d.get("text"):
                return str(d["text"])[:200_000]
        except Exception:
            pass
    return ""


def delete(file_id: str) -> dict:
    f = get(file_id)
    if not f:
        raise ValueError(f"no file '{file_id}'")
    p = resolve(file_id)
    with _lock:
        data = _load()
        data["files"] = [x for x in data["files"] if x["id"] != f["id"]]
        _save()
    if p:
        try:
            p.unlink()
        except Exception:
            pass
    return {"deleted": f["id"], "name": f["name"],
            "was_attached_to": {"client": f.get("client"),
                                "deliverable": f.get("deliverable")}}


def stats() -> dict:
    with _lock:
        rows = _load()["files"]
    tags: dict[str, int] = {}
    for f in rows:
        for t in f.get("tags") or []:
            tags[t] = tags.get(t, 0) + 1
    return {"files": len(rows),
            "bytes": sum(int(f.get("bytes") or 0) for f in rows),
            "indexed": sum(1 for f in rows if f.get("doc_id")),
            "clients": sorted({str(f.get("client")) for f in rows if f.get("client")}),
            "tags": sorted(tags, key=lambda k: (-tags[k], k))[:20]}
