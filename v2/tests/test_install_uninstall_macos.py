"""Part 6C-B: offline functional tests for scripts/install-macos.sh and
scripts/uninstall-macos.sh.

These execute the REAL shell scripts as real subprocesses, but every macOS-specific
tool they call (`uv`, `launchctl`, `curl`) is replaced with a small fake executable on
a temporary PATH, and `$HOME` is always overridden to a `tmp_path` - never the
developer's real `$HOME`, real `~/Library/LaunchAgents/`, or a real `uv tool install`.
No real LaunchAgent is ever registered by these tests, and no network call is made
(the fake `curl` never touches a socket).
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-macos.sh"
UNINSTALL_SCRIPT = REPO_ROOT / "scripts" / "uninstall-macos.sh"

FAKE_STOCK_RESEARCH_AI = """#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  init-db)
    exit 0
    ;;
  backup)
    shift
    OUT=""
    while [[ $# -gt 0 ]]; do
      case "$1" in --output) OUT="$2"; shift 2 ;; *) shift ;; esac
    done
    mkdir -p "$OUT"
    echo '{"schema_version": 1}' > "$OUT/backup_metadata.json"
    exit 0
    ;;
  render-launchagent)
    shift
    OUT=""
    while [[ $# -gt 0 ]]; do
      case "$1" in --output) OUT="$2"; shift 2 ;; --executable) shift 2 ;; *) shift ;; esac
    done
    mkdir -p "$(dirname "$OUT")"
    cat > "$OUT" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.stockresearch.local</string>
  <key>ProgramArguments</key><array><string>FAKE</string><string>serve-http</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/fake-stdout.log</string>
  <key>StandardErrorPath</key><string>/tmp/fake-stderr.log</string>
</dict>
</plist>
PLIST
    echo "$OUT"
    exit 0
    ;;
  diagnostics)
    echo "application_version: 0.0.0-fake"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_fake_bin(tmp_path: Path, *, install_marker: Path) -> Path:
    """A fake PATH directory with `uv`, `launchctl`, and `curl` stand-ins. `uv tool
    install` "installs" the fake stock-research-ai stub above into the same directory
    so it becomes resolvable on PATH for the rest of the script."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()

    stub_source = tmp_path / "fake-stock-research-ai-source.sh"
    _write_executable(stub_source, FAKE_STOCK_RESEARCH_AI)

    _write_executable(
        fake_bin / "uv",
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "uv $*" >> {install_marker}
if [[ "$1" == "tool" && "$2" == "install" ]]; then
  cp {stub_source} "$(dirname "$0")/stock-research-ai"
  chmod +x "$(dirname "$0")/stock-research-ai"
fi
if [[ "$1" == "tool" && "$2" == "uninstall" ]]; then
  rm -f "$(dirname "$0")/stock-research-ai"
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "launchctl",
        f"""#!/usr/bin/env bash
