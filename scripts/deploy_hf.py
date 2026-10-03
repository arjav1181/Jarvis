#!/usr/bin/env python3
"""Hash-verified deploy to the HF Space (abc1181/Jaarvis).

Usage:
    python3 scripts/deploy_hf.py path/one.py path/two.html ...   # files to sync
    python3 scripts/deploy_hf.py --check path/one.py ...         # diff only, no upload

Rules:
  * Token comes from the HF_TOKEN env var or the repo-local `.hf_token` file
    (gitignored). Never hardcoded, never printed.
  * Only the listed files are uploaded — `config/` must never appear here.
  * sha256 local-vs-remote decides what uploads; every uploaded file is
    re-fetched after the commit and re-verified before we call it done.
"""

import argparse
import fnmatch
import hashlib
import os
import sys
import time
from pathlib import Path

REPO_ID = "abc1181/Jaarvis"
ROOT = Path(__file__).resolve().parent.parent

# Belt-and-suspenders: these never leave the machine, tracked or not.
# (config/__init__.py, config/*.ico etc. are plain source and deploy fine.)
DENY_GLOBS = ("*api_keys.json", "config/certs/*", "config/whatsapp_web/*",
              "*credentials*", "*client_secret*", "*token*.json",
              ".env", ".env.*", "*.env", ".hf_token", "*.pem", "*.key",
              "jarvisd.json", "*agents.json", "dashboard_auth.json")


def _token() -> str:
    tok = os.environ.get("HF_TOKEN", "").strip()
    if not tok:
        f = ROOT / ".hf_token"
        if f.exists():
            tok = f.read_text().strip()
    if not tok:
        sys.exit("no HF token: set HF_TOKEN or create .hf_token")
    return tok


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _remote_bytes(path: str, token: str) -> bytes | None:
    """Raw file from the Space's main branch, cache-busted. None if absent."""
    import requests
    url = f"https://huggingface.co/spaces/{REPO_ID}/resolve/main/{path}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                     params={"download": "true"}, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="repo-relative paths")
    ap.add_argument("--check", action="store_true", help="diff only, no upload")
    ap.add_argument("--message", default=None, help="commit message")
    args = ap.parse_args()

    token = _token()
    from huggingface_hub import CommitOperationAdd, HfApi
    api = HfApi(token=token)

    ops, verified = [], []
    for rel in args.files:
        local = ROOT / rel
        if not local.is_file():
            sys.exit(f"missing local file: {rel}")
        if any(fnmatch.fnmatch(rel, g) for g in DENY_GLOBS):
            sys.exit(f"refusing to deploy {rel} (matches secret denylist)")
        want = _sha256(local.read_bytes())
        try:
            have = _sha256(_remote_bytes(rel, token) or b"")
        except Exception as e:
            sys.exit(f"remote check failed for {rel}: {e}")
        if want == have:
            print(f"  = {rel} (up to date)")
        else:
            print(f"  + {rel} ({want[:12]})")
            ops.append(CommitOperationAdd(path_in_repo=rel, path_or_fileobj=str(local)))
            verified.append((rel, want))

    if not ops:
        print("nothing to deploy")
        return 0
    if args.check:
        print(f"check: {len(ops)} file(s) differ")
        return 0

    msg = args.message or ("deploy: " + ", ".join(r for r, _ in verified))
    print(f"committing {len(ops)} file(s)…")
    api.create_commit(repo_id=REPO_ID, repo_type="space", operations=ops,
                      commit_message=msg)

    # Post-commit verification: every uploaded file must match byte-for-byte.
    for rel, want in verified:
        for attempt in range(5):
            got = _remote_bytes(rel, token)
            if got is not None and _sha256(got) == want:
                print(f"  ok {rel}")
                break
            time.sleep(2)
        else:
            sys.exit(f"VERIFY FAILED: {rel} does not match local after commit")

    try:
        rt = api.get_space_runtime(REPO_ID)
        print(f"space: {rt.stage} sha={rt.raw.get('sha')}")
        if rt.stage != "RUNNING":
            print("note: Space is rebuilding — watch logs before traffic")
    except Exception as e:
        print(f"(runtime status unavailable: {e})")
    print("DEPLOY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
