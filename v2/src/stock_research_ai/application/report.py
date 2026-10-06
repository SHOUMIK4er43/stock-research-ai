"""Deterministic Part 6A report assembler.

Owns: input pinning, section construction, deterministic tables, coverage
projection, data-gap aggregation, challenge integration, document-corpus
projection, executive-snapshot structure, static monitoring, appendices.

Zero arithmetic happens here. Every displayed number is re-read verbatim from an
already-canonical upstream value via evidence_matching's own accessor functions -
the same functions Part 5B's own validator and Part 5C's own challenger already
use. A valid report exists even with zero narrator involvement.
"""

from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from stock_research_ai.application.analysis import (
    PRIMARY_KINDS,
    SUPPORTING_KINDS,
    ResolvedEvidence,
    _reference_status,
    direct_raw_artifacts,
    resolve_evidence,
    transitive_raw_artifacts,
)
from stock_research_ai.application.evidence_matching import canonical_values, citation_period_scope
from stock_research_ai.application.ports import (
    CalculationRepository,
    ContextRepository,
    DocumentRepository,
    ExtractionRepository,
    MarketCalculationRepository,
    MarketRepository,
    PatternRepository,
    RawArtifactStore,
    ValuationRepository,
)
from stock_research_ai.domain.analysis import (
    POINT_IN_TIME_ISSUE_CODE,
    AnalysisSection,
    AnalysisSnapshot,
    AnalystClaim,
    EvidenceReference,
    SectionCoverageStatus,
)
from stock_research_ai.domain.challenge import ChallengeSnapshot, ClaimReview
from stock_research_ai.domain.company import Company
from stock_research_ai.domain.documents import DocumentType
from stock_research_ai.domain.report import (
    DocumentCorpus,
    DocumentCorpusEntry,
    ExecutiveResearchSnapshot,
    MonitoringEntry,
    ReportLimitation,
    ReportMetric,
    ReportQuoteAttribution,
    ReportSection,
    ReportSectionCoverage,
    ReportSectionId,
    ReportStatement,
    ReportSynthesis,
    ReportTable,
    ResearchCompleteness,
    ValuationEvidenceState,
)


class ReportIntegrityError(ValueError):
    """Closed-report re-resolution/persistence integrity failures - distinct from
    a plain malformed-request ValueError so the CLI maps it to its own exit code."""


_ANALYSIS_SECTION_MAP: dict[ReportSectionId, AnalysisSection] = {
    "BUSINESS_MODEL": "BUSINESS_MODEL",
    "ANNUAL_FINANCIAL_PERFORMANCE": "HISTORICAL_FUNDAMENTALS",
    "QUARTERLY_PERFORMANCE_AND_INFLECTION": "QUARTERLY_INFLECTIONS",
    "BALANCE_SHEET_ANALYSIS": "BALANCE_SHEET",
    "CASH_FLOW_AND_EARNINGS_QUALITY": "CASH_FLOW_QUALITY",
    "SECTOR_SPECIFIC_KPI_ANALYSIS": "SECTOR_KPIS",
    "DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE": "MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
    "OWNERSHIP_AND_SHAREHOLDING": "OWNERSHIP_AND_SHAREHOLDING",
    "MACRO_POLICY_REGULATION": "INDUSTRY_AND_MACRO",
    "MARKET_AND_PRICE_ANALYSIS": "MARKET_PERFORMANCE",
    "TECHNICAL_ANALYSIS": "TECHNICAL_STATE",
    "VALUATION": "VALUATION",
    "MARKET_IMPLIED_EXPECTATIONS": "EXPECTATIONS",
    "CATALYSTS": "CATALYSTS",
    "RISK_REGISTER": "RISKS",
    "BULL_BASE_BEAR_SCENARIOS": "BULL_BASE_BEAR_INTERPRETATION",
    "VARIANT_PERCEPTION": "VARIANT_PERCEPTION",
    "FORENSIC_AND_QUALITY_REVIEW": "FORENSIC_REVIEW",
}

_TABLE_TITLES: dict[ReportSectionId, str] = {
    "ANNUAL_FINANCIAL_PERFORMANCE": "Annual Financial Performance",
    "QUARTERLY_PERFORMANCE_AND_INFLECTION": "Quarterly Performance",
    "BALANCE_SHEET_ANALYSIS": "Balance Sheet Evidence",
    "CASH_FLOW_AND_EARNINGS_QUALITY": "Cash Flow & Earnings Quality Evidence",
    "SECTOR_SPECIFIC_KPI_ANALYSIS": "Sector KPI Evidence",
    "MARKET_AND_PRICE_ANALYSIS": "Market Evidence",
    "TECHNICAL_ANALYSIS": "Technical Indicator Evidence",
    "VALUATION": "Valuation Evidence",
    "MARKET_IMPLIED_EXPECTATIONS": "Market-Implied Expectation Evidence",
    "FORENSIC_AND_QUALITY_REVIEW": "Forensic Pattern Evidence",
    "OWNERSHIP_AND_SHAREHOLDING": "Shareholding Evidence",
}

_SINGLE_VOICE_DOCUMENT_TYPES = frozenset(
    {
        DocumentType.ANNUAL_REPORT,
        DocumentType.INVESTOR_PRESENTATION,
        DocumentType.EARNINGS_PRESENTATION,
        DocumentType.ANNOUNCEMENT,
    }
)

