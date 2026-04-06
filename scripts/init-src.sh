#!/usr/bin/env bash
# Initialize the src/ folder from repos.yaml using vcstool,
# install ROS2 dependencies, and optionally build the workspace.
#
# Usage: ./init-src.sh [--no-build]
#   --no-build  Skip the colcon build step (fetch repos + install deps only)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_DIR="$(dirname "$SCRIPT_DIR")"
DISTRO="${ROS_DISTRO:-jazzy}"

BUILD=true
for arg in "$@"; do
  case "$arg" in
    --no-build) BUILD=false ;;
  esac
done

# Source ROS2 base (temporarily relax nounset — ROS setup scripts use unset vars)
if [ -f "/opt/ros/${DISTRO}/setup.bash" ]; then
  set +u
  source "/opt/ros/${DISTRO}/setup.bash"
  set -u
else
  echo "ERROR: /opt/ros/${DISTRO}/setup.bash not found" >&2
  exit 1
fi

# Clone / update repos
mkdir -p "$WS_DIR/src"
vcs import --skip-existing "$WS_DIR/src" < "$WS_DIR/repos.yaml"
vcs pull "$WS_DIR/src"

# Install ROS2 package dependencies
if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then
  sudo rosdep init
fi
rosdep update --rosdistro "$DISTRO"
if ! rosdep install --from-paths "$WS_DIR/src" --ignore-src -y --rosdistro "$DISTRO"; then
  echo "WARNING: rosdep install failed (sudo may be required). Missing packages:" >&2
  rosdep install --from-paths "$WS_DIR/src" --ignore-src --simulate --rosdistro "$DISTRO" 2>&1 | grep "sudo" >&2
  echo "Install them manually and re-run this script." >&2
  exit 1
fi

# Build
if [ "$BUILD" = true ]; then
  cd "$WS_DIR"
  colcon build --symlink-install
fi
