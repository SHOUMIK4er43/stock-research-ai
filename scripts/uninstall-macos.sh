#!/usr/bin/env bash
# Part 6C-B macOS pilot uninstaller.
#
# Default behavior removes ONLY the installed application/runtime and the LaunchAgent
# registration. Research data (the SQLite database, artifacts/evidence, backups, and
# the pairing state file) is PRESERVED by default and never silently deleted. The
# macOS Keychain credential is never touched by this script under any flag.
set -euo pipefail

LABEL="ai.stockresearch.local"

INSTALL_HOME="${HOME}"
REMOVE_DATA=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: uninstall-macos.sh [--home DIR] [--remove-data] [--yes]

  --home DIR      Override $HOME for this uninstall. Intended for automated tests
                  only - a normal pilot uninstall never needs this.
  --remove-data   DESTRUCTIVE: also delete the local research data directory
                  (~/.stock-research-ai) - the SQLite database, artifacts/evidence,
                  logs, and backups. Requires --yes as well (or an interactive typed
                  confirmation). The macOS Keychain credential is never touched.
  --yes           Non-interactive confirmation for --remove-data. Without it,
                  --remove-data prompts interactively before deleting anything.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --home)
      INSTALL_HOME="$2"
      shift 2
      ;;
    --remove-data)
      REMOVE_DATA=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
      shift
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

if [[ "${SRAI_SKIP_OS_CHECK:-0}" != "1" ]]; then
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "error: this uninstaller supports macOS only." >&2
    exit 1
  fi
fi

DATA_DIR="${INSTALL_HOME}/.stock-research-ai"
LAUNCH_AGENTS_DIR="${INSTALL_HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"

# --- Stop and remove the LaunchAgent registration -----------------------------------

UID_NUM="$(id -u)"
launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 || true
if [[ -f "$PLIST_PATH" ]]; then
  rm -f "$PLIST_PATH"
  echo "Removed LaunchAgent: ${PLIST_PATH}"
else
  echo "No LaunchAgent registration found at: ${PLIST_PATH}"
fi

# --- Remove the installed application/runtime ---------------------------------------

if command -v uv >/dev/null 2>&1; then
  uv tool uninstall stock-research-ai >/dev/null 2>&1 || true
  echo "Uninstalled the stock-research-ai application (if it was installed via uv)."
else
  echo "note: 'uv' was not found - skipping application uninstall step (nothing to do if it was never installed this way)." >&2
fi

# --- Data retention: preserved by default, deleted only with explicit intent --------

if [[ "$REMOVE_DATA" -eq 0 ]]; then
  if [[ -d "$DATA_DIR" ]]; then
    echo "Your local research data was preserved at: ${DATA_DIR}"
  fi
  echo "Your macOS Keychain credential (if configured) was preserved. Uninstall does not remove it."
  exit 0
fi

# From here on, --remove-data was explicitly requested.
echo ""
echo "!!! --remove-data was specified. This will PERMANENTLY DELETE:"
echo "!!!   ${DATA_DIR}"
echo "!!! including your research database, evidence, reports, logs, and backups."
echo "!!! Your macOS Keychain credential is NOT affected by this."
echo ""

if [[ "$ASSUME_YES" -ne 1 ]]; then
  read -r -p "Type DELETE to permanently remove this data: " CONFIRMATION
  if [[ "$CONFIRMATION" != "DELETE" ]]; then
    echo "Confirmation not received - no data was deleted." >&2
    exit 1
  fi
fi

# Safety: refuse to touch anything unless the resolved path is unmistakably the
# application's own canonical data directory - never a bare/empty variable, never
# $HOME itself, never "/".
RESOLVED_DATA_DIR="$(cd "$DATA_DIR" 2>/dev/null && pwd || true)"
RESOLVED_HOME="$(cd "$INSTALL_HOME" 2>/dev/null && pwd || true)"
if [[ -z "$RESOLVED_DATA_DIR" ]]; then
  echo "Nothing to delete - ${DATA_DIR} does not exist."
  exit 0
fi
if [[ "$RESOLVED_DATA_DIR" == "/" || "$RESOLVED_DATA_DIR" == "$RESOLVED_HOME" ]]; then
  echo "error: refusing to delete an unsafe-looking path: ${RESOLVED_DATA_DIR}" >&2
  exit 1
fi
case "$RESOLVED_DATA_DIR" in
  */.stock-research-ai) ;;
  *)
    echo "error: refusing to delete a path that does not end in '.stock-research-ai': ${RESOLVED_DATA_DIR}" >&2
    exit 1
    ;;
esac

rm -rf -- "$RESOLVED_DATA_DIR"
echo "Deleted: ${RESOLVED_DATA_DIR}"
