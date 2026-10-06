"""Engine-level persistence, integrity, and CLI proof for Part 5C.

Offline only: FakeAnalystModel/FakeChallengerModel never touch a network or a live
provider. Built on real INFY Screener evidence, exactly like test_analysis_pipeline.py.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fakes.analyst import FakeAnalystModel
from fakes.challenger import FakeChallengerModel

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application import analysis as analyst_engine
from stock_research_ai.application import challenge as challenge_engine
from stock_research_ai.application.challenge import ChallengeIntegrityError
from stock_research_ai.application.engine import ChallengeServices, ResearchEngine
from stock_research_ai.bootstrap import (
    analyze_company,
    calculate_financials,
    challenge_analysis,
    evaluate_valuation,
    extract_snapshot,
    initialize_database,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import AnalysisRequest, AnalystExecutionSpec
from stock_research_ai.domain.calculations import CalculationRequest
from stock_research_ai.domain.challenge import ChallengeRequest, ChallengerExecutionSpec
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.valuation import ValuationRequest

FIX = Path(__file__).parent / "fixtures"
URL = "https://www.screener.in/company/INFY/"


def calc_request(source):
    return CalculationRequest.model_validate(
        dict(
            company_id=source.company.company_id,
            extraction_id=source.metadata.id,
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


def _citation(ref):
    return dict(
        kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id), item_key=ref.item_key
    )


def valid_claim(bundle, section="VALUATION"):
    ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    return ref, dict(
        section=section,
        statement="Observed valuation evidence is cited.",
        materiality="PRIMARY",
        citations=[_citation(ref)],
    )


def _row_count(database_path, table):
    import sqlite3

    with sqlite3.connect(database_path) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


async def _analyzed(settings, request, execution, claim, tmp_path, out="analysis-out"):
    return await analyze_company(
        settings, request, execution, tmp_path / out,
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )


async def test_honest_claim_is_supported_by_bundle(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "challenge-out"
    )
    assert len(result.claim_reviews) == 1
    review = result.claim_reviews[0]
    assert review.analyst_claim_id == analysis.claims[0].claim_id
    assert review.outcome == "SUPPORTED_BY_BUNDLE"
    assert review.findings == ()
    assert result.independence_basis == "DETERMINISTIC_ONLY"

    exported_path = tmp_path / "challenge-out/normalized/challenge_snapshot.json"
    exported = json.loads(exported_path.read_text())
    assert type(result).model_validate(exported) == result

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.challenges.challenge_history(analysis.id)
        assert history == (result,)
    finally:
        await database.close()


async def test_causal_language_without_management_quote_is_challenged(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    claim["statement"] = "Valuation improved because market sentiment shifted."
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out"
    )
    review = result.claim_reviews[0]
    assert review.outcome == "SUPPORTED_WITH_LIMITATIONS"
    assert any(f.finding_type == "UNSUPPORTED_CAUSALITY" for f in review.findings)


async def test_recommendation_language_is_invalid_against_bundle(world, tmp_path):
    """Independent re-scan of the frozen statement text - a defense-in-depth corroboration
    of a check Part 5B's own validator already runs at claim-creation time."""
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    # Tamper the persisted claim's statement directly in the DB row - bypassing Part 5B's
    # own claim-creation-time validator entirely, proving Part 5C re-checks independently.
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

    result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out"
    )
    review = result.claim_reviews[0]
    assert review.outcome == "INVALID_AGAINST_BUNDLE"
    assert any(f.finding_type == "FORBIDDEN_RECOMMENDATION_LANGUAGE" for f in review.findings)


async def test_tampered_calculation_row_refuses_challenge_persistence(world, tmp_path):
    """Corrupting an underlying calculation_snapshots row directly (not through any engine
    check) is DB-level corruption, not a reportable analytical finding: it must be refused
    at the same atomic-integrity layer Part 5B itself uses, exactly like
    test_snapshot_payload_corruption_refuses_insert_atomically in test_analysis_pipeline.py."""
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    calc_request_obj = request.model_copy(
        update={"valuation_snapshot_ids": (), "sections": ("HISTORICAL_FUNDAMENTALS",)}
    )
    bundle = await _bundle_for(settings, calc_request_obj)
    ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")
    claim = dict(
        section="HISTORICAL_FUNDAMENTALS", statement="Sales growth evidence is cited.",
        materiality="PRIMARY",
        citations=[_citation(ref)],
    )
    analysis = await analyze_company(
        settings, calc_request_obj, execution, tmp_path / "analysis-out",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )

    import sqlite3

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM calculation_snapshots WHERE id = ?", (str(calc_snapshot.id),)
        ).fetchone()
        payload = json.loads(row[0])
        payload["snapshot"]["results"][0]["label"] = "Tampered after analysis was persisted"
        connection.execute(
            "UPDATE calculation_snapshots SET payload = ? WHERE id = ?",
            (json.dumps(payload), str(calc_snapshot.id)),
        )
        connection.commit()

    with pytest.raises(ChallengeIntegrityError):
        await challenge_analysis(
            settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out"
        )
    assert _row_count(settings.database_path, "challenge_snapshots") == 0


