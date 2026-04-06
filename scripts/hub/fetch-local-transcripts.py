#!/usr/bin/env python3
"""
Deterministic fetcher for local Claude Code session transcripts stored in
the shared hub repo.

This is the **pull half** of the local-transcript lane (the publish half is
``scripts/hub/push-local-transcripts.py``). It is run by CI workflows
(``auto-improve-discover.yml`` and ``auto-improve-verify.yml``) before they
invoke Claude, so the ``workflow-insights-extractor`` subagent can fold
transcripts captured from **local runs** into the same clustering pass that
already consumes CI-side transcripts.

Layout read from the hub
------------------------
::

    transcripts/
      <workspace-slug>/
        <YYYY-MM-DD>/
          <session-id>.jsonl
          <session-id>.meta.json   (ignored by this script)

``<workspace-slug>`` is the current workspace's own slug — resolved from
``$GITHUB_REPOSITORY`` or the ``origin`` git remote. Own-slug only: a
workspace never reads transcripts published by another fork, matching the
default documented in ``claude-auto-tune-hub``'s README.

Layout written locally
----------------------
::

    <dest-dir>/
      <YYYY-MM-DD>/
        <session-id>.jsonl

Files are copied as-is (no redaction, no filtering) since redaction was
already applied on the publish side. ``.meta.json`` sidecars are skipped:
they don't feed the parser and keeping them would require extending the
subagent to ignore them.

Design invariants
-----------------
- **No LLM calls.** Pure ``gh`` + ``git`` + file copy.
- **Silent no-op when not opted in.** If ``hub.enabled`` or
  ``hub.local_transcripts.enabled`` is false/unset in
  ``auto_tune_config.yml``, the script exits 0 without touching anything.
  If ``HUB_TOKEN`` is not set in the environment, the script exits 0
  with a diagnostic — CI steps can call this unconditionally because a
  fork without the secret provisioned behaves exactly like one with the
  lane disabled.
- **Best-effort on failure.** Clone / fetch errors print a warning and
  exit 0. The extractor must work with an empty fetch: the whole pull
  lane is additive and must never fail a workflow.
- **Own-slug only.** Reads ``transcripts/<own-slug>/`` and nothing else.
- **Works inside the CI sandbox.** All writes land under the current
  working directory (no ``/tmp``). The hub clone goes under a caller-
  supplied cache dir (defaults to ``.scratch/hub-cache``) and the copied
  transcripts go under ``.scratch/hub-transcripts/`` by default. Both are
  inside the workspace checkout, which is where the CI sandbox allows
  writes.

Authentication
--------------
GitHub auth is read from ``HUB_TOKEN`` (a fork-provisioned fine-
grained PAT with ``contents: read`` on the hub repo). The token is
exported as ``GH_TOKEN`` only for the subprocess calls made by this
script, so it does not leak into the Claude step that runs after this
one. The token is **never** written to disk: we use ``gh repo clone``
which inherits ``GH_TOKEN`` and rewrites the origin URL without an
embedded credential.

Usage
-----
::

    HUB_TOKEN=ghp_xxx python3 scripts/hub/fetch-local-transcripts.py
    python3 scripts/hub/fetch-local-transcripts.py --dry-run
    python3 scripts/hub/fetch-local-transcripts.py \\
        --dest .scratch/hub-transcripts \\
        --hub-cache .scratch/hub-cache \\
        --max-age-days 30

Exit codes
----------
Always 0. This script never blocks a CI workflow: warnings go to stderr
and the hub-transcript directory is simply left empty when anything goes
wrong. Callers that want to tell "nothing fetched" from "fetched N" can
inspect the final JSON summary on stdout.
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

DEFAULT_CONFIG_PATH = Path("auto_tune_config.yml")
DEFAULT_DEST = Path(".scratch/hub-transcripts")
DEFAULT_HUB_CACHE = Path(".scratch/hub-cache")


# ----- Config loading ------------------------------------------------


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore

        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
    except ImportError:
        # Minimal scan that understands only the two booleans + repo slug
        # we need — avoids a hard PyYAML dependency on CI images that do
        # not preinstall it.
        return _minimal_yaml_scan(path)
    if not isinstance(data, dict):
        return {}
    return data


def _minimal_yaml_scan(path: Path) -> dict:
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
                    if key == "local_transcripts":
                        current = "hub.local_transcripts"
                        hub["local_transcripts"] = sub
                        continue
                    hub[key] = _coerce(value)
                elif line and not line.startswith(" "):
                    break
            elif current == "hub.local_transcripts":
                if line.startswith("    "):
                    key, _, value = line.strip().partition(":")
                    sub[key] = _coerce(value.strip())
                elif line.startswith("  "):
                    key, _, value = line.strip().partition(":")
                    current = "hub"
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
    rc, out, _ = _run(["git", "remote", "get-url", "origin"])
    if rc != 0:
        return None
    url = out.strip()
    m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return m.group(1)


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

    Uses ``gh repo clone`` under a scoped environment that only this
    subprocess sees, so ``GH_TOKEN`` never leaks into later workflow
    steps. Returns ``(repo_dir, error)``.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = cache_dir / "repo"
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    # Force gh to use the PAT, not any ambient OAuth state.
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


# ----- Copy pipeline -------------------------------------------------


_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iter_transcripts(
    repo_dir: Path, slug: str, max_age_days: int | None
) -> list[Path]:
    base = repo_dir / "transcripts" / slug
    if not base.exists():
        return []
    cutoff: _dt.date | None = None
    if max_age_days is not None and max_age_days > 0:
        cutoff = (
            _dt.datetime.now(_dt.timezone.utc).date()
            - _dt.timedelta(days=max_age_days)
        )
    out: list[Path] = []
    for date_dir in sorted(base.iterdir()):
        if not date_dir.is_dir() or not _DATE_DIR_RE.match(date_dir.name):
            continue
        if cutoff is not None:
            try:
                date = _dt.date.fromisoformat(date_dir.name)
            except ValueError:
                continue
            if date < cutoff:
                continue
        for jf in sorted(date_dir.glob("*.jsonl")):
            if jf.is_file():
                out.append(jf)
    return out


def copy_transcripts(sources: list[Path], base_src: Path, dest: Path) -> int:
    """Copy ``sources`` into ``dest`` preserving the ``<YYYY-MM-DD>/``
    parent directory so the parser's recursive glob still sees them.
    Returns the number of files copied."""
    count = 0
    for src in sources:
        rel = src.relative_to(base_src)  # e.g. 2026-04-05/<session>.jsonl
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        count += 1
    return count


# ----- Main ----------------------------------------------------------


def _warn(msg: str, *, ci_warning: bool = False) -> None:
    """Print a warning to stderr.

    When ``ci_warning`` is True **and** we are running inside GitHub
    Actions (``GITHUB_ACTIONS=true``), also emit a ``::warning::``
    workflow command so the message surfaces in the workflow summary UI
    instead of being buried in the log.
    """
    print(f"fetch-local-transcripts: {msg}", file=sys.stderr)
    if ci_warning and os.environ.get("GITHUB_ACTIONS") == "true":
        # Workflow command — GitHub renders this as a yellow annotation
        # on the step and in the job summary.
        print(f"::warning::fetch-local-transcripts: {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch local Claude Code session transcripts for the current "
            "workspace slug from the shared hub repo into a local "
            "directory that the workflow-insights-extractor can parse."
        )
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="path to auto_tune_config.yml (default: %(default)s)",
    )
    parser.add_argument(
        "--dest",
        default=str(DEFAULT_DEST),
        help=(
            "local directory to populate with hub transcripts "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--hub-cache",
        default=str(DEFAULT_HUB_CACHE),
        help="local cache for the hub clone (default: %(default)s)",
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
        "--max-age-days",
        type=int,
        default=30,
        help=(
            "skip date subdirectories older than this many days. Pass 0 "
            "to disable the filter. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve config + slug and exit without cloning or copying.",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    hub_config = config.get("hub") or {}
    lt_config = (
        (hub_config.get("local_transcripts") or {})
        if isinstance(hub_config, dict)
        else {}
    )

    if not hub_config.get("enabled"):
        _warn("hub.enabled is false; skipping hub transcript fetch.")
        return 0
    if not lt_config.get("enabled"):
        _warn(
            "hub.local_transcripts.enabled is false or unset; skipping "
            "hub transcript fetch."
        )
        return 0

    hub_repo = args.hub_repo or hub_config.get("repo")
    if not hub_repo:
        _warn("hub.repo missing from config; skipping.")
        return 0

    slug = resolve_workspace_slug()
    if not slug:
        _warn(
            "could not resolve workspace slug from $GITHUB_REPOSITORY or "
            "origin remote; skipping."
        )
        return 0

    token = os.environ.get("HUB_TOKEN", "").strip()
    if not token:
        _warn(
            "HUB_TOKEN env var is not set; skipping hub transcript "
            "fetch (this is expected on forks that have not provisioned "
            "the secret)."
        )
        return 0

    dest = Path(args.dest)
    cache_dir = Path(args.hub_cache)

    if args.dry_run:
        print(f"hub_repo: {hub_repo}")
        print(f"workspace_slug: {slug}")
        print(f"dest: {dest}")
        print(f"hub_cache: {cache_dir}")
        print(f"max_age_days: {args.max_age_days}")
        return 0

    if not shutil.which("gh"):
        _warn("gh CLI not found on PATH; skipping.", ci_warning=True)
        return 0
    if not shutil.which("git"):
        _warn("git not found on PATH; skipping.", ci_warning=True)
        return 0

    repo_dir, err = ensure_hub_clone(hub_repo, cache_dir, token)
    if err:
        _warn(err, ci_warning=True)
        return 0

    base_src = repo_dir / "transcripts" / slug
    sources = iter_transcripts(
        repo_dir, slug, max_age_days=args.max_age_days or None
    )

    # Always reset the destination so re-runs are hermetic. The parser
    # walks the directory recursively, so a stale file left over from a
    # previous run would otherwise be double-counted.
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    copied = copy_transcripts(sources, base_src, dest) if sources else 0

    summary = {
        "hub_repo": hub_repo,
        "workspace_slug": slug,
        "dest": str(dest),
        "transcripts_fetched": copied,
        "max_age_days": args.max_age_days,
    }
    sys.stdout.write(json.dumps(summary, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
