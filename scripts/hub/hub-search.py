#!/usr/bin/env python3
"""
Deterministic dedupe helper for hub proposals.

Searches the hub repo for existing ``status:active`` proposal issues
that might match a candidate improvement, so the ``hub-daily-sweep``
workflow can avoid opening duplicates.

Given a candidate title (and optional origin-repo hint), this script
queries the hub repo's issue index and returns a JSON array of potential
matches, each row including:

- number, title, url, state, labels, created_at, updated_at
- body (full, so the caller can compare diffs/applicability)
- origin_repo (parsed from the ``origin:<owner>/<repo>`` label, if any)

The *judgment* of whether any of the returned rows are genuine duplicates
is left to the caller (typically Claude inside the workflow). This script
is a deterministic `gh` wrapper — no LLM calls.

Usage::

    python3 scripts/hub/hub-search.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --query "refactor auto-improve verify"

    # Restrict to proposals this origin has not yet responded to:
    python3 scripts/hub/hub-search.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --origin damien-robotsix/claude_auto_tune \\
        --query "refactor auto-improve verify"

Exit codes:
    0  success (even if zero matches)
    2  usage error
    3  ``gh`` CLI not installed or not authenticated
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

MATCH_LIMIT = 25


def _hub_env() -> dict[str, str] | None:
    """Return a subprocess env dict that scopes ``gh`` to ``HUB_TOKEN``
    when set. Returns ``None`` (inherit parent env) otherwise."""
    token = os.environ.get("HUB_TOKEN", "").strip()
    if not token:
        return None
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


def _ci_warning(msg: str) -> None:
    """Emit a ``::warning::`` annotation when running inside GitHub
    Actions."""
    print(f"hub-search: {msg}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::hub-search: {msg}")


def _run_gh(args: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            env=_hub_env(),
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


def search_hub(
    hub_repo: str, query: str
) -> tuple[list[dict], str | None]:
    """Run `gh issue list` on the hub repo with a full-text search.

    We rely on GitHub's native ``--search`` so the server side handles
    tokenization; our job is just to shape the results.
    """
    search_terms = f"is:issue label:status:active {query}".strip()
    data, err = _gh_json(
        [
            "issue",
            "list",
            "--repo",
            hub_repo,
            "--state",
            "open",
            "--search",
            search_terms,
            "--limit",
            str(MATCH_LIMIT),
            "--json",
            (
                "number,title,url,state,body,labels,"
                "createdAt,updatedAt"
            ),
        ]
    )
    if err:
        return [], err
    out: list[dict] = []
    for row in data or []:
        labels = [l.get("name") for l in (row.get("labels") or [])]
        origin = None
        for name in labels:
            if name and name.startswith("origin:"):
                origin = name[len("origin:") :]
                break
        out.append(
            {
                "number": row.get("number"),
                "title": row.get("title") or "",
                "url": row.get("url"),
                "state": row.get("state"),
                "labels": labels,
                "origin_repo": origin,
                "created_at": row.get("createdAt"),
                "updated_at": row.get("updatedAt"),
                "body": row.get("body") or "",
            }
        )
    return out, None


def filter_not_responded_by(
    rows: list[dict], origin: str
) -> list[dict]:
    """Drop rows the given origin has already adopted or rejected."""
    adopted = f"adopted-by:{origin}"
    rejected = f"rejected-by:{origin}"
    kept: list[dict] = []
    for row in rows:
        labels = row.get("labels") or []
        if adopted in labels or rejected in labels:
            continue
        kept.append(row)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Search the hub repo for active proposals matching a "
            "candidate improvement (dedupe helper for hub-daily-sweep)."
        )
    )
    parser.add_argument(
        "--hub-repo",
        required=True,
        help="hub repo slug, e.g. damien-robotsix/claude-auto-tune-hub",
    )
    parser.add_argument(
        "--query",
        required=True,
        help=(
            "free-text search query (typically the candidate title or "
            "a short phrase derived from it)"
        ),
    )
    parser.add_argument(
        "--origin",
        help=(
            "if provided, drop results this origin has already "
            "adopted-by:* / rejected-by:* labeled"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        help="write JSON array to this path instead of stdout",
    )
    args = parser.parse_args()

    if not shutil.which("gh"):
        _ci_warning("gh CLI not found on PATH")
        return 3

    rows, err = search_hub(args.hub_repo, args.query)
    if err:
        _ci_warning(f"gh issue list failed: {err}")
        return 3

    if args.origin:
        rows = filter_not_responded_by(rows, args.origin)

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
