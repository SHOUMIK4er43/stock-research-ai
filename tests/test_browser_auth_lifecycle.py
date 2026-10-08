"""Offline lifecycle regressions; no live credentials, jobs or provider calls."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.application.browser_jobs import BrowserJobManager, BrowserResearchJob
from stock_research_ai.config import Settings
from stock_research_ai.interfaces.http import create_app

ORIGIN = "chrome-extension://ieplfiijheknpihonflfnnofbdlciepa"
OTHER = "chrome-extension://" + "a" * 32
JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
READ_PATH = f"/v1/research-jobs/{JOB_ID}"


@pytest.fixture
async def lifecycle(engine, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "unused.sqlite3",
        artifact_path=tmp_path / "artifacts",
        browser_auth_path=tmp_path / "auth.json",
        browser_extension_origin=ORIGIN,
    )
    now = datetime.now(UTC)
    job = BrowserResearchJob(
        job_id=JOB_ID,
        portal="SCREENER",
        source_url="https://www.screener.in/company/INFY/",
        research_plan="FULL_RESEARCH_V1",
        status="RUNNING",
        created_at=now,
        updated_at=now,
    )
    # Seed a read result; never launch a job or execute research.
    monkeypatch.setattr(BrowserJobManager, "get", lambda self, job_id: job)
    app = create_app(engine=engine, settings=settings)
    store = PairingStore(settings.browser_auth_path)  # separate CLI-style instance
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client:
            yield client, store
    assert not settings.database_path.exists()


@pytest.mark.parametrize("origin", [ORIGIN, None, OTHER, "https://evil.example", "null", ""])
async def test_pair_then_immediate_protected_read(lifecycle, origin):
    client, store = lifecycle
    response = await client.post(
        "/v1/pair",
        headers={"Origin": ORIGIN},
        json={"pairing_code": store.issue_pairing_code()},
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    if origin is not None:
        headers["Origin"] = origin
    result = await client.get(READ_PATH, headers=headers)
    assert result.status_code == (200 if origin in (ORIGIN, None) else 401)
    if result.status_code == 200:
        assert result.json()["job_id"] == str(JOB_ID)
    assert result.headers.get("access-control-allow-origin") == (
        ORIGIN if origin == ORIGIN else None
    )


@pytest.mark.parametrize("token", [None, "invalid"])
async def test_missing_or_invalid_bearer_without_origin_is_refused(lifecycle, token):
    client, store = lifecycle
    await client.post(
        "/v1/pair", headers={"Origin": ORIGIN}, json={"pairing_code": store.issue_pairing_code()}
    )
    response = await client.get(
        READ_PATH, headers={"Authorization": f"Bearer {token}"} if token else {}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("origin", [None, OTHER, "https://evil.example", "null", ""])
async def test_pair_requires_exact_configured_origin_without_consuming_code(lifecycle, origin):
    client, store = lifecycle
    code = store.issue_pairing_code()
    result = await client.post(
        "/v1/pair",
        json={"pairing_code": code},
        headers={"Origin": origin} if origin is not None else {},
    )
    assert result.status_code in (400, 401, 403)
    assert store.paired_origin() is None
    assert "access-control-allow-origin" not in result.headers
    result = await client.post("/v1/pair", json={"pairing_code": code}, headers={"Origin": ORIGIN})
    assert result.status_code == 200
    reused = await client.post("/v1/pair", json={"pairing_code": code}, headers={"Origin": ORIGIN})
    assert reused.status_code == 401


@pytest.mark.parametrize("origin", [ORIGIN, OTHER, "https://evil.example", "*"])
@pytest.mark.parametrize("path", ["/v1/pair", "/health", "/v1/version", READ_PATH])
async def test_cors_is_exact_configured_origin_only(lifecycle, origin, path):
    client, store = lifecycle
    await client.post(
        "/v1/pair", headers={"Origin": ORIGIN}, json={"pairing_code": store.issue_pairing_code()}
    )
    response = await client.options(
        path, headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
    )
    assert response.headers.get("access-control-allow-origin") == (
        ORIGIN if origin == ORIGIN else None
    )


async def test_unconfigured_pairing_fails_closed(engine, tmp_path):
    settings = Settings(
        _env_file=None, browser_extension_origin=None, browser_auth_path=tmp_path / "auth.json"
    )
    store = PairingStore(settings.browser_auth_path)
    app = create_app(engine=engine, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.post(
            "/v1/pair",
            headers={"Origin": ORIGIN},
            json={"pairing_code": store.issue_pairing_code()},
        )
        assert response.status_code == 401
        assert store.paired_origin() is None
        assert "access-control-allow-origin" not in response.headers


def test_code_generation_preserves_token_until_successful_repair(tmp_path):
    path = tmp_path / "auth.json"
    service = PairingStore(path)
    cli = PairingStore(path)
    old = service.complete_pairing(ORIGIN, cli.issue_pairing_code())
    for _ in range(3):
        code = cli.issue_pairing_code()
        assert service.verify(ORIGIN, old)
        assert service.paired_origin() == ORIGIN
    new = service.complete_pairing(ORIGIN, code)
    assert service.verify(ORIGIN, new)
    assert not service.verify(ORIGIN, old)
    assert not service.verify(None, old)
    assert service.verify(None, new)
    assert PairingStore(path).verify(ORIGIN, new)
