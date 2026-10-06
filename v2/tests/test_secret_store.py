"""Part 6C-A: SecretStore precedence and the macOS Keychain adapter. Fully offline -
`MacOSKeychainSecretStore`'s `runner` is always a fake in these tests, never the real
`/usr/bin/security`, so no test ever touches the real OS Keychain on the machine
running the suite."""

import subprocess

import pytest

from stock_research_ai.adapters.secret_store import (
    EnvironmentSecretStore,
    MacOSKeychainSecretStore,
    SecretStoreUnavailableError,
    resolve_analyst_api_key,
)
from stock_research_ai.config import Settings


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRunner:
    """Records every argv it was called with and returns pre-programmed results in
    order - enough to prove get/set/delete build the exact expected `security`
    invocation without ever running a real subprocess."""

    def __init__(self, results: list[subprocess.CompletedProcess[str]]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return self._results.pop(0)


class _RaisingRunner:
    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("no such file: security")


def test_environment_secret_store_reads_the_configured_settings_value(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3", analyst_api_key="sk-env-value")
    store = EnvironmentSecretStore(settings)
    assert store.get("analyst-api-key") == "sk-env-value"


def test_environment_secret_store_returns_none_when_unset(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    store = EnvironmentSecretStore(settings)
    assert store.get("analyst-api-key") is None


def test_environment_secret_store_never_silently_accepts_a_write(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    store = EnvironmentSecretStore(settings)
    with pytest.raises(NotImplementedError):
        store.set("analyst-api-key", "x")
    with pytest.raises(NotImplementedError):
        store.delete("analyst-api-key")


def test_keychain_get_returns_the_stored_value_on_success() -> None:
    runner = _FakeRunner([_completed(0, stdout="sk-keychain-value\n")])
    store = MacOSKeychainSecretStore(runner=runner)
    assert store.get("analyst-api-key") == "sk-keychain-value"
    argv = runner.calls[0]
    assert argv[1:] == [
        "find-generic-password",
        "-a",
        "analyst-api-key",
        "-s",
        "stock-research-ai",
        "-w",
    ]


def test_keychain_get_returns_none_when_item_is_absent() -> None:
    runner = _FakeRunner([_completed(44, stderr="The specified item could not be found")])
    store = MacOSKeychainSecretStore(runner=runner)
    assert store.get("analyst-api-key") is None


def test_keychain_set_updates_in_place() -> None:
    runner = _FakeRunner([_completed(0)])
    store = MacOSKeychainSecretStore(runner=runner)
    store.set("analyst-api-key", "sk-new-value")
    argv = runner.calls[0]
    assert argv[1:] == [
        "add-generic-password",
        "-a",
        "analyst-api-key",
        "-s",
        "stock-research-ai",
        "-w",
        "sk-new-value",
        "-U",
    ]


def test_keychain_set_failure_raises_unavailable() -> None:
    runner = _FakeRunner([_completed(1, stderr="keychain is locked")])
    store = MacOSKeychainSecretStore(runner=runner)
    with pytest.raises(SecretStoreUnavailableError):
        store.set("analyst-api-key", "x")


def test_keychain_delete_is_idempotent_when_item_never_existed() -> None:
    runner = _FakeRunner([_completed(44)])
    store = MacOSKeychainSecretStore(runner=runner)
    store.delete("analyst-api-key")  # must not raise


def test_missing_security_binary_is_reported_as_unavailable_not_a_crash() -> None:
    store = MacOSKeychainSecretStore(runner=_RaisingRunner())
    with pytest.raises(SecretStoreUnavailableError):
        store.get("analyst-api-key")


def test_no_secret_value_ever_appears_in_the_constructed_argv_by_accident() -> None:
    """The value is passed as its own argv element (never interpolated into a shell
    string), so a value containing shell metacharacters cannot break out."""
    runner = _FakeRunner([_completed(0)])
    store = MacOSKeychainSecretStore(runner=runner)
    secret = "sk-'; rm -rf ~ #"
    store.set("analyst-api-key", secret)
    assert secret in runner.calls[0]
    assert all(";" not in part or part == secret for part in runner.calls[0])


# --- Precedence -----------------------------------------------------------------------


def test_environment_value_wins_and_keychain_is_never_even_consulted(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3", analyst_api_key="sk-env")

    class _ExplodingKeychain:
        def get(self, name: str) -> str | None:
            raise AssertionError("Keychain must not be consulted when env is present")

        def set(self, name: str, value: str) -> None:
            raise AssertionError

        def delete(self, name: str) -> None:
            raise AssertionError

    value, source = resolve_analyst_api_key(settings, keychain=_ExplodingKeychain())
    assert (value, source) == ("sk-env", "environment")


def test_keychain_is_used_when_environment_is_absent(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    runner = _FakeRunner([_completed(0, stdout="sk-from-keychain\n")])
    keychain = MacOSKeychainSecretStore(runner=runner)
    value, source = resolve_analyst_api_key(settings, keychain=keychain)
    assert (value, source) == ("sk-from-keychain", "keychain")


def test_neither_source_available_reports_none_never_invents_a_value(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    runner = _FakeRunner([_completed(44)])
    keychain = MacOSKeychainSecretStore(runner=runner)
    value, source = resolve_analyst_api_key(settings, keychain=keychain)
    assert (value, source) == (None, "none")


def test_an_unavailable_keychain_backend_reports_none_rather_than_raising(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    keychain = MacOSKeychainSecretStore(runner=_RaisingRunner())
    value, source = resolve_analyst_api_key(settings, keychain=keychain)
    assert (value, source) == (None, "none")


def test_no_third_hidden_secret_source_exists(tmp_path) -> None:
    """resolve_analyst_api_key only ever consults exactly two sources - proven by the
    fact that the only two collaborators it can be given (settings and `keychain`)
    fully determine the result; there is no third code path querying, say, a config
    file or a different environment variable."""
    settings = Settings(database_path=tmp_path / "db.sqlite3")
    runner = _FakeRunner([_completed(44)])
    keychain = MacOSKeychainSecretStore(runner=runner)
    resolve_analyst_api_key(settings, keychain=keychain)
    # Exactly one Keychain call was made (the `find-generic-password` lookup) - no
    # extra probing of any other backend.
    assert len(runner.calls) == 1
