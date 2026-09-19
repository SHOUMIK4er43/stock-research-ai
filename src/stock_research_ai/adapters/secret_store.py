"""Separate analyst credentials, outside research storage and browser pairing.

The explicitly selected provider uses its own environment setting first, then its
own account under the stock-research-ai Keychain service. An unset selector keeps
the legacy Anthropic account; an AI Credits key never implicitly selects AI Credits.
No plaintext-file fallback or cross-provider credential lookup is performed.
"""

import shutil
import subprocess
from collections.abc import Callable
from typing import Literal, Protocol

from stock_research_ai.config import Settings

#: The exact (account, service) pair the Keychain item is filed under. `service` is
#: namespaced to this application so it can never collide with, or be confused for,
#: an unrelated Keychain item another tool created.
_KEYCHAIN_SERVICE = "stock-research-ai"
_ANALYST_API_KEY_ACCOUNT = "analyst-api-key"
_AICREDITS_API_KEY_ACCOUNT = "aicredits-api-key"


def _account(provider: str) -> str:
    if provider == "anthropic":
        return _ANALYST_API_KEY_ACCOUNT
    if provider == "aicredits":
        return _AICREDITS_API_KEY_ACCOUNT
    raise ValueError("Unsupported analyst provider")


CredentialSource = Literal["environment", "keychain", "none"]


class SecretStoreUnavailableError(RuntimeError):
    """The secret store backend could not be reached at all (e.g. `/usr/bin/security`
    is absent on this platform, or the Keychain is locked) - distinct from the secret
    simply not being set. Callers treat both as "no credential" for research purposes,
    but diagnostics should be able to tell them apart."""


class SecretStore(Protocol):
    """The only shape a secret backend may take. Deliberately narrow: get/set/delete
    of a single named secret, nothing more - no listing, no bulk export."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class EnvironmentSecretStore:
    """Read-only: the environment is owned by the process's own launcher (a shell,
    `.env`, CI), never mutated by this application. `set`/`delete` intentionally raise
    rather than silently no-op, so a caller can never be misled into thinking a write
    succeeded."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get(self, name: str) -> str | None:
        if name == _ANALYST_API_KEY_ACCOUNT and self._settings.analyst_api_key is not None:
            return self._settings.analyst_api_key.get_secret_value()
        if name == _AICREDITS_API_KEY_ACCOUNT and self._settings.aicredits_api_key is not None:
            return self._settings.aicredits_api_key.get_secret_value()
        return None

    def set(self, name: str, value: str) -> None:
        raise NotImplementedError("EnvironmentSecretStore is read-only by design.")

    def delete(self, name: str) -> None:
        raise NotImplementedError("EnvironmentSecretStore is read-only by design.")


class MacOSKeychainSecretStore:
    """Stores secrets in the current user's macOS login Keychain via the platform's
    own built-in `/usr/bin/security` CLI - no third-party dependency. Every value
    round-trips through a subprocess argv list, never a shell string, so no value is
    ever exposed to shell interpretation.

    `runner` is injectable so tests can prove behavior (including "Keychain
    unavailable") without touching a real Keychain or requiring macOS at all.
    """

    def __init__(
        self,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._run = runner or self._default_runner

    @staticmethod
    def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)

    def _security_path(self) -> str:
        path = shutil.which("security") or "/usr/bin/security"
        return path

    def get(self, name: str) -> str | None:
        argv = [
            self._security_path(),
            "find-generic-password",
            "-a",
            name,
            "-s",
            _KEYCHAIN_SERVICE,
            "-w",
        ]
        try:
            result = self._run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretStoreUnavailableError(
                "macOS Keychain is unavailable (`/usr/bin/security` could not be run)."
            ) from exc
        if result.returncode != 0:
            # `security` exits non-zero both for "not found" and for other failures
            # (e.g. a locked keychain); V1 does not need to distinguish those for
            # research purposes - either way there is no usable credential here.
            return None
        value = result.stdout.strip("\n")
        return value or None

    def set(self, name: str, value: str) -> None:
        argv = [
            self._security_path(),
            "add-generic-password",
            "-a",
            name,
            "-s",
            _KEYCHAIN_SERVICE,
            "-w",
            value,
            "-U",  # update in place if an item already exists, rather than erroring
        ]
        try:
            result = self._run(argv)
        except (OSError, subprocess.SubprocessError):
            raise SecretStoreUnavailableError(
                "macOS Keychain is unavailable (`/usr/bin/security` could not be run)."
            ) from None
        if result.returncode != 0:
            raise SecretStoreUnavailableError(
                "Failed to store the credential in Keychain."
            )

    def delete(self, name: str) -> None:
        argv = [
            self._security_path(),
            "delete-generic-password",
            "-a",
            name,
            "-s",
            _KEYCHAIN_SERVICE,
        ]
        try:
            result = self._run(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretStoreUnavailableError(
                "macOS Keychain is unavailable (`/usr/bin/security` could not be run)."
            ) from exc
        if result.returncode != 0:
            # Deleting an item that never existed is not an error for our purposes.
            return


def resolve_analyst_api_key(
    settings: Settings,
    keychain: SecretStore | None = None,
) -> tuple[str | None, CredentialSource]:
    """The one place secret precedence is decided. Never returns a value alongside
    "none", and never consults more than one source per call - the environment is
    checked first and, if present, the Keychain is never even touched."""
    provider = settings.analyst_provider or "anthropic"
    if provider not in ("anthropic", "aicredits"):
        return None, "none"
    account = _account(provider)
    environment = EnvironmentSecretStore(settings)
    value = environment.get(account)
    if value:
        return value, "environment"
    store = keychain if keychain is not None else MacOSKeychainSecretStore()
    try:
        value = store.get(account)
    except SecretStoreUnavailableError:
        return None, "none"
    if value:
        return value, "keychain"
    return None, "none"


def store_analyst_api_key_in_keychain(
    value: str, keychain: SecretStore | None = None, *, provider: str = "anthropic"
) -> None:
    """The one place `configure-analyst` (and its tests) writes the key - keeps the
    exact account/service naming an internal detail of this module rather than a
    constant other modules need to import directly."""
    store = keychain if keychain is not None else MacOSKeychainSecretStore()
    store.set(_account(provider), value)


__all__ = [
    "CredentialSource",
    "EnvironmentSecretStore",
    "MacOSKeychainSecretStore",
    "SecretStore",
    "SecretStoreUnavailableError",
    "resolve_analyst_api_key",
    "store_analyst_api_key_in_keychain",
]
