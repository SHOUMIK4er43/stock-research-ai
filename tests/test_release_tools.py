"""Part 6C-B: release/packaging helper tests. Fully offline - real temp directories,
no network, no real extension ZIP consumed by an actual Chrome install."""

import json
import plistlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stock_research_ai.release_tools import (
    LAUNCH_AGENT_LABEL,
    ReleaseValidationError,
    application_version,
    build_extension_zip,
    build_release_manifest,
    extension_version,
    render_launch_agent_plist,
    scan_for_forbidden_markers,
    sha256_of_file,
    validate_backend_archive,
    validate_extension_zip,
    validate_launch_agent_plist,
    write_release_manifest,
    write_sha256sums,
)

REAL_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"


def test_application_version_matches_the_unified_6c_a_source() -> None:
    import importlib.metadata

    assert application_version() == importlib.metadata.version("stock-research-ai")


def test_extension_version_reads_the_real_manifest() -> None:
    version = extension_version(REAL_EXTENSION_DIR)
    assert version == "0.1.0"


def test_extension_version_raises_on_missing_field(tmp_path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"manifest_version": 3}))
    with pytest.raises(ReleaseValidationError):
        extension_version(tmp_path)


# --- Checksums --------------------------------------------------------------------


def test_sha256_of_file_matches_hashlib_directly(tmp_path) -> None:
    import hashlib

    path = tmp_path / "a.txt"
    content = b"hello world"
    path.write_bytes(content)
    assert sha256_of_file(path) == hashlib.sha256(content).hexdigest()


def test_sha256_of_file_handles_content_larger_than_one_chunk(tmp_path) -> None:
    import hashlib

    path = tmp_path / "big.bin"
    content = b"x" * (3 * 1024 * 1024 + 17)  # spans multiple 1 MiB read chunks
    path.write_bytes(content)
    assert sha256_of_file(path) == hashlib.sha256(content).hexdigest()


def test_write_sha256sums_produces_sorted_relative_lines(tmp_path) -> None:
    (tmp_path / "b.bin").write_bytes(b"222")
    (tmp_path / "a.bin").write_bytes(b"111")
    output = tmp_path / "SHA256SUMS"
    write_sha256sums([tmp_path / "b.bin", tmp_path / "a.bin"], output, root=tmp_path)
    lines = output.read_text().strip().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("a.bin")
    assert lines[1].endswith("b.bin")
    for line in lines:
        digest, name = line.split("  ")
        assert len(digest) == 64
        assert "/" not in name or not name.startswith("/")


def test_sha256sums_never_records_an_absolute_path(tmp_path) -> None:
    nested = tmp_path / "sub" / "file.bin"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")
    output = tmp_path / "SHA256SUMS"
    write_sha256sums([nested], output, root=tmp_path)
    content = output.read_text()
    assert str(tmp_path) not in content
    assert "sub/file.bin" in content


# --- Release manifest ---------------------------------------------------------------


