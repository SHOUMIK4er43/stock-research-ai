"""DETAILED_RESEARCH_V1 Box 7: reconciliation of segment / geography / KPI observations.

Comparability keys never mix entity, metric, period, basis, currency, unit or (for
geography) attribution method. Within a key:
  * one eligible observation                     -> SINGLE_SOURCE
  * all agree exactly / within display rounding  -> EXACT_MATCH / MATCH_WITHIN_TOLERANCE
  * one presentation repeats the key differently -> AMBIGUOUS (no canonical value)
  * a later filing re-presents the period         -> RESTATEMENT (later wins), unless a
    structure change lies between the filings and comparatives were not restated
                                                  -> NOT_COMPARABLE (later kept, explicit)
  * same filing period, different authority       -> SOURCE_PRECEDENCE (higher wins)
  * same filing period, same authority, differ    -> CONFLICT (no canonical value)
LOW-confidence observations are preserved but never canonical.
"""

from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.business_lines import NS
from stock_research_ai.application.business_registry import bridge_sign, kpi_definition, name_key
from stock_research_ai.domain.business_intelligence import (
    CanonicalGeographyValue,
    CanonicalKPIValue,
    CanonicalSegmentValue,
    Domain,
    GeographyMetric,
    GeographyObservation,
    KPIPeriodKind,
    KPIUnit,
    OperatingKPIObservation,
    ReconciliationRecord,
    ReconOutcome,
    SegmentLevel,
    SegmentMetric,
    SegmentObservation,
    SegmentRole,
    TotalAdjustment,
    TotalOutcome,
    TotalReconciliation,
)
from stock_research_ai.domain.evidence import AUTHORITY_RANK, SourceAuthority
from stock_research_ai.domain.financial_canonical import (
    CANONICAL_SCALE,
    SCALE_FACTOR,
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    PeriodType,
    ReportingBasis,
    Scale,
    ValueKind,
)

#: Box 4's company revenue concept, preferred first (banks report REVENUE)
REVENUE_METRICS = (CanonicalMetric.REVENUE_FROM_OPERATIONS, CanonicalMetric.REVENUE)
_CONFIDENCE_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}


@dataclass(frozen=True)
class Item:
    id: UUID
    key: tuple[Hashable, ...]
    value: Decimal
    tolerance: Decimal
    authority: SourceAuthority
    confidence: Confidence
    document_id: UUID | None
    frame_id: UUID
    restated: bool
    reference_end: date | None  # the latest period the item's document reports
    period_end: date | None


@dataclass(frozen=True)
class Decision:
    record: ReconciliationRecord
    selected: Item | None
    supporting: tuple[UUID, ...]
    conflicting: tuple[UUID, ...]


def tolerance(decimals: int, scale: Scale, money: bool) -> Decimal:
    half = Decimal(5) / Decimal(10) ** (decimals + 1)
    if money and scale in SCALE_FACTOR:
        return half * SCALE_FACTOR[scale] / SCALE_FACTOR[CANONICAL_SCALE]
    return half


def _best(items: Sequence[Item]) -> Item:
    return min(
        items,
        key=lambda i: (
            AUTHORITY_RANK[i.authority],
            _CONFIDENCE_RANK[i.confidence],
            -(i.reference_end.toordinal() if i.reference_end else 0),
            str(i.id),
        ),
    )


def _agree(items: Sequence[Item]) -> tuple[bool, Decimal, Decimal]:
    values = [i.value for i in items]
    spread = max(values) - min(values)
    allowed = max(i.tolerance for i in items) * 2
    return spread <= allowed, spread, allowed


