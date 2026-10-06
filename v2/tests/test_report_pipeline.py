"""Engine-level persistence, integrity, and CLI proof for Part 6A.

Offline only: FakeAnalystModel/FakeChallengerModel/FakeReportNarrator never touch a
network or a live provider. Built on real INFY Screener evidence, exactly like
test_challenge_pipeline.py.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fakes.analyst import FakeAnalystModel
from fakes.narrator import FakeReportNarrator

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application import analysis as analyst_engine
from stock_research_ai.application.engine import ReportServices, ResearchEngine
from stock_research_ai.application.report import ReportIntegrityError
from stock_research_ai.bootstrap import (
    analyze_company,
    calculate_financials,
    challenge_analysis,
    evaluate_valuation,
    extract_snapshot,
    generate_report,
    initialize_database,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import AnalysisRequest, AnalystExecutionSpec
from stock_research_ai.domain.calculations import CalculationRequest
from stock_research_ai.domain.challenge import ChallengeRequest
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.report import ReportExecutionSpec, ReportRequest
from stock_research_ai.domain.valuation import ValuationRequest

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.screener.in/company/INFY/"


def calc_request(source):
    return CalculationRequest.model_validate(
        dict(
            company_id=source.company.company_id, extraction_id=source.metadata.id,
            reporting_scope=source.company.reporting_scope,
            sector=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit IT proof classification",
            ),
            alignment=dict(
                fiscal_year_end_month=3, declaration="Caller declares March fiscal year end."
            ),
            calculations=[
                dict(
                    metric_id="infy_sales_growth", formula_id="SOURCE_METRIC_GROWTH",
                    source_metric="sales", current_period="2025-03", comparison="ANNUAL_YOY",
                )
            ],
        )
    )


def pe_request(source):
    rows = [o for o in source.top_metrics if o.metric_id == "current_price"]
    eps_rows = [
        o for o in source.profit_loss
        if o.metric_id == "eps" and o.period.fiscal_label == "Mar 2026"
    ]
    return ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            profile_id="IT_SERVICES_PROFILE",
            classification=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit proof profile",
            ),
            selections=[
                dict(
                    method="PRICE_TO_EARNINGS",
                    price=dict(
                        kind="FINANCIAL", snapshot_id=str(source.metadata.id),
                        observation_id=str(rows[-1].id),
                    ),
                    denominator=dict(
                        kind="FINANCIAL", snapshot_id=str(source.metadata.id),
                        observation_id=str(eps_rows[-1].id),
                    ),
                )
            ],
        )
    )


@pytest.fixture
async def world(tmp_path):
    settings = Settings(database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener", url=URL, replay_path=FIX / "screener/infy_standalone.html"
        ),
        tmp_path / "extraction",
    )
    calc_snapshot = await calculate_financials(settings, calc_request(source), tmp_path / "calc")
    valuation_snapshot = await evaluate_valuation(settings, pe_request(source), tmp_path / "val")
    request = AnalysisRequest.model_validate(
        dict(
            company_id=source.company.company_id,
            classification=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit proof profile",
            ),
            profile_id="IT_SERVICES_PROFILE",
            financial_extraction_id=source.metadata.id,
            calculation_snapshot_ids=(calc_snapshot.id,),
            valuation_snapshot_ids=(valuation_snapshot.id,),
            sections=("HISTORICAL_FUNDAMENTALS", "VALUATION"),
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-base", template_version=1)
    yield settings, source, calc_snapshot, valuation_snapshot, request, execution


def _citation(ref):
    return dict(
        kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id), item_key=ref.item_key
    )


async def _bundle_for(settings, request):
    database = SQLiteDatabase(settings.database_path)
    try:
        company = await database.repository.get(request.company_id)
        evidence = await analyst_engine.resolve_evidence(
            request, company,
            extractions=database.extractions, market=database.market,
            calculations=database.calculations, patterns=database.patterns,
            market_calculations=database.market_calculations, valuations=database.valuations,
            documents=database.documents, context=database.context,
        )
        return analyst_engine.assemble_bundle(request, evidence)
    finally:
        await database.close()


def valid_claim(bundle, section="VALUATION"):
    ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    return ref, dict(
        section=section, statement="Observed valuation evidence is cited.",
        materiality="PRIMARY", citations=[_citation(ref)],
    )


def _row_count(database_path, table):
    import sqlite3

    with sqlite3.connect(database_path) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


async def _full_chain(settings, request, execution, claim, tmp_path):
    analysis = await analyze_company(
        settings, request, execution, tmp_path / "analysis-out",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )
    challenge = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "challenge-out"
    )
    return analysis, challenge


async def test_report_requires_challenge_snapshot(world, tmp_path):
    settings, *_ = world
    with pytest.raises(ValueError, match="Unknown challenge snapshot"):
        await generate_report(
            settings, ReportRequest(challenge_snapshot_id=uuid4()), tmp_path / "out"
        )


async def test_deterministic_report_generation_and_exact_pins(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)

    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    assert result.analysis_snapshot_id == analysis.id
    assert result.challenge_snapshot_id == challenge.id
    from stock_research_ai.application.hashing import digest

    assert result.analysis_snapshot_sha256 == digest(analysis)
    assert result.challenge_snapshot_sha256 == digest(challenge)
    assert result.reproducibility.analysis_snapshot_id == analysis.id
    assert result.reproducibility.challenge_snapshot_id == challenge.id

    valuation_section = next(s for s in result.sections if s.section == "VALUATION")
    assert valuation_section.coverage.status == "AVAILABLE"
    assert any(st.challenge_outcome == "SUPPORTED_BY_BUNDLE" for st in valuation_section.statements)

    for path in (
        "COMPANY_RESEARCH.json", "COMPANY_INSTITUTIONAL_RESEARCH.md",
        "COMPANY_INVESTOR_BRIEF.md", "investor_brief.json",
    ):
        assert (tmp_path / "report-out" / path).exists()

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.reports.report_history(challenge.id)
        assert history == (result,)
    finally:
        await database.close()


async def test_challenged_claim_finding_is_exposed(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    claim = dict(claim, statement="Valuation improved because market sentiment shifted.")
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    review = challenge.claim_reviews[0]
    assert review.outcome == "SUPPORTED_WITH_LIMITATIONS"

    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    valuation_section = next(s for s in result.sections if s.section == "VALUATION")
    claim_id = (analysis.claims[0].claim_id,)
    claim_statement = next(
        st for st in valuation_section.statements if st.source_claim_ids == claim_id
    )
    assert claim_statement.challenge_outcome == "SUPPORTED_WITH_LIMITATIONS"
    assert claim_statement.limitations


async def test_invalid_claim_never_appears_as_topical_conclusion(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, _ = await _full_chain(settings, request, execution, claim, tmp_path)

    import sqlite3

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM analysis_snapshots WHERE id = ?", (str(analysis.id),)
        ).fetchone()
        payload = json.loads(row[0])
        payload["claims"][0]["statement"] = "We recommend investors buy the stock."
        connection.execute(
            "UPDATE analysis_snapshots SET payload = ? WHERE id = ?",
            (json.dumps(payload), str(analysis.id)),
        )
        connection.commit()

    challenge = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "challenge-out2"
    )
    assert challenge.claim_reviews[0].outcome == "INVALID_AGAINST_BUNDLE"

    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    claim_id = (analysis.claims[0].claim_id,)
    valuation_section = next(s for s in result.sections if s.section == "VALUATION")
    assert not any(st.source_claim_ids == claim_id for st in valuation_section.statements)
    challenger_section = next(
        s for s in result.sections if s.section == "INDEPENDENT_CHALLENGER_REVIEW"
    )
    assert any(st.source_claim_ids == claim_id for st in challenger_section.statements)
    assert analysis.claims[0].claim_id not in result.synthesis.supporting_claim_ids
    assert analysis.claims[0].claim_id in result.synthesis.invalid_claim_ids


async def _build_report_snapshot_manually(settings, challenge):
    """Mirrors ResearchEngine.generate_report's construction exactly, without ever
    calling save_report - used to create a genuine build-then-tamper-then-persist
    TOCTOU scenario, exactly like test_challenge_pipeline.py's own regression test."""
    from uuid import uuid4 as _uuid4

    from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
    from stock_research_ai.application.hashing import digest
    from stock_research_ai.application.report import assemble_report, resolve_report_evidence
    from stock_research_ai.domain.report import ArtifactManifest, ReproducibilityManifest

    store = FileRawArtifactStore(settings.artifact_path)
    database = SQLiteDatabase(settings.database_path)
    try:
        reviewed = challenge.challenger_bundle.reviewed_analysis
        company = await database.repository.get(reviewed.company_id)
        evidence = await resolve_report_evidence(
            reviewed, company,
            extractions=database.extractions, market=database.market,
            calculations=database.calculations, patterns=database.patterns,
            market_calculations=database.market_calculations, valuations=database.valuations,
            documents=database.documents, context=database.context, artifacts=store,
        )
        assembly = assemble_report(challenge, evidence)
        from datetime import UTC, datetime

        from stock_research_ai.domain.report import ResearchReportSnapshot

        analysis_sha256 = digest(reviewed)
        challenge_sha256 = digest(challenge)
        return ResearchReportSnapshot(
            id=_uuid4(), company_id=reviewed.company_id, analysis_snapshot_id=reviewed.id,
            analysis_snapshot_sha256=analysis_sha256, challenge_snapshot_id=challenge.id,
            challenge_snapshot_sha256=challenge_sha256, sections=assembly.sections,
            executive_snapshot=assembly.executive_snapshot, synthesis=assembly.synthesis,
            document_corpus=assembly.document_corpus, monitoring=assembly.monitoring,
            limitations=assembly.limitations, artifact_manifest=ArtifactManifest(),
            reproducibility=ReproducibilityManifest(
                analysis_snapshot_id=reviewed.id, analysis_snapshot_sha256=analysis_sha256,
                challenge_snapshot_id=challenge.id, challenge_snapshot_sha256=challenge_sha256,
                relevance_registry_version=challenge.relevance_registry_version,
            ),
            evaluated_at=datetime.now(UTC),
        )
    finally:
        await database.close()