async def test_direct_raw_artifact_corruption_refuses_challenge(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")

    with pytest.raises(ChallengeIntegrityError):
        await challenge_analysis(
            settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out"
        )
    assert _row_count(settings.database_path, "challenge_snapshots") == 0


async def test_snapshot_payload_corruption_between_review_and_insert_refuses_atomically(
    world, tmp_path
):
    """Mirrors Part 5B's own TOCTOU regression: corrupt the reviewed AnalysisSnapshot's OWN
    DB row directly between constructing a ChallengeSnapshot and persisting it, then call
    the repository directly. Proves SQLiteChallengeRepository re-verifies from the database
    itself, inside its own transaction, rather than trusting the object it was handed."""
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore

    store = FileRawArtifactStore(settings.artifact_path)
    database = SQLiteDatabase(settings.database_path)
    try:
        company = await database.repository.get(analysis.company_id)
        challenger_bundle, evidence = await challenge_engine.build_challenger_bundle(
            analysis, company,
            extractions=database.extractions, market=database.market,
            calculations=database.calculations, patterns=database.patterns,
            market_calculations=database.market_calculations, valuations=database.valuations,
            documents=database.documents, context=database.context, artifacts=store,
        )
        challenge_snapshot = challenge_engine.review_snapshot(challenger_bundle, evidence)
    finally:
        await database.close()

    import sqlite3

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM analysis_snapshots WHERE id = ?", (str(analysis.id),)
        ).fetchone()
        payload = json.loads(row[0])
        payload["claims"][0]["statement"] = "Tampered after Part 5C already built its review"
        connection.execute(
            "UPDATE analysis_snapshots SET payload = ? WHERE id = ?",
            (json.dumps(payload), str(analysis.id)),
        )
        connection.commit()

    assert _row_count(settings.database_path, "challenge_snapshots") == 0
    database = SQLiteDatabase(settings.database_path)
    try:
        with pytest.raises(ChallengeIntegrityError, match="hash mismatch"):
            await database.challenges.save_challenge(challenge_snapshot)
    finally:
        await database.close()
    assert _row_count(settings.database_path, "challenge_snapshots") == 0


async def test_append_only_history_across_repeated_reviews(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    first = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out1"
    )
    assert _row_count(settings.database_path, "challenge_snapshots") == 1
    second = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out2"
    )
    assert _row_count(settings.database_path, "challenge_snapshots") == 2
    assert first.id != second.id

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.challenges.challenge_history(analysis.id)
        assert history == (first, second)
    finally:
        await database.close()


async def test_deterministic_challenge_available_without_challenger(world, tmp_path):
    from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore

    settings, *_ = world
    store = FileRawArtifactStore(settings.artifact_path)
    database = SQLiteDatabase(settings.database_path)
    try:
        engine = ResearchEngine(database.repository)
        capabilities = await engine.get_capabilities()
        assert capabilities.deterministic_challenge_available is False
        assert capabilities.live_challenger_available is False

        deterministic_only_engine = ResearchEngine(
            database.repository,
            challenge=ChallengeServices(
                database.extractions, database.market, database.calculations, database.patterns,
                database.market_calculations, database.valuations, database.documents,
                database.context, store, database.analyses, database.challenges,
            ),
        )
        capabilities = await deterministic_only_engine.get_capabilities()
        assert capabilities.deterministic_challenge_available is True
        assert capabilities.live_challenger_available is False

        fake_provider_engine = ResearchEngine(
            database.repository,
            challenge=ChallengeServices(
                database.extractions, database.market, database.calculations, database.patterns,
                database.market_calculations, database.valuations, database.documents,
                database.context, store, database.analyses, database.challenges,
                challenger=FakeChallengerModel(), provider_kind="FAKE",
            ),
        )
        capabilities = await fake_provider_engine.get_capabilities()
        assert capabilities.deterministic_challenge_available is True
        assert capabilities.live_challenger_available is False
    finally:
        await database.close()


async def test_model_assisted_review_without_challenger_raises_without_silent_fallback(
    world, tmp_path
):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    with pytest.raises(RuntimeError, match="No LIVE challenger provider"):
        await challenge_analysis(
            settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out",
            execution=ChallengerExecutionSpec(template_id="c1", template_version=1),
        )