_DOCUMENT_FILTER_SECTIONS: dict[ReportSectionId, tuple[DocumentType, ...]] = {
    "CONCALL_INTELLIGENCE": (DocumentType.CONCALL_TRANSCRIPT,),
    "ANNUAL_REPORT_INTELLIGENCE": (DocumentType.ANNUAL_REPORT,),
    "INVESTOR_PRESENTATION_INTELLIGENCE": (
        DocumentType.INVESTOR_PRESENTATION,
        DocumentType.EARNINGS_PRESENTATION,
    ),
}


def quote_attribution(document_type: DocumentType) -> ReportQuoteAttribution:
    """CONCALL_TRANSCRIPT (and any type this codebase cannot structurally prove is
    single-voice) always renders neutral - never inferred from prose or position."""
    return "MANAGEMENT_STATEMENT" if document_type in _SINGLE_VOICE_DOCUMENT_TYPES else (
        "TRANSCRIPT_STATEMENT"
    )


def _label_for_reference(ref: EvidenceReference, evidence: ResolvedEvidence) -> str:
    if ref.kind == "COMPANY":
        return "Company"
    if ref.kind in ("FINANCIAL", "SHAREHOLDING"):
        f = evidence.financial
        assert f is not None
        for section in (f.top_metrics, f.profit_loss, f.quarterly, f.balance_sheet, f.cashflow,
                        f.ratios):
            for o in section:
                if str(o.id) == ref.item_key:
                    return o.metric
        for holding in f.shareholding.holdings:
            if str(holding.observation.id) == ref.item_key:
                return f"{holding.category} holding"
        for count in f.shareholding.shareholder_counts:
            if str(count.id) == ref.item_key:
                return count.metric
        return "Financial observation"
    if ref.kind == "MARKET":
        return "Market close"
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI", "MARKET_CALCULATION", "MACRO"):
        return ref.item_key or ref.kind
    if ref.kind == "PATTERN":
        p = evidence.patterns[ref.owning_snapshot_id]
        for finding in p.findings:
            if str(finding.id) == ref.item_key:
                return finding.definition.pattern_id
        return "Pattern"
    if ref.kind == "TECHNICAL":
        t = evidence.technicals[ref.owning_snapshot_id]
        for series in t.series:
            for point in series.points:
                if str(point.id) == ref.item_key:
                    return series.definition.indicator_id
        return "Technical indicator"
    if ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for r in v.results:
            if str(r.id) == ref.item_key:
                return r.definition.method
        return "Valuation result"
    if ref.kind == "DOCUMENT_CHUNK":
        return "Document chunk"
    if ref.kind == "PUBLICATION":
        return "Publication passage"
    return ref.kind


def _metric_from_reference(ref: EvidenceReference, evidence: ResolvedEvidence) -> ReportMetric:
    values = canonical_values(ref, evidence)
    value, unit = values[0] if values else (None, None)
    period, scope = citation_period_scope(ref, evidence)
    return ReportMetric(
        label=_label_for_reference(ref, evidence),
        display_value=str(value) if value is not None else None,
        unit=unit,
        period=period,
        reporting_scope=str(scope) if scope is not None else None,
        status=_reference_status(ref, evidence),
        evidence_ref=ref,
    )


def _claim_report_statements(
    claim: AnalystClaim, review: ClaimReview, section_id: ReportSectionId
) -> tuple[ReportStatement, ...]:
    if review.outcome == "INVALID_AGAINST_BUNDLE":
        return ()
    limitations = list(claim.limitations)
    if review.outcome == "UNVERIFIABLE_FROM_BUNDLE":
        limitations.append("NOT_INDEPENDENTLY_VERIFIABLE_FROM_BUNDLE")
    statements = [
        ReportStatement(
            statement_id=uuid4(),
            section=section_id,
            text=claim.statement,
            epistemic_class="INTERPRETATION",
            origin="DETERMINISTIC",
            source_claim_ids=(claim.claim_id,),
            challenge_finding_ids=tuple(f.finding_id for f in review.findings),
            challenge_outcome=review.outcome,
            limitations=tuple(sorted(set(limitations))),
        )
    ]
    for finding in review.findings:
        statements.append(
            ReportStatement(
                statement_id=uuid4(),
                section=section_id,
                text=finding.statement,
                epistemic_class="INTERPRETATION",
                origin="DETERMINISTIC",
                challenge_finding_ids=(finding.finding_id,),
                evidence_refs=finding.cited_evidence,
                challenge_outcome=review.outcome,
            )
        )
    return tuple(statements)


def _placeholder_section(
    section_id: ReportSectionId, status: str, reason: str, reason_code: str | None
) -> ReportSection:
    return ReportSection(
        section=section_id,
        coverage=ReportSectionCoverage(status=status, reason=reason, reason_code=reason_code),  # type: ignore[arg-type]
    )


