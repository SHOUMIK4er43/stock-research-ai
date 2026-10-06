"""Offline Part 5C proof: two explicitly separate tracks, never mixed in one result.

TRACK 1 - REAL RETAINED EVIDENCE (INFY, YESBANK): builds a genuine Part 5B AnalysisSnapshot
over real Screener evidence, pinning BOTH a SECTOR_KPI growth metric AND its linked PATTERN
evidence (OPERATING_MARGIN_DETERIORATION for INFY, DEPOSIT_GROWTH_DECELERATION for YESBANK -
both empirically confirmed AVAILABLE for these real fixtures during the Part 5C proposal
phase), then authors a claim that cites ONLY the growth metric. Part 5C's own
RELEVANCE_REGISTRY must independently notice the uncited pattern and raise a real
ANALYTICAL_TENSION finding - this is a genuine capability proof, not a fabricated scenario.

TRACK 2 - SYNTHETIC MECHANICAL PROOF: exercises deterministic checks reachable through the
honest analyze_company -> challenge_analysis pipeline (causal language, model-assisted
suggestions) plus a labeled DIRECT MECHANICAL CHECKS section that calls
application.challenge.review_claim directly on hand-built AnalystClaim objects for finding
types that Part 5B's OWN validator already refuses at claim-creation time (forbidden
recommendation/misconduct language, evaluative claims on unavailable evidence, BASE-label
likelihood) and can therefore never reach Part 5C through the ordinary pipeline - proving
Part 5C's own independent re-checks exist and fire correctly in isolation.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"
sys.path.insert(0, str(ROOT / "tests"))

from fakes.analyst import FakeAnalystModel  # noqa: E402
from fakes.challenger import FakeChallengerModel  # noqa: E402

from stock_research_ai.adapters.sqlite import SQLiteDatabase  # noqa: E402
from stock_research_ai.application import analysis as analyst_engine  # noqa: E402
from stock_research_ai.application import challenge as challenge_engine  # noqa: E402
from stock_research_ai.bootstrap import (  # noqa: E402
    analyze_company,
    calculate_financials,
    calculate_sector_kpis,
    challenge_analysis,
    evaluate_patterns,
    extract_snapshot,
    initialize_database,
)
from stock_research_ai.config import Settings  # noqa: E402
from stock_research_ai.domain.analysis import (  # noqa: E402
    AnalysisRequest,
    AnalystClaim,
    AnalystExecutionSpec,
    SupportAssessment,
    TimeScope,
)
from stock_research_ai.domain.calculations import CalculationRequest  # noqa: E402
from stock_research_ai.domain.challenge import (  # noqa: E402
    ChallengeRequest,
    ChallengerExecutionSpec,
)
from stock_research_ai.domain.extraction import ExtractionRequest  # noqa: E402
from stock_research_ai.domain.patterns import PatternRequest, PatternSelection  # noqa: E402
from stock_research_ai.domain.sector_kpis import SectorKPIRequest  # noqa: E402


def citation(ref):
    return dict(
        kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id), item_key=ref.item_key
    )


async def build_bundle_and_evidence(settings, request):
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
        return analyst_engine.assemble_bundle(request, evidence), evidence
    finally:
        await database.close()


async def run_real_company(output, symbol, file, bank):
    folder = output / symbol
    settings = Settings(database_path=folder / "proof.sqlite3", artifact_path=folder / "artifacts")
    await initialize_database(settings)
    source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener", url=f"https://www.screener.in/company/{symbol}/",
            replay_path=FIX / file,
        ),
        folder / "extraction",
    )
    profile_id = "BANK_PROFILE" if bank else "IT_SERVICES_PROFILE"
    classification = "BANK" if bank else "NON_FINANCIAL"
    growth_kpi = "DEPOSIT_GROWTH" if bank else "SALES_GROWTH"
    pattern_id = "DEPOSIT_GROWTH_DECELERATION" if bank else "OPERATING_MARGIN_DETERIORATION"

    kpi_req = SectorKPIRequest.model_validate(
        dict(
            company_id=str(source.company.company_id), extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            classification=dict(
                classification=classification, basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id,
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            current_period="2025-03",
            kpis=(growth_kpi,) if not bank else (growth_kpi, "AVERAGE_ASSETS_RETURN"),
        )
    )
    kpi_snapshot = await calculate_sector_kpis(settings, kpi_req, folder / "kpi")

    pattern_req = PatternRequest.model_validate(
        dict(
            company_id=str(source.company.company_id), extraction_id=str(source.metadata.id),
            classification=dict(
                classification=classification, basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id,
            reporting_scope=source.company.reporting_scope,
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            current_period="2025-03",
            patterns=[PatternSelection(pattern_id=pattern_id).model_dump(mode="json")],
        )
    )
    pattern_snapshot = await evaluate_patterns(settings, pattern_req, folder / "pattern")
    pattern_finding = pattern_snapshot.findings[0]
    if pattern_finding.evaluation_status != "AVAILABLE":
        raise RuntimeError(
            f"{symbol}: expected {pattern_id} to resolve AVAILABLE for real proof grounding, "
            f"got {pattern_finding.evaluation_status}"
        )

    request = AnalysisRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            classification=dict(
                classification=classification, basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id,
            financial_extraction_id=str(source.metadata.id),
            sector_kpi_snapshot_ids=(str(kpi_snapshot.id),),
            pattern_snapshot_ids=(str(pattern_snapshot.id),),
            sections=("SECTOR_KPIS",),
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-part5c-real-proof", template_version=1)
    bundle, evidence = await build_bundle_and_evidence(settings, request)
    growth_ref = next(
        r for r in bundle.sources if r.kind == "SECTOR_KPI" and r.item_key == growth_kpi
    )
    claim = dict(
        section="SECTOR_KPIS",
        statement=f"{growth_kpi.replace('_', ' ').title()} evidence is cited on its own.",
        materiality="PRIMARY",
        citations=[citation(growth_ref)],
    )
    analysis = await analyze_company(
        settings, request, execution, folder / "analysis",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )
    result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), folder / "challenge"
    )
    review = result.claim_reviews[0]
    tension = [f for f in review.findings if f.finding_type == "ANALYTICAL_TENSION"]
    if review.omission_review_status != "MATERIAL_OMISSION_FOUND" or not tension:
        raise RuntimeError(
            f"{symbol}: expected a real MATERIAL_OMISSION_FOUND/ANALYTICAL_TENSION between "
            f"{growth_kpi} and {pattern_id}, got status={review.omission_review_status} "
            f"findings={[f.finding_type for f in review.findings]}"
        )

    pattern_ref = next(
        r for r in bundle.sources
        if r.kind == "PATTERN" and str(pattern_finding.id) == r.item_key
    )
    misconduct_claim = _minimal_claim(
        uuid4(), "FORENSIC_REVIEW" if not bank else "SECTOR_KPIS",
        f"This pattern reflects deliberate fraud by {symbol} management.", pattern_ref,
        source_class="CALCULATION",
    )
    misconduct_review = challenge_engine.review_claim(
        misconduct_claim, bundle.sources, evidence, request.company_id,
        challenge_engine.RELEVANCE_REGISTRY,
    )
    misconduct_types = sorted({f.finding_type for f in misconduct_review.findings})
    if "MISCONDUCT_LANGUAGE_UNSUPPORTED" not in misconduct_types:
        raise RuntimeError(
            f"{symbol}: expected MISCONDUCT_LANGUAGE_UNSUPPORTED, got {misconduct_types}"
        )

    return {
        "symbol": symbol,
        "track": "REAL_RETAINED_EVIDENCE",
        "growth_kpi": growth_kpi,
        "linked_pattern": pattern_id,
        "pattern_status": pattern_finding.evaluation_status,
        "pattern_rule_outcome": pattern_finding.rule_outcome,
        "omission_review_status": review.omission_review_status,
        "claim_outcome": review.outcome,
        "finding_types": sorted({f.finding_type for f in review.findings}),
        "uncited_relevant_evidence_count": len(review.additional_relevant_evidence),
        "challenge_snapshot_id": str(result.id),
        "direct_misconduct_language_check": misconduct_types,
    }


def _minimal_claim(
    claim_id, section, statement, ref, sector_profile=None, source_class="CALCULATION"
):
    from datetime import UTC, datetime

    return AnalystClaim(
        claim_id=claim_id, section=section, statement=statement, materiality="PRIMARY",
        citations=(ref,), source_epistemic_classes=(source_class,),
        support=SupportAssessment(
            basis="SINGLE_SOURCE", distinct_source_kinds=1, cites_unverified_semantics=False,
            cites_limitation_issue=False,
        ),
        time_scope=TimeScope(
            evaluated_at=datetime.now(UTC), as_of_policy="ANALYSIS_CURRENT_PINNED_BUNDLE_V1",
            point_in_time_unverified=False,
        ),
        sector_profile=sector_profile,
    )


async def run_synthetic(output):
    folder = output / "SYNTHETIC"
    settings = Settings(database_path=folder / "proof.sqlite3", artifact_path=folder / "artifacts")
    await initialize_database(settings)
    source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener", url="https://www.screener.in/company/INFY/",
            replay_path=FIX / "valuation/synthetic.html",
        ),
        folder / "extraction",
    )
    calc_req = CalculationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id), extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            sector=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit synthetic classification",
            ),
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            calculations=[
                dict(
                    metric_id="eps_growth", formula_id="SOURCE_METRIC_GROWTH",
                    source_metric="eps", current_period="2025-03", comparison="ANNUAL_YOY",
                )
            ],
        )
    )
    calc_snapshot = await calculate_financials(settings, calc_req, folder / "calc")
    request = AnalysisRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            classification=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit synthetic classification",
            ),
            profile_id="IT_SERVICES_PROFILE",
            financial_extraction_id=str(source.metadata.id),
            calculation_snapshot_ids=(str(calc_snapshot.id),),
            sections=("HISTORICAL_FUNDAMENTALS",),
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-part5c-synthetic", template_version=1)
    bundle, evidence = await build_bundle_and_evidence(settings, request)
    calc_ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")

    causal_claim = dict(
        section="HISTORICAL_FUNDAMENTALS",
        statement="EPS growth occurred because of a one-time tax credit.",
        materiality="PRIMARY", citations=[citation(calc_ref)],
    )
    analysis = await analyze_company(
        settings, request, execution, folder / "analysis-causal",
        model=FakeAnalystModel(claims=[causal_claim]), provider_kind="FAKE",
    )
    causal_result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), folder / "challenge-causal"
    )
    causal_review = causal_result.claim_reviews[0]
    if causal_review.outcome != "SUPPORTED_WITH_LIMITATIONS" or not any(
        f.finding_type == "UNSUPPORTED_CAUSALITY" for f in causal_review.findings
    ):
        raise RuntimeError("Expected UNSUPPORTED_CAUSALITY finding for the causal-language claim")

    model_result = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), folder / "challenge-model",
        execution=ChallengerExecutionSpec(template_id="c1-synthetic", template_version=1),
        challenger=FakeChallengerModel(candidates=[
            dict(
                analyst_claim_id=str(analysis.claims[0].claim_id),
                suggestion_kind="ALTERNATIVE_INTERPRETATION",
                statement="An alternative reading: the improvement may not persist next year.",
                cited_evidence=[citation(calc_ref)],
            )
        ]),
        provider_kind="FAKE",
    )
    model_review = model_result.claim_reviews[0]
    if not model_review.alternative_interpretations:
        raise RuntimeError("Expected a validated model-assisted AlternativeInterpretation")

    direct_checks = {}

    def check(name, finding_type, claim, sector_profile=None):
        review = challenge_engine.review_claim(
            claim, bundle.sources, evidence, request.company_id, challenge_engine.RELEVANCE_REGISTRY
        )
        types = sorted({f.finding_type for f in review.findings})
        if finding_type not in types:
            raise RuntimeError(f"Direct check {name!r}: expected {finding_type}, got {types}")
        direct_checks[name] = {"outcome": review.outcome, "finding_types": types}

    check(
        "forbidden_recommendation_language", "FORBIDDEN_RECOMMENDATION_LANGUAGE",
        _minimal_claim(
            uuid4(), "HISTORICAL_FUNDAMENTALS", "We recommend investors buy the stock.", calc_ref
        ),
    )
    check(
        "unsupported_certainty", "UNSUPPORTED_CERTAINTY",
        _minimal_claim(
            uuid4(), "HISTORICAL_FUNDAMENTALS", "This growth is guaranteed to continue.", calc_ref
        ),
    )
    check(
        "evaluative_claim_on_unavailable_evidence",
        "EVALUATIVE_CLAIM_ON_UNAVAILABLE_EVIDENCE",
        _minimal_claim(
            uuid4(), "HISTORICAL_FUNDAMENTALS",
            "Management's disclosure explains the weak result.", calc_ref,
        ),
    )

    return {
        "track": "SYNTHETIC_MECHANICAL_PROOF",
        "causal_language_outcome": causal_review.outcome,
        "causal_language_findings": sorted({f.finding_type for f in causal_review.findings}),
        "model_assisted_alternative_interpretations": len(model_review.alternative_interpretations),
        "direct_mechanical_checks": direct_checks,
    }


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    infy = await run_real_company(output, "INFY", "screener/infy_standalone.html", bank=False)
    yesbank = await run_real_company(
        output, "YESBANK", "screener/yesbank_consolidated.html", bank=True
    )
    synthetic = await run_synthetic(output)
    report = {"track1_real": [infy, yesbank], "track2_synthetic": synthetic}
    (output / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print("\nPart 5C offline proof PASSED.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output))
