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
    monkeypatch.setenv(
        "STOCK_RESEARCH_AI_BROWSER_AUTH_PATH", str(tmp_path / "browser_auth.json")
    )


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
        host = address[0] if isinstance(address, tuple) else None
        if host in {"127.0.0.1", "::1"}:
            # Loopback only: asyncio's Windows event loop builds its self-pipe this way.
            return original_connect(sock, address)
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


@pytest.fixture
def make_document_pdf():
    """Controlled test PDF bytes, generated locally without another PDF library."""
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    def make(
        texts=("Management remarks\nThis is a controlled evidence page for offline proof.",),
        scanned=False,
        metadata=True,
    ):
        writer = PdfWriter()
        for text in texts:
            page = writer.add_blank_page(width=612, height=792)
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    )
                }
            )
            stream = DecodedStreamObject()
            lines = []
            for line in text.splitlines():
                escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                lines.append(f"({escaped}) Tj 0 -18 Td")
            stream.set_data(("BT /F1 12 Tf 30 720 Td " + " ".join(lines) + " ET").encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
            if scanned:
                image = DecodedStreamObject()
                image.set_data(b"\x00\x00\x00")
                image.update(
                    {
                        NameObject("/Type"): NameObject("/XObject"),
                        NameObject("/Subtype"): NameObject("/Image"),
                        NameObject("/Width"): NumberObject(1),
                        NameObject("/Height"): NumberObject(1),
                        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                        NameObject("/BitsPerComponent"): NumberObject(8),
                    }
                )
                page["/Resources"][NameObject("/XObject")] = DictionaryObject(
                    {NameObject("/Im1"): writer._add_object(image)}
                )
        if not metadata:
            writer.metadata = None
        buffer = BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    return make


@pytest.fixture
async def document_settings(tmp_path):
    from stock_research_ai.bootstrap import initialize_database
    from stock_research_ai.config import Settings

    settings = Settings(
        database_path=tmp_path / "documents.sqlite3", artifact_path=tmp_path / "artifacts"
    )
    await initialize_database(settings)
    return settings


@pytest.fixture
async def document_company(document_settings):
    from stock_research_ai.bootstrap import research_engine
    from stock_research_ai.domain.company import CompanyRegistration

    async with research_engine(document_settings) as engine:
        return await engine.register_company(CompanyRegistration(name="YES BANK"))


@pytest.fixture
async def market_settings(tmp_path):
    from stock_research_ai.bootstrap import initialize_database
    from stock_research_ai.config import Settings

    settings = Settings(database_path=tmp_path / "market.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    return settings