def _build_mapped_section(
    section_id: ReportSectionId,
    analysis_section: AnalysisSection,
    reviewed: AnalysisSnapshot,
    evidence: ResolvedEvidence,
    reviews_by_claim: dict[UUID, ClaimReview],
) -> ReportSection:
    coverage_entry = next(
        (c for c in reviewed.section_coverage if c.section == analysis_section), None
    )
    coverage = (
        ReportSectionCoverage(
            status=coverage_entry.status,
            reason=coverage_entry.reason,
            contributing_evidence_kinds=coverage_entry.contributing_evidence_kinds,
        )
        if coverage_entry is not None
        else ReportSectionCoverage(
            status="UNAVAILABLE",
            reason="Section was not requested in the pinned Part 5B analysis.",
        )
    )
    statements: list[ReportStatement] = []
    for claim in reviewed.claims:
        if claim.section != analysis_section:
            continue
        review = reviews_by_claim.get(claim.claim_id)
        if review is None:
            continue
        statements.extend(_claim_report_statements(claim, review, section_id))
    relevant_kinds = (
        *PRIMARY_KINDS.get(analysis_section, ()),
        *SUPPORTING_KINDS.get(analysis_section, ()),
    )
    refs = [r for r in reviewed.sources if r.kind in relevant_kinds]
    tables: tuple[ReportTable, ...] = ()
    if refs:
        rows = tuple((_metric_from_reference(r, evidence),) for r in refs)
        tables = (
            ReportTable(
                table_id=f"{section_id.lower()}_evidence",
                title=_TABLE_TITLES.get(section_id, section_id.replace("_", " ").title()),
                section=section_id,
                columns=("Metric", "Value", "Unit", "Period", "Scope", "Status"),
                rows=rows,
            ),
        )
    return ReportSection(
        section=section_id, coverage=coverage, tables=tables, statements=tuple(statements)
    )


def _document_corpus_entries(
    reviewed: AnalysisSnapshot, evidence: ResolvedEvidence
) -> tuple[DocumentCorpusEntry, ...]:
    entries: list[DocumentCorpusEntry] = []
    for version_id, doc in evidence.documents.items():
        ref = next(
            (r for r in reviewed.sources if r.kind == "DOCUMENT_CHUNK"
             and r.owning_snapshot_id == version_id),
            None,
        )
        if ref is None:
            continue
        entries.append(
            DocumentCorpusEntry(
                document_type=doc.document_type,
                document_id=doc.identity.id,
                version_id=version_id,
                fiscal_label=doc.fiscal_label,
                publication_date=doc.publication_date.isoformat() if doc.publication_date else None,
                coverage_status=doc.coverage.overall,
                evidence_ref=ref,
            )
        )
    return tuple(entries)


def _build_document_corpus_section(corpus: DocumentCorpus) -> ReportSection:
    status = "AVAILABLE" if corpus.entries else "UNAVAILABLE"
    reason_code = None if corpus.entries else "NO_DOCUMENTS_OF_TYPE_PINNED"
    reason = (
        f"{len(corpus.entries)} document(s) present in the pinned research corpus."
        if corpus.entries
        else "No documents were pinned into the reviewed Part 5B analysis request."
    )
    return ReportSection(
        section="DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE",
        coverage=ReportSectionCoverage(status=status, reason=reason, reason_code=reason_code),  # type: ignore[arg-type]
    )


def _management_statement_entries(
    reviewed: AnalysisSnapshot,
    evidence: ResolvedEvidence,
    reviews_by_claim: dict[UUID, ClaimReview],
) -> list[tuple[ReportStatement, DocumentType]]:
    ClaimEntry = tuple[AnalystClaim | None, ClaimReview | None]
    claim_by_chunk: dict[tuple[str, str | None], ClaimEntry] = {}
    for source_claim in reviewed.claims:
        if source_claim.management_statement is None:
            continue
        key = (str(source_claim.management_statement.chunk.owning_snapshot_id),
               source_claim.management_statement.chunk.item_key)
        claim_by_chunk[key] = (source_claim, reviews_by_claim.get(source_claim.claim_id))
    out: list[tuple[ReportStatement, DocumentType]] = []
    for statement in reviewed.management_statements:
        doc = evidence.documents.get(statement.chunk.owning_snapshot_id)
        if doc is None:
            continue
        key = (str(statement.chunk.owning_snapshot_id), statement.chunk.item_key)
        found: ClaimEntry = claim_by_chunk.get(key, (None, None))
        owning_claim, review = found
        out.append(
            (
                ReportStatement(
                    statement_id=uuid4(),
                    section="DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
                    text=statement.text,
                    epistemic_class="FACT",
                    origin="DETERMINISTIC",
                    source_claim_ids=(owning_claim.claim_id,) if owning_claim else (),
                    evidence_refs=(statement.chunk,),
                    challenge_outcome=review.outcome if review else None,
                    quote_attribution=quote_attribution(doc.document_type),
                ),
                doc.document_type,
            )
        )
    return out


def _build_document_filtered_section(
    section_id: ReportSectionId,
    types: tuple[DocumentType, ...],
    quote_statements: list[tuple[ReportStatement, DocumentType]],
) -> ReportSection:
    matching = [
        s.model_copy(update={"section": section_id}) for s, doc_type in quote_statements
        if doc_type in types
    ]
    status = "AVAILABLE" if matching else "UNAVAILABLE"
    reason_code = None if matching else "NO_DOCUMENTS_OF_TYPE_PINNED"
    reason = (
        f"{len(matching)} retained statement(s) found in pinned documents of this type."
        if matching
        else "No documents of this type were pinned into the reviewed Part 5B analysis request."
    )
    return ReportSection(
        section=section_id,
        coverage=ReportSectionCoverage(status=status, reason=reason, reason_code=reason_code),  # type: ignore[arg-type]
        statements=tuple(matching),
    )


