"""Vercel: what the user's infrastructure is doing right now.

Deployments are the thing worth watching. A production deploy that went red at
03:00 and a user only noticing on Monday is the exact failure this exists to
catch, so `deploy_watch` is designed to be called by the scheduler rather than
by the user.

Read paths are free; `redeploy` and `rollback` are approval-gated in
`core/policy.py`, because they change what the public is being served.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Callable, Optional

from core import svc

API = "https://api.vercel.com"
TEAM = "team_"


def _token() -> str:
    return svc.get_token("vercel_token", "vercel_api_token")


def configured() -> bool:
    return bool(_token())


def _h() -> dict:
    return {"Authorization": f"Bearer {_token()}"}


def _call(method: str, path: str, *, params: Optional[dict] = None,
          body: Any = None, fetcher: Optional[Callable] = None) -> Any:
    url = API + path + svc.qs(params or {})
    if fetcher:
        return fetcher(method, url, body)
    return svc.request(method, url, headers=_h(), body=body)


def _team_prefix() -> str:
    """Scope to the configured team, if the user set one."""
    t = svc.get_token("vercel_team_id")
    return f"/v{TEAM}{t}" if t else ""


def whoami(fetcher: Optional[Callable] = None) -> dict:
    u = _call("GET", "/v2/user", fetcher=fetcher) or {}
    u = u.get("user") if isinstance(u, dict) else {}
    u = u or {}
    return {"username": (u.get("username") or ""), "email": (u.get("email") or "")}


def projects(limit: int = 20, fetcher: Optional[Callable] = None) -> list[dict]:
    rows = _call("GET", _team_prefix() + "/v9/projects", params={"limit": limit},
                 fetcher=fetcher) or {}
    rows = rows.get("projects") if isinstance(rows, dict) else rows
    out = []
    for p in (rows or []):
        latest = (p.get("targets") or {}).get("production") or {}
        out.append({
            "name": p.get("name", ""),
            "framework": p.get("framework") or "",
            "state": latest.get("readyState") or p.get("state") or "",
            "url": f"https://{p.get('name','')}.vercel.app" if p.get("name") else "",
            "updated_at": latest.get("createdAt") or p.get("updatedAt", 0),
        })
    return out


def _since(ts: Any) -> str:
    try:
        n = int(ts)
    except Exception:
        n = 0
    if n <= 0:
        return ""
    if n > 10_000_000_000:      # milliseconds
        n //= 1000
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(n))


def deployments(project: str = "", *, limit: int = 10,
                fetcher: Optional[Callable] = None) -> list[dict]:
    params: dict = {"limit": limit}
    if project:
        params["projectId"] = project
    rows = _call("GET", _team_prefix() + "/v6/deployments", params=params,
                 fetcher=fetcher) or {}
    rows = rows.get("deployments") if isinstance(rows, dict) else rows
    out = []
    for d in (rows or []):
        out.append({
            "uid": d.get("uid", ""),
            "name": d.get("name", ""),
            "state": d.get("state") or (d.get("readyState") or ""),
            "target": d.get("target") or "",
            "created_at": _since(d.get("created") or d.get("createdAt")),
            "url": d.get("url", ""),
            "git_branch": ((d.get("meta") or {}).get("githubCommitRef") or ""),
            "commit_msg": (((d.get("meta") or {}).get("githubCommitMessage")
                            or "")[:100]),
        })
    return out


def broken_deploys(*, limit: int = 20, fetcher: Optional[Callable] = None) -> list[dict]:
    """Deployments that did not end READY. The watch calls this."""
    bad = {"ERROR", "CANCELED", "CANCELLED"}
    return [d for d in deployments(limit=limit, fetcher=fetcher)
            if (d.get("state") or "").upper() in bad]


def redeploy(deployment_uid: str, fetcher: Optional[Callable] = None) -> dict:
    r = _call("POST", f"/v13/deployments/{urllib.parse.quote(str(deployment_uid))}",
              body={"name": ""}, fetcher=fetcher) or {}
    return {"ok": True, "uid": r.get("uid", ""), "url": r.get("url", "")}


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, project: str = "", uid: str = "", limit: int = 10,
         fetcher: Optional[Callable] = None) -> str:
    a = str(action or "").strip().lower()
    if not configured() and fetcher is None:
        return ("Vercel is not connected — add an API token in Settings "
                "(account settings → tokens).")
    try:
        if a in ("", "projects", "list"):
            rows = projects(limit=limit, fetcher=fetcher)
            if not rows:
                return "No Vercel projects found."
            return "\n".join(f"- {r['name']} · {r['framework'] or '—'} · "
                             f"{r['state'] or 'no deploy'}" for r in rows)
        if a in ("me", "whoami", "who"):
            u = whoami(fetcher=fetcher)
            return f"Vercel as {u['username'] or u['email'] or '(unknown)'}."
        if a in ("deploys", "deployments", "history"):
            rows = deployments(project, limit=limit, fetcher=fetcher)
            if not rows:
                return "No deployments recorded."
            return "\n".join(f"- {r['created_at']} {r['name']} · {r['state']}"
                             + (f" · {r['commit_msg'][:50]}" if r["commit_msg"] else "")
                             for r in rows)
        if a in ("broken", "failing", "red", "status"):
            rows = broken_deploys(limit=limit, fetcher=fetcher)
            if not rows:
                return "Every recent deployment finished READY. Nothing is broken."
            return ("Broken deployments:\n" + "\n".join(
                f"- {r['created_at']} {r['name']} · {r['state']}"
                + (f" · {r['commit_msg'][:50]}" if r["commit_msg"] else "")
                for r in rows[:10]))
        if a in ("redeploy", "retry"):
            if not uid:
                return "Give me the deployment id to redeploy."
            r = redeploy(uid, fetcher=fetcher)
            return f"Redeploying {uid} → {r.get('url') or 'in progress'}"
        return "Unknown action. Use projects / deploys / broken / redeploy."
    except svc.ServiceError as e:
        return f"Vercel: {e.message}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:180]
