#!/usr/bin/env python3
"""
Deterministic publisher for CI Claude Code session transcripts to the hub.

This is the CI counterpart of ``push-local-transcripts.py``. It runs as a
post-session step in every CI workflow that invokes Claude Code and pushes
the session transcript(s) to the shared hub repo under
``transcripts/<workspace-slug>/<YYYY-MM-DD>/<session-id>.jsonl``.

Together with the local push script, this gives the hub a **global view**
of all Claude Code transcripts across all workspaces — both local Docker
sessions and CI workflow sessions.

Key differences from the local push script:

- **Auth via ``HUB_TOKEN`` env var.** CI does not have ``gh auth login``
  state. The script exports ``HUB_TOKEN`` as ``GH_TOKEN`` only for its
  own subprocess calls, matching the pattern used by
  ``fetch-local-transcripts.py``.
- **Best-effort on failure.** All errors print a warning and exit 0 so
  the script never fails a CI workflow. The transcript push is additive
  and must never block the primary workflow job.
- **Transcript path.** CI transcripts live at
  ``~/.claude/projects/**/*.jsonl``, not in ``.claude-home/``.
- **Config key.** Gated on ``hub.ci_transcripts.enabled`` (separate from
  ``hub.local_transcripts.enabled``).
- **Richer metadata.** The ``.meta.json`` sidecar includes CI-specific
  fields: ``workflow``, ``run_id``, ``run_url``, ``actor``.

Design invariants
-----------------
- **No LLM calls.** Pure orchestration of ``gh`` + ``git`` + file copy.
- **Redaction on by default.** Same secret/host-path scrubbing as the
  local push script.
- **Own-slug only.** Writes to ``transcripts/<own-slug>/`` only.
- **Idempotent.** Sessions already present in the hub are skipped.

Layout written to the hub
-------------------------
::

    transcripts/
      <workspace-slug>/
        <YYYY-MM-DD>/
          <session-id>.jsonl
          <session-id>.meta.json

Usage
-----
::

    HUB_TOKEN=ghp_xxx python3 scripts/hub/push-ci-transcripts.py
    python3 scripts/hub/push-ci-transcripts.py --dry-run

Exit codes
----------
Always 0. This script never blocks a CI workflow.
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

# ----- Config loading ------------------------------------------------

DEFAULT_CONFIG_PATH = Path("auto_tune_config.yml")
DEFAULT_TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
    except ImportError:
        return _minimal_yaml_scan(path)
    if not isinstance(data, dict):
        return {}
    return data


def _minimal_yaml_scan(path: Path) -> dict:
    """Tiny fallback parser for the ``hub:`` subtree only."""
    hub: dict = {}
    sub: dict = {}
    current: str | None = None
    with open(path, "r") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line.startswith("hub:"):
                current = "hub"
                continue
            if current == "hub":
                if line.startswith("  ") and not line.startswith("    "):
                    key, _, value = line.strip().partition(":")
                    value = value.strip()
                    if key == "ci_transcripts":
                        current = "hub.ci_transcripts"
                        hub["ci_transcripts"] = sub
                        continue
                    if key == "local_transcripts":
                        # Skip the local_transcripts sub-block
                        current = "hub.skip_sub"
                        continue
                    hub[key] = _coerce(value)
                elif line and not line.startswith(" "):
                    break
            elif current == "hub.ci_transcripts":
                if line.startswith("    "):
                    key, _, value = line.strip().partition(":")
                    sub[key] = _coerce(value.strip())
                elif line.startswith("  "):
                    key, _, value = line.strip().partition(":")
                    current = "hub"
                    hub[key] = _coerce(value.strip())
                else:
                    break
            elif current == "hub.skip_sub":
                if line.startswith("    "):
                    continue  # skip nested keys
                elif line.startswith("  ") and not line.startswith("    "):
                    key, _, value = line.strip().partition(":")
                    value = value.strip()
                    if key == "ci_transcripts":
                        current = "hub.ci_transcripts"
                        hub["ci_transcripts"] = sub
                        continue
                    current = "hub"
                    hub[key] = _coerce(value)
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
    rc, out, _ = _run(["git", "remote", "get-url", "origin"])
    if rc != 0:
        return None
    url = out.strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return m.group(1)


def current_git_sha() -> str:
    rc, out, _ = _run(["git", "rev-parse", "HEAD"])
    return out.strip() if rc == 0 else "unknown"


# ----- Redaction -----------------------------------------------------

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


# ----- Subprocess helper ---------------------------------------------


def _run(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found on PATH"
    return proc.returncode, proc.stdout, proc.stderr


# ----- Hub clone -----------------------------------------------------


def ensure_hub_clone(
    hub_repo: str, cache_dir: Path, token: str
) -> tuple[Path, str | None]:
    """Shallow-clone (or fetch+reset) the hub repo into ``cache_dir``.

    Uses ``gh repo clone`` under a scoped environment so ``GH_TOKEN``
    never leaks into later workflow steps. Returns ``(repo_dir, error)``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = cache_dir / "repo"
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)
    if not (repo_dir / ".git").exists():
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
                "--no-tags",
            ],
            env=env,
        )
        if rc != 0:
            return repo_dir, f"gh repo clone {hub_repo} failed: {err.strip()}"
        return repo_dir, None
    rc, _, err = _run(
        ["git", "fetch", "origin", "main", "--depth", "50"],
        cwd=repo_dir,
        env=env,
    )
    if rc != 0:
        return repo_dir, f"git fetch failed: {err.strip()}"
    rc, _, err = _run(["git", "checkout", "main"], cwd=repo_dir, env=env)
    if rc != 0:
        return repo_dir, f"git checkout main failed: {err.strip()}"
    rc, _, err = _run(
        ["git", "reset", "--hard", "origin/main"], cwd=repo_dir, env=env
    )
    if rc != 0:
        return repo_dir, f"git reset failed: {err.strip()}"
    return repo_dir, None


