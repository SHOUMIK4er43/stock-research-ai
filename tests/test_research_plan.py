"""Offline, fully-mocked end-to-end proof of FULL_RESEARCH_V1: a real supported Screener
URL -> live-adapter-shaped Part 5B analysis -> Part 5C challenge -> Part 6A report,
without ANY test fake outside this file's own boundary substitutes and without any real
network call. This is the automated half of the Part 6B0 document-intelligence
acceptance chain; scripts/live_smoke_full_research.py documents the manual, opt-in,
real-network half.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from stock_research_ai.adapters.documents.pdf import PDFNormalizer
from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.adapters.screener.normalizer import ScreenerNormalizer
from stock_research_ai.adapters.screener.sector import ScreenerBroadIndustrySource
from stock_research_ai.adapters.screener.semantic_mappings import ScreenerSemanticResolver
from stock_research_ai.adapters.screener.source import ScreenerSource
from stock_research_ai.adapters.sqlite import SQLiteDatabase
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
from stock_research_ai.application.hashing import digest
from stock_research_ai.application.research_plan import FullResearchRequest, run_full_research
from stock_research_ai.bootstrap import initialize_database
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import (
    AnalystExecutionSpec,
    ModelMetadata,
    PromptTemplate,
    RawAnalystResponse,
    RenderedModelRequest,
)

FIX = Path(__file__).parent / "fixtures"
YESBANK_URL = "https://www.screener.in/company/YESBANK/consolidated/"

# One generic body used for every mocked document: it contains a keyword anchor for
# every one of the three DOCUMENT_RESEARCH_TOPICS_V1 topics, so whichever document types
# FULL_RESEARCH_V1 actually selects for ingestion, at least one topic will match.
_DOCUMENT_TEXT = (
    "Our business model focuses on retail lending. "
    "Key risk factors include credit risk in this segment. "
    "Management strategy update for this quarter follows."
)


async def _fake_dns_lookup(host: str) -> list[str]:
    return ["93.184.216.34"]


class _DynamicFakeAnalystModel:
    """Not tests/fakes/analyst.py::FakeAnalystModel - this one inspects the REAL bundle
    Part 5B assembled at call time (evidence IDs are only known once extraction/valuation/
    document-selection have actually run) and cites real evidence from it, exactly the
    shape a real live adapter would produce. provider_kind stays "FAKE": never "LIVE"."""

    def __init__(self) -> None:
        self.last_bundle = None

    async def generate(
        self, bundle, execution: AnalystExecutionSpec, *, model_input
    ) -> ModelMetadata:
        self.last_bundle = bundle
        financial_ref = next(s for s in bundle.sources if s.kind == "FINANCIAL")
        claims = [
            {
                "section": "HISTORICAL_FUNDAMENTALS",
                "statement": "Evidence-cited interpretation for the offline Part 6B0 proof.",
                "materiality": "PRIMARY",
                "citations": [
                    {
                        "kind": financial_ref.kind,
                        "owning_snapshot_id": str(financial_ref.owning_snapshot_id),
                        "item_key": financial_ref.item_key,
                    }
                ],
            }
        ]
        chunk_ref = next((s for s in bundle.sources if s.kind == "DOCUMENT_CHUNK"), None)
        if chunk_ref is not None:
            claims.append(
                {
                    "section": "BUSINESS_MODEL",
                    "statement": "A document-cited interpretation for the offline proof.",
                    "materiality": "SUPPORTING",
                    "citations": [
                        {
                            "kind": chunk_ref.kind,
                            "owning_snapshot_id": str(chunk_ref.owning_snapshot_id),
                            "item_key": chunk_ref.item_key,
                        }
                    ],
                }
            )
        structured_payload = {"claims": claims}
        template_content = "test-live-shaped-template"
        template = PromptTemplate(
            template_id=execution.template_id,
            template_version=execution.template_version,
            content=template_content,
            content_sha256=hashlib.sha256(template_content.encode()).hexdigest(),
        )
        rendered_text = f"{template_content}\nbundle:{bundle.bundle_id}"
        request = RenderedModelRequest(
            bundle_sha256=digest(bundle),
            template=template,
            generation_parameters=dict(execution.generation_parameters),
            generation_parameters_sha256=hashlib.sha256(b"{}").hexdigest(),
            rendered_text=rendered_text,
            rendered_sha256=hashlib.sha256(rendered_text.encode()).hexdigest(),
        )
        raw_response = RawAnalystResponse(
            structured_payload=structured_payload,
            structured_payload_sha256=hashlib.sha256(
                json.dumps(structured_payload, sort_keys=True).encode()
            ).hexdigest(),
        )
        return ModelMetadata(
            provider="test",
            model_id="test-live-shaped",
            model_version="1",
            request=request,
            raw_response=raw_response,
            generated_at=datetime.now(UTC),
        )


def _screener_handler(request: httpx.Request) -> httpx.Response:
    assert str(request.url) == YESBANK_URL
    content = (FIX / "screener/yesbank_consolidated.html").read_bytes()
    return httpx.Response(200, content=content, headers={"content-type": "text/html"})


@pytest.fixture
async def full_research_engine(tmp_path, make_document_pdf):
    settings = Settings(database_path=tmp_path / "research.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    database = SQLiteDatabase(settings.database_path)
    await database.check_ready()
    store = FileRawArtifactStore(settings.artifact_path)
    model = _DynamicFakeAnalystModel()
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
    try:
        yield engine, store, model
    finally:
        await database.close()


async def test_full_research_v1_reaches_a_real_report_without_test_fakes_outside_analyst(
    full_research_engine,
):
    engine, store, model = full_research_engine
    execution = AnalystExecutionSpec(template_id="full-research-v1", template_version=1)

    result = await run_full_research(
        engine,
        store,
        ScreenerBroadIndustrySource(),
        FullResearchRequest(source_url=YESBANK_URL),
        execution,
    )

    # Sector resolution: deterministic mapping from the real fixture's Broad Industry label.
    assert result.sector_classification.classification == "BANK"
    assert result.sector_classification.basis == "VERIFIED_MAPPING"

    # Document intelligence acceptance chain, steps 1-5: discovered, ingested, chunked,
    # deterministically selected, and pinned into the real AnalysisRequest.
    assert len(result.document_versions_ingested) > 0
    assert result.document_chunks_selected > 0
    assert model.last_bundle is not None
    assert any(s.kind == "DOCUMENT_CHUNK" for s in model.last_bundle.sources)

    # Steps 6-8: Part 5B accepted (or, at minimum, deterministically processed rather
    # than silently dropped) the document-cited claim; Part 5C and Part 6A both ran.
    assert result.analysis_snapshot_id is not None
    assert result.challenge_snapshot_id is not None
    assert result.report_snapshot_id is not None

    history = await engine._report.repository.report_history(  # noqa: SLF001
        result.challenge_snapshot_id
    )
    report = next(r for r in history if r.id == result.report_snapshot_id)
    assert report.document_corpus.corpus_scope == "PINNED_REPORT_INPUT_ONLY"


async def test_progress_stages_are_observed_in_order(full_research_engine):
    engine, store, model = full_research_engine
    execution = AnalystExecutionSpec(template_id="full-research-v1", template_version=1)
    observed = []

    class Sink:
        def on_stage_change(self, stage):
            observed.append(stage)

    await run_full_research(
        engine,
        store,
        ScreenerBroadIndustrySource(),
        FullResearchRequest(source_url=YESBANK_URL),
        execution,
        progress=Sink(),
    )

    assert observed == ["PREPARING", "EVIDENCE", "ANALYSIS", "CHALLENGE", "REPORT", "COMPLETED"]


async def test_no_invented_valuation_assumptions_only_observed_methods_run(full_research_engine):
    engine, store, model = full_research_engine
    execution = AnalystExecutionSpec(template_id="full-research-v1", template_version=1)

    await run_full_research(
        engine,
        store,
        ScreenerBroadIndustrySource(),
        FullResearchRequest(source_url=YESBANK_URL),
        execution,
    )

    valuation_history = await engine._valuations.repository.valuation_history(  # noqa: SLF001
        model.last_bundle.request.company_id
    )
    assert valuation_history
    methods = {r.definition.method for snapshot in valuation_history for r in snapshot.results}
    assert methods <= {"PRICE_TO_EARNINGS", "PRICE_TO_BOOK"}
    assert "FCFF_DCF" not in methods


async def test_document_recency_unverified_warning_recorded_for_undated_fixture_documents(
    full_research_engine,
):
    """Every discovered document in this real fixture lacks a <time> tag (confirmed by
    direct inspection) - so any type that gets ingested at all must carry the honest
    DOCUMENT_RECENCY_UNVERIFIED warning rather than silently calling discovery order
    "most recent"."""
    engine, store, model = full_research_engine
    execution = AnalystExecutionSpec(template_id="full-research-v1", template_version=1)

    result = await run_full_research(
        engine,
        store,
        ScreenerBroadIndustrySource(),
        FullResearchRequest(source_url=YESBANK_URL),
        execution,
    )

    assert any(w.code == "DOCUMENT_RECENCY_UNVERIFIED" for w in result.stage_warnings)


async def test_calculations_not_attempted_v1_scope_is_disclosed_not_hidden(full_research_engine):
    engine, store, model = full_research_engine
    execution = AnalystExecutionSpec(template_id="full-research-v1", template_version=1)

    result = await run_full_research(
        engine,
        store,
        ScreenerBroadIndustrySource(),
        FullResearchRequest(source_url=YESBANK_URL),
        execution,
    )

    assert any(w.code == "CALCULATIONS_NOT_ATTEMPTED_V1_SCOPE" for w in result.stage_warnings)


# --- Required-stage failure regression (freezes the "no surrounding try/except" -----
# --- invariant explicitly rather than relying on structural reasoning alone). -------


class _FailingSourceAcquirer:
    """Forces the core Screener extraction stage to raise, deterministically."""

    async def acquire(self, request):
        raise RuntimeError("forced test failure: extraction stage")


class _FailingAnalystModel:
    """Forces the Part 5B analysis stage to raise, deterministically. provider_kind
    stays "FAKE" - never "LIVE" - exactly like every other test double in this file."""

    async def generate(self, bundle, execution, *, model_input):
        raise RuntimeError("forced test failure: analysis stage")


class _RaisingChallengeRepository:
    """Wraps the real ChallengeRepository, forcing only save_challenge (i.e. the tail
    end of the Part 5C required stage) to raise, deterministically."""

    def __init__(self, real):
        self._real = real

    async def save_challenge(self, snapshot):
        raise RuntimeError("forced test failure: challenge stage")

    async def challenge_history(self, analysis_snapshot_id):
        return await self._real.challenge_history(analysis_snapshot_id)

    async def get_challenge(self, challenge_snapshot_id):
        return await self._real.get_challenge(challenge_snapshot_id)


class _RaisingReportRepository:
    """Wraps the real ReportRepository, forcing only save_report (i.e. the tail end of
    the Part 6A required stage) to raise, deterministically."""

    def __init__(self, real):
        self._real = real

    async def save_report(self, snapshot):
        raise RuntimeError("forced test failure: report stage")

    async def report_history(self, challenge_snapshot_id):
        return await self._real.report_history(challenge_snapshot_id)


@pytest.fixture
async def failure_engine_factory(tmp_path, make_document_pdf):
    """Builds a fresh engine per stage under test, reusing the exact same wiring as
    `full_research_engine`, with exactly one component swapped for a forced-failure
    double for the stage under test - never touching production code."""
    document_pdf_bytes = make_document_pdf(texts=(_DOCUMENT_TEXT,))

    def document_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=document_pdf_bytes, headers={"content-type": "application/pdf"}
        )

    opened_databases = []

    async def build(failing_stage: str):
        settings = Settings(
            database_path=tmp_path / f"{failing_stage}.sqlite3",
            artifact_path=tmp_path / f"{failing_stage}-raw",
        )
        await initialize_database(settings)
        database = SQLiteDatabase(settings.database_path)
        await database.check_ready()
        opened_databases.append(database)
        store = FileRawArtifactStore(settings.artifact_path)

        screener_source = (
            _FailingSourceAcquirer()
            if failing_stage == "EXTRACTION"
            else ScreenerSource(httpx.MockTransport(_screener_handler))
        )
        model = (
            _FailingAnalystModel() if failing_stage == "ANALYSIS" else _DynamicFakeAnalystModel()
        )
        challenges_repository = (
            _RaisingChallengeRepository(database.challenges)
            if failing_stage == "CHALLENGE"
            else database.challenges
        )
        reports_repository = (
            _RaisingReportRepository(database.reports)
            if failing_stage == "REPORT"
            else database.reports
        )

        engine = ResearchEngine(
            database.repository,
            extraction=ExtractionServices(
                source=screener_source,
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
                challenges_repository,
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
                reports_repository,
            ),
        )
        return engine, store

    try:
        yield build
    finally:
        for database in opened_databases:
            await database.close()


@pytest.mark.parametrize(
    "failing_stage,expected_observed_stages",
    [
        ("EXTRACTION", ["PREPARING"]),
        ("ANALYSIS", ["PREPARING", "EVIDENCE", "ANALYSIS"]),
        ("CHALLENGE", ["PREPARING", "EVIDENCE", "ANALYSIS", "CHALLENGE"]),
        ("REPORT", ["PREPARING", "EVIDENCE", "ANALYSIS", "CHALLENGE", "REPORT"]),
    ],
)
async def test_required_stage_failure_propagates_and_never_completes(
    failure_engine_factory, failing_stage, expected_observed_stages
):
    engine, store = await failure_engine_factory(failing_stage)
    execution = AnalystExecutionSpec(template_id="full-research-v1", template_version=1)
    observed = []

    class Sink:
        def on_stage_change(self, stage):
            observed.append(stage)

    result = None
    with pytest.raises(RuntimeError, match=f"forced test failure: {failing_stage.lower()} stage"):
        result = await run_full_research(
            engine,
            store,
            ScreenerBroadIndustrySource(),
            FullResearchRequest(source_url=YESBANK_URL),
            execution,
            progress=Sink(),
        )

    # No FullResearchResult is ever returned/assigned on a required-stage failure.
    assert result is None
    # COMPLETED - and every stage after the one that failed - is never emitted.
    assert observed == expected_observed_stages
    assert "COMPLETED" not in observed
    for later_stage in ["ANALYSIS", "CHALLENGE", "REPORT", "COMPLETED"]:
        if later_stage not in expected_observed_stages:
            assert later_stage not in observed
