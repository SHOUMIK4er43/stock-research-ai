import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import copy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import ValidationError

from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.adapters.sqlite import DatabaseNotReadyError, SQLiteDatabase
from stock_research_ai.application.engine import Capabilities, ResearchEngine
from stock_research_ai.config import Settings
from stock_research_ai.domain.company import Company, CompanyRegistration
from stock_research_ai.interfaces import http as http_interface

ORIGIN = "chrome-extension://ieplfiijheknpihonflfnnofbdlciepa"
LOOPBACK = "http://127.0.0.1"


def auth_settings(tmp_path: Path, **kw: object) -> Settings:
    kw.setdefault("database_path", tmp_path / "unused.sqlite3")
    return Settings(
        _env_file=None,
        artifact_path=tmp_path / "artifacts",
        browser_auth_path=tmp_path / "auth.json",
        browser_extension_origin=ORIGIN,
        **kw,
    )


async def pair(client: httpx.AsyncClient, settings: Settings) -> dict[str, str]:
    code = PairingStore(settings.browser_auth_path).issue_pairing_code()
    paired = await client.post("/v1/pair", headers={"Origin": ORIGIN}, json={"pairing_code": code})
    assert paired.status_code == 200
    return {"Authorization": f"Bearer {paired.json()['token']}"}


async def test_http_company_round_trip(engine: ResearchEngine, tmp_path: Path) -> None:
    settings = auth_settings(tmp_path)
    app = http_interface.create_app(engine=engine, settings=settings)
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
        ("/v1/version", "GET"),
        ("/v1/pair", "POST"),
        ("/v1/research-jobs", "POST"),
        ("/v1/research-jobs/{job_id}", "GET"),
        ("/v1/reports/{report_id}", "GET"),
        ("/v1/reports/{report_id}/artifacts/{artifact_kind}", "GET"),
        ("/v1/summaries", "POST"),
    }
    assert len(app.routes) == 11
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            capabilities = await client.get("/v1/capabilities")
            assert capabilities.status_code == 200
            assert capabilities.json() == (await engine.get_capabilities()).model_dump(mode="json")
            assert capabilities.json()["analysis_available"] is False
            auth = await pair(client, settings)

            registration = {
                "name": "Yes Bank",
                "listings": [{"exchange": "NSE", "symbol": "YESBANK"}],
                "provider_identifiers": [{"provider": "screener", "identifier": "YESBANK"}],
            }
            created = await client.post("/v1/companies", json=registration, headers=auth)
            assert created.status_code == 201
            company = Company.model_validate(created.json())
            assert company.name == "Yes Bank"
            assert company.listings[0].symbol == "YESBANK"

            fetched = await client.get(f"/v1/companies/{company.id}")
            assert fetched.status_code == 200
            assert fetched.json() == created.json()
            assert await engine.get_company(company.id) == company


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("live_optional", [False, True])
async def test_capabilities_use_browser_execution_engine(
    engine: ResearchEngine,
    monkeypatch: pytest.MonkeyPatch,
    injected: bool,
    live_optional: bool,
) -> None:
    # Default startup owns distinct company and browser engines. Only the latter
    # executes browser jobs; injected startup must use that same supplied engine.
    browser = copy(engine)
    expected = Capabilities(
        analysis_available=True,
        deterministic_challenge_available=True,
        live_challenger_available=live_optional,
        deterministic_report_assembly_available=True,
        live_report_narrator_available=live_optional,
    )

    async def browser_capabilities() -> Capabilities:
        return expected

    monkeypatch.setattr(browser, "get_capabilities", browser_capabilities)
    assert (await engine.get_capabilities()).analysis_available is False

    @asynccontextmanager
    async def company_context(_: Settings) -> AsyncIterator[ResearchEngine]:
        assert not injected
        yield engine

    @asynccontextmanager
    async def browser_context(_: Settings) -> AsyncIterator[tuple[object, object, object]]:
        assert not injected
        yield browser, object(), object()

    monkeypatch.setattr(http_interface, "research_engine", company_context)
    monkeypatch.setattr(http_interface, "browser_engine", browser_context)
    app = http_interface.create_app(engine=browser if injected else None)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/v1/capabilities")
    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")


