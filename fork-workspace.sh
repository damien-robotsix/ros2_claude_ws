#!/usr/bin/env bash
set -euo pipefail

# fork-workspace.sh — Create a customized fork of this ROS2 auto-tune workspace.
#
# Usage:
#   ./fork-workspace.sh /path/to/new-workspace
#   ./run.sh --fork /path/to/new-workspace   (alternative entry point)
#
# The script copies the template files, asks a series of questions to
# customize the new workspace, and initializes a fresh git repo.

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Helpers ──────────────────────────────────────────────────────────────

color() { printf '\033[%sm%s\033[0m' "$1" "$2"; }
info()  { echo "$(color '1;34' '→') $*"; }
ask()   {
    local prompt="$1" default="${2:-}"
    if [ -n "$default" ]; then
        printf "$(color '1;33' '?') %s [%s]: " "$prompt" "$default"
    else
        printf "$(color '1;33' '?') %s: " "$prompt"
    fi
    read -r REPLY || { echo ""; echo "Error: stdin closed (not running interactively?)"; exit 1; }
    if [ -z "$REPLY" ]; then REPLY="$default"; fi
}
ask_yn() {
    local prompt="$1" default="${2:-y}"
    while true; do
        printf "$(color '1;33' '?') %s [%s]: " "$prompt" "$default"
        read -r REPLY || { echo ""; echo "Error: stdin closed (not running interactively?)"; exit 1; }
        [ -z "$REPLY" ] && REPLY="$default"
        case "$REPLY" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
            *) echo "  Please answer y or n." ;;
        esac
    done
}

# ── Argument parsing ────────────────────────────────────────────────────

TARGET_DIR="${1:-}"
if [ -z "$TARGET_DIR" ]; then
    echo "Usage: $0 <target-directory>"
    echo ""
    echo "Creates a customized fork of the ROS2 auto-tune workspace at the given path."
    exit 1
fi

# Resolve to absolute path
TARGET_DIR="$(cd "$(dirname "$TARGET_DIR")" 2>/dev/null && pwd)/$(basename "$TARGET_DIR")" || {
    # Parent doesn't exist yet — try creating it
    TARGET_DIR="$(realpath -m "$TARGET_DIR")"
}

