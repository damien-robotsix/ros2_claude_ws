#!/usr/bin/env bash
# collect-doc-relevant-diff.sh
#
# Emit the commit list and unified diff for changes on the current branch
# since a cutoff (default: 24 hours ago), excluding paths that should not
# drive docs updates (docs/ itself, lockfiles, CI configs, etc.).
#
# Output goes to two files under .scratch/ inside the repo:
#   .scratch/doc-commits.txt  — one `git log` line per commit in window
#   .scratch/doc-diff.txt     — unified diff with excluded paths stripped
#
# Exit status:
#   0 — always (emptiness is checked by the caller, not via exit code).
#
# Usage:
#   scripts/collect-doc-relevant-diff.sh                # default: 24 hours
#   scripts/collect-doc-relevant-diff.sh "48 hours ago" # custom window

set -euo pipefail

SINCE="${1:-24 hours ago}"
OUT_DIR=".scratch"
mkdir -p "$OUT_DIR"

COMMITS_FILE="$OUT_DIR/doc-commits.txt"
DIFF_FILE="$OUT_DIR/doc-diff.txt"
: > "$COMMITS_FILE"
: > "$DIFF_FILE"

# Find the oldest commit on HEAD whose author date is within the window.
# If none, there's nothing to report.
BASE=$(git log --since="$SINCE" --reverse --format=%H HEAD | head -n 1 || true)

if [ -z "$BASE" ]; then
  echo "No commits on HEAD since '$SINCE'."
  echo ">>> Window commits: 0"
  exit 0
fi

# Determine the diff range. Use the parent of the oldest in-window commit as
# the base. If BASE is the repository root (no parent), fall back to the
# empty tree.
if git rev-parse --verify --quiet "${BASE}^" >/dev/null; then
  RANGE_BASE="${BASE}^"
else
  RANGE_BASE=$(git hash-object -t tree /dev/null)  # empty tree SHA
fi

# Commit list (one line per commit, oldest-first).
git log --reverse --format='%h  %ci  %s' "${RANGE_BASE}..HEAD" > "$COMMITS_FILE"

# Exclusion pathspecs. Keep this list narrow — only exclude things that
# demonstrably should not drive a docs update.
EXCLUDES=(
  ':!docs/**'                 # the agent's own output target
  ':!.github/**'              # workflow edits cannot be pushed by the agent anyway
  ':!**/*.lock'
  ':!**/*.lockb'
  ':!**/package-lock.json'
  ':!**/pnpm-lock.yaml'
  ':!**/yarn.lock'
  ':!**/Cargo.lock'
  ':!**/poetry.lock'
  ':!**/uv.lock'
  ':!.gitignore'
)

git diff "${RANGE_BASE}..HEAD" -- . "${EXCLUDES[@]}" > "$DIFF_FILE"

COMMIT_COUNT=$(wc -l < "$COMMITS_FILE" | tr -d ' ')
DIFF_BYTES=$(wc -c < "$DIFF_FILE" | tr -d ' ')
echo ">>> Window commits: $COMMIT_COUNT"
echo ">>> Diff bytes (post-exclude): $DIFF_BYTES"
echo ">>> Commits file: $COMMITS_FILE"
echo ">>> Diff file:    $DIFF_FILE"
