"""Explicitly initialized SQLite persistence for the company registry."""

import json
import os
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel
from sqlalchemy import JSON, ForeignKey, String, event, inspect, select, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stock_research_ai.adapters.migrations import (
    MigrationFailedError,
    UnsupportedSchemaVersionError,
    ensure_schema_version,
    read_schema_version,
    require_supported_schema_version,
)
from stock_research_ai.domain.analysis import AnalysisSnapshot
from stock_research_ai.domain.calculations import CalculationSnapshot
from stock_research_ai.domain.challenge import ChallengeSnapshot
from stock_research_ai.domain.company import (
    Company,
    ExchangeListing,
    Listing,
    ProviderIdentifier,
    SourceCompanyIdentity,
)
from stock_research_ai.domain.documents import (
    DocumentIdentity,
    DocumentRequest,
    DocumentSearchHit,
    ParsedDocument,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.macro import MacroSnapshot
from stock_research_ai.domain.market import MarketExtractionResult
from stock_research_ai.domain.market_calculations import MarketCalculationSnapshot
from stock_research_ai.domain.patterns import PatternSnapshot
from stock_research_ai.domain.provenance import RawArtifact, SourceRetrieval
from stock_research_ai.domain.publications import (
    PublicationHit,
    PublicationSnapshot,
)
from stock_research_ai.domain.report import ResearchReportSnapshot
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation
from stock_research_ai.domain.technicals import TechnicalSnapshot
from stock_research_ai.domain.valuation import ValuationSnapshot


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


async def _load_company(session: AsyncSession, company_id: UUID) -> Company | None:
    """Shared by SQLiteCompanyRepository.get and any in-transaction integrity check that
    needs the exact same reconstruction (so a re-hash matches what the engine saw)."""
    row = await session.get(_CompanyRow, str(company_id))
    if row is None:
        return None
    listing_rows = (
        await session.scalars(select(_ListingRow).where(_ListingRow.company_id == str(company_id)))
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
            ProviderIdentifier.model_validate(identifier) for identifier in row.provider_identifiers
        ),
    )


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
            return await _load_company(session, company_id)


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
                "Required schema is absent. Run `stock-research-ai init-db` first."
            )
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        if set(table.columns.keys()) - columns:
            raise DatabaseNotReadyError(f"Incompatible schema in {table.name}")


