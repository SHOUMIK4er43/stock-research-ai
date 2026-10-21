"""DETAILED_RESEARCH_V1 Box 9: peer operating-KPI benchmarks and segment comparisons.

KPI equivalence never comes from an everyday label - only from Box 7's canonical KPI
identity:
  EXACT           same canonical KPI, qualifier, unit, period kind (and currency for money)
  PROXY           same canonical KPI, qualifier and unit, but a different period kind
                  (e.g. a year-end stock vs a flow): displayed, never ranked
  NOT_COMPARABLE  same canonical KPI with a different qualifier or unit
A peer compatible at the business-class level only (peer_selection NEUTRAL_ONLY) never enters
a KPI statistic: operating KPIs are business-model specific (CLASSIFICATION_MISMATCH).
Segments are compared by identical normalized Box 7 keys only, and every identical name is
reported as 'definitions unverified'; nothing like 'Financial Services' -> 'BFSI' is mapped.
"""

from dataclasses import dataclass
from datetime import date
from uuid import uuid5

from stock_research_ai.application.peer_financials import rank, statistics
from stock_research_ai.application.peer_registry import KPI_DIRECTIONALITY, NS
from stock_research_ai.application.peer_selection import CompanyLayers
from stock_research_ai.domain.business_intelligence import (
    CanonicalKPIValue,
    Comparability,
    SegmentRole,
)
from stock_research_ai.domain.industry_intelligence import (
    Directionality,
    EvidenceRef,
    KPIEquivalence,
    KPIObservation,
    ObservationStatus,
    PeerKPIBenchmark,
    PeerSegmentComparison,
    SegmentMatch,
    SourceLayer,
)

OS = ObservationStatus
#: segment identity = (name, role): a reportable segment never matches a sub-segment
_COMPARED_ROLES = (SegmentRole.REPORTABLE, SegmentRole.SUBSEGMENT)
_PRIORITY = {KPIEquivalence.EXACT: 0, KPIEquivalence.PROXY: 1, KPIEquivalence.NOT_COMPARABLE: 2}


@dataclass(frozen=True)
class _Pick:
    value: CanonicalKPIValue
    equivalence: KPIEquivalence
    status: ObservationStatus
    detail: str | None


def company_kpis(company: CompanyLayers) -> list[CanonicalKPIValue]:
    if company.box7 is None:
        return []
    return sorted(
        (
            k
            for k in company.box7.canonical_kpi_values
            if k.segment_key is None and k.geography_key is None
        ),
        key=lambda k: (
            k.canonical_kpi,
            k.qualifier or "",
            k.unit.value,
            k.period_kind.value,
            k.period.period_end or date.min,
            str(k.canonical_value_id),
        ),
    )


def _observation(
    company: CompanyLayers,
    k: CanonicalKPIValue,
    equivalence: KPIEquivalence,
    status: ObservationStatus,
    *,
    target: bool = False,
    detail: str | None = None,
) -> KPIObservation:
    assert company.box7 is not None
    return KPIObservation(
        company_key=company.company_key,
        is_target=target,
        canonical_kpi=k.canonical_kpi,
        qualifier=k.qualifier,
        unit=k.unit.value,
        period_kind=k.period_kind.value,
        period_end=k.period.period_end,
        reporting_basis=k.reporting_basis,
        currency=k.currency,
        value=k.value,
        equivalence=equivalence,
        status=status,
        source=EvidenceRef(
            company_key=company.company_key,
            layer=SourceLayer.BOX7_KPI,
            record_id=k.canonical_value_id,
            dataset_id=company.box7.dataset_id,
            locator=k.period.label,
        ),
        upstream_ids=(k.selected_observation_id,),
        detail=detail,
    )


def _pick(anchor: CanonicalKPIValue, values: list[CanonicalKPIValue]) -> _Pick | None:
    picks: list[tuple[int, int, str, _Pick]] = []
    for k in values:
        if k.canonical_kpi != anchor.canonical_kpi:
            continue
        if k.qualifier != anchor.qualifier or k.unit != anchor.unit:
            pick = _Pick(
                k,
                KPIEquivalence.NOT_COMPARABLE,
                OS.DEFINITION_MISMATCH,
                f"qualifier/unit {k.qualifier}/{k.unit.value} vs "
                f"{anchor.qualifier}/{anchor.unit.value}",
            )
        else:
            equivalence = (
                KPIEquivalence.EXACT
                if k.period_kind == anchor.period_kind
                else KPIEquivalence.PROXY
            )
            if k.reporting_basis != anchor.reporting_basis:
                status, detail = OS.BASIS_MISMATCH, f"{k.reporting_basis.value} basis"
            elif k.currency != anchor.currency:
                status, detail = OS.CURRENCY_MISMATCH, f"{k.currency} vs {anchor.currency}"
            elif k.period.period_end is None or anchor.period.period_end is None:
                # an unknown period end is never evidence of the same period
                status, detail = OS.PERIOD_MISMATCH, "period end unknown"
            elif k.period.period_end != anchor.period.period_end:
                status, detail = OS.PERIOD_MISMATCH, f"period end {k.period.period_end}"
            else:
                status, detail = OS.COMPARABLE, None
            if equivalence == KPIEquivalence.PROXY:
                detail = (detail + "; " if detail else "") + (
                    f"period kind {k.period_kind.value} vs {anchor.period_kind.value}"
                )
            pick = _Pick(k, equivalence, status, detail)
        gap = abs(((k.period.period_end or date.min) - (anchor.period.period_end or date.min)).days)
        picks.append(
            (
                _PRIORITY[pick.equivalence] * 10 + (0 if pick.status == OS.COMPARABLE else 1),
                gap,
                str(k.canonical_value_id),
                pick,
            )
        )
    return min(picks, key=lambda p: p[:3])[3] if picks else None