def _build_narrative_change_section(evidence: ResolvedEvidence) -> ReportSection:
    """V1 scope: ReportRequest carries no caller-declared topic keyword, so only
    the structural fact of multiple retained versions of the same document is
    shown - never a content-level diff or sentiment judgement."""
    by_document: dict[UUID, list[UUID]] = {}
    for version_id, doc in evidence.documents.items():
        by_document.setdefault(doc.identity.id, []).append(version_id)
    revised = {doc_id: versions for doc_id, versions in by_document.items() if len(versions) > 1}
    if not revised:
        return ReportSection(
            section="MANAGEMENT_NARRATIVE_CHANGE_DETECTION",
            coverage=ReportSectionCoverage(
                status="INSUFFICIENT_INPUT",
                reason="Fewer than two retained versions of the same document are pinned; no "
                "narrative-change comparison is possible.",
            ),
        )
    return ReportSection(
        section="MANAGEMENT_NARRATIVE_CHANGE_DETECTION",
        coverage=ReportSectionCoverage(
            status="PARTIAL",
            reason=(
                f"{len(revised)} document(s) have multiple retained versions pinned; only the "
                "structural existence of revisions is shown, since no caller-declared topic "
                "was supplied to compare exact statement content."
            ),
        ),
    )


CORE_SECTIONS: frozenset[str] = frozenset(
    {
        "COMPANY_IDENTITY_AND_EVIDENCE_FILE",
        "ANNUAL_FINANCIAL_PERFORMANCE",
        "QUARTERLY_PERFORMANCE_AND_INFLECTION",
        "BALANCE_SHEET_ANALYSIS",
        "CASH_FLOW_AND_EARNINGS_QUALITY",
        "SECTOR_SPECIFIC_KPI_ANALYSIS",
        "VALUATION",
        "MARKET_IMPLIED_EXPECTATIONS",
        "INDEPENDENT_CHALLENGER_REVIEW",
        "RESEARCH_SYNTHESIS",
    }
)

MAJOR_COMPLEMENTARY_SECTIONS: frozenset[str] = frozenset(
    {
        "BUSINESS_MODEL",
        "INDUSTRY_AND_COMPETITIVE_STRUCTURE",
        "DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
        "CONCALL_INTELLIGENCE",
        "ANNUAL_REPORT_INTELLIGENCE",
        "INVESTOR_PRESENTATION_INTELLIGENCE",
        "DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE",
        "OWNERSHIP_AND_SHAREHOLDING",
        "FORENSIC_AND_QUALITY_REVIEW",
        "MARKET_AND_PRICE_ANALYSIS",
        "TECHNICAL_ANALYSIS",
        "MACRO_POLICY_REGULATION",
        "CATALYSTS",
        "RISK_REGISTER",
        "BULL_BASE_BEAR_SCENARIOS",
        "VARIANT_PERCEPTION",
    }
)

# Appendices, REPORT_METADATA, EXECUTIVE_RESEARCH_SNAPSHOT, MONITORING_DASHBOARD, and
# DATA_GAPS_AND_LIMITATIONS are deliberately in neither family: they are derived views of
# other sections' own content (or always-AVAILABLE-by-construction meta sections), not
# original research coverage - counting them would double-count or trivially inflate.

_COMPLETENESS_ACCEPTABLE_STATUSES: frozenset[str] = frozenset(
    {"AVAILABLE", "PARTIAL", "NOT_APPLICABLE"}
)
# A section whose non-availability reflects a permanent V1 capability boundary (no upstream
# data model exists at all) rather than a genuine evidence-coverage gap for THIS company is
# excluded from completeness entirely - otherwise HIGH would be permanently unreachable for
# every report regardless of how thoroughly evidence was actually gathered.
_CAPABILITY_GAP_REASON_CODES: frozenset[str] = frozenset(
    {"UPSTREAM_CAPABILITY_NOT_IMPLEMENTED", "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED"}
)


def compute_research_completeness(sections: tuple[ReportSection, ...]) -> ResearchCompleteness:
    """Mechanical, family-based rule over the FULL assembled Part 6A report - not the
    narrower set of AnalysisSections a particular Part 5B request happened to ask for.
    No percentage, no weighted score, no model input: only frozen SectionCoverageStatus/
    reason_code membership in two fixed, hardcoded section families.

    - LIMITED: 2 or more CORE sections have an unacceptable status (multiple core areas
      necessary for meaningful research are unavailable/insufficient).
    - MODERATE: exactly 1 CORE gap, or 1+ MAJOR_COMPLEMENTARY gaps with 0 CORE gaps
      (meaningful core research is available, but a material area remains incomplete).
    - HIGH: zero gaps in either family.
    """

    def gap(section: ReportSection) -> bool:
        return section.coverage.status not in _COMPLETENESS_ACCEPTABLE_STATUSES

    scored = [s for s in sections if s.coverage.reason_code not in _CAPABILITY_GAP_REASON_CODES]
    core_gaps = sum(1 for s in scored if s.section in CORE_SECTIONS and gap(s))
    complementary_gaps = sum(
        1 for s in scored if s.section in MAJOR_COMPLEMENTARY_SECTIONS and gap(s)
    )
    if core_gaps >= 2:
        return "LIMITED"
    if core_gaps == 1 or complementary_gaps >= 1:
        return "MODERATE"
    return "HIGH"


