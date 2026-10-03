"""GitHub: what the user's code is doing right now.

Read paths are free. Write paths — comment, open an issue, push, merge — are
registered in `core/policy.py` so the approval gate asks first, and this module
does not attempt to be clever about that. It has no opinion on whether you
approved; it only refuses to invent a token.

The interesting one is `failing_ci`, because that is what a background watch
calls: pull the recent workflow runs, and say which ones broke and on what.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from core import svc

API = "https://api.github.com"


def _token() -> str:
    return svc.get_token("github_token", "github_pat", "gh_token")


def configured() -> bool:
    return bool(_token())


def _h() -> dict:
    return {"Authorization": f"Bearer {_token()}",
            "X-GitHub-Api-Version": "2022-11-28"}


def _call(method: str, path: str, *, params: Optional[dict] = None,
          body: Any = None, fetcher: Optional[Callable] = None) -> Any:
    url = API + path + svc.qs(params or {})
    if fetcher:
        return fetcher(method, url, body)
    return svc.request(method, url, headers=_h(), body=body)


# ── read ─────────────────────────────────────────────────────────────────────

def whoami(fetcher: Optional[Callable] = None) -> dict:
    u = _call("GET", "/user", fetcher=fetcher) or {}
    return {"login": u.get("login", ""), "name": u.get("name", ""),
            "public_repos": u.get("public_repos", 0),
            "html_url": u.get("html_url", "")}


def repos(limit: int = 20, fetcher: Optional[Callable] = None) -> list[dict]:
    rows = _call("GET", "/user/repos",
                 params={"sort": "pushed", "per_page": limit},
                 fetcher=fetcher) or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        out.append({
            "full_name": r.get("full_name", ""),
            "private": bool(r.get("private")),
            "language": r.get("language") or "",
            "stars": r.get("stargazers_count", 0),
            "open_issues": r.get("open_issues_count", 0),
            "default_branch": r.get("default_branch", "main"),
            "pushed_at": r.get("pushed_at", ""),
            "url": r.get("html_url", ""),
        })
    return out


def _repo_path(repo: str, rest: str = "") -> str:
    r = str(repo or "").strip().strip("/")
    if "/" not in r:
        raise svc.ServiceError(f"'{r}' is not owner/repo — I need the full name, "
                              f"like facebook/react.")
    return f"/repos/{r}{rest}"


def prs(repo: str, *, state: str = "open", limit: int = 15,
        fetcher: Optional[Callable] = None) -> list[dict]:
    rows = _call("GET", _repo_path(repo, "/pulls"),
                 params={"state": state, "per_page": limit},
                 fetcher=fetcher) or []
    out = []
    for p in rows if isinstance(rows, list) else []:
        out.append({
            "number": p.get("number"), "title": p.get("title", ""),
            "user": (p.get("user") or {}).get("login", ""),
            "draft": bool(p.get("draft")),
            "created_at": p.get("created_at", ""), "updated_at": p.get("updated_at", ""),
            "url": p.get("html_url", ""),
            "head": ((p.get("head") or {}).get("ref") or ""),
            "base": ((p.get("base") or {}).get("ref") or ""),
        })
    return out


def issues(repo: str, *, state: str = "open", limit: int = 15,
           fetcher: Optional[Callable] = None) -> list[dict]:
    rows = _call("GET", _repo_path(repo, "/issues"),
                 params={"state": state, "per_page": limit},
                 fetcher=fetcher) or []
    # The issues endpoint returns pull requests too; a PR in an issue list is
    # noise the assistant will otherwise narrate as a bug report.
    return [{"number": i.get("number"), "title": i.get("title", ""),
             "labels": [l.get("name") for l in (i.get("labels") or [])],
             "user": (i.get("user") or {}).get("login", ""),
             "created_at": i.get("created_at", ""), "url": i.get("html_url", "")}
            for i in rows if isinstance(rows, list) and "pull_request" not in i]


def runs(repo: str, *, limit: int = 15, branch: str = "",
         fetcher: Optional[Callable] = None) -> list[dict]:
    params = {"per_page": limit}
    if branch:
        params["branch"] = branch
    data = _call("GET", _repo_path(repo, "/actions/runs"), params=params,
                 fetcher=fetcher) or {}
    out = []
    for r in ((data.get("workflow_runs") or []) if isinstance(data, dict) else []):
        out.append({
            "id": r.get("id"), "name": r.get("name", ""),
            "status": r.get("status", ""), "conclusion": r.get("conclusion") or "",
            "branch": (r.get("head_branch") or ""),
            "commit": ((r.get("head_commit") or {}) or {}).get("message", "")[:120],
            "created_at": r.get("created_at", ""), "url": r.get("html_url", ""),
        })
    return out


def failing_ci(repo: str, *, limit: int = 20,
               fetcher: Optional[Callable] = None) -> list[dict]:
    """Runs that actually broke. A watch calls this; a human rarely will."""
    return [r for r in runs(repo, limit=limit, fetcher=fetcher)
            if r.get("conclusion") in ("failure", "timed_out", "cancelled")
            or (r.get("status") == "completed"
                and r.get("conclusion") not in ("success", "skipped", None, ""))]


def run_log(repo: str, run_id: Any, fetcher: Optional[Callable] = None) -> str:
    """The tail of a failing job's log — where the actual error is."""
    if fetcher:
        return str(fetcher("GET", f"{API}{_repo_path(repo, f'/actions/runs/{run_id}/logs')}",
                           None) or "")
    import urllib.request
    req = urllib.request.Request(
        f"{API}{_repo_path(repo, f'/actions/runs/{run_id}/logs')}",
        headers=dict(_h(), Accept="application/vnd.github+json"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")[-4000:]
    except Exception as e:
        return f"(log unavailable: {type(e).__name__})"


# ── write (approval-gated upstream) ──────────────────────────────────────────

def comment(repo: str, number: Any, text: str,
            fetcher: Optional[Callable] = None) -> dict:
    r = _call("POST", _repo_path(repo, f"/issues/{number}/comments"),
              body={"body": str(text)}, fetcher=fetcher) or {}
    return {"ok": True, "url": r.get("html_url", "")}


def open_issue(repo: str, title: str, text: str = "",
               labels: str = "", fetcher: Optional[Callable] = None) -> dict:
    body: dict = {"title": str(title)}
    if text:
        body["body"] = str(text)
    lab = [l.strip() for l in str(labels or "").split(",") if l.strip()]
    if lab:
        body["labels"] = lab
    r = _call("POST", _repo_path(repo, "/issues"), body=body, fetcher=fetcher) or {}
    return {"ok": True, "number": r.get("number"), "url": r.get("html_url", "")}


# ── the model-facing surface ─────────────────────────────────────────────────

def tool(action: str = "", *, repo: str = "", number: Any = "", text: str = "",
         title: str = "", limit: int = 15, branch: str = "",
         fetcher: Optional[Callable] = None) -> str:
    a = str(action or "").strip().lower()
    if not configured() and fetcher is None:
        return ("GitHub is not connected — add a token in Settings (scope: repo "
                "for private repositories, public_repo if only public).")
    try:
        if a in ("", "repos", "list"):
            rows = repos(limit=limit, fetcher=fetcher)
            if not rows:
                return "No repositories found."
            return "\n".join(
                f"- {r['full_name']} · {r['language'] or '—'} · "
                f"{r['open_issues']} open · pushed {r['pushed_at'][:10]}"
                for r in rows)
        if a in ("me", "whoami", "who"):
            u = whoami(fetcher=fetcher)
            return f"Signed in as {u['login']} ({u['name'] or 'no name'})."
        if a in ("prs", "pr", "pulls", "reviews"):
            rows = prs(repo, limit=limit, fetcher=fetcher)
            if not rows:
                return f"No open pull requests on {repo or 'that repo'}."
            return "\n".join(f"- #{r['number']} {r['title']} "
                             f"({r['user']}{', draft' if r['draft'] else ''})"
                             for r in rows)
        if a in ("issues", "issue"):
            rows = issues(repo, limit=limit, fetcher=fetcher)
            if not rows:
                return f"No open issues on {repo or 'that repo'}."
            return "\n".join(f"- #{r['number']} {r['title']}"
                             + (f" [{', '.join(r['labels'])}]" if r["labels"] else "")
                             for r in rows)
        if a in ("ci", "runs", "workflows", "actions"):
            rows = runs(repo, limit=limit, branch=branch, fetcher=fetcher)
            if not rows:
                return f"No workflow runs recorded for {repo or 'that repo'}."
            return "\n".join(f"- {r['name']}: {r['status']}"
                             + (f" · {r['conclusion']}" if r["conclusion"] else "")
                             + f" · {r['commit'][:60]}" for r in rows)
        if a in ("failing", "broken", "red"):
            rows = failing_ci(repo, fetcher=fetcher)
            if not rows:
                return f"CI is green on {repo or 'the repo'}. Nothing failing."
            return ("CI is red on " + repo + ":\n" + "\n".join(
                f"- {r['name']} {r['conclusion']} · {r['branch']} · "
                f"{r['commit'][:60]}" for r in rows[:10]))
        if a == "log":
            if not repo or not number:
                return "Give me the repo and the run id."
            return run_log(repo, number, fetcher=fetcher)
        if a in ("comment", "say"):
            if not repo or not number:
                return "Give me the repo and the issue/PR number to comment on."
            r = comment(repo, number, text, fetcher=fetcher)
            return f"Commented on #{number} in {repo}. {r['url']}"
        if a in ("open", "create", "new"):
            if not repo:
                return "Give me the repo."
            r = open_issue(repo, title or text, text, fetcher=fetcher)
            return f"Opened #{r['number']} in {repo}. {r['url']}"
        return "Unknown action. Use repos / prs / issues / ci / failing / log / comment / open."
    except svc.ServiceError as e:
        return f"GitHub: {e.message}"
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:180]
