#!/usr/bin/env python3
"""
Deterministic opener for hub improvement proposals.

Reads a structured proposal description from a YAML (or JSON) file and
creates a corresponding issue in the configured hub repo, applying the
canonical label set:

- ``status:active``
- ``origin:<owner>/<repo>`` (derived from the proposal file or
  ``$GITHUB_REPOSITORY``)
- ``scope:<workflow|prompt|script|config>`` (one per distinct scope
  declared in the proposal)

Missing labels are created on demand (``gh label create --force``) so
the hub repo does not need to be pre-seeded.

Input file shape (YAML)::

    title: "short imperative summary"
    problem: |
        What pattern / failure mode this addresses.
    evidence: |
        Links to PRs, runs, transcripts that back the proposal.
    proposed_change: |
        File paths + a diff or prose description.
    applicability: |
        Preconditions (e.g. "requires docker harness",
        "only relevant if auto-improve runs on a schedule").
    origin_repo: damien-robotsix/claude_auto_tune
    origin_prs:
      - https://github.com/.../pull/42
    scopes:
      - workflow
      - script

JSON with the same keys is also accepted.

This script is a pure orchestration of ``gh``. It **never** calls an LLM.
All judgment ("is this proposal worth opening?", "what is its scope?")
lives in the workflow layer that invokes this script.

Usage::

    python3 scripts/hub/hub-open-proposal.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --file /tmp/proposal-123.yaml

    # dry-run: print the rendered body and labels without calling gh
    python3 scripts/hub/hub-open-proposal.py \\
        --hub-repo damien-robotsix/claude-auto-tune-hub \\
        --file /tmp/proposal-123.yaml \\
        --dry-run

Exit codes:
    0  success
    2  usage / input error
    3  ``gh`` CLI not installed or not authenticated
    4  proposal file failed schema validation
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

REQUIRED_FIELDS = ("title", "problem", "proposed_change")
ALLOWED_SCOPES = {"workflow", "prompt", "script", "config"}


def _hub_env() -> dict[str, str] | None:
    """Return a subprocess env dict that scopes ``gh`` to ``HUB_TOKEN``
    when it is set, so ``gh`` targets the hub repo rather than the
    runner's default ``GITHUB_TOKEN``. Returns ``None`` (inherit parent
    env) when ``HUB_TOKEN`` is not set — ``gh`` will use whatever
    ambient token is available."""
    token = os.environ.get("HUB_TOKEN", "").strip()
    if not token:
        return None
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    return env


def _ci_warning(msg: str) -> None:
    """Emit a ``::warning::`` annotation when running inside GitHub
    Actions, so the message is visible in the workflow summary."""
    print(f"hub-open-proposal: {msg}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::hub-open-proposal: {msg}")


def _run_gh(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
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


def load_proposal(path: str) -> tuple[dict, str | None]:
    try:
        with open(path, "r") as f:
            raw = f.read()
    except OSError as exc:
        return {}, f"could not read {path}: {exc}"

    # Prefer YAML if available; fall back to JSON. We keep the YAML
    # dependency optional so the script runs in minimal CI images.
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw)
    except ImportError:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return (
                {},
                (
                    f"{path} is not valid JSON and PyYAML is not "
                    f"installed: {exc}"
                ),
            )
    except Exception as exc:  # pragma: no cover - yaml error surface
        return {}, f"yaml parse error in {path}: {exc}"

    if not isinstance(data, dict):
        return {}, f"{path} must contain a mapping at the top level"
    return data, None


def validate(proposal: dict) -> str | None:
    for key in REQUIRED_FIELDS:
        value = proposal.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"missing required field: {key}"
    scopes = proposal.get("scopes") or []
    if not isinstance(scopes, list):
        return "scopes must be a list"
    for scope in scopes:
        if scope not in ALLOWED_SCOPES:
            return (
                f"scope {scope!r} not in "
                f"{sorted(ALLOWED_SCOPES)}"
            )
    origin_prs = proposal.get("origin_prs") or []
    if not isinstance(origin_prs, list):
        return "origin_prs must be a list of URLs"
    return None


def resolve_origin_repo(proposal: dict) -> str | None:
    origin = proposal.get("origin_repo")
    if isinstance(origin, str) and origin.strip():
        return origin.strip()
    return os.environ.get("GITHUB_REPOSITORY")


def render_body(proposal: dict, origin_repo: str) -> str:
    lines: list[str] = []
    lines.append("<!-- hub-proposal:v1 -->")
    lines.append("")
    lines.append("## Problem")
    lines.append(proposal["problem"].rstrip())
    lines.append("")
    lines.append("## Proposed change")
    lines.append(proposal["proposed_change"].rstrip())
    lines.append("")
    evidence = proposal.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        lines.append("## Evidence")
        lines.append(evidence.rstrip())
        lines.append("")
    applicability = proposal.get("applicability")
    if isinstance(applicability, str) and applicability.strip():
        lines.append("## Applicability")
        lines.append(applicability.rstrip())
        lines.append("")
    lines.append("## Origin")
    lines.append(f"- Repo: `{origin_repo}`")
    origin_prs = proposal.get("origin_prs") or []
    if origin_prs:
        lines.append("- PRs:")
        for url in origin_prs:
            lines.append(f"  - {url}")
    lines.append("")
    lines.append(
        "_This proposal is part of the cross-workspace improvement "
        "sharing protocol. It has a 7-day active lifetime and will "
        "then be archived._"
    )
    return "\n".join(lines) + "\n"


def ensure_label(hub_repo: str, name: str, color: str, desc: str) -> None:
    # --force makes this idempotent: creates if missing, updates if
    # present. Failures are swallowed so a read-only token can still
    # open proposals against a hub that already has its labels.
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
            desc,
            "--force",
        ]
    )


def ensure_canonical_labels(
    hub_repo: str, origin_repo: str, scopes: list[str]
) -> list[str]:
    """Create any labels we're about to use and return the list."""
    labels = ["status:active", f"origin:{origin_repo}"]
    ensure_label(
        hub_repo,
        "status:active",
        "0e8a16",
        "Within the 7-day active window",
    )
    ensure_label(
        hub_repo,
        f"origin:{origin_repo}",
        "c5def5",
        f"Proposed by {origin_repo}",
    )
    for scope in scopes:
        label = f"scope:{scope}"
        ensure_label(
            hub_repo,
            label,
            "fbca04",
            f"Touches {scope} surface area",
        )
        labels.append(label)
    return labels


