"""Explicitly initialized SQLite persistence for the company registry."""

from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import JSON, ForeignKey, String, event, inspect, select, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stock_research_ai.domain.company import (
    Company,
    ExchangeListing,
    Listing,
    ProviderIdentifier,
    SourceCompanyIdentity,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.provenance import RawArtifact, SourceRetrieval


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
            await session.flush()
            await _backfill_company(session, row)

    async def get(self, company_id: UUID) -> Company | None:
        async with self._sessions() as session:
            row = await session.get(_CompanyRow, str(company_id))
            if row is None:
                return None
            listing_rows = (
                await session.scalars(
                    select(_ListingRow).where(_ListingRow.company_id == str(company_id))
                )
            ).all()
            projected = list(row.listings)
            for stored in listing_rows:
                listing = Listing.model_validate(stored.payload)
                entry = {
                    "exchange": listing.exchange,
                    "symbol": listing.symbol or listing.exchange_identifier,
                }
                if entry not in projected:
                    projected.append(entry)
            return Company(
                id=UUID(row.id),
                name=row.name,
                listings=tuple(ExchangeListing.model_validate(listing) for listing in projected),
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

    for table in _Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            raise DatabaseNotReadyError(
                "Part 2 schema is absent. Run `stock-research-ai init-db` first."
            )
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        if set(table.columns.keys()) - columns:
            raise DatabaseNotReadyError(f"Incompatible schema in {table.name}")


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

        @event.listens_for(self._engine.sync_engine, "connect")
        def enable_foreign_keys(connection: Any, record: object) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._sessions = sessions
        self._repository = SQLiteCompanyRepository(sessions)
        self.extractions = SQLiteExtractionRepository(sessions)

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
        async with self._sessions() as session, session.begin():
            for row in (await session.scalars(select(_CompanyRow))).all():
                await _backfill_company(session, row)

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


class _ListingRow(_Base):
    __tablename__ = "listings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class _SourceKeyRow(_Base):
    __tablename__ = "company_source_keys"
    provider: Mapped[str] = mapped_column(String, primary_key=True)
    source_key: Mapped[str] = mapped_column(String, primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)


class _RetrievalRow(_Base):
    __tablename__ = "source_retrievals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class _ArtifactRow(_Base):
    __tablename__ = "raw_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    retrieval_id: Mapped[str] = mapped_column(ForeignKey("source_retrievals.id"), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class _ExtractionRow(_Base):
    __tablename__ = "extractions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), index=True)
    provider: Mapped[str] = mapped_column(String, index=True)
    reporting_scope: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    schema_version: Mapped[str] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class SQLiteExtractionRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                _RetrievalRow(id=str(retrieval.id), payload=retrieval.model_dump(mode="json"))
            )
            await session.flush()
            if artifact is not None:
                session.add(
                    _ArtifactRow(
                        id=str(artifact.id),
                        retrieval_id=str(retrieval.id),
                        payload=artifact.model_dump(mode="json"),
                    )
                )

    async def bind_company(self, identity: SourceCompanyIdentity) -> UUID:
        async with self._sessions() as session, session.begin():
            # Serialize key creation in SQLite, including concurrent first extractions.
            await session.execute(text("BEGIN IMMEDIATE"))
            key = await session.get(_SourceKeyRow, (identity.provider, identity.source_company_key))
            if key is not None:
                company_id = UUID(key.company_id)
            else:
                company_id = identity.company_id
                session.add(
                    _CompanyRow(
                        id=str(company_id),
                        name=identity.name,
                        listings=[],
                        provider_identifiers=[
                            {
                                "provider": identity.provider,
                                "identifier": identity.source_company_key,
                            }
                        ],
                    )
                )
                await session.flush()
                session.add(
                    _SourceKeyRow(
                        provider=identity.provider,
                        source_key=identity.source_company_key,
                        company_id=str(company_id),
                    )
                )
            for listing in identity.listings:
                identifier = listing.symbol or listing.exchange_identifier
                listing_id = uuid5(NAMESPACE_URL, f"{company_id}:{listing.exchange}:{identifier}")
                if await session.get(_ListingRow, str(listing_id)) is None:
                    bound = listing.model_copy(update={"id": listing_id, "company_id": company_id})
                    session.add(
                        _ListingRow(
                            id=str(listing_id),
                            company_id=str(company_id),
                            payload=bound.model_dump(mode="json"),
                        )
                    )
            return company_id

    async def save_extraction(self, result: CompanyExtractionResult) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                _ExtractionRow(
                    id=str(result.metadata.id),
                    company_id=str(result.company.company_id) if result.company else None,
                    provider=result.metadata.provider,
                    reporting_scope=result.company.reporting_scope.value
                    if result.company
                    else "unknown",
                    status=result.coverage.overall,
                    schema_version=result.schema_version,
                    created_at=result.metadata.created_at.isoformat(),
                    payload=result.model_dump(mode="json"),
                )
            )

    async def get_extraction(self, extraction_id: UUID) -> CompanyExtractionResult | None:
        async with self._sessions() as session:
            row = await session.get(_ExtractionRow, str(extraction_id))
            return CompanyExtractionResult.model_validate(row.payload) if row else None


async def _backfill_company(session: AsyncSession, row: _CompanyRow) -> None:
    """Promote only explicitly registered identifiers; never infer missing exchanges."""
    for entry in row.listings:
        exchange, symbol = entry.get("exchange"), entry.get("symbol")
        if not exchange or not symbol:
            continue
        listing_id = uuid5(NAMESPACE_URL, f"{row.id}:{exchange}:{symbol}")
        if await session.get(_ListingRow, str(listing_id)) is None:
            listing = Listing(
                id=listing_id, company_id=UUID(row.id), exchange=exchange, symbol=symbol
            )
            session.add(
                _ListingRow(
                    id=str(listing_id), company_id=row.id, payload=listing.model_dump(mode="json")
                )
            )
    for identifier in row.provider_identifiers:
        key = (identifier["provider"], identifier["identifier"])
        existing = await session.get(_SourceKeyRow, key)
        if existing is not None and existing.company_id != row.id:
            raise ValueError(f"Provider identity {key!r} is already bound to another company")
        if existing is None:
            session.add(_SourceKeyRow(provider=key[0], source_key=key[1], company_id=row.id))
