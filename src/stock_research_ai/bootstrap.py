"""Wire the engine to persistence and own the runtime resource lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import ResearchEngine
from stock_research_ai.config import Settings


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
