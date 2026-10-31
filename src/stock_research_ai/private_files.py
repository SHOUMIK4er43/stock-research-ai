"""P2-5: owner-only permissions for the application's local data.

Directories this application creates are 0700 and files holding research data, tokens
or logs are created 0600 from the first byte (never written first and narrowed after).
Directories that already exist are left exactly as they are: a user-chosen location
(e.g. a custom database or artifact path under ~/Documents) is never chmod-ed.
POSIX bits are best-effort: a filesystem that ignores them does not fail the caller.
"""

import contextlib
import os
import tempfile
from pathlib import Path

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_dir(path: Path) -> None:
    """Create `path` and any missing ancestors as 0700; existing ones are untouched."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        except FileExistsError:
            continue  # created concurrently by someone else: not ours to change
        with contextlib.suppress(OSError):
            os.chmod(directory, PRIVATE_DIRECTORY_MODE)  # mkdir's mode is umask-filtered
    if not path.is_dir():
        raise NotADirectoryError(path.name)


def create_private_file(path: Path) -> None:
    """Create an empty 0600 file if none exists; an existing file is narrowed to 0600."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE))
    except FileExistsError:
        pass
    with contextlib.suppress(OSError):
        os.chmod(path, PRIVATE_FILE_MODE)


def write_private_file(path: Path, data: str) -> None:
    """Atomically replace `path` with `data`: written to a 0600 temporary file in the
    same directory, then renamed, so no reader sees a partial or wider-mode file."""
    ensure_private_dir(path.parent)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")  # always 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


__all__ = [
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "create_private_file",
    "ensure_private_dir",
    "write_private_file",
]
