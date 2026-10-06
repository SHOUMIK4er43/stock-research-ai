"""Offline Part 6A proof: two explicitly separate tracks, never mixed in one result.

TRACK 1 - REAL RETAINED EVIDENCE (INFY, YESBANK): builds the full frozen chain -
evidence -> deterministic calculations -> Part 5B AnalysisSnapshot -> Part 5C
ChallengeSnapshot -> Part 6A ResearchReportSnapshot - over real Screener/document
fixtures only. For YESBANK, both genuinely retained document fixtures
(yesbank_presentation.pdf, yesbank_transcript.pdf) are ingested through the normal
Part 3A pipeline and cited, proving Document Corpus + Concall/Investor-Presentation
Intelligence render from real evidence, with the transcript-neutral speaker rule
applied for real.

TRACK 2 - SYNTHETIC MECHANICAL PROOF: exercises report generation, narrator
numeric-closure/fallback, and atomic-integrity corruption-blocks-append using the
same synthetic fixture pattern already established by verify_analysis.py/
verify_challenge.py. Never presented as real evidence.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"
sys.path.insert(0, str(ROOT / "tests"))

from fakes.analyst import FakeAnalystModel  # noqa: E402
from fakes.narrator import FakeReportNarrator  # noqa: E402

from stock_research_ai.adapters.sqlite import SQLiteDatabase  # noqa: E402
from stock_research_ai.application import analysis as analyst_engine  # noqa: E402
from stock_research_ai.application.report import ReportIntegrityError  # noqa: E402
from stock_research_ai.bootstrap import (  # noqa: E402
    analyze_company,
    calculate_financials,
    calculate_sector_kpis,
    challenge_analysis,
    evaluate_patterns,
    evaluate_valuation,
    extract_snapshot,
    generate_report,
    ingest_document,
    initialize_database,
)
from stock_research_ai.config import Settings  # noqa: E402
from stock_research_ai.domain.analysis import AnalysisRequest, AnalystExecutionSpec  # noqa: E402
from stock_research_ai.domain.calculations import CalculationRequest  # noqa: E402
from stock_research_ai.domain.challenge import ChallengeRequest  # noqa: E402
from stock_research_ai.domain.documents import DocumentRequest, DocumentType  # noqa: E402
from stock_research_ai.domain.extraction import ExtractionRequest  # noqa: E402
from stock_research_ai.domain.patterns import PatternRequest, PatternSelection  # noqa: E402
from stock_research_ai.domain.report import ReportExecutionSpec, ReportRequest  # noqa: E402
from stock_research_ai.domain.sector_kpis import SectorKPIRequest  # noqa: E402
from stock_research_ai.domain.valuation import ValuationRequest  # noqa: E402


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


async def run_infy(output):
    symbol = "INFY"
    folder = output / symbol
    settings = Settings(database_path=folder / "proof.sqlite3", artifact_path=folder / "artifacts")
    await initialize_database(settings)
    source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener", url=f"https://www.screener.in/company/{symbol}/",
            replay_path=FIX / "screener/infy_standalone.html",
        ),
        folder / "extraction",
    )
    profile_id = "IT_SERVICES_PROFILE"
    kpi_req = SectorKPIRequest.model_validate(
        dict(
            company_id=str(source.company.company_id), extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            classification=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id,
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            current_period="2025-03", kpis=("SALES_GROWTH",),
        )
    )
    kpi_snapshot = await calculate_sector_kpis(settings, kpi_req, folder / "kpi")

    pattern_req = PatternRequest.model_validate(
        dict(
            company_id=str(source.company.company_id), extraction_id=str(source.metadata.id),
            classification=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id, reporting_scope=source.company.reporting_scope,
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            current_period="2025-03",
            patterns=[PatternSelection(pattern_id="OPERATING_MARGIN_DETERIORATION").model_dump(
                mode="json"
            )],
        )
    )
    pattern_snapshot = await evaluate_patterns(settings, pattern_req, folder / "pattern")

    price_rows = [o for o in source.top_metrics if o.metric_id == "current_price"]
    eps_rows = [
        o for o in source.profit_loss
        if o.metric_id == "eps" and o.period.fiscal_label == "Mar 2026"
    ]
    val_req = ValuationRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            financial_extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope, profile_id=profile_id,
            classification=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            selections=[
                dict(
                    method="PRICE_TO_EARNINGS",
                    price=dict(
                        kind="FINANCIAL", snapshot_id=str(source.metadata.id),
                        observation_id=str(price_rows[-1].id),
                    ),
                    denominator=dict(
                        kind="FINANCIAL", snapshot_id=str(source.metadata.id),
                        observation_id=str(eps_rows[-1].id),
                    ),
                )
            ],
        )
    )
    valuation_snapshot = await evaluate_valuation(settings, val_req, folder / "valuation")

    request = AnalysisRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            classification=dict(
                classification="NON_FINANCIAL", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id, financial_extraction_id=str(source.metadata.id),
            sector_kpi_snapshot_ids=(str(kpi_snapshot.id),),
            pattern_snapshot_ids=(str(pattern_snapshot.id),),
            valuation_snapshot_ids=(str(valuation_snapshot.id),),
            sections=("SECTOR_KPIS", "VALUATION"),
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-part6a-real-proof", template_version=1)
    bundle, _ = await build_bundle_and_evidence(settings, request)
    growth_ref = next(r for r in bundle.sources if r.kind == "SECTOR_KPI")
    valuation_ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    claims = [
        dict(section="SECTOR_KPIS", statement="Sales growth evidence is cited on its own.",
             materiality="PRIMARY", citations=[citation(growth_ref)]),
        dict(section="VALUATION", statement="Observed P/E valuation evidence is cited.",
             materiality="PRIMARY", citations=[citation(valuation_ref)]),
    ]
    analysis = await analyze_company(
        settings, request, execution, folder / "analysis",
        model=FakeAnalystModel(claims=claims), provider_kind="FAKE",
    )
    challenge = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), folder / "challenge"
    )
    tension_review = next(
        r for r in challenge.claim_reviews
        if any(f.finding_type == "ANALYTICAL_TENSION" for f in r.findings)
    )
    report = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), folder / "report"
    )

    if report.document_corpus.entries:
        raise RuntimeError("INFY real proof expected an empty document corpus (none pinned)")
    valuation_section = next(s for s in report.sections if s.section == "VALUATION")
    if any(m.label.upper().find("FCFF") >= 0 for t in valuation_section.tables for m, in t.rows):
        raise RuntimeError("INFY real proof must never show an FCFF row (never requested)")
    if report.synthesis.valuation_evidence_state != "OBSERVED":
        raise RuntimeError(
            f"Expected OBSERVED valuation evidence state, got "
            f"{report.synthesis.valuation_evidence_state}"
        )
    return {
        "symbol": "INFY", "track": "REAL_RETAINED_EVIDENCE",
        "report_id": str(report.id),
        "research_completeness": report.synthesis.research_completeness,
        "valuation_evidence_state": report.synthesis.valuation_evidence_state,
        "document_corpus_entries": len(report.document_corpus.entries),
        "challenger_tension_claim": str(tension_review.analyst_claim_id),
        "monitoring_entries": len(report.monitoring),
        "data_gap_sections": sum(1 for s in report.sections if s.coverage.status != "AVAILABLE"),
    }


async def run_yesbank(output):
    symbol = "YESBANK"
    folder = output / symbol
    settings = Settings(database_path=folder / "proof.sqlite3", artifact_path=folder / "artifacts")
    await initialize_database(settings)
    source = await extract_snapshot(
        settings,
        ExtractionRequest(
            provider="screener", url=f"https://www.screener.in/company/{symbol}/",
            replay_path=FIX / "screener/yesbank_consolidated.html",
        ),
        folder / "extraction",
    )
    profile_id = "BANK_PROFILE"
    kpi_req = SectorKPIRequest.model_validate(
        dict(
            company_id=str(source.company.company_id), extraction_id=str(source.metadata.id),
            reporting_scope=source.company.reporting_scope,
            classification=dict(
                classification="BANK", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id,
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            current_period="2025-03", kpis=("DEPOSIT_GROWTH",),
        )
    )
    kpi_snapshot = await calculate_sector_kpis(settings, kpi_req, folder / "kpi")

    pattern_req = PatternRequest.model_validate(
        dict(
            company_id=str(source.company.company_id), extraction_id=str(source.metadata.id),
            classification=dict(
                classification="BANK", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id, reporting_scope=source.company.reporting_scope,
            alignment=dict(fiscal_year_end_month=3, declaration="Caller declares March FYE."),
            current_period="2025-03",
            patterns=[PatternSelection(pattern_id="DEPOSIT_GROWTH_DECELERATION").model_dump(
                mode="json"
            )],
        )
    )
    pattern_snapshot = await evaluate_patterns(settings, pattern_req, folder / "pattern")

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
            reporting_scope=source.company.reporting_scope, profile_id=profile_id,
            classification=dict(
                classification="BANK", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            selections=[
                dict(method="PRICE_TO_EARNINGS", price=pin("current_price"),
                     denominator=pin("eps", True)),
                dict(method="PRICE_TO_BOOK", price=pin("current_price"),
                     denominator=pin("book_value")),
            ],
        )
    )
    valuation_snapshot = await evaluate_valuation(settings, val_req, folder / "valuation")

    presentation = await ingest_document(
        settings,
        DocumentRequest(
            company_id=source.company.company_id,
            url="https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/"
            "19072025/188b8c46-9a03-4edb-921c-af9cadb1cd84.pdf",
            document_type=DocumentType.INVESTOR_PRESENTATION,
            title="YES BANK Investor Presentation",
            replay_path=FIX / "documents/yesbank_presentation.pdf",
        ),
        folder / "presentation",
    )
    transcript = await ingest_document(
        settings,
        DocumentRequest(
            company_id=source.company.company_id,
            url="https://bsmedia.business-standard.com/_media/bs/data/announcements/bse/"
            "22012026/847c7cb1-c4cd-436f-aeae-37a72c9bacb1.pdf",
            document_type=DocumentType.CONCALL_TRANSCRIPT,
            title="YES BANK Concall Transcript",
            replay_path=FIX / "documents/yesbank_transcript.pdf",
        ),
        folder / "transcript",
    )

    request = AnalysisRequest.model_validate(
        dict(
            company_id=str(source.company.company_id),
            classification=dict(
                classification="BANK", basis="CALLER_DECLARED",
                declaration="Explicit proof classification",
            ),
            profile_id=profile_id, financial_extraction_id=str(source.metadata.id),
            sector_kpi_snapshot_ids=(str(kpi_snapshot.id),),
            pattern_snapshot_ids=(str(pattern_snapshot.id),),
            valuation_snapshot_ids=(str(valuation_snapshot.id),),
            document_chunk_refs=(
                (presentation.version_id, presentation.chunks[0].id),
                (transcript.version_id, transcript.chunks[0].id),
            ),
            sections=(
                "SECTOR_KPIS", "VALUATION", "MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
            ),
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-part6a-real-proof", template_version=1)
    bundle, evidence = await build_bundle_and_evidence(settings, request)
    growth_ref = next(r for r in bundle.sources if r.kind == "SECTOR_KPI")

    def _valuation_method(ref):
        v = evidence.valuations[ref.owning_snapshot_id]
        return next(r.definition.method for r in v.results if str(r.id) == ref.item_key)

    valuation_refs = [r for r in bundle.sources if r.kind == "VALUATION"]
    pe_ref = next(r for r in valuation_refs if _valuation_method(r) == "PRICE_TO_EARNINGS")
    pb_ref = next(r for r in valuation_refs if _valuation_method(r) == "PRICE_TO_BOOK")
    # This is the exact frozen Part 5A rule this real proof must NOT silently bypass: a
    # PRICE_TO_BOOK denominator whose BVPS semantic mapping is not VERIFIED is refused
    # outright (application/valuation.py::validate_fact, "UNVERIFIED_" + item.semantic ->
    # status UNVERIFIED_METRIC_SEMANTICS) - real YESBANK book_value has no VERIFIED BVPS
    # mapping, so this MUST still be refused here. If it is not, Part 5A semantics have
    # been silently weakened and this proof must fail loudly rather than paper over it.
    pb_status = analyst_engine._reference_status(pb_ref, evidence)
    if pb_status != "UNVERIFIED_METRIC_SEMANTICS":
        raise RuntimeError(
            f"Expected real YESBANK PRICE_TO_BOOK to remain refused with "
            f"UNVERIFIED_METRIC_SEMANTICS (frozen Part 5A BVPS-verification rule); got "
            f"{pb_status}. Part 6A must never convert a refused valuation into an observed "
            "one, and this proof must never claim P/B is observed unless it genuinely is."
        )
    presentation_chunk_ref = next(
        r for r in bundle.sources
        if r.kind == "DOCUMENT_CHUNK" and r.owning_snapshot_id == presentation.version_id
    )
    transcript_chunk_ref = next(
        r for r in bundle.sources
        if r.kind == "DOCUMENT_CHUNK" and r.owning_snapshot_id == transcript.version_id
    )
    presentation_chunk_text = next(
        c.text for c in presentation.chunks if str(c.id) == presentation_chunk_ref.item_key
    )
    transcript_chunk_text = next(
        c.text for c in transcript.chunks if str(c.id) == transcript_chunk_ref.item_key
    )
    claims = [
        dict(section="SECTOR_KPIS", statement="Deposit growth evidence is cited on its own.",
             materiality="PRIMARY", citations=[citation(growth_ref)]),
        dict(section="VALUATION", statement="Observed P/E valuation evidence is cited.",
             materiality="PRIMARY", citations=[citation(pe_ref)]),
        dict(
            section="MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
            statement="The investor presentation contains this exact retained passage.",
            materiality="SUPPORTING", citations=[citation(presentation_chunk_ref)],
            management_quote=dict(chunk=citation(presentation_chunk_ref),
                                   text=presentation_chunk_text),
        ),
        dict(
            section="MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
            statement="The concall transcript contains this exact retained passage.",
            materiality="SUPPORTING", citations=[citation(transcript_chunk_ref)],
            management_quote=dict(chunk=citation(transcript_chunk_ref),
                                   text=transcript_chunk_text),
        ),
    ]
    analysis = await analyze_company(
        settings, request, execution, folder / "analysis",
        model=FakeAnalystModel(claims=claims), provider_kind="FAKE",
    )
    challenge = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), folder / "challenge"
    )
    report = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), folder / "report"
    )

    if len(report.document_corpus.entries) != 2:
        raise RuntimeError(
            f"YESBANK real proof expected 2 pinned documents, got "
            f"{len(report.document_corpus.entries)}"
        )
    valuation_section = next(s for s in report.sections if s.section == "VALUATION")
    if any(m.label.upper().find("FCFF") >= 0 for t in valuation_section.tables for m, in t.rows):
        raise RuntimeError("YESBANK real proof must never show an FCFF row (never requested)")
    deep_mgmt = next(
        s for s in report.sections if s.section == "DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE"
    )
    attributions = {st.quote_attribution for st in deep_mgmt.statements if st.quote_attribution}
    if "TRANSCRIPT_STATEMENT" not in attributions:
        raise RuntimeError("Expected at least one TRANSCRIPT_STATEMENT from the concall")
    concall_section = next(s for s in report.sections if s.section == "CONCALL_INTELLIGENCE")
    if concall_section.coverage.status != "AVAILABLE":
        raise RuntimeError("Expected CONCALL_INTELLIGENCE to be AVAILABLE for real YESBANK proof")
    if any(st.quote_attribution == "MANAGEMENT_STATEMENT" for st in concall_section.statements):
        raise RuntimeError("A concall-sourced statement was mislabeled MANAGEMENT_STATEMENT")
    presentation_section = next(
        s for s in report.sections if s.section == "INVESTOR_PRESENTATION_INTELLIGENCE"
    )
    if presentation_section.coverage.status != "AVAILABLE":
        raise RuntimeError("Expected INVESTOR_PRESENTATION_INTELLIGENCE to be AVAILABLE")

    # Part 6A must render the frozen Part 5A refusal honestly in the deterministic table -
    # a None display_value paired with UNVERIFIED_METRIC_SEMANTICS, never a fabricated
    # multiple - and must never itself compute a P/B value.
    pb_rows = [
        m for t in valuation_section.tables for (m,) in t.rows if m.label == "PRICE_TO_BOOK"
    ]
    if len(pb_rows) != 1 or pb_rows[0].status != "UNVERIFIED_METRIC_SEMANTICS":
        raise RuntimeError(
            f"Expected exactly one rendered PRICE_TO_BOOK row with status "
            f"UNVERIFIED_METRIC_SEMANTICS, got {pb_rows}"
        )
    if pb_rows[0].display_value is not None:
        raise RuntimeError(
            f"Part 6A must never display a value for a refused PRICE_TO_BOOK row; got "
            f"{pb_rows[0].display_value!r}"
        )
    pe_rows = [
        m for t in valuation_section.tables for (m,) in t.rows if m.label == "PRICE_TO_EARNINGS"
    ]
    if len(pe_rows) != 1 or pe_rows[0].status != "AVAILABLE" or pe_rows[0].display_value is None:
        raise RuntimeError(f"Expected a genuinely observed PRICE_TO_EARNINGS row, got {pe_rows}")

    return {
        "symbol": "YESBANK", "track": "REAL_RETAINED_EVIDENCE",
        "report_id": str(report.id),
        "research_completeness": report.synthesis.research_completeness,
        "valuation_evidence_state": report.synthesis.valuation_evidence_state,
        "price_to_earnings_status": pe_rows[0].status,
        "price_to_book_status": pb_rows[0].status,
        "document_corpus_entries": len(report.document_corpus.entries),
        "concall_attribution_neutral": True,
        "investor_presentation_available": True,
    }


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
                dict(metric_id="eps_growth", formula_id="SOURCE_METRIC_GROWTH",
                     source_metric="eps", current_period="2025-03", comparison="ANNUAL_YOY")
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
            profile_id="IT_SERVICES_PROFILE", financial_extraction_id=str(source.metadata.id),
            calculation_snapshot_ids=(str(calc_snapshot.id),),
            sections=("HISTORICAL_FUNDAMENTALS",),
        )
    )
    execution = AnalystExecutionSpec(template_id="v1-part6a-synthetic", template_version=1)
    bundle, _ = await build_bundle_and_evidence(settings, request)
    calc_ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")
    claim = dict(
        section="HISTORICAL_FUNDAMENTALS", statement="EPS growth evidence is cited.",
        materiality="PRIMARY", citations=[citation(calc_ref)],
    )
    analysis = await analyze_company(
        settings, request, execution, folder / "analysis",
        model=FakeAnalystModel(claims=[claim]), provider_kind="FAKE",
    )
    challenge = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), folder / "challenge"
    )

    narrator = FakeReportNarrator(
        blocks=[
            {"section": "HISTORICAL_FUNDAMENTALS",
             "text": "We recommend investors buy the stock immediately."},
        ]
    )
    narrated = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), folder / "report-narrated",
        execution=ReportExecutionSpec(template_id="synthetic-narrator", template_version=1),
        narrator=narrator, provider_kind="FAKE",
    )
    if not narrated.rejected_narrative_blocks:
        raise RuntimeError("Expected the forbidden-language narrator block to be rejected")
    if any(
        "buy the stock" in st.text.lower()
        for s in narrated.sections for st in s.statements if st.origin == "NARRATOR"
    ):
        raise RuntimeError("Forbidden narrator language leaked into the persisted report")

    first = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), folder / "report1"
    )
    second = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), folder / "report2"
    )
    if first.id == second.id:
        raise RuntimeError("Expected two independent report revisions")
    database = SQLiteDatabase(settings.database_path)
    try:
        history = await database.reports.report_history(challenge.id)
        if len(history) != 3 or first not in history or second not in history:
            raise RuntimeError("Report history is not honestly append-only/reconstructable")
    finally:
        await database.close()

    for artifact in source.raw_artifacts:
        (settings.artifact_path / artifact.content_reference).write_bytes(b"tampered")
    blocked = False
    try:
        await generate_report(
            settings, ReportRequest(challenge_snapshot_id=challenge.id), folder / "report-blocked"
        )
    except ReportIntegrityError:
        blocked = True
    if not blocked:
        raise RuntimeError("Expected raw-artifact corruption to block report generation")

    return {
        "track": "SYNTHETIC_MECHANICAL_PROOF",
        "narrator_rejected_blocks": len(narrated.rejected_narrative_blocks),
        "report_revisions": 3,
        "raw_corruption_blocked_generation": True,
    }


async def main(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    infy = await run_infy(output)
    yesbank = await run_yesbank(output)
    synthetic = await run_synthetic(output)
    report = {"track1_real": [infy, yesbank], "track2_synthetic": synthetic}
    (output / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print("\nPart 6A offline proof PASSED.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.output))