def compute_valuation_evidence_state(
    reviewed: AnalysisSnapshot, evidence: ResolvedEvidence
) -> ValuationEvidenceState:
    coverage_entry = next(
        (c for c in reviewed.section_coverage if c.section == "VALUATION"), None
    )
    if coverage_entry is None or coverage_entry.status not in ("AVAILABLE", "PARTIAL"):
        return "NOT_DETERMINABLE"
    categories = set()
    for ref in reviewed.sources:
        if ref.kind != "VALUATION":
            continue
        v = evidence.valuations.get(ref.owning_snapshot_id)
        if v is None:
            continue
        for r in v.results:
            if str(r.id) == ref.item_key:
                categories.add(r.definition.category)
    if "OBSERVED_MULTIPLE_CALCULATION" in categories:
        return "OBSERVED"
    if categories & {"SCENARIO_CALCULATION", "SCENARIO_EXPECTATION"}:
        return "SCENARIO_DEPENDENT"
    return "NOT_DETERMINABLE"


def _build_synthesis(
    reviewed: AnalysisSnapshot,
    challenge: ChallengeSnapshot,
    evidence: ResolvedEvidence,
    sections: tuple[ReportSection, ...],
) -> ReportSynthesis:
    supporting, challenged, invalid, unverifiable = [], [], [], []
    disconfirming: list[EvidenceReference] = []
    material_findings: list[UUID] = []
    unresolved: list[str] = []
    for review in challenge.claim_reviews:
        if review.outcome == "SUPPORTED_BY_BUNDLE":
            supporting.append(review.analyst_claim_id)
        elif review.outcome in ("CHALLENGED", "SUPPORTED_WITH_LIMITATIONS"):
            challenged.append(review.analyst_claim_id)
        elif review.outcome == "INVALID_AGAINST_BUNDLE":
            invalid.append(review.analyst_claim_id)
        elif review.outcome == "UNVERIFIABLE_FROM_BUNDLE":
            unverifiable.append(review.analyst_claim_id)
        for finding in review.findings:
            material_findings.append(finding.finding_id)
            disconfirming.extend(finding.cited_evidence)
        unresolved.extend(review.unresolved_questions)
    return ReportSynthesis(
        research_completeness=compute_research_completeness(sections),
        valuation_evidence_state=compute_valuation_evidence_state(reviewed, evidence),
        supporting_claim_ids=tuple(supporting),
        challenged_claim_ids=tuple(challenged),
        invalid_claim_ids=tuple(invalid),
        unverifiable_claim_ids=tuple(unverifiable),
        material_disconfirming_evidence=tuple(dict.fromkeys(disconfirming)),
        material_challenger_finding_ids=tuple(material_findings),
        evidence_limitations=(),
        unresolved_questions=tuple(dict.fromkeys(unresolved)),
    )


def _build_limitations(
    sections: tuple[ReportSection, ...], challenge: ChallengeSnapshot
) -> tuple[ReportLimitation, ...]:
    limitations: list[ReportLimitation] = []
    for section in sections:
        if section.coverage.status != "AVAILABLE":
            limitations.append(
                ReportLimitation(
                    code=section.coverage.reason_code or section.coverage.status,
                    message=section.coverage.reason,
                    section=section.section,
                )
            )
    for review in challenge.claim_reviews:
        if review.omission_review_status != "NO_RELEVANT_OMISSION_FOUND":
            limitations.append(
                ReportLimitation(
                    code=review.omission_review_status,
                    message=f"Omission review for claim {review.analyst_claim_id}: "
                    f"{review.omission_review_status}",
                    evidence_refs=review.additional_relevant_evidence,
                )
            )
    reviewed = challenge.challenger_bundle.reviewed_analysis
    for claim in reviewed.claims:
        if POINT_IN_TIME_ISSUE_CODE in claim.limitations:
            limitations.append(
                ReportLimitation(
                    code=POINT_IN_TIME_ISSUE_CODE,
                    message=f"Claim {claim.claim_id} cites evidence with unverified "
                    "point-in-time availability.",
                    section=None,
                )
            )
    return tuple(limitations)


_COVERAGE_STATUSES = (
    "AVAILABLE", "PARTIAL", "INSUFFICIENT_INPUT", "NOT_APPLICABLE", "UNAVAILABLE",
    "UNVERIFIED_METRIC_SEMANTICS", "INCOMPATIBLE_SCOPE", "INCOMPATIBLE_PERIOD",
)


def _as_coverage_status(status: str) -> SectionCoverageStatus:
    if status in _COVERAGE_STATUSES:
        return cast(SectionCoverageStatus, status)
    return "AVAILABLE"


