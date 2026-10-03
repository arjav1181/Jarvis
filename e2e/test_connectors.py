"""Tests for the connectors added to close the 'it can't touch anything' gap:
email, github, vercel, hugging face, and the watches built on top of them.

Every remote call is faked through the `fetcher` seam, so this suite never
opens a socket. That is the same convention core/mail.py uses for SMTP/IMAP,
and it is the reason these tests can run in CI at all.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# an isolated data root so the suite never reads or writes the real config
_TMP = tempfile.mkdtemp(prefix="jarvis-test-svc-")
os.environ["JARVIS_DATA"] = _TMP

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(f"{name} — {detail}")
        print(f"  FAIL  {name} — {detail}")


# ── svc: the shared plumbing ─────────────────────────────────────────────────

def test_svc():
    from core import svc
    svc.set_token("github_token", "ghp_secret")
    check("svc.token_roundtrip", svc.get_token("github_token") == "ghp_secret")
    check("svc.token_fallback", svc.get_token("nope", "github_token") == "ghp_secret")
    check("svc.token_absent", svc.get_token("never_set_key") == "")

    # a token must never appear in a URL that could be logged
    check("svc.qs_drops_empty", "a=" not in svc.qs({"a": "", "b": 1}),
          svc.qs({"a": "", "b": 1}))

    # errors must be written for a human, and name the fix
    err = svc.ServiceError("The token is missing, expired, or lacks that scope.",
                           status=401)
    check("svc.error_is_readable", "token" in err.message.lower())
    check("svc.error_has_status", err.status == 401)

    import urllib.error
    import io

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {},
            io.BytesIO(b'{"message":"Bad credentials"}'))
    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        svc.request("GET", "https://api.github.com/user",
                    headers={"Authorization": "Bearer x"})
        check("svc.401_raises", False, "no exception")
    except svc.ServiceError as e:
        check("svc.401_names_the_credential", "token" in e.message.lower(), e.message)
        check("svc.401_keeps_detail", "Bad credentials" in e.detail, e.detail)
    except Exception as e:
        check("svc.401_raises", False, f"wrong type {type(e).__name__}")
    finally:
        urllib.request.urlopen = orig


# ── github ───────────────────────────────────────────────────────────────────

GH_REPOS = [{"full_name": "me/jarvis", "language": "Python", "private": False,
             "open_issues_count": 2, "stargazers_count": 11,
             "default_branch": "main", "pushed_at": "2026-09-28T10:00:00Z",
             "html_url": "https://github.com/me/jarvis"}]
GH_PULLS = [{"number": 7, "title": "Wire the email tool", "user": {"login": "me"},
             "draft": False, "created_at": "2026-09-27", "updated_at": "2026-09-28",
             "html_url": "u", "head": {"ref": "feat/email"},
             "base": {"ref": "main"}}]
# the issues endpoint also returns PRs — a real API wart worth testing against
GH_ISSUES = [{"number": 3, "title": "CI red on main", "labels": [{"name": "bug"}],
              "user": {"login": "me"}, "created_at": "2026-09-28",
              "html_url": "i3"},
             {"number": 7, "title": "Wire the email tool", "labels": [],
              "user": {"login": "me"}, "created_at": "2026-09-27",
              "html_url": "p7", "pull_request": {"url": "x"}}]
GH_RUNS = {"workflow_runs": [
    {"id": 101, "name": "ci", "status": "completed", "conclusion": "failure",
     "head_branch": "main", "head_commit": {"message": "fix: budget keys"},
     "created_at": "2026-09-28", "html_url": "r101"},
    {"id": 100, "name": "ci", "status": "completed", "conclusion": "success",
     "head_branch": "main", "head_commit": {"message": "chore"},
     "created_at": "2026-09-27", "html_url": "r100"},
    {"id": 99, "name": "ci", "status": "in_progress", "conclusion": None,
     "head_branch": "feat", "head_commit": {"message": "wip"},
     "created_at": "2026-09-26", "html_url": "r99"}]}


def gh_fetch(method, url, body):
    path = url.replace("https://api.github.com", "").split("?")[0]
    if path == "/user":
        return {"login": "runner", "name": "Sam", "public_repos": 4}
    if path == "/user/repos":
        return GH_REPOS
    if path.endswith("/pulls"):
        return GH_PULLS
    if path.endswith("/issues"):
        return GH_ISSUES
    if path.endswith("/actions/runs"):
        return GH_RUNS
    if path.endswith("/comments"):
        return {"html_url": "https://github.com/me/jarvis/issues/3#c1"}
    return {}


def test_github():
    from core import github as gh, svc
    svc.set_token("github_token", "ghp_fake")

    check("gh.whoami", gh.tool("me", fetcher=gh_fetch) == "Signed in as runner (Sam).",
          gh.tool("me", fetcher=gh_fetch))
    check("gh.repos", "me/jarvis" in gh.tool("repos", fetcher=gh_fetch))

    prs = gh.prs("me/jarvis", fetcher=gh_fetch)
    check("gh.prs", len(prs) == 1 and prs[0]["number"] == 7)

    # a pull request must not be reported to the user as an issue
    iss = gh.issues("me/jarvis", fetcher=gh_fetch)
    check("gh.issues_exclude_prs", len(iss) == 1 and iss[0]["number"] == 3,
          f"got {[i['number'] for i in iss]}")

    # in_progress is not failing; only completed-and-bad is
    bad = gh.failing_ci("me/jarvis", fetcher=gh_fetch)
    check("gh.failing_only_real", len(bad) == 1 and bad[0]["id"] == 101,
          f"got {[b['id'] for b in bad]}")

    # a bare repo name is a user error, and must be explained not crashed on
    out = gh.tool("prs", repo="jarvis", fetcher=gh_fetch)
    check("gh.bad_repo_explained", "owner/repo" in out, out)

    c = gh.comment("me/jarvis", 3, "looking", fetcher=gh_fetch)
    check("gh.comment", c["ok"] and "#c1" in c["url"])

    # a failing service must produce advice, not a traceback
    def boom(method, url, body):
        raise svc.ServiceError("The token is missing, expired, or lacks that scope.",
                               status=401)
    out = gh.tool("repos", fetcher=boom)
    check("gh.service_error_is_prose", out.startswith("GitHub:") and "token" in out, out)


# ── vercel ───────────────────────────────────────────────────────────────────

VERCEL_DEPLOYS = {"deployments": [
    {"uid": "d1", "name": "jarvis-web", "state": "ERROR", "created": 1756460000,
     "url": "jarvis-web.vercel.app",
     "meta": {"githubCommitRef": "main", "githubCommitMessage": "fix: keys"}},
    {"uid": "d0", "name": "jarvis-web", "state": "READY", "created": 1756450000,
     "url": "jarvis-web.vercel.app", "meta": {"githubCommitRef": "main"}},
    {"uid": "d2", "name": "api", "state": "CANCELED", "created": 1756440000,
     "url": "api.vercel.app", "meta": {}}]}


def vc_fetch(method, url, body):
    path = url.replace("https://api.vercel.com", "").split("?")[0]
    if path == "/v2/user":
        return {"user": {"username": "sam", "email": "s@x.com"}}
    if path == "/v9/projects":
        return {"projects": [{"name": "jarvis-web", "framework": "Next.js",
                              "state": "READY", "updatedAt": 1756460000}]}
    if path == "/v6/deployments":
        return VERCEL_DEPLOYS
    return {}


def test_vercel():
    from core import vercel as vc, svc
    svc.set_token("vercel_token", "vrc_fake")
    check("vc.whoami", "sam" in vc.tool("me", fetcher=vc_fetch))
    check("vc.projects", "jarvis-web" in vc.tool("projects", fetcher=vc_fetch))
    bad = vc.broken_deploys(fetcher=vc_fetch)
    check("vc.broken", len(bad) == 2 and {b["uid"] for b in bad} == {"d1", "d2"},
          f"got {[b['uid'] for b in bad]}")
    out = vc.tool("broken", fetcher=vc_fetch)
    check("vc.broken_prose", "Broken deployments" in out, out)
    # a READY deploy must never be listed as broken
    check("vc.ready_excluded", "d0" not in out, out)


# ── hugging face ─────────────────────────────────────────────────────────────

def hf_fetch(method, url, body):
    path = url.replace("https://huggingface.co/api", "").split("?")[0]
    if path == "/models":
        return [{"id": "openai/whisper-large-v3", "downloads": 900000,
                 "likes": 300, "pipeline_tag": "automatic-speech-recognition",
                 "lastModified": "2026-01-01T00:00:00Z"}]
    if path == "/spaces":
        return [{"id": "me/agent", "lastModified": "2026-09-28T00:00:00Z",
                 "runtime": {"stage": "RUNNING",
                             "hardware": {"current": "t4-small"}}}]
    if path == "/whoami-v2":
        return {"name": "sam", "type": "user"}
    return {}


def test_hf():
    from core import hf, svc
    svc.set_token("hf_token", "hf_fake")
    out = hf.tool("models", query="whisper", fetcher=hf_fetch)
    check("hf.models", "whisper-large-v3" in out, out)
    check("hf.formats_downloads", "900,000" in out, out)
    check("hf.spaces", "RUNNING" in hf.tool("spaces", fetcher=hf_fetch),
          hf.tool("spaces", fetcher=hf_fetch))
    check("hf.whoami", "sam" in hf.tool("me", fetcher=hf_fetch))


# ── email ────────────────────────────────────────────────────────────────────

class FakeMbox:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, a, p):
        pass

    def select(self, b):
        return ("OK", [b"2"])

    def search(self, *a):
        return ("OK", [b"1"])

    def fetch(self, mid, spec):
        raw = ("From: bob@acme.com\nTo: me@gmail.com\nSubject: Invoice?\n"
               "Date: Mon, 1 Sep 2026 10:00:00 +0000\n"
               "Message-ID: <m1@acme>\n\nCan you send the invoice today?")
        return ("OK", [(b"1 (RFC822 {12}", raw.encode())])


class FakeSmtp:
    sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, a, p):
        pass

    def send_message(self, m):
        FakeSmtp.sent.append(m["To"])


def test_email():
    from core import mail as M
    M.set_config("me@gmail.com", "app-password")

    r = M.fetch_inbox(limit=5, imap_factory=lambda a, p: FakeMbox())
    check("mail.inbox_ok", r["ok"] and len(r["messages"]) == 1, str(r)[:120])
    m = r["messages"][0]
    # a bytes snippet would be read aloud as "b'Can you send...'"
    check("mail.snippet_is_text", isinstance(m["snippet"], str), type(m["snippet"]).__name__)
    check("mail.snippet_body", "invoice" in m["snippet"], m["snippet"])
    check("mail.no_headers_leak", "Message-ID" not in m["snippet"], m["snippet"])

    check("mail.decode_plain", "invoice" in M._decode_body(
        "Content-Type: text/plain\n\nsend the invoice"))
    check("mail.decode_html", "send it" in M._decode_body(
        "Content-Type: text/html\n\n<div>please <b>send it</b></div>"))
    check("mail.decode_empty", M._decode_body("") == "")

    check("mail.bad_address", "not an address" in M.tool(
        "send", to="nope", subject="s", body="b", smtp_factory=lambda a, p: FakeSmtp()))

    out = M.tool("draft", to="bob@acme.com", subject="Invoice 12", body="Attached.")
    check("mail.draft_not_sent", "NOT sent" in out, out)

    FakeSmtp.sent = []
    out = M.tool("send", to="bob@acme.com", subject="Invoice 12", body="Attached.",
                 smtp_factory=lambda a, p: FakeSmtp())
    check("mail.sent", out.startswith("Sent to bob@acme.com"), out)
    check("mail.delivered", FakeSmtp.sent == ["bob@acme.com"], str(FakeSmtp.sent))
    check("mail.ledger_records", len(M._ledger().get("sent") or []) == 1)

    # the shared budget must actually stop a runaway send loop
    blocked = [M.tool("send", to=f"x{i}@acme.com", subject="s", body="b",
                      smtp_factory=lambda a, p: FakeSmtp())
               for i in range(3)]
    check("mail.budget_blocks", all("did not send" in b for b in blocked),
          str(blocked))
    b = M.budget()
    check("mail.budget_keys", "ceiling" in b and "remaining" in b, str(b))


# ── watches ──────────────────────────────────────────────────────────────────

def test_watches():
    from core import watches as W, svc
    svc.set_token("github_token", "ghp_fake")
    svc.set_token("vercel_token", "vrc_fake")

    state = {"red": True}

    def gh2(method, url, body):
        url = url.split("?")[0]
        if url.endswith("/user/repos"):
            return GH_REPOS
        if url.endswith("/actions/runs"):
            return {"workflow_runs": [dict(
                GH_RUNS["workflow_runs"][0],
                conclusion="failure" if state["red"] else "success")]}
        return {}

    def vc2(method, url, body):
        url = url.split("?")[0]
        if url.endswith("/deployments"):
            return {"deployments": [dict(
                VERCEL_DEPLOYS["deployments"][0],
                state="ERROR" if state["red"] else "READY")]}
        return {}

    a = W.ci_watch(fetcher=gh2)
    check("watch.ci_announces_new", "went red" in a, a)
    check("watch.ci_quiet_when_same", W.ci_watch(fetcher=gh2) == "",
          W.ci_watch(fetcher=gh2))
    state["red"] = False
    W.ci_watch(fetcher=gh2)
    state["red"] = True
    check("watch.ci_announces_again", "went red" in W.ci_watch(fetcher=gh2))

    a = W.deploy_watch(fetcher=vc2)
    check("watch.deploy_announces", "failed" in a, a)
    check("watch.deploy_quiet", W.deploy_watch(fetcher=vc2) == "")

    # a watch must never throw into the scheduler
    def boom(method, url, body):
        raise RuntimeError("network down")
    check("watch.survives_error", W.ci_watch(fetcher=boom) == "")
    check("watch.digest_survives", isinstance(W.infra_digest(fetcher=boom), str))


# ── policy: reading is free, acting asks ─────────────────────────────────────

def test_policy():
    from core import policy as P
    free = [("github", "repos"), ("github", "ci"), ("github", "failing"),
            ("vercel", "projects"), ("vercel", "broken"),
            ("hf", "models"), ("hf", "spaces"),
            ("email", "inbox"), ("email", "status"), ("email", "draft")]
    for tool, action in free:
        d = P.check(tool, {"action": action})
        check(f"policy.{tool}.{action}.free", not d.needs_approval,
              f"{d.tier} approval={d.needs_approval}")

    asks = [("github", "comment"), ("github", "open"),
            ("vercel", "redeploy"), ("hf", "restart"),
            ("email", "send"), ("email", "configure")]
    for tool, action in asks:
        d = P.check(tool, {"action": action})
        check(f"policy.{tool}.{action}.asks", d.needs_approval,
              f"{d.tier} approval={d.needs_approval}")


# ── the model can actually see these tools ───────────────────────────────────

def test_declarations():
    with open(ROOT / "main.py", encoding="utf-8", newline="") as fh:
        src = fh.read()
    src = src.replace("\r\n", "\n")
    i = src.index("TOOL_DECLARATIONS = [")
    j = src.index("\n]\n", i) + 3
    ns = {}
    exec(compile(src[i:j], "<t>", "exec"), ns)
    decls = ns["TOOL_DECLARATIONS"]
    names = [d["name"] for d in decls]
    for want in ("email", "github", "vercel", "hf", "goals"):
        check(f"decl.{want}", want in names)
    check("decl.no_dupes", len(names) == len(set(names)),
          str([n for n in set(names) if names.count(n) > 1]))
    for want in ("email", "github", "vercel", "hf"):
        check(f"decl.{want}.dispatched",
              f'elif name == "{want}"' in src or f'"{want}",' in src)
    check("decl.multi_dispatch", 'elif name in ("github", "vercel", "hf")' in src)

    from google.genai import types
    try:
        for d in decls:
            types.FunctionDeclaration(
                name=d["name"], description=d.get("description", ""),
                parameters=d.get("parameters", {}))
        check("decl.sdk_valid", True)
    except Exception as e:
        check("decl.sdk_valid", False, str(e)[:150])


if __name__ == "__main__":
    for fn in (test_svc, test_github, test_vercel, test_hf, test_email,
               test_watches, test_policy, test_declarations):
        try:
            fn()
        except Exception as e:
            import traceback
            check(fn.__name__, False, f"raised {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\nPASS {COUNT - len(FAILS)}  FAIL {len(FAILS)}")
    for f in FAILS:
        print(f"  FAIL  {f}")
    sys.exit(1 if FAILS else 0)