class SQLiteDatabase:
    """Own the engine and repository without opening a database on construction."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path.expanduser().resolve()
        self._read_only = read_only
        # Existing-file mode prevents ordinary application connections from creating
        # a database. initialize() is the sole operation that creates the file.
        url = URL.create(
            "sqlite+aiosqlite",
            database=self.path.as_uri(),
            query={"mode": "ro" if read_only else "rw", "uri": "true"},
        )
        self._engine = create_async_engine(url)

        @event.listens_for(self._engine.sync_engine, "connect")
        def configure_connection(connection: Any, record: object) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if read_only:
                # Connection-local settings only. WAL changes persistent journal
                # mode; diagnostics must observe the existing mode, not set it.
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()
                return
            # Part 6C-A production hardening. Applied to every connection this engine
            # ever opens - including both of the two independent SQLiteDatabase
            # instances the running HTTP process constructs (research_engine's and
            # browser_engine's), which is exactly the composition WAL is meant to make
            # safe: a background research write and an HTTP report read no longer
            # race into an immediate "database is locked" error.
            #
            # journal_mode=WAL: readers no longer block behind a writer (and vice
            # versa) - the two-connection composition documented since Part 6B-A
            # becomes safe for this single-user V1 without an engine-consolidation
            # refactor. WAL requires local-filesystem support for shared memory
            # mmap; production is documented as local-disk-only (never a network
            # filesystem), so this is never a concern here. WAL leaves `-wal`/`-shm`
            # sidecar files beside the main DB file - backup.py checkpoints
            # (TRUNCATE) before copying so a backup never needs those sidecars.
            #
            # busy_timeout=5000: a writer holding a transaction now makes a
            # contending connection wait up to 5s (SQLite's own retry loop) instead
            # of raising "database is locked" instantly. 5s comfortably exceeds any
            # single ORM write in this codebase (each `save_*` is one short
            # `session.begin()` block, never held open across a network/LLM await -
            # see docs/part6c-a-proof.md's transaction-duration audit) while staying
            # well under any human-perceptible delay for the rare contended case.
            #
            # synchronous=NORMAL: safe specifically *because* journal_mode=WAL is
            # already set - SQLite's own documentation guarantees WAL+NORMAL can
            # never corrupt the database, only (in the event of an OS-level crash or
            # power loss, not an ordinary application crash) lose the most recent
            # committed transaction(s) since the last checkpoint. For a local
            # single-user research tool that is not a financial ledger, this is an
            # acceptable, well-documented tradeoff for materially fewer fsyncs.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._sessions = sessions
        self._repository = SQLiteCompanyRepository(sessions)
        self.extractions = SQLiteExtractionRepository(sessions)
        self.documents = SQLiteDocumentRepository(sessions)
        self.market = SQLiteMarketRepository(sessions)
        self.context = SQLiteContextRepository(sessions)
        self.calculations = SQLiteCalculationRepository(sessions)
        self.patterns = SQLitePatternRepository(sessions)
        self.market_calculations = SQLiteMarketCalculationRepository(sessions)
        self.valuations = SQLiteValuationRepository(sessions)
        self.analyses = SQLiteAnalysisRepository(sessions)
        self.challenges = SQLiteChallengeRepository(sessions)
        self.reports = SQLiteReportRepository(sessions)

    @property
    def repository(self) -> SQLiteCompanyRepository:
        return self._repository

    async def initialize(self) -> None:
        """Create the data directory and initial schema only on explicit request."""
        if self._read_only:
            raise DatabaseNotReadyError("Cannot initialize through a read-only connection.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.touch(exist_ok=False)
        except FileExistsError:
            pass
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass  # best-effort: not every filesystem/OS supports POSIX permission bits
        async with self._engine.begin() as connection:
            # Guard before create_all: an unknown future schema must never be
            # normalized to this build's tables or version.
            require_supported_schema_version(await read_schema_version(connection))
            await connection.run_sync(_Base.metadata.create_all)
            await connection.run_sync(_check_schema)
            # Establish legacy ownership through the same forward-only registry
            # as readiness, and only after the required structure is present.
            await ensure_schema_version(connection)
        async with self._sessions() as session, session.begin():
            for row in (await session.scalars(select(_CompanyRow))).all():
                await _backfill_company(session, row)

    async def inspect_schema_version(self) -> int:
        """Observe version only, without checking table shape or running migrations.

        Use a read_only=True instance for diagnostics so connection setup also
        leaves persistent journal mode untouched. Version support is distinct from
        structural readiness; check_ready remains the startup gate.
        """
        if not self.path.is_file():
            raise DatabaseNotReadyError(f"SQLite database does not exist at {self.path}.")
        try:
            async with self._engine.connect() as connection:
                return await read_schema_version(connection)
        except SQLAlchemyError as exc:
            raise DatabaseNotReadyError(
                f"Unable to read the SQLite database at {self.path}."
            ) from exc

    async def check_ready(self) -> None:
        """Validate an existing database without creating a file or any tables, and -
        Part 6C-A - bring its PRAGMA user_version forward to CURRENT_SCHEMA_VERSION if
        it is an older (or never-versioned, i.e. legacy value 0) database. Never
        creates the file/initial tables (that remains initialize()'s sole job) and
        never touches a single research/report row - see adapters/migrations.py."""
        if not self.path.is_file():
            raise DatabaseNotReadyError(
                f"SQLite database does not exist at {self.path}. "
                "Run `stock-research-ai init-db` first."
            )
        try:
            async with self._engine.begin() as connection:
                await connection.run_sync(_check_schema)
                await ensure_schema_version(connection)
        except SQLAlchemyError as exc:
            raise DatabaseNotReadyError(
                f"Unable to read the SQLite database at {self.path}."
            ) from exc
        except (UnsupportedSchemaVersionError, MigrationFailedError) as exc:
            raise DatabaseNotReadyError(str(exc)) from exc

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


class _DocumentRow(_Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class _DocumentExtractionRow(_Base):
    __tablename__ = "document_extractions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    retrieval_id: Mapped[str] = mapped_column(ForeignKey("source_retrievals.id"), index=True)
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("raw_artifacts.id"))
    created_at: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class SQLiteDocumentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._evidence = SQLiteExtractionRepository(sessions)

    async def bind_document(self, request: DocumentRequest) -> DocumentIdentity:
        source_key = (
            ("key", request.source_document_key)
            if request.source_document_key
            else ("url", request.url)
        )
        document_id = uuid5(
            NAMESPACE_URL, json.dumps([str(request.company_id), request.provider, source_key])
        )
        async with self._sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            row = await session.get(_DocumentRow, str(document_id))
            if row:
                return DocumentIdentity.model_validate(row.payload)
            identity = DocumentIdentity(
                id=document_id,
                company_id=request.company_id,
                provider=request.provider,
                source_document_key=request.source_document_key,
                original_url=request.url,
            )
            session.add(
                _DocumentRow(
                    id=str(document_id),
                    company_id=str(request.company_id),
                    payload=identity.model_dump(mode="json"),
                )
            )
            return identity

    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None:
        await self._evidence.record_evidence(retrieval, artifact)

    async def save_document(self, document: ParsedDocument) -> None:
        async with self._sessions() as session, session.begin():
            session.add(
                _DocumentExtractionRow(
                    id=str(document.version_id),
                    document_id=str(document.identity.id),
                    retrieval_id=str(document.retrieval.id),
                    artifact_id=str(document.artifact.id) if document.artifact else None,
                    created_at=document.created_at.isoformat(),
                    payload=document.model_dump(mode="json"),
                )
            )

    async def get_document(self, version_id: UUID) -> ParsedDocument | None:
        async with self._sessions() as session:
            row = await session.get(_DocumentExtractionRow, str(version_id))
            return ParsedDocument.model_validate(row.payload) if row else None

    async def document_history(self, document_id: UUID) -> tuple[ParsedDocument, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(_DocumentExtractionRow)
                .where(_DocumentExtractionRow.document_id == str(document_id))
                .order_by(_DocumentExtractionRow.created_at, _DocumentExtractionRow.id)
            )
            return tuple(ParsedDocument.model_validate(row.payload) for row in rows)

    async def search(
        self, company_id: UUID, query: str, limit: int
    ) -> tuple[DocumentSearchHit, ...]:
        hits: list[DocumentSearchHit] = []
        async with self._sessions() as session:
            rows = await session.stream_scalars(
                select(_DocumentExtractionRow)
                .join(_DocumentRow)
                .where(_DocumentRow.company_id == str(company_id))
                .order_by(_DocumentExtractionRow.created_at.desc(), _DocumentExtractionRow.id)
            )
            async for row in rows:
                document = ParsedDocument.model_validate(row.payload)
                if document.artifact is None:
                    continue
                for chunk in document.chunks:
                    if query.casefold() in chunk.text.casefold():
                        section = next(
                            s.label for s in document.sections if s.id == chunk.section_id
                        )
                        hits.append(
                            DocumentSearchHit(
                                document_id=document.identity.id,
                                version_id=document.version_id,
                                title=document.title,
                                source_url=document.retrieval.final_url,
                                retrieval_id=document.retrieval.id,
                                artifact_id=document.artifact.id,
                                artifact_sha256=document.artifact.content_sha256,
                                page_id=chunk.page_id,
                                page_number=chunk.page_number,
                                chunk_id=chunk.id,
                                section=section,
                                text=chunk.text,
                            )
                        )
                        if len(hits) >= limit:
                            return tuple(hits)
        return tuple(hits)


class _MarketInstrumentRow(_Base):
    __tablename__ = "market_instruments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    exchange: Mapped[str] = mapped_column(String)
    symbol: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)


class _MarketExtractionRow(_Base):
    __tablename__ = "market_extractions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instrument_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_instruments.id"), index=True
    )
    created_at: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class SQLiteMarketRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._evidence = SQLiteExtractionRepository(sessions)

    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None:
        await self._evidence.record_evidence(retrieval, artifact)

    async def save_market(self, result: MarketExtractionResult) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            instrument = result.instrument
            if instrument and await session.get(_MarketInstrumentRow, str(instrument.id)) is None:
                session.add(
                    _MarketInstrumentRow(
                        id=str(instrument.id),
                        exchange=instrument.exchange,
                        symbol=instrument.symbol,
                        kind=instrument.kind,
                    )
                )
                await session.flush()
            session.add(
                _MarketExtractionRow(
                    id=str(result.id),
                    instrument_id=str(instrument.id) if instrument else None,
                    created_at=result.created_at.isoformat(),
                    payload=result.model_dump(mode="json"),
                )
            )

    async def get_market(self, extraction_id: UUID) -> MarketExtractionResult | None:
        async with self._sessions() as session:
            row = await session.get(_MarketExtractionRow, str(extraction_id))
            return MarketExtractionResult.model_validate(row.payload) if row else None

    async def market_history(self, instrument_id: UUID) -> tuple[MarketExtractionResult, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(_MarketExtractionRow)
                .where(_MarketExtractionRow.instrument_id == str(instrument_id))
                .order_by(_MarketExtractionRow.created_at, _MarketExtractionRow.id)
            )
            return tuple(MarketExtractionResult.model_validate(row.payload) for row in rows)


class _PublicationRow(_Base):
    __tablename__ = "publications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class _PublicationSnapshotRow(_Base):
    __tablename__ = "publication_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    retrieved_at: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class _MacroSeriesRow(_Base):
    __tablename__ = "macro_series"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class _MacroSnapshotRow(_Base):
    __tablename__ = "macro_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    identity_id: Mapped[str | None] = mapped_column(ForeignKey("macro_series.id"), index=True)
    retrieved_at: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class SQLiteContextRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.evidence = SQLiteExtractionRepository(sessions)

    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None:
        await self.evidence.record_evidence(retrieval, artifact)

    async def save_publication(self, snapshot: PublicationSnapshot) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            key = str(snapshot.identity.id)
            if await session.get(_PublicationRow, key) is None:
                session.add(
                    _PublicationRow(id=key, payload=snapshot.identity.model_dump(mode="json"))
                )
                await session.flush()
            session.add(
                _PublicationSnapshotRow(
                    id=str(snapshot.id),
                    identity_id=key,
                    retrieved_at=snapshot.retrieved_at.isoformat(),
                    payload=snapshot.model_dump(mode="json"),
                )
            )

    async def save_macro(self, snapshot: MacroSnapshot) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            key = str(snapshot.series.id) if snapshot.series else None
            if snapshot.series and await session.get(_MacroSeriesRow, key) is None:
                session.add(
                    _MacroSeriesRow(id=key, payload=snapshot.series.model_dump(mode="json"))
                )
                await session.flush()
            session.add(
                _MacroSnapshotRow(
                    id=str(snapshot.id),
                    identity_id=key,
                    retrieved_at=max(r.retrieved_at for r in snapshot.retrievals).isoformat(),
                    payload=snapshot.model_dump(mode="json"),
                )
            )

    async def publication_history(self, identity_id: UUID) -> tuple[PublicationSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_PublicationSnapshotRow)
                .where(_PublicationSnapshotRow.identity_id == str(identity_id))
                .order_by(_PublicationSnapshotRow.retrieved_at, _PublicationSnapshotRow.id)
            )
            return tuple(PublicationSnapshot.model_validate(r.payload) for r in rows)

    async def macro_history(self, series_id: UUID) -> tuple[MacroSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_MacroSnapshotRow)
                .where(_MacroSnapshotRow.identity_id == str(series_id))
                .order_by(_MacroSnapshotRow.retrieved_at, _MacroSnapshotRow.id)
            )
            return tuple(MacroSnapshot.model_validate(r.payload) for r in rows)

    async def get_publication(self, snapshot_id: UUID) -> PublicationSnapshot | None:
        async with self.sessions() as session:
            row = await session.get(_PublicationSnapshotRow, str(snapshot_id))
            return PublicationSnapshot.model_validate(row.payload) if row else None

    async def get_macro(self, snapshot_id: UUID) -> MacroSnapshot | None:
        async with self.sessions() as session:
            row = await session.get(_MacroSnapshotRow, str(snapshot_id))
            return MacroSnapshot.model_validate(row.payload) if row else None

    async def search_publications(self, query: str, limit: int) -> tuple[PublicationHit, ...]:
        hits = []
        async with self.sessions() as session:
            rows = await session.stream_scalars(
                select(_PublicationSnapshotRow).order_by(
                    _PublicationSnapshotRow.retrieved_at.desc(), _PublicationSnapshotRow.id
                )
            )
            async for row in rows:
                snapshot = PublicationSnapshot.model_validate(row.payload)
                for passage in snapshot.passages:
                    if query.casefold() in passage.text.casefold():
                        hits.append(
                            PublicationHit(
                                snapshot_id=snapshot.id,
                                publication_id=snapshot.identity.id,
                                title=snapshot.title,
                                publisher=snapshot.identity.publisher,
                                source_url=snapshot.identity.source_url,
                                text=passage.text,
                                provenance=passage.provenance,
                                retrieval_id=snapshot.retrieval.id,
                            )
                        )
                        if len(hits) >= limit:
                            return tuple(hits)
        return tuple(hits)


class _CalculationSnapshotRow(_Base):
    __tablename__ = "calculation_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.id"), index=True)
    calculated_at: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


def decode_calculation(payload: dict[str, Any]) -> CalculationSnapshot | SectorKPIEvaluation:
    if "payload_type" not in payload and payload.get("schema_version") == "4.1":
        return CalculationSnapshot.model_validate(payload)
    if payload.get("payload_version") != 1:
        raise ValueError("Unsupported calculation payload version")
    if payload.get("payload_type") == "financial_calculation":
        return CalculationSnapshot.model_validate(payload["snapshot"])
    if payload.get("payload_type") == "sector_kpi_evaluation":
        return SectorKPIEvaluation.model_validate(payload["snapshot"])
    raise ValueError("Unsupported calculation payload type")


class SQLiteCalculationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def get_calculation(
        self, snapshot_id: UUID
    ) -> CalculationSnapshot | SectorKPIEvaluation | None:
        async with self.sessions() as session:
            row = await session.get(_CalculationSnapshotRow, str(snapshot_id))
            return decode_calculation(row.payload) if row else None

    async def save_calculation(self, snapshot: CalculationSnapshot | SectorKPIEvaluation) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                _CalculationSnapshotRow(
                    id=str(snapshot.id),
                    extraction_id=str(snapshot.request.extraction_id),
                    calculated_at=snapshot.calculated_at.isoformat(),
                    payload={
                        "payload_type": "financial_calculation"
                        if isinstance(snapshot, CalculationSnapshot)
                        else "sector_kpi_evaluation",
                        "payload_version": 1,
                        "snapshot": snapshot.model_dump(mode="json"),
                    },
                )
            )

    async def calculation_history(
        self, extraction_id: UUID
    ) -> tuple[CalculationSnapshot | SectorKPIEvaluation, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_CalculationSnapshotRow)
                .where(_CalculationSnapshotRow.extraction_id == str(extraction_id))
                .order_by(_CalculationSnapshotRow.calculated_at, _CalculationSnapshotRow.id)
            )
            return tuple(decode_calculation(row.payload) for row in rows)


class _MarketCalculationRow(_Base):
    __tablename__ = "market_calculation_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("market_extractions.id"), index=True)
    benchmark_extraction_id: Mapped[str | None] = mapped_column(ForeignKey("market_extractions.id"))
    calculated_at: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


def decode_market_calculation(
    payload: dict[str, Any],
) -> MarketCalculationSnapshot | TechnicalSnapshot:
    if "payload_type" not in payload and payload.get("schema_version") == "4B.1":
        return MarketCalculationSnapshot.model_validate(payload)
    if payload.get("payload_version") != 1:
        raise ValueError("Unknown market calculation payload version")
    if payload.get("payload_type") == "market_scalar":
        return MarketCalculationSnapshot.model_validate(payload["snapshot"])
    if payload.get("payload_type") == "technical_series":
        return TechnicalSnapshot.model_validate(payload["snapshot"])
    raise ValueError("Unknown market calculation payload type")


class SQLiteMarketCalculationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_market_calculation(self, snapshot: MarketCalculationSnapshot) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                _MarketCalculationRow(
                    id=str(snapshot.id),
                    extraction_id=str(snapshot.request.extraction_id),
                    benchmark_extraction_id=str(snapshot.request.benchmark_extraction_id)
                    if snapshot.request.benchmark_extraction_id
                    else None,
                    calculated_at=snapshot.calculated_at.isoformat(),
                    payload={
                        "payload_type": "market_scalar",
                        "payload_version": 1,
                        "snapshot": snapshot.model_dump(mode="json"),
                    },
                )
            )

    async def market_calculation_history(
        self, extraction_id: UUID
    ) -> tuple[MarketCalculationSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_MarketCalculationRow)
                .where(_MarketCalculationRow.extraction_id == str(extraction_id))
                .order_by(_MarketCalculationRow.calculated_at, _MarketCalculationRow.id)
            )
            decoded = (decode_market_calculation(row.payload) for row in rows)
            return tuple(s for s in decoded if isinstance(s, MarketCalculationSnapshot))

    async def save_technical(self, snapshot: TechnicalSnapshot) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                _MarketCalculationRow(
                    id=str(snapshot.id),
                    extraction_id=str(snapshot.request.extraction_id),
                    benchmark_extraction_id=None,
                    calculated_at=snapshot.calculated_at.isoformat(),
                    payload={
                        "payload_type": "technical_series",
                        "payload_version": 1,
                        "snapshot": snapshot.model_dump(mode="json"),
                    },
                )
            )

    async def technical_history(self, extraction_id: UUID) -> tuple[TechnicalSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_MarketCalculationRow)
                .where(_MarketCalculationRow.extraction_id == str(extraction_id))
                .order_by(_MarketCalculationRow.calculated_at, _MarketCalculationRow.id)
            )
            decoded = (decode_market_calculation(row.payload) for row in rows)
            return tuple(s for s in decoded if isinstance(s, TechnicalSnapshot))


class _PatternSnapshotRow(_Base):
    __tablename__ = "pattern_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.id"), index=True)
    evaluated_at: Mapped[str] = mapped_column(String)
    schema_version: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class SQLitePatternRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_pattern(self, snapshot: PatternSnapshot) -> None:
        # Validate every referenced immutable payload before the append, in this transaction.
        from stock_research_ai.application.patterns import digest, validate_calculation

        async with self.sessions() as session, session.begin():
            source_row = await session.get(_ExtractionRow, str(snapshot.request.extraction_id))
            if source_row is None:
                raise ValueError("Pattern extraction missing")
            source = CompanyExtractionResult.model_validate(source_row.payload)
            if digest(source) != snapshot.source_payload_sha256:
                raise ValueError("Pattern source digest mismatch")
            for finding in snapshot.findings:
                for ref in finding.calculations:
                    row = await session.get(_CalculationSnapshotRow, str(ref.snapshot_id))
                    if row is None:
                        raise ValueError("Pattern calculation reference missing")
                    calculation = decode_calculation(row.payload)
                    metric, key = ref.role.split(":")
                    if (
                        validate_calculation(
                            calculation, snapshot.request, metric, key, ref.metric_id, source
                        )
                        != ref
                    ):
                        raise ValueError("Pattern calculation reference mismatch")
            session.add(
                _PatternSnapshotRow(
                    id=str(snapshot.id),
                    extraction_id=str(snapshot.request.extraction_id),
                    evaluated_at=snapshot.evaluated_at.isoformat(),
                    schema_version=snapshot.schema_version,
                    payload=snapshot.model_dump(mode="json"),
                )
            )

    async def pattern_history(self, extraction_id: UUID) -> tuple[PatternSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_PatternSnapshotRow)
                .where(_PatternSnapshotRow.extraction_id == str(extraction_id))
                .order_by(_PatternSnapshotRow.evaluated_at, _PatternSnapshotRow.id)
            )
            return tuple(PatternSnapshot.model_validate(row.payload) for row in rows)


class _ValuationRow(_Base):
    __tablename__ = "valuation_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    financial_extraction_id: Mapped[str | None] = mapped_column(ForeignKey("extractions.id"))
    market_extraction_id: Mapped[str | None] = mapped_column(ForeignKey("market_extractions.id"))
    evaluated_at: Mapped[str] = mapped_column(String)
    payload_type: Mapped[str] = mapped_column(String)
    schema_version: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class SQLiteValuationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_valuation(self, snapshot: ValuationSnapshot) -> None:
        from stock_research_ai.application.valuation import digest

        snapshot = ValuationSnapshot.model_validate_json(snapshot.model_dump_json())
        # Validate immutable references in the append transaction, without association tables.
        async with self.sessions() as session, session.begin():
            source: (
                CompanyExtractionResult
                | MarketExtractionResult
                | CalculationSnapshot
                | SectorKPIEvaluation
                | None
            )
            for reference in snapshot.sources:
                if reference.kind == "FINANCIAL":
                    row = await session.get(_ExtractionRow, str(reference.snapshot_id))
                    source = CompanyExtractionResult.model_validate(row.payload) if row else None
                elif reference.kind == "MARKET":
                    market_row = await session.get(_MarketExtractionRow, str(reference.snapshot_id))
                    source = (
                        MarketExtractionResult.model_validate(market_row.payload)
                        if market_row
                        else None
                    )
                else:
                    calculation_row = await session.get(
                        _CalculationSnapshotRow, str(reference.snapshot_id)
                    )
                    source = (
                        decode_calculation(calculation_row.payload) if calculation_row else None
                    )
                if source is None or digest(source) != reference.payload_sha256:
                    raise ValueError("Valuation source reference/hash mismatch")
            markets = [s.snapshot_id for s in snapshot.sources if s.kind == "MARKET"]
            session.add(
                _ValuationRow(
                    id=str(snapshot.id),
                    company_id=str(snapshot.request.company_id),
                    financial_extraction_id=str(snapshot.request.financial_extraction_id)
                    if snapshot.request.financial_extraction_id
                    else None,
                    market_extraction_id=str(markets[0]) if len(markets) == 1 else None,
                    evaluated_at=snapshot.evaluated_at.isoformat(),
                    payload_type=snapshot.payload_type,
                    schema_version=snapshot.schema_version,
                    payload=snapshot.model_dump(mode="json"),
                )
            )

    async def valuation_history(self, company_id: UUID) -> tuple[ValuationSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_ValuationRow)
                .where(_ValuationRow.company_id == str(company_id))
                .order_by(_ValuationRow.evaluated_at, _ValuationRow.id)
            )
            return tuple(ValuationSnapshot.model_validate(row.payload) for row in rows)


class _AnalysisRow(_Base):
    __tablename__ = "analysis_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    evaluated_at: Mapped[str] = mapped_column(String)
    schema_version: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


async def _load_evidence_owner_in_session(
    session: AsyncSession, kind: str, owning_snapshot_id: UUID
) -> BaseModel | None:
    """Neutral transaction-scoped integrity helper: reload the exact owning object for one
    evidence kind, using the SAME session/transaction the caller is about to insert with.
    Pure read + decode - no formula, no calculation, no recomputation of any kind."""
    if kind == "COMPANY":
        return await _load_company(session, owning_snapshot_id)
    if kind in ("FINANCIAL", "SHAREHOLDING"):
        row = await session.get(_ExtractionRow, str(owning_snapshot_id))
        return CompanyExtractionResult.model_validate(row.payload) if row else None
    if kind == "MARKET":
        market_row = await session.get(_MarketExtractionRow, str(owning_snapshot_id))
        return MarketExtractionResult.model_validate(market_row.payload) if market_row else None
    if kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        calc_row = await session.get(_CalculationSnapshotRow, str(owning_snapshot_id))
        return decode_calculation(calc_row.payload) if calc_row else None
    if kind == "PATTERN":
        pattern_row = await session.get(_PatternSnapshotRow, str(owning_snapshot_id))
        return PatternSnapshot.model_validate(pattern_row.payload) if pattern_row else None
    if kind in ("MARKET_CALCULATION", "TECHNICAL"):
        mc_row = await session.get(_MarketCalculationRow, str(owning_snapshot_id))
        return decode_market_calculation(mc_row.payload) if mc_row else None
    if kind == "VALUATION":
        valuation_row = await session.get(_ValuationRow, str(owning_snapshot_id))
        return ValuationSnapshot.model_validate(valuation_row.payload) if valuation_row else None
    if kind == "DOCUMENT_CHUNK":
        document_row = await session.get(_DocumentExtractionRow, str(owning_snapshot_id))
        return ParsedDocument.model_validate(document_row.payload) if document_row else None
    if kind == "PUBLICATION":
        publication_row = await session.get(_PublicationSnapshotRow, str(owning_snapshot_id))
        return (
            PublicationSnapshot.model_validate(publication_row.payload)
            if publication_row
            else None
        )
    if kind == "MACRO":
        macro_row = await session.get(_MacroSnapshotRow, str(owning_snapshot_id))
        return MacroSnapshot.model_validate(macro_row.payload) if macro_row else None
    raise ValueError(f"Unknown evidence kind: {kind}")


async def _verify_upstream_lineage_in_session(
    session: AsyncSession, kind: str, decoded_source: object
) -> None:
    """Safeguard 1B, replayed inside the SAME transaction as the append: derived snapshots
    are walked back to their own originating extraction(s)/market extraction(s), using
    their own already-existing lineage fields, and re-verified. Pure read + digest compare;
    no financial/calculation/technical logic is reimplemented or recomputed here."""
    from stock_research_ai.application.hashing import digest

    if kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        assert isinstance(decoded_source, CalculationSnapshot | SectorKPIEvaluation)
        upstream = await _load_evidence_owner_in_session(
            session, "FINANCIAL", decoded_source.request.extraction_id
        )
        if upstream is None or digest(upstream) != decoded_source.source_payload_sha256:
            raise ValueError(f"Pinned {kind} source's upstream extraction hash mismatch")
    elif kind == "PATTERN":
        assert isinstance(decoded_source, PatternSnapshot)
        upstream = await _load_evidence_owner_in_session(
            session, "FINANCIAL", decoded_source.request.extraction_id
        )
        if upstream is None or digest(upstream) != decoded_source.source_payload_sha256:
            raise ValueError("Pinned PATTERN source's upstream extraction hash mismatch")
    elif kind == "MARKET_CALCULATION":
        assert isinstance(decoded_source, MarketCalculationSnapshot)
        for market_source in decoded_source.sources:
            upstream = await _load_evidence_owner_in_session(
                session, "MARKET", market_source.extraction_id
            )
            if upstream is None or digest(upstream) != market_source.payload_sha256:
                raise ValueError(
                    "Pinned MARKET_CALCULATION source's upstream market extraction hash mismatch"
                )
    elif kind == "TECHNICAL":
        assert isinstance(decoded_source, TechnicalSnapshot)
        upstream = await _load_evidence_owner_in_session(
            session, "MARKET", decoded_source.source.extraction_id
        )
        if upstream is None or digest(upstream) != decoded_source.source.payload_sha256:
            raise ValueError(
                "Pinned TECHNICAL source's upstream market extraction hash mismatch"
            )
    elif kind == "VALUATION":
        assert isinstance(decoded_source, ValuationSnapshot)
        for valuation_source in decoded_source.sources:
            if valuation_source.kind == "FINANCIAL":
                upstream = await _load_evidence_owner_in_session(
                    session, "FINANCIAL", valuation_source.snapshot_id
                )
            elif valuation_source.kind == "MARKET":
                upstream = await _load_evidence_owner_in_session(
                    session, "MARKET", valuation_source.snapshot_id
                )
            else:
                upstream = await _load_evidence_owner_in_session(
                    session, "FINANCIAL_CALCULATION", valuation_source.snapshot_id
                )
                if upstream is None:
                    upstream = await _load_evidence_owner_in_session(
                        session, "SECTOR_KPI", valuation_source.snapshot_id
                    )
                if upstream is not None:
                    upstream_kind = (
                        "FINANCIAL_CALCULATION"
                        if isinstance(upstream, CalculationSnapshot)
                        else "SECTOR_KPI"
                    )
                    await _verify_upstream_lineage_in_session(session, upstream_kind, upstream)
            if upstream is None or digest(upstream) != valuation_source.payload_sha256:
                raise ValueError(
                    "Pinned VALUATION source's upstream reference hash mismatch"
                )
    # COMPANY, FINANCIAL, SHAREHOLDING, MARKET, DOCUMENT_CHUNK, PUBLICATION, MACRO directly
    # own their own raw evidence - there is no further upstream snapshot to walk.


class SQLiteAnalysisRepository:
    """Append-only. Every distinct pinned source - across all thirteen evidence kinds - is
    reloaded and re-hashed, and every derived kind's upstream lineage is re-verified,
    inside the SAME session/transaction that performs the insert. If any check fails, the
    transaction is rolled back and nothing is inserted - eliminating the TOCTOU gap of
    checking through separate sibling-repository sessions before a later, separate insert."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_analysis(self, snapshot: AnalysisSnapshot) -> None:
        from stock_research_ai.application.hashing import digest

        distinct = {
            (r.kind, r.owning_snapshot_id): r.snapshot_payload_sha256 for r in snapshot.sources
        }
        snapshot = AnalysisSnapshot.model_validate_json(snapshot.model_dump_json())
        async with self.sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            for (kind, owning_snapshot_id), expected_sha in distinct.items():
                source = await _load_evidence_owner_in_session(session, kind, owning_snapshot_id)
                if source is None or digest(source) != expected_sha:
                    raise ValueError(f"Pinned {kind} source no longer exists or hash mismatch")
                await _verify_upstream_lineage_in_session(session, kind, source)
            session.add(
                _AnalysisRow(
                    id=str(snapshot.id),
                    company_id=str(snapshot.company_id),
                    evaluated_at=snapshot.evaluated_at.isoformat(),
                    schema_version=snapshot.schema_version,
                    payload=snapshot.model_dump(mode="json"),
                )
            )

    async def analysis_history(self, company_id: UUID) -> tuple[AnalysisSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_AnalysisRow)
                .where(_AnalysisRow.company_id == str(company_id))
                .order_by(_AnalysisRow.evaluated_at, _AnalysisRow.id)
            )
            return tuple(AnalysisSnapshot.model_validate(row.payload) for row in rows)

    async def get_analysis(self, analysis_snapshot_id: UUID) -> AnalysisSnapshot | None:
        async with self.sessions() as session:
            row = await session.get(_AnalysisRow, str(analysis_snapshot_id))
            return AnalysisSnapshot.model_validate(row.payload) if row else None