def reconcile(
    items: Sequence[Item],
    domain: Domain,
    describe: Callable[[tuple[Hashable, ...]], str],
    structure_changed: Callable[[date | None, date | None], bool],
) -> list[Decision]:
    groups: dict[tuple[Hashable, ...], list[Item]] = {}
    for item in items:
        groups.setdefault(item.key, []).append(item)
    decisions: list[Decision] = []
    for key, members in sorted(groups.items(), key=lambda kv: describe(kv[0])):
        members = sorted(members, key=lambda i: str(i.id))
        label = describe(key)
        record_id = uuid5(NS, f"record|{domain.value}|{label}")
        ids = tuple(i.id for i in members)

        def decide(
            outcome: ReconOutcome,
            selected: Item | None,
            reason: str,
            conflicting: Sequence[Item] = (),
            difference: Decimal | None = None,
            allowed: Decimal | None = None,
            members: Sequence[Item] = members,
            record_id: UUID = record_id,
            label: str = label,
            ids: tuple[UUID, ...] = ids,
        ) -> Decision:
            conflict_ids = tuple(sorted(i.id for i in conflicting))
            support = tuple(
                i.id
                for i in members
                if selected is not None and i.id != selected.id and i.id not in conflict_ids
            )
            return Decision(
                ReconciliationRecord(
                    record_id=record_id,
                    domain=domain,
                    key=label[:1000],
                    outcome=outcome,
                    observation_ids=ids,
                    selected_observation_id=selected.id if selected else None,
                    difference=difference,
                    tolerance=allowed,
                    reason=reason[:1000],
                ),
                selected,
                support,
                conflict_ids,
            )

        if len(members) == 1:
            decisions.append(decide(ReconOutcome.SINGLE_SOURCE, members[0], "one source"))
            continue
        ok, spread, allowed = _agree(members)
        if ok:
            outcome = (
                ReconOutcome.EXACT_MATCH if spread == 0 else ReconOutcome.MATCH_WITHIN_TOLERANCE
            )
            decisions.append(
                decide(
                    outcome,
                    _best(members),
                    f"{len(members)} sources agree",
                    difference=spread,
                    allowed=allowed,
                )
            )
            continue
        frames: dict[UUID, list[Item]] = {}
        for item in members:
            frames.setdefault(item.frame_id, []).append(item)
        if any(len(v) > 1 and not _agree(v)[0] for v in frames.values()):
            decisions.append(
                decide(
                    ReconOutcome.AMBIGUOUS,
                    None,
                    "one presentation prints this key twice with different values",
                    difference=spread,
                    allowed=allowed,
                )
            )
            continue
        latest_ref = max((i.reference_end for i in members if i.reference_end), default=None)
        newest = [i for i in members if i.reference_end == latest_ref]
        older = [i for i in members if i.reference_end != latest_ref]
        if latest_ref is not None and older and _agree(newest)[0]:
            best_newest = _best(newest)
            if AUTHORITY_RANK[best_newest.authority] <= min(
                AUTHORITY_RANK[i.authority] for i in older
            ):
                oldest_ref = min((i.reference_end for i in older if i.reference_end), default=None)
                stale = [i for i in older if abs(i.value - best_newest.value) > allowed]
                if structure_changed(oldest_ref, latest_ref) and not any(
                    i.restated for i in newest
                ):
                    decisions.append(
                        decide(
                            ReconOutcome.NOT_COMPARABLE,
                            best_newest,
                            "the later filing presents this period under a changed segment "
                            "structure without restated comparatives; the later definition is "
                            "kept and the earlier value is not reconciled to it",
                            stale,
                            spread,
                            allowed,
                        )
                    )
                    continue
                decisions.append(
                    decide(
                        ReconOutcome.RESTATEMENT,
                        best_newest,
                        f"re-presented in a later filing (reporting to {latest_ref}); the later "
                        "figure supersedes",
                        stale,
                        spread,
                        allowed,
                    )
                )
                continue
        tiers = sorted({AUTHORITY_RANK[i.authority] for i in members})
        top = [i for i in members if AUTHORITY_RANK[i.authority] == tiers[0]]
        if len(tiers) > 1 and _agree(top)[0]:
            chosen = _best(top)
            others = [i for i in members if abs(i.value - chosen.value) > allowed]
            decisions.append(
                decide(
                    ReconOutcome.SOURCE_PRECEDENCE,
                    chosen,
                    f"{chosen.authority.value} outranks the differing source(s)",
                    others,
                    spread,
                    allowed,
                )
            )
            continue
        decisions.append(
            decide(
                ReconOutcome.CONFLICT,
                None,
                "sources of equal authority disagree; no value chosen",
                members,
                spread,
                allowed,
            )
        )
    return decisions


# --- domain wrappers -------------------------------------------------------------------------


def _period_text(period: FinancialPeriod) -> str:
    return f"{period.period_type.value}|{period.period_end}|{period.duration_months}"


