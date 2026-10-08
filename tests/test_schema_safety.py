"""R2-A safety proofs: disposable files, exact public entry points, no live services."""

import asyncio
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from stock_research_ai.__main__ import main
from stock_research_ai.adapters import migrations
from stock_research_ai.adapters.migrations import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    MigrationFailedError,
    UnsupportedSchemaVersionError,
)
from stock_research_ai.adapters.sqlite import DatabaseNotReadyError, SQLiteDatabase
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.diagnostics import gather_diagnostics


class NoSecrets:
    def get(self, name):
        return None

    def set(self, *args):
        pytest.fail("No credential writes")

    def delete(self, *args):
        pytest.fail("No credential writes")


def settings_at(tmp_path):
    return Settings(
        _env_file=None,
        database_path=tmp_path / "proof.sqlite3",
        browser_auth_path=tmp_path / "auth.json",
        analyst_provider=None,
    )


def snapshot(path):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        dump = tuple(connection.iterdump())
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    return version, dump, mode


async def seed(settings, version, *, complete=True):
    if complete:
        await initialize_database(settings)
    with closing(sqlite3.connect(settings.database_path)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("CREATE TABLE preservation_probe (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO preservation_probe VALUES (1, 'unchanged')")
        if complete:
            connection.execute(
                "INSERT INTO companies (id, name, listings, provider_identifiers) "
                "VALUES ('11111111-1111-1111-1111-111111111111', 'Preserve', '[]', '[]')"
            )
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()


async def initialize_via(entry, settings, monkeypatch):
    if entry == "bootstrap":
        await initialize_database(settings)
    elif entry == "cli":
        monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(settings.database_path))
        await asyncio.to_thread(main, ["init-db"])
    else:
        database = SQLiteDatabase(settings.database_path)
        try:
            await database.initialize()
        finally:
            await database.close()


@pytest.mark.parametrize("entry", ["direct", "bootstrap", "cli"])
@pytest.mark.parametrize("complete", [True, False])
async def test_future_initialize_refuses_and_preserves_version_rows_and_tables(
    tmp_path, monkeypatch, entry, complete
):
    settings = settings_at(tmp_path)
    future = CURRENT_SCHEMA_VERSION + 9
    await seed(settings, future, complete=complete)
    before = snapshot(settings.database_path)
    caught = None
    try:
        await initialize_via(entry, settings, monkeypatch)
    except UnsupportedSchemaVersionError as exc:
        caught = exc
    after = snapshot(settings.database_path)
    # Assert persisted state first: the pre-fix failure proves an actual downgrade.
    assert after[0] == future
    assert after[1] == before[1]
    assert caught is not None


@pytest.mark.parametrize("version", [0, CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION + 9])
async def test_diagnostics_observes_version_without_schema_row_or_journal_writes(tmp_path, version):
    settings = settings_at(tmp_path)
    await seed(settings, version)
    before = snapshot(settings.database_path)
    before_bytes = settings.database_path.read_bytes()
    result = await gather_diagnostics(settings, secret_store=NoSecrets())
    after = snapshot(settings.database_path)
    assert after[0] == version
    assert after == before
    assert settings.database_path.read_bytes() == before_bytes
    assert result.database_reachable is True
    assert result.database_schema_version == version
    assert result.database_schema_supported is (version <= CURRENT_SCHEMA_VERSION)


@pytest.mark.parametrize("entry", ["direct", "bootstrap", "cli"])
async def test_new_initialize_establishes_current_version(tmp_path, monkeypatch, entry):
    settings = settings_at(tmp_path)
    await initialize_via(entry, settings, monkeypatch)
    assert snapshot(settings.database_path)[0] == CURRENT_SCHEMA_VERSION
    database = SQLiteDatabase(settings.database_path)
    try:
        await database.check_ready()
    finally:
        await database.close()


@pytest.mark.parametrize("version", [0, CURRENT_SCHEMA_VERSION])
async def test_initialize_legacy_and_current_preserves_existing_data(tmp_path, version):
    settings = settings_at(tmp_path)
    await seed(settings, version)
    before = snapshot(settings.database_path)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.upper())

    event.listen(Engine, "before_cursor_execute", record)
    try:
        await initialize_database(settings)
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    after = snapshot(settings.database_path)
    assert after[0] == CURRENT_SCHEMA_VERSION
    assert after[1] == before[1]
    if version == CURRENT_SCHEMA_VERSION:
        assert not any("PRAGMA USER_VERSION =" in statement for statement in statements)


async def test_failed_initialize_migration_does_not_advance_version(tmp_path, monkeypatch):
    settings = settings_at(tmp_path)
    await seed(settings, 0)
    before = snapshot(settings.database_path)

    async def broken(connection):
        from sqlalchemy import text

        await connection.execute(text("UPDATE preservation_probe SET value='rolled back'"))
        raise RuntimeError("offline migration failure")

    monkeypatch.setattr(migrations, "MIGRATIONS", (Migration(0, 1, "test failure", broken),))
    with pytest.raises(MigrationFailedError):
        await initialize_database(settings)
    after = snapshot(settings.database_path)
    assert after[:2] == before[:2]