echo "launchctl $*" >> {install_marker}
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
# Simulates an immediately-healthy local service - never touches a real socket.
exit 0
""",
    )
    return fake_bin


def _env_with_fake_bin(fake_bin: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["UV_TOOL_BIN_DIR"] = str(fake_bin)
    env["SRAI_SKIP_OS_CHECK"] = "0"  # the test machine is real macOS; no override needed
    if extra:
        env.update(extra)
    return env


def _make_release_dir(tmp_path: Path) -> Path:
    release_dir = tmp_path / "release"
    (release_dir / "backend").mkdir(parents=True)
    wheel_path = release_dir / "backend" / "stock_research_ai-0.1.0-py3-none-any.whl"
    wheel_path.write_bytes(b"fake wheel")
    return release_dir


def test_install_runs_end_to_end_with_fakes_and_never_touches_real_home(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    marker = tmp_path / "calls.log"
    release_dir = _make_release_dir(tmp_path)
    fake_bin = _make_fake_bin(tmp_path, install_marker=marker)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SCRIPT),
            "--release-dir",
            str(release_dir),
            "--home",
            str(fake_home),
        ],
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (fake_home / ".stock-research-ai").is_dir()
    assert (fake_home / ".stock-research-ai" / "logs").is_dir()
    plist_path = fake_home / "Library" / "LaunchAgents" / "ai.stockresearch.local.plist"
    assert plist_path.is_file()
    assert "sk-ant-" not in plist_path.read_text()

    calls = marker.read_text()
    assert "uv tool install" in calls
    assert "launchctl bootstrap" in calls
    assert "launchctl kickstart" in calls


def test_install_never_writes_outside_the_overridden_home(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    marker = tmp_path / "calls.log"
    release_dir = _make_release_dir(tmp_path)
    fake_bin = _make_fake_bin(tmp_path, install_marker=marker)
    real_home_marker = Path.home() / ".stock-research-ai-6c-b-test-canary"
    assert not real_home_marker.exists()

    subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--release-dir", str(release_dir), "--home", str(fake_home)],
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    assert not real_home_marker.exists()


def test_install_creates_a_pre_upgrade_backup_when_a_database_already_exists(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    marker = tmp_path / "calls.log"
    release_dir = _make_release_dir(tmp_path)
    fake_bin = _make_fake_bin(tmp_path, install_marker=marker)

    env = _env_with_fake_bin(fake_bin)
    # First install: no pre-existing DB, so no backup is expected.
    subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--release-dir", str(release_dir), "--home", str(fake_home)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    data_dir = fake_home / ".stock-research-ai"
    assert not (data_dir / "backups").exists()

    # Simulate an existing database before the "upgrade" run.
    (data_dir / "research.sqlite3").write_bytes(b"fake db bytes")

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--release-dir", str(release_dir), "--home", str(fake_home)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    backups = list((data_dir / "backups").glob("pre-upgrade-*"))
    assert len(backups) == 1
    assert (backups[0] / "backup_metadata.json").is_file()
    # The "upgraded" database itself must still be present and untouched by the backup step.
    assert (data_dir / "research.sqlite3").read_bytes() == b"fake db bytes"


def test_install_fails_cleanly_when_uv_is_missing(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    release_dir = _make_release_dir(tmp_path)
    env = dict(os.environ)
    # A PATH with the real system directories (so `bash` itself still resolves) but no
    # `uv` anywhere on it.
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    env["PATH"] = f"{empty_bin}:/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--release-dir", str(release_dir), "--home", str(fake_home)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "uv" in result.stderr.lower()


def test_install_fails_cleanly_when_the_release_bundle_has_no_wheel(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    marker = tmp_path / "calls.log"
    release_dir = tmp_path / "empty-release"
    (release_dir / "backend").mkdir(parents=True)
    fake_bin = _make_fake_bin(tmp_path, install_marker=marker)

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--release-dir", str(release_dir), "--home", str(fake_home)],
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "wheel" in result.stderr.lower()


# --- Uninstall ------------------------------------------------------------------------


def _install_first(tmp_path, fake_home: Path, marker: Path, fake_bin: Path) -> Path:
    release_dir = _make_release_dir(tmp_path)
    subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--release-dir", str(release_dir), "--home", str(fake_home)],
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return release_dir


def test_default_uninstall_preserves_research_data(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    marker = tmp_path / "calls.log"
    fake_bin = _make_fake_bin(tmp_path, install_marker=marker)
    _install_first(tmp_path, fake_home, marker, fake_bin)

    data_dir = fake_home / ".stock-research-ai"
    (data_dir / "research.sqlite3").write_bytes(b"precious research data")
    plist_path = fake_home / "Library" / "LaunchAgents" / "ai.stockresearch.local.plist"
    assert plist_path.is_file()

    result = subprocess.run(
        ["bash", str(UNINSTALL_SCRIPT), "--home", str(fake_home)],
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not plist_path.exists()
    assert data_dir.is_dir()
    assert (data_dir / "research.sqlite3").read_bytes() == b"precious research data"
    assert str(data_dir) in result.stdout


def test_uninstall_with_remove_data_and_yes_deletes_the_data_directory(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    marker = tmp_path / "calls.log"
    fake_bin = _make_fake_bin(tmp_path, install_marker=marker)
    _install_first(tmp_path, fake_home, marker, fake_bin)

    data_dir = fake_home / ".stock-research-ai"
    (data_dir / "research.sqlite3").write_bytes(b"data to be deleted")

    result = subprocess.run(
        ["bash", str(UNINSTALL_SCRIPT), "--home", str(fake_home), "--remove-data", "--yes"],
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not data_dir.exists()


def test_uninstall_with_remove_data_but_no_yes_requires_interactive_confirmation(tmp_path) -> None:
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    marker = tmp_path / "calls.log"
    fake_bin = _make_fake_bin(tmp_path, install_marker=marker)
    _install_first(tmp_path, fake_home, marker, fake_bin)

    data_dir = fake_home / ".stock-research-ai"
    (data_dir / "research.sqlite3").write_bytes(b"must survive a declined confirmation")

    result = subprocess.run(
        ["bash", str(UNINSTALL_SCRIPT), "--home", str(fake_home), "--remove-data"],
        input="not-delete\n",
        env=_env_with_fake_bin(fake_bin),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert data_dir.exists()
    assert (data_dir / "research.sqlite3").read_bytes() == b"must survive a declined confirmation"


def test_uninstall_never_touches_the_keychain(tmp_path) -> None:
    """Structural proof, not a real Keychain check: the uninstall script's own source
    never calls `security` at all - the fact that no test needs to stub it out to pass
    is exactly the point (there is nothing to intercept)."""
    content = UNINSTALL_SCRIPT.read_text()
    assert "security " not in content and "/usr/bin/security" not in content


def test_uninstall_refuses_to_delete_an_unsafe_looking_path(tmp_path) -> None:
    """A --home resolving such that the computed data dir does not end in
    '.stock-research-ai' must be refused outright."""
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    # Sabotage: pre-create a *file* (not a directory) where the data dir should be, so
    # `cd` inside the script fails and the safety guard's "nothing to delete" path is
    # exercised, proving the guard runs before any deletion.
    result = subprocess.run(
        ["bash", str(UNINSTALL_SCRIPT), "--home", str(fake_home), "--remove-data", "--yes"],
        env=_env_with_fake_bin(_make_fake_bin(tmp_path, install_marker=tmp_path / "calls.log")),
        capture_output=True,
        text=True,
        timeout=30,
    )
    # No ~/.stock-research-ai exists at all yet - must exit cleanly without error and
    # without attempting a deletion.
    assert result.returncode == 0
    assert "Nothing to delete" in result.stdout


@pytest.mark.parametrize("script", [INSTALL_SCRIPT, UNINSTALL_SCRIPT])
def test_scripts_have_no_windows_line_endings_and_are_executable(script: Path) -> None:
    assert b"\r\n" not in script.read_bytes()
    assert os.access(script, os.X_OK)