if [ -d "$TARGET_DIR" ] && [ "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
    echo "Error: $TARGET_DIR already exists and is not empty."
    exit 1
fi

echo ""
echo "$(color '1;36' '╔══════════════════════════════════════════════╗')"
echo "$(color '1;36' '║')  ROS2 Auto-Tune Workspace Fork Setup         $(color '1;36' '║')"
echo "$(color '1;36' '╚══════════════════════════════════════════════╝')"
echo ""
info "New workspace will be created at: $TARGET_DIR"
echo ""

# ── Gather configuration ────────────────────────────────────────────────

echo "$(color '1;35' '── Project basics ──')"
echo ""

DEFAULT_PROJECT_NAME="$(basename "$TARGET_DIR")"
ask "Project name (used in CLAUDE.md and docker-compose)" "$DEFAULT_PROJECT_NAME"
PROJECT_NAME="$REPLY"

ask "Short description of the project's purpose" "A self-improving Claude Code workspace for ROS2"
PROJECT_DESC="$REPLY"

ask "GitHub repo slug (owner/repo) — leave blank if no remote yet" ""
GITHUB_REPO="$REPLY"

ask "GitHub username (for workflow actor filters)" ""
GITHUB_USER="$REPLY"

echo ""
echo "$(color '1;35' '── ROS2 settings ──')"
echo ""

ask "Default ROS distro" "jazzy"
ROS_DISTRO="$REPLY"

echo ""
echo "$(color '1;35' '── Model preferences ──')"
echo ""
echo "  Available aliases: haiku, sonnet, opus"
echo "  Or use a full model ID (e.g. claude-sonnet-4-6)"
echo ""

ask "Default model for Claude Code sessions" "opus"
MODEL_CLAUDE_CODE="$REPLY"

ask "Model for code review" "opus"
MODEL_CODE_REVIEW="$REPLY"

ask "Model for auto-improve" "opus"
MODEL_AUTO_IMPROVE="$REPLY"

echo ""
echo "$(color '1;35' '── Features ──')"
echo ""

ENABLE_HUB=false
HUB_REPO=""
ENABLE_TRANSCRIPTS=false

if ask_yn "Enable hub integration (cross-workspace improvement sharing)?" "n"; then
    ENABLE_HUB=true
    ask "Hub repo slug (owner/repo)" "damien-robotsix/claude-auto-tune-hub"
    HUB_REPO="$REPLY"

    if ask_yn "Enable local transcript publishing to hub?" "n"; then
        ENABLE_TRANSCRIPTS=true
    fi
fi

ENABLE_AUTO_IMPROVE=true
if ! ask_yn "Enable auto-improve workflows (discover + verify)?" "y"; then
    ENABLE_AUTO_IMPROVE=false
fi

ENABLE_AGENT=true
if ! ask_yn "Enable autonomous Claude agent workflow?" "y"; then
    ENABLE_AGENT=false
fi

echo ""

# ── Confirmation ─────────────────────────────────────────────────────────

echo "$(color '1;35' '── Summary ──')"
echo ""
echo "  Project:       $PROJECT_NAME"
echo "  Description:   $PROJECT_DESC"
echo "  GitHub repo:   ${GITHUB_REPO:-<none>}"
echo "  GitHub user:   ${GITHUB_USER:-<none>}"
echo "  ROS distro:    $ROS_DISTRO"
echo "  Models:        code=$MODEL_CLAUDE_CODE  review=$MODEL_CODE_REVIEW  improve=$MODEL_AUTO_IMPROVE"
echo "  Hub:           $ENABLE_HUB"
echo "  Auto-improve:  $ENABLE_AUTO_IMPROVE"
echo "  Agent:         $ENABLE_AGENT"
echo ""

if ! ask_yn "Create workspace with these settings?" "y"; then
    echo "Aborted."
    exit 0
fi

echo ""

# ── Copy template files ─────────────────────────────────────────────────

info "Creating workspace directory..."
mkdir -p "$TARGET_DIR"

# Files/dirs to copy from the template
COPY_ITEMS=(
    Dockerfile
    docker-compose.yml
    entrypoint.sh
    run.sh
    .gitignore
    CLAUDE.md
    README.md
    fork-workspace.sh
    auto_tune_config.yml
    scripts
    docs
    .github
)

for item in "${COPY_ITEMS[@]}"; do
    src="$TEMPLATE_DIR/$item"
    if [ -e "$src" ]; then
        cp -r "$src" "$TARGET_DIR/"
    fi
done

# Create empty dirs that run.sh expects
mkdir -p "$TARGET_DIR/.claude-home/.claude"
mkdir -p "$TARGET_DIR/.claude-home/.config/gh"

info "Template files copied."

# ── Customize auto_tune_config.yml ──────────────────────────────────────

info "Customizing configuration..."

CONFIG="$TARGET_DIR/auto_tune_config.yml"
cat > "$CONFIG" << EOF
# auto_tune workspace configuration — $PROJECT_NAME
#
# Model aliases: use "haiku", "sonnet", or "opus" to automatically resolve
# to the latest pinned version of that model family.
# You may also specify a full model ID (e.g. "claude-sonnet-4-6") to pin
# to a specific version.

models:
  claude_code: "$MODEL_CLAUDE_CODE"
  code_review: "$MODEL_CODE_REVIEW"
  auto_improve: "$MODEL_AUTO_IMPROVE"
  auto_improve_verify: "$MODEL_AUTO_IMPROVE"
  claude_agent: "$MODEL_CLAUDE_CODE"

model_aliases:
  haiku: "claude-haiku-4-5-20251001"
  sonnet: "claude-sonnet-4-6"
  opus: "claude-opus-4-6"

auto_improve:
  default_conversation_limit: 20

hub:
  enabled: $ENABLE_HUB
  repo: "${HUB_REPO:-damien-robotsix/claude-auto-tune-hub}"
  local_transcripts:
    enabled: $ENABLE_TRANSCRIPTS
EOF

# ── Customize CLAUDE.md ─────────────────────────────────────────────────

CLAUDE_MD="$TARGET_DIR/CLAUDE.md"
# Replace the purpose section
python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    content = f.read()
content = re.sub(
    r'## Purpose\n\n.*?(?=\n## )',
    '## Purpose\n\n$PROJECT_DESC\n\n',
    content,
    flags=re.DOTALL
)
with open(sys.argv[1], 'w') as f:
    f.write(content)
" "$CLAUDE_MD"

# ── Customize README.md ─────────────────────────────────────────────────

README="$TARGET_DIR/README.md"
if [ -f "$README" ]; then
    python3 -c "
import sys
project_name = sys.argv[1]
project_desc = sys.argv[2]
github_repo  = sys.argv[3]

lines = []
lines.append('# ' + project_name)
lines.append('')
lines.append(project_desc)
lines.append('')
lines.append('## Quick start')
lines.append('')
lines.append('Local (Docker):')
lines.append('')
lines.append('\`\`\`bash')
lines.append('./run.sh')
lines.append('\`\`\`')
lines.append('')
lines.append('CI (GitHub Actions):')
lines.append('')
lines.append('\`\`\`bash')
lines.append('gh auth login -h github.com -s repo,workflow')
lines.append('# then, inside Claude Code CLI:')
lines.append('/install-github-app')
lines.append('\`\`\`')
lines.append('')
lines.append('Then mention \`@claude\` in any issue or PR comment.')
lines.append('')
lines.append('## Documentation')
lines.append('')
if github_repo:
    owner, repo = github_repo.split('/', 1)
    lines.append('Full documentation is published to GitHub Pages:')
    lines.append('')
    lines.append(f'**https://{owner}.github.io/{repo}/**')
    lines.append('')
lines.append('Source lives in [\`docs/\`](docs/):')
lines.append('')
lines.append('- [Quick start](docs/quickstart.md)')
lines.append('- [Configuration](docs/configuration.md)')
lines.append('- [Workflows](docs/workflows.md)')
lines.append('- [Architecture](docs/architecture.md)')
lines.append('')

with open(sys.argv[4], 'w') as f:
    f.write('\n'.join(lines))
" "$PROJECT_NAME" "$PROJECT_DESC" "$GITHUB_REPO" "$README"
    info "README.md customized."
fi

# ── Customize docker-compose.yml ────────────────────────────────────────

DC="$TARGET_DIR/docker-compose.yml"
# Update service name in run.sh
SANITIZED_NAME="$(echo "$PROJECT_NAME" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_-]/_/g')"
sed -i "s/ros2_claude_ws-claude/${SANITIZED_NAME}-claude/g" "$TARGET_DIR/run.sh" 2>/dev/null || true

# Update default ROS_DISTRO in docker-compose.yml
sed -i "s/ROS_DISTRO:-jazzy/ROS_DISTRO:-${ROS_DISTRO}/g" "$DC" 2>/dev/null || true

# ── Customize workflows (GitHub user) ───────────────────────────────────

if [ -n "$GITHUB_USER" ]; then
    # Update actor filter in claude-agent.yml
    AGENT_WF="$TARGET_DIR/.github/workflows/claude-agent.yml"
    if [ -f "$AGENT_WF" ]; then
        sed -i "s/damien-robotsix/$GITHUB_USER/g" "$AGENT_WF"
    fi
fi

# ── Remove disabled workflows ───────────────────────────────────────────

if [ "$ENABLE_AUTO_IMPROVE" = false ]; then
    rm -f "$TARGET_DIR/.github/workflows/auto-improve-discover.yml"
    rm -f "$TARGET_DIR/.github/workflows/auto-improve-verify.yml"
    rm -f "$TARGET_DIR/scripts/auto-improve-discover-prompt.md"
    rm -f "$TARGET_DIR/scripts/auto-improve-verify-prompt.md"
fi

if [ "$ENABLE_AGENT" = false ]; then
    rm -f "$TARGET_DIR/.github/workflows/claude-agent.yml"
fi

if [ "$ENABLE_HUB" = false ]; then
    rm -f "$TARGET_DIR/.github/workflows/hub-daily-sweep.yml"
    rm -f "$TARGET_DIR/.github/workflows/hub-sync.yml"
    rm -f "$TARGET_DIR/.github/workflows/hub-adopt.yml"
    rm -f "$TARGET_DIR/scripts/hub-daily-sweep-prompt.md"
    rm -f "$TARGET_DIR/scripts/hub-sync-prompt.md"
    rm -f "$TARGET_DIR/scripts/hub-adopt-prompt.md"
fi

# ── Initialize git repo ─────────────────────────────────────────────────

info "Initializing git repository..."
cd "$TARGET_DIR"
git init -q -b main

if [ -n "$GITHUB_REPO" ]; then
    git remote add origin "https://github.com/$GITHUB_REPO.git"
    info "Remote 'origin' set to https://github.com/$GITHUB_REPO.git"
fi

git add -A
git commit -q -m "Initial workspace setup from ROS2 auto-tune template

Project: $PROJECT_NAME
$([ -n "$GITHUB_REPO" ] && echo "Repo: $GITHUB_REPO")"

echo ""
echo "$(color '1;32' '✓') Workspace created at: $TARGET_DIR"
echo ""
echo "  Next steps:"
echo "    cd $TARGET_DIR"
[ -n "$GITHUB_REPO" ] && echo "    git push -u origin main     # Push to GitHub"
echo "    ./run.sh                    # Start a local Claude session"
echo "    ./run.sh --ros-distro humble  # Use a different ROS distro"
echo ""
echo "  $(color '1;35' 'GitHub App & secrets setup:')"
echo "    Workflow files are already configured from the template."
echo "    Inside a Claude Code session, run:"
echo ""
echo "      /install-github-app"
echo ""
echo "    When prompted, skip the workflow file configuration (already done)"
echo "    but complete the secrets setup (ANTHROPIC_API_KEY)."
echo ""
