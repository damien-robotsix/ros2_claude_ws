#!/usr/bin/env python3
"""
Deterministic publisher for local Claude Code session transcripts.

Copies new ``*.jsonl`` session transcripts from the local
``.claude-home/.claude/projects/`` tree into the shared hub repo, under
``transcripts/<workspace-slug>/<YYYY-MM-DD>/<session-id>.jsonl``. This
lets CI-side workflows (the ``workflow-insights-extractor`` subagent in
particular) fold signals from local runs into the same discover /
verify clustering pass that already consumes CI transcripts.

This script is the **publish half** of the local-transcript lane. It
reuses the ``claude-auto-tune-hub`` repo that was created for the
improvement-proposal protocol (issue #32) but it is otherwise fully
independent of that protocol: different directory tree, different
payload shape, no shared scripts, no shared labels.

Design invariants
-----------------
- **No LLM calls.** Pure orchestration of ``gh`` + ``git`` + file copy.
  All judgment ("which sessions are worth publishing?", "which lines
  are sensitive?") lives upstream in the caller, or in the static
  redaction regexes below.
- **Auth via ``gh``.** All GitHub interaction — clone, push — reuses
  the credentials of the locally installed ``gh`` CLI. The script runs
  ``gh auth setup-git`` on every invocation (idempotent) so a plain
  ``git push`` inside the hub clone uses ``gh``'s token as its
  credential helper. No separate PAT, SSH key, or ``~/.netrc`` entry
  is needed: if ``gh auth status`` succeeds on the host, this script
  can publish.
- **Own-slug only.** A workspace writes to its own
  ``transcripts/<slug>/`` subtree and never touches another workspace's
  subtree. Collisions between forks are impossible.
- **Idempotent.** Files already present in the hub with the same
  ``session-id.jsonl`` name are skipped. Running the script twice in a
  row is a no-op.
- **Opt-in via config, unconditional in invocation.** If
  ``hub.enabled`` is false or ``hub.local_transcripts.enabled`` is
  absent/false in ``auto_tune_config.yml``, the script exits 0
  without touching anything. ``run.sh`` calls this script after every
  local Docker session unconditionally — the config file is the
  single source of truth, so enabling the lane is a one-line config
  change, not a wiring change.
- **Redaction on by default.** Lines matching common secret patterns or
  containing ``$HOME``-prefixed absolute paths are scrubbed before
  upload. ``--no-redact`` disables this (discouraged, even for a
  private hub).

Layout written to the hub
-------------------------
::

    transcripts/
      <workspace-slug>/
        <YYYY-MM-DD>/
          <session-id>.jsonl
          <session-id>.meta.json

``<workspace-slug>`` is derived from ``$GITHUB_REPOSITORY`` if set, or
parsed from the ``origin`` git remote of the current workspace. The
slug uses the literal ``owner/repo`` form (slashes are valid in path
segments).

``<session-id>.meta.json`` carries provenance for the extractor::

    {
      "source": "local",
      "workspace": "damien-robotsix/claude_auto_tune",
      "session_id": "<uuid>",
      "captured_at": "2026-04-05T21:34:12Z",
      "git_sha": "<HEAD sha at push time>",
      "redacted": true
    }

Usage
-----
::

    python3 scripts/hub/push-local-transcripts.py
    python3 scripts/hub/push-local-transcripts.py --dry-run
    python3 scripts/hub/push-local-transcripts.py --no-redact
    python3 scripts/hub/push-local-transcripts.py \\
        --transcripts-dir .claude-home/.claude/projects \\
        --hub-cache ~/.cache/claude-auto-tune-hub

Exit codes
----------
    0  success (including the no-op path when disabled)
    2  usage / configuration error
    3  ``gh``/``git`` not installed, ``gh`` not authenticated, or hub
       clone/push failed
    4  nothing to push (all sessions already in hub) — still soft
       success, but distinguishable for callers that want to log it
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# ----- Config loading ------------------------------------------------

DEFAULT_CONFIG_PATH = Path("auto_tune_config.yml")
DEFAULT_TRANSCRIPTS_DIR = Path(".claude-home/.claude/projects")
DEFAULT_HUB_CACHE = Path.home() / ".cache" / "claude-auto-tune-hub"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
    except ImportError:
        # Fall back to a tiny hand-rolled scan for the two keys we
        # care about so the script stays usable in minimal images.
        return _minimal_yaml_scan(path)
    if not isinstance(data, dict):
        return {}
    return data


def _minimal_yaml_scan(path: Path) -> dict:
    """Very small fallback parser for `hub:` subtree only.

    Handles the exact shape we ship in auto_tune_config.yml. Not a
    general YAML parser — just enough to read booleans and strings out
    of the ``hub:`` / ``hub.local_transcripts:`` nesting.
    """
    hub: dict = {}
    current_section: str | None = None
    sub: dict = {}
    with open(path, "r") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line.startswith("hub:"):
                current_section = "hub"
                continue
            if current_section == "hub":
                if line.startswith("  ") and not line.startswith("    "):
                    # top-level key under hub
                    key, _, value = line.strip().partition(":")
                    value = value.strip()
                    if key == "local_transcripts":
                        current_section = "hub.local_transcripts"
                        hub["local_transcripts"] = sub
                        continue
                    hub[key] = _coerce(value)
                elif line.startswith("    "):
                    # still inside hub subsection
                    pass
                elif line and not line.startswith(" "):
                    break
            elif current_section == "hub.local_transcripts":
                if line.startswith("    "):
                    key, _, value = line.strip().partition(":")
                    sub[key] = _coerce(value.strip())
                elif line.startswith("  "):
                    key, _, value = line.strip().partition(":")
                    if key == "local_transcripts":
                        continue
                    current_section = "hub"
                    hub[key] = _coerce(value.strip())
                else:
                    break
    return {"hub": hub} if hub else {}


def _coerce(value: str):
    v = value.strip().strip('"').strip("'")
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    return v


# ----- Workspace identity --------------------------------------------


def resolve_workspace_slug() -> str | None:
    env = os.environ.get("GITHUB_REPOSITORY")
    if env and "/" in env:
        return env.strip()
    # Fall back to parsing the origin remote.
    rc, out, _ = _run(["git", "remote", "get-url", "origin"])
    if rc != 0:
        return None
    url = out.strip()
    # Accept git@github.com:owner/repo.git and https://github.com/owner/repo(.git)
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return m.group(1)


def current_git_sha() -> str:
    rc, out, _ = _run(["git", "rev-parse", "HEAD"])
    return out.strip() if rc == 0 else "unknown"


# ----- Redaction -----------------------------------------------------

# Conservative patterns. Matches what most secret scanners look for.
# Redaction replaces the full match with a tagged placeholder so the
# extractor can still see *that* a secret was present without seeing
# its value.
_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "[REDACTED:anthropic_key]"),
    (re.compile(r"ghp_[A-Za-z0-9]{30,}"), "[REDACTED:github_pat]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{40,}"), "[REDACTED:github_pat]"),
    (re.compile(r"gho_[A-Za-z0-9]{30,}"), "[REDACTED:github_oauth]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_key]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
]


def _home_path_pattern() -> re.Pattern[str] | None:
    home = os.environ.get("HOME")
    if not home or home in ("/", ""):
        return None
    return re.compile(re.escape(home) + r"[^\s\"']*")


def redact_line(line: str, home_re: re.Pattern[str] | None) -> str:
    out = line
    for pat, placeholder in _REDACT_PATTERNS:
        out = pat.sub(placeholder, out)
    if home_re is not None:
        out = home_re.sub("[REDACTED:host_path]", out)
    return out


def copy_with_redaction(src: Path, dst: Path, redact: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not redact:
        shutil.copyfile(src, dst)
        return
    home_re = _home_path_pattern()
    with open(src, "r", encoding="utf-8", errors="replace") as fin, open(
        dst, "w", encoding="utf-8"
    ) as fout:
        for line in fin:
            fout.write(redact_line(line, home_re))


# ----- Git / hub interaction -----------------------------------------


def _run(
    args: list[str], cwd: Path | None = None, stdin: str | None = None
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found on PATH"
    return proc.returncode, proc.stdout, proc.stderr


def ensure_gh_ready() -> str | None:
    """Verify ``gh`` is installed + authenticated and register it as
    the git credential helper for github.com. Returns an error string
    on failure, or ``None`` on success.

    This is the single point where the script learns about GitHub
    credentials. Everything downstream (``gh repo clone``, ``git push``)
    reuses the helper wired up here.
    """
    if not shutil.which("gh"):
        return (
            "gh CLI not found on PATH (install from https://cli.github.com/)"
        )
    rc, _, err = _run(["gh", "auth", "status"])
    if rc != 0:
        hint = err.strip() or "run `gh auth login` first"
        return f"gh not authenticated: {hint}"
    # Idempotent: rewrites ~/.gitconfig credential.helper for github.com
    # to call `gh auth git-credential`. Safe to run every invocation.
    rc, _, err = _run(["gh", "auth", "setup-git"])
    if rc != 0:
        return f"gh auth setup-git failed: {err.strip()}"
    return None


def gh_commit_identity() -> tuple[str, str]:
    """Return (name, email) to author the hub commit as, derived from
    the authenticated ``gh`` user. Falls back to a generic bot identity
    if ``gh api user`` cannot be reached (should not happen after
    ``ensure_gh_ready`` succeeds, but we stay defensive)."""
    rc, out, _ = _run(["gh", "api", "user", "--jq", ".login"])
    login = out.strip() if rc == 0 and out.strip() else "claude-auto-tune"
    rc, out, _ = _run(["gh", "api", "user", "--jq", ".id"])
    uid = out.strip() if rc == 0 and out.strip() else "0"
    # GitHub no-reply address format — avoids leaking the user's real
    # email into the hub commit history.
    email = f"{uid}+{login}@users.noreply.github.com"
    return login, email


def ensure_hub_clone(hub_repo: str, cache_dir: Path) -> tuple[Path, str | None]:
    """Clone the hub into ``cache_dir`` via ``gh repo clone`` if absent,
    otherwise fetch and hard-reset to ``origin/main``.

    Returns (repo_dir, error).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = cache_dir / "repo"
    if not (repo_dir / ".git").exists():
        # `gh repo clone` writes an authenticated remote URL and
        # inherits the credential helper wired by `gh auth setup-git`.
        rc, _, err = _run(
            [
                "gh",
                "repo",
                "clone",
                hub_repo,
                str(repo_dir),
                "--",
                "--depth",
                "50",
            ]
        )
        if rc != 0:
            return repo_dir, f"gh repo clone {hub_repo} failed: {err.strip()}"
        return repo_dir, None
    rc, _, err = _run(
        ["git", "fetch", "origin", "main", "--depth", "50"], cwd=repo_dir
    )
    if rc != 0:
        return repo_dir, f"git fetch failed: {err.strip()}"
    rc, _, err = _run(["git", "checkout", "main"], cwd=repo_dir)
    if rc != 0:
        return repo_dir, f"git checkout main failed: {err.strip()}"
    rc, _, err = _run(["git", "reset", "--hard", "origin/main"], cwd=repo_dir)
    if rc != 0:
        return repo_dir, f"git reset failed: {err.strip()}"
    return repo_dir, None


