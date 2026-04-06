#!/bin/bash
set -e

# Ensure required directories exist in the bind-mounted home
mkdir -p "$HOME/.claude" "$HOME/.config/gh"

# Bootstrap .bashrc if the host volume doesn't have one yet
if [ ! -f "$HOME/.bashrc" ] && [ -f /tmp/.bashrc.default ]; then
    cp /tmp/.bashrc.default "$HOME/.bashrc"
fi

exec claude "$@"
