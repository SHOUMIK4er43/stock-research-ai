import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from stock_research_ai.adapters.sqlite import DatabaseNotReadyError, SQLiteDatabase
from stock_research_ai.application.engine import ResearchEngine
from stock_research_ai.config import Settings
from stock_research_ai.domain.company import Company
from stock_research_ai.interfaces import http as http_interface


async def test_http_company_round_trip(engine: ResearchEngine) -> None:
    app = http_interface.create_app(engine=engine)
    assert {
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    } == {
        ("/health", "GET"),
        ("/v1/capabilities", "GET"),
        ("/v1/companies", "POST"),
        ("/v1/companies/{company_id}", "GET"),
    }
    assert len(app.routes) == 4
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            capabilities = await client.get("/v1/capabilities")
            assert capabilities.status_code == 200
            assert capabilities.json() == (await engine.get_capabilities()).model_dump(mode="json")
            assert capabilities.json()["analysis_available"] is False

            registration = {
                "name": "Yes Bank",
                "listings": [{"exchange": "NSE", "symbol": "YESBANK"}],
                "provider_identifiers": [{"provider": "screener", "identifier": "YESBANK"}],
            }
            created = await client.post("/v1/companies", json=registration)
            assert created.status_code == 201
            company = Company.model_validate(created.json())
            assert company.name == "Yes Bank"
            assert company.listings[0].symbol == "YESBANK"

            fetched = await client.get(f"/v1/companies/{company.id}")
            assert fetched.status_code == 200
            assert fetched.json() == created.json()
            assert await engine.get_company(company.id) == company


async def test_http_validation_and_missing_company(engine: ResearchEngine) -> None:
    app = http_interface.create_app(engine=engine)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/v1/companies/not-a-uuid")).status_code == 422
            missing = await client.get(f"/v1/companies/{uuid4()}")
            assert missing.status_code == 404
            assert missing.json() == {"detail": "Company not found"}
            assert (await client.post("/v1/companies", json={})).status_code == 422
            assert (await client.post("/v1/companies", json={"name": "   "})).status_code == 422


@pytest.mark.parametrize("injected", [False, True])
async def test_http_resource_ownership(
    engine: ResearchEngine, monkeypatch: pytest.MonkeyPatch, injected: bool
) -> None:
    events: list[str] = []

    @asynccontextmanager
    async def tracked_engine(_: Settings) -> AsyncIterator[ResearchEngine]:
        events.append("opened")
        try:
            yield engine
        finally:
            events.append("closed")

    monkeypatch.setattr(http_interface, "research_engine", tracked_engine)
    app = http_interface.create_app(engine=engine if injected else None)
    assert events == []
    async with app.router.lifespan_context(app):
        assert events == ([] if injected else ["opened"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/v1/capabilities")).status_code == 200
    assert events == ([] if injected else ["opened", "closed"])


async def test_http_bootstrap_persists_company_to_sqlite(database: SQLiteDatabase) -> None:
    settings = Settings(database_path=database.path)
    app = http_interface.create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post("/v1/companies", json={"name": "Persistent HTTP Company"})
            assert created.status_code == 201
            company = Company.model_validate(created.json())
    assert await database.repository.get(company.id) == company

    # A fresh application lifecycle reads the persisted record, with no shared engine.
    reopened = http_interface.create_app(settings=settings)
    async with reopened.router.lifespan_context(reopened):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=reopened), base_url="http://test"
        ) as client:
            fetched = await client.get(f"/v1/companies/{company.id}")
            assert fetched.json() == company.model_dump(mode="json")


async def test_http_startup_requires_explicit_database_initialization(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "registry.sqlite3"
    app = http_interface.create_app(settings=Settings(database_path=path))
    with pytest.raises(DatabaseNotReadyError, match="init-db"):
        async with app.router.lifespan_context(app):
            pytest.fail("An uninitialized database must not start the application")
    assert not path.parent.exists()


def test_settings_validate_configuration_without_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STOCK_RESEARCH_AI_DATABASE_PATH", "nested/registry.sqlite3")
    monkeypatch.setenv("STOCK_RESEARCH_AI_HTTP_PORT", "8123")
    settings = Settings()
    assert settings.database_path == tmp_path / "nested" / "registry.sqlite3"
    assert settings.http_port == 8123
    assert not settings.database_path.parent.exists()
    monkeypatch.setenv("STOCK_RESEARCH_AI_HTTP_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings()


def test_console_entry_point_and_imports_have_no_database_side_effects(tmp_path: Path) -> None:
    path = tmp_path / "uncreated" / "registry.sqlite3"
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "STOCK_RESEARCH_AI_DATABASE_PATH": str(path),
    }
    console = Path(sys.executable).parent / "stock-research-ai"
    result = subprocess.run(
        [str(console), "--help"], env=env, capture_output=True, text=True, check=True, timeout=10
    )
    assert "serve-http" in result.stdout and "serve-mcp" in result.stdout
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            (
                "import stock_research_ai.__main__; "
                "from stock_research_ai.interfaces.http import create_app; "
                "from stock_research_ai.interfaces.mcp import create_server; "
                "create_app(); create_server()"
            ),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout == ""
    assert not path.parent.exists()


async def test_console_initializes_only_the_explicit_database(tmp_path: Path) -> None:
    path = tmp_path / "explicit" / "registry.sqlite3"
    unused = tmp_path / "environment" / "registry.sqlite3"
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "stock-research-ai"),
            "--database",
            str(path),
            "init-db",
        ],
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "STOCK_RESEARCH_AI_DATABASE_PATH": str(unused),
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert result.stdout == ""
    assert not unused.parent.exists()
    database = SQLiteDatabase(path)
    try:
        await database.check_ready()
    finally:
        await database.close()
