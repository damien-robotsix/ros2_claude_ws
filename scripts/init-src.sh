#!/usr/bin/env bash
# Initialize the src/ folder from repos.yaml using vcstool,
# install ROS2 dependencies, and build the workspace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_DIR="$(dirname "$SCRIPT_DIR")"

# Source ROS2 base (ROS_DISTRO is set by the Docker image)
source "/opt/ros/${ROS_DISTRO:-jazzy}/setup.bash"

# Clone / update repos
mkdir -p "$WS_DIR/src"
vcs import "$WS_DIR/src" < "$WS_DIR/repos.yaml"
vcs pull "$WS_DIR/src"

# Install ROS2 package dependencies
rosdep install --from-paths "$WS_DIR/src" --ignore-src -y --rosdistro "${ROS_DISTRO:-jazzy}" || true

# Build
cd "$WS_DIR"
colcon build --symlink-install