async def test_challenge_snapshot_corruption_between_build_and_append_refuses_atomically(
    world, tmp_path
):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    report_snapshot = await _build_report_snapshot_manually(settings, challenge)

    import sqlite3

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM challenge_snapshots WHERE id = ?", (str(challenge.id),)
        ).fetchone()
        payload = json.loads(row[0])
        payload["relevance_registry_version"] = "tampered-after-report-was-built"
        connection.execute(
            "UPDATE challenge_snapshots SET payload = ? WHERE id = ?",
            (json.dumps(payload), str(challenge.id)),
        )
        connection.commit()

    assert _row_count(settings.database_path, "report_snapshots") == 0
    database = SQLiteDatabase(settings.database_path)
    try:
        with pytest.raises(ReportIntegrityError, match="hash mismatch"):
            await database.reports.save_report(report_snapshot)
    finally:
        await database.close()
    assert _row_count(settings.database_path, "report_snapshots") == 0


async def test_analysis_snapshot_corruption_blocks_report_append(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)

    import sqlite3

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM analysis_snapshots WHERE id = ?", (str(analysis.id),)
        ).fetchone()
        payload = json.loads(row[0])
        payload["claims"][0]["materiality"] = "CONTEXTUAL"
        connection.execute(
            "UPDATE analysis_snapshots SET payload = ? WHERE id = ?",
            (json.dumps(payload), str(analysis.id)),
        )
        connection.commit()

    with pytest.raises(ReportIntegrityError):
        await generate_report(
            settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
        )
    assert _row_count(settings.database_path, "report_snapshots") == 0


