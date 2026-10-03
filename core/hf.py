"""Hugging Face: models, Spaces and datasets, plus running jobs.

Mostly a *read* surface — the Hub has a generous public API, so JARVIS can
answer questions about a model without a token at all. The token only widens
what it can see to private repos.

The action worth having is `restart` on a Space: a Gradio Space that has been
`RUNNING`-for-nothing (or crashed) is the single most common thing people want
fixed, and it is one call.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from core import svc

API = "https://huggingface.co/api"


def _token() -> str:
    return svc.get_token("hf_token", "huggingface_token", "huggingface_api_token")


def configured() -> bool:
    return bool(_token())


def _h() -> dict:
    """A private token is only attached when one exists. Public reads work
    anonymously, which is the common case."""
    t = _token()
    return {"Authorization": f"Bearer {t}"} if t else {}


def _call(method: str, path: str, *, params: Optional[dict] = None,
          body: Any = None, fetcher: Optional[Callable] = None) -> Any:
    url = API + path + svc.qs(params or {})
    if fetcher:
        return fetcher(method, url, body)
    return svc.request(method, url, headers=_h(), body=body)


def whoami(fetcher: Optional[Callable] = None) -> dict:
    u = _call("GET", "/whoami-v2", fetcher=fetcher) or {}
    return {"name": u.get("name", ""), "type": u.get("type", "")}


def models(*, search: str = "", limit: int = 10, sort: str = "downloads",
           fetcher: Optional[Callable] = None) -> list[dict]:
    params = {"limit": limit, "sort": sort, "direction": -1}
    if search:
        params["search"] = search
    rows = _call("GET", "/models", params=params, fetcher=fetcher) or []
    out = []
    for m in (rows if isinstance(rows, list) else []):
        out.append({
            "id": m.get("id", m.get("modelId", "")),
            "downloads": m.get("downloads", 0),
            "likes": m.get("likes", 0),
            "pipeline": m.get("pipeline_tag", ""),
            "updated": m.get("lastModified", "")[:10],
            "private": bool(m.get("private")),
        })
    return out


def model_info(model_id: str, fetcher: Optional[Callable] = None) -> dict:
    m = _call("GET", f"/models/{model_id}", fetcher=fetcher) or {}
    card = m.get("cardData") or {}
    tags = [t for t in (m.get("tags") or []) if "/" not in t][:12]
    return {
        "id": m.get("id", model_id),
        "downloads": m.get("downloads", 0), "likes": m.get("likes", 0),
        "pipeline": m.get("pipeline_tag", ""),
        "library": m.get("library_name", ""),
        "license": (card.get("license") or (m.get("cardData") or {}).get("license") or ""),
        "tags": tags, "gated": bool(m.get("gated")),
        "private": bool(m.get("private")),
        "updated": m.get("lastModified", "")[:10],
    }


def spaces(*, author: str = "", limit: int = 10,
           fetcher: Optional[Callable] = None) -> list[dict]:
    params: dict = {"limit": limit, "sort": "lastModified", "direction": -1}
    if author:
        params["author"] = author
    rows = _call("GET", "/spaces", params=params, fetcher=fetcher) or []
    out = []
    for s in (rows if isinstance(rows, list) else []):
        rt = s.get("runtime") or {}
        stage = (rt.get("stage") or "")
        out.append({
            "id": s.get("id", ""), "stage": stage,
            "hardware": (rt.get("hardware") or {}).get("current", ""),
            "private": bool(s.get("private")),
            "updated": (s.get("lastModified") or "")[:10],
            "url": f"https://huggingface.co/spaces/{s.get('id','')}",
        })
    return out


def datasets(*, search: str = "", limit: int = 10,
             fetcher: Optional[Callable] = None) -> list[dict]:
    params: dict = {"limit": limit, "sort": "downloads", "direction": -1}
    if search:
        params["search"] = search
    rows = _call("GET", "/datasets", params=params, fetcher=fetcher) or []
    return [{"id": d.get("id", ""), "downloads": d.get("downloads", 0),
             "likes": d.get("likes", 0), "private": bool(d.get("private"))}
            for d in (rows if isinstance(rows, list) else [])]


def restart_space(space_id: str, fetcher: Optional[Callable] = None) -> dict:
    _call("POST", f"/spaces/{space_id}/restart", body={}, fetcher=fetcher)
    return {"ok": True, "id": space_id}


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, query: str = "", id: str = "", limit: int = 10,
         fetcher: Optional[Callable] = None) -> str:
    a = str(action or "").strip().lower()
    try:
        if a in ("", "models", "search"):
            rows = models(search=query, limit=limit, fetcher=fetcher)
            if not rows:
                return f"No models matched '{query}'." if query else "No models returned."
            return "\n".join(f"- {r['id']} · {r['pipeline'] or '—'} · "
                             f"{r['downloads']:,} downloads · {r['likes']} likes"
                             for r in rows)
        if a in ("model", "info", "about"):
            if not (id or query):
                return "Give me the model id."
            m = model_info(id or query, fetcher=fetcher)
            return (f"{m['id']} · {m['pipeline'] or 'no pipeline tag'} · "
                    f"{m['downloads']:,} downloads · {m['likes']} likes"
                    + (f" · licence {m['license']}" if m["license"] else "")
                    + (" · GATED" if m["gated"] else "")
                    + (" · private" if m["private"] else ""))
        if a in ("spaces", "space"):
            rows = spaces(author=query, limit=limit, fetcher=fetcher)
            if not rows:
                return "No spaces returned."
            return "\n".join(f"- {r['id']} · {r['stage'] or 'unknown'}"
                             + (f" · {r['hardware']}" if r["hardware"] else "")
                             for r in rows)
        if a in ("restart", "reboot"):
            if not (id or query):
                return "Give me the space id to restart."
            sid = id or query
            restart_space(sid, fetcher=fetcher)
            return f"Restarted {sid}. It takes a minute to come back up."
        if a in ("datasets", "data"):
            rows = datasets(search=query, limit=limit, fetcher=fetcher)
            return ("\n".join(f"- {r['id']} · {r['downloads']:,} downloads"
                               for r in rows)
                    or f"No datasets matched '{query}'.")
        if a in ("me", "whoami"):
            if not configured() and fetcher is None:
                return "No Hugging Face token set, so I can only see public repos."
            u = whoami(fetcher=fetcher)
            return f"Hugging Face as {u['name'] or '(anonymous)'}."
        return "Unknown action. Use models / model / spaces / datasets / restart / me."
    except svc.ServiceError as e:
        return f"Hugging Face: {e.message}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:180]
