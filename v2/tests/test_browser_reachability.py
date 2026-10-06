"""Exact approved-origin readiness before pairing; temporary auth state, no network."""

from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError

from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.config import Settings
from stock_research_ai.interfaces.http import create_app

ORIGIN = "chrome-extension://ieplifjjheknpihonflfnnofbdlciepa"
OTHER = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@asynccontextmanager
async def client_for(engine, tmp_path, approved=ORIGIN, paired=None):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "unused.sqlite3",
        artifact_path=tmp_path / "raw",
        browser_auth_path=tmp_path / "auth.json",
        browser_extension_origin=approved,
    )
    store = PairingStore(settings.browser_auth_path)
    token = store.complete_pairing(paired, store.issue_pairing_code()) if paired else None
    app = create_app(engine=engine, settings=settings, pairing_store=store)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client:
            yield client, store, token
    assert not settings.database_path.exists()


@pytest.mark.parametrize("path", ["/health", "/v1/version"])
@pytest.mark.parametrize("method", ["GET", "OPTIONS"])
async def test_actual_extension_probe_has_exact_cors_before_pairing(engine, tmp_path, path, method):
    async with client_for(engine, tmp_path) as (client, store, _):
        response = await client.request(
            method, path, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
        )
        assert response.status_code == (204 if method == "OPTIONS" else 200)
        assert response.headers.get("access-control-allow-origin") == ORIGIN
        assert response.headers["vary"] == "Origin"
        assert store.paired_origin() is None  # readiness never pairs or issues a token
        if method == "GET":
            assert "token" not in response.json()
            assert "api_key" not in response.text
            if path == "/health":
                assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("origin", [OTHER, "*", "https://evil.example", "null", ORIGIN + "/"])
@pytest.mark.parametrize("path", ["/health", "/v1/version"])
async def test_other_origins_cannot_read_readiness(engine, tmp_path, origin, path):
    async with client_for(engine, tmp_path) as (client, _, _):
        for method in ("GET", "OPTIONS"):
            response = await client.request(
                method, path, headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
            )
            assert "access-control-allow-origin" not in response.headers


async def test_no_implicit_approved_origin(engine, tmp_path):
    async with client_for(engine, tmp_path, approved=None) as (client, _, _):
        response = await client.get("/health", headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert "access-control-allow-origin" not in response.headers


async def test_stored_pairing_cannot_expand_configured_cors_origin(engine, tmp_path):
    async with client_for(engine, tmp_path, paired=OTHER) as (client, _, _):
        for path in ("/health", "/v1/version"):
            allowed = await client.get(path, headers={"Origin": OTHER})
            assert "access-control-allow-origin" not in allowed.headers
            denied = await client.get(path, headers={"Origin": ORIGIN})
            assert "access-control-allow-origin" not in denied.headers


async def test_bootstrap_allowance_does_not_expose_other_routes(engine, tmp_path):
    async with client_for(engine, tmp_path) as (client, _, _):
        for path in ("/v1/capabilities", "/v1/research-jobs", "/health/", "/v1/version/"):
            preflight = await client.options(
                path, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "GET"}
            )
            assert "access-control-allow-origin" not in preflight.headers
        response = await client.post(
            "/v1/research-jobs",
            headers={"Origin": ORIGIN},
            json={"portal": "SCREENER", "source_url": "https://www.screener.in/company/INFY/"},
        )
        assert response.status_code == 401
        assert "access-control-allow-origin" not in response.headers


async def test_pairing_and_exact_bearer_origin_security(engine, tmp_path):
    async with client_for(engine, tmp_path) as (client, store, _):
        # Pairing bootstrap is restricted to the configured exact extension origin.
        preflight = await client.options(
            "/v1/pair", headers={"Origin": OTHER, "Access-Control-Request-Method": "POST"}
        )
        assert "access-control-allow-origin" not in preflight.headers
        code = store.issue_pairing_code()
        wrong_field = await client.post("/v1/pair", headers={"Origin": ORIGIN}, json={"code": code})
        assert wrong_field.status_code == 422
        result = await client.post(
            "/v1/pair", headers={"Origin": ORIGIN}, json={"pairing_code": code}
        )
        assert result.status_code == 200
        token = result.json()["token"]
        reused = await client.post(
            "/v1/pair", headers={"Origin": ORIGIN}, json={"pairing_code": code}
        )
        assert reused.status_code == 401
        path = "/v1/research-jobs/00000000-0000-0000-0000-000000000000"
        allowed = await client.get(
            path, headers={"Origin": ORIGIN, "Authorization": f"Bearer {token}"}
        )
        assert allowed.status_code == 404  # authenticated; ID is unknown, no job launched
        assert allowed.headers["access-control-allow-origin"] == ORIGIN
        for headers in ({"Origin": ORIGIN}, {"Origin": OTHER, "Authorization": f"Bearer {token}"}):
            assert (await client.get(path, headers=headers)).status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://evil.example",
        ORIGIN + "/",
        ORIGIN + "?x=1",
        "chrome-extension://short",
        "chrome-extension://" + "z" * 32,
    ],
)
def test_approved_origin_must_be_one_exact_chrome_extension_origin(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, browser_extension_origin=origin)


def test_approved_origin_is_loaded_from_explicit_nonsecret_environment(monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", ORIGIN)
    assert Settings(_env_file=None).browser_extension_origin == ORIGIN
