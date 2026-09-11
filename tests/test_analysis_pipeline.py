"""Engine-level persistence, immutability, revision, corruption-refusal and CLI proof.

Offline only: FakeAnalystModel never touches a network or a live provider.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fakes.analyst import FakeAnalystModel

from stock_research_ai.adapters.sqlite import SQLiteDatabase
from stock_research_ai.application import analysis as analyst_engine
from stock_research_ai.application.engine import AnalystServices, ResearchEngine
from stock_research_ai.application.hashing import digest
from stock_research_ai.bootstrap import (
    analyze_company,
    calculate_financials,
    evaluate_valuation,
    extract_snapshot,
    initialize_database,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import AnalysisRequest, AnalystExecutionSpec
from stock_research_ai.domain.calculations import CalculationRequest
from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.valuation import ValuationRequest

FIX = Path(__file__).parent / "fixtures"


def calc_request(source, extraction_id=None):
    return CalculationRequest.model_validate(
        dict(
            company_id=source.company.company_id,
            extraction_id=extraction_id or source.metadata.id,
            reporting_scope=source.company.reporting_scope,
            sector=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit IT proof classification",
            ),
            alignment=dict(
                fiscal_year_end_month=3, declaration="Caller declares March fiscal year end."
            ),
            calculations=[
                dict(
                    metric_id="infy_sales_growth",
                    formula_id="SOURCE_METRIC_GROWTH",
                    source_metric="sales",
                    current_period="2025-03",
                    comparison="ANNUAL_YOY",
                )
            ],
        )
    )


def pe_request(source):
    rows = [o for o in source.top_metrics if o.metric_id == "current_price"]
    eps_rows = [
        o
        for o in source.profit_loss
        if o.metric_id == "eps" and o.period.fiscal_label == "Mar 2026"
    ]
    return ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            profile_id="IT_SERVICES_PROFILE",
            classification=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof profile",
            ),
            selections=[
                dict(
                    method="PRICE_TO_EARNINGS",
                    price=dict(
                        kind="FINANCIAL",
                        snapshot_id=str(source.metadata.id),
                        observation_id=str(rows[-1].id),
                    ),
                    denominator=dict(
                        kind="FINANCIAL",
                        snapshot_id=str(source.metadata.id),
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
            provider="screener",
            url="https://www.screener.in/company/INFY/",
            replay_path=FIX / "screener/infy_standalone.html",
        ),
        tmp_path / "extraction",
    )
    calc_snapshot = await calculate_financials(settings, calc_request(source), tmp_path / "calc")
    valuation_snapshot = await evaluate_valuation(settings, pe_request(source), tmp_path / "val")
    request = AnalysisRequest.model_validate(
        dict(
            company_id=source.company.company_id,
            classification=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
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


def valid_claim(bundle):
    ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    return ref, dict(
        section="VALUATION",
        statement="Observed valuation evidence is cited.",
        materiality="PRIMARY",
        citations=[
            dict(
                kind=ref.kind,
                owning_snapshot_id=str(ref.owning_snapshot_id),
                item_key=ref.item_key,
            )
        ],
    )


async def _bundle_for(settings, request):
    database = SQLiteDatabase(settings.database_path)
    try:
        company = await database.repository.get(request.company_id)
        evidence = await analyst_engine.resolve_evidence(
            request,
            company,
            extractions=database.extractions,
            market=database.market,
            calculations=database.calculations,
            patterns=database.patterns,
            market_calculations=database.market_calculations,
            valuations=database.valuations,
            documents=database.documents,
            context=database.context,
        )
        return analyst_engine.assemble_bundle(request, evidence)
    finally:
        await database.close()


async def test_engine_persists_exact_bundle_and_reconstructs(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    model = FakeAnalystModel(claims=[claim])
    snapshot = await analyze_company(
        settings, request, execution, tmp_path / "out", model=model, provider_kind="FAKE"
    )
    assert snapshot.bundle_sha256 == digest(snapshot.input_bundle)
    assert len(snapshot.claims) == 1 and snapshot.claims[0].epistemic_class == "INTERPRETATION"
    assert snapshot.rejected == ()
    exported = json.loads((tmp_path / "out/normalized/analysis_snapshot.json").read_text())
    reconstructed = type(snapshot).model_validate(exported)
    assert reconstructed == snapshot

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.analyses.analysis_history(request.company_id)
        assert history == (snapshot,)
    finally:
        await database.close()


async def test_rejected_candidate_persisted_alongside_accepted(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, good = valid_claim(bundle)
    bad = dict(
        section="VALUATION",
        statement="We recommend investors buy the stock.",
        materiality="PRIMARY",
        citations=good["citations"],
    )
    model = FakeAnalystModel(claims=[good, bad])
    snapshot = await analyze_company(
        settings, request, execution, tmp_path / "out", model=model, provider_kind="FAKE"
    )
    assert len(snapshot.claims) == 1
    assert len(snapshot.rejected) == 1
    assert snapshot.rejected[0].issues[0].code == "FORBIDDEN_RECOMMENDATION_LANGUAGE"
    assert snapshot.rejected[0].raw_candidate == bad


async def test_provider_model_template_and_generation_param_changes_create_new_snapshot(
    world, tmp_path
):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    original_model = FakeAnalystModel(claims=[claim], model_id="model-a")
    original = await analyze_company(
        settings, request, execution, tmp_path / "out1", model=original_model, provider_kind="FAKE"
    )
    before = original.model_dump_json()

    template_changed = await analyze_company(
        settings,
        request,
        AnalystExecutionSpec(template_id="v2-base", template_version=1),
        tmp_path / "out2",
        model=FakeAnalystModel(claims=[claim], model_id="model-a"),
        provider_kind="FAKE",
    )
    model_changed = await analyze_company(
        settings,
        request,
        execution,
        tmp_path / "out3",
        model=FakeAnalystModel(claims=[claim], model_id="model-b"),
        provider_kind="FAKE",
    )
    params_changed = await analyze_company(
        settings,
        request,
        AnalystExecutionSpec(
            template_id="v1-base", template_version=1, generation_parameters={"temperature": 0.2}
        ),
        tmp_path / "out4",
        model=FakeAnalystModel(claims=[claim], model_id="model-a"),
        provider_kind="FAKE",
    )
    ids = {original.id, template_changed.id, model_changed.id, params_changed.id}
    assert len(ids) == 4

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.analyses.analysis_history(request.company_id)
        assert len(history) == 4
        assert history[0].model_dump_json() == before
    finally:
        await database.close()


async def test_evidence_revision_creates_new_snapshot(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    first = await analyze_company(
        settings, request, execution, tmp_path / "out1",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )
    before = first.model_dump_json()

    revised_source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener",
            url="https://www.screener.in/company/INFY/",
            replay_path=FIX / "screener/infy_standalone.html",
        ),
        tmp_path / "extraction2",
    )
    revised_calc = await calculate_financials(
        settings, calc_request(revised_source), tmp_path / "calc2"
    )
    revised_valuation = await evaluate_valuation(
        settings, pe_request(revised_source), tmp_path / "val2"
    )
    revised_request = request.model_copy(
        update={
            "financial_extraction_id": revised_source.metadata.id,
            "calculation_snapshot_ids": (revised_calc.id,),
            "valuation_snapshot_ids": (revised_valuation.id,),
        }
    )
    revised_bundle = await _bundle_for(settings, revised_request)
    _, revised_claim = valid_claim(revised_bundle)
    second = await analyze_company(
        settings, revised_request, execution, tmp_path / "out2",
        model=FakeAnalystModel(claims=[revised_claim]), provider_kind="FAKE",
    )
    assert second.id != first.id
    assert second.request.financial_extraction_id != first.request.financial_extraction_id

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.analyses.analysis_history(request.company_id)
        assert len(history) == 2 and history[0].model_dump_json() == before
    finally:
        await database.close()


async def test_direct_raw_artifact_corruption_refuses_persistence(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        await analyze_company(
            settings, request, execution, tmp_path / "out",
            model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
        )
    database = SQLiteDatabase(settings.database_path)
    try:
        assert await database.analyses.analysis_history(request.company_id) == ()
    finally:
        await database.close()


async def test_derived_snapshot_upstream_raw_corruption_refuses_persistence(world, tmp_path):
    """Safeguard 1B: pin ONLY the calculation snapshot (no FINANCIAL evidence directly),
    corrupt the raw bytes underneath its ORIGINATING extraction, and prove persistence
    still refuses - proving the transitive walk, not just top-level snapshot hashes."""
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    calc_only_request = request.model_copy(
        update={"financial_extraction_id": None, "valuation_snapshot_ids": ()}
    )
    bundle = await _bundle_for(settings, calc_only_request)
    ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")
    claim = dict(
        section="HISTORICAL_FUNDAMENTALS",
        statement="Sales growth evidence is cited.",
        materiality="PRIMARY",
        citations=[
            dict(
                kind=ref.kind,
                owning_snapshot_id=str(ref.owning_snapshot_id),
                item_key=ref.item_key,
            )
        ],
    )
    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        await analyze_company(
            settings, calc_only_request, execution, tmp_path / "out",
            model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
        )


def _row_count(database_path, table):
    import sqlite3

    with sqlite3.connect(database_path) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608


async def test_snapshot_payload_corruption_refuses_insert_atomically(world, tmp_path):
    """Regression for the atomic-persistence fix: corrupt a pinned CALCULATION SNAPSHOT'S
    OWN DB ROW payload directly (not a raw artifact file, and not through analyze_company's
    own engine-level checks) between building a snapshot and persisting it, then call the
    repository directly. Proves SQLiteAnalysisRepository re-verifies from the database
    itself, inside its own transaction, rather than trusting anything the caller already
    checked earlier - closing the exact TOCTOU gap the review flagged."""
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    model = FakeAnalystModel(claims=[claim])
    model_metadata = await model.generate(bundle, execution)
    from stock_research_ai.application.analysis_validation import validate_response

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
    finally:
        await database.close()
    claims, rejected = validate_response(
        model_metadata.raw_response, bundle, evidence, request.profile_id
    )
    from stock_research_ai.domain.analysis import AnalysisSnapshot

    snapshot = AnalysisSnapshot(
        id=uuid4(), company_id=request.company_id, request=request, execution=execution,
        input_bundle=bundle, bundle_sha256=digest(bundle), sources=bundle.sources,
        model_metadata=model_metadata, section_coverage=bundle.section_coverage,
        claims=claims, management_statements=(), rejected=rejected,
        evaluated_at=bundle.assembled_at,
    )
    assert _row_count(settings.database_path, "calculation_snapshots") == 1

    import json
    import sqlite3

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM calculation_snapshots WHERE id = ?", (str(calc_snapshot.id),)
        ).fetchone()
        payload = json.loads(row[0])
        payload["snapshot"]["results"][0]["label"] = "Tampered after engine validation"
        connection.execute(
            "UPDATE calculation_snapshots SET payload = ? WHERE id = ?",
            (json.dumps(payload), str(calc_snapshot.id)),
        )
        connection.commit()

    assert _row_count(settings.database_path, "analysis_snapshots") == 0
    database = SQLiteDatabase(settings.database_path)
    try:
        with pytest.raises(ValueError, match="hash mismatch"):
            await database.analyses.save_analysis(snapshot)
    finally:
        await database.close()
    assert _row_count(settings.database_path, "analysis_snapshots") == 0


async def test_failed_integrity_check_leaves_row_count_unchanged(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    await analyze_company(
        settings, request, execution, tmp_path / "good",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )
    assert _row_count(settings.database_path, "analysis_snapshots") == 1

    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        await analyze_company(
            settings, request, execution, tmp_path / "bad",
            model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
        )
    # Row count is exactly what it was before the failed attempt - no partial append.
    assert _row_count(settings.database_path, "analysis_snapshots") == 1


async def test_successful_append_and_old_snapshots_remain_reconstructable(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    bundle = await _bundle_for(settings, request)
    _, claim = valid_claim(bundle)
    first = await analyze_company(
        settings, request, execution, tmp_path / "out1",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )
    assert _row_count(settings.database_path, "analysis_snapshots") == 1
    before = first.model_dump_json()

    second = await analyze_company(
        settings, request, execution, tmp_path / "out2",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )
    assert _row_count(settings.database_path, "analysis_snapshots") == 2
    assert second.id != first.id

    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.analyses.analysis_history(request.company_id)
        assert len(history) == 2
        assert history[0].model_dump_json() == before
        assert history == (first, second)
    finally:
        await database.close()


async def test_production_bootstrap_never_uses_fake_model(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    with pytest.raises(RuntimeError, match="No LIVE analyst provider"):
        await analyze_company(settings, request, execution, tmp_path / "out")


async def test_analysis_available_false_without_live_provider(world, tmp_path):
    settings, *_ = world
    database = SQLiteDatabase(settings.database_path)
    try:
        engine = ResearchEngine(database.repository)
        assert (await engine.get_capabilities()).analysis_available is False

        engine_with_fake = ResearchEngine(
            database.repository,
            analyst=AnalystServices(
                database.extractions, database.market, database.calculations, database.patterns,
                database.market_calculations, database.valuations, database.documents,
                database.context, database.repository, FakeAnalystModel(), database.analyses,
                provider_kind="FAKE",
            ),
        )
        assert (await engine_with_fake.get_capabilities()).analysis_available is False
    finally:
        await database.close()


async def test_cli_reports_provider_unavailable_without_secretly_using_fake(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    request_path = tmp_path / "request.json"
    request_path.write_text(request.model_dump_json())
    execution_path = tmp_path / "execution.json"
    execution_path.write_text(execution.model_dump_json())
    result = subprocess.run(
        [
            sys.executable, "-m", "stock_research_ai",
            "--database", str(settings.database_path),
            "analyze-company",
            "--request", str(request_path),
            "--execution", str(execution_path),
            "--output", str(tmp_path / "cli-out"),
        ],
        env=dict(os.environ, STOCK_RESEARCH_AI_ARTIFACT_PATH=str(settings.artifact_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 4, result.stderr
    assert "provider" in result.stderr.lower()
    assert not (tmp_path / "cli-out").exists()


async def test_variant_perception_requires_exact_assumption_lineage(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    dcf_request = ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            profile_id="IT_SERVICES_PROFILE",
            classification=dict(
                classification="NON_FINANCIAL",
                basis="CALLER_DECLARED",
                declaration="Explicit proof profile",
            ),
            selections=[
                dict(
                    method="FCFF_DCF",
                    scenario=dict(
                        scenario_id=str(uuid4()),
                        label="BASE",
                        method="FCFF_DCF",
                        assumptions=[
                            dict(
                                assumption_id=str(uuid4()),
                                semantic=name,
                                value=value,
                                unit=unit,
                                currency="INR" if unit == "CURRENCY" else None,
                                provenance_basis="CALLER_DECLARED",
                                declaration="Explicit synthetic scenario input",
                            )
                            for name, value, unit in (
                                ("fcff_1", "110", "CURRENCY"),
                                ("fcff_2", "121", "CURRENCY"),
                                ("horizon", "2", "YEARS"),
                                ("wacc", "0.10", "DECIMAL_RATE"),
                                ("terminal_growth", "0", "DECIMAL_RATE"),
                                ("net_debt", "200", "CURRENCY"),
                                ("diluted_shares", "10", "SHARES"),
                            )
                        ],
                    ),
                )
            ],
        )
    )
    dcf_snapshot = await evaluate_valuation(settings, dcf_request, tmp_path / "dcf")
    variant_request = request.model_copy(
        update={"valuation_snapshot_ids": (dcf_snapshot.id,), "sections": ("VARIANT_PERCEPTION",)}
    )
    bundle = await _bundle_for(settings, variant_request)
    valuation_ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    calc_ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")
    real_assumption_ids = [
        str(a.assumption_id) for a in dcf_request.selections[0].scenario.assumptions
    ]

    from stock_research_ai.application.analysis_validation import validate_candidate

    database = SQLiteDatabase(settings.database_path)
    try:
        company = await database.repository.get(variant_request.company_id)
        evidence = await analyst_engine.resolve_evidence(
            variant_request, company,
            extractions=database.extractions, market=database.market,
            calculations=database.calculations, patterns=database.patterns,
            market_calculations=database.market_calculations, valuations=database.valuations,
            documents=database.documents, context=database.context,
        )
    finally:
        await database.close()

    def citation(ref):
        return dict(
            kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id), item_key=ref.item_key
        )

    good = dict(
        section="VARIANT_PERCEPTION",
        statement="Market-implied and fundamental evidence are compared.",
        materiality="PRIMARY",
        citations=[citation(valuation_ref), citation(calc_ref)],
        dependency_assumption_ids=real_assumption_ids,
    )
    claim, rejection = validate_candidate(good, bundle, evidence, "IT_SERVICES_PROFILE")
    assert rejection is None and claim is not None

    bad = dict(good, dependency_assumption_ids=[str(uuid4())])
    claim, rejection = validate_candidate(bad, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None
    assert rejection.issues[0].code == "VARIANT_PERCEPTION_ASSUMPTION_LINEAGE_INVALID"


async def test_base_label_receives_no_probability_language(world, tmp_path):
    settings, source, calc_snapshot, valuation_snapshot, request, execution = world
    dcf_request = ValuationRequest.model_validate(
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
                    method="FCFF_DCF",
                    scenario=dict(
                        scenario_id=str(uuid4()),
                        label="BASE",
                        method="FCFF_DCF",
                        assumptions=[
                            dict(
                                assumption_id=str(uuid4()), semantic=name, value=value, unit=unit,
                                currency="INR" if unit == "CURRENCY" else None,
                                provenance_basis="CALLER_DECLARED",
                                declaration="Explicit synthetic scenario input",
                            )
                            for name, value, unit in (
                                ("fcff_1", "110", "CURRENCY"), ("fcff_2", "121", "CURRENCY"),
                                ("horizon", "2", "YEARS"), ("wacc", "0.10", "DECIMAL_RATE"),
                                ("terminal_growth", "0", "DECIMAL_RATE"),
                                ("net_debt", "200", "CURRENCY"), ("diluted_shares", "10", "SHARES"),
                            )
                        ],
                    ),
                )
            ],
        )
    )
    dcf_snapshot = await evaluate_valuation(settings, dcf_request, tmp_path / "dcf")
    bull_request = request.model_copy(
        update={
            "valuation_snapshot_ids": (dcf_snapshot.id,),
            "sections": ("BULL_BASE_BEAR_INTERPRETATION",),
        }
    )
    bundle = await _bundle_for(settings, bull_request)
    ref = next(r for r in bundle.sources if r.kind == "VALUATION")

    from stock_research_ai.application.analysis_validation import validate_candidate

    database = SQLiteDatabase(settings.database_path)
    try:
        company = await database.repository.get(bull_request.company_id)
        evidence = await analyst_engine.resolve_evidence(
            bull_request, company,
            extractions=database.extractions, market=database.market,
            calculations=database.calculations, patterns=database.patterns,
            market_calculations=database.market_calculations, valuations=database.valuations,
            documents=database.documents, context=database.context,
        )
    finally:
        await database.close()

    citation = dict(
        kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id), item_key=ref.item_key
    )
    candidate = dict(
        section="BULL_BASE_BEAR_INTERPRETATION",
        statement="This base scenario is the most likely outcome.",
        materiality="PRIMARY",
        citations=[citation],
    )
    claim, rejection = validate_candidate(candidate, bundle, evidence, "IT_SERVICES_PROFILE")
    assert claim is None
    assert rejection.issues[0].code == "BASE_LABEL_LIKELIHOOD_LANGUAGE"

    honest = dict(candidate, statement="This base scenario's assumptions are stated explicitly.")
    claim, rejection = validate_candidate(honest, bundle, evidence, "IT_SERVICES_PROFILE")
    assert rejection is None and claim is not None
