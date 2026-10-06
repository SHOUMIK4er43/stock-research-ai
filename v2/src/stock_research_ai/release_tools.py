"""Part 6C-B: deterministic, offline-testable release/packaging helpers.

This module contains no research/financial/valuation semantics whatsoever - it exists
purely to build and validate the pilot release bundle (checksums, the machine-readable
release manifest, the extension ZIP, and the LaunchAgent plist). It is shipped inside
the installed package (consistent with backup.py/diagnostics.py/logging_setup.py from
Part 6C-A) purely so it is importable, type-checked, and unit-testable the same way -
it is not itself part of the running research/browser product.

Nothing here performs network I/O, and nothing here ever handles a secret value.
"""

import hashlib
import json
import plistlib
import zipfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stock_research_ai import __version__
from stock_research_ai.adapters.http.browser_schemas import API_VERSION

#: The stable reverse-domain-style LaunchAgent label used consistently across the
#: plist filename, launchctl commands, installer, uninstaller, and docs.
LAUNCH_AGENT_LABEL = "ai.stockresearch.local"

#: Files/directories a production extension release ZIP must never contain - these are
#: development-only, test-only, or version-control artifacts.
EXTENSION_ZIP_EXCLUDE_NAMES = frozenset(
    {
        "tests",
        "node_modules",
        ".git",
        ".gitignore",
        "package.json",
        "package-lock.json",
        ".DS_Store",
    }
)

#: Files that must exist at the root of a valid extension release ZIP.
EXTENSION_REQUIRED_FILES = (
    "manifest.json",
    "sidepanel.html",
    "src/background.js",
    "src/sidepanel.js",
)

#: Icon files a complete extension release ZIP must include (Chrome's common sizes).
EXTENSION_REQUIRED_ICONS = (
    "icons/icon16.png",
    "icons/icon32.png",
    "icons/icon48.png",
    "icons/icon128.png",
)

#: Unambiguous secret-VALUE indicators: a real Anthropic key prefix, or the presence
#: of an actual `.env` file. These can never legitimately appear in extension source
#: code, a plist, or a release manifest, so they are safe to scan for everywhere.
FORBIDDEN_SECRET_MARKERS = ("sk-ant-", ".env")

#: "Authorization:"/"Bearer " are legitimate, expected substrings inside the
#: extension's own client code (it correctly builds an `Authorization: Bearer <token>`
#: header - see extension/src/api/client.js) - flagging them there would be exactly
#: the same class of false positive as flagging the word "Secretary" for containing
#: "secret" (see docs/part6b-c-proof.md). They are only meaningful as a leak indicator
#: in artifacts that should never carry live request/response traffic at all: a
#: rendered LaunchAgent plist or a release manifest. Scanned there, never against
#: general source code or the extension ZIP.
FORBIDDEN_RUNTIME_TRAFFIC_MARKERS = ("Authorization:", "Bearer ")


class ReleaseValidationError(ValueError):
    """A release artifact failed a safety/structural check."""


def application_version() -> str:
    """The single authoritative backend version - identical to what Part 6C-A's
    diagnostics/`/v1/version` already report, re-exposed here so release tooling never
    hand-duplicates it."""
    return __version__


def read_extension_manifest(extension_dir: Path) -> dict[str, Any]:
    manifest_path = extension_dir / "manifest.json"
    data: dict[str, Any] = json.loads(manifest_path.read_text())
    return data


def extension_version(extension_dir: Path) -> str:
    manifest = read_extension_manifest(extension_dir)
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseValidationError(f"{extension_dir}/manifest.json has no valid 'version'")
    return version


# --- Checksums ------------------------------------------------------------------------


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sha256sums(artifact_paths: Sequence[Path], output_path: Path, root: Path) -> None:
    """Writes a `shasum -a 256`-compatible SHA256SUMS file: one `<hex>  <relative path>`
    line per artifact, sorted by relative path for determinism. `root` is the directory
    the recorded paths are made relative to (the release directory itself) - the
    checksum file never records an absolute local path."""
    lines = []
    for path in artifact_paths:
        relative = path.relative_to(root)
        lines.append(f"{sha256_of_file(path)}  {relative.as_posix()}")
    lines.sort(key=lambda line: line.split("  ", 1)[1])
    output_path.write_text("\n".join(lines) + "\n")


# --- Release manifest -------------------------------------------------------------------


