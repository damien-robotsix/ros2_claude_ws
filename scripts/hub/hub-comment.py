#!/usr/bin/env python3
"""
Post a structured comment on a hub proposal issue.

Used by the ``hub-sync`` workflow to record this workspace's relevance
verdict (adopt, reject, or defer) on a proposal.

The comment includes a machine-readable marker so subsequent runs can
detect that this repo has already responded.

This is a pure ``gh`` wrapper — no LLM calls.

Usage::

    python3 scripts/hub/hub-comment.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --issue 42 \\
        --this-repo damien-robotsix/claude_auto_tune \\
        --verdict reject \\
        --reason "Not applicable — this workspace does not use Docker"

    python3 scripts/hub/hub-comment.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --issue 42 \\
        --this-repo damien-robotsix/claude_auto_tune \\
        --verdict adopt \\
        --reason "Relevant improvement, will open adoption PR"

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

VALID_VERDICTS = ("adopt", "reject", "defer")


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


def _run_gh(
    args: list[str], stdin: str | None = None
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=_hub_env(),
        )
    except FileNotFoundError:
        return 127, "", "gh CLI not found on PATH"
    return proc.returncode, proc.stdout, proc.stderr


def _ci_warning(msg: str) -> None:
    print(f"hub-comment: {msg}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::hub-comment: {msg}")


def render_comment(
    this_repo: str, verdict: str, reason: str
) -> str:
    """Render a structured sync-response comment."""
    lines = [
        f"<!-- hub-sync-response:{this_repo} -->",
        "",
        f"**Sync response from `{this_repo}`**",
        "",
        f"- **Verdict:** `{verdict}`",
        f"- **Reason:** {reason}",
        "",
        "_This comment was posted automatically by the hub-sync workflow._",
    ]
    return "\n".join(lines) + "\n"


def post_comment(
    hub_repo: str, issue_number: str, body: str
) -> tuple[str | None, str | None]:
    """Post a comment on the given hub issue. Returns (url, error)."""
    rc, stdout, err = _run_gh(
        [
            "issue",
            "comment",
            issue_number,
            "--repo",
            hub_repo,
            "--body-file",
            "-",
        ],
        stdin=body,
    )
    if rc != 0:
        return None, (err or stdout or f"gh exited with {rc}").strip()
    url = stdout.strip().splitlines()[-1] if stdout.strip() else None
    return url, None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post a structured sync-response comment on a hub issue."
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
        "--this-repo",
        required=True,
        help="this workspace's repo slug",
    )
    parser.add_argument(
        "--verdict",
        required=True,
        choices=VALID_VERDICTS,
        help="relevance verdict: adopt, reject, or defer",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="short explanation for the verdict",
    )
    args = parser.parse_args()

    if not shutil.which("gh"):
        print("error: gh CLI not found on PATH", file=sys.stderr)
        return 3

    body = render_comment(args.this_repo, args.verdict, args.reason)
    url, err = post_comment(args.hub_repo, args.issue, body)
    if err:
        _ci_warning(f"failed to comment on issue #{args.issue}: {err}")
        return 3

    result = {
        "hub_repo": args.hub_repo,
        "issue": args.issue,
        "this_repo": args.this_repo,
        "verdict": args.verdict,
        "comment_url": url,
    }
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
