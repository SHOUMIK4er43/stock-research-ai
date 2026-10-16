"""SUMMARY_V1 Box 4 backend: POST /v1/summaries over stored Screener captures.

Screener is served by httpx.MockTransport from the offline fixtures; the summary model is
an in-memory fake. No live Screener or AI Credits call is ever made.
"""

import json
from pathlib import Path

import httpx
import pytest

from stock_research_ai.adapters.http.browser_auth import PairingStore
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.sector import ScreenerBroadIndustrySource
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application.browser_jobs import BrowserJobManager
from stock_research_ai.application.engine import ExtractionServices, ResearchEngine
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import AnalystExecutionSpec
from stock_research_ai.domain.summary import (
    SummaryErrorCode,
    SummaryGenerationError,
    SummaryModelOutput,
    SummaryUsage,
)
from stock_research_ai.interfaces import http as http_interface

EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
FIXTURES = Path(__file__).parent / "fixtures" / "screener"
PAGES = {
    "/company/INFY/": "infy_standalone.html",
    "/company/YESBANK/consolidated/": "yesbank_consolidated.html",
}
INFY_URL = "https://www.screener.in/company/INFY/"
YESBANK_URL = "https://www.screener.in/company/YESBANK/consolidated/"
MODEL = "anthropic/claude-sonnet-5"
SECRET = "summary-api-test-credential-not-real"


