"""Part 6B-A: full HTTP integration tests for the local browser-facing API. All network
mocked (Screener/documents via httpx.MockTransport, no Anthropic call - the analyst
model is the same offline, bundle-inspecting fake test_research_plan.py already uses).
No real external call is ever made.
"""

import asyncio
from urllib.parse import quote

import httpx
import pytest
from test_research_plan import _DOCUMENT_TEXT, YESBANK_URL, _fake_dns_lookup, _screener_handler
from test_research_plan import _DynamicFakeAnalystModel as _DynamicFakeAnalystModel  # noqa: F401
from test_research_plan import full_research_engine as full_research_engine  # noqa: F401

from stock_research_ai.adapters.documents.pdf import PDFNormalizer
from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.sector import ScreenerBroadIndustrySource
from stock_research_ai.adapters.screener.semantic_mappings import ScreenerSemanticResolver
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.browser_jobs import BrowserJobManager
from stock_research_ai.application.engine import (
    AnalystServices,
    ChallengeServices,
    DocumentServices,
    ExtractionServices,
    ReportServices,
    ResearchEngine,
    SectorKPIServices,
    ValuationServices,
)
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import AnalystExecutionSpec
from stock_research_ai.interfaces import http as http_interface

EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
EVIL_ORIGIN = "https://evil.example"


