"""DETAILED_RESEARCH_V1 Box 15: the final-report artifact store (local filesystem).

Layout (every component generated here from validated values, never from caller text):

    <root>/<company>/<as_of>/<artifact_id>/
        report.json  report.md  report.html  report.pdf  manifest.json

  <company>     'nse-<SYMBOL>' (A-Z 0-9 _ -) or 'key-<sha256(company_key)[:16]>'
  <as_of>       YYYY-MM-DD
  <artifact_id> uuid5(Box 14 SHA-256 | renderer version) — a different Box 14 content or
                renderer version is a different directory, so no unrelated run is ever
                overwritten; the same content re-rendered writes identical bytes.

Safety: the root is resolved once; every directory below it is created 0700 and refused if
it is a symlink; the final path must stay under the root. Writes are temp-file + fsync +
os.replace (a reader never sees a partial file); the manifest is written last, under an
exclusive per-run lock, and a format that failed has any stale file removed so the
directory never shows a file the manifest does not vouch for. Reads re-hash every file
against the manifest before serving it.
"""

import contextlib
import hashlib
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from stock_research_ai.application.report_orchestrator import ManifestSeed
from stock_research_ai.config import Settings
from stock_research_ai.domain.report_rendering import (
    FILE_NAMES,
    MAX_HTML_BYTES,
    MAX_MARKDOWN_BYTES,
    MAX_PDF_BYTES,
    RenderFormat,
    ReportManifest,
)
from stock_research_ai.private_files import PRIVATE_FILE_MODE, ensure_private_dir

try:  # POSIX advisory lock; elsewhere the in-process lock still serialises writers
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

MANIFEST = "manifest.json"
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&_.-]{0,19}$")
COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
AS_OF = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PAGE = re.compile(r"^[A-Z0-9_-]{1,40}$")
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_LISTED = 50
MAX_FILE_BYTES = {
    RenderFormat.JSON: 256 * 1024 * 1024, RenderFormat.MARKDOWN: MAX_MARKDOWN_BYTES,
    RenderFormat.HTML: MAX_HTML_BYTES, RenderFormat.PDF: MAX_PDF_BYTES}
_THREAD_LOCK = threading.Lock()


class UnsafePathError(ValueError):
    """A path component or the filesystem state would escape the report root."""


def final_report_root(settings: Settings) -> Path:
    return settings.database_path.parent / "final-reports"


def company_component(nse_symbol: str | None, company_key: str) -> str:
    if nse_symbol and SYMBOL.fullmatch(nse_symbol):
        return "nse-" + re.sub(r"[^A-Z0-9_-]", "_", nse_symbol)
    return "key-" + hashlib.sha256(company_key.encode("utf-8")).hexdigest()[:16]


