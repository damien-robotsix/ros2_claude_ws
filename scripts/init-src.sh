#!/usr/bin/env bash
# Initialize the src/ folder from repos.yaml using vcstool
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS_DIR="$(dirname "$SCRIPT_DIR")"

mkdir -p "$WS_DIR/src"
vcs import "$WS_DIR/src" < "$WS_DIR/repos.yaml"
vcs pull "$WS_DIR/src"
