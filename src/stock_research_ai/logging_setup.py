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
from collections.abc import Iterable
from pathlib import Path

#: Small, bounded defaults: five 2 MiB files (~10 MiB total) is comfortably enough for
#: weeks of a single local user's `serve-http` activity without unbounded growth.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5

_LOGGER_NAME = "stock_research_ai"

#: Matches an `Authorization: Bearer <token>` header value regardless of case/spacing -
#: a structural, header-shaped pattern, never a generic word, so it can never mangle
#: ordinary research prose (e.g. a company's "Company Secretary" signatory line).
_AUTHORIZATION_HEADER_PATTERN = re.compile(r"(?i)authorization:\s*bearer\s+\S+")


class RedactingFilter(logging.Filter):
    """Redacts only:
      (a) the exact, currently-configured secret value(s) passed to it at
          construction time (e.g. the real Anthropic API key actually in use), and
      (b) the structural `Authorization: Bearer <token>` header shape.

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
        for secret in self._known_secrets:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        return _AUTHORIZATION_HEADER_PATTERN.sub("Authorization: Bearer [REDACTED]", text)


def configure_logging(
    log_path: Path,
    known_secrets: Iterable[str] = (),
    level: int = logging.INFO,
) -> logging.Logger:
    """Idempotent: safe to call more than once (e.g. across repeated test setup) -
    clears any handlers this function previously attached before adding a fresh one,
    rather than accumulating duplicate handlers/log lines."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    for handler in list(logger.handlers):
        if getattr(handler, "_stock_research_ai_managed", False):
            logger.removeHandler(handler)

    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler._stock_research_ai_managed = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    handler.addFilter(RedactingFilter(known_secrets))
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
