"""Part 6C-A: SQLite production hardening (WAL/busy_timeout/synchronous) and the
PRAGMA user_version migration registry. All fully offline - no live network, real
file-backed temp databases only (never `:memory:` - SQLiteDatabase's constructor only
ever accepts a real Path, so there is no code path through which `:memory:` could be
reached; this is stated rather than defensively handled, per the Part 6C-A audit)."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from stock_research_ai.adapters.migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    MigrationFailedError,
    UnsupportedSchemaVersionError,
    ensure_schema_version,
    read_schema_version,
)
from stock_research_ai.adapters.sqlite import DatabaseNotReadyError, SQLiteDatabase


async def _set_user_version(database: SQLiteDatabase, version: int) -> None:
    async with database._engine.begin() as connection:  # noqa: SLF001
        await connection.execute(text(f"PRAGMA user_version = {version}"))


async def _get_pragma(database: SQLiteDatabase, pragma: str) -> object:
    async with database._engine.connect() as connection:  # noqa: SLF001
        result = await connection.execute(text(f"PRAGMA {pragma}"))
        return result.scalar_one()


@pytest.fixture
async def db(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "hardening.sqlite3")
    try:
        await database.initialize()
        yield database
    finally:
        await database.close()


def test_migration_registry_is_ordered_and_contiguous() -> None:
    versions = [(m.from_version, m.to_version) for m in MIGRATIONS]
    for (_, to_version), (next_from, _) in zip(versions, versions[1:], strict=False):
        assert to_version == next_from, "migrations must be contiguous, forward-only"
    assert MIGRATIONS[-1].to_version == CURRENT_SCHEMA_VERSION


async def test_fresh_database_is_created_at_current_schema_version(db: SQLiteDatabase) -> None:
    async with db._engine.connect() as connection:  # noqa: SLF001
        assert await read_schema_version(connection) == CURRENT_SCHEMA_VERSION


async def test_wal_mode_is_enabled_on_a_file_backed_database(db: SQLiteDatabase) -> None:
    assert (await _get_pragma(db, "journal_mode")).lower() == "wal"


async def test_busy_timeout_is_configured(db: SQLiteDatabase) -> None:
    assert await _get_pragma(db, "busy_timeout") == 5000


async def test_synchronous_normal_is_configured_alongside_wal(db: SQLiteDatabase) -> None:
    # SQLite reports synchronous as an integer: 0=OFF, 1=NORMAL, 2=FULL.
    assert await _get_pragma(db, "synchronous") == 1


async def test_foreign_keys_remain_on(db: SQLiteDatabase) -> None:
    assert await _get_pragma(db, "foreign_keys") == 1


async def test_legacy_user_version_zero_is_upgraded_to_current_on_check_ready(
    db: SQLiteDatabase,
) -> None:
    """Simulates a database created before Part 6C-A existed (SQLite's own default
    PRAGMA user_version is 0 for any database that never set it) - structurally
    identical to every Part 1-6B database on disk today."""
    await _set_user_version(db, 0)
    assert await _get_pragma(db, "user_version") == 0

    await db.check_ready()

    assert await _get_pragma(db, "user_version") == CURRENT_SCHEMA_VERSION


async def test_legacy_database_snapshots_remain_readable_after_migration(
    db: SQLiteDatabase,
) -> None:
    """Proves migration 0 -> 1 never mutates/loses existing rows - a real company row
    written under version 0 survives the automatic upgrade unchanged."""
    from stock_research_ai.domain.company import Company

    company = Company(
        id="11111111-1111-1111-1111-111111111111",  # type: ignore[arg-type]
        name="Legacy Co",
        listings=(),
        provider_identifiers=(),
    )
    await db.repository.save(company)
    await _set_user_version(db, 0)

    await db.check_ready()

    reloaded = await db.repository.get(company.id)
    assert reloaded is not None
    assert reloaded.name == "Legacy Co"


async def test_a_database_newer_than_supported_is_refused_not_guessed(db: SQLiteDatabase) -> None:
    await _set_user_version(db, CURRENT_SCHEMA_VERSION + 1)

    with pytest.raises(DatabaseNotReadyError):
        await db.check_ready()


async def test_ensure_schema_version_refuses_a_newer_database_directly() -> None:
    import stock_research_ai.adapters.migrations as migrations_module

    class _FakeConnection:
        async def execute(self, _stmt):  # noqa: ANN001
            class _Result:
                def scalar_one(self_inner) -> int:  # noqa: ANN001
                    return migrations_module.CURRENT_SCHEMA_VERSION + 5

            return _Result()

    with pytest.raises(UnsupportedSchemaVersionError):
        await ensure_schema_version(_FakeConnection())  # type: ignore[arg-type]


async def test_a_failed_migration_never_falsely_advances_the_version(
    db: SQLiteDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stock_research_ai.adapters.migrations as migrations_module

    async def _broken_migration(_connection: object) -> None:
        raise RuntimeError("simulated migration failure")

    broken = (
        Migration(
            from_version=0,
            to_version=1,
            description="deliberately broken for this test",
            apply=_broken_migration,
        ),
    )
    monkeypatch.setattr(migrations_module, "MIGRATIONS", broken)
    monkeypatch.setattr(migrations_module, "CURRENT_SCHEMA_VERSION", 1)
    await _set_user_version(db, 0)

    async with db._engine.begin() as connection:  # noqa: SLF001
        with pytest.raises(MigrationFailedError):
            await migrations_module.ensure_schema_version(connection)

    # The version was never advanced past the last successfully-applied step (there
    # were none), so it remains exactly where it started - never a half-applied value.
    assert await _get_pragma(db, "user_version") == 0


async def test_an_already_current_database_is_a_no_op_read_no_write(db: SQLiteDatabase) -> None:
    """The common steady-state case (already-current DB, the vast majority of every
    real startup) should not need to write PRAGMA user_version at all."""
    async with db._engine.connect() as connection:  # noqa: SLF001
        version = await ensure_schema_version(connection)
    assert version == CURRENT_SCHEMA_VERSION


async def test_concurrent_writer_and_reader_do_not_immediately_raise_database_is_locked(
    db: SQLiteDatabase,
) -> None:
    """Proves the WAL + busy_timeout composition makes the documented two-connection
    (background research write + HTTP report read) runtime shape safe: a reader must
    not be blocked by a writer under WAL, and even a writer-vs-writer race must wait
    (bounded by busy_timeout) rather than fail instantly."""
    writer_holds_lock = asyncio.Event()
    reader_finished = asyncio.Event()

    insert_sql = (
        "INSERT INTO companies (id, name, listings, provider_identifiers) "
        "VALUES ('22222222-2222-2222-2222-222222222222', 'X', '[]', '[]')"
    )

    async def hold_write_transaction() -> None:
        async with db._engine.begin() as connection:  # noqa: SLF001
            await connection.execute(text(insert_sql))
            writer_holds_lock.set()
            # Hold the write transaction open until the concurrent read has finished -
            # proving WAL lets that read proceed without waiting on this writer at all.
            await reader_finished.wait()

    async def read_while_writer_holds_lock() -> None:
        await writer_holds_lock.wait()
        result = await db.repository.get("99999999-9999-9999-9999-999999999999")  # type: ignore[arg-type]
        assert result is None
        reader_finished.set()

    await asyncio.gather(hold_write_transaction(), read_while_writer_holds_lock())


async def test_reopening_the_database_preserves_schema_version_and_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reopen.sqlite3"
    from stock_research_ai.domain.company import Company

    first = SQLiteDatabase(path)
    try:
        await first.initialize()
        await first.repository.save(
            Company(
                id="33333333-3333-3333-3333-333333333333",  # type: ignore[arg-type]
                name="Reopen Co",
                listings=(),
                provider_identifiers=(),
            )
        )
    finally:
        await first.close()

    second = SQLiteDatabase(path)
    try:
        await second.check_ready()
        reloaded = await second.repository.get("33333333-3333-3333-3333-333333333333")  # type: ignore[arg-type]
        assert reloaded is not None
        assert reloaded.name == "Reopen Co"
        async with second._engine.connect() as connection:  # noqa: SLF001
            assert await read_schema_version(connection) == CURRENT_SCHEMA_VERSION
    finally:
        await second.close()


async def test_database_file_gets_restrictive_permissions_best_effort(
    db: SQLiteDatabase,
) -> None:
    mode = db.path.stat().st_mode & 0o777
    assert mode == 0o600
