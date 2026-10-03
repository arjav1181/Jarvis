"""E2E — Phase 6: knowledge, transcripts and the recall that goes in the prompt.

The claim under test is narrow and important: **JARVIS stops asking things it
was already told.** So the checks are not "does the vector store work" — they
are:

  * a paraphrase with NO shared keywords still finds the document (that is
    embeddings, not grep);
  * a past conversation turn is findable by what was said, not by document id;
  * the prompt block is empty when there is nothing to say, so the model is
    never handed an empty "here is what you remember" header;
  * deleting a document really removes it;
  * the lexical fallback works with no API key, because a vector store that
    returns nothing and looks broken is worse than one that degrades honestly.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_E2E_PORT") or "3117")
BASE = f"http://127.0.0.1:{PORT}"
DATA = ROOT / "e2e" / "data_knowledge"
LOG = Path("/tmp/jarvis_e2e_knowledge.log")

_failures: list[str] = []
_passes: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passes.append(name)
        show = detail if detail and any(c.isdigit() for c in str(detail)) else ""
        print(f"  PASS  {name}" + (f" — {show}" if show else ""), flush=True)
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  FAIL  {name} — {str(detail)[:150]}", flush=True)


# ── unit ─────────────────────────────────────────────────────────────────────

def unit() -> None:
    print("== unit: store, chunking, lexical fallback ==", flush=True)
    # keep the unit phase out of the repo root
    tmp = Path("/tmp/jarvis_e2e_knowledge_unit")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    os.environ["JARVIS_DATA"] = str(tmp)
    sys.path.insert(0, str(ROOT))
    from core import knowledge as K
    K.reset()

    check("empty_prompt_block", K.prompt_block() == "",
          repr(K.prompt_block()[:40]))
    check("empty_search", K.search("") == [] and K.search("   ") == [])
    try:
        K.ingest("nothing", "y")
        check("rejects_empty_text", False, "accepted 1 char")
    except ValueError:
        check("rejects_empty_text", True)

    # chunking: paragraphs joined, oversized paragraphs hard-wrapped
    long_para = "word " * 600
    parts = K._chunk_text(long_para)
    check("chunks.hard_wraps_long_text", len(parts) > 1, f"{len(parts)} parts")
    check("chunks.respect_the_limit", all(len(p) <= K.CHUNK_CHARS + 60 for p in parts),
          max(len(p) for p in parts))
    joined = K._chunk_text("one para\n\ntwo para\n\nthree para")
    check("chunks.keep_paragraphs", len(joined) == 1 and "two para" in joined[0],
          joined)

    # turns
    K.record_turn("user", "my hourly rate is 85 euros, I do Next.js and React")
    K.record_turn("jarvis", "Noted — 85 EUR per hour")
    check("turns.recorded", K.turn_count() == 2, K.turn_count())
    check("turns.ignore_empty", K.record_turn("user", "   ") is False)
    hits = K.search("what do I charge")
    kinds = {h["kind"] for h in hits}
    check("turns.findable_by_meaning", "turn" in kinds, [h["kind"] for h in hits[:3]])
    check("turns.filter_by_role",
          all(r["role"] == "jarvis" for r in K.recent_turns(5, role="jarvis")))

    # lexical fallback: no key, and it must still find things
    real = K._api_key
    K._api_key = lambda: ""
    try:
        K.ingest("Client Acme", "Acme wants a Next.js dashboard. Budget 4000 EUR. "
                 "Contact ops@acme.example. Weekly sprints, Slack.", source="note")
        hits = K.search("acme budget euros")
        check("fallback.finds_documents", any(h["kind"] == "doc" for h in hits),
              [h["kind"] for h in hits[:3]])
        check("fallback.still_finds_turns", any(h["kind"] == "turn" for h in hits))
        check("fallback.mode_says_lexical", K.stats()["mode"] == "lexical", K.stats())
    finally:
        K._api_key = real

    # a nonsense query returns nothing rather than everything
    check("nonsense_query_is_empty", K.search("zzqqxx unrelated gibberish") == [])

    # prompt block
    block = K.prompt_block()
    check("prompt.has_section", "[WHAT YOU KNOW]" in block, block[:60])
    check("prompt.mentions_documents", "stored document" in block, block[:120])
    check("prompt.uses_real_newlines", "\\n" not in block and "\n" in block,
          repr(block[:60]))
    K.note_query("what is the acme budget")
    check("note_query_stashes_recall", "[WHAT YOU KNOW]" in K.prompt_block())
    K.note_query("hi")           # too short to be worth a recall

    # forget
    docs = K.docs()
    check("docs.listed", len(docs) >= 1, len(docs))
    target = docs[0]["id"]
    check("forget.removes", K.forget(target) is True)
    check("forget.gone", all(d["id"] != target for d in K.docs()))
    check("forget.missing_is_false", K.forget(99999) is False)
    check("chunks_went_with_it",
          all(h.get("doc_id") != target for h in K.search("acme budget")))


def embeddings() -> None:
    """Only meaningful with a real key; skipped honestly otherwise."""
    print("== unit: semantic recall (needs the API key) ==", flush=True)
    sys.path.insert(0, str(ROOT))
    from core import knowledge as K
    K.reset()
    if not K._api_key():
        check("semantic.skipped_no_key", True, "no key on this machine")
        return
    d = K.ingest("Client Acme", "Acme wants a Next.js dashboard. Budget 4000 EUR. "
                 "Contact ops@acme.example. Weekly sprints, Slack.", source="note")
    if not d.get("embedded"):
        check("semantic.embeddings_work", False, "no vectors stored")
        return
    check("semantic.embeddings_work", True, f"{d['chunks']} embedded")
    # the real test: no shared keywords with the source text
    hits = K.search("what does the customer have money for")
    check("semantic.paraphrase_finds_it",
          any(h["kind"] == "doc" for h in hits),
          [(h["kind"], h["score"]) for h in hits[:2]])
    hits2 = K.search("how do they prefer to work together")
    check("semantic.second_paraphrase",
          any("weekly" in (h.get("text") or "").lower() or h["kind"] == "doc"
              for h in hits2), [(h["kind"], h["score"]) for h in hits2[:2]])
    check("semantic.stats_mode", K.stats()["mode"] == "semantic", K.stats()["mode"])


# ── server ───────────────────────────────────────────────────────────────────

def _port_free(port: int) -> bool:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start() -> subprocess.Popen:
    if not _port_free(PORT):
        raise RuntimeError(f"port {PORT} in use")
    if LOG.exists():
        LOG.unlink()
    shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"JARVIS_MODE": "server", "JARVIS_PORT": str(PORT),
                "JARVIS_DATA": str(DATA), "PYTHONUNBUFFERED": "1"})
    p = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=str(ROOT), env=env,
                         stdout=LOG.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    end = time.time() + 60
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/login", timeout=2) as r:
                if r.status < 500:
                    return p
        except Exception:
            pass
        time.sleep(0.25)
    p.kill()
    raise RuntimeError("server did not start:\n"
                       + (LOG.read_text(errors="replace")[-1200:] if LOG.exists() else ""))


_tok = ""


def token() -> str:
    global _tok
    if _tok:
        return _tok
    import re
    r = urllib.request.Request(BASE + "/api/bootstrap-key", data=b"{}", method="POST")
    with urllib.request.urlopen(r, timeout=30) as x:
        key = json.loads(x.read())["key"]
    with urllib.request.urlopen(f"{BASE}/auto-login?key={key}", timeout=30) as x:
        html = x.read().decode(errors="replace")
    _tok = re.search(r"sessionStorage\.setItem\('jarvis_token','([^']+)'", html).group(1)
    return _tok


def api(path: str, body: dict | None = None, tok: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               method="POST" if data else "GET")
    if data:
        r.add_header("Content-Type", "application/json")
    if tok:
        r.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(r, timeout=90) as x:
            b = x.read()
            return x.status, (json.loads(b) if b else {})
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, (json.loads(b) if b else {})
        except Exception:
            return e.code, {}


def served() -> None:
    print("== server: /api/knowledge ==", flush=True)
    tok = token()
    check("auth.401_search", api("/api/knowledge?q=x")[0] == 401)
    check("auth.401_post", api("/api/knowledge", {"title": "x", "text": "y"})[0] == 401)
    check("auth.401_forget", api("/api/knowledge/forget", {"id": 1})[0] == 401)

    st, d = api("/api/knowledge?q=anything", tok=tok)
    check("get.search_shape",
          st == 200 and "hits" in d and "stats" in d, sorted(d)[:4])
    st, d = api("/api/knowledge?view=stats", tok=tok)
    check("get.stats", st == 200 and "docs" in d.get("stats", {}), d.get("stats"))
    st, d = api("/api/knowledge?view=docs", tok=tok)
    check("get.docs", st == 200 and isinstance(d.get("docs"), list), st)
    st, d = api("/api/knowledge?view=recent", tok=tok)
    check("get.recent", st == 200 and isinstance(d.get("turns"), list), st)

    st, d = api("/api/knowledge",
                {"title": "Client Globex", "text": "Globex pays 3000 EUR for a "
                 "landing page. Primary contact sales@globex.example.",
                 "source": "client"}, tok=tok)
    check("post.stores", st == 201 and d.get("chunks", 0) >= 1, d)
    doc_id = d.get("id")
    st, d = api("/api/knowledge?q=globex%20payment", tok=tok)
    found = [h for h in d.get("hits", []) if h.get("kind") == "doc"]
    check("post.findable", any("Globex" in (h.get("title") or "") for h in found),
          [h.get("title") for h in d.get("hits", [])[:3]])

    st, d = api("/api/knowledge", {"title": "x", "text": "y"}, tok=tok)
    check("post.rejects_too_short", st == 400, st)

    st, d = api("/api/knowledge/forget", {"id": doc_id}, tok=tok)
    check("post.forget", st == 200 and d.get("ok") is True, d)
    st, d = api("/api/knowledge?q=globex", tok=tok)
    check("post.forget_removed_it",
          not any("Globex" in (h.get("title") or "") for h in d.get("hits", [])),
          [h.get("title") for h in d.get("hits", [])[:3]])

    # forgetting is a delete, so it goes through the policy's audit trail
    st, d = api("/api/audit?action=knowledge.forget&limit=5", tok=tok)
    rows = d.get("rows", [])
    check("forget_is_audited", bool(rows) and rows[0]["tier"] == "delete",
          rows[0] if rows else d)

    app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    for probe, label in [('id="kn-btn"', "header button"),
                         ("function openKnowledge(", "panel opener"),
                         ("_knRender", "renderer"),
                         ("_knForget", "forget action"),
                         ("/api/knowledge?view=recent", "recent tab")]:
        check(f"client.{label.replace(' ', '_')}", probe in app, probe)

    mp = (ROOT / "main.py").read_text(encoding="utf-8")
    check("tool.declared", '"name": "knowledge"' in mp)
    check("tool.dispatched", 'elif name == "knowledge":' in mp)
    check("tool.turns_persisted", 'record_turn("user"' in mp)
    check("tool.recall_in_prompt", "_kn.prompt_block()" in mp)


def main() -> int:
    unit()
    embeddings()
    proc = start()
    try:
        served()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    log = LOG.read_text(errors="replace") if LOG.exists() else ""
    check("boot.no_traceback", "Traceback (most recent call last)" not in log,
          log[-400:] if "Traceback" in log else "")
    print()
    print(f"PASS {len(_passes)}  FAIL {len(_failures)}")
    for f in _failures:
        print(f"  x {f}")
    return 1 if _failures else 0


def _hard_exit(code: int) -> None:
    """Leave immediately. A suite that has printed its result can still hang in
    interpreter teardown (a Playwright browser, a thread that never joins), which
    is the difference between a 2-minute suite and a 15-minute one."""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    _hard_exit(main())
