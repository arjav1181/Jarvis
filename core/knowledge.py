"""core/knowledge.py — it should remember, not re-ask.

Phase 6. Two things live in one store:

  * **turns** — every conversation turn, so "what did I say yesterday" is a
    query and not a scroll-back;
  * **chunks** — documents the user (or JARVIS) chose to keep, embedded for
    semantic recall.

Embeddings are Gemini's, stored as float32 blobs in plain SQLite, and searched
with a cosine over numpy. PLAN.md originally said sqlite-vec; that is a Rust
extension with no wheel for this platform, and adding a compiler to the Space
image to save 20ms of search on a few thousand chunks is the wrong trade. The
search is O(n·d) in numpy and stays instant to ~50k chunks.

The important design choice is the **lexical fallback**: if there is no API key,
or the embedding call fails, search still works on token overlap. Recall that
degrades honestly is far better than a vector store that returns nothing and
looks broken.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from core.data_paths import data_root

CHUNK_CHARS = 900          # ~a paragraph or two; big enough to be a fact
CHUNK_OVERLAP = 120        # so a fact spanning the cut is still found
# gemini-embedding-001 is the model this API version actually serves;
# text-embedding-004 404s on v1beta embed_content. Probed, not assumed.
EMBED_MODEL = "gemini-embedding-001"
DIM = 768

_lock = threading.RLock()
_db: Optional[sqlite3.Connection] = None


# ── store ────────────────────────────────────────────────────────────────────

def _path() -> Path:
    return data_root() / "knowledge.sqlite"


def db() -> sqlite3.Connection:
    global _db
    with _lock:
        if _db is not None:
            return _db
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe HERE specifically because every access
        # in this module is wrapped in _lock. Without it, the server's
        # asyncio.to_thread workers each get a different thread and the second
        # one to touch the database raises.
        conn = sqlite3.connect(str(p), timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS docs (
            id INTEGER PRIMARY KEY, title TEXT, source TEXT, uri TEXT,
            created REAL, meta TEXT, chunks INTEGER DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY, doc_id INTEGER, ord INTEGER, text TEXT,
            vec BLOB, dim INTEGER, ts REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY, ts REAL, role TEXT, text TEXT, session TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts)")
        conn.commit()
        _db = conn
        return _db


def reset() -> None:
    """Test/deploy hook."""
    global _db
    with _lock:
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass
        _db = None


# ── embeddings ───────────────────────────────────────────────────────────────

def _api_key() -> str:
    try:
        from core import gemini
        return (gemini.api_key() or "").strip()
    except Exception:
        return ""


def embed(texts: list[str], *, timeout: float = 25.0) -> Optional[list[list[float]]]:
    """Gemini embeddings, or None. Never raises: callers fall back to lexical."""
    if not texts:
        return []
    key = _api_key()
    if not key:
        return None
    try:
        from google import genai
        from google.genai import types as gtypes
        client = genai.Client(api_key=key)
        out: list[list[float]] = []
        # one call for the whole batch where it fits, chunked to stay in limits
        for i in range(0, len(texts), 20):
            batch = [t[:6000] for t in texts[i:i + 20]]
            resp = client.models.embed_content(
                model=EMBED_MODEL, contents=batch,
                config=gtypes.EmbedContentConfig(output_dimensionality=DIM))
            for e in (resp.embeddings or []):
                out.append(list((e.values or [])))
        return out or None
    except Exception:
        return None


def _pack(vec: list[float]) -> bytes:
    import struct
    return struct.pack(f"<{len(vec)}f", *[float(v) for v in vec])


def _unpack(blob: bytes) -> list[float]:
    import struct
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ── lexical fallback ─────────────────────────────────────────────────────────

_WORD = re.compile(r"[a-z0-9_]{2,}")

_STOP = {"the", "and", "for", "you", "that", "this", "with", "was", "are", "but",
         "not", "have", "has", "had", "from", "they", "them", "his", "her",
         "she", "him", "its", "our", "your", "what", "when", "where", "who",
         "how", "why", "can", "could", "would", "should", "will", "did", "does",
         "been", "there", "here", "about", "just", "like", "get", "got"}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def _lexical_score(query: str, text: str) -> float:
    """Token overlap with a small idf-ish twist: rarer words count more."""
    q = set(_tokens(query))
    if not q:
        return 0.0
    t = _tokens(text)
    if not t:
        return 0.0
    overlap = sum(1 for w in set(t) if w in q)
    if not overlap:
        return 0.0
    # reward density, not just presence
    density = overlap / (len(set(t)) ** 0.5)
    return overlap * 0.6 + density


# ── transcripts ──────────────────────────────────────────────────────────────

def record_turn(role: str, text: str, *, session: str = "") -> bool:
    """One conversation turn. Cheap enough to always call."""
    text = str(text or "").strip()
    if not text:
        return False
    try:
        with _lock:
            db().execute(
                "INSERT INTO turns (ts, role, text, session) VALUES (?,?,?,?)",
                (time.time(), str(role or "?"), text[:4000], str(session or "")))
            db().commit()
        return True
    except Exception:
        return False


def recent_turns(limit: int = 20, *, role: str = "", query: str = "") -> list[dict]:
    with _lock:
        c = db()
        if query:
            rows = c.execute(
                "SELECT * FROM turns WHERE text LIKE ? ORDER BY ts DESC LIMIT ?",
                (f"%{query}%", max(1, min(limit, 500)))).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM turns WHERE (? = '' OR role = ?) "
                "ORDER BY ts DESC LIMIT ?",
                (role, role, max(1, min(limit, 500)))).fetchall()
    return [{"ts": r["ts"], "role": r["role"], "text": r["text"],
             "session": r["session"]} for r in rows]


def turn_count() -> int:
    with _lock:
        return int(db().execute("SELECT COUNT(*) c FROM turns").fetchone()["c"])


# ── ingest ───────────────────────────────────────────────────────────────────

def _chunk_text(text: str) -> list[str]:
    """Split on paragraph boundaries, then hard-wrap anything oversized."""
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not text:
        return []
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    out: list[str] = []
    buf = ""
    for para in paras:
        while len(para) > CHUNK_CHARS:
            head, para = para[:CHUNK_CHARS], para[CHUNK_CHARS - CHUNK_OVERLAP:]
            out.append(head)
        if len(buf) + len(para) + 2 <= CHUNK_CHARS:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                out.append(buf)
            buf = para
    if buf:
        out.append(buf)
    return [c for c in out if len(c) > 20]


def ingest(title: str, text: str, *, source: str = "note", uri: str = "",
           meta: Optional[dict] = None) -> dict:
    """Store a document and make it recallable. Returns the doc record."""
    parts = _chunk_text(text)
    if not parts:
        raise ValueError("nothing worth storing — that text is empty or too short")
    vecs = embed(parts)
    with _lock:
        c = db()
        cur = c.execute(
            "INSERT INTO docs (title, source, uri, created, meta, chunks) "
            "VALUES (?,?,?,?,?,?)",
            (str(title or "untitled")[:160], str(source or "note")[:40],
             str(uri or "")[:300], time.time(),
             json.dumps(meta or {}, ensure_ascii=False), len(parts)))
        doc_id = int(cur.lastrowid)
        now = time.time()
        for i, part in enumerate(parts):
            vec = vecs[i] if vecs and i < len(vecs) else None
            c.execute(
                "INSERT INTO chunks (doc_id, ord, text, vec, dim, ts) "
                "VALUES (?,?,?,?,?,?)",
                (doc_id, i, part,
                 _pack(vec) if vec else None, len(vec) if vec else 0, now))
        c.commit()
    return {"id": doc_id, "title": str(title or "untitled")[:160],
            "source": str(source or "note")[:40], "uri": str(uri or "")[:300],
            "chunks": len(parts), "embedded": bool(vecs)}


def fetch_url(url: str, *, limit: int = 40000) -> tuple[str, str]:
    """Title + readable text from a URL. Deliberately plain: strip tags, keep
    paragraph breaks, no readability library to keep in the image."""
    req = urllib.request.Request(url, headers={"User-Agent": "Jaarvis/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", "replace")
        ctype = r.headers.get("content-type", "")
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else (url.split("/")[-1] or url)
    if "html" not in ctype.lower() and "html" not in raw[:200].lower():
        return title, raw[:limit]
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", raw)
    text = re.sub(r"<[^>]+>", " ", raw)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return title, text.strip()[:limit]


def forget(doc_id: int) -> bool:
    with _lock:
        c = db()
        row = c.execute("SELECT id FROM docs WHERE id = ?", (int(doc_id),)).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM chunks WHERE doc_id = ?", (int(doc_id),))
        c.execute("DELETE FROM docs WHERE id = ?", (int(doc_id),))
        c.commit()
    return True


def docs(limit: int = 100) -> list[dict]:
    with _lock:
        rows = db().execute(
            "SELECT * FROM docs ORDER BY created DESC LIMIT ?",
            (max(1, min(int(limit or 100), 500)),)).fetchall()
    return [{"id": r["id"], "title": r["title"], "source": r["source"],
             "uri": r["uri"], "created": r["created"], "chunks": r["chunks"],
             "meta": json.loads(r["meta"] or "{}")} for r in rows]


# ── search ───────────────────────────────────────────────────────────────────

def search(query: str, *, k: int = 5, include_turns: bool = True) -> list[dict]:
    """Semantic when embeddings exist, lexical otherwise. Always returns
    something if any text overlaps, and says which mode it used."""
    query = str(query or "").strip()
    if not query:
        return []
    k = max(1, min(int(k or 5), 25))
    qv = embed([query])
    use_vectors = bool(qv and qv[0])
    scored: list[tuple[float, dict]] = []

    with _lock:
        c = db()
        if use_vectors:
            rows = c.execute(
                "SELECT c.id, c.doc_id, c.text, c.vec, d.title, d.source, d.uri "
                "FROM chunks c LEFT JOIN docs d ON d.id = c.doc_id").fetchall()
            import numpy as np
            qa = np.asarray(qv[0], dtype="float32")
            qn = float(np.linalg.norm(qa)) or 1.0
            for r in rows:
                if not r["vec"]:
                    continue
                va = np.asarray(_unpack(r["vec"]), dtype="float32")
                if va.shape[0] != qa.shape[0]:
                    continue
                vn = float(np.linalg.norm(va)) or 1.0
                score = float(np.dot(qa, va) / (qn * vn))
                scored.append((score, _row(r)))
        # turns are always searched lexically: a conversation turn is short and
        # its own words are the best signal there is
        if include_turns:
            for r in c.execute(
                    "SELECT id, ts, role, text FROM turns "
                    "ORDER BY ts DESC LIMIT 2000").fetchall():
                s = _lexical_score(query, r["text"])
                if s > 0:
                    scored.append((s * 0.8, {
                        "kind": "turn", "text": r["text"][:600],
                        "title": f"{r['role']} said this",
                        "source": "conversation", "uri": "",
                        "ts": r["ts"], "doc_id": None}))

    if not use_vectors:
        with _lock:
            for r in db().execute(
                    "SELECT c.id, c.doc_id, c.text, d.title, d.source, d.uri "
                    "FROM chunks c LEFT JOIN docs d ON d.id = c.doc_id").fetchall():
                s = _lexical_score(query, r["text"])
                if s > 0:
                    scored.append((s * 0.7, _row(r)))

    scored.sort(key=lambda x: -x[0])
    out = []
    for score, row in scored[:k]:
        row["score"] = round(float(score), 4)
        out.append(row)
    return out


def _row(r) -> dict:
    return {"kind": "doc", "text": r["text"][:800], "title": r["title"] or "untitled",
            "source": r["source"] or "", "uri": r["uri"] or "", "doc_id": r["doc_id"],
            "ts": None}


def recall_block(query: str, *, k: int = 4, budget: int = 1200) -> str:
    """A prompt-sized block of what we already know. Returns '' when we don't,
    so the model is never handed an empty 'here is what you remember' header."""
    hits = search(query, k=k)
    if not hits:
        return ""
    lines, used = [], 0
    for h in hits:
        snippet = " ".join(str(h.get("text") or "").split())
        room = budget - used
        if room <= 60:
            break
        snippet = snippet[:room]
        used += len(snippet) + 40
        where = h.get("title") or h.get("source") or "memory"
        lines.append(f"- [{where}] {snippet}")
    if not lines:
        return ""
    return "Relevant things you already know:\n" + "\n".join(lines)


def stats() -> dict:
    with _lock:
        c = db()
        d = int(c.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"])
        ch = int(c.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"])
        vecs = int(c.execute(
            "SELECT COUNT(*) c FROM chunks WHERE vec IS NOT NULL").fetchone()["c"])
        t = int(c.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"])
        newest = c.execute("SELECT MAX(ts) t FROM turns").fetchone()["t"]
    return {"docs": d, "chunks": ch, "embedded_chunks": vecs, "turns": t,
            "db": str(_path()), "bytes": _path().stat().st_size
            if _path().exists() else 0,
            "newest_turn": newest, "embed_model": EMBED_MODEL,
            "mode": "semantic" if vecs else "lexical",
            "api_key": bool(_api_key())}


def describe() -> str:
    s = stats()
    return (f"knowledge: {s['docs']} docs / {s['chunks']} chunks "
            f"({s['embedded_chunks']} embedded, {s['mode']}) · {s['turns']} turns")


# ── what goes in the prompt ──────────────────────────────────────────────────

_pending: dict[str, str] = {}


def note_query(text: str) -> None:
    """Called with the user's last words so the NEXT system prompt can carry
    what we already know about the thing they are talking about.

    Best-effort and off the hot path: an embedding call per turn is a latency
    risk, so a failure just means the prompt carries the index instead.
    """
    text = str(text or "").strip()
    if len(text) < 8:
        return
    try:
        block = recall_block(text, k=3, budget=700)
        _pending["recall"] = block
    except Exception:
        pass


def index_line(limit: int = 12) -> str:
    """Titles only. Listing documents is cheap; pasting their contents into every
    prompt is not, and the model can search when it needs the detail."""
    try:
        ds = docs(limit)
    except Exception:
        return ""
    if not ds:
        return ""
    names = ", ".join(f"{d['title']}" + (f" ({d['source']})" if d.get("source") else "")
                      for d in ds[:limit])
    more = f" and {len(ds) - limit} more" if len(ds) > limit else ""
    return f"You have {len(ds)} stored document(s): {names}{more}. " \
           f"Use the knowledge tool to read the detail of any of them."


def prompt_block(budget: int = 900) -> str:
    """The knowledge section of the system prompt. Empty when there is nothing,
    so the model is never handed an empty 'here is what you remember'."""
    bits = []
    try:
        rec = _pending.pop("recall", "") or ""
    except Exception:
        rec = ""
    if rec:
        bits.append(rec)
    try:
        idx = index_line()
    except Exception:
        idx = ""
    if idx:
        bits.append(idx)
    if not bits:
        return ""
    return "[WHAT YOU KNOW]\n" + "\n".join(bits) + "\n"