async def test_http_validation_and_missing_company(engine: ResearchEngine, tmp_path: Path) -> None:
    settings = auth_settings(tmp_path)
    app = http_interface.create_app(engine=engine, settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get("/v1/companies/not-a-uuid")).status_code == 422
            missing = await client.get(f"/v1/companies/{uuid4()}")
            assert missing.status_code == 404
            assert missing.json() == {"detail": "Company not found"}
            auth = await pair(client, settings)
            assert (await client.post("/v1/companies", json={}, headers=auth)).status_code == 422
            blank = await client.post("/v1/companies", json={"name": "   "}, headers=auth)
            assert blank.status_code == 422


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

    @asynccontextmanager
    async def tracked_browser_engine(_: Settings) -> AsyncIterator[tuple[object, object, object]]:
        events.append("browser-opened")
        try:
            yield engine, object(), object()
        finally:
            events.append("browser-closed")

    monkeypatch.setattr(http_interface, "research_engine", tracked_engine)
    monkeypatch.setattr(http_interface, "browser_engine", tracked_browser_engine)
    app = http_interface.create_app(engine=engine if injected else None)
    assert events == []
    async with app.router.lifespan_context(app):
        assert events == ([] if injected else ["opened", "browser-opened"])
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.get("/v1/capabilities")).status_code == 200
    assert events == (
        [] if injected else ["opened", "browser-opened", "browser-closed", "closed"]
    )


async def test_http_bootstrap_persists_company_to_sqlite(
    database: SQLiteDatabase, tmp_path: Path
) -> None:
    settings = auth_settings(tmp_path, database_path=database.path)
    app = http_interface.create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            created = await client.post(
                "/v1/companies",
                json={"name": "Persistent HTTP Company"},
                headers=await pair(client, settings),
            )
            assert created.status_code == 201
            company = Company.model_validate(created.json())
    assert await database.repository.get(company.id) == company

    # A fresh application lifecycle reads the persisted record, with no shared engine.
    reopened = http_interface.create_app(settings=settings)
    async with reopened.router.lifespan_context(reopened):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=reopened), base_url="http://127.0.0.1"
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


# --- P0 security boundaries: company registration auth, request size, trusted Host -------


@asynccontextmanager
async def paired_client(
    engine: ResearchEngine, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str], FastAPI]]:
    settings = auth_settings(tmp_path)
    app = http_interface.create_app(engine=engine, settings=settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=LOOPBACK
        ) as client:
            yield client, await pair(client, settings), app


async def test_company_registration_requires_paired_bearer(
    engine: ResearchEngine, tmp_path: Path
) -> None:
    body = {"name": "Infosys", "listings": [{"exchange": "NSE", "symbol": "INFY"}]}
    async with paired_client(engine, tmp_path) as (client, auth, _):
        refused = [
            await client.post("/v1/companies", json=body),
            await client.post("/v1/companies", json=body, headers={"Authorization": "Bearer x"}),
            await client.post("/v1/companies", json=body, headers={"Authorization": "x"}),
            await client.post(
                "/v1/companies", json=body, headers={**auth, "Origin": "https://evil.example"}
            ),
            await client.post(
                "/v1/companies",
                json=body,
                headers={**auth, "Origin": "chrome-extension://" + "a" * 32},
            ),
        ]
        assert [r.status_code for r in refused] == [401] * 5
        assert all("access-control-allow-origin" not in r.headers for r in refused)
        accepted = await client.post("/v1/companies", json=body, headers=auth)
        assert accepted.status_code == 201
        with_origin = await client.post(
            "/v1/companies", json=body, headers={**auth, "Origin": ORIGIN}
        )
        assert with_origin.status_code == 201
        assert with_origin.headers["access-control-allow-origin"] == ORIGIN
        assert (await client.get(f"/v1/companies/{accepted.json()['id']}")).status_code == 200