def segment_eligible(o: SegmentObservation) -> bool:
    return (
        o.normalized_value is not None
        and o.confidence != Confidence.LOW
        and o.reporting_basis != ReportingBasis.UNKNOWN
        and o.period.period_type != PeriodType.UNKNOWN
        and o.value_kind in (ValueKind.MONEY, ValueKind.PERCENT)
        and (o.value_kind != ValueKind.MONEY or o.currency != Currency.UNKNOWN)
    )


def segment_key(o: SegmentObservation, canonical: str) -> tuple[Hashable, ...]:
    metric = o.metric.value
    if o.metric == SegmentMetric.OTHER:  # unregistered metrics stay apart by their label
        metric = f"OTHER:{name_key(o.raw_metric_label)}"
    return (
        canonical,
        o.segment_role.value,
        metric,
        _period_text(o.period),
        o.reporting_basis.value,
        o.currency.value,
        o.value_kind.value,
    )


def canonical_segments(
    observations: Sequence[SegmentObservation],
    canonical_key: Callable[[SegmentObservation], str],
    reference: dict[UUID, date],
    structure_changed: Callable[[date | None, date | None], bool],
    names: dict[str, str],
) -> tuple[list[ReconciliationRecord], list[CanonicalSegmentValue]]:
    index = {o.observation_id: o for o in observations}
    items = [
        Item(
            o.observation_id,
            segment_key(o, canonical_key(o)),
            o.normalized_value,
            tolerance(o.parsed.decimals, o.scale, o.value_kind == ValueKind.MONEY),
            o.source.authority,
            o.confidence,
            o.source.document_id,
            o.frame_id,
            o.restated,
            reference.get(o.source.document_id) if o.source.document_id else None,
            o.period.period_end,
        )
        for o in observations
        if segment_eligible(o) and o.normalized_value is not None
    ]
    decisions = reconcile(
        items, Domain.SEGMENT, lambda k: "SEGMENT|" + "|".join(map(str, k)), structure_changed
    )
    values: list[CanonicalSegmentValue] = []
    for d in decisions:
        if d.selected is None:
            continue
        o = index[d.selected.id]
        key = str(d.selected.key[0])
        values.append(
            CanonicalSegmentValue(
                canonical_value_id=uuid5(NS, f"canonical|{d.record.record_id}"),
                segment_key=key,
                segment_name=names.get(key, o.segment_raw_name),
                segment_role=o.segment_role,
                metric=o.metric,
                value=d.selected.value,
                value_kind=o.value_kind,
                currency=o.currency,
                scale=CANONICAL_SCALE if o.value_kind == ValueKind.MONEY else Scale.ONES,
                period=o.period,
                reporting_basis=o.reporting_basis,
                selected_observation_id=o.observation_id,
                supporting_observation_ids=d.supporting,
                conflicting_observation_ids=d.conflicting,
                reconciliation_record_id=d.record.record_id,
                outcome=d.record.outcome,
                confidence=o.confidence,
            )
        )
    return [d.record for d in decisions], values


def canonical_geographies(
    observations: Sequence[GeographyObservation],
    reference: dict[UUID, date],
) -> tuple[list[ReconciliationRecord], list[CanonicalGeographyValue]]:
    index = {o.observation_id: o for o in observations}
    items = [
        Item(
            o.observation_id,
            (
                o.geography_key,
                o.attribution.value,
                o.metric.value,
                _period_text(o.period),
                o.reporting_basis.value,
                o.currency.value,
                o.value_kind.value,
            ),
            o.normalized_value,
            tolerance(o.parsed.decimals, o.scale, o.value_kind == ValueKind.MONEY),
            o.source.authority,
            o.confidence,
            o.source.document_id,
            o.frame_id,
            False,
            reference.get(o.source.document_id) if o.source.document_id else None,
            o.period.period_end,
        )
        for o in observations
        if o.normalized_value is not None
        and o.confidence != Confidence.LOW
        and o.reporting_basis != ReportingBasis.UNKNOWN
        and o.period.period_type != PeriodType.UNKNOWN
        and o.value_kind in (ValueKind.MONEY, ValueKind.PERCENT)
    ]
    decisions = reconcile(
        items,
        Domain.GEOGRAPHY,
        lambda k: "GEOGRAPHY|" + "|".join(map(str, k)),
        lambda _a, _b: False,
    )
    values: list[CanonicalGeographyValue] = []
    for d in decisions:
        if d.selected is None:
            continue
        o = index[d.selected.id]
        values.append(
            CanonicalGeographyValue(
                canonical_value_id=uuid5(NS, f"canonical|{d.record.record_id}"),
                geography_key=o.geography_key,
                geography_name=o.geography_raw_name,
                dimension=o.dimension,
                attribution=o.attribution,
                metric=o.metric,
                value=d.selected.value,
                value_kind=o.value_kind,
                currency=o.currency,
                scale=CANONICAL_SCALE if o.value_kind == ValueKind.MONEY else Scale.ONES,
                period=o.period,
                reporting_basis=o.reporting_basis,
                is_total=o.is_total,
                selected_observation_id=o.observation_id,
                supporting_observation_ids=d.supporting,
                conflicting_observation_ids=d.conflicting,
                reconciliation_record_id=d.record.record_id,
                outcome=d.record.outcome,
                confidence=o.confidence,
            )
        )
    return [d.record for d in decisions], values


