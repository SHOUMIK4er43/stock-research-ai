#!/usr/bin/env bash
# Part 6C-B: assemble the complete pilot release bundle (backend wheel, extension ZIP,
# install/uninstall scripts, RELEASE-MANIFEST.json, SHA256SUMS). Thin wrapper - all
# deterministic logic lives in scripts/build_release.py / stock_research_ai.release_tools
# so it stays unit-testable in pytest. No live network calls are made by this script.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is required to build a release (https://astral.sh/uv). Install it first." >&2
  exit 1
fi

exec uv run python scripts/build_release.py "$@"