def _extract_issue_number(url: str) -> str | None:
    """Extract the issue number from a GitHub issue URL."""
    # URL looks like https://github.com/owner/repo/issues/42
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == "issues":
        return parts[-1]
    return None


def _apply_labels_fallback(
    hub_repo: str, issue_number: str, labels: list[str]
) -> None:
    """Explicitly apply labels via ``gh issue edit`` as a fallback.

    ``gh issue create --label`` can silently drop labels under certain
    token permission configurations. This ensures labels are applied
    even when that happens."""
    if not labels:
        return
    args = ["issue", "edit", issue_number, "--repo", hub_repo]
    for label in labels:
        args.extend(["--add-label", label])
    rc, _stdout, err = _run_gh(args)
    if rc != 0:
        _ci_warning(
            f"fallback label application failed for issue #{issue_number}: "
            f"{err.strip() if err else f'exit {rc}'}"
        )


def open_proposal(
    hub_repo: str,
    title: str,
    body: str,
    labels: list[str],
) -> tuple[str | None, str | None]:
    args = [
        "issue",
        "create",
        "--repo",
        hub_repo,
        "--title",
        title,
        "--body-file",
        "-",
    ]
    for label in labels:
        args.extend(["--label", label])
    rc, stdout, err = _run_gh(args, stdin=body)
    if rc != 0:
        return None, (err or stdout or f"gh exited with {rc}").strip()
    url = stdout.strip().splitlines()[-1] if stdout.strip() else None
    # Fallback: explicitly apply labels in case gh issue create silently
    # dropped them (can happen with certain token permission configs).
    if url:
        issue_num = _extract_issue_number(url)
        if issue_num:
            _apply_labels_fallback(hub_repo, issue_num, labels)
    return url, None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Open a cross-workspace improvement proposal issue in the "
            "hub repo from a YAML/JSON file."
        )
    )
    parser.add_argument(
        "--hub-repo",
        required=True,
        help="hub repo slug, e.g. damien-robotsix/claude-auto-tune-hub",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="path to the YAML or JSON proposal file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the rendered title, labels, and body to stdout "
            "instead of calling gh"
        ),
    )
    args = parser.parse_args()

    if not shutil.which("gh") and not args.dry_run:
        _ci_warning("gh CLI not found on PATH")
        return 3

    proposal, err = load_proposal(args.file)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    verr = validate(proposal)
    if verr:
        print(f"error: invalid proposal: {verr}", file=sys.stderr)
        return 4

    origin_repo = resolve_origin_repo(proposal)
    if not origin_repo:
        print(
            "error: could not resolve origin_repo; set it in the "
            "proposal file or $GITHUB_REPOSITORY",
            file=sys.stderr,
        )
        return 2

    title = proposal["title"].strip()
    if not title.lower().startswith("[proposal]"):
        title = f"[proposal] {title}"

    body = render_body(proposal, origin_repo)
    scopes = [s for s in (proposal.get("scopes") or []) if s]

    if args.dry_run:
        labels = ["status:active", f"origin:{origin_repo}"] + [
            f"scope:{s}" for s in scopes
        ]
        print(f"title: {title}")
        print(f"labels: {labels}")
        print("body:")
        print(body)
        return 0

    labels = ensure_canonical_labels(args.hub_repo, origin_repo, scopes)
    url, err = open_proposal(args.hub_repo, title, body, labels)
    if err:
        _ci_warning(f"gh issue create failed: {err}")
        return 3

    result = {
        "hub_repo": args.hub_repo,
        "origin_repo": origin_repo,
        "title": title,
        "labels": labels,
        "url": url,
    }
    sys.stdout.write(json.dumps(result, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