async def test_model_assisted_suggestion_becomes_non_blocking_finding_never_invalid(
    world, tmp_path
):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)
    analyst_claim_id = str(analysis.claims[0].claim_id)

    candidate = dict(
        analyst_claim_id=analyst_claim_id, suggestion_kind="MATERIAL_EVIDENCE",
        statement="A related metric worth also reviewing.",
        cited_evidence=[_citation(ref)],
    )
    result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out",
        execution=ChallengerExecutionSpec(template_id="c1", template_version=1),
        challenger=FakeChallengerModel(candidates=[candidate]), provider_kind="FAKE",
    )
    review = result.claim_reviews[0]
    assert review.outcome == "CHALLENGED"
    model_findings = [f for f in review.findings if f.source == "MODEL_ASSISTED"]
    assert len(model_findings) == 1
    assert model_findings[0].finding_type == "MODEL_SUGGESTED_MATERIAL_EVIDENCE"
    assert model_findings[0].severity == "NON_BLOCKING"
    # Both fakes default to provider="stub"; only model_id differs (analyst vs challenger).
    assert result.independence_basis == "SAME_PROVIDER_DIFFERENT_MODEL"
    assert result.challenger_model_metadata is not None


async def test_malformed_model_candidate_is_rejected_not_silently_dropped(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    bad_candidate = dict(
        analyst_claim_id=str(analysis.claims[0].claim_id),
        suggestion_kind="MATERIAL_EVIDENCE",
        statement="We recommend investors buy the stock.",
        cited_evidence=[_citation(ref)],
    )
    result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "out",
        execution=ChallengerExecutionSpec(template_id="c1", template_version=1),
        challenger=FakeChallengerModel(candidates=[bad_candidate]), provider_kind="FAKE",
    )
    assert len(result.rejected_candidates) == 1
    assert result.rejected_candidates[0].issues[0].code == "FORBIDDEN_RECOMMENDATION_LANGUAGE"
    assert result.claim_reviews[0].outcome == "SUPPORTED_BY_BUNDLE"


async def test_unknown_analysis_snapshot_is_a_malformed_request(world, tmp_path):
    from uuid import uuid4

    settings, *_ = world
    with pytest.raises(ValueError, match="Unknown analysis snapshot"):
        await challenge_analysis(
            settings, ChallengeRequest(analysis_snapshot_id=uuid4()), tmp_path / "out"
        )


async def test_cli_deterministic_round_trip(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "challenge-analysis",
            "--analysis-snapshot", str(analysis.id),
            "--output", str(tmp_path / "cli-out"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "cli-out/normalized/challenge_snapshot.json").exists()


async def test_cli_reports_provider_unavailable_without_secretly_using_fake(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    ref, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)
    execution_path = tmp_path / "challenger-execution.json"
    execution_path.write_text(
        ChallengerExecutionSpec(template_id="c1", template_version=1).model_dump_json()
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "challenge-analysis",
            "--analysis-snapshot", str(analysis.id),
            "--execution", str(execution_path),
            "--output", str(tmp_path / "cli-out"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 4, result.stderr
    assert "provider" in result.stderr.lower()
    assert not (tmp_path / "cli-out").exists()


async def test_cli_reports_malformed_request_for_unknown_snapshot(world, tmp_path):
    from uuid import uuid4

    settings, *_ = world
    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "challenge-analysis",
            "--analysis-snapshot", str(uuid4()),
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
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)

    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")

    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "challenge-analysis",
            "--analysis-snapshot", str(analysis.id),
            "--output", str(tmp_path / "cli-out"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 6, result.stderr
    assert not (tmp_path / "cli-out").exists()


async def test_r1_projection_provenance_preserves_challenger_closed_sources(world, tmp_path):
    """Richer model transport must not change Part 5C's closed evidence universe."""
    settings, _, _, _, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    analysis = await _analyzed(settings, request, execution, claim, tmp_path)
    from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore

    rendered = analysis.model_metadata.request.rendered_text
    assert '"projection_version":"ANALYST_MODEL_INPUT_V1"' in rendered
    database = SQLiteDatabase(settings.database_path)
    try:
        stored = await database.analyses.get_analysis(analysis.id)
        assert stored == analysis
        company = await database.repository.get(analysis.company_id)
        closed, _ = await challenge_engine.build_challenger_bundle(
            stored, company,
            extractions=database.extractions, market=database.market,
            calculations=database.calculations, patterns=database.patterns,
            market_calculations=database.market_calculations, valuations=database.valuations,
            documents=database.documents, context=database.context,
            artifacts=FileRawArtifactStore(settings.artifact_path),
        )
        assert closed.reviewed_analysis.input_bundle == analysis.input_bundle
    finally:
        await database.close()
