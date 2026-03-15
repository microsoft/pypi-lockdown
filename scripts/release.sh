#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 TAG"
    echo ""
    echo "Build cross-platform .pyz artifacts and create a GitHub release."
    echo ""
    echo "  TAG   Git tag to release (e.g. v1.0.0). Must already exist."
    echo ""
    echo "Environment variables:"
    echo "  PIP_ARGS   Extra pip arguments passed to the build (e.g. '--find-links /path/to/wheels')"
    exit 1
}

[[ $# -eq 1 ]] || usage
TAG="$1"

# Validate tag exists
if ! git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "Error: tag '$TAG' does not exist. Create it first:"
    echo "  git tag $TAG"
    exit 1
fi

# Validate gh CLI
if ! command -v gh >/dev/null 2>&1; then
    echo "Error: gh CLI not found. Install it from https://cli.github.com/"
    exit 1
fi

# Validate tox
if ! command -v tox >/dev/null 2>&1; then
    echo "Error: tox not found. Install with: pip install 'pypi-lockdown[build]'"
    exit 1
fi

echo "=== Building .pyz artifacts for $TAG ==="
echo ""

# Clean previous build
rm -rf dist/*.pyz

# Build all platforms, forwarding PIP_ARGS if set
if [[ -n "${PIP_ARGS:-}" ]]; then
    tox -e standalone -- all -- $PIP_ARGS
else
    tox -e standalone
fi

# List artifacts
echo ""
echo "=== Artifacts ==="
ls -lh dist/*.pyz
echo ""

# Create GitHub release
echo "=== Creating GitHub release $TAG ==="
gh release create "$TAG" dist/*.pyz \
    --title "$TAG" \
    --generate-notes

echo ""
echo "Done. Release $TAG created."