def commit_and_push(
    repo_dir: Path, slug: str, added: int
) -> tuple[bool, str | None]:
    rc, out, err = _run(["git", "status", "--porcelain"], cwd=repo_dir)
    if rc != 0:
        return False, f"git status failed: {err.strip()}"
    if not out.strip():
        return False, None  # nothing staged, nothing to do
    rc, _, err = _run(["git", "add", "transcripts"], cwd=repo_dir)
    if rc != 0:
        return False, f"git add failed: {err.strip()}"
    name, email = gh_commit_identity()
    message = f"transcripts: publish {added} local session(s) from {slug}"
    # Pass identity via `-c` so we never touch the user's global git
    # config. The hub remote's credential helper is already ``gh``
    # (set by ``ensure_gh_ready``), so the push reuses that token.
    rc, _, err = _run(
        [
            "git",
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "-m",
            message,
        ],
        cwd=repo_dir,
    )
    if rc != 0:
        return False, f"git commit failed: {err.strip()}"
    rc, _, err = _run(["git", "push", "origin", "main"], cwd=repo_dir)
    if rc != 0:
        return False, f"git push failed: {err.strip()}"
    return True, None


# ----- Main pipeline -------------------------------------------------


def discover_sessions(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.jsonl") if p.is_file())


def session_date(path: Path) -> str:
    # Use the file's mtime as the session date. JSONL files are append-
    # only until the session ends, so mtime is a good proxy for "when
    # the conversation happened". UTC keeps partition keys stable
    # across contributors.
    ts = _dt.datetime.fromtimestamp(path.stat().st_mtime, tz=_dt.timezone.utc)
    return ts.strftime("%Y-%m-%d")


def write_meta(
    meta_path: Path,
    slug: str,
    session_id: str,
    git_sha: str,
    redacted: bool,
    parent_session_id: str | None = None,
) -> None:
    meta: dict = {
        "source": "local",
        "workspace": slug,
        "session_id": session_id,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "git_sha": git_sha,
        "redacted": redacted,
    }
    if parent_session_id is not None:
        meta["parent_session_id"] = parent_session_id
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")


def _is_subagent(path: Path) -> bool:
    """Return True if *path* lives under a ``subagents/`` directory."""
    return "subagents" in path.parts


def _parent_session_id(path: Path) -> str | None:
    """Extract the parent session UUID from a subagent file path.

    Claude Code stores subagent transcripts at::

        <session-uuid>/subagents/agent-<id>.jsonl

    Returns the session UUID or ``None`` if the path doesn't match.
    """
    parts = path.parts
    try:
        idx = parts.index("subagents")
    except ValueError:
        return None
    if idx > 0:
        return parts[idx - 1]
    return None


def plan_copies(
    sessions: Iterable[Path], hub_repo_dir: Path, slug: str
) -> list[tuple[Path, Path, Path, str, str | None]]:
    """Return list of (src, dst_jsonl, dst_meta, session_id,
    parent_session_id) tuples for sessions not yet in the hub clone.

    Subagent files are stored under
    ``<date>/<parent-session-id>/subagents/<agent-id>.jsonl``
    to preserve the parent-child relationship.
    """
    plan: list[tuple[Path, Path, Path, str, str | None]] = []
    base = hub_repo_dir / "transcripts" / slug
    for src in sessions:
        session_id = src.stem
        date = session_date(src)
        parent_sid = _parent_session_id(src)
        if parent_sid is not None:
            # Subagent: nest under parent session directory
            dst_jsonl = base / date / parent_sid / "subagents" / f"{session_id}.jsonl"
            dst_meta = base / date / parent_sid / "subagents" / f"{session_id}.meta.json"
        else:
            dst_jsonl = base / date / f"{session_id}.jsonl"
            dst_meta = base / date / f"{session_id}.meta.json"
        if dst_jsonl.exists():
            continue
        plan.append((src, dst_jsonl, dst_meta, session_id, parent_sid))
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish local Claude Code session transcripts to the "
            "shared hub repo under transcripts/<slug>/<date>/."
        )
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="path to auto_tune_config.yml (default: %(default)s)",
    )
    parser.add_argument(
        "--transcripts-dir",
        default=str(DEFAULT_TRANSCRIPTS_DIR),
        help=(
            "root directory containing Claude Code session JSONL files "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--hub-cache",
        default=str(DEFAULT_HUB_CACHE),
        help=(
            "local cache directory for the hub clone "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--hub-repo",
        default=None,
        help=(
            "override the hub repo slug (normally read from "
            "auto_tune_config.yml: hub.repo)"
        ),
    )
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help=(
            "disable the default secret/host-path redaction pass "
            "(discouraged, even for a private hub)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "show what would be published without cloning, copying, or "
            "pushing"
        ),
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    hub_config = config.get("hub") or {}
    lt_config = (hub_config.get("local_transcripts") or {}) if isinstance(
        hub_config, dict
    ) else {}

    if not hub_config.get("enabled"):
        print("hub.enabled is false; nothing to do.", file=sys.stderr)
        return 0
    if not lt_config.get("enabled"):
        print(
            "hub.local_transcripts.enabled is false or unset; nothing "
            "to do.",
            file=sys.stderr,
        )
        return 0

    hub_repo = args.hub_repo or hub_config.get("repo")
    if not hub_repo:
        print(
            "error: hub.repo missing from config and --hub-repo not "
            "provided",
            file=sys.stderr,
        )
        return 2

    slug = resolve_workspace_slug()
    if not slug:
        print(
            "error: could not resolve workspace slug from "
            "$GITHUB_REPOSITORY or git origin remote",
            file=sys.stderr,
        )
        return 2

    transcripts_dir = Path(args.transcripts_dir)
    sessions = discover_sessions(transcripts_dir)
    if not sessions:
        print(
            f"no sessions found under {transcripts_dir}; nothing to do.",
            file=sys.stderr,
        )
        return 0

    redact = not args.no_redact

    if args.dry_run:
        # Show what we would write without touching the hub clone.
        print(f"hub_repo: {hub_repo}")
        print(f"workspace_slug: {slug}")
        print(f"redact: {redact}")
        print(f"sessions found: {len(sessions)}")
        for src in sessions:
            print(
                f"  would publish: transcripts/{slug}/"
                f"{session_date(src)}/{src.stem}.jsonl"
            )
        return 0

    if not shutil.which("git"):
        print("error: git not found on PATH", file=sys.stderr)
        return 3
    gh_err = ensure_gh_ready()
    if gh_err:
        print(f"error: {gh_err}", file=sys.stderr)
        return 3

    repo_dir, err = ensure_hub_clone(hub_repo, Path(args.hub_cache))
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 3

    plan = plan_copies(sessions, repo_dir, slug)
    if not plan:
        print("all local sessions already published; nothing new.")
        return 4

    git_sha = current_git_sha()
    for src, dst_jsonl, dst_meta, session_id, parent_sid in plan:
        copy_with_redaction(src, dst_jsonl, redact=redact)
        write_meta(
            dst_meta, slug, session_id, git_sha,
            redacted=redact, parent_session_id=parent_sid,
        )

    ok, err = commit_and_push(repo_dir, slug, added=len(plan))
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 3
    if not ok:
        print("nothing to commit (possible race); no push performed.")
        return 4

    summary = {
        "hub_repo": hub_repo,
        "workspace_slug": slug,
        "published": len(plan),
        "redacted": redact,
    }
    sys.stdout.write(json.dumps(summary, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
