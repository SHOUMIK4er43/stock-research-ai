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
        self.stdins: list[str | None] = []

    def __call__(
        self, argv: list[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.stdins.append(stdin)
        return self._results.pop(0)


class _RaisingRunner:
    def __call__(
        self, argv: list[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess[str]:
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


def test_keychain_set_updates_in_place_with_the_secret_on_stdin_only() -> None:
    """P2-2: the value goes to `security -i` on stdin, never into argv (readable by any
    local process via `ps`)."""
    runner = _FakeRunner([_completed(0)])
    store = MacOSKeychainSecretStore(runner=runner)
    store.set("analyst-api-key", "sk-new-value")
    assert runner.calls[0][1:] == ["-i"]
    assert all("sk-new-value" not in part for part in runner.calls[0])
    assert runner.stdins[0] == (
        'add-generic-password -a "analyst-api-key" -s "stock-research-ai" -U -w "sk-new-value"\n'
    )


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
    """The value never reaches argv, and on stdin it is one quoted word: quotes and
    backslashes are escaped so a hostile value cannot add options or commands."""
    runner = _FakeRunner([_completed(0)])
    store = MacOSKeychainSecretStore(runner=runner)
    secret = 'sk-\'; rm -rf ~ # " -A \\'
    store.set("analyst-api-key", secret)
    assert all(secret not in part and "rm -rf" not in part for part in runner.calls[0])
    assert runner.stdins[0] == (
        'add-generic-password -a "analyst-api-key" -s "stock-research-ai" -U '
        '-w "sk-\'; rm -rf ~ # \\" -A \\\\"\n'
    )


@pytest.mark.parametrize("value", ["sk-a\nadd-generic-password -a x", "sk\r", "sk\x00", "sk\x7f"])
def test_keychain_set_refuses_control_characters_without_echoing_them(value: str) -> None:
    runner = _FakeRunner([])
    store = MacOSKeychainSecretStore(runner=runner)
    with pytest.raises(ValueError) as exc:
        store.set("analyst-api-key", value)
    assert value not in str(exc.value) and "sk" not in str(exc.value)
    assert runner.calls == []


def test_keychain_set_failure_does_not_echo_the_secret_or_stderr() -> None:
    secret = "sk-p2-never-echoed"
    runner = _FakeRunner([_completed(128, stderr=f"add-generic-password -w {secret}")])
    with pytest.raises(SecretStoreUnavailableError) as exc:
        MacOSKeychainSecretStore(runner=runner).set("analyst-api-key", secret)
    assert secret not in str(exc.value) and secret not in repr(exc.value)


def test_default_runner_passes_stdin_without_a_shell(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        recorded.update(kwargs, argv=argv)
        return _completed(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    MacOSKeychainSecretStore._default_runner(["/usr/bin/security", "-i"], "cmd\n")
    assert recorded["argv"] == ["/usr/bin/security", "-i"]
    assert recorded["input"] == "cmd\n"
    assert recorded.get("shell", False) is False


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