@pytest.mark.parametrize(
    "registration",
    [
        {"name": "x" * 301},
        {"name": "x" * 2_000_000},
        {"name": "Infosys", "listings": [{"exchange": "N" * 33, "symbol": "INFY"}]},
        {"name": "Infosys", "listings": [{"exchange": "NSE", "symbol": "I" * 65}]},
        {"name": "Infosys", "provider_identifiers": [{"provider": "p" * 65, "identifier": "1"}]},
        {"name": "Infosys", "provider_identifiers": [{"provider": "s", "identifier": "1" * 257}]},
        {"name": "Infosys", "listings": [{"symbol": f"S{i}"} for i in range(33)]},
        {
            "name": "Infosys",
            "provider_identifiers": [{"provider": "s", "identifier": str(i)} for i in range(33)],
        },
    ],
)
async def test_company_registration_fields_are_bounded(
    engine: ResearchEngine, tmp_path: Path, registration: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        CompanyRegistration.model_validate(registration)
    async with paired_client(engine, tmp_path) as (client, auth, _):
        response = await client.post("/v1/companies", json=registration, headers=auth)
    # A multi-megabyte body never reaches validation: the transport bound refuses it first.
    assert response.status_code == (413 if len(response.request.content) > 128 * 1024 else 422)


async def test_company_registration_at_field_limits_is_accepted(
    engine: ResearchEngine, tmp_path: Path
) -> None:
    registration = {
        "name": "N" * 300,
        "listings": [{"exchange": "E" * 32, "symbol": f"{i:0>64}"} for i in range(32)],
        "provider_identifiers": [
            {"provider": "P" * 64, "identifier": f"{i:0>256}"} for i in range(32)
        ],
    }
    async with paired_client(engine, tmp_path) as (client, auth, _):
        created = await client.post("/v1/companies", json=registration, headers=auth)
    assert created.status_code == 201
    company = Company.model_validate(created.json())
    assert await engine.get_company(company.id) == company


async def asgi_post(
    app: FastAPI, headers: list[tuple[bytes, bytes]], chunks: list[bytes]
) -> tuple[object, int]:
    """Drive the ASGI app directly, so Content-Length can be omitted or false."""
    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    consumed = 0
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal consumed
        if pending:
            consumed += 1
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/companies",
        "raw_path": b"/v1/companies",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json"), *headers],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], consumed


async def test_request_body_bound_cannot_be_bypassed(
    engine: ResearchEngine, tmp_path: Path
) -> None:
    limit = http_interface.MAX_REQUEST_BODY_BYTES
    chunk = b"x" * (16 * 1024)
    many = [chunk] * 1000  # ~16 MB if fully read
    async with paired_client(engine, tmp_path) as (client, auth, app):
        token = [(b"authorization", auth["Authorization"].encode())]
        # Honest oversize Content-Length: refused before any body is read.
        response = await client.post(
            "/v1/companies",
            content=b"x" * (limit + 1),
            headers={**auth, "content-type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["detail"]["code"] == "REQUEST_TOO_LARGE"
        # No Content-Length (chunked) and a false small Content-Length: counted, then refused
        # as soon as the bound is crossed - the rest of the stream is never consumed.
        for declared in ([], [(b"content-length", b"10")]):
            status, consumed = await asgi_post(app, token + declared, many)
            assert status == 413
            assert consumed == limit // len(chunk) + 1
        status, consumed = await asgi_post(app, token + [(b"content-length", b"-1")], [b"{}"])
        assert (status, consumed) == (400, 0)
        status, _ = await asgi_post(
            app, token + [(b"content-length", b"2"), (b"content-length", b"2")], [b"{}"]
        )
        assert status == 400
        # A legitimate chunked registration within the bound is replayed intact.
        body = b'{"name": "Chunked Co"}'
        status, _ = await asgi_post(app, token, [body[:5], body[5:]])
        assert status == 201


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("127.0.0.1", True),
        ("127.0.0.1:8000", True),
        ("localhost", True),
        ("localhost:8000", True),
        ("rebind.evil.example:8000", False),
        ("evil.example", False),
        ("localhost.evil.example", False),
        ("127.0.0.1.nip.io:8000", False),
        ("192.168.1.20:8000", False),
        ("0.0.0.0:8000", False),
        ("", False),
    ],
)
async def test_only_loopback_host_headers_are_served(
    engine: ResearchEngine, tmp_path: Path, host: str, allowed: bool
) -> None:
    body = {"name": "Rebind Co"}
    async with paired_client(engine, tmp_path) as (client, auth, _):
        health = await client.get("/health", headers={"Host": host})
        version = await client.get("/v1/version", headers={"Host": host})
        created = await client.post("/v1/companies", json=body, headers={**auth, "Host": host})
        unauthenticated = await client.post("/v1/companies", json=body, headers={"Host": host})
        foreign = await client.post(
            "/v1/companies",
            json=body,
            headers={**auth, "Host": host, "Origin": "https://evil.example"},
        )
    if allowed:
        assert (health.status_code, version.status_code, created.status_code) == (200, 200, 201)
        assert unauthenticated.status_code == foreign.status_code == 401
    else:
        statuses = {r.status_code for r in (health, version, created, unauthenticated, foreign)}
        assert statuses == {400}