def build_release_manifest(
    *,
    backend_version: str,
    extension_version_value: str,
    artifact_hashes: dict[str, str],
    git_commit: str | None,
    platform_target: str = "macOS (pilot)",
    minimum_macos_version: str | None = None,
    python_runtime_strategy: str = "uv tool install (managed Python runtime; uv must be present)",
    built_at: datetime | None = None,
) -> dict[str, Any]:
    """Returns the exact RELEASE-MANIFEST.json content. Never accepts or embeds an API
    key, browser pairing token, machine username, absolute local path, or any DB
    metadata - the caller only ever passes already-public, non-secret values."""
    timestamp = (built_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    return {
        "product": "Stock Research AI",
        "backend_version": backend_version,
        "extension_version": extension_version_value,
        "api_version": API_VERSION,
        "research_plan_version": "FULL_RESEARCH_V1",
        "built_at_utc": timestamp,
        "git_commit": git_commit,
        "artifacts": dict(sorted(artifact_hashes.items())),
        "platform_target": platform_target,
        "minimum_macos_version": minimum_macos_version,
        "python_runtime_strategy": python_runtime_strategy,
    }


def write_release_manifest(manifest: dict[str, Any], output_path: Path) -> None:
    output_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n")


# --- Extension packaging ----------------------------------------------------------------


def _iter_extension_files(extension_dir: Path) -> Iterable[Path]:
    for path in sorted(extension_dir.rglob("*")):
        if path.is_dir():
            continue
        relative_parts = path.relative_to(extension_dir).parts
        if any(part in EXTENSION_ZIP_EXCLUDE_NAMES for part in relative_parts):
            continue
        yield path


def build_extension_zip(extension_dir: Path, output_zip: Path) -> None:
    """Zips the extension directory's production contents at the ZIP root (never
    nested under an `extension/` path) - excludes tests, node_modules, .git,
    package.json, and OS cruft. Deterministic: files are added in sorted relative-path
    order and with a fixed archive date, so two builds from identical source content
    produce byte-identical ZIPs (build timestamps live only in RELEASE-MANIFEST.json,
    never inside the ZIP itself)."""
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in _iter_extension_files(extension_dir):
            relative = path.relative_to(extension_dir).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def validate_extension_zip(zip_path: Path) -> list[str]:
    """Returns a list of problems (empty means valid)."""
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        name_set = set(names)

        if "manifest.json" not in name_set:
            problems.append("manifest.json missing from ZIP root")
        else:
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("manifest_version") != 3:
                problems.append("manifest_version is not 3")
            permissions = set(manifest.get("permissions", []))
            if permissions - {"storage", "sidePanel"}:
                problems.append(f"unexpected permissions: {sorted(permissions)}")
            host_permissions = set(manifest.get("host_permissions", []))
            allowed_hosts = {"https://www.screener.in/*", "http://127.0.0.1/*"}
            if host_permissions - allowed_hosts:
                problems.append(f"unexpected host_permissions: {sorted(host_permissions)}")
            if "icons" not in manifest:
                problems.append("manifest.json has no 'icons' entry")

        for required in EXTENSION_REQUIRED_FILES:
            if required not in name_set:
                problems.append(f"required file missing: {required}")
        for icon in EXTENSION_REQUIRED_ICONS:
            if icon not in name_set:
                problems.append(f"required icon missing: {icon}")

        for name in names:
            top_or_any_part = name.split("/")
            if any(part in EXTENSION_ZIP_EXCLUDE_NAMES for part in top_or_any_part):
                problems.append(f"forbidden path present in ZIP: {name}")
            lowered = name.lower()
            if lowered.endswith(".env") or "secret" in lowered or "credential" in lowered:
                problems.append(f"suspicious path present in ZIP: {name}")

        for name in names:
            content = archive.read(name)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for marker in FORBIDDEN_SECRET_MARKERS:
                if marker in text:
                    problems.append(f"forbidden marker {marker!r} found in {name}")
    return problems


# --- LaunchAgent plist --------------------------------------------------------------------


def render_launch_agent_plist(
    *,
    label: str,
    program_arguments: Sequence[str],
    stdout_path: Path,
    stderr_path: Path,
    working_directory: Path | None = None,
    keep_alive: bool = True,
    run_at_load: bool = True,
) -> bytes:
    """Returns a well-formed plist (bytes) via stdlib `plistlib` - never a hand-written
    XML string - so structural validity (and macOS's own `plutil -lint`) is guaranteed
    by construction. Never accepts or embeds a secret: production credentials are
    resolved by the running process itself from the macOS Keychain at start time (see
    adapters/secret_store.py) or left to the environment for development - neither is
    ever baked into this file."""
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": list(program_arguments),
        "RunAtLoad": run_at_load,
        "KeepAlive": {"SuccessfulExit": False} if keep_alive else False,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "ProcessType": "Background",
    }
    if working_directory is not None:
        plist["WorkingDirectory"] = str(working_directory)
    return plistlib.dumps(plist)


def validate_launch_agent_plist(plist_bytes: bytes, expected_label: str) -> list[str]:
    problems: list[str] = []
    try:
        data = plistlib.loads(plist_bytes)
    except Exception as exc:  # noqa: BLE001 - any parse failure is itself the finding
        return [f"plist did not parse: {exc}"]

    if data.get("Label") != expected_label:
        problems.append(f"Label is {data.get('Label')!r}, expected {expected_label!r}")
    program_arguments = data.get("ProgramArguments")
    if not program_arguments or not isinstance(program_arguments, list):
        problems.append("ProgramArguments missing or empty")
    if "RunAtLoad" not in data:
        problems.append("RunAtLoad missing")
    if "StandardOutPath" not in data or "StandardErrorPath" not in data:
        problems.append("StandardOutPath/StandardErrorPath missing")

    # A plist should never carry live request/response traffic at all, so both
    # marker tiers apply here (unlike source code, which legitimately implements
    # "Authorization: Bearer <token>" as part of the API client).
    raw_text = plist_bytes.decode("utf-8", errors="ignore")
    for marker in (*FORBIDDEN_SECRET_MARKERS, *FORBIDDEN_RUNTIME_TRAFFIC_MARKERS):
        if marker in raw_text:
            problems.append(f"forbidden marker {marker!r} found in plist")
    return problems


# --- Static secret scan -----------------------------------------------------------------


def scan_for_forbidden_markers(paths: Iterable[Path]) -> dict[str, list[str]]:
    """Returns {path: [markers found]} for any text file containing a forbidden
    marker. Empty dict means the scan found nothing. Binary files (e.g. PNG icons) are
    skipped rather than raising."""
    findings: dict[str, list[str]] = {}
    for path in paths:
        if path.is_dir():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits = [marker for marker in FORBIDDEN_SECRET_MARKERS if marker in text]
        if hits:
            findings[str(path)] = hits
    return findings
