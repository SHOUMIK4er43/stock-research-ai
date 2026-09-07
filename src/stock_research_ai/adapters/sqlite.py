"""Explicitly initialized SQLite persistence for the company registry."""

from pathlib import Path
from uuid import UUID

from sqlalchemy import JSON, String, inspect
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stock_research_ai.domain.company import (
    Company,
    ExchangeListing,
    ProviderIdentifier,
)


class DatabaseNotReadyError(RuntimeError):
    """The database is absent, unreadable, or lacks the required schema."""


class _Base(DeclarativeBase):
    pass


class _CompanyRow(_Base):
    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    listings: Mapped[list[dict[str, str | None]]] = mapped_column(JSON, nullable=False)
    provider_identifiers: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)


class SQLiteCompanyRepository:
    """Map companies to rows, with a separate session for each operation."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save(self, company: Company) -> None:
        row = _CompanyRow(
            id=str(company.id),
            name=company.name,
            listings=[listing.model_dump(mode="json") for listing in company.listings],
            provider_identifiers=[
                identifier.model_dump(mode="json") for identifier in company.provider_identifiers
            ],
        )
        async with self._sessions() as session, session.begin():
            session.add(row)

    async def get(self, company_id: UUID) -> Company | None:
        async with self._sessions() as session:
            row = await session.get(_CompanyRow, str(company_id))
            if row is None:
                return None
            return Company(
                id=UUID(row.id),
                name=row.name,
                listings=tuple(ExchangeListing.model_validate(listing) for listing in row.listings),
                provider_identifiers=tuple(
                    ProviderIdentifier.model_validate(identifier)
                    for identifier in row.provider_identifiers
                ),
            )


def _check_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table("companies"):
        raise DatabaseNotReadyError(
            "The SQLite database has no companies table. Run `stock-research-ai init-db` first."
        )
    actual_columns = {column["name"] for column in inspector.get_columns("companies")}
    missing_columns = set(_CompanyRow.__table__.columns.keys()) - actual_columns
    if missing_columns:
        raise DatabaseNotReadyError(
            "The SQLite companies table has an incompatible schema; missing columns: "
            f"{', '.join(sorted(missing_columns))}. "
            "Use a new database or migrate the existing database explicitly."
        )


class SQLiteDatabase:
    """Own the engine and repository without opening a database on construction."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        # Existing-file mode prevents ordinary application connections from creating
        # a database. initialize() is the sole operation that creates the file.
        url = URL.create(
            "sqlite+aiosqlite",
            database=self.path.as_uri(),
            query={"mode": "rw", "uri": "true"},
        )
        self._engine = create_async_engine(url)
        sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._repository = SQLiteCompanyRepository(sessions)

    @property
    def repository(self) -> SQLiteCompanyRepository:
        return self._repository

    async def initialize(self) -> None:
        """Create the data directory and initial schema only on explicit request."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.touch(exist_ok=False)
        except FileExistsError:
            pass
        async with self._engine.begin() as connection:
            await connection.run_sync(_Base.metadata.create_all)
            await connection.run_sync(_check_schema)

    async def check_ready(self) -> None:
        """Validate an existing database without creating a file or any tables."""
        if not self.path.is_file():
            raise DatabaseNotReadyError(
                f"SQLite database does not exist at {self.path}. "
                "Run `stock-research-ai init-db` first."
            )
        try:
            async with self._engine.connect() as connection:
                await connection.run_sync(_check_schema)
        except SQLAlchemyError as exc:
            raise DatabaseNotReadyError(
                f"Unable to read the SQLite database at {self.path}."
            ) from exc

    async def close(self) -> None:
        await self._engine.dispose()
