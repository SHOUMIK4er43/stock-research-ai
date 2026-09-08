"""Wire the engine to persistence and own the runtime resource lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from stock_research_ai.adapters.json_export import check_output, export_result
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import ExtractionServices, ResearchEngine
from stock_research_ai.application.ports import SourceAcquirer
from stock_research_ai.config import Settings
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
