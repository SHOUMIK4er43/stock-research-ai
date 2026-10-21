"""DETAILED_RESEARCH_V1 Box 9: peer financial benchmarks, statistics and rankings.

Values are selected from trusted Box 5 calculated metrics / Box 4 canonical values only
(never recalculated). The target's latest period of the metric's period type, in its
analytical basis (select_analytical_basis; never per metric), anchors every comparison:

  * a peer value counts only with the SAME period type, period end and reporting basis
    (different fiscal calendars are NEAR_ALIGNED / NOT_ALIGNED and never pooled)
  * and the IDENTICAL definition: formula (with its variant), building-block variants and
    canonical inputs (revenue from operations / a provider 'Revenue' / total income, total
    PAT / owners' PAT, accounting / ordinary / total equity are never interchangeable)
  * a peer whose Box 4 business model is proven only at the class level (peer_selection
    NEUTRAL_ONLY) counts on business-model-neutral metrics only (CLASSIFICATION_MISMATCH)
  * absolute money compares only within one currency; percentages / ratios may cross
  * a Box 4 value whose selected observation fails extraction integrity is never used
  * ranks need registered directionality and at least `min_peers_for_rank` comparable peers
Only COMPARABLE observations enter statistics and ranks; every other observation is counted
as excluded, by status, and a benchmark with no COMPARABLE peer is UNAVAILABLE.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.financial_analytics import select_analytical_basis
from stock_research_ai.application.financial_metrics import REVENUE
from stock_research_ai.application.financial_reconciliation import extraction_integrity_issue
from stock_research_ai.application.peer_registry import (
    BENCHMARK_METRICS,
    NEAR_ALIGNED_DAYS,
    NS,
    BenchmarkMetric,
    applies,
)
from stock_research_ai.application.peer_selection import CompanyLayers, business_model
from stock_research_ai.domain.financial_analytics import CalculationStatus
from stock_research_ai.domain.financial_canonical import Currency, PeriodType, ReportingBasis
from stock_research_ai.domain.industry_intelligence import (
    Alignment,
    BenchmarkStatistics,
    Directionality,
    EvidenceRef,
    MetricObservation,
    NotApplicableMetric,
    ObservationStatus,
    PeerFinancialBenchmark,
    PositionObservation,
    Ranking,
    SourceLayer,
)

OS = ObservationStatus
_SIX = Decimal("0.000001")


@dataclass(frozen=True)
class Value:
    """One trusted upstream value, as Box 9 sees it."""

    layer: SourceLayer
    record_id: UUID
    dataset_id: UUID
    company_key: str
    code: str
    value: Decimal | None
    calculated: bool
    status_text: str
    unit: str
    period_type: PeriodType
    period_end: date | None
    basis: ReportingBasis
    currency: Currency | None
    upstream: tuple[UUID, ...]
    locator: str | None = None
    #: the top-line definition a Box 5 value rests on (revenue from operations vs a
    #: reported / provider 'Revenue'); peers compare only on the same one
    concept: str | None = None
    #: the exact definition: 'formula <- building-block formulas | canonical inputs'
    definition: str | None = None


_TOPLINE = {m.value for m in REVENUE}


def definition_of(formula_id: str, building_blocks: Iterable[str], inputs: Iterable[str]) -> str:
    return (f"{formula_id} <- {','.join(sorted(set(building_blocks))) or '-'} | "
            f"{','.join(sorted(set(inputs))) or '-'}")


def values_of(company: CompanyLayers, metric: BenchmarkMetric) -> list[Value]:
    out: list[Value] = []
    if metric.layer == "BOX5" and company.box5 is not None:
        formulas = {m.metric_id: m.formula_id for m in company.box5.all_metrics()}
        for m in company.box5.all_metrics():
            if m.code != metric.code:
                continue
            out.append(
                Value(
                    SourceLayer.BOX5_METRIC,
                    m.metric_id,
                    company.box5.analytics_id,
                    company.company_key,
                    m.code,
                    m.value,
                    m.status == CalculationStatus.CALCULATED,
                    m.status.value,
                    m.unit.value,
                    m.period.period_type,
                    m.period.period_end,
                    m.reporting_basis,
                    m.currency,
                    m.input_value_ids,
                    m.formula_id,
                    next((i.metric.value for i in m.provenance if i.metric.value in _TOPLINE),
                         None),
                    definition_of(
                        m.formula_id,
                        (formulas[i] for i in m.input_metric_ids if i in formulas),
                        (i.metric.value for i in m.provenance),
                    ),
                )
            )
    if metric.layer == "BOX4" and company.box4 is not None:
        rejected = {o.observation_id for o in company.box4.observations
                    if extraction_integrity_issue(o) is not None}
        for v in company.box4.canonical_values:
            if v.metric.value != metric.code:
                continue
            valid = v.selected_observation_id not in rejected
            out.append(
                Value(
                    SourceLayer.BOX4_VALUE,
                    v.canonical_value_id,
                    company.box4.dataset_id,
                    company.company_key,
                    v.metric.value,
                    v.value,
                    valid,
                    v.outcome.value if valid else "EXTRACTION_INTEGRITY_REJECTED",
                    v.value_kind.value,
                    v.period.period_type,
                    v.period.period_end,
                    v.reporting_basis,
                    v.currency,
                    (v.selected_observation_id,),
                    v.period.label,
                    None,
                    definition_of(v.metric.value, (), (v.metric.value,)),
                )
            )
    return sorted(out, key=lambda v: (v.period_end or date.min, v.basis.value, str(v.record_id)))


def target_basis(company: CompanyLayers) -> ReportingBasis | None:
    """The target's analytical basis (financial_analytics.select_analytical_basis over its
    Box 4; from its Box 5 frames when no Box 4 is supplied): the one basis of every anchor."""
    if company.box4 is not None:
        chosen = select_analytical_basis(company.box4)
        return chosen[0] if chosen else None
    if company.box5 is not None:
        bases = {f.reporting_basis for f in company.box5.frames}
        for basis in (ReportingBasis.CONSOLIDATED, ReportingBasis.STANDALONE):
            if basis in bases:
                return basis
    return None


def _anchor(values: list[Value], basis: ReportingBasis | None) -> Value | None:
    """The target's latest calculated value of its main period type (FY first, then
    POINT_IN_TIME) in the analytical basis only: a metric missing there is never filled
    from the other basis."""
    for period_type in (PeriodType.FY, PeriodType.POINT_IN_TIME):
        chosen = [
            v
            for v in values
            if v.period_type == period_type
            and v.calculated
            and v.period_end is not None
            and v.value is not None
            and v.basis == basis
        ]
        if chosen:
            return max(chosen, key=lambda v: (v.period_end or date.min, str(v.record_id)))
    return None


def _other_basis_only(values: list[Value], basis: ReportingBasis | None) -> list[str]:
    return sorted({
        v.basis.value for v in values
        if v.basis != basis and v.calculated and v.value is not None
        and v.period_type in (PeriodType.FY, PeriodType.POINT_IN_TIME)
    })


def _ref(v: Value) -> EvidenceRef:
    return EvidenceRef(
        company_key=v.company_key,
        layer=v.layer,
        record_id=v.record_id,
        dataset_id=v.dataset_id,
        locator=v.locator,
    )


def _observation(
    v: Value | None,
    status: ObservationStatus,
    company: str,
    *,
    target: bool = False,
    alignment: Alignment | None = None,
    detail: str | None = None,
    days: int | None = None,
) -> MetricObservation:
    return MetricObservation(
        company_key=company,
        is_target=target,
        status=status,
        value=v.value if v is not None else None,
        unit=v.unit if v else None,
        period_type=v.period_type if v else None,
        period_end=v.period_end if v else None,
        alignment=alignment,
        reporting_basis=v.basis if v else None,
        currency=v.currency if v else None,
        source=_ref(v) if v else None,
        upstream_ids=v.upstream if v else (),
        detail=detail,
        definition=v.definition if v else None,
        alignment_days=days,
    )


def peer_observation(
    anchor: Value, metric: BenchmarkMetric, peer: CompanyLayers, *, neutral_only: bool = False
) -> MetricObservation:
    """One peer's observation; COMPARABLE only when every dimension matches the anchor."""
    key = peer.company_key
    if neutral_only and not metric.neutral:
        return _observation(
            None,
            OS.CLASSIFICATION_MISMATCH,
            key,
            detail=f"peer compatible at the business-class level only: {metric.code} is "
            "not business-model neutral",
        )
    values = values_of(peer, metric)
    if not values:
        return _observation(None, OS.MISSING, key, detail=f"no {metric.layer} {metric.code}")
    typed = [v for v in values if v.period_type == anchor.period_type and v.period_end]
    if not typed:
        return _observation(
            None, OS.PERIOD_MISMATCH, key, detail=f"no {anchor.period_type.value} value"
        )
    same_basis = [v for v in typed if v.basis == anchor.basis]
    if not same_basis:
        bases = sorted({v.basis.value for v in typed})
        return _observation(
            max(typed, key=lambda v: v.period_end or date.min),
            OS.BASIS_MISMATCH,
            key,
            detail=f"only {', '.join(bases)} values",
        )
    anchor_end = anchor.period_end
    assert anchor_end is not None
    exact = [v for v in same_basis if v.period_end == anchor_end]
    if not exact:
        nearest = min(
            same_basis,
            key=lambda v: (abs(((v.period_end or date.min) - anchor_end).days), str(v.record_id)),
        )
        gap = abs(((nearest.period_end or date.min) - anchor_end).days)
        alignment = Alignment.NEAR_ALIGNED if gap <= NEAR_ALIGNED_DAYS else Alignment.NOT_ALIGNED
        return _observation(
            nearest,
            OS.PERIOD_MISMATCH,
            key,
            alignment=alignment,
            detail=f"period end {nearest.period_end} vs {anchor.period_end} ({gap} days; "
            "never pooled)",
            days=gap,
        )
    v = exact[0]
    if not v.calculated or v.value is None:
        return _observation(
            v,
            OS.NOT_CALCULATED,
            key,
            alignment=Alignment.ALIGNED,
            detail=f"upstream status {v.status_text}",
            days=0,
        )
    if v.concept != anchor.concept:
        return _observation(
            v,
            OS.DEFINITION_MISMATCH,
            key,
            alignment=Alignment.ALIGNED,
            detail=f"top line {v.concept} vs {anchor.concept}",
            days=0,
        )
    if v.definition != anchor.definition:
        return _observation(
            v,
            OS.DEFINITION_MISMATCH,
            key,
            alignment=Alignment.ALIGNED,
            detail=f"definition {v.definition} vs {anchor.definition}",
            days=0,
        )
    if metric.money and v.currency != anchor.currency:
        return _observation(
            v,
            OS.CURRENCY_MISMATCH,
            key,
            alignment=Alignment.ALIGNED,
            detail=f"{v.currency} vs {anchor.currency}: no trusted FX layer",
            days=0,
        )
    if v.unit != anchor.unit:
        return _observation(
            v,
            OS.UNIT_MISMATCH,
            key,
            alignment=Alignment.ALIGNED,
            detail=f"{v.unit} vs {anchor.unit}",
            days=0,
        )
    return _observation(v, OS.COMPARABLE, key, alignment=Alignment.ALIGNED, days=0)