def symbol_component(symbol: str) -> str:
    """The directory of a caller-supplied NSE symbol, or UnsafePathError."""
    if not SYMBOL.fullmatch(symbol):
        raise UnsafePathError("Not a valid NSE symbol")
    return company_component(symbol, "")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileFinalReportStore:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise UnsafePathError("The report root must be absolute")
        self._root_input = root

    # --- paths ----------------------------------------------------------------------------------

    def _root(self, *, create: bool) -> Path:
        if create:
            ensure_private_dir(self._root_input)
        return self._root_input.resolve()

    @staticmethod
    def _no_link(path: Path) -> None:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise UnsafePathError("A symlink inside the report root is refused")

    def _run_dir(self, location: str, *, create: bool) -> Path:
        parts = location.split("/")
        if len(parts) != 3 or not COMPONENT.fullmatch(parts[0]) or not AS_OF.fullmatch(
                parts[1]) or not COMPONENT.fullmatch(parts[2]):
            raise UnsafePathError("Invalid report location")
        root = self._root(create=create)
        current = root
        for part in parts:
            current = current / part
            self._no_link(current)
            if create:
                ensure_private_dir(current)
        if not current.resolve().is_relative_to(root):
            raise UnsafePathError("The report location escapes the root")
        return current

    def location(self, seed: ManifestSeed) -> str:
        loc = f"{company_component(seed.nse_symbol, seed.company_key)}/{seed.as_of}/" \
              f"{seed.artifact_id}"
        self._run_dir(loc, create=False)  # validates every component
        return loc

    # --- writing --------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, run: Path) -> Iterator[None]:
        with _THREAD_LOCK:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(run / ".lock", flags, PRIVATE_FILE_MODE)
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")  # 0600
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def publish(self, manifest: ReportManifest, files: dict[RenderFormat, bytes]) -> None:
        run = self._run_dir(manifest.location, create=True)
        listed = {f.format: f for f in manifest.files}
        if set(listed) != set(files) or any(
                _sha(files[k]) != v.sha256 for k, v in listed.items()):
            raise ValueError("The manifest does not describe these files")
        with self._locked(run):
            for fmt, data in files.items():
                target = run / FILE_NAMES[fmt]
                self._no_link(target)
                self._atomic_write(target, data)
            for failure in manifest.failures:
                stale = run / FILE_NAMES[failure.format]
                self._no_link(stale)
                stale.unlink(missing_ok=True)
            body = manifest.model_dump_json(indent=2) + "\n"
            self._atomic_write(run / MANIFEST, body.encode("utf-8"))

    # --- reading --------------------------------------------------------------------------------

    def _read_bounded(self, path: Path, limit: int) -> bytes:
        self._no_link(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise UnsafePathError("A stored report file exceeds its size bound")
        return data

    def _manifest_at(self, run: Path) -> ReportManifest | None:
        try:
            raw = self._read_bounded(run / MANIFEST, MAX_MANIFEST_BYTES)
            manifest = ReportManifest.model_validate_json(raw)
        except (FileNotFoundError, NotADirectoryError, ValidationError, UnsafePathError):
            return None
        if self._run_dir(manifest.location, create=False) != run:
            return None
        return manifest

    def load(self, artifact_id: UUID) -> ReportManifest | None:
        root = self._root(create=False)
        if not root.is_dir():
            return None
        name = str(artifact_id)
        for company in sorted(os.scandir(root), key=lambda e: e.name):
            if not company.is_dir(follow_symlinks=False) or not COMPONENT.fullmatch(
                    company.name):
                continue
            for day in sorted(os.scandir(company.path), key=lambda e: e.name):
                if not day.is_dir(follow_symlinks=False) or not AS_OF.fullmatch(day.name):
                    continue
                run = Path(day.path) / name
                if run.is_dir() and not run.is_symlink():
                    manifest = self._manifest_at(run)
                    if manifest is not None and manifest.artifact_id == artifact_id:
                        return manifest
        return None

    def read_file(self, manifest: ReportManifest, fmt: RenderFormat) -> bytes | None:
        entry = next((f for f in manifest.files if f.format == fmt), None)
        if entry is None:
            return None
        run = self._run_dir(manifest.location, create=False)
        try:
            data = self._read_bounded(run / FILE_NAMES[fmt], MAX_FILE_BYTES[fmt])
        except FileNotFoundError:
            return None
        return data if _sha(data) == entry.sha256 and len(data) == entry.bytes else None

    def verified_formats(self, manifest: ReportManifest) -> set[RenderFormat]:
        return {f.format for f in manifest.files if self.read_file(manifest, f.format)
                is not None}

    def list_for_page(self, page: str) -> list[ReportManifest]:
        """Runs whose acquired primary Screener page is `page` (the manifest records the page
        key taken from the acquired dataset, never from a caller); newest as_of first, at
        most MAX_LISTED. `page` must already be a validated page key."""
        if not PAGE.fullmatch(page):
            raise UnsafePathError("Not a Screener company page key")
        root = self._root(create=False)
        if not root.is_dir():
            return []
        found: list[ReportManifest] = []
        for company in sorted(os.scandir(root), key=lambda e: e.name):
            if not company.is_dir(follow_symlinks=False) or not COMPONENT.fullmatch(
                    company.name):
                continue
            for day in os.scandir(company.path):
                if not day.is_dir(follow_symlinks=False) or not AS_OF.fullmatch(day.name):
                    continue
                for run in os.scandir(day.path):
                    if run.is_dir(follow_symlinks=False):
                        manifest = self._manifest_at(Path(run.path))
                        if manifest is not None and manifest.screener_page == page:
                            found.append(manifest)
        found.sort(key=lambda m: (m.as_of, m.box14_generated_at, str(m.artifact_id)),
                   reverse=True)
        return found[:MAX_LISTED]

    def list_for_symbol(self, symbol: str) -> list[ReportManifest]:
        """Newest as_of first; at most MAX_LISTED runs."""
        root = self._root(create=False)
        company = root / symbol_component(symbol)
        if not company.is_dir() or company.is_symlink():
            return []
        found: list[ReportManifest] = []
        for day in sorted(os.scandir(company), key=lambda e: e.name, reverse=True):
            if not day.is_dir(follow_symlinks=False) or not AS_OF.fullmatch(day.name):
                continue
            same_day: list[ReportManifest] = []
            for run in sorted(os.scandir(day.path), key=lambda e: e.name):
                if run.is_dir(follow_symlinks=False):
                    manifest = self._manifest_at(Path(run.path))
                    if manifest is not None and manifest.nse_symbol == symbol:
                        same_day.append(manifest)
            same_day.sort(key=lambda m: (m.box14_generated_at, str(m.artifact_id)),
                          reverse=True)
            found.extend(same_day)
            if len(found) >= MAX_LISTED:
                return found[:MAX_LISTED]
        return found
