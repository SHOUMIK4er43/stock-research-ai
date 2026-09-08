"""Shared fixtures: every database lives in pytest's temporary directory."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.engine import ResearchEngine
from stock_research_ai.domain.company import Company


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from selecting a personal database via environment settings."""
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", str(tmp_path / "default.sqlite3"))
    monkeypatch.setenv("STOCK_RESEARCH_AI_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("STOCK_RESEARCH_AI_HTTP_PORT", "8000")


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[SQLiteDatabase]:
    registry = SQLiteDatabase(tmp_path / "registry.sqlite3")
    try:
        await registry.initialize()
        yield registry
    finally:
        await registry.close()


@pytest.fixture
def engine() -> ResearchEngine:
    """Isolate interface tests; explicit integration tests use the database fixture."""

    class MemoryRepository:
        def __init__(self) -> None:
            self.companies: dict[UUID, Company] = {}

        async def save(self, company: Company) -> None:
            self.companies[company.id] = company

        async def get(self, company_id: UUID) -> Company | None:
            return self.companies.get(company_id)

    return ResearchEngine(MemoryRepository())


@pytest.fixture(autouse=True)
def offline_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    import httpx

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must stay offline; use MockTransport or local fixtures")

    original_connect = socket.socket.connect

    def connect(sock: socket.socket, address: object) -> object:
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            return blocked()
        return original_connect(sock, address)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures/screener"


@pytest.fixture
async def yesbank_result(tmp_path: Path, fixture_dir: Path):
    from stock_research_ai.bootstrap import extract_snapshot, initialize_database
    from stock_research_ai.config import Settings
    from stock_research_ai.domain.extraction import ExtractionRequest

    settings = Settings(
        database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "artifacts"
    )
    await initialize_database(settings)
    return await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/YESBANK/consolidated/",
            replay_path=fixture_dir / "yesbank_consolidated.html",
        ),
        tmp_path / "export",
    )