def kpi_time_key(o: OperatingKPIObservation) -> str:
    if o.period_kind == KPIPeriodKind.POINT_IN_TIME:
        return f"ASOF|{o.as_of}"
    assert o.period is not None
    return _period_text(o.period)


def kpi_eligible(o: OperatingKPIObservation) -> bool:
    return (
        o.normalized_value is not None
        and o.confidence != Confidence.LOW
        and o.unit != KPIUnit.UNKNOWN
        and o.period is not None
        and o.period_kind != KPIPeriodKind.UNKNOWN
        and (o.period_kind != KPIPeriodKind.POINT_IN_TIME or o.as_of is not None)
    )


def canonical_kpis(
    observations: Sequence[OperatingKPIObservation],
    reference: dict[UUID, date],
    box4: CanonicalFinancialDataset,
) -> tuple[list[ReconciliationRecord], list[CanonicalKPIValue], list[str]]:
    index = {o.kpi_observation_id: o for o in observations}
    items = [
        Item(
            o.kpi_observation_id,
            (
                o.canonical_kpi,
                o.qualifier,
                o.segment_key,
                o.geography_key,
                o.unit.value,
                (o.currency.value if o.currency else None),
                o.period_kind.value,
                kpi_time_key(o),
                o.reporting_basis.value,
            ),
            o.normalized_value,
            tolerance(o.parsed.decimals, o.scale, o.unit == KPIUnit.MONEY),
            o.source.authority,
            o.confidence,
            o.source.document_id,
            o.frame_id,
            False,
            reference.get(o.source.document_id) if o.source.document_id else None,
            o.as_of or (o.period.period_end if o.period else None),
        )
        for o in observations
        if kpi_eligible(o) and o.normalized_value is not None
    ]
    decisions = reconcile(
        items, Domain.KPI, lambda k: "KPI|" + "|".join(map(str, k)), lambda _a, _b: False
    )
    box4_values = {
        (v.metric, v.period.period_end, v.reporting_basis, v.currency): v
        for v in box4.canonical_values
    }
    conflicts: list[str] = []
    values: list[CanonicalKPIValue] = []
    for d in decisions:
        if d.selected is None:
            continue
        o = index[d.selected.id]
        assert o.period is not None
        definition = kpi_definition(o.canonical_kpi)
        reference_id = comparison = None
        if definition.box4_metric is not None and o.unit == KPIUnit.MONEY and o.currency:
            end = o.as_of or o.period.period_end
            found = box4_values.get((definition.box4_metric, end, o.reporting_basis, o.currency))
            if found is not None:
                reference_id = found.canonical_value_id
                difference = abs(found.value - d.selected.value)
                if difference == 0:
                    comparison = "EXACT_MATCH"
                elif difference <= d.selected.tolerance * 2:
                    comparison = "MATCH_WITHIN_TOLERANCE"
                else:
                    comparison = "CONFLICT"
                    conflicts.append(
                        f"{o.canonical_kpi} {end}: Box 7 {d.selected.value} vs Box 4 {found.value}"
                    )
        values.append(
            CanonicalKPIValue(
                canonical_value_id=uuid5(NS, f"canonical|{d.record.record_id}"),
                canonical_kpi=o.canonical_kpi,
                qualifier=o.qualifier,
                segment_key=o.segment_key,
                geography_key=o.geography_key,
                value=d.selected.value,
                unit=o.unit,
                currency=o.currency,
                scale=(
                    CANONICAL_SCALE
                    if o.unit == KPIUnit.MONEY
                    else o.scale
                    if o.unit in (KPIUnit.PERCENT, KPIUnit.BASIS_POINTS, KPIUnit.MONEY_PER_UNIT)
                    else Scale.ONES
                ),
                period=o.period,
                period_kind=o.period_kind,
                reporting_basis=o.reporting_basis,
                selected_observation_id=o.kpi_observation_id,
                supporting_observation_ids=d.supporting,
                conflicting_observation_ids=d.conflicting,
                reconciliation_record_id=d.record.record_id,
                outcome=d.record.outcome,
                confidence=o.confidence,
                box4_canonical_value_id=reference_id,
                box4_comparison=comparison,
            )
        )
    return [d.record for d in decisions], values, conflicts


