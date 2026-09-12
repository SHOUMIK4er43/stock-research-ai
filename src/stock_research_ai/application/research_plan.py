"""FULL_RESEARCH_V1: the server-owned, versioned orchestration plan a Part 6B browser job
will eventually request by name only. This module owns sequencing and evidence-selection
glue; it introduces no new financial/valuation/analysis/challenge/report semantics and
calls only existing ResearchEngine/application operations.

Explicitly NOT attempted in this V1 (see docs/part6b0-proof.md for the full rationale -
each is a deliberate scope boundary, not a swallowed failure):
  - calculate_financials / evaluate_patterns: a correct, general, sector-agnostic
    request (growth metric list, pattern selection) cannot be constructed without
    company/sector-specific knowledge this milestone did not audit. Part 5B still
    receives substantial raw FINANCIAL evidence directly from the extraction.
  - market/context (Kite/publication/macro) acquisition: requires per-company
    instrument/series identifiers Screener evidence alone does not provide generically.
Both remain reachable by hand today (existing CLI commands) exactly as before; a future
milestone may wire them into FULL_RESEARCH_V1 once specifically audited.
"""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from stock_research_ai.application.calculations import InputFailure, period_key
from stock_research_ai.application.document_topics import (
    DOCUMENT_RESEARCH_TOPICS_V1,
    match_document_topics,
)
from stock_research_ai.application.engine import ResearchEngine
from stock_research_ai.application.ports import BroadIndustrySource, RawArtifactStore
from stock_research_ai.application.sector_kpis import PROFILES
from stock_research_ai.application.sector_resolution import SectorResolution, resolve_sector
from stock_research_ai.domain.analysis import AnalysisRequest, AnalysisSection, AnalystExecutionSpec
from stock_research_ai.domain.calculations import AlignmentPolicy
from stock_research_ai.domain.challenge import ChallengeRequest
from stock_research_ai.domain.documents import (
    DocumentReference,
    DocumentRequest,
    DocumentType,
    ParsedDocument,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest
from stock_research_ai.domain.financial import FinancialObservation, PeriodFrequency
from stock_research_ai.domain.provenance import RawArtifact
from stock_research_ai.domain.report import ReportRequest
from stock_research_ai.domain.sector_kpis import ProfileClassification, SectorKPIRequest
from stock_research_ai.domain.valuation import EvidencePin, ValuationRequest, ValuationSelection

ResearchPlanId = Literal["FULL_RESEARCH_V1"]
ProgressStage = Literal["PREPARING", "EVIDENCE", "ANALYSIS", "CHALLENGE", "REPORT", "COMPLETED"]

#: Recency-bounded, per-DocumentType V1 caps. QUARTERLY_RESULT and AGM have no distinct
#: DocumentType (confirmed limitation, see docs/part6b0-proof.md) and fall under
#: ANNOUNCEMENT/OTHER, which stay strictly small rather than widening frozen semantics.
DOCUMENT_INGESTION_CAPS: dict[DocumentType, int] = {
    DocumentType.ANNUAL_REPORT: 3,
    DocumentType.INVESTOR_PRESENTATION: 4,
    DocumentType.CONCALL_TRANSCRIPT: 4,
    DocumentType.EARNINGS_PRESENTATION: 4,
    DocumentType.CREDIT_RATING: 2,
    DocumentType.ANNOUNCEMENT: 2,
    DocumentType.OTHER: 2,
}

#: Sections always requested when their underlying evidence kind is present at all - raw
#: financial/shareholding evidence from the extraction is unconditionally available once
#: extract_company succeeds, independent of any calculation/valuation/document step.
_BASE_SECTIONS: tuple[AnalysisSection, ...] = (
    "COMPANY_OVERVIEW",
    "HISTORICAL_FUNDAMENTALS",
    "BALANCE_SHEET",
    "OWNERSHIP_AND_SHAREHOLDING",
)


class ProgressSink(Protocol):
    def on_stage_change(self, stage: ProgressStage) -> None: ...


class FullResearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_url: str
    research_plan: ResearchPlanId = "FULL_RESEARCH_V1"


class FullResearchWarning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    stage: ProgressStage
    message: str


class FullResearchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    company_id: UUID
    extraction_id: UUID
    sector_classification: ProfileClassification
    document_versions_ingested: tuple[UUID, ...]
    document_chunks_selected: int
    analysis_snapshot_id: UUID
    challenge_snapshot_id: UUID
    report_snapshot_id: UUID
    stage_warnings: tuple[FullResearchWarning, ...]


def _notify(progress: ProgressSink | None, stage: ProgressStage) -> None:
    if progress is not None:
        progress.on_stage_change(stage)


def _select_documents_for_ingestion(
    references: tuple[DocumentReference, ...],
) -> tuple[tuple[DocumentReference, ...], tuple[FullResearchWarning, ...]]:
    """Preferred ordering: publication date descending, then discovery order as a
    fallback for anything undated. Never labeled "most recent" when date metadata is
    absent - a DOCUMENT_RECENCY_UNVERIFIED warning is recorded instead."""
    warnings: list[FullResearchWarning] = []
    by_type: dict[DocumentType, list[DocumentReference]] = {}
    for ref in references:
        by_type.setdefault(ref.document_type, []).append(ref)
    selected: list[DocumentReference] = []
    for doc_type, refs in by_type.items():
        cap = DOCUMENT_INGESTION_CAPS.get(doc_type, 0)
        if cap == 0:
            continue
        dated = sorted(
            (r for r in refs if r.publication_at is not None),
            key=lambda r: r.publication_at,  # type: ignore[arg-type,return-value]
            reverse=True,
        )
        undated = [r for r in refs if r.publication_at is None]
        ordered = (dated + undated)[:cap]
        if any(r.publication_at is None for r in ordered):
            warnings.append(
                FullResearchWarning(
                    code="DOCUMENT_RECENCY_UNVERIFIED",
                    stage="EVIDENCE",
                    message=(
                        f"{doc_type.value}: publication date unavailable for one or more "
                        "selected documents; selection used deterministic discovery order, "
                        "not verified recency, for those documents."
                    ),
                )
            )
        selected.extend(ordered)
    return tuple(selected), tuple(warnings)


async def _ingest_documents(
    engine: ResearchEngine, company_id: UUID, references: tuple[DocumentReference, ...]
) -> tuple[list[ParsedDocument], list[FullResearchWarning]]:
    ingested: list[ParsedDocument] = []
    warnings: list[FullResearchWarning] = []
    for ref in references:
        try:
            request = DocumentRequest(
                company_id=company_id,
                url=ref.link.resolved_url,
                document_type=ref.document_type,
                title=ref.title,
                reference=ref,
                publication_date=ref.publication_at.date() if ref.publication_at else None,
            )
            ingested.append(await engine.ingest_document(request))
        except Exception as exc:  # a single bad document must not abort the run
            warnings.append(
                FullResearchWarning(
                    code="DOCUMENT_INGESTION_FAILED",
                    stage="EVIDENCE",
                    message=f"{ref.link.resolved_url}: {type(exc).__name__}: {exc}",
                )
            )
    return ingested, warnings


def _period_key_safe(observation: FinancialObservation) -> str | None:
    """period_key (application.calculations, frozen/reused unchanged) reads the real
    Screener fiscal_label convention ("Mar 2025") into the "YYYY-MM" form every other
    period-addressed request in this codebase already uses - it is NOT the same string
    as fiscal_label itself. Never invents a period; a conflicting/unparseable label is
    simply treated as unusable here, exactly as the rest of the codebase already does."""
    try:
        return period_key(observation)
    except InputFailure:
        return None


def _annual_period_and_alignment(source: CompanyExtractionResult) -> tuple[str, int] | None:
    keys = [
        key
        for o in source.profit_loss
        if o.period.frequency == PeriodFrequency.ANNUAL
        for key in (_period_key_safe(o),)
        if key is not None
    ]
    if not keys:
        return None
    label = max(keys)
    return label, int(label[5:7])


async def _attempt_sector_kpis(
    engine: ResearchEngine, source: CompanyExtractionResult, sector: SectorResolution
) -> tuple[UUID | None, list[FullResearchWarning]]:
    if sector.resolution_status != "MAPPED" or sector.profile_id is None:
        return None, [
            FullResearchWarning(
                code="SECTOR_KPI_NOT_ATTEMPTED_UNRESOLVED_PROFILE",
                stage="EVIDENCE",
                message="Sector profile did not resolve to a known profile; sector KPI "
                "calculation was not attempted.",
            )
        ]
    profile = next((p for p in PROFILES if p.profile_id == sector.profile_id), None)
    if profile is None or not profile.supported_kpis:
        return None, [
            FullResearchWarning(
                code="SECTOR_KPI_NOT_ATTEMPTED_NO_SUPPORTED_KPIS",
                stage="EVIDENCE",
                message=f"No supported KPIs registered for profile {sector.profile_id!r}.",
            )
        ]
    period = _annual_period_and_alignment(source)
    if period is None:
        return None, [
            FullResearchWarning(
                code="SECTOR_KPI_NOT_ATTEMPTED_NO_ANNUAL_PERIOD",
                stage="EVIDENCE",
                message="No usable annual profit_loss period was found to anchor sector "
                "KPI evaluation.",
            )
        ]
    current_period, fiscal_year_end_month = period
    try:
        assert source.company is not None
        request = SectorKPIRequest(
            company_id=source.company.company_id,
            extraction_id=source.metadata.id,
            reporting_scope=source.company.reporting_scope,
            classification=sector.declaration,
            profile_id=sector.profile_id,
            profile_version=1,
            alignment=AlignmentPolicy(
                fiscal_year_end_month=fiscal_year_end_month,
                declaration=(
                    "Derived from the most recent annual profit_loss period observed in "
                    "this extraction."
                ),
            ),
            current_period=current_period,
            comparison="ANNUAL_YOY",
            kpis=tuple(profile.supported_kpis),
        )
        snapshot = await engine.calculate_sector_kpis(request)
        return snapshot.id, []
    except Exception as exc:
        return None, [
            FullResearchWarning(
                code="SECTOR_KPI_FAILED", stage="EVIDENCE", message=f"{type(exc).__name__}: {exc}"
            )
        ]


def _financial_pin(extraction_id: UUID, observation_id: UUID) -> EvidencePin:
    return EvidencePin(kind="FINANCIAL", snapshot_id=extraction_id, observation_id=observation_id)


async def _attempt_valuation(
    engine: ResearchEngine, source: CompanyExtractionResult, sector: SectorResolution
) -> tuple[UUID | None, list[FullResearchWarning]]:
    """Observation-only: PRICE_TO_EARNINGS/PRICE_TO_BOOK from already-pinned FINANCIAL
    evidence only. No scenario is ever constructed - there is no caller-declared
    assumption source in an unattended run. Note: the existing, frozen Part 5A gate
    (application/valuation.py) additionally requires profile_id to resolve to one of the
    two currently-implemented profiles (BANK_PROFILE/IT_SERVICES_PROFILE); for any other
    company this correctly and honestly returns NOT_APPLICABLE, not an invented value."""
    price = next((o for o in source.top_metrics if o.metric_id == "current_price"), None)
    book_value = next((o for o in source.top_metrics if o.metric_id == "book_value"), None)
    eps_keyed = [
        (key, o)
        for o in source.profit_loss
        if o.metric_id == "eps" and o.period.frequency == PeriodFrequency.ANNUAL
        for key in (_period_key_safe(o),)
        if key is not None
    ]
    eps = max(eps_keyed, key=lambda pair: pair[0])[1] if eps_keyed else None
    selections: list[ValuationSelection] = []
    if price is not None and eps is not None:
        selections.append(
            ValuationSelection(
                method="PRICE_TO_EARNINGS",
                price=_financial_pin(source.metadata.id, price.id),
                denominator=_financial_pin(source.metadata.id, eps.id),
            )
        )
    if price is not None and book_value is not None:
        selections.append(
            ValuationSelection(
                method="PRICE_TO_BOOK",
                price=_financial_pin(source.metadata.id, price.id),
                denominator=_financial_pin(source.metadata.id, book_value.id),
            )
        )
    if not selections:
        return None, [
            FullResearchWarning(
                code="VALUATION_NOT_ATTEMPTED_MISSING_INPUTS",
                stage="EVIDENCE",
                message="current_price/eps/book_value observations were not all present; "
                "no valuation was attempted.",
            )
        ]
    try:
        assert source.company is not None
        request = ValuationRequest(
            company_id=source.company.company_id,
            financial_extraction_id=source.metadata.id,
            reporting_scope=source.company.reporting_scope,
            profile_id=sector.profile_id or "UNRESOLVED_V1",
            classification=sector.declaration,
            selections=tuple(selections),
        )
        snapshot = await engine.evaluate_valuation(request)
        return snapshot.id, []
    except Exception as exc:
        return None, [
            FullResearchWarning(
                code="VALUATION_FAILED", stage="EVIDENCE", message=f"{type(exc).__name__}: {exc}"
            )
        ]


def _sections_for(
    document_chunk_refs: tuple[tuple[UUID, UUID], ...],
    valuation_snapshot_id: UUID | None,
    sector_kpi_snapshot_id: UUID | None,
) -> tuple[AnalysisSection, ...]:
    sections: list[AnalysisSection] = list(_BASE_SECTIONS)
    if valuation_snapshot_id is not None:
        sections.append("VALUATION")
    if sector_kpi_snapshot_id is not None:
        sections.append("SECTOR_KPIS")
    if document_chunk_refs:
        # Any selected chunk means at least one document-shaped section is supportable;
        # include every registered topic's section that FULL_RESEARCH_V1 attempted -
        # coverage/completeness rules (frozen, Part 6A) already decide what to actually
        # show if a section ends up uncited.
        topic_sections = {topic.section for topic in DOCUMENT_RESEARCH_TOPICS_V1}
        sections.extend(topic_sections)
    return tuple(dict.fromkeys(sections))


async def run_full_research(
    engine: ResearchEngine,
    artifacts: RawArtifactStore,
    broad_industry: BroadIndustrySource,
    request: FullResearchRequest,
    execution: AnalystExecutionSpec,
    progress: ProgressSink | None = None,
) -> FullResearchResult:
    warnings: list[FullResearchWarning] = []

    _notify(progress, "PREPARING")
    extraction = await engine.extract_company(
        ExtractionRequest(provider="screener", url=request.source_url)
    )
    if extraction.company is None:
        raise ValueError(
            f"Screener acquisition did not yield a usable company: {request.source_url}"
        )
    company_id = extraction.company.company_id

    artifact_id = extraction.company.provenance.raw_artifact_id
    company_artifact: RawArtifact = next(a for a in extraction.raw_artifacts if a.id == artifact_id)
    content = await artifacts.read(company_artifact)
    sector = resolve_sector(broad_industry, company_artifact, content)

    _notify(progress, "EVIDENCE")
    to_ingest, ordering_warnings = _select_documents_for_ingestion(extraction.documents)
    warnings.extend(ordering_warnings)
    ingested_documents, ingestion_warnings = await _ingest_documents(engine, company_id, to_ingest)
    warnings.extend(ingestion_warnings)

    selected_chunks = match_document_topics(tuple(ingested_documents))
    document_chunk_refs = tuple(
        (chunk.document_version_id, chunk.chunk_id) for chunk in selected_chunks
    )

    sector_kpi_snapshot_id, kpi_warnings = await _attempt_sector_kpis(engine, extraction, sector)
    warnings.extend(kpi_warnings)

    valuation_snapshot_id, valuation_warnings = await _attempt_valuation(engine, extraction, sector)
    warnings.extend(valuation_warnings)

    warnings.append(
        FullResearchWarning(
            code="CALCULATIONS_NOT_ATTEMPTED_V1_SCOPE",
            stage="EVIDENCE",
            message="calculate_financials/evaluate_patterns/market/context acquisition are "
            "not yet wired into FULL_RESEARCH_V1 (see application/research_plan.py module "
            "docstring); raw FINANCIAL evidence from the extraction remains fully available.",
        )
    )

    analysis_request = AnalysisRequest(
        company_id=company_id,
        classification=sector.declaration,
        profile_id=sector.profile_id or "UNRESOLVED_V1",
        financial_extraction_id=extraction.metadata.id,
        sector_kpi_snapshot_ids=(sector_kpi_snapshot_id,) if sector_kpi_snapshot_id else (),
        valuation_snapshot_ids=(valuation_snapshot_id,) if valuation_snapshot_id else (),
        document_chunk_refs=document_chunk_refs,
        sections=_sections_for(document_chunk_refs, valuation_snapshot_id, sector_kpi_snapshot_id),
    )

    _notify(progress, "ANALYSIS")
    analysis = await engine.analyze_company(analysis_request, execution)

    _notify(progress, "CHALLENGE")
    challenge = await engine.challenge_analysis(ChallengeRequest(analysis_snapshot_id=analysis.id))

    _notify(progress, "REPORT")
    report = await engine.generate_report(ReportRequest(challenge_snapshot_id=challenge.id))

    _notify(progress, "COMPLETED")
    return FullResearchResult(
        company_id=company_id,
        extraction_id=extraction.metadata.id,
        sector_classification=sector.declaration,
        document_versions_ingested=tuple(d.version_id for d in ingested_documents),
        document_chunks_selected=len(selected_chunks),
        analysis_snapshot_id=analysis.id,
        challenge_snapshot_id=challenge.id,
        report_snapshot_id=report.id,
        stage_warnings=tuple(warnings),
    )
