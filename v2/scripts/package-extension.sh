#!/usr/bin/env bash
# Part 6C-B: build only the Chrome extension release ZIP (no backend wheel, no full
# release bundle). Thin wrapper - logic lives in scripts/package_extension.py /
# stock_research_ai.release_tools, unit-tested in tests/test_release_tools.py.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
  exec uv run python scripts/package_extension.py "$@"
else
  exec python3 scripts/package_extension.py "$@"
fi
