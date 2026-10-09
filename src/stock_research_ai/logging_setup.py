"""Part 6C-A: production local logging for `serve-http`. Stdlib only - no new
dependency. A bounded, rotating, user-local file (never repo-relative, never shipped
anywhere) that structured application code (job/report failure classification, the
pairing rate limiter, etc.) can log to safely.

Deliberately NOT wired into every CLI one-shot command (`extract-document`,
`calculate-market`, ...) - those already print their own clear stdout/stderr output and
have their own existing tests; only the long-running `serve-http` process gets this
rotating file handler, per the Part 6C-A scope.
"""

import logging
import logging.handlers
import os
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from stock_research_ai.private_files import PRIVATE_FILE_MODE, ensure_private_dir

#: Small, bounded defaults: five 2 MiB files (~10 MiB total) is comfortably enough for
#: weeks of a single local user's `serve-http` activity without unbounded growth.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5

_LOGGER_NAME = "stock_research_ai"

#: Matches an `Authorization: Bearer <token>` header value regardless of case/spacing -
#: a structural, header-shaped pattern, never a generic word, so it can never mangle
#: ordinary research prose (e.g. a company's "Company Secretary" signatory line).
_AUTHORIZATION_HEADER_PATTERN = re.compile(r"(?i)authorization:\s*bearer\s+\S+")

#: P2-7: secret values that become known while the process runs (a Keychain-loaded API
#: key, an issued bearer token or pairing code) are registered here and redacted by every
#: RedactingFilter from then on. Too-short values are ignored so a blank or trivial value
#: can never redact ordinary text; the registry is bounded (oldest dropped first).
_MIN_REGISTERED_SECRET_LENGTH = 8
_MAX_REGISTERED_SECRETS = 64
_registered_secrets: dict[str, None] = {}
_registry_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Redact `value` from all service logs written by this process from now on."""
    if not value or len(value) < _MIN_REGISTERED_SECRET_LENGTH:
        return
    with _registry_lock:
        _registered_secrets.pop(value, None)
        _registered_secrets[value] = None
        while len(_registered_secrets) > _MAX_REGISTERED_SECRETS:
            del _registered_secrets[next(iter(_registered_secrets))]


def _registered() -> tuple[str, ...]:
    with _registry_lock:
        return tuple(_registered_secrets)


class RedactingFilter(logging.Filter):
    """Redacts only:
      (a) the exact, currently-configured secret value(s) passed to it at
          construction time (e.g. the real Anthropic API key actually in use),
      (b) the exact values registered via `register_secret` while running, and
      (c) the structural `Authorization: Bearer <token>` header shape.

    Never redacts on a generic keyword like "secret" or "token" - doing so would
    corrupt ordinary research text that happens to contain a word like "Secretary"
    (a real, legitimate company-filing signatory title), which is exactly the failure
    mode this design explicitly avoids.
    """

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._known_secrets = tuple(s for s in known_secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(str(record.msg))
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(self._redact_value(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._redact_value(v) for k, v in record.args.items()}
        return True

    def _redact_value(self, value: object) -> object:
        return self._redact(value) if isinstance(value, str) else value

    def _redact(self, text: str) -> str:
        # Longest first, so a secret that contains another is replaced whole.
        for secret in sorted({*self._known_secrets, *_registered()}, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        return _AUTHORIZATION_HEADER_PATTERN.sub("Authorization: Bearer [REDACTED]", text)


class _RedactingFormatter(logging.Formatter):
    """Redacts the final line too: exception tracebacks and stack info are formatted
    after filters run, so a secret in an exception message would otherwise escape."""

    def __init__(self, redactor: RedactingFilter, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor._redact(super().format(record))


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Every log file it opens - the first and each one after a rotation - is created
    0600, never created with the umask default and narrowed later."""

    def _open(self) -> Any:
        def opener(path: str, flags: int) -> int:
            return os.open(path, flags, PRIVATE_FILE_MODE)

        return open(  # noqa: SIM115 - the handler owns and closes this stream
            self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
            opener=opener,
        )


def configure_logging(
    log_path: Path,
    known_secrets: Iterable[str] = (),
    level: int = logging.INFO,
) -> logging.Logger:
    """Idempotent: safe to call more than once (e.g. across repeated test setup) -
    clears any handlers this function previously attached before adding a fresh one,
    rather than accumulating duplicate handlers/log lines."""
    ensure_private_dir(log_path.parent)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        if getattr(handler, "_stock_research_ai_managed", False):
            logger.removeHandler(handler)

    handler = _PrivateRotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler._stock_research_ai_managed = True  # type: ignore[attr-defined]
    redactor = RedactingFilter(known_secrets)
    handler.setFormatter(
        _RedactingFormatter(
            redactor,
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(redactor)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass  # best-effort: not every filesystem/OS supports POSIX permission bits
    return logger


def get_logger(component: str) -> logging.Logger:
    """A child of the one configured `stock_research_ai` logger, named per component
    (e.g. "stock_research_ai.jobs", "stock_research_ai.pairing") so log lines are
    attributable without every call site re-deriving the same setup."""
    return logging.getLogger(_LOGGER_NAME).getChild(component)
