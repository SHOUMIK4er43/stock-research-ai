#!/usr/bin/env bash
# Part 6C-B macOS pilot installer.
#
# User-local install only: no sudo, no Git clone, no virtualenv setup, no `curl | sh`,
# no unverified remote executable download, no system Python modification. Consumes an
# already-built release bundle (see scripts/build-release.sh) - the installed
# application never needs the source repository afterward.
#
# Safe to re-run: a second run is treated as an upgrade and preserves the existing
# database, artifacts, Keychain credential, and pairing state.
set -euo pipefail

# Must match stock_research_ai.release_tools.LAUNCH_AGENT_LABEL exactly - used
# consistently here, in the uninstaller, and in the rendered plist's own Label key.
LABEL="ai.stockresearch.local"
HEALTH_TIMEOUT_SECONDS="${SRAI_HEALTH_TIMEOUT_SECONDS:-15}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_DIR="$SCRIPT_DIR"
INSTALL_HOME="${HOME}"

usage() {
  cat <<'EOF'
Usage: install-macos.sh [--release-dir DIR] [--home DIR]

  --release-dir DIR   Directory containing the release bundle (backend/, extension/,
                       RELEASE-MANIFEST.json, SHA256SUMS). Defaults to this script's
                       own directory (the normal case when run from an extracted
                       release download).
  --home DIR          Override $HOME for this install. Intended for automated tests
                       only - a normal pilot install never needs this.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir)
      RELEASE_DIR="$2"
      shift 2
      ;;
    --home)
      INSTALL_HOME="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# --- Platform checks ----------------------------------------------------------------

if [[ "${SRAI_SKIP_OS_CHECK:-0}" != "1" ]]; then
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: this installer supports macOS only." >&2
    exit 1
  fi
fi

ARCH="$(uname -m)"
case "$ARCH" in
  arm64|x86_64) ;;
  *)
    echo "warning: unrecognized architecture '${ARCH}' - continuing, but this has not been validated." >&2
    ;;
esac
echo "Detected macOS (${ARCH})."

if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: `uv` was not found on PATH.

Stock Research AI is installed using uv, Astral's official Python package/tool
manager (https://astral.sh/uv). Install it first by following the official
instructions at that URL, then re-run this installer. This installer never
downloads or executes uv automatically on your behalf.
EOF
  exit 1
fi

# --- Locate the backend wheel in the release bundle ----------------------------------

WHEEL=""
for candidate in "${RELEASE_DIR}"/backend/*.whl; do
  if [[ -f "$candidate" ]]; then
    WHEEL="$candidate"
    break
  fi
done
if [[ -z "$WHEEL" ]]; then
  echo "error: no backend wheel found under ${RELEASE_DIR}/backend/. Is --release-dir correct?" >&2
  exit 1
fi

# --- Resolve canonical production paths (Part 6C-A conventions) ---------------------

if [[ -n "${UV_TOOL_BIN_DIR:-}" ]]; then
  TOOL_BIN_DIR="$UV_TOOL_BIN_DIR"
else
  TOOL_BIN_DIR="${INSTALL_HOME}/.local/bin"
fi

export HOME="$INSTALL_HOME"
export PATH="${TOOL_BIN_DIR}:${PATH}"

DATA_DIR="${INSTALL_HOME}/.stock-research-ai"
LOG_DIR="${DATA_DIR}/logs"
DB_PATH="${DATA_DIR}/research.sqlite3"
LAUNCH_AGENTS_DIR="${INSTALL_HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$LAUNCH_AGENTS_DIR"

# --- Safety backup before any upgrade that touches an existing database -------------

EXISTING_BINARY="$(command -v stock-research-ai || true)"
if [[ -n "$EXISTING_BINARY" && -f "$DB_PATH" ]]; then
  echo "Existing installation and database detected - creating a safety backup before upgrading..."
  BACKUP_DIR="${DATA_DIR}/backups/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"
  if "$EXISTING_BINARY" backup --output "$BACKUP_DIR"; then
    echo "Safety backup created at: ${BACKUP_DIR}"
  else
    echo "warning: pre-upgrade safety backup failed; continuing with the upgrade anyway." >&2
  fi
fi

# --- Install/upgrade the application via uv (never touches the source repository) ---

echo "Installing Stock Research AI from ${WHEEL} ..."
uv tool install --force "$WHEEL"

BINARY="$(command -v stock-research-ai || true)"
if [[ -z "$BINARY" ]]; then
  cat >&2 <<EOF
error: installation completed but 'stock-research-ai' was not found on PATH.
It was installed into: ${TOOL_BIN_DIR}
Add that directory to your PATH (for example, in ~/.zprofile) and re-run this
script, or open a new terminal window and try again.
EOF
  exit 1
fi
echo "Installed: ${BINARY}"

echo "Preparing the local database (existing data is preserved; a schema upgrade, if any, is applied automatically on first start)..."
"$BINARY" init-db

# --- LaunchAgent: render via the installed binary itself, never duplicated in shell --

echo "Writing LaunchAgent (${LABEL}) to ${PLIST_PATH} ..."
"$BINARY" render-launchagent --output "$PLIST_PATH" --executable "$BINARY"

UID_NUM="$(id -u)"
launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${UID_NUM}" "$PLIST_PATH"
launchctl kickstart -k "gui/${UID_NUM}/${LABEL}"

# --- Bounded local health check (loopback only, no external network) ----------------

PORT="${STOCK_RESEARCH_AI_HTTP_PORT:-8000}"
echo "Waiting for the local service to become healthy (up to ${HEALTH_TIMEOUT_SECONDS}s)..."
HEALTHY=0
for _ in $(seq 1 "$HEALTH_TIMEOUT_SECONDS"); do
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 1
done

if [[ "$HEALTHY" -eq 1 ]]; then
  cat <<EOF
Stock Research AI is installed and running at http://127.0.0.1:${PORT}

Next steps:
  ${BINARY} configure-analyst   # store your Anthropic API key in the macOS Keychain
  ${BINARY} diagnostics         # confirm everything is configured correctly
EOF
else
  cat >&2 <<EOF
warning: the local service did not report healthy within ${HEALTH_TIMEOUT_SECONDS}s.
Check the log at: ${LOG_DIR}/launchd-stderr.log
Run: ${BINARY} diagnostics
Run: launchctl print gui/${UID_NUM}/${LABEL}
EOF
fi
