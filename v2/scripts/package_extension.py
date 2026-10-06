#!/usr/bin/env python3
"""Part 6C-B: build (and validate) the pilot Chrome extension release ZIP.

Deterministic, offline, no third-party dependency - delegates all logic to
stock_research_ai.release_tools (unit-tested in tests/test_release_tools.py). This
script is a thin CLI wrapper only.

Usage:
    uv run python scripts/package_extension.py --output release/extension
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_research_ai.release_tools import (  # noqa: E402
    build_extension_zip,
    extension_version,
    validate_extension_zip,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extension-dir", type=Path, default=REPO_ROOT / "extension",
        help="Source extension directory (default: repo's extension/)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "release" / "extension",
        help="Directory to write the release ZIP into (default: release/extension/)",
    )
    args = parser.parse_args()

    version = extension_version(args.extension_dir)
    output_zip = args.output_dir / f"stock-research-ai-extension-{version}.zip"
    build_extension_zip(args.extension_dir, output_zip)

    problems = validate_extension_zip(output_zip)
    if problems:
        print("Extension package validation FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(output_zip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