def test_release_manifest_has_the_required_fields() -> None:
    manifest = build_release_manifest(
        backend_version="0.1.0",
        extension_version_value="0.1.0",
        artifact_hashes={"backend.whl": "a" * 64, "extension.zip": "b" * 64},
        git_commit="deadbeef",
        built_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    for field in (
        "product",
        "backend_version",
        "extension_version",
        "api_version",
        "research_plan_version",
        "built_at_utc",
        "git_commit",
        "artifacts",
        "platform_target",
        "minimum_macos_version",
        "python_runtime_strategy",
    ):
        assert field in manifest
    assert manifest["research_plan_version"] == "FULL_RESEARCH_V1"
    assert manifest["api_version"] == "v1"


def test_release_manifest_never_contains_a_secret_or_local_path() -> None:
    manifest = build_release_manifest(
        backend_version="0.1.0",
        extension_version_value="0.1.0",
        artifact_hashes={"backend.whl": "a" * 64},
        git_commit="deadbeef",
    )
    rendered = json.dumps(manifest)
    for forbidden in ("sk-ant-", "Authorization", "Bearer", "/Users/", str(Path.home())):
        assert forbidden not in rendered


def test_write_release_manifest_round_trips(tmp_path) -> None:
    manifest = build_release_manifest(
        backend_version="0.1.0",
        extension_version_value="0.1.0",
        artifact_hashes={"x": "a" * 64},
        git_commit=None,
    )
    output = tmp_path / "RELEASE-MANIFEST.json"
    write_release_manifest(manifest, output)
    reloaded = json.loads(output.read_text())
    assert reloaded == manifest


# --- Extension ZIP packaging ---------------------------------------------------------


def test_build_extension_zip_excludes_forbidden_paths_and_includes_required_files(
    tmp_path,
) -> None:
    output_zip = tmp_path / "extension.zip"
    build_extension_zip(REAL_EXTENSION_DIR, output_zip)

    with zipfile.ZipFile(output_zip) as archive:
        names = set(archive.namelist())

    assert "manifest.json" in names
    assert "sidepanel.html" in names
    assert "src/background.js" in names
    assert "icons/icon128.png" in names
    assert not any(name.startswith("tests/") for name in names)
    assert "package.json" not in names
    assert not any(".git" in name for name in names)
    assert not any("real_yesbank_report.json" in name for name in names)


def test_extension_zip_has_no_nested_repository_path(tmp_path) -> None:
    output_zip = tmp_path / "extension.zip"
    build_extension_zip(REAL_EXTENSION_DIR, output_zip)
    with zipfile.ZipFile(output_zip) as archive:
        names = archive.namelist()
    assert "manifest.json" in names  # at the ZIP root, not "extension/manifest.json"
    assert not any(name.startswith("extension/") for name in names)


def test_extension_zip_build_is_deterministic_across_two_builds(tmp_path) -> None:
    zip1 = tmp_path / "one.zip"
    zip2 = tmp_path / "two.zip"
    build_extension_zip(REAL_EXTENSION_DIR, zip1)
    build_extension_zip(REAL_EXTENSION_DIR, zip2)
    assert zip1.read_bytes() == zip2.read_bytes()


def test_validate_extension_zip_accepts_the_real_built_zip(tmp_path) -> None:
    output_zip = tmp_path / "extension.zip"
    build_extension_zip(REAL_EXTENSION_DIR, output_zip)
    assert validate_extension_zip(output_zip) == []


def test_validate_extension_zip_flags_a_missing_manifest(tmp_path) -> None:
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr("sidepanel.html", "<html></html>")
    problems = validate_extension_zip(bad_zip)
    assert any("manifest.json" in p for p in problems)


def test_validate_extension_zip_flags_a_forbidden_test_directory(tmp_path) -> None:
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "manifest_version": 3,
                    "permissions": ["storage", "sidePanel"],
                    "host_permissions": [
                        "https://www.screener.in/*",
                        "http://127.0.0.1/*",
                    ],
                    "icons": {"16": "icons/icon16.png"},
                }
            ),
        )
        archive.writestr("sidepanel.html", "x")
        archive.writestr("src/background.js", "x")
        archive.writestr("src/sidepanel.js", "x")
        for icon in ("icon16.png", "icon32.png", "icon48.png", "icon128.png"):
            archive.writestr(f"icons/{icon}", b"\x89PNG")
        archive.writestr("tests/evil.test.js", "x")
    problems = validate_extension_zip(bad_zip)
    assert any("tests/evil.test.js" in p for p in problems)


def test_validate_extension_zip_flags_unexpected_permissions(tmp_path) -> None:
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "manifest_version": 3,
                    "permissions": ["storage", "sidePanel", "tabs"],
                    "host_permissions": ["https://www.screener.in/*"],
                    "icons": {"16": "icons/icon16.png"},
                }
            ),
        )
    problems = validate_extension_zip(bad_zip)
    assert any("tabs" in p for p in problems)


def test_validate_extension_zip_flags_an_embedded_secret_marker(tmp_path) -> None:
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "manifest_version": 3,
                    "permissions": ["storage", "sidePanel"],
                    "host_permissions": ["https://www.screener.in/*"],
                    "icons": {"16": "icons/icon16.png"},
                }
            ),
        )
        archive.writestr("src/leaky.js", "const key = 'sk-ant-oops';")
    problems = validate_extension_zip(bad_zip)
    assert any("sk-ant-" in p for p in problems)


# --- Backend wheel / sdist -----------------------------------------------------------


def test_validate_backend_archive_flags_a_tool_cache_database_in_a_wheel(tmp_path) -> None:
    """Release smoke test (2026-10-23): uv_build does not read .gitignore, so a stray
    src/stock_research_ai/.mypy_cache (SQLite files holding absolute local paths) shipped
    inside the wheel and sdist."""
    wheel = tmp_path / "pkg-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("stock_research_ai/__init__.py", "")
        archive.writestr("stock_research_ai/.mypy_cache/3.12/cache.0.db", b"SQLite")
        archive.writestr("stock_research_ai/data/research.sqlite3", b"SQLite")
    problems = validate_backend_archive(wheel)
    assert any(".mypy_cache/3.12/cache.0.db" in p for p in problems)
    assert any("research.sqlite3" in p for p in problems)


def test_validate_backend_archive_flags_an_env_file_in_an_sdist(tmp_path) -> None:
    import io
    import tarfile

    sdist = tmp_path / "pkg-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in ("pkg-0.1.0/src/stock_research_ai/__init__.py", "pkg-0.1.0/.env.local"):
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    problems = validate_backend_archive(sdist)
    assert problems == ["forbidden file present in pkg-0.1.0.tar.gz: pkg-0.1.0/.env.local"]


def test_validate_backend_archive_accepts_code_fonts_and_metadata(tmp_path) -> None:
    wheel = tmp_path / "pkg-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in ("stock_research_ai/__init__.py",
                     "stock_research_ai/adapters/rendering/fonts/DejaVuSansCondensed.ttf",
                     "stock_research_ai-0.1.0.dist-info/METADATA"):
            archive.writestr(name, "x")
    assert validate_backend_archive(wheel) == []


