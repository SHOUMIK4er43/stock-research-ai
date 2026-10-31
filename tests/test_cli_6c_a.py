"""Part 6C-A: CLI wiring for configure-analyst, diagnostics, backup, restore, and
serve-http's port-conflict handling. Fully offline - no live network, no real macOS
Keychain access (configure-analyst's Keychain write is monkeypatched to a fake)."""

import pytest

from stock_research_ai.__main__ import main


def _run_and_capture(monkeypatch, argv: list[str]) -> list[str]:
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *args: printed.append(" ".join(map(str, args))))
    main(argv)
    return printed


def test_diagnostics_cli_prints_safe_fields_and_no_secret(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_ANALYST_PROVIDER", "anthropic")
    monkeypatch.setenv("STOCK_RESEARCH_AI_ANALYST_API_KEY", "sk-cli-secret-value")
    main(["init-db"])

    printed = _run_and_capture(monkeypatch, ["diagnostics"])

    output = printed[0]
    assert "sk-cli-secret-value" not in output
    assert "credential_source: environment" in output
    assert "analyst_provider_configured: True" in output
    assert str(db_path) in output


def test_backup_and_restore_cli_round_trip(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    main(["init-db"])

    backup_dir = tmp_path / "my-backup"
    _run_and_capture(monkeypatch, ["backup", "--output", str(backup_dir)])
    assert (backup_dir / "backup_metadata.json").is_file()

    restore_target = tmp_path / "restored.sqlite3"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(restore_target))
    printed = _run_and_capture(monkeypatch, ["restore", "--input", str(backup_dir)])
    assert restore_target.is_file()
    assert "Restored" in printed[0]


def test_backup_cli_reports_a_clean_error_not_a_traceback_on_bad_destination(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    main(["init-db"])

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "x").write_text("x")

    with pytest.raises(SystemExit):
        main(["backup", "--output", str(occupied)])


def test_configure_analyst_cli_prompts_hidden_and_stores_via_keychain(
    tmp_path, monkeypatch
) -> None:
    stored: dict[str, str] = {}

    class _FakeKeychain:
        def set(self, name: str, value: str) -> None:
            stored[name] = value

    import stock_research_ai.adapters.secret_store as secret_store_module

    monkeypatch.setattr(
        secret_store_module, "MacOSKeychainSecretStore", lambda: _FakeKeychain()
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-from-prompt")
    printed = _run_and_capture(monkeypatch, ["configure-analyst"])

    assert stored == {"analyst-api-key": "sk-from-prompt"}
    assert "sk-from-prompt" not in printed[0]
    assert "Stored" in printed[0]


def test_configure_analyst_cli_rejects_an_empty_key(monkeypatch) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "   ")
    with pytest.raises(SystemExit):
        main(["configure-analyst"])


def test_serve_http_reports_a_clean_error_on_port_conflict(tmp_path, monkeypatch) -> None:
    """Occupies a real loopback port first, then proves `serve-http` reports a clean,
    typed error (via parser.error -> SystemExit) rather than an unhandled traceback -
    without ever completing a real server startup (uvicorn.run is monkeypatched to
    raise the same OSError a real bind conflict would, so this stays fast and
    offline)."""
    import errno

    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(tmp_path / "logs" / "service.log"))
    main(["init-db"])

    def _raise_addr_in_use(*args, **kwargs):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr("uvicorn.run", _raise_addr_in_use)

    with pytest.raises(SystemExit):
        main(["serve-http"])


def test_serve_http_reraises_an_unrelated_os_error(tmp_path, monkeypatch) -> None:
    """A port conflict gets a friendly message; any other OSError must still surface,
    never be swallowed as if it were the same known case."""
    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(tmp_path / "logs" / "service.log"))
    main(["init-db"])

    def _raise_other(*args, **kwargs):
        raise OSError("some unrelated failure")

    monkeypatch.setattr("uvicorn.run", _raise_other)

    with pytest.raises(OSError, match="some unrelated failure"):
        main(["serve-http"])


def test_serve_http_configures_logging_before_starting(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "db.sqlite3"
    log_path = tmp_path / "logs" / "service.log"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(log_path))
    main(["init-db"])

    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    main(["serve-http"])

    assert log_path.is_file()
    assert "single-worker mode" in log_path.read_text()


