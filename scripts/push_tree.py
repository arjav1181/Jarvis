#!/usr/bin/env python3
"""Push a commit's file set to the Space in chunks (git push is not usable here:
scripts/deploy_hf.py has been creating commits on the Space outside our local
history, so the refs diverge and the transport fights us).

    python3 scripts/push_tree.py 58b36ef [--chunk 350]

Only the files named by that commit are uploaded, the deny-list from
deploy_hf.py is enforced, and every chunk is committed on top of whatever the
Space already has. The token comes from HF_TOKEN or the repo-local .hf_token
and is never printed.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_ID = "abc1181/Jaarvis"



def token() -> str:
    t = os.environ.get("HF_TOKEN", "").strip()
    if not t:
        f = ROOT / ".hf_token"
        if f.exists():
            t = f.read_text().strip()
    if not t:
        sys.exit("no HF token: set HF_TOKEN or create .hf_token")
    return t


def denied(rel: str) -> bool:
    """Never upload these: secrets, and the vendored node_modules."""
    globs = ("*api_keys.json", "config/certs/*", "config/whatsapp_web/*",
             "*credentials*", "*client_secret*", "*token*.json", ".env", ".env.*",
             "*.env", ".hf_token", "*.pem", "*.key", "jarvisd.json", "*agents.json",
             "dashboard_auth.json", "*/node_modules/*", "node_modules/*")
    if "node_modules" in rel:
        return True
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, f"*/{g}")
               for g in globs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("commit")
    ap.add_argument("--chunk", type=int, default=350)
    args = ap.parse_args()

    raw = subprocess.run(["git", "show", "--name-only", "--format=", args.commit],
                         cwd=str(ROOT), capture_output=True, text=True, check=True).stdout
    files = [f for f in (x.strip() for x in raw.splitlines())
             if f and not f.startswith("godseye/_server/node_modules")
             and not denied(f)]
    missing = [f for f in files if not (ROOT / f).exists()]
    files = [f for f in files if f not in missing]
    total = sum((ROOT / f).stat().st_size for f in files)
    print(f"{len(files)} files, {total/1e6:.1f} MB"
          + (f"  (skipped {len(missing)} absent)" if missing else ""))

    from huggingface_hub import HfApi, CommitOperationAdd
    api = HfApi(token=token())
    for i in range(0, len(files), args.chunk):
        part = files[i:i + args.chunk]
        ops = [CommitOperationAdd(path_in_repo=rel, path_or_fileobj=(ROOT / rel).read_bytes())
              for rel in part]
        api.create_commit(
            repo_id=REPO_ID, repo_type="space", operations=ops,
            commit_message=f"Deploy {args.commit} ({i//args.chunk + 1})"
                           f" [{i + len(part)}/{len(files)}]")
        print(f"  chunk {i//args.chunk + 1}: {len(part)} files", flush=True)
    print("PUSH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