async def test_raw_artifact_corruption_blocks_report_generation(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)

    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")

    with pytest.raises(ReportIntegrityError):
        await generate_report(
            settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
        )
    assert _row_count(settings.database_path, "report_snapshots") == 0


async def test_failed_integrity_leaves_row_count_unchanged(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "good"
    )
    assert _row_count(settings.database_path, "report_snapshots") == 1

    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")
    with pytest.raises(ReportIntegrityError):
        await generate_report(
            settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "bad"
        )
    assert _row_count(settings.database_path, "report_snapshots") == 1


async def test_report_revisions_append_only_and_reconstruct(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)

    first = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "out1"
    )
    before = first.model_dump_json()
    second = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "out2"
    )
    assert first.id != second.id
    assert _row_count(settings.database_path, "report_snapshots") == 2

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.reports.report_history(challenge.id)
        assert len(history) == 2
        assert history[0].model_dump_json() == before
        assert history == (first, second)
    finally:
        await database.close()


async def test_document_corpus_scope_and_no_completeness_claim(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    assert result.document_corpus.corpus_scope == "PINNED_REPORT_INPUT_ONLY"
    # No document was pinned into this INFY request - the corpus is honestly empty.
    assert result.document_corpus.entries == ()
    doc_section = next(
        s for s in result.sections if s.section == "DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE"
    )
    assert doc_section.coverage.status == "UNAVAILABLE"


async def test_guidance_and_agm_sections_render_honest_placeholders(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    by_section = {s.section: s for s in result.sections}
    guidance = by_section["MANAGEMENT_GUIDANCE_AND_COMMITMENTS"]
    assert guidance.coverage.status == "INSUFFICIENT_INPUT"
    assert guidance.coverage.reason_code == "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED"
    guidance_vs_actual = by_section["MANAGEMENT_GUIDANCE_VS_ACTUAL"]
    assert guidance_vs_actual.coverage.reason_code == "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED"
    agm = by_section["AGM_INTELLIGENCE"]
    assert agm.coverage.status == "UNAVAILABLE"
    assert agm.coverage.reason_code == "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED"
    competitive = by_section["INDUSTRY_AND_COMPETITIVE_STRUCTURE"]
    assert competitive.coverage.reason_code == "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED"


async def test_research_completeness_and_valuation_state_are_deterministic(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    # This fixture's AnalysisRequest only ever pins HISTORICAL_FUNDAMENTALS + VALUATION -
    # five other CORE-family sections (company identity, quarterly, balance sheet, cash
    # flow, market-implied expectations) are honestly UNAVAILABLE as a result, which
    # correctly yields LIMITED under the mechanical family rule (>=2 CORE gaps), not a
    # narrower "were the requested sections themselves satisfied" check.
    assert result.synthesis.research_completeness == "LIMITED"
    assert result.synthesis.valuation_evidence_state == "OBSERVED"
    assert not hasattr(result.synthesis, "research_evidence_stance")


async def test_monitoring_is_static_snapshot_only(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    claim = dict(claim, statement="Valuation improved because market sentiment shifted.")
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    assert len(result.monitoring) >= 1
    assert all(m.direction == "UNKNOWN" for m in result.monitoring)


async def test_narrator_numeric_closure_and_fallback(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)

    narrator = FakeReportNarrator(
        blocks=[
            {"section": "VALUATION", "text": "We recommend investors buy the stock."},
            {"section": "NOT_A_REAL_SECTION", "text": "x"},
        ]
    )
    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out",
        execution=ReportExecutionSpec(template_id="n1", template_version=1),
        narrator=narrator, provider_kind="FAKE",
    )
    assert len(result.rejected_narrative_blocks) == 2
    codes = {issue.code for block in result.rejected_narrative_blocks for issue in block.issues}
    assert "FORBIDDEN_RECOMMENDATION_LANGUAGE" in codes
    # An unknown ReportSectionId fails the candidate's own Literal schema first - this is
    # the same MALFORMED_RESPONSE path a genuinely malformed block takes; the UNKNOWN_SECTION
    # code is exercised directly in test_report.py against a real-but-absent section.
    assert "MALFORMED_RESPONSE" in codes
    assert result.narrator_metadata is not None


async def test_narrator_unavailable_without_silent_fallback(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    with pytest.raises(RuntimeError, match="No LIVE report narrator"):
        await generate_report(
            settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "out",
            execution=ReportExecutionSpec(template_id="n1", template_version=1),
        )


async def test_investor_brief_and_json_derive_from_same_snapshot(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    research_json = json.loads((tmp_path / "report-out/COMPANY_RESEARCH.json").read_text())
    brief_json = json.loads((tmp_path / "report-out/investor_brief.json").read_text())
    assert research_json["lineage"]["analysis_snapshot_id"] == str(analysis.id)
    assert brief_json["report_id"] == str(result.id)
    valuation_texts = {st["text"] for st in research_json["valuation"]["statements"]}
    assert set(brief_json["valuation"]) <= valuation_texts


async def test_capabilities_report_deterministic_available_narrator_not(world, tmp_path):
    settings, *_ = world
    database = SQLiteDatabase(settings.database_path)
    try:
        bare_engine = ResearchEngine(database.repository)
        capabilities = await bare_engine.get_capabilities()
        assert capabilities.deterministic_report_assembly_available is False
        assert capabilities.live_report_narrator_available is False

        from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore

        store = FileRawArtifactStore(settings.artifact_path)
        wired_engine = ResearchEngine(
            database.repository,
            report=ReportServices(
                database.extractions, database.market, database.calculations, database.patterns,
                database.market_calculations, database.valuations, database.documents,
                database.context, store, database.challenges, database.reports,
            ),
        )
        capabilities = await wired_engine.get_capabilities()
        assert capabilities.deterministic_report_assembly_available is True
        assert capabilities.live_report_narrator_available is False

        fake_provider_engine = ResearchEngine(
            database.repository,
            report=ReportServices(
                database.extractions, database.market, database.calculations, database.patterns,
                database.market_calculations, database.valuations, database.documents,
                database.context, store, database.challenges, database.reports,
                narrator=FakeReportNarrator(), provider_kind="FAKE",
            ),
        )
        capabilities = await fake_provider_engine.get_capabilities()
        assert capabilities.live_report_narrator_available is False
    finally:
        await database.close()


async def test_cli_deterministic_round_trip(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)

    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "generate-report",
            "--challenge-snapshot", str(challenge.id),
            "--output", str(tmp_path / "cli-out"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cli-out/COMPANY_RESEARCH.json").exists()


async def test_cli_reports_malformed_request_for_unknown_snapshot(world, tmp_path):
    settings, *_ = world
    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "generate-report",
            "--challenge-snapshot", str(uuid4()),
            "--output", str(tmp_path / "cli-out"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2, result.stderr


async def test_cli_reports_integrity_failure_with_dedicated_exit_code(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)

    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")

    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "generate-report",
            "--challenge-snapshot", str(challenge.id),
            "--output", str(tmp_path / "cli-out"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 6, result.stderr
    assert not (tmp_path / "cli-out").exists()


async def test_refused_price_to_book_is_preserved_and_never_calculated_by_part6a(tmp_path):
    """Frozen Part 5A rule (application/valuation.py::validate_fact): a PRICE_TO_BOOK
    denominator whose BVPS semantic mapping is not VERIFIED is refused with
    status=UNVERIFIED_METRIC_SEMANTICS and zero output values - real YESBANK book_value
    has no VERIFIED BVPS mapping. Part 6A must render that refusal exactly, never promote
    it to an observed multiple, and never compute a P/B value itself."""
    settings = Settings(database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "raw")
    await initialize_database(settings)
    source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener", url="https://www.screener.in/company/YESBANK/",
            replay_path=FIX / "screener/yesbank_consolidated.html",
        ),
        tmp_path / "extraction",
    )

    def pin(metric, annual=False):
        obs = [
            o for rows in (source.top_metrics, source.profit_loss, source.balance_sheet)
            for o in rows
            if o.metric_id == metric and (not annual or o.period.fiscal_label == "Mar 2026")
        ]
        return dict(
            kind="FINANCIAL", snapshot_id=str(source.metadata.id), observation_id=str(obs[-1].id)
        )

    val_req = ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope, profile_id="BANK_PROFILE",
            classification=dict(
                classification="BANK", basis="CALLER_DECLARED", declaration="x"
            ),
            selections=[
                dict(method="PRICE_TO_EARNINGS", price=pin("current_price"),
                     denominator=pin("eps", True)),
                dict(method="PRICE_TO_BOOK", price=pin("current_price"),
                     denominator=pin("book_value")),
            ],
        )
    )
    valuation_snapshot = await evaluate_valuation(settings, val_req, tmp_path / "val")
    pb_result = next(
        r for r in valuation_snapshot.results if r.definition.method == "PRICE_TO_BOOK"
    )
    assert pb_result.status == "UNVERIFIED_METRIC_SEMANTICS"
    assert pb_result.values == ()

    request = AnalysisRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            classification=dict(
                classification="BANK", basis="CALLER_DECLARED", declaration="x"
            ),
            profile_id="BANK_PROFILE", financial_extraction_id=str(source.metadata.id),
            valuation_snapshot_ids=(str(valuation_snapshot.id),), sections=("VALUATION",),
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-pb-refusal", template_version=1)
    bundle = await _bundle_for(settings, request)
    pe_ref = next(
        r for r in bundle.sources if r.kind == "VALUATION"
        and str(pb_result.id) != r.item_key
    )
    claim = dict(
        section="VALUATION", statement="Observed P/E valuation evidence is cited.",
        materiality="PRIMARY", citations=[_citation(pe_ref)],
    )
    analysis, challenge = await _full_chain(settings, request, execution, claim, tmp_path)
    result = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report-out"
    )
    valuation_section = next(s for s in result.sections if s.section == "VALUATION")
    pb_rows = [
        m for t in valuation_section.tables for (m,) in t.rows if m.label == "PRICE_TO_BOOK"
    ]
    assert len(pb_rows) == 1
    assert pb_rows[0].status == "UNVERIFIED_METRIC_SEMANTICS"
    assert pb_rows[0].display_value is None
    # Part 6A performs no calculation of its own: the None display_value comes directly
    # from an empty ValuationResult.values tuple via canonical_values(), never a computed
    # ratio - there is no arithmetic operator anywhere in application/report.py's table
    # construction path (_metric_from_reference only re-displays canonical_values()).