def _build_monitoring(
    challenge: ChallengeSnapshot, evidence: ResolvedEvidence
) -> tuple[MonitoringEntry, ...]:
    entries: list[MonitoringEntry] = []
    seen: set[tuple[str, str | None]] = set()
    for review in challenge.claim_reviews:
        if review.outcome not in ("CHALLENGED", "SUPPORTED_WITH_LIMITATIONS"):
            continue
        for ref in review.reviewed_citations:
            key = (str(ref.owning_snapshot_id), ref.item_key)
            if key in seen:
                continue
            seen.add(key)
            metric = _metric_from_reference(ref, evidence)
            entries.append(
                MonitoringEntry(
                    metric=metric.label,
                    current_value=metric.display_value,
                    prior_value=None,
                    direction="UNKNOWN",
                    source=ref,
                    period=metric.period,
                    coverage=_as_coverage_status(metric.status),
                    monitoring_rationale=f"Cited in a {review.outcome} claim.",
                )
            )
        for ref in review.additional_relevant_evidence:
            key = (str(ref.owning_snapshot_id), ref.item_key)
            if key in seen:
                continue
            seen.add(key)
            metric = _metric_from_reference(ref, evidence)
            entries.append(
                MonitoringEntry(
                    metric=metric.label, current_value=metric.display_value, prior_value=None,
                    direction="UNKNOWN", source=ref, period=metric.period,
                    coverage="AVAILABLE",
                    monitoring_rationale="Identified as relevant but uncited by the independent "
                    "challenger's omission review.",
                )
            )
    return tuple(entries)


def _build_executive_snapshot(
    sections: tuple[ReportSection, ...], synthesis: ReportSynthesis
) -> ExecutiveResearchSnapshot:
    by_section = {s.section: s for s in sections}

    def statements_for(section_id: ReportSectionId) -> tuple[ReportStatement, ...]:
        section = by_section.get(section_id)
        if section is None:
            return ()
        return tuple(
            st for st in section.statements
            if st.challenge_outcome in (None, "SUPPORTED_BY_BUNDLE", "SUPPORTED_WITH_LIMITATIONS")
        )

    supporting = tuple(
        st
        for s in sections
        for st in s.statements
        if st.challenge_outcome == "SUPPORTED_BY_BUNDLE"
    )
    major_gaps = tuple(
        ReportLimitation(
            code=s.coverage.reason_code or s.coverage.status,
            message=s.coverage.reason,
            section=s.section,
        )
        for s in sections
        if s.coverage.status != "AVAILABLE"
    )
    return ExecutiveResearchSnapshot(
        company_overview=statements_for("COMPANY_IDENTITY_AND_EVIDENCE_FILE"),
        key_financial_trend=statements_for("ANNUAL_FINANCIAL_PERFORMANCE"),
        key_sector_trend=statements_for("SECTOR_SPECIFIC_KPI_ANALYSIS"),
        supporting_evidence=supporting,
        disconfirming_evidence=synthesis.material_disconfirming_evidence,
        document_highlights=statements_for("DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE"),
        valuation_evidence=statements_for("VALUATION"),
        market_implied_expectations=statements_for("MARKET_IMPLIED_EXPECTATIONS"),
        catalysts=statements_for("CATALYSTS"),
        risks=statements_for("RISK_REGISTER"),
        material_challenger_finding_ids=synthesis.material_challenger_finding_ids,
        unresolved_questions=synthesis.unresolved_questions,
        major_data_gaps=major_gaps,
        research_completeness=synthesis.research_completeness,
        valuation_evidence_state=synthesis.valuation_evidence_state,
    )


def _build_challenger_section(challenge: ChallengeSnapshot) -> ReportSection:
    statements: list[ReportStatement] = []
    reviewed = challenge.challenger_bundle.reviewed_analysis
    claims_by_id = {c.claim_id: c for c in reviewed.claims}
    for review in challenge.claim_reviews:
        claim = claims_by_id.get(review.analyst_claim_id)
        if claim is None:
            continue
        if review.outcome == "INVALID_AGAINST_BUNDLE":
            statements.append(
                ReportStatement(
                    statement_id=uuid4(), section="INDEPENDENT_CHALLENGER_REVIEW",
                    text=claim.statement, epistemic_class="INTERPRETATION", origin="DETERMINISTIC",
                    source_claim_ids=(claim.claim_id,),
                    challenge_finding_ids=tuple(f.finding_id for f in review.findings),
                    challenge_outcome=review.outcome,
                )
            )
        for finding in review.findings:
            statements.append(
                ReportStatement(
                    statement_id=uuid4(), section="INDEPENDENT_CHALLENGER_REVIEW",
                    text=finding.statement, epistemic_class="INTERPRETATION",
                    origin="DETERMINISTIC",
                    challenge_finding_ids=(finding.finding_id,),
                    evidence_refs=finding.cited_evidence,
                    challenge_outcome=review.outcome,
                )
            )
        for alt in review.alternative_interpretations:
            statements.append(
                ReportStatement(
                    statement_id=uuid4(), section="INDEPENDENT_CHALLENGER_REVIEW",
                    text=alt.statement, epistemic_class="INTERPRETATION", origin="DETERMINISTIC",
                    alternative_interpretation_ids=(alt.interpretation_id,),
                    evidence_refs=alt.citations,
                )
            )
    return ReportSection(
        section="INDEPENDENT_CHALLENGER_REVIEW",
        coverage=ReportSectionCoverage(
            status="AVAILABLE", reason="Independent Part 5C review completed for every claim."
        ),
        statements=tuple(statements),
    )


