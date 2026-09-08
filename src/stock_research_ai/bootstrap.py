"""Wire the engine to persistence and own the runtime resource lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from stock_research_ai.adapters.documents.pdf import PDFNormalizer
from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.adapters.json_export import check_output, export_document, export_result
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import (
    DocumentServices,
    ExtractionServices,
    ResearchEngine,
)
from stock_research_ai.application.ports import DocumentAcquirer, SourceAcquirer
from stock_research_ai.config import Settings
from stock_research_ai.domain.documents import DocumentRequest, ParsedDocument
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest


@asynccontextmanager
async def research_engine(settings: Settings) -> AsyncIterator[ResearchEngine]:
    """Open an initialized registry, then close resources even on failure."""
    database = SQLiteDatabase(settings.database_path)
    try:
        await database.check_ready()
        yield ResearchEngine(database.repository)
    finally:
        await database.close()


async def initialize_database(settings: Settings) -> None:
    """Explicitly initialize the company registry; never run on import."""
    database = SQLiteDatabase(settings.database_path)
    try:
        await database.initialize()
    finally:
        await database.close()


async def extract_snapshot(
    settings: Settings,
    request: ExtractionRequest,
    output: Path,
    source: SourceAcquirer | None = None,
) -> CompanyExtractionResult:
    """One pipeline used by both the CLI and offline acceptance tests."""
    check_output(output)
    database = SQLiteDatabase(settings.database_path)
    store = FileRawArtifactStore(
        settings.artifact_path or settings.database_path.parent / "artifacts"
    )
    try:
        await database.check_ready()
        engine = ResearchEngine(
            database.repository,
            ExtractionServices(
                source=source or ScreenerSource(),
                artifacts=store,
                normalizer=ScreenerNormalizer(),
                repository=database.extractions,
            ),
        )
        result = await engine.extract_company(request)
        await export_result(result, output, store)
        return result
    finally:
        await database.close()


@asynccontextmanager
async def document_engine(
    settings: Settings, source: DocumentAcquirer | None = None
) -> AsyncIterator[ResearchEngine]:
    database = SQLiteDatabase(settings.database_path)
    store = FileRawArtifactStore(
        settings.artifact_path or settings.database_path.parent / "artifacts"
    )
    try:
        await database.check_ready()
        yield ResearchEngine(
            database.repository,
            documents=DocumentServices(
                source or DocumentSource(), store, PDFNormalizer(), database.documents
            ),
        )
    finally:
        await database.close()


async def ingest_document(
    settings: Settings,
    request: DocumentRequest,
    output: Path,
    source: DocumentAcquirer | None = None,
) -> ParsedDocument:
    check_output(output)
    async with document_engine(settings, source) as engine:
        document = await engine.ingest_document(request)
        await export_document(
            document,
            output,
            FileRawArtifactStore(
                settings.artifact_path or settings.database_path.parent / "artifacts"
            ),
        )
        return document
