"""Deterministic evidence resolution, bundle assembly and section coverage for Part 5B.

No financial/calculation logic is reimplemented here. Every value the analyst may cite
already exists as a canonical result somewhere in Parts 1-5A; this module only resolves
pinned IDs to their owning objects, builds hash-verified EvidenceReferences, collects the
transitive raw-artifact surface for tamper detection, and computes SectionCoverage.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from stock_research_ai.application.hashing import digest
from stock_research_ai.application.ports import (
    CalculationRepository,
    ContextRepository,
    DocumentRepository,
    ExtractionRepository,
    MarketCalculationRepository,
    MarketRepository,
    PatternRepository,
    ValuationRepository,
)
from stock_research_ai.domain.analysis import (
    AnalysisRequest,
    AnalysisSection,
    AnstInputBundle,
    EvidenceKind,
    EvidenceReference,
    SectionCoverage,
)
from stock_research_ai.domain.calculations import CalculationResult, CalculationSnapshot
from stock_research_ai.domain.company import Company
from stock_research_ai.domain.documents import ParsedDocument
from stock_research_ai.domain.extraction import CompanyExtractionResult
from stock_research_ai.domain.financial import FinancialObservation
from stock_research_ai.domain.macro import MacroSnapshot
from stock_research_ai.domain.market import MarketExtractionResult
from stock_research_ai.domain.market_calculations import MarketCalculationSnapshot
from stock_research_ai.domain.patterns import PatternSnapshot
from stock_research_ai.domain.provenance import RawArtifact
from stock_research_ai.domain.publications import PublicationSnapshot
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation
from stock_research_ai.domain.technicals import TechnicalSnapshot
from stock_research_ai.domain.valuation import ValuationSnapshot

Calculation = CalculationSnapshot | SectorKPIEvaluation


class MissingEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedEvidence:
    company: Company | None
    financial: CompanyExtractionResult | None
    markets: dict[UUID, MarketExtractionResult] = field(default_factory=dict)
    calculations: dict[UUID, Calculation] = field(default_factory=dict)
    patterns: dict[UUID, PatternSnapshot] = field(default_factory=dict)
    market_calculations: dict[UUID, MarketCalculationSnapshot] = field(default_factory=dict)
    technicals: dict[UUID, TechnicalSnapshot] = field(default_factory=dict)
    valuations: dict[UUID, ValuationSnapshot] = field(default_factory=dict)
    documents: dict[UUID, ParsedDocument] = field(default_factory=dict)  # keyed by version_id
    publications: dict[UUID, PublicationSnapshot] = field(default_factory=dict)
    macros: dict[UUID, MacroSnapshot] = field(default_factory=dict)


async def resolve_evidence(
    request: AnalysisRequest,
    company: Company | None,
    *,
    extractions: ExtractionRepository,
    market: MarketRepository,
    calculations: CalculationRepository,
    patterns: PatternRepository,
    market_calculations: MarketCalculationRepository,
    valuations: ValuationRepository,
    documents: DocumentRepository,
    context: ContextRepository,
) -> ResolvedEvidence:
    """Resolve every explicitly pinned ID. Refuses (ValueError) on any unknown pin."""
    financial = None
    if request.financial_extraction_id is not None:
        financial = await extractions.get_extraction(request.financial_extraction_id)
        if financial is None or financial.company is None:
            raise MissingEvidenceError("Unknown pinned financial extraction")
        if financial.company.company_id != request.company_id:
            raise MissingEvidenceError("Pinned financial extraction belongs to another company")

    markets: dict[UUID, MarketExtractionResult] = {}
    for market_id in request.market_extraction_ids:
        m = await market.get_market(market_id)
        if m is None:
            raise MissingEvidenceError("Unknown pinned market extraction")
        markets[market_id] = m

    calc_map: dict[UUID, Calculation] = {}
    for calc_id in (*request.calculation_snapshot_ids, *request.sector_kpi_snapshot_ids):
        c = await calculations.get_calculation(calc_id)
        if c is None:
            raise MissingEvidenceError("Unknown pinned calculation/sector-KPI snapshot")
        calc_map[calc_id] = c

    pattern_map: dict[UUID, PatternSnapshot] = {}
    if request.pattern_snapshot_ids and request.financial_extraction_id is not None:
        pattern_history = await patterns.pattern_history(request.financial_extraction_id)
        pattern_by_id = {p.id: p for p in pattern_history}
        for pattern_id in request.pattern_snapshot_ids:
            if pattern_id not in pattern_by_id:
                raise MissingEvidenceError("Unknown pinned pattern snapshot")
            pattern_map[pattern_id] = pattern_by_id[pattern_id]

    mc_map: dict[UUID, MarketCalculationSnapshot] = {}
    tech_map: dict[UUID, TechnicalSnapshot] = {}
    for market_id in request.market_extraction_ids:
        if request.market_calculation_snapshot_ids:
            mc_history = await market_calculations.market_calculation_history(market_id)
            for mc_snapshot in mc_history:
                if mc_snapshot.id in request.market_calculation_snapshot_ids:
                    mc_map[mc_snapshot.id] = mc_snapshot
        if request.technical_snapshot_ids:
            tech_history = await market_calculations.technical_history(market_id)
            for tech_snapshot in tech_history:
                if tech_snapshot.id in request.technical_snapshot_ids:
                    tech_map[tech_snapshot.id] = tech_snapshot
    if len(mc_map) != len(request.market_calculation_snapshot_ids):
        raise MissingEvidenceError("Unknown pinned market calculation snapshot")
    if len(tech_map) != len(request.technical_snapshot_ids):
        raise MissingEvidenceError("Unknown pinned technical snapshot")

    valuation_map: dict[UUID, ValuationSnapshot] = {}
    if request.valuation_snapshot_ids:
        valuation_history = await valuations.valuation_history(request.company_id)
        valuation_by_id = {v.id: v for v in valuation_history}
        for valuation_id in request.valuation_snapshot_ids:
            if valuation_id not in valuation_by_id:
                raise MissingEvidenceError("Unknown pinned valuation snapshot")
            valuation_map[valuation_id] = valuation_by_id[valuation_id]

    document_map: dict[UUID, ParsedDocument] = {}
    for version_id, _chunk_id in request.document_chunk_refs:
        if version_id in document_map:
            continue
        doc = await documents.get_document(version_id)
        if doc is None:
            raise MissingEvidenceError("Unknown pinned document version")
        document_map[version_id] = doc
    for version_id, chunk_id in request.document_chunk_refs:
        doc = document_map[version_id]
        if not any(c.id == chunk_id for c in doc.chunks):
            raise MissingEvidenceError("Unknown pinned document chunk")

    publication_map: dict[UUID, PublicationSnapshot] = {}
    for publication_id in request.publication_snapshot_ids:
        publication = await context.get_publication(publication_id)
        if publication is None:
            raise MissingEvidenceError("Unknown pinned publication snapshot")
        publication_map[publication_id] = publication

    macro_map: dict[UUID, MacroSnapshot] = {}
    for macro_id, period in request.macro_snapshot_lookups:
        if macro_id not in macro_map:
            macro_snapshot = await context.get_macro(macro_id)
            if macro_snapshot is None:
                raise MissingEvidenceError("Unknown pinned macro snapshot")
            macro_map[macro_id] = macro_snapshot
        if not any(o.reference_period == period for o in macro_map[macro_id].observations):
            raise MissingEvidenceError("Unknown pinned macro observation period")

    return ResolvedEvidence(
        company=company,
        financial=financial,
        markets=markets,
        calculations=calc_map,
        patterns=pattern_map,
        market_calculations=mc_map,
        technicals=tech_map,
        valuations=valuation_map,
        documents=document_map,
        publications=publication_map,
        macros=macro_map,
    )


def _single_raw_artifact_sha(artifacts: tuple[RawArtifact, ...]) -> str | None:
    return artifacts[0].content_sha256 if len(artifacts) == 1 else None


def build_source_references(
    request: AnalysisRequest, evidence: ResolvedEvidence
) -> tuple[EvidenceReference, ...]:
    """Enumerate every individually-citable evidence item across all pinned snapshots."""
    refs: list[EvidenceReference] = []

    if evidence.company is not None:
        refs.append(
            EvidenceReference(
                kind="COMPANY",
                owning_snapshot_id=evidence.company.id,
                item_key=None,
                snapshot_payload_sha256=digest(evidence.company),
                raw_artifact_sha256=None,
            )
        )

    if evidence.financial is not None:
        f = evidence.financial
        f_hash = digest(f)
        raw_sha = _single_raw_artifact_sha(f.raw_artifacts)
        observations: tuple[FinancialObservation, ...] = (
            *f.top_metrics,
            *f.profit_loss,
            *f.quarterly,
            *f.balance_sheet,
            *f.cashflow,
            *f.ratios,
        )
        for observation in observations:
            refs.append(
                EvidenceReference(
                    kind="FINANCIAL",
                    owning_snapshot_id=f.metadata.id,
                    item_key=str(observation.id),
                    snapshot_payload_sha256=f_hash,
                    raw_artifact_sha256=raw_sha,
                )
            )
        for holding in f.shareholding.holdings:
            refs.append(
                EvidenceReference(
                    kind="SHAREHOLDING",
                    owning_snapshot_id=f.metadata.id,
                    item_key=str(holding.observation.id),
                    snapshot_payload_sha256=f_hash,
                    raw_artifact_sha256=raw_sha,
                )
            )
        for count in f.shareholding.shareholder_counts:
            refs.append(
                EvidenceReference(
                    kind="SHAREHOLDING",
                    owning_snapshot_id=f.metadata.id,
                    item_key=str(count.id),
                    snapshot_payload_sha256=f_hash,
                    raw_artifact_sha256=raw_sha,
                )
            )

    for market_id, m in evidence.markets.items():
        m_hash = digest(m)
        for market_observation in m.observations:
            refs.append(
                EvidenceReference(
                    kind="MARKET",
                    owning_snapshot_id=market_id,
                    item_key=str(market_observation.id),
                    snapshot_payload_sha256=m_hash,
                    raw_artifact_sha256=None,
                )
            )

    for calc_id, c in evidence.calculations.items():
        c_hash = digest(c)
        calc_kind: EvidenceKind = (
            "FINANCIAL_CALCULATION" if isinstance(c, CalculationSnapshot) else "SECTOR_KPI"
        )
        for calc_result in c.results:
            metric_id = (
                calc_result.metric_id
                if isinstance(calc_result, CalculationResult)
                else calc_result.definition.metric_id
            )
            refs.append(
                EvidenceReference(
                    kind=calc_kind,
                    owning_snapshot_id=calc_id,
                    item_key=metric_id,
                    snapshot_payload_sha256=c_hash,
                    raw_artifact_sha256=None,
                )
            )

    for pattern_id, p in evidence.patterns.items():
        p_hash = digest(p)
        for finding in p.findings:
            refs.append(
                EvidenceReference(
                    kind="PATTERN",
                    owning_snapshot_id=pattern_id,
                    item_key=str(finding.id),
                    snapshot_payload_sha256=p_hash,
                    raw_artifact_sha256=None,
                )
            )

    for mc_id, mc in evidence.market_calculations.items():
        mc_hash = digest(mc)
        for mc_result in mc.results:
            refs.append(
                EvidenceReference(
                    kind="MARKET_CALCULATION",
                    owning_snapshot_id=mc_id,
                    item_key=str(mc_result.metric_id),
                    snapshot_payload_sha256=mc_hash,
                    raw_artifact_sha256=None,
                )
            )

    for tech_id, t in evidence.technicals.items():
        t_hash = digest(t)
        for series in t.series:
            for point in series.points:
                if point.status != "AVAILABLE":
                    continue
                refs.append(
                    EvidenceReference(
                        kind="TECHNICAL",
                        owning_snapshot_id=tech_id,
                        item_key=str(point.id),
                        snapshot_payload_sha256=t_hash,
                        raw_artifact_sha256=None,
                    )
                )

    for val_id, v in evidence.valuations.items():
        v_hash = digest(v)
        for val_result in v.results:
            refs.append(
                EvidenceReference(
                    kind="VALUATION",
                    owning_snapshot_id=val_id,
                    item_key=str(val_result.id),
                    snapshot_payload_sha256=v_hash,
                    raw_artifact_sha256=None,
                )
            )

    for version_id, chunk_id in request.document_chunk_refs:
        doc = evidence.documents[version_id]
        d_hash = digest(doc)
        raw_sha = doc.artifact.content_sha256 if doc.artifact else None
        refs.append(
            EvidenceReference(
                kind="DOCUMENT_CHUNK",
                owning_snapshot_id=version_id,
                item_key=str(chunk_id),
                snapshot_payload_sha256=d_hash,
                raw_artifact_sha256=raw_sha,
            )
        )

    for pub_id, pub in evidence.publications.items():
        pub_hash = digest(pub)
        raw_sha = pub.artifact.content_sha256 if pub.artifact else None
        for index in range(1, len(pub.passages) + 1):
            refs.append(
                EvidenceReference(
                    kind="PUBLICATION",
                    owning_snapshot_id=pub_id,
                    item_key=str(index),
                    snapshot_payload_sha256=pub_hash,
                    raw_artifact_sha256=raw_sha,
                )
            )

    macro_periods: dict[UUID, set[str]] = {}
    for macro_id, period in request.macro_snapshot_lookups:
        macro_periods.setdefault(macro_id, set()).add(period)
    for macro_id, periods in macro_periods.items():
        macro_snapshot = evidence.macros[macro_id]
        macro_hash = digest(macro_snapshot)
        raw_sha = _single_raw_artifact_sha(macro_snapshot.artifacts)
        for period in sorted(periods):
            refs.append(
                EvidenceReference(
                    kind="MACRO",
                    owning_snapshot_id=macro_id,
                    item_key=period,
                    snapshot_payload_sha256=macro_hash,
                    raw_artifact_sha256=raw_sha,
                )
            )

    def sort_key(r: EvidenceReference) -> tuple[str, str, str]:
        return (r.kind, str(r.owning_snapshot_id), r.item_key or "")

    deduped = {(r.kind, r.owning_snapshot_id, r.item_key): r for r in refs}
    return tuple(sorted(deduped.values(), key=sort_key))


def direct_raw_artifacts(evidence: ResolvedEvidence) -> tuple[RawArtifact, ...]:
    """Every raw artifact directly owned by a pinned snapshot (safeguard: Phase 1)."""
    artifacts: dict[UUID, RawArtifact] = {}
    if evidence.financial is not None:
        for a in evidence.financial.raw_artifacts:
            artifacts[a.id] = a
    for m in evidence.markets.values():
        for a in m.raw_artifacts:
            artifacts[a.id] = a
    for doc in evidence.documents.values():
        if doc.artifact is not None:
            artifacts[doc.artifact.id] = doc.artifact
    for pub in evidence.publications.values():
        if pub.artifact is not None:
            artifacts[pub.artifact.id] = pub.artifact
    for macro_snapshot in evidence.macros.values():
        for a in macro_snapshot.artifacts:
            artifacts[a.id] = a
    return tuple(artifacts.values())


async def transitive_raw_artifacts(
    evidence: ResolvedEvidence,
    *,
    extractions: ExtractionRepository,
    market: MarketRepository,
    calculations: CalculationRepository,
) -> tuple[RawArtifact, ...]:
    """Safeguard 1B: walk every DERIVED pinned snapshot back to its own upstream raw
    evidence and independently re-fetch it, so corruption underneath a derived snapshot
    (not just underneath a directly-cited extraction) is caught before persistence.
    Pure read/refetch of already-existing lineage fields; no calculation logic runs here.
    """
    artifacts: dict[UUID, RawArtifact] = {}
    extraction_cache: dict[UUID, CompanyExtractionResult] = {}
    market_cache: dict[UUID, MarketExtractionResult] = {}

    async def extraction_for(extraction_id: UUID) -> CompanyExtractionResult:
        if extraction_id not in extraction_cache:
            fetched = await extractions.get_extraction(extraction_id)
            if fetched is None:
                raise MissingEvidenceError(
                    "Upstream extraction referenced by a derived snapshot is missing"
                )
            extraction_cache[extraction_id] = fetched
        return extraction_cache[extraction_id]

    async def market_for(extraction_id: UUID) -> MarketExtractionResult:
        if extraction_id not in market_cache:
            fetched = await market.get_market(extraction_id)
            if fetched is None:
                raise MissingEvidenceError(
                    "Upstream market extraction referenced by a derived snapshot is missing"
                )
            market_cache[extraction_id] = fetched
        return market_cache[extraction_id]

    async def add_calculation_lineage(c: Calculation) -> None:
        extraction_id = c.request.extraction_id
        upstream = await extraction_for(extraction_id)
        if digest(upstream) != c.source_payload_sha256:
            raise MissingEvidenceError(
                "Derived snapshot's recorded source hash no longer matches its upstream extraction"
            )
        for a in upstream.raw_artifacts:
            artifacts[a.id] = a

    for c in evidence.calculations.values():
        await add_calculation_lineage(c)

    for p in evidence.patterns.values():
        extraction_id = p.request.extraction_id
        upstream = await extraction_for(extraction_id)
        if digest(upstream) != p.source_payload_sha256:
            raise MissingEvidenceError(
                "Pattern snapshot's recorded source hash no longer matches its upstream extraction"
            )
        for a in upstream.raw_artifacts:
            artifacts[a.id] = a

    for mc in evidence.market_calculations.values():
        for mc_source in mc.sources:
            mc_market = await market_for(mc_source.extraction_id)
            if digest(mc_market) != mc_source.payload_sha256:
                raise MissingEvidenceError(
                    "Market calculation's recorded source hash no longer matches its "
                    "upstream market extraction"
                )
            for a in mc_market.raw_artifacts:
                artifacts[a.id] = a

    for t in evidence.technicals.values():
        tech_market = await market_for(t.source.extraction_id)
        if digest(tech_market) != t.source.payload_sha256:
            raise MissingEvidenceError(
                "Technical snapshot's recorded source hash no longer matches its upstream "
                "market extraction"
            )
        for a in tech_market.raw_artifacts:
            artifacts[a.id] = a

    for v in evidence.valuations.values():
        for valuation_source in v.sources:
            if valuation_source.kind == "FINANCIAL":
                val_extraction = await extraction_for(valuation_source.snapshot_id)
                if digest(val_extraction) != valuation_source.payload_sha256:
                    raise MissingEvidenceError(
                        "Valuation's recorded financial source hash no longer matches its "
                        "upstream extraction"
                    )
                for a in val_extraction.raw_artifacts:
                    artifacts[a.id] = a
            elif valuation_source.kind == "MARKET":
                val_market = await market_for(valuation_source.snapshot_id)
                if digest(val_market) != valuation_source.payload_sha256:
                    raise MissingEvidenceError(
                        "Valuation's recorded market source hash no longer matches its "
                        "upstream market extraction"
                    )
                for a in val_market.raw_artifacts:
                    artifacts[a.id] = a
            else:
                cited_calc = await calculations.get_calculation(valuation_source.snapshot_id)
                if cited_calc is None:
                    raise MissingEvidenceError("Valuation's cited calculation snapshot is missing")
                if digest(cited_calc) != valuation_source.payload_sha256:
                    raise MissingEvidenceError(
                        "Valuation's recorded calculation source hash no longer matches "
                        "its upstream calculation snapshot"
                    )
                await add_calculation_lineage(cited_calc)

    return tuple(artifacts.values())


PRIMARY_KINDS: dict[AnalysisSection, tuple[EvidenceKind, ...]] = {
    "COMPANY_OVERVIEW": ("COMPANY",),
    "BUSINESS_MODEL": ("DOCUMENT_CHUNK",),
    "HISTORICAL_FUNDAMENTALS": ("FINANCIAL_CALCULATION",),
    "QUARTERLY_INFLECTIONS": ("FINANCIAL_CALCULATION",),
    "BALANCE_SHEET": ("FINANCIAL",),
    "CASH_FLOW_QUALITY": ("PATTERN",),
    "SECTOR_KPIS": ("SECTOR_KPI",),
    "FORENSIC_REVIEW": ("PATTERN",),
    "MANAGEMENT_AND_DOCUMENT_INTELLIGENCE": ("DOCUMENT_CHUNK",),
    "OWNERSHIP_AND_SHAREHOLDING": ("SHAREHOLDING",),
    "INDUSTRY_AND_MACRO": ("PUBLICATION", "MACRO"),
    "MARKET_PERFORMANCE": ("MARKET_CALCULATION",),
    "TECHNICAL_STATE": ("TECHNICAL",),
    "VALUATION": ("VALUATION",),
    "EXPECTATIONS": ("VALUATION",),
    "CATALYSTS": ("PATTERN",),
    "RISKS": ("PATTERN",),
    "VARIANT_PERCEPTION": ("VALUATION",),
    "BULL_BASE_BEAR_INTERPRETATION": ("VALUATION",),
}

SUPPORTING_KINDS: dict[AnalysisSection, tuple[EvidenceKind, ...]] = {
    "BUSINESS_MODEL": ("COMPANY", "FINANCIAL"),
    "CATALYSTS": ("SECTOR_KPI", "MARKET_CALCULATION", "VALUATION"),
    "RISKS": ("SECTOR_KPI", "MARKET_CALCULATION", "VALUATION"),
    "VARIANT_PERCEPTION": ("FINANCIAL_CALCULATION", "SECTOR_KPI"),
}


def _reference_status(ref: EvidenceReference, evidence: ResolvedEvidence) -> str:
    """Resolve a citable reference's own upstream status - AVAILABLE for plain facts,
    or the underlying CalculationStatus/PatternStatus for derived results."""
    if ref.kind in ("COMPANY", "FINANCIAL", "SHAREHOLDING", "MARKET", "DOCUMENT_CHUNK",
                    "PUBLICATION", "MACRO"):
        return "AVAILABLE"
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        c = evidence.calculations[ref.owning_snapshot_id]
        for calc_result in c.results:
            metric_id = (
                calc_result.metric_id
                if isinstance(calc_result, CalculationResult)
                else calc_result.definition.metric_id
            )
            if metric_id == ref.item_key:
                return calc_result.status
    if ref.kind == "PATTERN":
        p = evidence.patterns[ref.owning_snapshot_id]
        for finding in p.findings:
            if str(finding.id) == ref.item_key:
                return finding.evaluation_status
    if ref.kind == "MARKET_CALCULATION":
        mc = evidence.market_calculations[ref.owning_snapshot_id]
        for mc_result in mc.results:
            if str(mc_result.metric_id) == ref.item_key:
                return mc_result.status
    if ref.kind == "TECHNICAL":
        return "AVAILABLE"  # only AVAILABLE points are ever enumerated as references
    if ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for val_result in v.results:
            if str(val_result.id) == ref.item_key:
                return val_result.status
    raise AssertionError(f"Unhandled evidence kind for status resolution: {ref.kind}")


def compute_section_coverage(
    sections: tuple[AnalysisSection, ...],
    sources: tuple[EvidenceReference, ...],
    evidence: ResolvedEvidence,
) -> tuple[SectionCoverage, ...]:
    """Safeguard 1C: deterministic aggregate coverage, computed before any model call."""
    coverage: list[SectionCoverage] = []
    for section in sorted(sections):
        primary_kinds = PRIMARY_KINDS.get(section, ())
        supporting_kinds = SUPPORTING_KINDS.get(section, ())
        primary_refs = [r for r in sources if r.kind in primary_kinds]
        supporting_refs = [r for r in sources if r.kind in supporting_kinds]
        if section == "VALUATION":
            primary_refs = [
                r
                for r in primary_refs
                if _valuation_category(evidence, r)
                in ("OBSERVED_MULTIPLE_CALCULATION", "SCENARIO_CALCULATION")
            ]
        elif section == "EXPECTATIONS":
            primary_refs = [
                r
                for r in primary_refs
                if _valuation_category(evidence, r) == "SCENARIO_EXPECTATION"
            ]
        if section == "VARIANT_PERCEPTION":
            has_market_implied = any(
                r.kind == "VALUATION"
                and _valuation_category(evidence, r) == "SCENARIO_EXPECTATION"
                for r in primary_refs
            )
            has_fundamental = bool(supporting_refs)
            if not has_market_implied or not has_fundamental:
                missing = []
                if not has_market_implied:
                    missing.append("no pinned SCENARIO_EXPECTATION valuation evidence")
                if not has_fundamental:
                    missing.append("no pinned fundamental (calculation/sector-KPI) evidence")
                coverage.append(
                    SectionCoverage(
                        section=section,
                        status=(
                            "INSUFFICIENT_INPUT"
                            if (primary_refs or supporting_refs)
                            else "UNAVAILABLE"
                        ),
                        reason="; ".join(missing),
                        contributing_evidence_kinds=tuple(
                            sorted({r.kind for r in (*primary_refs, *supporting_refs)})
                        ),
                    )
                )
                continue
        if section == "BULL_BASE_BEAR_INTERPRETATION":
            labeled = [
                r
                for r in primary_refs
                if _valuation_scenario_label(evidence, r) is not None
            ]
            if not labeled:
                coverage.append(
                    SectionCoverage(
                        section=section,
                        status="INSUFFICIENT_INPUT" if primary_refs else "UNAVAILABLE",
                        reason="No pinned valuation scenario carries a caller-declared label",
                        contributing_evidence_kinds=tuple(sorted({r.kind for r in primary_refs})),
                    )
                )
                continue
            primary_refs = labeled
        if primary_refs:
            statuses = sorted({_reference_status(r, evidence) for r in primary_refs})
            coverage.append(_aggregate(section, statuses, primary_refs))
        elif supporting_refs:
            coverage.append(
                SectionCoverage(
                    section=section,
                    status="INSUFFICIENT_INPUT",
                    reason=(
                        "Only supporting-only evidence kinds are pinned; primary evidence "
                        "for this section was not pinned."
                    ),
                    contributing_evidence_kinds=tuple(sorted({r.kind for r in supporting_refs})),
                )
            )
        else:
            coverage.append(
                SectionCoverage(
                    section=section,
                    status="UNAVAILABLE",
                    reason="No relevant evidence of any kind was pinned.",
                    contributing_evidence_kinds=(),
                )
            )
    return tuple(coverage)


def _aggregate(
    section: AnalysisSection, statuses: list[str], refs: list[EvidenceReference]
) -> SectionCoverage:
    kinds = tuple(sorted({r.kind for r in refs}))
    available = [s for s in statuses if s == "AVAILABLE"]
    non_available = sorted({s for s in statuses if s != "AVAILABLE"})
    if not non_available:
        return SectionCoverage(
            section=section, status="AVAILABLE", reason="All relevant evidence is available.",
            contributing_evidence_kinds=kinds,
        )
    if available:
        return SectionCoverage(
            section=section,
            status="PARTIAL",
            reason="Some but not all relevant evidence is available.",
            contributing_evidence_kinds=kinds,
            upstream_statuses=tuple(non_available),
        )
    if len(non_available) == 1:
        status = non_available[0]
        if status not in (
            "PARTIAL", "INSUFFICIENT_INPUT", "NOT_APPLICABLE", "UNAVAILABLE",
            "UNVERIFIED_METRIC_SEMANTICS", "INCOMPATIBLE_SCOPE", "INCOMPATIBLE_PERIOD",
        ):
            status = "INSUFFICIENT_INPUT"
        return SectionCoverage(
            section=section,
            status=status,  # type: ignore[arg-type]
            reason=f"All relevant evidence resolved to {status}.",
            contributing_evidence_kinds=kinds,
            upstream_statuses=tuple(non_available),
        )
    return SectionCoverage(
        section=section,
        status="UNAVAILABLE",
        reason="HETEROGENEOUS_NON_AVAILABLE_INPUTS",
        contributing_evidence_kinds=kinds,
        upstream_statuses=tuple(non_available),
    )


def _valuation_category(evidence: ResolvedEvidence, ref: EvidenceReference) -> str | None:
    v = evidence.valuations.get(ref.owning_snapshot_id)
    if v is None:
        return None
    for r in v.results:
        if str(r.id) == ref.item_key:
            return r.definition.category
    return None


def _valuation_scenario_label(evidence: ResolvedEvidence, ref: EvidenceReference) -> str | None:
    v = evidence.valuations.get(ref.owning_snapshot_id)
    if v is None:
        return None
    for selection, r in zip(v.request.selections, v.results, strict=True):
        if str(r.id) == ref.item_key and selection.scenario is not None:
            return selection.scenario.label
    return None


def assemble_bundle(request: AnalysisRequest, evidence: ResolvedEvidence) -> AnstInputBundle:
    sources = build_source_references(request, evidence)
    coverage = compute_section_coverage(request.sections, sources, evidence)
    return AnstInputBundle(
        bundle_id=uuid4(),
        request=request,
        assembled_at=datetime.now(UTC),
        sources=sources,
        section_coverage=coverage,
    )
