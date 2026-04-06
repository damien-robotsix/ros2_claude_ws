#!/bin/bash
set -e

# Ensure required directories exist in the bind-mounted home
mkdir -p "$HOME/.claude" "$HOME/.config/gh"

# Bootstrap .bashrc if the host volume doesn't have one yet
if [ ! -f "$HOME/.bashrc" ] && [ -f /tmp/.bashrc.default ]; then
    cp /tmp/.bashrc.default "$HOME/.bashrc"
fi

# Install workspace-specific ROS dependencies via rosdep.
# This keeps the Dockerfile generic while each workspace gets its deps
# resolved automatically from the packages in src/.
if [ -d /workspace/src ]; then
    echo "Installing workspace dependencies via rosdep..."
    source /opt/ros/${ROS_DISTRO}/setup.bash
    sudo apt-get update -qq
    rosdep install --from-paths /workspace/src --ignore-src -y -q 2>/dev/null || true
    sudo rm -rf /var/lib/apt/lists/*
fi

exec claude "$@"
