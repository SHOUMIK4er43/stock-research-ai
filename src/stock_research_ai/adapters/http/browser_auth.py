"""User-local browser extension pairing/auth persistence (Part 6B-A).

A security domain completely separate from Anthropic/AnalystModel credentials
(config.py's analyst_* settings): this store never touches the research/report
database, is never committed (it lives under the user's home directory, matching
Settings.database_path's own convention), and no research endpoint can read or alter
it. Only a token's SHA-256 hash is ever persisted - never the raw token or pairing code.

Token lifecycle (file version 2): every bearer carries a server-side `issued_at` /
`expires_at` (timezone-aware UTC, server clock only - nothing from the client is
trusted) and stops verifying at expiry; the default lifetime is 30 days. `revoke()` (CLI
`revoke-pairing`) erases both the paired and any pending record, so the bearer fails on
the very next request; re-pairing likewise replaces the previous token at once. A
pairing file from an older version (no expiry metadata) or one that cannot be read is
treated as "not paired": the old token fails and the user pairs again - it is never
granted a fresh lifetime.
"""

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from stock_research_ai.logging_setup import register_secret
from stock_research_ai.private_files import write_private_file

DEFAULT_PAIRING_CODE_TTL_SECONDS = 300
#: Bearer lifetime. Long enough that a single local user re-runs `pair-code` about once
#: a month, short enough that a token copied out of extension storage (or forgotten on
#: an old browser profile) does not stay valid indefinitely.
DEFAULT_TOKEN_TTL = timedelta(days=30)
_FILE_VERSION = 2

#: Part 6C-A pairing brute-force hardening, kept per attempted Origin. In-memory only (no
#: persisted state) - a restart clears every cooldown. It contains guessing of a
#: short-lived pairing code and stops failures from one Origin locking out another; it
#: cannot tell a local process forging the configured extension's Origin header from the
#: real extension (any local process can send any header). Lockout never affects an
#: already-paired token: `verify()` does not consult it.
_MAX_FAILED_PAIRING_ATTEMPTS = 5
_FAILED_ATTEMPT_WINDOW_SECONDS = 60.0
_COOLDOWN_SECONDS = 60.0
_MAX_TRACKED_ORIGINS = 32


class PairingError(ValueError):
    """A typed, non-secret-leaking pairing failure (invalid/expired/reused code, or a
    non chrome-extension:// origin attempting to pair)."""


class PairingLockedOutError(PairingError):
    """Too many failed pairing attempts happened recently; a short, self-clearing
    cooldown is in effect. Never a permanent lockout - the counter and cooldown are
    both purely in-memory and a service restart clears them entirely."""


class _PendingPairing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code_hash: str
    expires_at: AwareDatetime
    used: bool = False


