#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Fork mode: create a customized workspace copy
if [ "${1:-}" = "--fork" ]; then
    shift
    exec "$SCRIPT_DIR/fork-workspace.sh" "$@"
fi

# Parse --ros-distro flag (must come before any Claude args)
ROS_DISTRO="${ROS_DISTRO:-jazzy}"
if [ "${1:-}" = "--ros-distro" ]; then
    ROS_DISTRO="$2"
    shift 2
fi
export ROS_DISTRO

# Build if needed
docker compose build

# Ensure host dirs/files exist for bind mounts
mkdir -p "$SCRIPT_DIR/.claude-home/.claude" "$SCRIPT_DIR/.claude-home/.config/gh"
[ -f "$SCRIPT_DIR/.claude-home/.claude.json" ] || touch "$SCRIPT_DIR/.claude-home/.claude.json"

# Run with explicit interactive TTY allocation. `|| true` so a
# non-zero exit from the container (Ctrl-C, Claude Code error) still
# falls through to the post-run transcript sync below.
docker run -it --rm \
    --network host \
    -v "$SCRIPT_DIR:/workspace" \
    -v "$SCRIPT_DIR/.claude-home/.claude:/home/claude/.claude" \
    -v "$SCRIPT_DIR/.claude-home/.claude.json:/home/claude/.claude.json" \
    -v "$SCRIPT_DIR/.claude-home/.config/gh:/home/claude/.config/gh" \
    -w /workspace \
    ros2_claude_ws-claude \
    --dangerously-skip-permissions "$@" || true

# Post-run: publish local Claude Code session transcripts to the hub
# if the local-transcripts lane is enabled in auto_tune_config.yml.
# The script is a hard no-op when the flag is off, so this runs
# unconditionally — the config file is the single source of truth.
# Failures here (no network, missing git credentials, hub
# unreachable) are logged but never fail the shell, because a failed
# transcript sync must not mask the exit status of the actual Claude
# Code session.
if command -v python3 >/dev/null 2>&1; then
    python3 "$SCRIPT_DIR/scripts/hub/push-local-transcripts.py" \
        || echo "run.sh: hub transcript sync skipped or failed (non-fatal)" >&2
fi