async def test_check_ready_future_refusal_preserves_rows_and_version(tmp_path):
    settings = settings_at(tmp_path)
    await seed(settings, CURRENT_SCHEMA_VERSION + 9)
    before = snapshot(settings.database_path)
    database = SQLiteDatabase(settings.database_path)
    try:
        with pytest.raises(DatabaseNotReadyError) as exc:
            await database.check_ready()
        assert isinstance(exc.value.__cause__, UnsupportedSchemaVersionError)
    finally:
        await database.close()
    assert snapshot(settings.database_path)[:2] == before[:2]


@pytest.mark.parametrize("state", ["missing", "invalid", "empty"])
async def test_diagnostics_reachability_and_no_ownership_creation(tmp_path, state):
    settings = settings_at(tmp_path)
    if state == "invalid":
        settings.database_path.write_bytes(b"not a SQLite database")
    elif state == "empty":
        with closing(sqlite3.connect(settings.database_path)):
            pass
    before = settings.database_path.read_bytes() if settings.database_path.exists() else None
    result = await gather_diagnostics(settings, secret_store=NoSecrets())
    assert result.database_reachable is (state == "empty")
    assert result.database_schema_version == (0 if state == "empty" else None)
    assert result.database_schema_supported is (state == "empty")
    after = settings.database_path.read_bytes() if settings.database_path.exists() else None
    assert after == before


async def test_diagnostics_never_calls_readiness_or_migration(tmp_path, monkeypatch):
    settings = settings_at(tmp_path)
    await seed(settings, 0)

    async def forbidden(*args):
        pytest.fail("Observational diagnostics must not enter readiness/migration")

    monkeypatch.setattr(SQLiteDatabase, "check_ready", forbidden)
    monkeypatch.setattr(migrations, "MIGRATIONS", (Migration(0, 1, "forbidden", forbidden),))
    result = await gather_diagnostics(settings, secret_store=NoSecrets())
    assert result.database_schema_version == 0


def test_diagnostics_cli_does_not_migrate_legacy_database(tmp_path, monkeypatch, capsys):
    settings = settings_at(tmp_path)
    asyncio.run(seed(settings, 0))
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(settings.database_path))
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(settings.browser_auth_path))
    monkeypatch.setattr(
        "stock_research_ai.diagnostics.resolve_analyst_api_key", lambda *args: (None, "none")
    )
    main(["diagnostics"])
    output = capsys.readouterr().out
    assert snapshot(settings.database_path)[0] == 0
    assert "database_reachable: True" in output
    assert "database_schema_version: 0" in output
    assert "database_schema_supported: True" in output


async def test_diagnostics_reads_committed_wal_without_checkpoint_or_version_write(tmp_path):
    settings = settings_at(tmp_path)
    await seed(settings, CURRENT_SCHEMA_VERSION)
    with closing(sqlite3.connect(settings.database_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("PRAGMA user_version=0")
        writer.execute("UPDATE preservation_probe SET value='committed in WAL'")
        writer.commit()
        wal_path = settings.database_path.with_name(settings.database_path.name + "-wal")
        before_db = settings.database_path.read_bytes()
        before_wal = wal_path.read_bytes()
        result = await gather_diagnostics(settings, secret_store=NoSecrets())
        assert result.database_reachable is True
        assert result.database_schema_version == 0
        assert result.database_schema_supported is True
        assert settings.database_path.read_bytes() == before_db
        assert wal_path.read_bytes() == before_wal
        assert writer.execute("PRAGMA user_version").fetchone()[0] == 0
        assert writer.execute("SELECT value FROM preservation_probe").fetchone()[0] == (
            "committed in WAL"
        )


async def test_read_only_connection_rejects_version_and_row_writes(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    settings = settings_at(tmp_path)
    await seed(settings, 0)
    before = snapshot(settings.database_path)
    database = SQLiteDatabase(settings.database_path, read_only=True)
    try:
        assert await database.inspect_schema_version() == 0
        async with database._engine.connect() as connection:
            for sql in ("PRAGMA user_version=1", "UPDATE preservation_probe SET value='forbidden'"):
                with pytest.raises(SQLAlchemyError, match="readonly"):
                    await connection.execute(text(sql))
    finally:
        await database.close()
    assert snapshot(settings.database_path) == before


async def test_read_only_initialize_refuses_without_creating_file(tmp_path):
    database = SQLiteDatabase(tmp_path / "absent" / "new.sqlite3", read_only=True)
    try:
        with pytest.raises(DatabaseNotReadyError, match="read-only"):
            await database.initialize()
        assert not database.path.parent.exists()
    finally:
        await database.close()


@pytest.mark.parametrize("version", [0, CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION + 9])
async def test_http_startup_retains_migration_and_future_refusal(tmp_path, version, monkeypatch):
    import httpx

    from stock_research_ai.interfaces.http import create_app

    settings = settings_at(tmp_path)
    await seed(settings, version)
    before = snapshot(settings.database_path)
    monkeypatch.setattr(
        "stock_research_ai.adapters.secret_store.MacOSKeychainSecretStore", NoSecrets
    )
    app = create_app(settings=settings)
    if version > CURRENT_SCHEMA_VERSION:
        with pytest.raises(DatabaseNotReadyError):
            async with app.router.lifespan_context(app):
                pytest.fail("Future schema must prevent startup")
        assert snapshot(settings.database_path)[:2] == before[:2]
    else:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
            ) as client:
                assert (await client.get("/health")).json() == {"status": "ok"}
        assert snapshot(settings.database_path)[0] == CURRENT_SCHEMA_VERSION