def _build_appendices(
    reviewed: AnalysisSnapshot, challenge: ChallengeSnapshot
) -> tuple[ReportSection, ...]:
    evidence_table = ReportTable(
        table_id="evidence_appendix", title="Evidence Appendix", section="EVIDENCE_APPENDIX",
        columns=("Kind", "Owning Snapshot", "Item Key", "Hash"),
        rows=tuple(
            (
                ReportMetric(
                    label=f"{r.kind}:{r.item_key or ''}", status="AVAILABLE", evidence_ref=r,
                    display_value=r.snapshot_payload_sha256[:12],
                ),
            )
            for r in reviewed.sources
        ),
    )
    calc_kinds = {"FINANCIAL_CALCULATION", "SECTOR_KPI", "MARKET_CALCULATION", "TECHNICAL"}
    calc_table = ReportTable(
        table_id="calculation_appendix", title="Calculation Appendix",
        section="CALCULATION_APPENDIX",
        columns=("Kind", "Item Key", "Hash"),
        rows=tuple(
            (
                ReportMetric(
                    label=f"{r.kind}:{r.item_key or ''}", status="AVAILABLE", evidence_ref=r,
                    display_value=r.snapshot_payload_sha256[:12],
                ),
            )
            for r in reviewed.sources
            if r.kind in calc_kinds
        ),
    )
    mgmt_statements = tuple(
        ReportStatement(
            statement_id=uuid4(), section="MANAGEMENT_STATEMENT_APPENDIX", text=ms.text,
            epistemic_class="FACT", origin="DETERMINISTIC", evidence_refs=(ms.chunk,),
        )
        for ms in reviewed.management_statements
    )
    challenger_statements = tuple(
        st.model_copy(update={"section": "CHALLENGER_APPENDIX"})
        for st in _build_challenger_section(challenge).statements
    )
    return (
        ReportSection(
            section="EVIDENCE_APPENDIX",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="Full pinned evidence list."),
            tables=(evidence_table,) if reviewed.sources else (),
        ),
        ReportSection(
            section="CALCULATION_APPENDIX",
            coverage=ReportSectionCoverage(
                status="AVAILABLE" if calc_table.rows else "UNAVAILABLE",
                reason="Pinned calculation-kind evidence."
                if calc_table.rows else "No calculation-kind evidence was pinned.",
            ),
            tables=(calc_table,) if calc_table.rows else (),
        ),
        ReportSection(
            section="MANAGEMENT_STATEMENT_APPENDIX",
            coverage=ReportSectionCoverage(
                status="AVAILABLE" if mgmt_statements else "UNAVAILABLE",
                reason="Pinned management statements."
                if mgmt_statements else "No management statements were pinned.",
            ),
            statements=mgmt_statements,
        ),
        ReportSection(
            section="GUIDANCE_VS_ACTUAL_APPENDIX",
            coverage=ReportSectionCoverage(
                status="INSUFFICIENT_INPUT",
                reason="No structured management-guidance evidence model exists upstream.",
                reason_code="REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED",
            ),
        ),
        ReportSection(
            section="CHALLENGER_APPENDIX",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="Full Part 5C review dump."),
            statements=challenger_statements,
        ),
    )


_PLACEHOLDER_SECTIONS: tuple[tuple[ReportSectionId, str, str, str | None], ...] = (
    (
        "MANAGEMENT_GUIDANCE_AND_COMMITMENTS", "INSUFFICIENT_INPUT",
        "No structured management-guidance evidence model exists upstream.",
        "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED",
    ),
    (
        "MANAGEMENT_GUIDANCE_VS_ACTUAL", "INSUFFICIENT_INPUT",
        "No structured management-guidance evidence model exists upstream.",
        "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED",
    ),
    (
        "AGM_INTELLIGENCE", "UNAVAILABLE",
        "No AGM-specific document classification exists upstream.",
        "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED",
    ),
    (
        "MANAGEMENT_CREDIBILITY_CONSISTENCY", "INSUFFICIENT_INPUT",
        "Management credibility review requires the same structured guidance evidence that "
        "does not yet exist upstream.",
        "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED",
    ),
    (
        "GOVERNANCE_AND_CAPITAL_ALLOCATION", "UNAVAILABLE",
        "No governance/capital-allocation evidence model exists upstream.",
        "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED",
    ),
)


@dataclass(frozen=True)
class ReportAssembly:
    sections: tuple[ReportSection, ...]
    executive_snapshot: ExecutiveResearchSnapshot
    synthesis: ReportSynthesis
    document_corpus: DocumentCorpus
    monitoring: tuple[MonitoringEntry, ...]
    limitations: tuple[ReportLimitation, ...]


