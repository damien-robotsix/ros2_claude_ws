#!/usr/bin/env python3
"""
Deterministic PR-review context collector.

Given a pull request number (and optionally a ``owner/repo`` slug), gather
in a single invocation all of the context an LLM-driven reviewer would
otherwise re-derive via 10+ ``gh api`` / ``gh pr view`` Bash calls:

- PR metadata (title, body, author, state, base/head refs, labels, draft)
- Unified diff
- Linked issues (parsed from the PR body via the standard GitHub closing
  keywords) with their title, state, and body
- Recent issue comments and review comments
- Check-run conclusions

The output is a single JSON object on stdout. It is **pure orchestration
of ``gh``** — no LLM calls, no credentials beyond whatever ``gh`` already
has, no network side-effects beyond GETs. All reasoning over the bundle
is the job of the caller.

This script exists because workflow-insights-extractor observed long
monotonic Bash chains (11–18 consecutive ``gh api`` calls) across review
sessions collecting the same information. See issue #35.

Usage::

    python3 scripts/collect-pr-review-context.py <pr-number>
    python3 scripts/collect-pr-review-context.py <pr-number> --repo owner/repo
    python3 scripts/collect-pr-review-context.py <pr-number> -o /tmp/pr-ctx.json

Exit codes:
    0  success (even if some optional sections were unavailable)
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
from typing import Any


# Cap on the number of comments we include. Reviews with hundreds of
# comments would blow up the context window otherwise; the caller can
# always request a specific comment by ID if it needs more.
COMMENT_CAP = 40

# Cap on the size of the diff we include. Very large refactors can
# produce megabytes of diff; past this cap we truncate and set a flag so
# the caller knows to fetch specific files directly.
DIFF_CHAR_CAP = 200_000

# GitHub's documented closing keywords. Matched case-insensitively, with
# an optional ``#`` and an issue number, optionally prefixed by a
# ``owner/repo`` slug for cross-repo references.
LINKED_ISSUE_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+))?#(\d+)"
)


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    """Run ``gh`` and return (returncode, stdout, stderr).

    Never raises on non-zero exit — the caller decides how to degrade.
    """
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
    """Run a ``gh`` command expected to emit JSON. Returns (data, error)."""
    rc, out, err = _run_gh(args)
    if rc != 0:
        return None, (err or out or f"gh exited with {rc}").strip()
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON from gh: {exc}"


def collect_pr_metadata(repo: str, number: int) -> tuple[dict, str | None]:
    fields = ",".join(
        [
            "number",
            "title",
            "body",
            "state",
            "isDraft",
            "author",
            "baseRefName",
            "headRefName",
            "headRefOid",
            "labels",
            "additions",
            "deletions",
            "changedFiles",
            "createdAt",
            "updatedAt",
            "mergeable",
            "url",
        ]
    )
    data, err = _gh_json(
        ["pr", "view", str(number), "--repo", repo, "--json", fields]
    )
    return data or {}, err


def collect_pr_diff(repo: str, number: int) -> tuple[str, bool, str | None]:
    """Return (diff_text, truncated, error)."""
    rc, out, err = _run_gh(["pr", "diff", str(number), "--repo", repo])
    if rc != 0:
        return "", False, (err or f"gh pr diff exited with {rc}").strip()
    if len(out) > DIFF_CHAR_CAP:
        return out[:DIFF_CHAR_CAP], True, None
    return out, False, None


def collect_comments(repo: str, number: int) -> tuple[list[dict], str | None]:
    """Fetch the PR conversation comments (not inline review comments)."""
    data, err = _gh_json(
        [
            "api",
            f"repos/{repo}/issues/{number}/comments",
            "--paginate",
        ]
    )
    if err:
        return [], err
    comments = [
        {
            "id": c.get("id"),
            "author": (c.get("user") or {}).get("login"),
            "created_at": c.get("created_at"),
            "body": c.get("body") or "",
        }
        for c in (data or [])
    ]
    # Keep the most recent COMMENT_CAP, preserving chronological order.
    if len(comments) > COMMENT_CAP:
        comments = comments[-COMMENT_CAP:]
    return comments, None


def collect_review_comments(
    repo: str, number: int
) -> tuple[list[dict], str | None]:
    """Fetch inline review comments attached to specific diff lines."""
    data, err = _gh_json(
        [
            "api",
            f"repos/{repo}/pulls/{number}/comments",
            "--paginate",
        ]
    )
    if err:
        return [], err
    comments = [
        {
            "id": c.get("id"),
            "author": (c.get("user") or {}).get("login"),
            "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"),
            "created_at": c.get("created_at"),
            "body": c.get("body") or "",
        }
        for c in (data or [])
    ]
    if len(comments) > COMMENT_CAP:
        comments = comments[-COMMENT_CAP:]
    return comments, None


def parse_linked_issues(body: str, default_repo: str) -> list[tuple[str, int]]:
    """Return a list of (repo, number) for issues closed by this PR body."""
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for match in LINKED_ISSUE_RE.finditer(body or ""):
        repo = match.group(1) or default_repo
        num = int(match.group(2))
        key = (repo, num)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def collect_linked_issue(repo: str, number: int) -> tuple[dict, str | None]:
    data, err = _gh_json(
        [
            "issue",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "number,title,state,body,labels,url",
        ]
    )
    return data or {}, err


def collect_checks(repo: str, head_sha: str) -> tuple[list[dict], str | None]:
    if not head_sha:
        return [], "no head SHA"
    data, err = _gh_json(
        [
            "api",
            f"repos/{repo}/commits/{head_sha}/check-runs",
        ]
    )
    if err:
        return [], err
    runs = (data or {}).get("check_runs") or []
    return [
        {
            "name": r.get("name"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "html_url": r.get("html_url"),
        }
        for r in runs
    ], None


def resolve_default_repo() -> str | None:
    """Best-effort default for ``--repo`` so CI callers can omit it."""
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    data, err = _gh_json(
        ["repo", "view", "--json", "nameWithOwner"]
    )
    if err or not data:
        return None
    return data.get("nameWithOwner")


def build_bundle(repo: str, number: int) -> dict:
    bundle: dict[str, Any] = {
        "repo": repo,
        "pr_number": number,
        "errors": {},
    }

    meta, err = collect_pr_metadata(repo, number)
    bundle["pr"] = meta
    if err:
        bundle["errors"]["pr"] = err

    diff, truncated, err = collect_pr_diff(repo, number)
    bundle["diff"] = diff
    bundle["diff_truncated"] = truncated
    if err:
        bundle["errors"]["diff"] = err

    comments, err = collect_comments(repo, number)
    bundle["comments"] = comments
    if err:
        bundle["errors"]["comments"] = err

    review_comments, err = collect_review_comments(repo, number)
    bundle["review_comments"] = review_comments
    if err:
        bundle["errors"]["review_comments"] = err

    linked: list[dict] = []
    for issue_repo, issue_num in parse_linked_issues(
        meta.get("body") or "", repo
    ):
        data, err = collect_linked_issue(issue_repo, issue_num)
        if err:
            bundle["errors"][f"linked_issue_{issue_repo}#{issue_num}"] = err
            continue
        linked.append(data)
    bundle["linked_issues"] = linked

    checks, err = collect_checks(repo, meta.get("headRefOid") or "")
    bundle["checks"] = checks
    if err:
        bundle["errors"]["checks"] = err

    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Collect full PR review context (metadata, diff, linked "
            "issues, comments, checks) in a single deterministic call."
        )
    )
    parser.add_argument("pr_number", type=int, help="pull request number")
    parser.add_argument(
        "--repo",
        help="owner/repo slug (default: $GITHUB_REPOSITORY or gh default)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="write JSON bundle to this path instead of stdout",
    )
    args = parser.parse_args()

    if not shutil.which("gh"):
        print("error: gh CLI not found on PATH", file=sys.stderr)
        return 3

    repo = args.repo or resolve_default_repo()
    if not repo:
        print(
            "error: could not determine repo; pass --repo owner/name",
            file=sys.stderr,
        )
        return 2

    bundle = build_bundle(repo, args.pr_number)
    text = json.dumps(bundle, indent=2)

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