# --- totals ----------------------------------------------------------------------------------


def _sum_tolerance(items: Sequence[SegmentObservation | GeographyObservation]) -> Decimal:
    return sum(
        (tolerance(i.parsed.decimals, i.scale, i.value_kind == ValueKind.MONEY) for i in items),
        Decimal(0),
    )


def _printed_half_unit(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return Decimal(5) / Decimal(10) ** (places + 1)


def _outcome(difference: Decimal, allowed: Decimal) -> TotalOutcome:
    if difference == 0:
        return TotalOutcome.MATCH
    if abs(difference) <= allowed:
        return TotalOutcome.MATCH_WITHIN_TOLERANCE
    return TotalOutcome.CONFLICT


def segment_totals(
    observations: Sequence[SegmentObservation],
    box4: CanonicalFinancialDataset,
    financial: bool = False,
) -> list[TotalReconciliation]:
    """Per presentation (frame), period and summable metric:
    1. sum of reportable segments vs the table's own total row;
    2. for revenue / result: the bridge to the company figure - the table's company
       line ('Revenue from operations', 'Profit before tax') or else Box 4 - with the
       printed eliminations / unallocated / reconciling items. Values are never changed."""
    checks: list[TotalReconciliation] = []
    groups: dict[tuple[UUID, str, str], list[SegmentObservation]] = {}
    for o in observations:
        if (
            o.normalized_value is None
            or o.value_kind != ValueKind.MONEY
            or o.metric == SegmentMetric.OTHER
        ):
            continue
        groups.setdefault((o.frame_id, o.metric.value, _period_text(o.period)), []).append(o)
    box4_index = {
        (v.metric, v.period.period_end, v.period.period_type, v.reporting_basis, v.currency): v
        for v in box4.canonical_values
    }
    for (frame_id, metric_code, _period), members in sorted(
        groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2])
    ):
        metric = SegmentMetric(metric_code)
        parts = [
            o
            for o in members
            if o.segment_role == SegmentRole.REPORTABLE and o.segment_level == SegmentLevel.PRIMARY
        ]
        if len(parts) < 2:
            continue
        sample = parts[0]
        component_sum = sum((o.normalized_value or Decimal(0) for o in parts), Decimal(0))
        allowed = _sum_tolerance(parts)
        totals = [o for o in members if o.segment_role == SegmentRole.TOTAL]
        base = dict(
            domain=Domain.SEGMENT,
            metric=metric.value,
            period=sample.period,
            reporting_basis=sample.reporting_basis,
            currency=sample.currency,
            frame_id=frame_id,
            component_sum=component_sum,
            component_ids=tuple(sorted(o.observation_id for o in parts)),
        )
        if totals:
            total = totals[0]
            assert total.normalized_value is not None
            difference = component_sum - total.normalized_value
            tol = allowed + _sum_tolerance([total])
            checks.append(
                TotalReconciliation(
                    check_id=uuid5(NS, f"total|{frame_id}|{metric.value}|{_period}|TABLE"),
                    reported_total=total.normalized_value,
                    reported_total_source="TABLE_TOTAL",
                    reported_total_ref=total.observation_id,
                    difference=difference,
                    tolerance=tol,
                    outcome=_outcome(difference, tol),
                    explanation="sum of reportable segments vs the table's total line",
                    **base,  # type: ignore[arg-type]
                )
            )
        company = [o for o in members if o.segment_role == SegmentRole.COMPANY_TOTAL]
        if metric not in (SegmentMetric.REVENUE, SegmentMetric.SEGMENT_RESULT, SegmentMetric.EBIT):
            if not company:
                continue  # assets / liabilities: only the table's own company line counts
        bridge = sorted(
            (
                o
                for o in members
                if o.segment_role
                in (SegmentRole.ELIMINATION, SegmentRole.UNALLOCATED, SegmentRole.RECONCILING_ITEM)
            ),
            key=lambda o: (o.source.row or 0, str(o.observation_id)),
        )
        # the bridge ends at the printed company line, or else at the first printed
        # subtotal ('Operating Profit'); lines below it (taxes, net profit) never join
        if company and company[-1].source.row is not None:
            stop = company[-1].source.row
            bridge = [o for o in bridge if (o.source.row or 0) < stop]
        else:
            first = next((o for o in bridge if bridge_sign(o.segment_raw_name) == 0), None)
            if first is not None:
                bridge = [o for o in bridge if (o.source.row or 0) <= (first.source.row or 0)]
        target: Decimal | None = None
        source, ref = "NONE", None
        if company and company[-1].normalized_value is not None:
            target, source, ref = (
                company[-1].normalized_value,
                "TABLE_COMPANY_LINE",
                (company[-1].observation_id),
            )
        elif financial and metric == SegmentMetric.REVENUE:
            checks.append(
                TotalReconciliation(
                    check_id=uuid5(NS, f"total|{frame_id}|{metric.value}|{_period}|COMPANY"),
                    reported_total=None,
                    reported_total_source="NONE",
                    reported_total_ref=None,
                    difference=None,
                    tolerance=None,
                    outcome=TotalOutcome.NOT_COMPARABLE,
                    explanation="a bank's segment revenue (total income) is not Box 4 revenue "
                    "(interest earned); not reconciled",
                    **base,  # type: ignore[arg-type]
                )
            )
            continue
        else:
            candidates = (
                REVENUE_METRICS if metric == SegmentMetric.REVENUE else (CanonicalMetric.PBT,)
            )
            found = next(
                (
                    f
                    for m in candidates
                    if (
                        f := box4_index.get(
                            (
                                m,
                                sample.period.period_end,
                                sample.period.period_type,
                                sample.reporting_basis,
                                sample.currency,
                            )
                        )
                    )
                    is not None
                ),
                None,
            )
            if found is not None:
                target, source, ref = found.value, "BOX4_CANONICAL", found.canonical_value_id
        adjustments: list[TotalAdjustment] = []
        unsigned: list[str] = []
        subtotals: list[str] = []
        for o in bridge:
            assert o.normalized_value is not None
            sign = 1 if o.normalized_value < 0 else bridge_sign(o.segment_raw_name)
            if (
                sign is None
                and o.segment_role == SegmentRole.UNALLOCATED
                and metric
                in (
                    SegmentMetric.ASSETS,
                    SegmentMetric.LIABILITIES,
                    SegmentMetric.CAPITAL_EMPLOYED,
                )
            ):
                sign = 1  # unallocated assets / liabilities add to the company total
            if sign == 0:
                subtotals.append(o.segment_raw_name)
                continue
            if sign is None:
                unsigned.append(o.segment_raw_name)
                continue
            signed = o.normalized_value if o.normalized_value < 0 else o.normalized_value * sign
            adjustments.append(
                TotalAdjustment(
                    label=o.segment_raw_name[:1000],
                    role=o.segment_role,
                    signed_value=signed,
                    observation_id=o.observation_id,
                )
            )
        if target is None:
            elsewhere = any(
                key[1:4]
                == (sample.period.period_end, sample.period.period_type, sample.reporting_basis)
                for key in box4_index
                if key[0] in (*REVENUE_METRICS, CanonicalMetric.PBT)
            )
            checks.append(
                TotalReconciliation(
                    check_id=uuid5(NS, f"total|{frame_id}|{metric.value}|{_period}|COMPANY"),
                    reported_total=None,
                    reported_total_source="NONE",
                    reported_total_ref=None,
                    adjustments=tuple(adjustments),
                    difference=None,
                    tolerance=None,
                    outcome=(
                        TotalOutcome.NOT_COMPARABLE if elsewhere else TotalOutcome.INSUFFICIENT_DATA
                    ),
                    explanation=(
                        "the company figure exists only in another currency; never converted"
                        if elsewhere
                        else "no company figure in the table or in Box 4 for this period/basis"
                    ),
                    **base,  # type: ignore[arg-type]
                )
            )
            continue
        tol = allowed * 2 + sum((_sum_tolerance([o]) for o in bridge), Decimal(0))
        if source == "BOX4_CANONICAL":
            tol += _printed_half_unit(target)  # Box 4 / Screener rounding of the company figure
        raw_difference = component_sum - target
        eliminations = sum(
            (a.signed_value for a in adjustments if a.role == SegmentRole.ELIMINATION), Decimal(0)
        )
        everything = sum((a.signed_value for a in adjustments), Decimal(0))
        if abs(raw_difference) <= tol:
            outcome = _outcome(raw_difference, tol)
            explanation = f"segments sum to the company figure ({source})"
        elif eliminations and abs(raw_difference + eliminations) <= tol:
            outcome = TotalOutcome.ELIMINATIONS_EXPLAIN_DIFFERENCE
            explanation = f"printed eliminations {eliminations} bridge the difference ({source})"
        elif adjustments and abs(raw_difference + everything) <= tol:
            outcome = TotalOutcome.UNALLOCATED_EXPLAINS_DIFFERENCE
            explanation = (
                f"printed unallocated / reconciling items {everything} bridge the "
                f"difference ({source})"
            )
        else:
            outcome = TotalOutcome.CONFLICT
            explanation = f"difference {raw_difference} not explained by printed items ({source})"
        if subtotals:
            explanation += f"; printed subtotals not added: {', '.join(subtotals)[:200]}"
        if unsigned:
            explanation += f"; items without a stated sign ignored: {', '.join(unsigned)[:300]}"
        checks.append(
            TotalReconciliation(
                check_id=uuid5(NS, f"total|{frame_id}|{metric.value}|{_period}|COMPANY"),
                reported_total=target,
                reported_total_source=source,
                reported_total_ref=ref,
                adjustments=tuple(adjustments),
                difference=raw_difference,
                tolerance=tol,
                outcome=outcome,
                explanation=explanation[:1000],
                **base,  # type: ignore[arg-type]
            )
        )
    # hierarchy: subsegments vs their parent line
    parents: dict[tuple[UUID, str, str, str], list[SegmentObservation]] = {}
    for o in observations:
        if (
            o.segment_role == SegmentRole.SUBSEGMENT
            and o.segment_parent_key
            and o.normalized_value is not None
        ):
            parents.setdefault(
                (o.frame_id, o.metric.value, _period_text(o.period), o.segment_parent_key), []
            ).append(o)
    for (frame_id, metric_code, period_text, parent), children in sorted(
        parents.items(), key=lambda kv: tuple(map(str, kv[0]))
    ):
        parent_rows = [
            o
            for o in observations
            if o.frame_id == frame_id
            and o.segment_key == parent
            and o.metric.value == metric_code
            and _period_text(o.period) == period_text
            and o.segment_role == SegmentRole.REPORTABLE
        ]
        if not parent_rows or parent_rows[0].normalized_value is None:
            continue
        component_sum = sum((c.normalized_value or Decimal(0) for c in children), Decimal(0))
        difference = component_sum - parent_rows[0].normalized_value
        tol = _sum_tolerance([*children, parent_rows[0]])
        checks.append(
            TotalReconciliation(
                check_id=uuid5(NS, f"total|{frame_id}|{metric_code}|{period_text}|{parent}"),
                domain=Domain.SEGMENT,
                metric=f"{metric_code}_HIERARCHY",
                period=children[0].period,
                reporting_basis=children[0].reporting_basis,
                currency=children[0].currency,
                frame_id=frame_id,
                component_sum=component_sum,
                component_ids=tuple(sorted(c.observation_id for c in children)),
                reported_total=parent_rows[0].normalized_value,
                reported_total_source="TABLE_TOTAL",
                reported_total_ref=parent_rows[0].observation_id,
                difference=difference,
                tolerance=tol,
                outcome=_outcome(difference, tol),
                explanation=f"subsegments vs their parent '{parent}'",
            )
        )
    return checks


