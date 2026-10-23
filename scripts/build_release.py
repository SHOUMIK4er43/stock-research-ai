#!/usr/bin/env python3
"""Part 6C-B: assemble the complete pilot release bundle.

    release/
        backend/
            stock_research_ai-<version>-py3-none-any.whl
            stock_research_ai-<version>.tar.gz   (if produced)
        extension/
            stock-research-ai-extension-<version>.zip
        install-macos.sh
        uninstall-macos.sh
        RELEASE-MANIFEST.json
        SHA256SUMS
        getting-started.md
        privacy-disclosure.md
        v1-user-guide.md

No live network calls are made by this script itself. Building the Python wheel via
`uv build` uses only already-resolved/cached dependencies (uv.lock); if the local `uv`
cache is cold, `uv build` may need network the same way `uv sync` would - that is an
environment/tooling concern, not something this script disguises (see Part 6C-B
instructions, "no live network" - normal release/CI process installs declared build
tooling beforehand, exactly like every other command in this project already assumes
`uv sync`/`uv run` has been set up once).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from stock_research_ai.release_tools import (  # noqa: E402
    application_version,
    build_extension_zip,
    build_release_manifest,
    extension_version,
    validate_extension_zip,
    write_release_manifest,
    write_sha256sums,
)


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _run_offline_checks() -> None:
    print("Running offline pre-release checks (pytest, ruff, mypy)...")
    for command in (
        ["uv", "run", "pytest", "-q"],
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "mypy", "src"],
    ):
        subprocess.run(command, cwd=REPO_ROOT, check=True)


def build_backend_wheel(output_dir: Path) -> list[Path]:
    """Runs `uv build`, which also writes its own `.gitignore` into --out-dir - that
    file is real (uv-generated, harmless) but is not a distributable artifact, so it
    is deliberately excluded here rather than swept up by a bare glob (a real defect
    caught during this slice's own offline release-build proof: an earlier version of
    this function hashed and published that .gitignore as if it were a release
    artifact)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["uv", "build", "--out-dir", str(output_dir)], cwd=REPO_ROOT, check=True
    )
    allowed_suffixes = (".whl", ".tar.gz")
    artifacts = sorted(
        p for p in output_dir.glob("*") if any(p.name.endswith(s) for s in allowed_suffixes)
    )
    if not artifacts:
        raise RuntimeError(f"`uv build` produced no wheel/sdist artifacts in {output_dir}")
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "release")
    parser.add_argument(
        "--skip-tests", action="store_true", help="Skip the pytest/ruff/mypy pre-build gate"
    )
    args = parser.parse_args()

    if not args.skip_tests:
        _run_offline_checks()

    release_dir: Path = args.output_dir
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)

    backend_dir = release_dir / "backend"
    wheel_paths = build_backend_wheel(backend_dir)

    extension_dir = release_dir / "extension"
    ext_version = extension_version(REPO_ROOT / "extension")
    extension_zip = extension_dir / f"stock-research-ai-extension-{ext_version}.zip"
    build_extension_zip(REPO_ROOT / "extension", extension_zip)
    problems = validate_extension_zip(extension_zip)
    if problems:
        print("Extension package validation FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    for script_name in ("install-macos.sh", "uninstall-macos.sh"):
        source = REPO_ROOT / "scripts" / script_name
        destination = release_dir / script_name
        shutil.copy2(source, destination)
        destination.chmod(0o755)

    for doc_name in ("getting-started.md", "privacy-disclosure.md", "v1-user-guide.md"):
        source = REPO_ROOT / "docs" / doc_name
        if source.is_file():
            shutil.copy2(source, release_dir / doc_name)

    backend_version = application_version()
    artifact_paths = [*wheel_paths, extension_zip]
    artifact_hashes = {}
    from stock_research_ai.release_tools import sha256_of_file

    for path in artifact_paths:
        artifact_hashes[str(path.relative_to(release_dir).as_posix())] = sha256_of_file(path)

    manifest = build_release_manifest(
        backend_version=backend_version,
        extension_version_value=ext_version,
        artifact_hashes=artifact_hashes,
        git_commit=_git_commit(),
        minimum_macos_version="13.0",
    )
    write_release_manifest(manifest, release_dir / "RELEASE-MANIFEST.json")

    checksum_targets = [*artifact_paths, release_dir / "RELEASE-MANIFEST.json"]
    write_sha256sums(checksum_targets, release_dir / "SHA256SUMS", root=release_dir)

    print(f"Release bundle assembled at {release_dir}")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