@pytest.fixture(autouse=True)
def configured_browser_origin(monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", EXTENSION_ORIGIN)


def _execution_factory() -> AnalystExecutionSpec:
    return AnalystExecutionSpec(template_id="full-research-v1", template_version=1)


@pytest.fixture
async def paired(full_research_engine, tmp_path):
    engine, store, model = full_research_engine
    job_manager = BrowserJobManager(
        engine, store, ScreenerBroadIndustrySource(), _execution_factory
    )
    pairing_store = PairingStore(tmp_path / "auth.json")
    token = pairing_store.complete_pairing(EXTENSION_ORIGIN, pairing_store.issue_pairing_code())
    app = http_interface.create_app(
        engine=engine, job_manager=job_manager, pairing_store=pairing_store
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, token, job_manager, model, pairing_store


def _auth(token: str) -> dict[str, str]:
    return {"Origin": EXTENSION_ORIGIN, "Authorization": f"Bearer {token}"}


async def _wait_completed(client, headers, job_id, timeout=10.0):
    async def poll():
        while True:
            response = await client.get(f"/v1/research-jobs/{job_id}", headers=headers)
            body = response.json()
            if body["status"] in ("COMPLETED", "FAILED"):
                return body
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout)


# --- HEALTH / VERSION --------------------------------------------------------------


async def test_health_is_cheap_and_secret_free(paired):
    client, _token, _jm, _model, _ps = paired
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_version_reports_expected_fields_and_no_secrets(paired):
    client, _token, _jm, _model, _ps = paired
    response = await client.get("/v1/version")
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "v1"
    assert body["supported_portals"] == ["SCREENER"]
    assert body["supported_research_plans"] == ["FULL_RESEARCH_V1"]
    assert "report_schema_version" in body
    assert "application_version" in body


async def test_application_version_matches_the_single_authoritative_source(paired):
    """Part 6C-A: application_version must come from importlib.metadata (backed by
    pyproject.toml's [project].version), never a second hand-duplicated string that
    could silently drift from it."""
    import importlib.metadata

    client, _token, _jm, _model, _ps = paired
    response = await client.get("/v1/version")
    assert response.json()["application_version"] == importlib.metadata.version(
        "stock-research-ai"
    )


# --- PAIRING (HTTP layer) -----------------------------------------------------------


async def test_pair_via_http_succeeds_for_extension_origin(tmp_path, full_research_engine):
    engine, store, _model = full_research_engine
    job_manager = BrowserJobManager(
        engine, store, ScreenerBroadIndustrySource(), _execution_factory
    )
    pairing_store = PairingStore(tmp_path / "auth.json")
    code = pairing_store.issue_pairing_code()
    app = http_interface.create_app(
        engine=engine, job_manager=job_manager, pairing_store=pairing_store
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/pair",
                json={"pairing_code": code},
                headers={"Origin": EXTENSION_ORIGIN},
            )
            assert response.status_code == 200
            assert "token" in response.json() and len(response.json()["token"]) > 20


async def test_pair_with_invalid_code_is_refused(paired):
    client, _token, _jm, _model, pairing_store = paired
    pairing_store.issue_pairing_code()
    response = await client.post(
        "/v1/pair", json={"pairing_code": "wrong"}, headers={"Origin": EXTENSION_ORIGIN}
    )
    assert response.status_code == 401


async def test_pair_is_locked_out_with_429_after_5_failed_attempts_within_60s(paired):
    """Part 6C-A pairing brute-force hardening, exercised through the real HTTP route."""
    client, _token, _jm, _model, pairing_store = paired
    pairing_store.issue_pairing_code()
    for _ in range(5):
        response = await client.post(
            "/v1/pair", json={"pairing_code": "wrong"}, headers={"Origin": EXTENSION_ORIGIN}
        )
        assert response.status_code == 401

    locked = await client.post(
        "/v1/pair", json={"pairing_code": "wrong"}, headers={"Origin": EXTENSION_ORIGIN}
    )
    assert locked.status_code == 429
    assert locked.json()["detail"]["code"] == "PAIRING_LOCKED_OUT"


async def test_pair_without_origin_header_is_refused(paired):
    client, _token, _jm, _model, pairing_store = paired
    code = pairing_store.issue_pairing_code()
    response = await client.post("/v1/pair", json={"pairing_code": code})
    assert response.status_code == 400


# --- AUTH ----------------------------------------------------------------------------


async def test_valid_origin_and_token_succeeds(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.get("/v1/research-jobs/00000000-0000-0000-0000-000000000000",
                                 headers=_auth(token))
    assert response.status_code == 404  # authorized, just an unknown job


@pytest.mark.parametrize(
    "headers_fn",
    [
        lambda token: {"Origin": EVIL_ORIGIN, "Authorization": f"Bearer {token}"},
        lambda token: {"Origin": EXTENSION_ORIGIN},  # missing token
        lambda token: {"Origin": EXTENSION_ORIGIN, "Authorization": "Bearer wrong-token"},
        lambda token: {"Origin": EVIL_ORIGIN, "Authorization": f"Bearer {token}"},
    ],
)
async def test_protected_route_rejects_every_incomplete_or_wrong_credential(paired, headers_fn):
    client, token, _jm, _model, _ps = paired
    response = await client.get(
        "/v1/research-jobs/00000000-0000-0000-0000-000000000000", headers=headers_fn(token)
    )
    assert response.status_code == 401


async def test_normal_https_website_cannot_launch_research(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers={"Origin": EVIL_ORIGIN, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401


# --- CORS ------------------------------------------------------------------------


async def test_no_wildcard_cors_header_ever(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.get("/v1/version", headers={"Origin": EXTENSION_ORIGIN})
    assert response.headers.get("access-control-allow-origin") != "*"
    response = await client.get(
        "/v1/research-jobs/00000000-0000-0000-0000-000000000000", headers=_auth(token)
    )
    assert response.headers.get("access-control-allow-origin") != "*"


async def test_paired_origin_is_reflected_in_cors_header(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.get(
        "/v1/research-jobs/00000000-0000-0000-0000-000000000000", headers=_auth(token)
    )
    assert response.headers.get("access-control-allow-origin") == EXTENSION_ORIGIN


async def test_arbitrary_web_origin_gets_no_cors_header(paired):
    client, _token, _jm, _model, _ps = paired
    response = await client.get("/v1/version", headers={"Origin": EVIL_ORIGIN})
    assert "access-control-allow-origin" not in response.headers


async def test_unconfigured_extension_pairing_preflight_is_denied(paired):
    client, _token, _jm, _model, _ps = paired
    response = await client.options(
        "/v1/pair",
        headers={
            "Origin": "chrome-extension://someunpairedidxxxxxxxxxxxxxxxxxx",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 204
    assert "access-control-allow-origin" not in response.headers


async def test_preflight_for_a_normal_web_origin_is_not_accommodated(paired):
    client, _token, _jm, _model, _ps = paired
    response = await client.options(
        "/v1/research-jobs",
        headers={"Origin": EVIL_ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers


# --- RESEARCH REQUEST --------------------------------------------------------------


async def test_valid_screener_request_is_accepted(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    assert response.status_code == 202
    assert response.json()["status"] in ("QUEUED", "RUNNING")


async def test_unknown_portal_is_rejected(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.post(
        "/v1/research-jobs",
        json={"portal": "TRADINGVIEW", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.example/company/YESBANK/",
        "http://127.0.0.1/company/YESBANK/",
        "https://localhost/company/YESBANK/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hello",
        "https://www.screener.in/login/",
    ],
)
async def test_unsupported_source_urls_are_rejected(paired, bad_url):
    client, token, _jm, _model, _ps = paired
    response = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": bad_url},
        headers=_auth(token),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UNSUPPORTED_SOURCE_URL"


# --- JOBS ----------------------------------------------------------------------------


async def test_job_completes_and_is_pollable(paired):
    client, token, _jm, _model, _ps = paired
    launch = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    job_id = launch.json()["job_id"]
    final = await _wait_completed(client, _auth(token), job_id)
    assert final["status"] == "COMPLETED"
    assert final["report_snapshot_id"] is not None


async def test_stale_unknown_job_id_returns_typed_lost_state(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.get(
        "/v1/research-jobs/11111111-1111-1111-1111-111111111111", headers=_auth(token)
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "JOB_LOST_AFTER_SERVER_RESTART"


async def test_duplicate_concurrent_request_returns_the_same_job(paired):
    client, token, _jm, _model, _ps = paired
    first = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    second = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    assert first.json()["job_id"] == second.json()["job_id"]
    await _wait_completed(client, _auth(token), first.json()["job_id"])


# --- REPORTS / ARTIFACTS -------------------------------------------------------------


async def test_report_and_all_three_artifacts_are_retrievable(paired):
    client, token, _jm, _model, _ps = paired
    launch = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    final = await _wait_completed(client, _auth(token), launch.json()["job_id"])
    report_id = final["report_snapshot_id"]

    report = await client.get(f"/v1/reports/{report_id}", headers=_auth(token))
    assert report.status_code == 200
    assert report.json()["id"] == report_id

    for kind, content_type in (
        ("INSTITUTIONAL_REPORT", "text/markdown"),
        ("INVESTOR_BRIEF", "text/markdown"),
        ("RESEARCH_JSON", "application/json"),
        ("REPORT_PDF", "application/pdf"),
    ):
        artifact = await client.get(
            f"/v1/reports/{report_id}/artifacts/{kind}", headers=_auth(token)
        )
        assert artifact.status_code == 200
        assert content_type in artifact.headers["content-type"]
        assert len(artifact.text) > 0


async def test_unknown_report_id_is_rejected(paired):
    client, token, _jm, _model, _ps = paired
    response = await client.get(
        "/v1/reports/22222222-2222-2222-2222-222222222222", headers=_auth(token)
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "traversal",
    [
        "../../.env",
        "/etc/passwd",
        "..%2Fdatabase.sqlite",
        "../database.sqlite",
        "RESEARCH_JSON/../..",
    ],
)
async def test_artifact_path_traversal_attempts_are_rejected(paired, traversal):
    """Sent fully percent-encoded so httpx/the ASGI router treat it as one opaque
    artifact_kind path-parameter value (never as relative path segments the CLIENT
    itself would normalize away) - this is what proves the SERVER never accepts a
    filesystem path, independent of any client-side URL normalization behavior."""
    client, token, _jm, _model, _ps = paired
    launch = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    final = await _wait_completed(client, _auth(token), launch.json()["job_id"])
    encoded = quote(traversal, safe="")
    response = await client.get(
        f"/v1/reports/{final['report_snapshot_id']}/artifacts/{encoded}", headers=_auth(token)
    )
    # A value containing a real "/" once decoded never matches the single-segment
    # {artifact_kind} route at all (404, rejected before any handler runs); a value
    # without "/" reaches the handler and fails the closed Literal validation (422).
    # Either way, no filesystem path is ever accepted or used.
    assert response.status_code in (404, 422)
    assert "root:" not in response.text
    assert "ANALYST_API_KEY" not in response.text


async def test_arbitrary_artifact_kind_is_rejected(paired):
    client, token, _jm, _model, _ps = paired
    launch = await client.post(
        "/v1/research-jobs",
        json={"portal": "SCREENER", "source_url": YESBANK_URL},
        headers=_auth(token),
    )
    final = await _wait_completed(client, _auth(token), launch.json()["job_id"])
    response = await client.get(
        f"/v1/reports/{final['report_snapshot_id']}/artifacts/NOT_A_REAL_KIND",
        headers=_auth(token),
    )
    assert response.status_code == 422


# --- MISSING ANALYST CONFIG ---------------------------------------------------------


@pytest.fixture
async def unconfigured_analyst_engine(tmp_path, make_document_pdf):
    settings = Settings(
        database_path=tmp_path / "noanalyst.sqlite3", artifact_path=tmp_path / "noanalyst-raw"
    )
    await initialize_database(settings)
    database = SQLiteDatabase(settings.database_path)
    await database.check_ready()
    store = FileRawArtifactStore(settings.artifact_path)
    document_pdf_bytes = make_document_pdf(texts=(_DOCUMENT_TEXT,))

    def document_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=document_pdf_bytes, headers={"content-type": "application/pdf"}
        )

    engine = ResearchEngine(
        database.repository,
        extraction=ExtractionServices(
            source=ScreenerSource(httpx.MockTransport(_screener_handler)),
            artifacts=store,
            normalizer=ScreenerNormalizer(),
            repository=database.extractions,
        ),
        documents=DocumentServices(
            DocumentSource(httpx.MockTransport(document_handler), lookup=_fake_dns_lookup),
            store,
            PDFNormalizer(),
            database.documents,
        ),
        sector_kpis=SectorKPIServices(
            database.extractions, database.calculations, ScreenerSemanticResolver(), store
        ),
        valuations=ValuationServices(
            database.extractions,
            database.market,
            database.calculations,
            ScreenerSemanticResolver(),
            store,
            database.valuations,
        ),
        challenge=ChallengeServices(
            database.extractions,
            database.market,
            database.calculations,
            database.patterns,
            database.market_calculations,
            database.valuations,
            database.documents,
            database.context,
            store,
            database.analyses,
            database.challenges,
        ),
        report=ReportServices(
            database.extractions,
            database.market,
            database.calculations,
            database.patterns,
            database.market_calculations,
            database.valuations,
            database.documents,
            database.context,
            store,
            database.challenges,
            database.reports,
        ),
        # Deliberately no analyst= kwarg: this is the "no live provider configured" case.
    )
    try:
        yield engine, store
    finally:
        await database.close()


async def test_service_boots_and_health_works_without_analyst_configured(
    unconfigured_analyst_engine, tmp_path
):
    engine, store = unconfigured_analyst_engine
    job_manager = BrowserJobManager(
        engine, store, ScreenerBroadIndustrySource(), _execution_factory
    )
    pairing_store = PairingStore(tmp_path / "auth.json")
    token = pairing_store.complete_pairing(EXTENSION_ORIGIN, pairing_store.issue_pairing_code())
    app = http_interface.create_app(
        engine=engine, job_manager=job_manager, pairing_store=pairing_store
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/v1/version")).status_code == 200
            capabilities = await client.get("/v1/capabilities")
            assert capabilities.json()["analysis_available"] is False

            launch = await client.post(
                "/v1/research-jobs",
                json={"portal": "SCREENER", "source_url": YESBANK_URL},
                headers=_auth(token),
            )
            assert launch.status_code == 202
            final = await _wait_completed(client, _auth(token), launch.json()["job_id"])
            assert final["status"] == "FAILED"
            assert final["failure"]["code"] == "ANALYST_UNAVAILABLE"


# --- PERSISTENCE BOUNDARY: report survives restart, job does not -------------------


def _build_full_engine(settings: Settings, screener_handler, document_pdf_bytes):
    """Builds a fresh ResearchEngine/SQLiteDatabase pair against `settings.database_path`
    - calling this twice against the SAME path, with independent SQLiteDatabase
    connections, is exactly what simulates two separate backend process lifetimes
    sharing one persistent database."""
    database = SQLiteDatabase(settings.database_path)
    store = FileRawArtifactStore(settings.artifact_path)

    def document_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=document_pdf_bytes, headers={"content-type": "application/pdf"}
        )

    model = _DynamicFakeAnalystModel()
    engine = ResearchEngine(
        database.repository,
        extraction=ExtractionServices(
            source=ScreenerSource(httpx.MockTransport(screener_handler)),
            artifacts=store,
            normalizer=ScreenerNormalizer(),
            repository=database.extractions,
        ),
        documents=DocumentServices(
            DocumentSource(httpx.MockTransport(document_handler), lookup=_fake_dns_lookup),
            store,
            PDFNormalizer(),
            database.documents,
        ),
        sector_kpis=SectorKPIServices(
            database.extractions, database.calculations, ScreenerSemanticResolver(), store
        ),
        valuations=ValuationServices(
            database.extractions,
            database.market,
            database.calculations,
            ScreenerSemanticResolver(),
            store,
            database.valuations,
        ),
        analyst=AnalystServices(
            database.extractions,
            database.market,
            database.calculations,
            database.patterns,
            database.market_calculations,
            database.valuations,
            database.documents,
            database.context,
            store,
            model,
            database.analyses,
            provider_kind="FAKE",
        ),
        challenge=ChallengeServices(
            database.extractions,
            database.market,
            database.calculations,
            database.patterns,
            database.market_calculations,
            database.valuations,
            database.documents,
            database.context,
            store,
            database.analyses,
            database.challenges,
        ),
        report=ReportServices(
            database.extractions,
            database.market,
            database.calculations,
            database.patterns,
            database.market_calculations,
            database.valuations,
            database.documents,
            database.context,
            store,
            database.challenges,
            database.reports,
        ),
    )
    return database, engine, store, model


async def test_completed_report_survives_restart_but_old_job_does_not(
    tmp_path, make_document_pdf
):
    """The central Part 6B-A persistence-boundary fix, proven end to end: process A
    completes a research job; its BrowserJobManager/engine/database are then discarded
    entirely (simulating a backend restart); a brand-new process B, built only from the
    same persistent database file, can still retrieve the report and all three
    artifacts - while the old job_id is genuinely, honestly lost."""
    settings = Settings(
        database_path=tmp_path / "restart.sqlite3", artifact_path=tmp_path / "restart-raw"
    )
    await initialize_database(settings)
    document_pdf_bytes = make_document_pdf(texts=(_DOCUMENT_TEXT,))

    # --- Process A ---
    database_a, engine_a, store_a, _model_a = _build_full_engine(
        settings, _screener_handler, document_pdf_bytes
    )
    await database_a.check_ready()
    job_manager_a = BrowserJobManager(
        engine_a, store_a, ScreenerBroadIndustrySource(), _execution_factory
    )
    pairing_store = PairingStore(tmp_path / "auth.json")
    token = pairing_store.complete_pairing(EXTENSION_ORIGIN, pairing_store.issue_pairing_code())
    app_a = http_interface.create_app(
        engine=engine_a, job_manager=job_manager_a, pairing_store=pairing_store
    )
    async with app_a.router.lifespan_context(app_a):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_a), base_url="http://test"
        ) as client_a:
            launch = await client_a.post(
                "/v1/research-jobs",
                json={"portal": "SCREENER", "source_url": YESBANK_URL},
                headers=_auth(token),
            )
            old_job_id = launch.json()["job_id"]
            final = await _wait_completed(client_a, _auth(token), old_job_id)
            assert final["status"] == "COMPLETED"
            report_id = final["report_snapshot_id"]
    await database_a.close()
    # `job_manager_a`/`engine_a` are now discarded entirely - nothing below holds a
    # reference to them (no closure reuse of the original job manager).
    del job_manager_a, engine_a, store_a, app_a

    # --- Process B ("after restart") ---
    def _must_not_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "No new Screener acquisition may occur while retrieving an already-"
            "persisted report/artifact - this would mean research was rerun."
        )

    database_b, engine_b, store_b, _model_b = _build_full_engine(
        settings, _must_not_be_called, document_pdf_bytes
    )
    await database_b.check_ready()
    job_manager_b = BrowserJobManager(
        engine_b, store_b, ScreenerBroadIndustrySource(), _execution_factory
    )
    app_b = http_interface.create_app(
        engine=engine_b, job_manager=job_manager_b, pairing_store=pairing_store
    )
    async with app_b.router.lifespan_context(app_b):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_b), base_url="http://test"
        ) as client_b:
            # The old job is genuinely lost - a fresh job manager never heard of it.
            stale = await client_b.get(f"/v1/research-jobs/{old_job_id}", headers=_auth(token))
            assert stale.status_code == 404
            assert stale.json()["detail"]["code"] == "JOB_LOST_AFTER_SERVER_RESTART"

            # The persisted report is fully retrievable with no dependency on it.
            report = await client_b.get(f"/v1/reports/{report_id}", headers=_auth(token))
            assert report.status_code == 200
            assert report.json()["id"] == report_id

            for kind in ("INSTITUTIONAL_REPORT", "INVESTOR_BRIEF", "RESEARCH_JSON"):
                artifact = await client_b.get(
                    f"/v1/reports/{report_id}/artifacts/{kind}", headers=_auth(token)
                )
                assert artifact.status_code == 200
                assert len(artifact.text) > 0
    await database_b.close()


async def test_persisted_report_retrieval_uses_integrity_checked_reconstruction(
    tmp_path, make_document_pdf
):
    """The browser route must use the SAME canonical, integrity-checked repository
    lookup as everything else - not a bypassing "fast browser deserialize". Proven by
    tampering with the persisted row's payload directly in the database and confirming
    the corrupted report fails Pydantic reconstruction rather than being silently served."""
    settings = Settings(
        database_path=tmp_path / "integrity.sqlite3", artifact_path=tmp_path / "integrity-raw"
    )
    await initialize_database(settings)
    document_pdf_bytes = make_document_pdf(texts=(_DOCUMENT_TEXT,))
    database, engine, store, _model = _build_full_engine(
        settings, _screener_handler, document_pdf_bytes
    )
    await database.check_ready()
    job_manager = BrowserJobManager(
        engine, store, ScreenerBroadIndustrySource(), _execution_factory
    )
    pairing_store = PairingStore(tmp_path / "auth.json")
    token = pairing_store.complete_pairing(EXTENSION_ORIGIN, pairing_store.issue_pairing_code())
    app = http_interface.create_app(
        engine=engine, job_manager=job_manager, pairing_store=pairing_store
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            launch = await client.post(
                "/v1/research-jobs",
                json={"portal": "SCREENER", "source_url": YESBANK_URL},
                headers=_auth(token),
            )
            final = await _wait_completed(client, _auth(token), launch.json()["job_id"])
            report_id = final["report_snapshot_id"]

            report_services = engine._report  # noqa: SLF001
            assert report_services is not None
            fresh = await report_services.repository.get_report(report_id)
            assert fresh is not None
            corrupted = fresh.model_dump(mode="json")
            corrupted["schema_version"] = "not-a-real-schema-version"
            import json as json_module

            async with report_services.repository.sessions() as session:
                from sqlalchemy import text as sql_text

                await session.execute(
                    sql_text("UPDATE report_snapshots SET payload = :payload WHERE id = :id"),
                    {"payload": json_module.dumps(corrupted), "id": str(report_id)},
                )
                await session.commit()

            # The ASGI test transport re-raises unhandled server exceptions rather than
            # returning them as a response - either way, the corrupted report is never
            # silently served as valid: it fails the same canonical Pydantic
            # reconstruction every other reader of this repository already relies on.
            from pydantic import ValidationError

            with pytest.raises(ValidationError, match="schema_version"):
                await client.get(f"/v1/reports/{report_id}", headers=_auth(token))
    await database.close()
