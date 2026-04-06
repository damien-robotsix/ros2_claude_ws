#!/usr/bin/env python3
"""Delete remote branches that have no associated open PR and are older than a
given age threshold.

Usage:
    python3 scripts/clean-stale-branches.py [--dry-run] [--max-age-hours 24] [--repo OWNER/REPO]

Protected branches (main, master, develop) and the default branch are never
deleted.  Branches that have an open pull request are also kept.

Requires the ``gh`` CLI to be authenticated.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone


PROTECTED_NAMES = {"main", "master", "develop"}


def run_gh(*args: str) -> str:
    """Run a ``gh`` CLI command and return its stdout."""
    result = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"gh {' '.join(args)} failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def resolve_repo(repo: str) -> str:
    """Return the OWNER/REPO slug, auto-detecting from the current repo if empty."""
    if repo:
        return repo
    raw = run_gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    return raw


def get_default_branch(repo_slug: str) -> str:
    raw = run_gh("api", f"repos/{repo_slug}", "--jq", ".default_branch")
    return raw or "main"


def list_remote_branches(repo_slug: str) -> list[dict]:
    """Return a list of dicts with keys *name* and *sha*."""
    branches: list[dict] = []
    page = 1
    per_page = 100
    while True:
        raw = run_gh(
            "api", f"repos/{repo_slug}/branches?per_page={per_page}&page={page}",
        )
        page_data = json.loads(raw)
        if not page_data:
            break
        for b in page_data:
            branches.append({
                "name": b["name"],
                "sha": b.get("commit", {}).get("sha", ""),
            })
        if len(page_data) < per_page:
            break
        page += 1
    return branches


def get_commit_date(sha: str, repo_slug: str) -> datetime | None:
    """Get the committer date for a given commit SHA."""
    raw = run_gh(
        "api", f"repos/{repo_slug}/commits/{sha}",
        "--jq", ".commit.committer.date",
    )
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def get_open_pr_branches(repo_slug: str) -> set[str]:
    """Return the set of head branch names that have an open PR."""
    raw = run_gh(
        "pr", "list", "--repo", repo_slug,
        "--state", "open", "--json", "headRefName", "--limit", "1000",
    )
    prs = json.loads(raw)
    return {pr["headRefName"] for pr in prs}


def delete_branch(name: str, repo_slug: str) -> None:
    """Delete a remote branch via the GitHub API."""
    subprocess.run(
        ["gh", "api", "--method", "DELETE",
         f"repos/{repo_slug}/git/refs/heads/{name}"],
        capture_output=True,
        text=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean stale remote branches")
    parser.add_argument("--dry-run", action="store_true", help="Only print what would be deleted")
    parser.add_argument("--max-age-hours", type=int, default=24,
                        help="Delete branches older than this many hours (default: 24)")
    parser.add_argument("--repo", type=str, default="",
                        help="Repository in OWNER/REPO format (default: current repo)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cutoff_hours = args.max_age_hours

    repo_slug = resolve_repo(args.repo)
    print(f"Repository: {repo_slug}")
    print(f"Fetching branches (max age: {cutoff_hours}h, dry-run: {args.dry_run}) ...")

    default_branch = get_default_branch(repo_slug)
    protected = PROTECTED_NAMES | {default_branch}

    branches = list_remote_branches(repo_slug)
    open_pr_branches = get_open_pr_branches(repo_slug)

    deleted = 0
    skipped = 0
    for branch in branches:
        name = branch["name"]

        if name in protected:
            print(f"  SKIP (protected)  {name}")
            skipped += 1
            continue

        if name in open_pr_branches:
            print(f"  SKIP (has open PR) {name}")
            skipped += 1
            continue

        commit_date = get_commit_date(branch["sha"], repo_slug)
        if commit_date is None:
            print(f"  SKIP (no date)    {name}")
            skipped += 1
            continue

        age_hours = (now - commit_date).total_seconds() / 3600
        if age_hours < cutoff_hours:
            print(f"  SKIP (age {age_hours:.1f}h)  {name}")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  WOULD DELETE      {name}  (age {age_hours:.1f}h)")
        else:
            delete_branch(name, repo_slug)
            print(f"  DELETED           {name}  (age {age_hours:.1f}h)")
        deleted += 1

    action = "would delete" if args.dry_run else "deleted"
    print(f"\nDone. {action} {deleted} branch(es), skipped {skipped}.")


if __name__ == "__main__":
    main()