def test_pyproject_excludes_tool_caches_from_the_build_backend() -> None:
    import tomllib

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text())
    excluded = set(config["tool"]["uv"]["build-backend"]["source-exclude"])
    assert {".mypy_cache", ".ruff_cache", ".pytest_cache"} <= excluded


# --- LaunchAgent plist ---------------------------------------------------------------


def test_render_launch_agent_plist_parses_and_has_no_secret(tmp_path) -> None:
    plist_bytes = render_launch_agent_plist(
        label=LAUNCH_AGENT_LABEL,
        program_arguments=["/usr/local/bin/stock-research-ai", "serve-http"],
        stdout_path=tmp_path / "logs" / "stdout.log",
        stderr_path=tmp_path / "logs" / "stderr.log",
    )
    data = plistlib.loads(plist_bytes)
    assert data["Label"] == LAUNCH_AGENT_LABEL
    assert data["ProgramArguments"][0].endswith("stock-research-ai")
    assert data["RunAtLoad"] is True
    assert "sk-ant-" not in plist_bytes.decode()
    assert "Authorization" not in plist_bytes.decode()


def test_validate_launch_agent_plist_accepts_a_well_formed_plist(tmp_path) -> None:
    plist_bytes = render_launch_agent_plist(
        label=LAUNCH_AGENT_LABEL,
        program_arguments=["/x/stock-research-ai", "serve-http"],
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    assert validate_launch_agent_plist(plist_bytes, LAUNCH_AGENT_LABEL) == []


def test_validate_launch_agent_plist_flags_a_wrong_label(tmp_path) -> None:
    plist_bytes = render_launch_agent_plist(
        label="com.wrong.label",
        program_arguments=["/x/stock-research-ai", "serve-http"],
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    problems = validate_launch_agent_plist(plist_bytes, LAUNCH_AGENT_LABEL)
    assert any("Label" in p for p in problems)


def test_validate_launch_agent_plist_flags_an_embedded_secret() -> None:
    poisoned = plistlib.dumps(
        {
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": ["/x/y"],
            "RunAtLoad": True,
            "StandardOutPath": "/x/out.log",
            "StandardErrorPath": "/x/err.log",
            "EnvironmentVariables": {"STOCK_RESEARCH_AI_ANALYST_API_KEY": "sk-ant-leaked"},
        }
    )
    problems = validate_launch_agent_plist(poisoned, LAUNCH_AGENT_LABEL)
    assert any("sk-ant-" in p for p in problems)


def test_plutil_lint_accepts_the_rendered_plist(tmp_path) -> None:
    """If macOS's own plutil is available (it is, on the machine this ships for),
    prove the rendered plist passes its structural linter directly - not just our own
    plistlib round-trip."""
    import shutil
    import subprocess

    plutil = shutil.which("plutil")
    if plutil is None:
        pytest.skip("plutil not available on this machine")

    plist_bytes = render_launch_agent_plist(
        label=LAUNCH_AGENT_LABEL,
        program_arguments=["/x/stock-research-ai", "serve-http"],
        stdout_path=tmp_path / "out.log",
        stderr_path=tmp_path / "err.log",
    )
    plist_path = tmp_path / "test.plist"
    plist_path.write_bytes(plist_bytes)
    result = subprocess.run(
        [plutil, "-lint", str(plist_path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- Static secret scan ---------------------------------------------------------------


def test_scan_for_forbidden_markers_finds_a_planted_secret(tmp_path) -> None:
    bad_file = tmp_path / "oops.txt"
    bad_file.write_text("STOCK_RESEARCH_AI_ANALYST_API_KEY=sk-ant-planted")
    findings = scan_for_forbidden_markers([bad_file])
    assert str(bad_file) in findings


def test_scan_for_forbidden_markers_is_clean_on_ordinary_files(tmp_path) -> None:
    clean_file = tmp_path / "ok.txt"
    clean_file.write_text("Revenue grew year over year.")
    assert scan_for_forbidden_markers([clean_file]) == {}


def test_scan_for_forbidden_markers_skips_binary_files_without_crashing(tmp_path) -> None:
    binary_file = tmp_path / "icon.png"
    binary_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff")
    assert scan_for_forbidden_markers([binary_file]) == {}


def test_real_release_inputs_contain_no_forbidden_marker() -> None:
    """Scans the actual extension source tree that will be packaged - the real,
    end-to-end static secret scan this Part 6C-B slice is required to perform."""
    candidates = [p for p in REAL_EXTENSION_DIR.rglob("*") if p.is_file()]
    findings = scan_for_forbidden_markers(candidates)
    assert findings == {}


def test_forbidden_marker_scan_does_not_flag_legitimate_authorization_header_code() -> None:
    """`extension/src/api/client.js` legitimately builds an
    `Authorization: Bearer <token>` header (that is the correct, required
    implementation, not a leak) - scan_for_forbidden_markers's marker set must not
    treat that source code as a finding, the same way redaction elsewhere in this
    project never flags the word "Secretary" for containing "secret"."""
    client_js = REAL_EXTENSION_DIR / "src" / "api" / "client.js"
    assert client_js.is_file()
    assert "Authorization" in client_js.read_text()
    assert scan_for_forbidden_markers([client_js]) == {}