class _PairedExtension(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str
    token_hash: str
    issued_at: AwareDatetime
    expires_at: AwareDatetime


class _PairingFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = _FILE_VERSION
    pending: _PendingPairing | None = None
    paired: _PairedExtension | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize_origin(origin: str) -> str:
    return origin.strip().lower()


@dataclass
class _FailureBucket:
    attempts: list[float] = field(default_factory=list)
    locked_out_until: float | None = None
    last_seen: float = 0.0


class PairingStore:
    """Exactly one paired extension at a time (V1: a single local user running a single
    extension instance). Re-pairing replaces the previous paired record entirely - the
    old token stops verifying immediately."""

    def __init__(
        self,
        path: Path,
        *,
        token_ttl: timedelta = DEFAULT_TOKEN_TTL,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if token_ttl <= timedelta(0):
            raise ValueError("token_ttl must be positive")
        self._path = path
        self._token_ttl = token_ttl
        self._now = now
        self._monotonic = monotonic or (lambda: time.monotonic())
        # Part 6C-A: in-memory brute-force state per attempted Origin - deliberately
        # never persisted (see the module constants). Bounded to _MAX_TRACKED_ORIGINS.
        self._failures: dict[str, _FailureBucket] = {}

    def _read(self) -> _PairingFile:
        """Fail closed: a missing, unreadable or older-version file reads as unpaired."""
        try:
            data = _PairingFile.model_validate_json(self._path.read_text())
        except (OSError, ValueError, ValidationError):
            return _PairingFile()
        return data if data.version == _FILE_VERSION else _PairingFile()

    def _write(self, data: _PairingFile) -> None:
        # Token hashes are never on disk wider than 0600, and never half-written.
        write_private_file(self._path, data.model_dump_json())

    def issue_pairing_code(self, ttl_seconds: int = DEFAULT_PAIRING_CODE_TTL_SECONDS) -> str:
        """Generates a cryptographically random, short-lived, single-use pairing code
        and persists only its hash. Returns the code in plaintext exactly once - the
        one intentional exception to "never printed after successful pairing" (the user
        must receive this initial code through some out-of-band channel, e.g. a future
        CLI command)."""
        code = secrets.token_urlsafe(16)
        register_secret(code)
        data = self._read()
        self._write(
            data.model_copy(
                update={
                    "pending": _PendingPairing(
                        code_hash=_hash(code),
                        expires_at=self._now() + timedelta(seconds=ttl_seconds),
                    )
                }
            )
        )
        return code

    def _check_not_locked_out(self, origin: str) -> None:
        bucket = self._failures.get(origin)
        if bucket is None or bucket.locked_out_until is None:
            return
        remaining = bucket.locked_out_until - self._monotonic()
        if remaining <= 0:
            del self._failures[origin]
            return
        raise PairingLockedOutError(
            f"Too many failed pairing attempts. Try again in {remaining:.0f}s."
        )

    def _record_failed_attempt(self, origin: str) -> None:
        now = self._monotonic()
        bucket = self._failures.get(origin)
        if bucket is None:
            if len(self._failures) >= _MAX_TRACKED_ORIGINS:
                # Evict the least recently seen origin that is not locked out right now;
                # if every tracked origin is locked out, the least recently seen one.
                def eviction_order(item: tuple[str, _FailureBucket]) -> tuple[bool, float]:
                    locked = (item[1].locked_out_until or 0) > now
                    return locked, item[1].last_seen

                del self._failures[min(self._failures.items(), key=eviction_order)[0]]
            bucket = self._failures[origin] = _FailureBucket()
        bucket.attempts = [
            t for t in bucket.attempts if now - t < _FAILED_ATTEMPT_WINDOW_SECONDS
        ]
        bucket.attempts.append(now)
        bucket.last_seen = now
        if len(bucket.attempts) >= _MAX_FAILED_PAIRING_ATTEMPTS:
            bucket.locked_out_until = now + _COOLDOWN_SECONDS

    def complete_pairing(self, origin: str, pairing_code: str) -> str:
        """Validates a one-time pairing code against the CURRENT request's own Origin
        (never a client-supplied body field - callers must pass the value read from the
        real HTTP Origin header) and, on success, replaces any previous paired extension
        with (origin, new token). Returns the new token in plaintext exactly once.

        Part 6C-A: raises PairingLockedOutError (never attempting validation at all)
        if too many attempts from this Origin have failed recently - see the module-level
        constants. A successful pairing clears this Origin's failure state."""
        bucket_key = _normalize_origin(origin)
        self._check_not_locked_out(bucket_key)
        try:
            if not origin.startswith("chrome-extension://"):
                raise PairingError("Pairing is only permitted for a chrome-extension:// origin")
            data = self._read()
            pending = data.pending
            if pending is None or pending.used:
                raise PairingError("No pairing is currently pending")
            if self._now() > pending.expires_at:
                raise PairingError("Pairing code has expired")
            if not hmac.compare_digest(pending.code_hash, _hash(pairing_code)):
                raise PairingError("Invalid pairing code")
        except PairingError:
            self._record_failed_attempt(bucket_key)
            raise
        token = secrets.token_urlsafe(32)
        register_secret(token)
        issued_at = self._now()
        self._write(
            _PairingFile(
                pending=pending.model_copy(update={"used": True}),
                paired=_PairedExtension(
                    origin=origin,
                    token_hash=_hash(token),
                    issued_at=issued_at,
                    expires_at=issued_at + self._token_ttl,
                ),
            )
        )
        self._failures.pop(bucket_key, None)
        return token

    def revoke(self) -> None:
        """Erase the paired extension and any pending pairing code. The previous bearer
        fails on its next request; nothing secret is left behind (only hashes were ever
        stored). Needs no provider credential."""
        self._write(_PairingFile())
        self._failures.clear()

    def verify(self, origin: str | None, token: str | None) -> bool:
        """Require the paired bearer; reject an explicitly different Origin.

        Chrome extension GETs can omit Origin. Its absence does not substitute for
        the bearer secret; pairing still requires an exact configured HTTP Origin.
        """
        if not token:
            return False
        paired = self._read().paired
        if paired is None:
            return False
        if origin is not None and origin != paired.origin:
            return False
        if self._now() >= paired.expires_at:
            return False
        if not hmac.compare_digest(paired.token_hash, _hash(token)):
            return False
        register_secret(token)  # only a valid bearer: callers cannot grow the registry
        return True

    def paired_origin(self) -> str | None:
        paired = self._read().paired
        return paired.origin if paired else None