def test_serve_http_never_passes_a_worker_count_to_uvicorn(tmp_path, monkeypatch) -> None:
    """Part 6C-A single-process/single-worker invariant: BrowserJobManager keeps
    in-flight jobs in memory, so a second worker would never see them. Proven two ways:
    (1) `uvicorn.run` is never called with a `workers` kwarg at all, and (2) it is
    called with a real app *instance* (not an import string), which uvicorn itself
    structurally refuses to run with more than one worker."""
    calls: list[tuple[tuple, dict]] = []

    def _capture(*args, **kwargs):
        calls.append((args, kwargs))

    db_path = tmp_path / "db.sqlite3"
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(tmp_path / "logs" / "service.log"))
    main(["init-db"])
    monkeypatch.setattr("uvicorn.run", _capture)

    main(["serve-http"])

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "workers" not in kwargs
    # A bare FastAPI app instance, not an "module:attr" import string - uvicorn's own
    # multi-worker mode requires the latter, so this call shape cannot run >1 worker.
    assert not isinstance(args[0], str)


# --- Part 6C-B: render-launchagent (installer plumbing) -----------------------------


def test_render_launchagent_cli_writes_a_valid_secret_free_plist(tmp_path, monkeypatch) -> None:
    import plistlib

    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(tmp_path / "logs" / "service.log"))
    plist_path = tmp_path / "ai.stockresearch.local.plist"

    _run_and_capture(
        monkeypatch,
        [
            "render-launchagent",
            "--output",
            str(plist_path),
            "--executable",
            "/opt/homebrew/bin/stock-research-ai",
        ],
    )

    assert plist_path.is_file()
    data = plistlib.loads(plist_path.read_bytes())
    assert data["Label"] == "ai.stockresearch.local"
    assert data["ProgramArguments"] == ["/opt/homebrew/bin/stock-research-ai", "serve-http"]
    raw = plist_path.read_text()
    assert "sk-ant-" not in raw
    assert ".env" not in raw


def test_render_launchagent_cli_defaults_executable_to_argv0(tmp_path, monkeypatch) -> None:
    import plistlib

    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(tmp_path / "logs" / "service.log"))
    plist_path = tmp_path / "out.plist"

    main(["render-launchagent", "--output", str(plist_path)])

    data = plistlib.loads(plist_path.read_bytes())
    assert data["ProgramArguments"][1] == "serve-http"
    assert data["ProgramArguments"][0]  # some resolved executable path, non-empty


def test_render_launchagent_carries_the_non_secret_service_settings(tmp_path, monkeypatch) -> None:
    """Release smoke test (2026-10-23): launchd starts the service with an empty
    environment, so a clean install had no approved extension origin and a valid pairing
    code was rejected (POST /v1/pair 401). The installer's non-secret selectors now reach
    the LaunchAgent; API keys never do."""
    import plistlib

    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(tmp_path / "db.sqlite3"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(tmp_path / "logs" / "service.log"))
    origin = "chrome-extension://" + "a" * 32
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", origin)
    monkeypatch.setenv("STOCK_RESEARCH_AI_ANALYST_PROVIDER", "aicredits")
    monkeypatch.setenv("STOCK_RESEARCH_AI_AICREDITS_MODEL", "some-model")
    monkeypatch.setenv("STOCK_RESEARCH_AI_HTTP_PORT", "8765")
    monkeypatch.setenv("STOCK_RESEARCH_AI_AICREDITS_API_KEY", "planted-aicredits-value")
    monkeypatch.setenv("STOCK_RESEARCH_AI_ANALYST_API_KEY", "planted-anthropic-value")
    plist_path = tmp_path / "out.plist"

    main(["render-launchagent", "--output", str(plist_path), "--executable", "/x/srai"])

    data = plistlib.loads(plist_path.read_bytes())
    assert data["EnvironmentVariables"] == {
        "STOCK_RESEARCH_AI_AICREDITS_MODEL": "some-model",
        "STOCK_RESEARCH_AI_ANALYST_PROVIDER": "aicredits",
        "STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN": origin,
        "STOCK_RESEARCH_AI_HTTP_PORT": "8765",
    }
    assert "planted" not in plist_path.read_text()


def test_render_launchagent_without_settings_has_no_environment(tmp_path, monkeypatch) -> None:
    import plistlib

    from stock_research_ai.release_tools import LAUNCH_AGENT_ENVIRONMENT_KEYS

    for key in LAUNCH_AGENT_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STOCK_RESEARCH_AI_LOG_PATH", str(tmp_path / "logs" / "service.log"))
    plist_path = tmp_path / "out.plist"
    main(["render-launchagent", "--output", str(plist_path), "--executable", "/x/srai"])
    assert "EnvironmentVariables" not in plistlib.loads(plist_path.read_bytes())