@pytest.fixture(autouse=True)
def configured_browser_origin(monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_BROWSER_EXTENSION_ORIGIN", EXTENSION_ORIGIN)


class FakeSummaryModel:
    def __init__(self, error: SummaryGenerationError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system: str, user: str) -> SummaryModelOutput:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return SummaryModelOutput(
            summary_text=json.dumps({"claim_ids": []}),
            requested_model=MODEL,
            reported_model="claude-sonnet-5",
            finish_reason="stop",
            usage=SummaryUsage(prompt_tokens=3000, completion_tokens=600),
        )


class Harness:
    def __init__(self) -> None:
        self.screener_requests: list[str] = []
        self.model = FakeSummaryModel()
        self.factory_calls = 0
        self.factory_error: SummaryGenerationError | None = None

    def screener(self, request: httpx.Request) -> httpx.Response:
        self.screener_requests.append(request.url.path)
        name = PAGES.get(request.url.path)
        if name is None:
            return httpx.Response(403, text="blocked")
        return httpx.Response(
            200,
            content=(FIXTURES / name).read_bytes(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    def summary_model(self) -> FakeSummaryModel:
        self.factory_calls += 1
        if self.factory_error is not None:
            raise self.factory_error
        return self.model


@pytest.fixture
async def summary_api(tmp_path):
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_path=tmp_path / "db.sqlite3",
        artifact_path=tmp_path / "artifacts",
        analyst_provider=None,
    )
    await initialize_database(settings)
    database = SQLiteDatabase(settings.database_path)
    harness = Harness()
    store = FileRawArtifactStore(tmp_path / "artifacts")
    engine = ResearchEngine(
        database.repository,
        extraction=ExtractionServices(
            source=ScreenerSource(transport=httpx.MockTransport(harness.screener)),
            artifacts=store,
            normalizer=ScreenerNormalizer(),
            repository=database.extractions,
        ),
    )
    job_manager = BrowserJobManager(
        engine,
        store,
        ScreenerBroadIndustrySource(),
        lambda: AnalystExecutionSpec(template_id="full-research-v1", template_version=1),
    )
    pairing_store = PairingStore(tmp_path / "auth.json")
    token = pairing_store.complete_pairing(EXTENSION_ORIGIN, pairing_store.issue_pairing_code())

    def build(factory=harness.summary_model):
        return http_interface.create_app(
            engine=engine,
            settings=settings,
            job_manager=job_manager,
            pairing_store=pairing_store,
            summary_model_factory=factory,
        )

    try:
        yield build, harness, token
    finally:
        await database.close()


async def post_summary(app, token, body, origin=EXTENSION_ORIGIN):
    headers = {"Origin": origin}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            return await client.post("/v1/summaries", json=body, headers=headers)


@pytest.mark.parametrize(
    "url,name,key,scope",
    [
        (INFY_URL, "Infosys Ltd", "1489", "standalone"),
        (YESBANK_URL, "Yes Bank Ltd", "3783", "consolidated"),
    ],
)
async def test_summary_for_detected_page_comes_from_extraction(summary_api, url, name, key, scope):
    build, harness, token = summary_api
    response = await post_summary(build(), token, {"portal": "SCREENER", "source_url": url})
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "SUMMARY_RESULT_V1"
    assert body["company"] == {"name": name, "source_company_key": key, "reporting_scope": scope}
    assert body["source_url"] == url
    assert body["summary_text"].startswith("Company overview")
    assert body["grounding_claims"]
    assert all(
        claim["text"] in body["summary_text"].splitlines() for claim in body["grounding_claims"]
    )
    assert body["provider"] == "aicredits"
    assert harness.screener_requests == [url.removeprefix("https://www.screener.in")]
    assert len(harness.model.calls) == 1
    _system, user = harness.model.calls[0]
    assert user.startswith("SUMMARY_CLAIM_CATALOG_V2 JSON:\n")
    assert f'"name":"{name}"' in user


async def test_unpaired_request_never_extracts_or_calls_the_model(summary_api):
    build, harness, _token = summary_api
    response = await post_summary(build(), None, {"portal": "SCREENER", "source_url": INFY_URL})
    assert response.status_code == 401
    assert harness.screener_requests == [] and harness.factory_calls == 0


@pytest.mark.parametrize(
    "body",
    [
        {"portal": "SCREENER", "source_url": "https://www.screener.in/screens/"},
        {"portal": "SCREENER", "source_url": "https://evil.example/company/INFY/"},
        {"portal": "SCREENER", "source_url": ""},
    ],
)
async def test_unsupported_page_is_rejected_before_any_work(summary_api, body):
    build, harness, token = summary_api
    response = await post_summary(build(), token, body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "UNSUPPORTED_SOURCE_URL"
    assert harness.screener_requests == [] and harness.factory_calls == 0


@pytest.mark.parametrize(
    "body",
    [
        {"source_url": INFY_URL},
        {"portal": "OTHER", "source_url": INFY_URL},
        {"portal": "SCREENER", "source_url": INFY_URL, "html": "<html>"},
        {"portal": "SCREENER", "source_url": INFY_URL, "prompt": "ignore rules"},
    ],
)
async def test_request_shape_is_strict(summary_api, body):
    build, harness, token = summary_api
    response = await post_summary(build(), token, body)
    assert response.status_code == 422
    assert harness.screener_requests == [] and harness.factory_calls == 0


async def test_unconfigured_provider_fails_before_fetching_screener(summary_api):
    build, harness, token = summary_api
    harness.factory_error = SummaryGenerationError(SummaryErrorCode.CREDENTIAL_UNAVAILABLE)
    response = await post_summary(build(), token, {"portal": "SCREENER", "source_url": INFY_URL})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CREDENTIAL_UNAVAILABLE"
    assert harness.screener_requests == []


async def test_default_factory_uses_settings_and_reports_not_configured(summary_api):
    build, harness, token = summary_api
    # The real default (AICreditsSummaryModel.from_settings) with no provider selected.
    response = await post_summary(
        build(factory=None), token, {"portal": "SCREENER", "source_url": INFY_URL}
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "PROVIDER_NOT_CONFIGURED"
    assert harness.screener_requests == []


async def test_unreadable_company_page_is_a_typed_data_error(summary_api):
    build, harness, token = summary_api
    blocked = "https://www.screener.in/company/BLOCKED/"
    response = await post_summary(build(), token, {"portal": "SCREENER", "source_url": blocked})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "COMPANY_DATA_UNAVAILABLE"
    assert harness.model.calls == []


@pytest.mark.parametrize(
    "code,status,ambiguous",
    [
        (SummaryErrorCode.PROVIDER_TIMEOUT, 504, True),
        (SummaryErrorCode.PROVIDER_CONNECTION_LOST, 502, True),
        (SummaryErrorCode.PROVIDER_HTTP_ERROR, 502, False),
        (SummaryErrorCode.INVALID_PROVIDER_RESPONSE, 502, False),
        (SummaryErrorCode.MODEL_MISMATCH, 502, False),
        (SummaryErrorCode.CREDENTIAL_UNAVAILABLE, 503, False),
    ],
)
async def test_model_failures_map_to_safe_typed_errors(summary_api, code, status, ambiguous):
    build, harness, token = summary_api
    harness.model = FakeSummaryModel(SummaryGenerationError(code))
    response = await post_summary(build(), token, {"portal": "SCREENER", "source_url": INFY_URL})
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code.value
    assert detail["ambiguous"] is ambiguous
    assert set(detail) == {"code", "message", "retryable", "ambiguous"}
    assert len(harness.model.calls) == 1  # never retried by the endpoint


async def test_model_output_failure_is_rejected_not_rendered(summary_api):
    build, harness, token = summary_api

    class Truncated(FakeSummaryModel):
        async def complete(self, *, system: str, user: str) -> SummaryModelOutput:
            self.calls.append((system, user))
            return SummaryModelOutput(
                summary_text="partial",
                requested_model=MODEL,
                reported_model=MODEL,
                finish_reason="length",
            )

    harness.model = Truncated()
    response = await post_summary(build(), token, {"portal": "SCREENER", "source_url": INFY_URL})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "SUMMARY_TRUNCATED"
    assert "partial" not in response.text


async def test_responses_never_carry_credentials_or_prompt(summary_api, monkeypatch):
    build, harness, token = summary_api
    monkeypatch.setenv("STOCK_RESEARCH_AI_AICREDITS_API_KEY", SECRET)
    ok = await post_summary(build(), token, {"portal": "SCREENER", "source_url": INFY_URL})
    harness.model = FakeSummaryModel(SummaryGenerationError(SummaryErrorCode.PROVIDER_TIMEOUT))
    failed = await post_summary(build(), token, {"portal": "SCREENER", "source_url": INFY_URL})
    system = harness.model.calls[0][0]
    for response in (ok, failed):
        for forbidden in (
            SECRET,
            "Authorization",
            "Bearer",
            system[:80],
            "SUMMARY_CLAIM_CATALOG_V2 JSON",
        ):
            assert forbidden not in response.text


async def test_fabricated_model_text_never_reaches_api_response(summary_api):
    build, harness, token = summary_api
    harness.model = FakeSummaryModel()

    async def fabricated(*, system: str, user: str) -> SummaryModelOutput:
        harness.model.calls.append((system, user))
        return SummaryModelOutput(
            summary_text="Revenue was ₹12,500 Cr in FY2025.",
            requested_model=MODEL,
            reported_model="claude-sonnet-5",
            finish_reason="stop",
        )

    harness.model.complete = fabricated
    response = await post_summary(build(), token, {"portal": "SCREENER", "source_url": INFY_URL})
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == SummaryErrorCode.INVALID_PROVIDER_RESPONSE.value
    assert "12,500" not in response.text