def statistics(values: list[Decimal], *, mean: bool) -> BenchmarkStatistics:
    if not values:
        return BenchmarkStatistics(peer_count=0, minimum=None, maximum=None, median=None, mean=None)
    ordered = sorted(values)
    n = len(ordered)
    middle = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / Decimal(2)
    return BenchmarkStatistics(
        peer_count=n,
        minimum=ordered[0],
        maximum=ordered[-1],
        median=middle.quantize(_SIX, rounding=ROUND_HALF_EVEN),
        mean=(sum(ordered, Decimal(0)) / Decimal(n)).quantize(_SIX, rounding=ROUND_HALF_EVEN)
        if mean
        else None,
    )


def rank(
    target: Decimal, peers: list[Decimal], directionality: Directionality, minimum: int
) -> tuple[Ranking | None, str | None]:
    if directionality == Directionality.NOT_REGISTERED:
        return None, "directionality is not registered for this metric: values only"
    if len(peers) < minimum:
        return None, f"{len(peers)} comparable peers < {minimum}: no rank"
    higher = directionality == Directionality.HIGHER_IS_BETTER
    better = sum(1 for p in peers if (p > target if higher else p < target))
    worse = sum(1 for p in peers if (p < target if higher else p > target))
    ties = sum(1 for p in peers if p == target)
    return (
        Ranking(
            directionality=directionality,
            rank=better + 1,
            of=len(peers) + 1,
            percentile=(Decimal(worse) * 100 / len(peers)).quantize(Decimal("0.01")),
            ties=ties,
        ),
        None,
    )


