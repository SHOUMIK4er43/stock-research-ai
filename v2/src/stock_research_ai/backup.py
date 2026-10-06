"""Part 6C-A: local backup/restore. Uses SQLite's own online backup API (via the
stdlib `sqlite3` module directly, in a worker thread) rather than copying the raw
database file - this is safe against a concurrent writer and correct under WAL
(SQLite's backup API reads a consistent snapshot; a naive file copy of a WAL database
could otherwise miss committed data still sitting only in the `-wal` file).

Never includes the Anthropic API key (it is never in the database or artifact
directory to begin with - see adapters/secret_store.py) and never includes the browser
pairing state (a restored install is expected to re-pair; excluding it is documented,
not silently unsupported - the pairing file is intentionally left in place across a
restore rather than being overwritten by a backup's, since a restore is about
*research data*, not re-establishing browser trust).
"""

import asyncio
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from stock_research_ai import __version__
from stock_research_ai.adapters.migrations import CURRENT_SCHEMA_VERSION
from stock_research_ai.config import Settings

_METADATA_FILENAME = "backup_metadata.json"
_DATABASE_FILENAME = "research.sqlite3"
_ARTIFACTS_DIRNAME = "artifacts"


class BackupError(RuntimeError):
    """A backup or restore operation could not complete safely. The source is left
    untouched - this is only ever raised before any destination file is considered
    complete/valid."""


@dataclass(frozen=True)
class BackupMetadata:
    application_version: str
    schema_version: int
    created_at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "application_version": self.application_version,
                "schema_version": self.schema_version,
                "created_at": self.created_at,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "BackupMetadata":
        data = json.loads(text)
        return cls(
            application_version=data["application_version"],
            schema_version=data["schema_version"],
            created_at=data["created_at"],
        )


def _backup_sqlite_file_sync(source_path: Path, destination_path: Path) -> None:
    """Runs SQLite's own consistent-snapshot backup API. Safe to call while another
    connection (e.g. a running `serve-http` process) holds the database open."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(source_path))
    try:
        destination = sqlite3.connect(str(destination_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    try:
        os.chmod(destination_path, 0o600)
    except OSError:
        pass  # best-effort: not every filesystem/OS supports POSIX permission bits


async def backup_database_file(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise BackupError(f"No database exists at {source_path} to back up.")
    await asyncio.to_thread(_backup_sqlite_file_sync, source_path, destination_path)


async def create_backup(settings: Settings, output_dir: Path) -> Path:
    """Backs up the research database and artifact directory into `output_dir`
    (created if absent; must not already contain a backup, so a backup destination is
    never silently overwritten). Never touches settings.browser_auth_path (pairing
    state) or any Anthropic credential - see module docstring."""
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BackupError(
            f"{output_dir} already exists and is not empty. "
            "Choose a new, empty destination directory for each backup."
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    await backup_database_file(settings.database_path, output_dir / _DATABASE_FILENAME)

    artifact_path = settings.artifact_path or settings.database_path.parent / "artifacts"
    if artifact_path.is_dir():
        shutil.copytree(artifact_path, output_dir / _ARTIFACTS_DIRNAME)

    metadata = BackupMetadata(
        application_version=__version__,
        schema_version=CURRENT_SCHEMA_VERSION,
        created_at=datetime.now(UTC).isoformat(),
    )
    (output_dir / _METADATA_FILENAME).write_text(metadata.to_json())
    return output_dir


async def restore_backup(settings: Settings, backup_dir: Path, force: bool = False) -> None:
    """Restores a backup created by create_backup(). Refuses a schema version newer
    than this build supports. Refuses to overwrite an existing database unless
    `force=True`, and even then takes a safety backup of the current database first
    (suffixed `.pre-restore-backup`) so a mistaken restore is itself recoverable.

    The caller is responsible for ensuring `serve-http` is not running against this
    database path during a restore - this function does not check for or manage a
    running service (V1: a CLI command the operator runs with the service stopped)."""
    backup_dir = backup_dir.expanduser().resolve()
    metadata_path = backup_dir / _METADATA_FILENAME
    if not metadata_path.is_file():
        raise BackupError(f"{backup_dir} does not look like a stock-research-ai backup.")
    metadata = BackupMetadata.from_json(metadata_path.read_text())
    if metadata.schema_version > CURRENT_SCHEMA_VERSION:
        raise BackupError(
            f"This backup's schema version ({metadata.schema_version}) is newer than "
            f"this application build supports (supports up to {CURRENT_SCHEMA_VERSION}). "
            "Upgrade the application before restoring it."
        )

    backup_db_path = backup_dir / _DATABASE_FILENAME
    if not backup_db_path.is_file():
        raise BackupError(f"{backup_dir} has no {_DATABASE_FILENAME} to restore.")

    destination = settings.database_path
    if destination.exists():
        if not force:
            raise BackupError(
                f"{destination} already exists. Pass force=True to overwrite it "
                "(a safety backup of the current database is taken first)."
            )
        safety_path = destination.with_suffix(destination.suffix + ".pre-restore-backup")
        await backup_database_file(destination, safety_path)

    await backup_database_file(backup_db_path, destination)

    backup_artifacts = backup_dir / _ARTIFACTS_DIRNAME
    if backup_artifacts.is_dir():
        artifact_path = settings.artifact_path or settings.database_path.parent / "artifacts"
        if artifact_path.exists() and not force:
            raise BackupError(
                f"{artifact_path} already exists. Pass force=True to overwrite it."
            )
        if artifact_path.exists():
            shutil.rmtree(artifact_path)
        shutil.copytree(backup_artifacts, artifact_path)
