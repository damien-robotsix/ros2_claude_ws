#!/usr/bin/env python3
"""
Add or remove labels on a hub proposal issue.

Used by the ``hub-sync`` workflow to apply per-repo verdict labels
(``rejected-by:<owner>/<repo>``, ``adopted-by:<owner>/<repo>``).

Labels are created on demand (``gh label create --force``) before being
applied, so the hub repo does not need to be pre-seeded with every
possible per-repo label.

This is a pure ``gh`` wrapper — no LLM calls.

Usage::

    python3 scripts/hub/hub-label.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --issue 42 \\
        --add "rejected-by:damien-robotsix/claude_auto_tune"

    python3 scripts/hub/hub-label.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --issue 42 \\
        --add "adopted-by:damien-robotsix/claude_auto_tune"

    python3 scripts/hub/hub-label.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --issue 42 \\
        --remove "status:active"

Exit codes:
    0  success
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


def _ci_warning(msg: str) -> None:
    print(f"hub-label: {msg}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::hub-label: {msg}")


def ensure_label(hub_repo: str, name: str) -> None:
    """Create label if it doesn't exist. Failures are swallowed."""
    color = "c5def5"
    if name.startswith("rejected-by:"):
        color = "e11d48"
    elif name.startswith("adopted-by:"):
        color = "0e8a16"
    _run_gh(
        [
            "label",
            "create",
            name,
            "--repo",
            hub_repo,
            "--color",
            color,
            "--description",
            name,
            "--force",
        ]
    )


def add_labels(
    hub_repo: str, issue_number: str, labels: list[str]
) -> str | None:
    """Add labels to an issue. Returns error string or None."""
    for label in labels:
        ensure_label(hub_repo, label)
    args = ["issue", "edit", issue_number, "--repo", hub_repo]
    for label in labels:
        args.extend(["--add-label", label])
    rc, _stdout, err = _run_gh(args)
    if rc != 0:
        return (err or f"gh exited with {rc}").strip()
    return None


def remove_labels(
    hub_repo: str, issue_number: str, labels: list[str]
) -> str | None:
    """Remove labels from an issue. Returns error string or None."""
    args = ["issue", "edit", issue_number, "--repo", hub_repo]
    for label in labels:
        args.extend(["--remove-label", label])
    rc, _stdout, err = _run_gh(args)
    if rc != 0:
        return (err or f"gh exited with {rc}").strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add or remove labels on a hub proposal issue."
    )
    parser.add_argument(
        "--hub-repo",
        required=True,
        help="hub repo slug",
    )
    parser.add_argument(
        "--issue",
        required=True,
        help="hub issue number",
    )
    parser.add_argument(
        "--add",
        action="append",
        default=[],
        help="label to add (repeatable)",
    )
    parser.add_argument(
        "--remove",
        action="append",
        default=[],
        help="label to remove (repeatable)",
    )
    args = parser.parse_args()

    if not args.add and not args.remove:
        print("error: at least one --add or --remove required", file=sys.stderr)
        return 2

    if not shutil.which("gh"):
        print("error: gh CLI not found on PATH", file=sys.stderr)
        return 3

    results: dict = {
        "hub_repo": args.hub_repo,
        "issue": args.issue,
        "added": [],
        "removed": [],
        "errors": [],
    }

    if args.add:
        err = add_labels(args.hub_repo, args.issue, args.add)
        if err:
            _ci_warning(f"add labels failed: {err}")
            results["errors"].append(f"add: {err}")
        else:
            results["added"] = args.add

    if args.remove:
        err = remove_labels(args.hub_repo, args.issue, args.remove)
        if err:
            _ci_warning(f"remove labels failed: {err}")
            results["errors"].append(f"remove: {err}")
        else:
            results["removed"] = args.remove

    sys.stdout.write(json.dumps(results, indent=2) + "\n")
    return 3 if results["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