def commit_and_push(
    repo_dir: Path, slug: str, added: int, token: str
) -> tuple[bool, str | None]:
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    env.pop("GITHUB_TOKEN", None)

    rc, out, err = _run(["git", "status", "--porcelain"], cwd=repo_dir, env=env)
    if rc != 0:
        return False, f"git status failed: {err.strip()}"
    if not out.strip():
        return False, None

    rc, _, err = _run(["git", "add", "transcripts"], cwd=repo_dir, env=env)
    if rc != 0:
        return False, f"git add failed: {err.strip()}"

    # Derive commit identity from the token's owner
    rc, login_out, _ = _run(
        ["gh", "api", "user", "--jq", ".login"], env=env
    )
    login = login_out.strip() if rc == 0 and login_out.strip() else "claude-auto-tune"
    rc, uid_out, _ = _run(
        ["gh", "api", "user", "--jq", ".id"], env=env
    )
    uid = uid_out.strip() if rc == 0 and uid_out.strip() else "0"
    email = f"{uid}+{login}@users.noreply.github.com"

    message = f"transcripts: publish {added} CI session(s) from {slug}"
    rc, _, err = _run(
        [
            "git",
            "-c", f"user.name={login}",
            "-c", f"user.email={email}",
            "commit", "-m", message,
        ],
        cwd=repo_dir,
        env=env,
    )
    if rc != 0:
        return False, f"git commit failed: {err.strip()}"

    # gh auth setup-git so push uses the token
    _run(["gh", "auth", "setup-git"], env=env)

    rc, _, err = _run(
        ["git", "push", "origin", "main"], cwd=repo_dir, env=env
    )
    if rc != 0:
        return False, f"git push failed: {err.strip()}"
    return True, None


# ----- Main pipeline -------------------------------------------------