def kpi_benchmarks(
    target: CompanyLayers,
    peers: list[CompanyLayers],
    min_peers: int,
    neutral_only: frozenset[str] = frozenset(),
) -> list[PeerKPIBenchmark]:
    mine = company_kpis(target)
    latest: dict[tuple[str, str, str, str], CanonicalKPIValue] = {}
    for k in mine:  # sorted: the last per identity is the latest period
        latest[(k.canonical_kpi, k.qualifier or "", k.unit.value, k.period_kind.value)] = k
    out = []
    for (code, qualifier, unit, kind), anchor in sorted(latest.items()):
        observations = []
        for peer in peers:
            pick = _pick(anchor, company_kpis(peer))
            if pick is not None and peer.company_key in neutral_only:
                pick = _Pick(pick.value, pick.equivalence, OS.CLASSIFICATION_MISMATCH,
                             "peer compatible at the business-class level only: operating "
                             "KPIs are business-model specific")
            if pick is not None:
                observations.append(
                    _observation(
                        peer, pick.value, pick.equivalence, pick.status, detail=pick.detail
                    )
                )
        exact = [
            o.value
            for o in observations
            if o.equivalence == KPIEquivalence.EXACT and o.status == OS.COMPARABLE
        ]
        directionality = KPI_DIRECTIONALITY.get(code, Directionality.NOT_REGISTERED)
        ranking, withheld = rank(anchor.value, exact, directionality, min_peers)
        if anchor.period.period_end is None and directionality != Directionality.NOT_REGISTERED:
            ranking, withheld = None, "target KPI period end is unknown: no rank"
        warnings = []
        if any(o.equivalence == KPIEquivalence.PROXY for o in observations):
            warnings.append("PROXY_KPI_DISPLAYED_NOT_RANKED")
        if not observations:
            warnings.append("NO_PEER_KPI")
        out.append(
            PeerKPIBenchmark(
                benchmark_id=uuid5(
                    NS,
                    f"kpi|{target.company_key}|{code}|{qualifier}|{unit}|"
                    f"{kind}|{anchor.period.period_end}",
                ),
                kpi_code=code,
                qualifier=anchor.qualifier,
                unit=unit,
                period_kind=kind,
                period_end=anchor.period.period_end,
                directionality=directionality,
                target=_observation(
                    target, anchor, KPIEquivalence.EXACT, OS.COMPARABLE, target=True
                ),
                peers=tuple(observations),
                statistics=statistics(exact, mean=True),
                ranking=ranking,
                rank_withheld_reason=withheld,
                warnings=tuple(warnings),
            )
        )
    return out


def _structure_changed(company: CompanyLayers) -> bool:
    if company.box7 is None or not company.box7.segment_comparability:
        return False
    latest = max(
        company.box7.segment_comparability, key=lambda a: (a.to_period_end, str(a.assessment_id))
    )
    return latest.status in (Comparability.STRUCTURE_CHANGED, Comparability.NOT_COMPARABLE)


def segment_comparisons(
    target: CompanyLayers, peers: list[CompanyLayers]
) -> list[PeerSegmentComparison]:
    if target.box7 is None:
        return []
    out = []
    mine_segments = {
        f"{s.segment_key} ({s.role.value})"
        for s in target.box7.business_structure.segments
        if s.role in _COMPARED_ROLES
    }
    mine_geos = {g.geography_key for g in target.box7.business_structure.geographies}
    for peer in peers:
        if peer.box7 is None:
            continue
        theirs_segments = {
            f"{s.segment_key} ({s.role.value})"
            for s in peer.box7.business_structure.segments
            if s.role in _COMPARED_ROLES
        }
        theirs_geos = {g.geography_key for g in peer.box7.business_structure.geographies}
        changed = _structure_changed(target) or _structure_changed(peer)
        for domain, mine, theirs in (
            ("SEGMENT", mine_segments, theirs_segments),
            ("GEOGRAPHY", mine_geos, theirs_geos),
        ):
            same = sorted(mine & theirs)
            if domain == "SEGMENT" and changed:
                status = SegmentMatch.STRUCTURE_CHANGED
                note = "a Box 7 segment-structure change without restatement blocks comparison"
            elif same:
                status = SegmentMatch.IDENTICAL_NAME
                note = "identical normalized names only; segment definitions are unverified"
            else:
                status = SegmentMatch.UNMATCHED
                note = "no identical segment names; related-sounding names are never mapped"
            out.append(
                PeerSegmentComparison(
                    comparison_id=uuid5(
                        NS, f"segments|{target.company_key}|{peer.company_key}|{domain}"
                    ),
                    peer_company_key=peer.company_key,
                    domain=domain,  # type: ignore[arg-type]
                    identical_names=tuple(same),
                    target_only=tuple(sorted(mine - theirs)),
                    peer_only=tuple(sorted(theirs - mine)),
                    status=status,
                    note=note,
                )
            )
    return out
