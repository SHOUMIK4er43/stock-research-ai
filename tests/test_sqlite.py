"""SQLite integration tests using only temporary database files."""

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from stock_research_ai.adapters.sqlite import DatabaseNotReadyError, SQLiteDatabase
from stock_research_ai.domain.company import Company, ExchangeListing, ProviderIdentifier


@pytest.mark.asyncio
async def test_constructor_and_readiness_do_not_create_database(tmp_path: Path) -> None:
    path = tmp_path / "missing-directory" / "companies.sqlite3"
    database = SQLiteDatabase(path)
    try:
        assert not path.parent.exists()
        with pytest.raises(DatabaseNotReadyError, match="init-db"):
            await database.check_ready()
        assert not path.parent.exists()
    finally:
        await database.close()
    assert not path.exists()


@pytest.mark.asyncio
async def test_check_ready_does_not_create_missing_table(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    path.touch()
    database = SQLiteDatabase(path)
    try:
        with pytest.raises(DatabaseNotReadyError, match="no companies table"):
            await database.check_ready()
        with sqlite3.connect(path) as connection:
            assert (
                connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
                == []
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_check_ready_rejects_incompatible_schema(tmp_path: Path) -> None:
    path = tmp_path / "incompatible.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE companies (id TEXT PRIMARY KEY)")
    database = SQLiteDatabase(path)
    try:
        with pytest.raises(DatabaseNotReadyError, match="missing columns"):
            await database.check_ready()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_company_round_trip_preserves_nested_identifiers(
    database: SQLiteDatabase,
) -> None:
    company = Company(
        id=uuid4(),
        name="Example & Company",
        listings=(
            ExchangeListing(exchange="NSE", symbol="EXAMPLE"),
            ExchangeListing(exchange=None, symbol="UNVERIFIED"),
        ),
        provider_identifiers=(
            ProviderIdentifier(provider="example-source", identifier="source-company-42"),
        ),
    )
    await database.repository.save(company)
    assert await database.repository.get(company.id) == company
    assert await database.repository.get(uuid4()) is None


@pytest.mark.asyncio
async def test_separate_database_instances_share_persisted_companies(
    tmp_path: Path,
) -> None:
    # URI-reserved characters must remain part of the filename.
    path = tmp_path / "nested" / "companies #1?.sqlite3"
    writer = SQLiteDatabase(path)
    company = Company(id=uuid4(), name="Persistent Company")
    try:
        await writer.initialize()
        await writer.repository.save(company)
        await writer.initialize()
        assert await writer.repository.get(company.id) == company
    finally:
        await writer.close()

    reader = SQLiteDatabase(path)
    try:
        await reader.check_ready()
        assert await reader.repository.get(company.id) == company
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_failed_write_rolls_back_without_poisoning_next_operation(
    database: SQLiteDatabase,
) -> None:
    original = Company(id=uuid4(), name="Original")
    await database.repository.save(original)
    duplicate = Company(id=original.id, name="Duplicate")
    with pytest.raises(IntegrityError):
        await database.repository.save(duplicate)

    subsequent = Company(id=uuid4(), name="Subsequent")
    await database.repository.save(subsequent)
    assert await database.repository.get(original.id) == original
    assert await database.repository.get(subsequent.id) == subsequent


@pytest.mark.asyncio
async def test_concurrent_calls_keep_sessions_separate(database: SQLiteDatabase) -> None:
    companies = tuple(Company(id=uuid4(), name=f"Company {index}") for index in range(8))
    await asyncio.gather(*(database.repository.save(company) for company in companies))
    loaded = await asyncio.gather(*(database.repository.get(company.id) for company in companies))
    assert loaded == list(companies)


@pytest.mark.asyncio
async def test_separate_database_files_remain_isolated(
    database: SQLiteDatabase, tmp_path: Path
) -> None:
    other_database = SQLiteDatabase(tmp_path / "other.sqlite3")
    company = Company(id=uuid4(), name="Isolated Company")
    try:
        await other_database.initialize()
        await database.repository.save(company)
        assert await other_database.repository.get(company.id) is None
    finally:
        await other_database.close()