def discover_sessions(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.jsonl") if p.is_file())


def session_date(path: Path) -> str:
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
        "source": "ci",
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
    # Include CI context when available
    for env_key, meta_key in [
        ("GITHUB_WORKFLOW", "workflow"),
        ("GITHUB_RUN_ID", "run_id"),
        ("GITHUB_ACTOR", "actor"),
        ("GITHUB_REF_NAME", "ref"),
    ]:
        val = os.environ.get(env_key)
        if val:
            meta[meta_key] = val
    # Build run URL from server + repo + run_id
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        meta["run_url"] = f"{server}/{repo}/actions/runs/{run_id}"

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
    sessions: list[Path], hub_repo_dir: Path, slug: str
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


def _warn(msg: str) -> None:
    print(f"push-ci-transcripts: {msg}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::push-ci-transcripts: {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Push CI Claude Code session transcripts to the shared hub "
            "repo under transcripts/<slug>/<date>/."
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
        default=".scratch/hub-push-cache",
        help="local cache directory for the hub clone (default: %(default)s)",
    )
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="disable secret/host-path redaction (discouraged)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be published without cloning or pushing",
    )
    args = parser.parse_args()

    # ---- Config gate ----
    config = load_config(Path(args.config))
    hub_config = config.get("hub") or {}
    ct_config = (
        hub_config.get("ci_transcripts") or {}
    ) if isinstance(hub_config, dict) else {}

    if not hub_config.get("enabled"):
        print("hub.enabled is false; nothing to do.", file=sys.stderr)
        return 0
    if not ct_config.get("enabled"):
        print(
            "hub.ci_transcripts.enabled is false or unset; nothing to do.",
            file=sys.stderr,
        )
        return 0

    # ---- Token gate ----
    token = os.environ.get("HUB_TOKEN", "")
    if not token:
        _warn(
            "HUB_TOKEN not set; skipping CI transcript push. "
            "Add a fine-grained PAT with contents:write on the hub repo "
            "as the HUB_TOKEN repository secret."
        )
        return 0

    hub_repo = hub_config.get("repo")
    if not hub_repo:
        _warn("hub.repo missing from config; skipping.")
        return 0

    slug = resolve_workspace_slug()
    if not slug:
        _warn("could not resolve workspace slug; skipping.")
        return 0

    # ---- Discover sessions ----
    transcripts_dir = Path(args.transcripts_dir)
    sessions = discover_sessions(transcripts_dir)
    if not sessions:
        print(
            f"no CI sessions found under {transcripts_dir}; nothing to do.",
            file=sys.stderr,
        )
        return 0

    redact = not args.no_redact

    if args.dry_run:
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

    # ---- Pre-flight checks ----
    if not shutil.which("gh"):
        _warn("gh CLI not found; skipping CI transcript push.")
        return 0
    if not shutil.which("git"):
        _warn("git not found; skipping CI transcript push.")
        return 0

    # ---- Clone hub & push ----
    repo_dir, err = ensure_hub_clone(hub_repo, Path(args.hub_cache), token)
    if err:
        _warn(f"hub clone failed: {err}")
        return 0

    plan = plan_copies(sessions, repo_dir, slug)
    if not plan:
        print("all CI sessions already published; nothing new.")
        return 0

    git_sha = current_git_sha()
    for src, dst_jsonl, dst_meta, session_id, parent_sid in plan:
        copy_with_redaction(src, dst_jsonl, redact=redact)
        write_meta(
            dst_meta, slug, session_id, git_sha,
            redacted=redact, parent_session_id=parent_sid,
        )

    ok, err = commit_and_push(repo_dir, slug, added=len(plan), token=token)
    if err:
        _warn(f"commit/push failed: {err}")
        return 0
    if not ok:
        print("nothing to commit (possible race); no push performed.")
        return 0

    summary = {
        "hub_repo": hub_repo,
        "workspace_slug": slug,
        "published": len(plan),
        "redacted": redact,
        "source": "ci",
    }
    sys.stdout.write(json.dumps(summary, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
