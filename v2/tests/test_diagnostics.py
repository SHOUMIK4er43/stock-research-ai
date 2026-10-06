"""Part 6C-A: `diagnostics` reports only safe, non-secret production state. Fully
offline - real temp-file databases, no live network, no real Keychain access (the
secret store is always exercised via resolve_analyst_api_key's `keychain` injection
point, never the real MacOSKeychainSecretStore default)."""

import stock_research_ai.diagnostics as diagnostics_module
from stock_research_ai.adapters.migrations import CURRENT_SCHEMA_VERSION
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.diagnostics import format_diagnostics, gather_diagnostics


class _NoCredentialKeychain:
    def get(self, name: str) -> str | None:
        return None

    def set(self, name: str, value: str) -> None:
        raise AssertionError

    def delete(self, name: str) -> None:
        raise AssertionError


async def test_diagnostics_on_a_freshly_initialized_database(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
    )
    await initialize_database(settings)

    result = await gather_diagnostics(settings, secret_store=_NoCredentialKeychain())

    assert result.database_reachable is True
    assert result.database_schema_version == CURRENT_SCHEMA_VERSION
    assert result.database_schema_supported is True
    assert result.credential_source == "none"
    assert result.analyst_provider_configured is False
    assert result.pairing_configured is False


async def test_diagnostics_reports_environment_configured_credential(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
        analyst_provider="anthropic",
        analyst_api_key="sk-test-value",
    )
    await initialize_database(settings)

    result = await gather_diagnostics(settings)

    assert result.credential_source == "environment"
    assert result.analyst_provider_configured is True


async def test_diagnostics_never_includes_the_actual_secret_value(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
        analyst_provider="anthropic",
        analyst_api_key="sk-must-never-appear",
    )
    await initialize_database(settings)

    result = await gather_diagnostics(settings)
    rendered = format_diagnostics(result)

    assert "sk-must-never-appear" not in rendered
    assert "sk-must-never-appear" not in str(vars(result))


async def test_diagnostics_never_includes_pairing_token(tmp_path) -> None:
    from stock_research_ai.adapters.http.browser_auth import PairingStore

    auth_path = tmp_path / "auth.json"
    settings = Settings(database_path=tmp_path / "db.sqlite3", browser_auth_path=auth_path)
    await initialize_database(settings)
    store = PairingStore(auth_path)
    code = store.issue_pairing_code()
    token = store.complete_pairing(
        "chrome-extension://abcdefghijklmnopabcdefghijklmnop", code
    )

    result = await gather_diagnostics(settings, secret_store=_NoCredentialKeychain())
    rendered = format_diagnostics(result)

    assert token not in rendered
    assert code not in rendered
    assert result.pairing_configured is True


async def test_diagnostics_on_an_uninitialized_database_reports_unreachable(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "never-created.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
    )
    result = await gather_diagnostics(settings, secret_store=_NoCredentialKeychain())
    assert result.database_reachable is False
    assert result.database_schema_version is None


async def test_diagnostics_reports_the_configured_http_bind(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "db.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
        http_host="127.0.0.1",
        http_port=8123,
    )
    await initialize_database(settings)
    result = await gather_diagnostics(settings, secret_store=_NoCredentialKeychain())
    assert result.http_host == "127.0.0.1"
    assert result.http_port == 8123


def test_format_diagnostics_produces_stable_key_value_lines(tmp_path) -> None:
    diag = diagnostics_module.Diagnostics(
        application_version="0.1.0",
        api_version="v1",
        report_schema_version="6A.1",
        database_path=tmp_path / "db.sqlite3",
        database_reachable=True,
        database_schema_version=1,
        database_schema_supported=True,
        http_host="127.0.0.1",
        http_port=8000,
        analyst_provider_configured=False,
        credential_source="none",
        analyst_model="claude-sonnet-5",
        pairing_configured=False,
        log_path=tmp_path / "logs" / "service.log",
        artifact_path=tmp_path / "artifacts",
        browser_auth_path=tmp_path / "auth.json",
    )
    rendered = format_diagnostics(diag)
    assert "application_version: 0.1.0" in rendered
    assert "credential_source: none" in rendered
    assert "database_schema_supported: True" in rendered