class _ChallengeRow(_Base):
    __tablename__ = "challenge_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_snapshots.id"), index=True
    )
    evaluated_at: Mapped[str] = mapped_column(String)
    schema_version: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


async def _load_analysis_snapshot_in_session(
    session: AsyncSession, analysis_snapshot_id: UUID
) -> AnalysisSnapshot | None:
    """Same-transaction re-fetch of the reviewed AnalysisSnapshot, mirroring
    _load_evidence_owner_in_session's pattern for Part 5C's own append."""
    row = await session.get(_AnalysisRow, str(analysis_snapshot_id))
    return AnalysisSnapshot.model_validate(row.payload) if row else None


class SQLiteChallengeRepository:
    """Append-only. Re-verifies the reviewed AnalysisSnapshot itself, plus every distinct
    pinned source across its own bundle and their upstream lineage, inside the SAME
    session/transaction as the insert - reusing Part 5B's integrity helpers unchanged."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_challenge(self, snapshot: ChallengeSnapshot) -> None:
        from stock_research_ai.application.challenge import ChallengeIntegrityError
        from stock_research_ai.application.hashing import digest

        reviewed = snapshot.challenger_bundle.reviewed_analysis
        distinct = {
            (r.kind, r.owning_snapshot_id): r.snapshot_payload_sha256 for r in reviewed.sources
        }
        snapshot = ChallengeSnapshot.model_validate_json(snapshot.model_dump_json())
        async with self.sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            fresh_reviewed = await _load_analysis_snapshot_in_session(
                session, snapshot.analysis_snapshot_id
            )
            fresh_hash = digest(fresh_reviewed) if fresh_reviewed is not None else None
            if fresh_reviewed is None or fresh_hash != snapshot.analysis_snapshot_sha256:
                raise ChallengeIntegrityError(
                    "Reviewed analysis snapshot no longer exists or hash mismatch"
                )
            for (kind, owning_snapshot_id), expected_sha in distinct.items():
                source = await _load_evidence_owner_in_session(session, kind, owning_snapshot_id)
                if source is None or digest(source) != expected_sha:
                    raise ChallengeIntegrityError(
                        f"Pinned {kind} source no longer exists or hash mismatch"
                    )
                await _verify_upstream_lineage_in_session(session, kind, source)
            session.add(
                _ChallengeRow(
                    id=str(snapshot.id),
                    analysis_snapshot_id=str(snapshot.analysis_snapshot_id),
                    evaluated_at=snapshot.evaluated_at.isoformat(),
                    schema_version=snapshot.schema_version,
                    payload=snapshot.model_dump(mode="json"),
                )
            )

    async def challenge_history(self, analysis_snapshot_id: UUID) -> tuple[ChallengeSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_ChallengeRow)
                .where(_ChallengeRow.analysis_snapshot_id == str(analysis_snapshot_id))
                .order_by(_ChallengeRow.evaluated_at, _ChallengeRow.id)
            )
            return tuple(ChallengeSnapshot.model_validate(row.payload) for row in rows)

    async def get_challenge(self, challenge_snapshot_id: UUID) -> ChallengeSnapshot | None:
        async with self.sessions() as session:
            row = await session.get(_ChallengeRow, str(challenge_snapshot_id))
            return ChallengeSnapshot.model_validate(row.payload) if row else None


async def _load_challenge_snapshot_in_session(
    session: AsyncSession, challenge_snapshot_id: UUID
) -> ChallengeSnapshot | None:
    """Same-transaction re-fetch of the pinned ChallengeSnapshot, mirroring
    _load_analysis_snapshot_in_session's pattern for Part 6A's own append."""
    row = await session.get(_ChallengeRow, str(challenge_snapshot_id))
    return ChallengeSnapshot.model_validate(row.payload) if row else None


class _ReportRow(_Base):
    __tablename__ = "report_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    challenge_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("challenge_snapshots.id"), index=True
    )
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    evaluated_at: Mapped[str] = mapped_column(String)
    schema_version: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class SQLiteReportRepository:
    """Append-only. Re-verifies the pinned ChallengeSnapshot AND its own transitively
    embedded AnalysisSnapshot, plus every distinct pinned evidence source and its
    upstream lineage and raw bytes, inside the SAME session/transaction as the
    insert - reusing Part 5B/5C's integrity helpers unchanged (Option B: this row
    never re-embeds the full upstream chain, only pins it by ID + hash)."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_report(self, snapshot: ResearchReportSnapshot) -> None:
        from stock_research_ai.application.hashing import digest
        from stock_research_ai.application.report import ReportIntegrityError

        snapshot = ResearchReportSnapshot.model_validate_json(snapshot.model_dump_json())
        async with self.sessions() as session, session.begin():
            await session.execute(text("BEGIN IMMEDIATE"))
            fresh_challenge = await _load_challenge_snapshot_in_session(
                session, snapshot.challenge_snapshot_id
            )
            fresh_challenge_hash = digest(fresh_challenge) if fresh_challenge is not None else None
            expected_challenge_hash = snapshot.challenge_snapshot_sha256
            if fresh_challenge is None or fresh_challenge_hash != expected_challenge_hash:
                raise ReportIntegrityError(
                    "Pinned challenge snapshot no longer exists or hash mismatch"
                )
            fresh_reviewed = await _load_analysis_snapshot_in_session(
                session, snapshot.analysis_snapshot_id
            )
            fresh_reviewed_hash = digest(fresh_reviewed) if fresh_reviewed is not None else None
            if fresh_reviewed is None or fresh_reviewed_hash != snapshot.analysis_snapshot_sha256:
                raise ReportIntegrityError(
                    "Pinned analysis snapshot no longer exists or hash mismatch"
                )
            if fresh_challenge.analysis_snapshot_id != snapshot.analysis_snapshot_id:
                raise ReportIntegrityError(
                    "Pinned challenge snapshot no longer references the pinned analysis snapshot"
                )
            distinct = {
                (r.kind, r.owning_snapshot_id): r.snapshot_payload_sha256
                for r in fresh_reviewed.sources
            }
            for (kind, owning_snapshot_id), expected_sha in distinct.items():
                source = await _load_evidence_owner_in_session(session, kind, owning_snapshot_id)
                if source is None or digest(source) != expected_sha:
                    raise ReportIntegrityError(
                        f"Pinned {kind} source no longer exists or hash mismatch"
                    )
                await _verify_upstream_lineage_in_session(session, kind, source)
            session.add(
                _ReportRow(
                    id=str(snapshot.id),
                    challenge_snapshot_id=str(snapshot.challenge_snapshot_id),
                    company_id=str(snapshot.company_id),
                    evaluated_at=snapshot.evaluated_at.isoformat(),
                    schema_version=snapshot.schema_version,
                    payload=snapshot.model_dump(mode="json"),
                )
            )

    async def report_history(
        self, challenge_snapshot_id: UUID
    ) -> tuple[ResearchReportSnapshot, ...]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(_ReportRow)
                .where(_ReportRow.challenge_snapshot_id == str(challenge_snapshot_id))
                .order_by(_ReportRow.evaluated_at, _ReportRow.id)
            )
            return tuple(ResearchReportSnapshot.model_validate(row.payload) for row in rows)

    async def get_report(self, report_snapshot_id: UUID) -> ResearchReportSnapshot | None:
        """Exact-ID lookup, mirroring get_analysis/get_challenge above verbatim. Read-only:
        no re-verification, no mutation, no new snapshot - integrity was already
        established once, permanently, inside save_report's own atomic transaction."""
        async with self.sessions() as session:
            row = await session.get(_ReportRow, str(report_snapshot_id))
            return ResearchReportSnapshot.model_validate(row.payload) if row else None
