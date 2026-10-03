"""GitHub ops — issues and pull requests by voice.

Uses the `gh` CLI (auth you already did there is reused; nothing new to log
into). Say "list my open issues", "open an issue to fix login", "are my
checks green". Never raises: every failure comes back as a spoken sentence.
"""

import shutil
import subprocess

_GH = shutil.which("gh")


def _gh(*args: str) -> str:
    if not _GH:
        raise RuntimeError("the gh CLI is not installed on this machine")
    p = subprocess.run([_GH, *args], capture_output=True, text=True,
                       timeout=60)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "gh failed").strip()[:200])
    return p.stdout.strip()


def run(parameters: dict, player=None, session_memory=None) -> str:
    action = ((parameters or {}).get("action") or "issues").strip().lower()
    repo = ((parameters or {}).get("repo") or "").strip()
    title = ((parameters or {}).get("title") or "").strip()[:120]
    extra = [*(["-R", repo] if repo else [])]
    try:
        if action == "issues":
            out = _gh("issue", "list", "--limit", "10", *extra)
            return "Open issues:\n" + (out or "none — inbox zero, sir.")
        if action == "prs":
            out = _gh("pr", "list", "--limit", "10", *extra)
            return "Open pull requests:\n" + (out or "none open.")
        if action == "checks":
            out = _gh("pr", "checks", *extra)
            return "Check status:\n" + (out or "no checks found.")
        if action == "create_issue":
            if not title:
                return "Tell me the issue title first, sir."
            body = ((parameters or {}).get("body") or "").strip()[:2000]
            out = _gh("issue", "create", "--title", title,
                      "--body", body or "_Filed by Jaarvis._", *extra)
            return f"Issue filed: {out}"
        return f"Unknown github action '{action}' — try issues, prs, checks, create_issue."
    except Exception as e:
        return f"GitHub failed, sir: {e}"


PLUGIN = {
    "server": True,  # same brain server-side
    "name": "github_ops",
    "description": ("GitHub by voice: list open issues, list pull requests, "
                    "check PR checks, file an issue. Trigger on 'github', "
                    "'my issues', 'my PRs', 'are checks green', 'file an issue'. "
                    "Do NOT use for general web search or code questions."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "issues, prs, checks, or create_issue"},
            "repo": {"type": "STRING",
                     "description": "owner/repo (omit for current repo context)"},
            "title": {"type": "STRING",
                      "description": "Issue title (create_issue only)"},
            "body": {"type": "STRING",
                     "description": "Issue body (create_issue only)"},
        },
        "required": [],
    },
}
