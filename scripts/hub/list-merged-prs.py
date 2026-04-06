#!/usr/bin/env python3
"""
Deterministic lister for recently-merged PRs.

Lists PRs merged into the repository's default branch within a lookback
window and emits a single JSON array to stdout. One row per PR with:

- number, title, body, url, author, merged_at, merge_commit_sha
- baseRefName, headRefName, labels
- files: list of changed file paths (with additions/deletions)
- diff: unified diff (truncated at DIFF_CHAR_CAP per PR)
- diff_truncated: bool

This is a pure orchestration of ``gh``. No LLM calls, no network beyond
what ``gh`` already does, no writes. It exists so that the
``hub-daily-sweep`` workflow's Claude agent can get a deterministic PR
bundle in one tool call instead of chaining 10+ ``gh`` invocations.

Usage::

    python3 scripts/hub/list-merged-prs.py
    python3 scripts/hub/list-merged-prs.py --since 48h
    python3 scripts/hub/list-merged-prs.py --repo owner/name --since 24h
    python3 scripts/hub/list-merged-prs.py --since 24h -o /tmp/merged.json

Exit codes:
    0  success (even if zero PRs matched)
    2  usage error
    3  ``gh`` CLI not installed or not authenticated
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Per-PR diff cap. Bigger diffs get truncated and flagged so the caller
# can fetch the specific files it needs via other tools.
DIFF_CHAR_CAP = 80_000

# Max PRs we'll bundle in one call. The sweep workflow should never be
# asked to reason about hundreds of merges at once.
PR_LIMIT = 50


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "gh CLI not found on PATH"
    return proc.returncode, proc.stdout, proc.stderr


def _gh_json(args: list[str]) -> tuple[Any, str | None]:
    rc, out, err = _run_gh(args)
    if rc != 0:
        return None, (err or out or f"gh exited with {rc}").strip()
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON from gh: {exc}"


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([hdw])\s*$", re.IGNORECASE)


def parse_since(value: str) -> timedelta:
    """Parse a lookback like ``24h``, ``3d``, ``1w`` into a timedelta."""
    m = _DURATION_RE.match(value)
    if not m:
        raise ValueError(
            f"invalid --since {value!r}; expected e.g. 24h, 3d, 1w"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    return timedelta(weeks=n)


def resolve_default_repo() -> str | None:
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    data, err = _gh_json(["repo", "view", "--json", "nameWithOwner"])
    if err or not data:
        return None
    return data.get("nameWithOwner")


def resolve_default_branch(repo: str) -> tuple[str | None, str | None]:
    data, err = _gh_json(
        ["repo", "view", repo, "--json", "defaultBranchRef"]
    )
    if err or not data:
        return None, err or "no repo data"
    ref = data.get("defaultBranchRef") or {}
    return ref.get("name"), None


def list_merged_prs(
    repo: str, base: str, since: datetime
) -> tuple[list[dict], str | None]:
    """Fetch merged PRs into ``base`` and filter by mergedAt >= since."""
    fields = ",".join(
        [
            "number",
            "title",
            "body",
            "url",
            "author",
            "mergedAt",
            "mergeCommit",
            "baseRefName",
            "headRefName",
            "labels",
            "files",
            "additions",
            "deletions",
        ]
    )
    data, err = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--base",
            base,
            "--limit",
            str(PR_LIMIT),
            "--json",
            fields,
        ]
    )
    if err:
        return [], err
    out: list[dict] = []
    for pr in data or []:
        merged_at_str = pr.get("mergedAt")
        if not merged_at_str:
            continue
        try:
            merged_at = datetime.fromisoformat(
                merged_at_str.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if merged_at < since:
            continue
        out.append(pr)
    return out, None


def fetch_pr_diff(
    repo: str, number: int
) -> tuple[str, bool, str | None]:
    rc, stdout, err = _run_gh(
        ["pr", "diff", str(number), "--repo", repo]
    )
    if rc != 0:
        return "", False, (err or f"gh pr diff exited with {rc}").strip()
    if len(stdout) > DIFF_CHAR_CAP:
        return stdout[:DIFF_CHAR_CAP], True, None
    return stdout, False, None


def build_row(repo: str, pr: dict) -> dict:
    number = pr.get("number")
    files = [
        {
            "path": f.get("path"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
        }
        for f in (pr.get("files") or [])
    ]
    labels = [l.get("name") for l in (pr.get("labels") or [])]
    diff, truncated, diff_err = fetch_pr_diff(repo, int(number))
    row: dict[str, Any] = {
        "repo": repo,
        "number": number,
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "url": pr.get("url"),
        "author": (pr.get("author") or {}).get("login"),
        "merged_at": pr.get("mergedAt"),
        "merge_commit_sha": (pr.get("mergeCommit") or {}).get("oid"),
        "base_ref": pr.get("baseRefName"),
        "head_ref": pr.get("headRefName"),
        "labels": labels,
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "files": files,
        "diff": diff,
        "diff_truncated": truncated,
    }
    if diff_err:
        row["diff_error"] = diff_err
    return row


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List PRs merged into the default branch within a lookback "
            "window as a JSON array (used by the hub-daily-sweep "
            "workflow)."
        )
    )
    parser.add_argument(
        "--since",
        default="24h",
        help="lookback window: e.g. 24h, 3d, 1w (default: 24h)",
    )
    parser.add_argument(
        "--repo",
        help=(
            "owner/name slug (default: $GITHUB_REPOSITORY or gh default)"
        ),
    )
    parser.add_argument(
        "--base",
        help=(
            "base branch to filter on (default: the repo's default "
            "branch)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        help="write JSON array to this path instead of stdout",
    )
    args = parser.parse_args()

    if not shutil.which("gh"):
        print("error: gh CLI not found on PATH", file=sys.stderr)
        return 3

    try:
        window = parse_since(args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    repo = args.repo or resolve_default_repo()
    if not repo:
        print(
            "error: could not determine repo; pass --repo owner/name",
            file=sys.stderr,
        )
        return 2

    base = args.base
    if not base:
        base, err = resolve_default_branch(repo)
        if not base:
            print(
                f"error: could not resolve default branch: {err}",
                file=sys.stderr,
            )
            return 2

    since_dt = datetime.now(timezone.utc) - window
    prs, err = list_merged_prs(repo, base, since_dt)
    if err:
        print(f"error: gh pr list failed: {err}", file=sys.stderr)
        return 3

    rows = [build_row(repo, pr) for pr in prs]
    text = json.dumps(rows, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
            f.write("\n")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
