"""DETAILED_RESEARCH_V1 Box 9: per-peer comparability, dimension by dimension.

Overall:
  NOT_COMPARABLE        sector family or business model not comparable
  UNKNOWN               the peer's classification or financial data is missing
  COMPARABLE            every assessed dimension COMPARABLE
  PARTIALLY_COMPARABLE  otherwise
"""

from uuid import uuid5

from stock_research_ai.application.peer_registry import NS
from stock_research_ai.application.peer_selection import CompanyLayers, model_label
from stock_research_ai.domain.industry_intelligence import (
    Comparability,
    Dimension,
    DimensionAssessment,
    KPIEquivalence,
    ObservationStatus,
    PeerCandidate,
    PeerComparabilityAssessment,
    PeerFinancialBenchmark,
    PeerKPIBenchmark,
    PeerSegmentComparison,
    SegmentMatch,
)

C = Comparability


def _bases(company: CompanyLayers) -> set[str]:
    if company.box4 is None:
        return set()
    return {v.reporting_basis.value for v in company.box4.canonical_values}


def _currencies(company: CompanyLayers) -> set[str]:
    if company.box4 is None:
        return set()
    return {
        v.currency.value for v in company.box4.canonical_values if v.value_kind.value == "MONEY"
    }


def assess(
    target: CompanyLayers,
    peer: CompanyLayers | None,
    candidate: PeerCandidate,
    financial: list[PeerFinancialBenchmark],
    kpis: list[PeerKPIBenchmark],
    segments: list[PeerSegmentComparison],
) -> PeerComparabilityAssessment:
    key = candidate.peer_company_key
    dims: list[DimensionAssessment] = []

    def add(dimension: Dimension, status: Comparability, detail: str) -> None:
        dims.append(DimensionAssessment(dimension=dimension, status=status, detail=detail))

    target_family, _ = target.family()
    target_model = model_label(target.box4)
    add(
        Dimension.SECTOR_FAMILY,
        C.UNKNOWN
        if "UNKNOWN" in (candidate.sector_family, target_family)
        else C.COMPARABLE
        if candidate.sector_family == target_family
        else C.NOT_COMPARABLE
        if candidate.sector_family
        else C.UNKNOWN,
        f"target {target_family}, peer {candidate.sector_family}",
    )
    if "FINANCIAL_SUBTYPE_UNRESOLVED" in candidate.exclusion_reasons:
        add(
            Dimension.BUSINESS_MODEL,
            C.PARTIALLY_COMPARABLE,
            f"Box 4 financial subtype unresolved (target {target_model}, peer "
            f"{candidate.business_model}): business-model-neutral metrics only",
        )
    elif "SAME_INDUSTRY" in candidate.selection_reasons:
        add(Dimension.BUSINESS_MODEL, C.COMPARABLE, "same Screener Industry classification")
    elif "SAME_BROAD_INDUSTRY" in candidate.selection_reasons:
        add(
            Dimension.BUSINESS_MODEL,
            C.PARTIALLY_COMPARABLE,
            "same Broad Industry, different Industry classification",
        )
    else:
        add(
            Dimension.BUSINESS_MODEL,
            C.NOT_COMPARABLE if candidate.exclusion_reasons else C.UNKNOWN,
            ", ".join(candidate.exclusion_reasons) or "no classification",
        )
    if peer is not None and peer.box4 is not None and target.box4 is not None:
        shared = _bases(target) & _bases(peer)
        add(
            Dimension.REPORTING_BASIS,
            C.COMPARABLE
            if "CONSOLIDATED" in shared
            else C.PARTIALLY_COMPARABLE
            if shared
            else C.NOT_COMPARABLE,
            f"shared bases: {', '.join(sorted(shared)) or 'none'}",
        )
        a, b = target.box4.fiscal_year_end_month, peer.box4.fiscal_year_end_month
        add(
            Dimension.ACCOUNTING_PERIOD,
            C.UNKNOWN if a is None or b is None else C.COMPARABLE if a == b else C.NOT_COMPARABLE,
            f"fiscal year-end month: target {a}, peer {b}",
        )
        mine, theirs = _currencies(target), _currencies(peer)
        add(
            Dimension.CURRENCY,
            C.COMPARABLE if mine and mine == theirs else C.PARTIALLY_COMPARABLE,
            f"money currencies: target {sorted(mine)}, peer {sorted(theirs)} (ratios may cross)",
        )
    else:
        for d in (Dimension.REPORTING_BASIS, Dimension.ACCOUNTING_PERIOD, Dimension.CURRENCY):
            add(d, C.UNKNOWN, "no Box 4 dataset for one side")
    good: list[str] = []
    bad: list[str] = []
    for bench in financial:
        seen = next((x for x in bench.peers if x.company_key == key), None)
        if seen is None:
            continue
        (good if seen.status == ObservationStatus.COMPARABLE else bad).append(bench.metric_code)
    add(
        Dimension.FINANCIAL_METRICS,
        C.UNKNOWN
        if not good and not bad
        else C.COMPARABLE
        if not bad
        else C.PARTIALLY_COMPARABLE
        if good
        else C.NOT_COMPARABLE,
        f"{len(good)} comparable, {len(bad)} not comparable",
    )
    exact = [
        k.kpi_code
        for k in kpis
        for o in k.peers
        if o.company_key == key
        and o.equivalence == KPIEquivalence.EXACT
        and o.status == ObservationStatus.COMPARABLE
    ]
    other = [
        k.kpi_code
        for k in kpis
        for o in k.peers
        if o.company_key == key
        and not (o.equivalence == KPIEquivalence.EXACT and o.status == ObservationStatus.COMPARABLE)
    ]
    add(
        Dimension.OPERATING_KPIS,
        C.UNKNOWN
        if not exact and not other
        else C.COMPARABLE
        if not other
        else C.PARTIALLY_COMPARABLE
        if exact
        else C.NOT_COMPARABLE,
        f"{len(exact)} exact comparable KPIs, {len(other)} proxy / not comparable",
    )
    seg = [s for s in segments if s.peer_company_key == key and s.domain == "SEGMENT"]
    if not seg:
        add(Dimension.SEGMENTS, C.UNKNOWN, "no Box 7 segments for one side")
    else:
        s = seg[0]
        add(
            Dimension.SEGMENTS,
            C.NOT_COMPARABLE
            if s.status in (SegmentMatch.UNMATCHED, SegmentMatch.STRUCTURE_CHANGED)
            else C.PARTIALLY_COMPARABLE,
            f"{s.status.value}: {len(s.identical_names)} identical names ({s.note})",
        )
    status = {d.dimension: d.status for d in dims}
    core = (status[Dimension.SECTOR_FAMILY], status[Dimension.BUSINESS_MODEL])
    if C.NOT_COMPARABLE in core:
        overall = C.NOT_COMPARABLE
    elif C.UNKNOWN in core or status[Dimension.FINANCIAL_METRICS] == C.UNKNOWN:
        overall = C.UNKNOWN
    elif all(d.status == C.COMPARABLE for d in dims if d.status != C.UNKNOWN) and (
        status[Dimension.BUSINESS_MODEL] == C.COMPARABLE
    ):
        overall = C.COMPARABLE
    else:
        overall = C.PARTIALLY_COMPARABLE
    return PeerComparabilityAssessment(
        assessment_id=uuid5(NS, f"comparability|{target.company_key}|{key}"),
        peer_company_key=key,
        dimensions=tuple(dims),
        overall=overall,
        comparable_metrics=tuple(sorted(set(good + exact))),
        not_comparable_metrics=tuple(sorted(set(bad + other) - set(good + exact))),
    )