def assemble_report(challenge: ChallengeSnapshot, evidence: ResolvedEvidence) -> ReportAssembly:
    reviewed = challenge.challenger_bundle.reviewed_analysis
    reviews_by_claim = {r.analyst_claim_id: r for r in challenge.claim_reviews}

    sections: list[ReportSection] = [
        _build_mapped_section(section_id, analysis_section, reviewed, evidence, reviews_by_claim)
        for section_id, analysis_section in _ANALYSIS_SECTION_MAP.items()
    ]
    sections.append(
        _placeholder_section(
            "INDUSTRY_AND_COMPETITIVE_STRUCTURE", "UNAVAILABLE",
            "Competitive/peer evidence is not independently citable in the closed evidence "
            "bundle.",
            "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED",
        )
    )
    sections.append(_build_company_identity_section(reviewed, reviews_by_claim))
    document_corpus = DocumentCorpus(entries=_document_corpus_entries(reviewed, evidence))
    sections.append(_build_document_corpus_section(document_corpus))

    quote_statements = _management_statement_entries(reviewed, evidence, reviews_by_claim)
    deep_mgmt = next(
        s for s in sections if s.section == "DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE"
    )
    sections[sections.index(deep_mgmt)] = deep_mgmt.model_copy(
        update={"statements": deep_mgmt.statements + tuple(s for s, _ in quote_statements)}
    )
    for section_id, types in _DOCUMENT_FILTER_SECTIONS.items():
        sections.append(_build_document_filtered_section(section_id, types, quote_statements))

    sections.append(_build_narrative_change_section(evidence))
    for section_id, status, reason, reason_code in _PLACEHOLDER_SECTIONS:
        sections.append(_placeholder_section(section_id, status, reason, reason_code))
    sections.append(_build_challenger_section(challenge))
    sections.append(
        ReportSection(
            section="RESEARCH_SYNTHESIS",
            coverage=ReportSectionCoverage(
                status="AVAILABLE", reason="Deterministic synthesis fields computed."
            ),
        )
    )
    monitoring = _build_monitoring(challenge, evidence)
    sections.append(
        ReportSection(
            section="MONITORING_DASHBOARD",
            coverage=ReportSectionCoverage(
                status="AVAILABLE" if monitoring else "UNAVAILABLE",
                reason="Static snapshot of already-known monitoring variables."
                if monitoring else "No claim carries a challenge or omission worth monitoring.",
            ),
        )
    )
    frozen_sections_pre_gaps = tuple(sections)
    synthesis = _build_synthesis(reviewed, challenge, evidence, frozen_sections_pre_gaps)
    limitations = _build_limitations(frozen_sections_pre_gaps, challenge)
    sections.append(
        ReportSection(
            section="DATA_GAPS_AND_LIMITATIONS",
            coverage=ReportSectionCoverage(
                status="AVAILABLE", reason="Deterministic aggregation of every non-AVAILABLE "
                "section and omission review."
            ),
        )
    )
    sections.extend(_build_appendices(reviewed, challenge))
    sections.append(
        ReportSection(
            section="REPRODUCIBILITY_APPENDIX",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="Hash/provenance manifest."),
        )
    )
    sections.append(
        ReportSection(
            section="REPORT_METADATA",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="Generation provenance."),
        )
    )
    frozen_sections = tuple(sections)
    executive_snapshot = _build_executive_snapshot(frozen_sections, synthesis)
    sections.append(
        ReportSection(
            section="EXECUTIVE_RESEARCH_SNAPSHOT",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="Deterministic projection."),
        )
    )
    return ReportAssembly(
        sections=tuple(sections),
        executive_snapshot=executive_snapshot,
        synthesis=synthesis,
        document_corpus=document_corpus,
        monitoring=monitoring,
        limitations=limitations,
    )


def _build_company_identity_section(
    reviewed: AnalysisSnapshot, reviews_by_claim: dict[UUID, ClaimReview]
) -> ReportSection:
    coverage_entry = next(
        (c for c in reviewed.section_coverage if c.section == "COMPANY_OVERVIEW"), None
    )
    coverage = (
        ReportSectionCoverage(
            status=coverage_entry.status, reason=coverage_entry.reason,
            contributing_evidence_kinds=coverage_entry.contributing_evidence_kinds,
        )
        if coverage_entry is not None
        else ReportSectionCoverage(status="UNAVAILABLE", reason="Company overview not requested.")
    )
    statements: list[ReportStatement] = []
    for claim in reviewed.claims:
        if claim.section != "COMPANY_OVERVIEW":
            continue
        review = reviews_by_claim.get(claim.claim_id)
        if review is None:
            continue
        statements.extend(
            _claim_report_statements(claim, review, "COMPANY_IDENTITY_AND_EVIDENCE_FILE")
        )
    return ReportSection(
        section="COMPANY_IDENTITY_AND_EVIDENCE_FILE", coverage=coverage,
        statements=tuple(statements),
    )


async def resolve_report_evidence(
    reviewed: AnalysisSnapshot,
    company: Company,
    *,
    extractions: ExtractionRepository,
    market: MarketRepository,
    calculations: CalculationRepository,
    patterns: PatternRepository,
    market_calculations: MarketCalculationRepository,
    valuations: ValuationRepository,
    documents: DocumentRepository,
    context: ContextRepository,
    artifacts: RawArtifactStore,
) -> ResolvedEvidence:
    """Re-resolve the exact same pinned evidence Part 5B/5C already resolved, then
    independently re-read every raw artifact byte-for-byte - the same safeguard
    Part 5C itself needed. A failure here is an integrity failure, not a
    malformed request."""
    try:
        evidence = await resolve_evidence(
            reviewed.request, company, extractions=extractions, market=market,
            calculations=calculations, patterns=patterns, market_calculations=market_calculations,
            valuations=valuations, documents=documents, context=context,
        )
        raw_artifacts = {
            a.id: a
            for a in (
                *direct_raw_artifacts(evidence),
                *await transitive_raw_artifacts(
                    evidence, extractions=extractions, market=market, calculations=calculations
                ),
            )
        }
        for artifact in raw_artifacts.values():
            await artifacts.read(artifact)
    except ValueError as exc:
        raise ReportIntegrityError(str(exc)) from exc
    return evidence


__all__ = [
    "ReportAssembly",
    "ReportIntegrityError",
    "assemble_report",
    "compute_research_completeness",
    "compute_valuation_evidence_state",
    "quote_attribution",
    "resolve_report_evidence",
]
