"""Wire the engine to persistence and own the runtime resource lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from stock_research_ai.adapters.documents.pdf import PDFNormalizer
from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.adapters.json_export import (
    check_output,
    export_calculation,
    export_context,
    export_document,
    export_market,
    export_market_calculation,
    export_result,
)
from stock_research_ai.adapters.kite.normalizer import KiteNormalizer
from stock_research_ai.adapters.kite.source import KiteSource
from stock_research_ai.adapters.mospi.normalizer import MacroNormalizer
from stock_research_ai.adapters.mospi.source import MospiSource
from stock_research_ai.adapters.publications.normalizer import PublicationNormalizer
from stock_research_ai.adapters.publications.source import PublicationSource
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import (
    CalculationServices,
    ContextServices,
    DocumentServices,
    ExtractionServices,
    MarketCalculationServices,
    MarketServices,
    ResearchEngine,
)
from stock_research_ai.application.ports import (
    DocumentAcquirer,
    MacroAcquirer,
    MarketSource,
    PublicationAcquirer,
    SourceAcquirer,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.calculations import CalculationRequest, CalculationSnapshot
from stock_research_ai.domain.documents import DocumentRequest, ParsedDocument
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest
from stock_research_ai.domain.macro import MacroRequest, MacroSnapshot
from stock_research_ai.domain.market import MarketExtractionResult, MarketRequest
from stock_research_ai.domain.market_calculations import (
    MarketCalculationRequest,
    MarketCalculationSnapshot,
)
from stock_research_ai.domain.publications import (
    PublicationRequest,
    PublicationSnapshot,
)


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


@asynccontextmanager
async def market_engine(
    settings: Settings, source: MarketSource | None = None
) -> AsyncIterator[ResearchEngine]:
    database = SQLiteDatabase(settings.database_path)
    store = FileRawArtifactStore(
        settings.artifact_path or settings.database_path.parent / "artifacts"
    )
    try:
        await database.check_ready()
        yield ResearchEngine(
            database.repository,
            market=MarketServices(
                source or KiteSource.from_environment(),
                store,
                KiteNormalizer(),
                database.market,
            ),
        )
    finally:
        await database.close()


async def ingest_market(
    settings: Settings, request: MarketRequest, output: Path, source: MarketSource | None = None
) -> MarketExtractionResult:
    check_output(output)
    async with market_engine(settings, source) as engine:
        result = await engine.ingest_market_data(request)
        await export_market(
            result,
            output,
            FileRawArtifactStore(
                settings.artifact_path or settings.database_path.parent / "artifacts"
            ),
        )
        return result


@asynccontextmanager
async def context_engine(
    settings: Settings,
    publications: PublicationAcquirer | None = None,
    macro: MacroAcquirer | None = None,
) -> AsyncIterator[ResearchEngine]:
    database = SQLiteDatabase(settings.database_path)
    store = FileRawArtifactStore(
        settings.artifact_path or settings.database_path.parent / "artifacts"
    )
    try:
        await database.check_ready()
        yield ResearchEngine(
            database.repository,
            context=ContextServices(
                publications or PublicationSource(),
                PublicationNormalizer(),
                macro or MospiSource(),
                MacroNormalizer(),
                store,
                database.context,
            ),
        )
    finally:
        await database.close()


async def ingest_publication(
    settings: Settings, request: PublicationRequest, output: Path
) -> PublicationSnapshot:
    check_output(output)
    async with context_engine(settings) as engine:
        result = await engine.ingest_publication(request)
        await export_context(
            result,
            output,
            FileRawArtifactStore(
                settings.artifact_path or settings.database_path.parent / "artifacts"
            ),
        )
        return result


async def ingest_macro(settings: Settings, request: MacroRequest, output: Path) -> MacroSnapshot:
    check_output(output)
    async with context_engine(settings) as engine:
        result = await engine.ingest_macro(request)
        await export_context(
            result,
            output,
            FileRawArtifactStore(
                settings.artifact_path or settings.database_path.parent / "artifacts"
            ),
        )
        return result


async def calculate_financials(
    settings: Settings, request: CalculationRequest, output: Path
) -> CalculationSnapshot:
    check_output(output)
    database = SQLiteDatabase(settings.database_path)
    try:
        await database.check_ready()
        engine = ResearchEngine(
            database.repository,
            calculations=CalculationServices(
                database.extractions,
                database.calculations,
                FileRawArtifactStore(
                    settings.artifact_path or settings.database_path.parent / "artifacts"
                ),
            ),
        )
        result = await engine.calculate_financials(request)
        export_calculation(result, output)
        return result
    finally:
        await database.close()


async def calculate_market_snapshot(
    settings: Settings, request: MarketCalculationRequest, output: Path
) -> MarketCalculationSnapshot:
    check_output(output)
    database = SQLiteDatabase(settings.database_path)
    try:
        await database.check_ready()
        engine = ResearchEngine(
            database.repository,
            market_calculations=MarketCalculationServices(
                database.market,
                database.market_calculations,
                FileRawArtifactStore(
                    settings.artifact_path or settings.database_path.parent / "artifacts"
                ),
            ),
        )
        result = await engine.calculate_market(request)
        export_market_calculation(result, output)
        return result
    finally:
        await database.close()