def financial_benchmarks(
    target: CompanyLayers,
    peers: list[CompanyLayers],
    family: str,
    min_peers: int,
    neutral_only: frozenset[str] = frozenset(),
) -> tuple[list[PeerFinancialBenchmark], list[NotApplicableMetric], list[tuple[str, str]]]:
    """(benchmarks, sector-blocked metrics, warnings). A metric the target has only on the
    other basis gets no benchmark and a TARGET_BASIS_UNAVAILABLE warning. `neutral_only`:
    peer keys compatible at the business-class level only (business-neutral metrics)."""
    benchmarks: list[PeerFinancialBenchmark] = []
    blocked: list[NotApplicableMetric] = []
    warnings: list[tuple[str, str]] = []
    basis = target_basis(target)
    model, _subtype = business_model(target.box4)
    for metric in BENCHMARK_METRICS:
        if not applies(metric, family, model):
            scope = metric.families + (f" / {', '.join(metric.models)}" if metric.models else "")
            blocked.append(
                NotApplicableMetric(
                    metric_code=metric.code,
                    sector_family=family,
                    reason=f"{metric.code} is registered for {scope} companies only "
                    f"(target {model})",
                )
            )
            continue
        values = values_of(target, metric)
        anchor = _anchor(values, basis)
        if anchor is None or anchor.period_end is None or anchor.value is None:
            others = _other_basis_only(values, basis)
            if others:
                warnings.append((
                    "TARGET_BASIS_UNAVAILABLE",
                    f"{metric.code}: no calculated {basis.value if basis else 'known-basis'} "
                    f"value; {', '.join(others)} not substituted",
                ))
            continue
        observations = [
            peer_observation(anchor, metric, p, neutral_only=p.company_key in neutral_only)
            for p in peers
        ]
        comparable = [
            o.value for o in observations if o.status == OS.COMPARABLE and o.value is not None
        ]
        excluded = Counter(o.status.value for o in observations if o.status != OS.COMPARABLE)
        ranking, withheld = rank(anchor.value, comparable, metric.directionality, min_peers)
        benchmarks.append(
            PeerFinancialBenchmark(
                benchmark_id=uuid5(
                    NS,
                    f"benchmark|{target.company_key}|{metric.code}|"
                    f"{anchor.period_type.value}|{anchor.period_end}|"
                    f"{anchor.basis.value}",
                ),
                metric_code=metric.code,
                layer="BOX5" if metric.layer == "BOX5" else "BOX4",
                unit=anchor.unit,
                period_type=anchor.period_type,
                period_end=anchor.period_end,
                reporting_basis=anchor.basis,
                directionality=metric.directionality,
                target=_observation(
                    anchor,
                    OS.COMPARABLE,
                    target.company_key,
                    target=True,
                    alignment=Alignment.ALIGNED,
                    days=0,
                ),
                peers=tuple(observations),
                statistics=statistics(comparable, mean=not metric.money),
                ranking=ranking,
                rank_withheld_reason=withheld,
                missing_peers=tuple(o.company_key for o in observations if o.status == OS.MISSING),
                definition=anchor.definition,
                excluded_peers=sum(excluded.values()),
                exclusion_counts=dict(sorted(excluded.items())),
                availability="AVAILABLE" if comparable else "UNAVAILABLE",
                unavailable_reason=None
                if comparable
                else "no eligible peer: "
                + (", ".join(f"{k} {n}" for k, n in sorted(excluded.items())) or "no peers"),
            )
        )
    return benchmarks, blocked, warnings


_DIMENSIONS = {m.code: m.dimension for m in BENCHMARK_METRICS}


def positions(
    target_name: str, benchmarks: Iterable[PeerFinancialBenchmark]
) -> list[PositionObservation]:
    out = []
    for b in benchmarks:
        if b.statistics.peer_count == 0 or b.target.value is None:
            continue
        text = (
            f"{target_name} {b.period_type.value} {b.period_end} {b.metric_code} = "
            f"{b.target.value} {b.unit}; peer median = {b.statistics.median} "
            f"(n = {b.statistics.peer_count} comparable peers)"
        )
        if b.ranking is not None:
            text += f"; rank = {b.ranking.rank}/{b.ranking.of} ({b.directionality.value})"
        else:
            text += f"; no rank: {b.rank_withheld_reason}"
        out.append(
            PositionObservation(
                observation_id=uuid5(NS, f"position|{b.benchmark_id}"),
                dimension=_DIMENSIONS.get(b.metric_code, "OTHER"),
                metric_code=b.metric_code,
                benchmark_id=b.benchmark_id,
                text=text,
            )
        )
    return out
