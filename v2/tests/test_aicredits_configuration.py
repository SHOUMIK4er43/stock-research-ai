"""Provider selection and separate credentials; no real Keychain calls."""

import subprocess

import pytest

from stock_research_ai.__main__ import main
from stock_research_ai.adapters.secret_store import (
    MacOSKeychainSecretStore,
    resolve_analyst_api_key,
    store_analyst_api_key_in_keychain,
)
from stock_research_ai.bootstrap import live_analyst_model
from stock_research_ai.config import Settings
from stock_research_ai.diagnostics import format_diagnostics, gather_diagnostics


class MemorySecrets:
    def __init__(self, values=None):
        self.values = values or {}
        self.lookups = []

    def get(self, name):
        self.lookups.append(name)
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)


@pytest.mark.parametrize("model", ["catalog-model-a", "catalog-model-b"])
async def test_explicit_selection_and_configurable_model(monkeypatch, model):
    from stock_research_ai.adapters.aicredits.analyst import AICreditsAnalystModel

    monkeypatch.setenv("STOCK_RESEARCH_AI_AICREDITS_MODEL", model)
    monkeypatch.setenv("STOCK_RESEARCH_AI_AICREDITS_API_KEY", "fake-ai-key")
    settings = Settings(_env_file=None, analyst_provider="aicredits", analyst_api_key="other-key")
    store = MemorySecrets()
    adapter, kind = await live_analyst_model(settings, store)
    assert isinstance(adapter, AICreditsAnalystModel)
    assert adapter._model == model
    assert adapter._api_key == "fake-ai-key"
    assert kind == "LIVE"
    assert store.lookups == []
    assert "fake-ai-key" not in repr(settings)


@pytest.mark.parametrize(
    "provider,other", [("aicredits", "analyst_api_key"), ("anthropic", "aicredits_api_key")]
)
async def test_selected_missing_key_fails_without_cross_provider_fallback(provider, other):
    settings = Settings(
        _env_file=None,
        analyst_provider=provider,
        aicredits_model="configured",
        **{other: "other-provider-key"},
    )
    store = MemorySecrets()
    with pytest.raises(RuntimeError, match="credential"):
        await live_analyst_model(settings, store)
    assert store.lookups == ["aicredits-api-key" if provider == "aicredits" else "analyst-api-key"]


async def test_missing_model_fails_explicitly():
    settings = Settings(_env_file=None, analyst_provider="aicredits", aicredits_api_key="test")
    with pytest.raises(RuntimeError, match="AICREDITS_MODEL"):
        await live_analyst_model(settings, MemorySecrets())


async def test_aicredits_key_alone_does_not_select_provider():
    settings = Settings(_env_file=None, aicredits_api_key="test", aicredits_model="model")
    assert await live_analyst_model(settings, MemorySecrets()) == (None, None)


async def test_legacy_anthropic_keychain_default_preserved():
    from stock_research_ai.adapters.anthropic.analyst import AnthropicAnalystModel

    adapter, kind = await live_analyst_model(
        Settings(_env_file=None), MemorySecrets({"analyst-api-key": "test"})
    )
    assert isinstance(adapter, AnthropicAnalystModel)
    assert kind == "LIVE"


def test_keychain_separate_slot_and_env_precedence():
    store = MemorySecrets({"analyst-api-key": "anthropic-key"})
    store_analyst_api_key_in_keychain("ai-key", store, provider="aicredits")
    settings = Settings(_env_file=None, analyst_provider="aicredits")
    assert resolve_analyst_api_key(settings, store) == ("ai-key", "keychain")
    assert store.lookups == ["aicredits-api-key"]
    store.lookups.clear()
    settings = Settings(_env_file=None, analyst_provider="aicredits", aicredits_api_key="env-key")
    assert resolve_analyst_api_key(settings, store) == ("env-key", "environment")
    assert store.lookups == []
    assert store.values["analyst-api-key"] == "anthropic-key"


def test_platform_keychain_slot_roundtrip_with_fake_runner():
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "ai-key\n", "")

    keychain = MacOSKeychainSecretStore(runner=runner)
    store_analyst_api_key_in_keychain("ai-key", keychain, provider="aicredits")
    assert resolve_analyst_api_key(
        Settings(_env_file=None, analyst_provider="aicredits"), keychain
    ) == ("ai-key", "keychain")
    assert all(c[c.index("-a") + 1] == "aicredits-api-key" for c in calls)


def test_cli_provider_configuration_is_hidden_and_separate(monkeypatch, capsys):
    store = MemorySecrets()
    monkeypatch.setattr(
        "stock_research_ai.adapters.secret_store.MacOSKeychainSecretStore", lambda: store
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt: "p1-cli-test-key")
    main(["configure-analyst", "--provider", "aicredits"])
    assert store.values == {"aicredits-api-key": "p1-cli-test-key"}
    output = capsys.readouterr().out
    assert "p1-cli-test-key" not in output
    assert "STOCK_RESEARCH_AI_AICREDITS_API_KEY" in output
    assert "STOCK_RESEARCH_AI_ANALYST_PROVIDER=aicredits" in output


@pytest.mark.parametrize("model,configured", [("catalog-model", True), (None, False)])
async def test_diagnostics_selected_provider_safe_offline(tmp_path, model, configured):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "missing.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
        analyst_provider="aicredits",
        aicredits_api_key="diagnostic-test-key",
        aicredits_model=model,
    )
    report = await gather_diagnostics(settings, MemorySecrets())
    assert report.analyst_provider == "aicredits"
    assert report.analyst_provider_configured is configured
    assert report.credential_source == "environment"
    assert report.analyst_model == (model or "")
    text = format_diagnostics(report)
    assert "analyst_provider: aicredits" in text
    assert "diagnostic-test-key" not in text
    assert not settings.database_path.exists()


def test_keychain_failure_traceback_does_not_disclose_value():
    import traceback

    from stock_research_ai.adapters.secret_store import SecretStoreUnavailableError

    secret = "p1-sensitive-keychain-value"

    def runner(argv):
        raise subprocess.TimeoutExpired(argv, 10, stderr=secret)

    with pytest.raises(SecretStoreUnavailableError) as exc:
        store_analyst_api_key_in_keychain(
            secret, MacOSKeychainSecretStore(runner=runner), provider="aicredits"
        )
    assert secret not in "".join(traceback.format_exception(exc.value))


async def test_unknown_provider_refused_before_credential_lookup():
    store = MemorySecrets()
    with pytest.raises(RuntimeError, match="Unsupported"):
        await live_analyst_model(Settings(_env_file=None, analyst_provider="unknown"), store)
    assert store.lookups == []
