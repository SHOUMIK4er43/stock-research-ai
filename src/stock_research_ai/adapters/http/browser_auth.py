"""User-local browser extension pairing/auth persistence (Part 6B-A).

A security domain completely separate from Anthropic/AnalystModel credentials
(config.py's analyst_* settings): this store never touches the research/report
database, is never committed (it lives under the user's home directory, matching
Settings.database_path's own convention), and no research endpoint can read or alter
it. Only a token's SHA-256 hash is ever persisted - never the raw token or pairing code.
"""

import hashlib
import hmac
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict

DEFAULT_PAIRING_CODE_TTL_SECONDS = 300


class PairingError(ValueError):
    """A typed, non-secret-leaking pairing failure (invalid/expired/reused code, or a
    non chrome-extension:// origin attempting to pair)."""


class _PendingPairing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code_hash: str
    expires_at: AwareDatetime
    used: bool = False


class _PairedExtension(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str
    token_hash: str
    paired_at: AwareDatetime


class _PairingFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    pending: _PendingPairing | None = None
    paired: _PairedExtension | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PairingStore:
    """Exactly one paired extension at a time (V1: a single local user running a single
    extension instance). Re-pairing replaces the previous paired record entirely - the
    old token stops verifying immediately."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> _PairingFile:
        if not self._path.exists():
            return _PairingFile()
        return _PairingFile.model_validate_json(self._path.read_text())

    def _write(self, data: _PairingFile) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(data.model_dump_json())
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass  # best-effort: not every filesystem/OS supports POSIX permission bits

    def issue_pairing_code(self, ttl_seconds: int = DEFAULT_PAIRING_CODE_TTL_SECONDS) -> str:
        """Generates a cryptographically random, short-lived, single-use pairing code
        and persists only its hash. Returns the code in plaintext exactly once - the
        one intentional exception to "never printed after successful pairing" (the user
        must receive this initial code through some out-of-band channel, e.g. a future
        CLI command)."""
        code = secrets.token_urlsafe(16)
        data = self._read()
        self._write(
            data.model_copy(
                update={
                    "pending": _PendingPairing(
                        code_hash=_hash(code),
                        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
                    )
                }
            )
        )
        return code

    def complete_pairing(self, origin: str, pairing_code: str) -> str:
        """Validates a one-time pairing code against the CURRENT request's own Origin
        (never a client-supplied body field - callers must pass the value read from the
        real HTTP Origin header) and, on success, replaces any previous paired extension
        with (origin, new token). Returns the new token in plaintext exactly once."""
        if not origin.startswith("chrome-extension://"):
            raise PairingError("Pairing is only permitted for a chrome-extension:// origin")
        data = self._read()
        pending = data.pending
        if pending is None or pending.used:
            raise PairingError("No pairing is currently pending")
        if datetime.now(UTC) > pending.expires_at:
            raise PairingError("Pairing code has expired")
        if not hmac.compare_digest(pending.code_hash, _hash(pairing_code)):
            raise PairingError("Invalid pairing code")
        token = secrets.token_urlsafe(32)
        self._write(
            _PairingFile(
                version=data.version,
                pending=pending.model_copy(update={"used": True}),
                paired=_PairedExtension(
                    origin=origin, token_hash=_hash(token), paired_at=datetime.now(UTC)
                ),
            )
        )
        return token

    def verify(self, origin: str | None, token: str | None) -> bool:
        """Both origin AND token must match the paired record - neither alone suffices."""
        if not origin or not token:
            return False
        paired = self._read().paired
        if paired is None:
            return False
        if origin != paired.origin:
            return False
        return hmac.compare_digest(paired.token_hash, _hash(token))

    def paired_origin(self) -> str | None:
        paired = self._read().paired
        return paired.origin if paired else None
