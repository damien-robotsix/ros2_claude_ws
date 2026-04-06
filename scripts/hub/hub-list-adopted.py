#!/usr/bin/env python3
"""
List hub proposals that a given repo has adopted but not yet opened a PR for.

Queries the hub repo for open issues carrying ``adopted-by:<repo>`` but
**not** ``pr-opened-by:<repo>``, so the Phase 3 adopt workflow knows
which proposals still need an adoption PR.

This is a pure ``gh`` wrapper — no LLM calls.

Usage::

    python3 scripts/hub/hub-list-adopted.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --this-repo damien-robotsix/claude_auto_tune

Exit codes:
    0  success (even if zero results)
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

ISSUE_LIMIT = 50


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


def list_adopted_proposals(
    hub_repo: str, this_repo: str
) -> tuple[list[dict], str | None]:
    """List open issues with ``adopted-by:<this_repo>`` label."""
    data, err = _gh_json(
        [
            "issue",
            "list",
            "--repo",
            hub_repo,
            "--state",
            "open",
            "--label",
            f"adopted-by:{this_repo}",
            "--limit",
            str(ISSUE_LIMIT),
            "--json",
            "number,title,url,state,body,labels,createdAt,updatedAt",
        ]
    )
    if err:
        return [], err
    return data or [], None


def filter_no_pr_yet(rows: list[dict], this_repo: str) -> list[dict]:
    """Drop proposals that already have a ``pr-opened-by:<this_repo>`` label."""
    pr_label = f"pr-opened-by:{this_repo}"
    return [
        row
        for row in rows
        if pr_label not in [l.get("name", "") for l in (row.get("labels") or [])]
    ]


def format_output(rows: list[dict]) -> list[dict]:
    """Reshape rows for downstream consumption."""
    out: list[dict] = []
    for row in rows:
        labels = [l.get("name") for l in (row.get("labels") or [])]
        origin = None
        for name in labels:
            if name and name.startswith("origin:"):
                origin = name[len("origin:"):]
                break
        out.append(
            {
                "number": row.get("number"),
                "title": row.get("title") or "",
                "url": row.get("url"),
                "labels": labels,
                "origin_repo": origin,
                "created_at": row.get("createdAt"),
                "body": row.get("body") or "",
            }
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List hub proposals adopted by this repo that still need a PR."
        )
    )
    parser.add_argument(
        "--hub-repo",
        required=True,
        help="hub repo slug, e.g. damien-robotsix/claude-auto-tune-hub",
    )
    parser.add_argument(
        "--this-repo",
        required=True,
        help=(
            "this workspace's repo slug, e.g. "
            "damien-robotsix/claude_auto_tune"
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

    rows, err = list_adopted_proposals(args.hub_repo, args.this_repo)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 3

    filtered = filter_no_pr_yet(rows, args.this_repo)
    result = format_output(filtered)
    payload = json.dumps(result, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(payload + "\n")
        print(
            f"wrote {len(result)} proposal(s) to {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(payload + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