def geography_totals(
    observations: Sequence[GeographyObservation],
    box4: CanonicalFinancialDataset,
) -> list[TotalReconciliation]:
    checks: list[TotalReconciliation] = []
    groups: dict[tuple[UUID, str, str], list[GeographyObservation]] = {}
    for o in observations:
        if o.normalized_value is not None and o.value_kind in (ValueKind.MONEY, ValueKind.PERCENT):
            groups.setdefault((o.frame_id, o.metric.value, _period_text(o.period)), []).append(o)
    box4_revenue: dict[tuple[object, ...], CanonicalFinancialValue] = {}
    for metric in reversed(REVENUE_METRICS):  # the preferred metric is written last and wins
        for v in box4.canonical_values:
            if v.metric == metric:
                box4_revenue[
                    (v.period.period_end, v.period.period_type, v.reporting_basis, v.currency)
                ] = v
    for (frame_id, metric_code, period_text), members in sorted(
        groups.items(), key=lambda kv: tuple(map(str, kv[0]))
    ):
        parts = [o for o in members if not o.is_total]
        if len(parts) < 2:
            continue
        sample = parts[0]
        component_sum = sum((o.normalized_value or Decimal(0) for o in parts), Decimal(0))
        allowed = _sum_tolerance(parts)
        base = dict(
            domain=Domain.GEOGRAPHY,
            metric=metric_code,
            period=sample.period,
            reporting_basis=sample.reporting_basis,
            currency=sample.currency,
            frame_id=frame_id,
            component_sum=component_sum,
            component_ids=tuple(sorted(o.observation_id for o in parts)),
        )
        totals = [o for o in members if o.is_total]
        if metric_code == GeographyMetric.REVENUE_SHARE.value:
            difference = component_sum - Decimal(100)
            checks.append(
                TotalReconciliation(
                    check_id=uuid5(NS, f"total|{frame_id}|{metric_code}|{period_text}|HUNDRED"),
                    reported_total=Decimal(100),
                    reported_total_source="ONE_HUNDRED_PERCENT",
                    reported_total_ref=None,
                    difference=difference,
                    tolerance=allowed,
                    outcome=_outcome(difference, allowed),
                    explanation="reported shares vs 100% (display rounding allowed)",
                    **base,  # type: ignore[arg-type]
                )
            )
            continue
        if totals and totals[0].normalized_value is not None:
            difference = component_sum - totals[0].normalized_value
            tol = allowed + _sum_tolerance(totals[:1])
            checks.append(
                TotalReconciliation(
                    check_id=uuid5(NS, f"total|{frame_id}|{metric_code}|{period_text}|TABLE"),
                    reported_total=totals[0].normalized_value,
                    reported_total_source="TABLE_TOTAL",
                    reported_total_ref=totals[0].observation_id,
                    difference=difference,
                    tolerance=tol,
                    outcome=_outcome(difference, tol),
                    explanation="sum of geographies vs the table's total line",
                    **base,  # type: ignore[arg-type]
                )
            )
        if metric_code != GeographyMetric.REVENUE.value:
            continue
        found = box4_revenue.get(
            (
                sample.period.period_end,
                sample.period.period_type,
                sample.reporting_basis,
                sample.currency,
            )
        )
        if found is None:
            other = [
                v
                for (end, ptype, basis, _c), v in box4_revenue.items()
                if end == sample.period.period_end
                and ptype == sample.period.period_type
                and basis == sample.reporting_basis
            ]
            checks.append(
                TotalReconciliation(
                    check_id=uuid5(NS, f"total|{frame_id}|{metric_code}|{period_text}|COMPANY"),
                    reported_total=None,
                    reported_total_source="NONE",
                    reported_total_ref=None,
                    difference=None,
                    tolerance=None,
                    outcome=TotalOutcome.NOT_COMPARABLE
                    if other
                    else TotalOutcome.INSUFFICIENT_DATA,
                    explanation=(
                        "Box 4 revenue exists only in another currency; not converted"
                        if other
                        else "no Box 4 revenue for this period/basis"
                    ),
                    **base,  # type: ignore[arg-type]
                )
            )
            continue
        difference = component_sum - found.value
        tol = allowed * 2
        checks.append(
            TotalReconciliation(
                check_id=uuid5(NS, f"total|{frame_id}|{metric_code}|{period_text}|COMPANY"),
                reported_total=found.value,
                reported_total_source="BOX4_CANONICAL",
                reported_total_ref=found.canonical_value_id,
                difference=difference,
                tolerance=tol,
                outcome=_outcome(difference, tol),
                explanation="sum of geographies vs Box 4 company revenue (attribution-independent "
                "total)",
                **base,  # type: ignore[arg-type]
            )
        )
    return checks


def period_key(period: FinancialPeriod) -> str:
    return _period_text(period)
