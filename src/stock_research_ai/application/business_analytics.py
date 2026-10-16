"""DETAILED_RESEARCH_V1 Box 7: segment / geography / KPI analytics (business-formulas/1).

Box 5 owns company-level formulas; Box 7 owns segment-level ones. Every analytic names
its formula, its exact input observations and canonical values, and a status. Inputs
are taken from ONE presentation (one filing table) whenever it shows both periods, so
definitions match by construction; across filings only when the segment structure is
COMPARABLE. Nothing is interpolated, nothing crosses a structure change, no mix is
forced to 100% (the denominator is stated).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.business_lineage import Presentation
from stock_research_ai.application.business_lines import NS
from stock_research_ai.application.business_reconciliation import kpi_time_key, period_key
from stock_research_ai.application.financial_parsing import add_months
from stock_research_ai.domain.business_intelligence import (
    FORMULA_VERSION,
    AnalyticStatus,
    CanonicalGeographyValue,
    CanonicalKPIValue,
    CanonicalSegmentValue,
    Comparability,
    ComparabilityAssessment,
    Domain,
    FormulaDefinition,
    GeographyMetric,
    GeographyObservation,
    KPIPeriodKind,
    SegmentAnalytic,
    SegmentLevel,
    SegmentMetric,
    SegmentObservation,
    SegmentRole,
    TotalReconciliation,
)
from stock_research_ai.domain.financial_canonical import (
    Currency,
    FinancialPeriod,
    PeriodType,
    ReportingBasis,
)


def _f(code: str, expression: str, rules: str) -> FormulaDefinition:
    return FormulaDefinition(
        formula_id=f"{FORMULA_VERSION}:{code}", code=code, expression=expression, rules=rules
    )


SAME = (
    "inputs from one filing table showing both periods, or canonical values across "
    "filings only when the segment structure is COMPARABLE; same basis, currency, unit"
)
FORMULAS: tuple[FormulaDefinition, ...] = (
    _f("SEGMENT_REVENUE_GROWTH", "(revenue[t] / revenue[t-12m] - 1) x 100", SAME),
    _f("SEGMENT_RESULT_GROWTH", "(result[t] / result[t-12m] - 1) x 100; positive base only", SAME),
    _f(
        "SEGMENT_RESULT_MARGIN",
        "segment result / segment revenue x 100 (same table, period)",
        "called EBIT / EBITDA margin only when the source labels the result so",
    ),
    _f("SEGMENT_EBIT_MARGIN", "segment EBIT / segment revenue x 100", "source labels EBIT"),
    _f("SEGMENT_EBITDA_MARGIN", "segment EBITDA / segment revenue x 100", "source labels EBITDA"),
    _f(
        "SEGMENT_REVENUE_MIX",
        "segment revenue / sum of reportable segment revenue in the same table x 100",
        "before eliminations / unallocated; the company-total gap is reported separately",
    ),
    _f(
        "GEOGRAPHIC_REVENUE_MIX",
        "geography revenue / sum of non-total geography revenue in the same table x 100",
        "one attribution method per table",
    ),
    _f(
        "GEOGRAPHIC_REVENUE_GROWTH",
        "(revenue[t] / revenue[t-12m] - 1) x 100",
        "same geography name and attribution; one table or identical geography sets",
    ),
    _f(
        "ORDER_BOOK_GROWTH",
        "(order book[as of d] / order book[as of d-12m] - 1) x 100",
        "same scope, currency; never annualised, never treated as revenue",
    ),
    _f(
        "CAPACITY_GROWTH",
        "(capacity[d] / capacity[d-12m] - 1) x 100",
        "same capacity status and unit; statuses are never summed",
    ),
    _f("VOLUME_GROWTH", "(sales volume[t] / sales volume[t-12m] - 1) x 100", "same unit"),
    _f("PRODUCTION_GROWTH", "(production[t] / production[t-12m] - 1) x 100", "same unit"),
    _f(
        "REALIZATION_GROWTH",
        "(realization[t] / realization[t-12m] - 1) x 100",
        "reported realization only; never revenue / volume",
    ),
)
_FORMULA = {f.code: f.formula_id for f in FORMULAS}
_KPI_GROWTH = {
    "ORDER_BOOK": "ORDER_BOOK_GROWTH",
    "CAPACITY": "CAPACITY_GROWTH",
    "SALES_VOLUME": "VOLUME_GROWTH",
    "PRODUCTION": "PRODUCTION_GROWTH",
    "REALIZATION": "REALIZATION_GROWTH",
}
_RESULT_METRICS = {
    SegmentMetric.SEGMENT_RESULT: "SEGMENT_RESULT_MARGIN",
    SegmentMetric.EBIT: "SEGMENT_EBIT_MARGIN",
    SegmentMetric.EBITDA: "SEGMENT_EBITDA_MARGIN",
}
HUNDRED = Decimal(100)


def growth(current: Decimal, prior: Decimal) -> tuple[Decimal | None, AnalyticStatus, str]:
    if prior == 0:
        return None, AnalyticStatus.DIVISION_BY_ZERO, "zero base"
    if prior < 0:
        return None, AnalyticStatus.NOT_APPLICABLE, "negative base"
    return (current / prior - 1) * HUNDRED, AnalyticStatus.CALCULATED, "calculated"


def year_earlier(period: FinancialPeriod) -> date | None:
    return add_months(period.period_end, -12) if period.period_end else None


@dataclass
class Builder:
    analytics: list[SegmentAnalytic]
    canonical_of: dict[UUID, UUID]  # observation -> canonical value holding it

    def add(
        self,
        code: str,
        domain: Domain,
        key: str,
        name: str,
        period: FinancialPeriod,
        basis: ReportingBasis,
        currency: Currency | None,
        value: Decimal | None,
        status: AnalyticStatus,
        reason: str,
        inputs: Sequence[UUID],
        comparison: FinancialPeriod | None = None,
        comparability: Comparability | None = None,
        qualifier: str | None = None,
        metadata: dict[str, str] | None = None,
        extra: str = "",
    ) -> None:
        canonical = tuple(
            sorted({self.canonical_of[i] for i in inputs if i in self.canonical_of}, key=str)
        )
        self.analytics.append(
            SegmentAnalytic(
                analytic_id=uuid5(
                    NS,
                    f"analytic|{code}|{domain.value}|{key}|{qualifier}|"
                    f"{basis.value}|{period_key(period)}|{extra}",
                ),
                code=code,
                formula_id=_FORMULA[code],
                subject_domain=domain,
                subject_key=key,
                subject_name=name[:1000],
                qualifier=qualifier,
                period=period,
                comparison_period=comparison,
                value=value if status == AnalyticStatus.CALCULATED else None,
                unit="PERCENT",
                status=status,
                reason=reason[:1000],
                reporting_basis=basis,
                currency=currency,
                comparability=comparability,
                input_canonical_ids=canonical,
                input_observation_ids=tuple(sorted(set(inputs), key=str)),
                metadata=metadata or {},
            )
        )


def segment_analytics(
    observations: Sequence[SegmentObservation],
    canonical_key: dict[UUID, str],
    presented: dict[tuple[ReportingBasis, str, date], list[Presentation]],
    assessments: Sequence[ComparabilityAssessment],
    values: Sequence[CanonicalSegmentValue],
    totals: Sequence[TotalReconciliation],
    builder: Builder,
    *,
    industrial: bool = True,
) -> None:
    by_frame: dict[tuple[UUID, str, str, date], list[SegmentObservation]] = {}
    for o in observations:
        if o.period.period_end is None or o.normalized_value is None:
            continue
        if o.segment_role != SegmentRole.REPORTABLE or o.segment_level != SegmentLevel.PRIMARY:
            continue
        by_frame.setdefault(
            (o.frame_id, o.metric.value, canonical_key[o.observation_id], o.period.period_end), []
        ).append(o)
    assessed = {
        (a.reporting_basis, a.frequency, a.from_period_end, a.to_period_end): a for a in assessments
    }
    canonical = {
        (
            v.segment_key,
            v.metric.value,
            v.reporting_basis,
            v.period.period_type,
            v.period.period_end,
            v.currency,
        ): v
        for v in values
        if v.segment_role == SegmentRole.REPORTABLE
    }
    company_check = {
        (t.frame_id, t.metric, t.period.period_end): t.outcome.value
        for t in totals
        if t.reported_total_source in ("TABLE_COMPANY_LINE", "BOX4_CANONICAL")
    }
    names = {canonical_key[o.observation_id]: o.segment_raw_name for o in observations}

    def one(frame: UUID, metric: str, key: str, end: date) -> SegmentObservation | None:
        found = by_frame.get((frame, metric, key, end), [])
        return found[0] if len(found) == 1 else None

    for (basis, frequency, end), items in sorted(
        presented.items(), key=lambda kv: (kv[0][0].value, kv[0][1], kv[0][2])
    ):
        latest = items[0]
        period_type = PeriodType.FY if frequency == "FY" else PeriodType.QUARTER
        prior_end = add_months(end, -12)
        prior = presented.get((basis, frequency, prior_end), [])
        shared = next((p for p in items if any(q.frame_id == p.frame_id for q in prior)), None)
        assessment = assessed.get((basis, frequency, prior_end, end))
        for key in sorted(latest.keys):
            for metric, code in (
                (SegmentMetric.REVENUE, "SEGMENT_REVENUE_GROWTH"),
                (SegmentMetric.SEGMENT_RESULT, "SEGMENT_RESULT_GROWTH"),
            ):
                frame = shared.frame_id if shared else latest.frame_id
                current = one(frame, metric.value, key, end)
                if current is None:
                    continue
                assert current.normalized_value is not None
                if shared is not None:
                    before = one(shared.frame_id, metric.value, key, prior_end)
                    if before is not None and before.normalized_value is not None:
                        value, status, reason = growth(
                            current.normalized_value, before.normalized_value
                        )
                        restated = shared.restated
                        builder.add(
                            code,
                            Domain.SEGMENT,
                            key,
                            names[key],
                            current.period,
                            basis,
                            current.currency,
                            value,
                            status,
                            f"{reason}; both periods from one filing table",
                            [current.observation_id, before.observation_id],
                            before.period,
                            Comparability.RESTATED_COMPARABLE
                            if restated
                            else Comparability.COMPARABLE,
                        )
                        continue
                if not prior:
                    builder.add(
                        code,
                        Domain.SEGMENT,
                        key,
                        names[key],
                        current.period,
                        basis,
                        current.currency,
                        None,
                        AnalyticStatus.INSUFFICIENT_HISTORY,
                        f"no {frequency} period ending {prior_end} is presented",
                        [current.observation_id],
                    )
                    continue
                if assessment is None or assessment.status != Comparability.COMPARABLE:
                    builder.add(
                        code,
                        Domain.SEGMENT,
                        key,
                        names[key],
                        current.period,
                        basis,
                        current.currency,
                        None,
                        AnalyticStatus.NOT_COMPARABLE,
                        "segment structure not comparable across "
                        f"{prior_end} -> {end}: "
                        + (assessment.reason if assessment else "periods not adjacent"),
                        [current.observation_id],
                        None,
                        assessment.status if assessment else Comparability.UNKNOWN,
                    )
                    continue
                now = canonical.get((key, metric.value, basis, period_type, end, current.currency))
                then = canonical.get(
                    (key, metric.value, basis, period_type, prior_end, current.currency)
                )
                if now is None or then is None:
                    builder.add(
                        code,
                        Domain.SEGMENT,
                        key,
                        names[key],
                        current.period,
                        basis,
                        current.currency,
                        None,
                        AnalyticStatus.INPUT_MISSING,
                        "a canonical value is missing (conflict or not presented)",
                        [current.observation_id],
                    )
                    continue
                value, status, reason = growth(now.value, then.value)
                builder.add(
                    code,
                    Domain.SEGMENT,
                    key,
                    names[key],
                    now.period,
                    basis,
                    now.currency,
                    value,
                    status,
                    f"{reason}; canonical values, structure COMPARABLE",
                    [now.selected_observation_id, then.selected_observation_id],
                    then.period,
                    Comparability.COMPARABLE,
                )
        # margins and mix from the latest presentation of this period
        revenue = {
            key: one(latest.frame_id, SegmentMetric.REVENUE.value, key, end)
            for key in sorted(latest.keys)
        }
        for key, rev in revenue.items():
            if rev is None or rev.normalized_value is None:
                continue
            for metric, code in _RESULT_METRICS.items():
                result = one(latest.frame_id, metric.value, key, end)
                if result is None or result.normalized_value is None:
                    continue
                if not industrial:
                    value, status, reason = (
                        None, AnalyticStatus.NOT_APPLICABLE, "business model does not support "
                        "industrial segment margin interpretation")
                elif rev.normalized_value == 0:
                    value, status, reason = None, AnalyticStatus.DIVISION_BY_ZERO, "zero revenue"
                else:
                    value = result.normalized_value / rev.normalized_value * HUNDRED
                    status, reason = AnalyticStatus.CALCULATED, "calculated; one filing table"
                builder.add(
                    code,
                    Domain.SEGMENT,
                    key,
                    names[key],
                    rev.period,
                    basis,
                    rev.currency,
                    value,
                    status,
                    reason,
                    [result.observation_id, rev.observation_id],
                    metadata={"result_label": result.raw_metric_label[:200]},
                )
        parts = [o for o in revenue.values() if o is not None and o.normalized_value is not None]
        if len(parts) >= 2:
            denominator = sum((o.normalized_value or Decimal(0) for o in parts), Decimal(0))
            check = company_check.get(
                (latest.frame_id, SegmentMetric.REVENUE.value, end), "NOT_CHECKED"
            )
            for o in parts:
                assert o.normalized_value is not None
                key = canonical_key[o.observation_id]
                if denominator == 0:
                    builder.add(
                        "SEGMENT_REVENUE_MIX",
                        Domain.SEGMENT,
                        key,
                        names[key],
                        o.period,
                        basis,
                        o.currency,
                        None,
                        AnalyticStatus.DIVISION_BY_ZERO,
                        "segments sum to zero",
                        [p.observation_id for p in parts],
                    )
                    continue
                builder.add(
                    "SEGMENT_REVENUE_MIX",
                    Domain.SEGMENT,
                    key,
                    names[key],
                    o.period,
                    basis,
                    o.currency,
                    o.normalized_value / denominator * HUNDRED,
                    AnalyticStatus.CALCULATED,
                    "share of the sum of reportable segments in one filing table",
                    [p.observation_id for p in parts],
                    metadata={
                        "denominator": "SUM_OF_REPORTABLE_SEGMENTS",
                        "denominator_value": str(denominator),
                        "company_total_check": check,
                    },
                )


def geography_analytics(
    observations: Sequence[GeographyObservation],
    values: Sequence[CanonicalGeographyValue],
    builder: Builder,
) -> None:
    frames: dict[tuple[ReportingBasis, str, str, date], list[UUID]] = {}
    rows: dict[tuple[UUID, str, date], GeographyObservation] = {}
    for o in observations:
        if (
            o.metric != GeographyMetric.REVENUE
            or o.normalized_value is None
            or o.period.period_end is None
            or o.reporting_basis == ReportingBasis.UNKNOWN
            or o.period.period_type not in (PeriodType.FY, PeriodType.QUARTER)
        ):
            continue
        key = (
            o.reporting_basis,
            o.attribution.value,
            o.period.period_type.value,
            o.period.period_end,
        )
        if o.frame_id not in frames.setdefault(key, []):
            frames[key].append(o.frame_id)
        rows[(o.frame_id, o.geography_key, o.period.period_end)] = o
    canonical = {
        (
            v.geography_key,
            v.attribution.value,
            v.reporting_basis,
            v.period.period_type.value,
            v.period.period_end,
            v.currency,
        ): v
        for v in values
        if v.metric == GeographyMetric.REVENUE
    }
    for (basis, method, ptype, end), frame_ids in sorted(
        frames.items(), key=lambda kv: (kv[0][0].value, kv[0][1], kv[0][2], kv[0][3])
    ):
        frame = sorted(frame_ids, key=str)[0]
        members = [o for (f, _k, e), o in rows.items() if f == frame and e == end]
        members.sort(key=lambda o: o.geography_key)
        parts = [o for o in members if not o.is_total]
        denominator = sum((o.normalized_value or Decimal(0) for o in parts), Decimal(0))
        prior_end = add_months(end, -12)
        for o in parts:
            assert o.normalized_value is not None
            if len(parts) >= 2 and denominator != 0:
                builder.add(
                    "GEOGRAPHIC_REVENUE_MIX",
                    Domain.GEOGRAPHY,
                    o.geography_key,
                    o.geography_raw_name,
                    o.period,
                    basis,
                    o.currency,
                    o.normalized_value / denominator * HUNDRED,
                    AnalyticStatus.CALCULATED,
                    "share of the sum of geographies in one filing table",
                    [p.observation_id for p in parts],
                    qualifier=method,
                    metadata={
                        "denominator": "SUM_OF_GEOGRAPHIES",
                        "denominator_value": str(denominator),
                        "attribution": method,
                    },
                )
            before = rows.get((frame, o.geography_key, prior_end))
            if before is not None and before.normalized_value is not None:
                value, status, reason = growth(o.normalized_value, before.normalized_value)
                builder.add(
                    "GEOGRAPHIC_REVENUE_GROWTH",
                    Domain.GEOGRAPHY,
                    o.geography_key,
                    o.geography_raw_name,
                    o.period,
                    basis,
                    o.currency,
                    value,
                    status,
                    f"{reason}; both periods from one filing table",
                    [o.observation_id, before.observation_id],
                    before.period,
                    qualifier=method,
                )
                continue
            now = canonical.get((o.geography_key, method, basis, ptype, end, o.currency))
            then = canonical.get((o.geography_key, method, basis, ptype, prior_end, o.currency))
            if now is None or then is None:
                builder.add(
                    "GEOGRAPHIC_REVENUE_GROWTH",
                    Domain.GEOGRAPHY,
                    o.geography_key,
                    o.geography_raw_name,
                    o.period,
                    basis,
                    o.currency,
                    None,
                    AnalyticStatus.INSUFFICIENT_HISTORY,
                    f"no comparable value for the period ending {prior_end}",
                    [o.observation_id],
                    qualifier=method,
                )
                continue
            value, status, reason = growth(now.value, then.value)
            builder.add(
                "GEOGRAPHIC_REVENUE_GROWTH",
                Domain.GEOGRAPHY,
                o.geography_key,
                o.geography_raw_name,
                now.period,
                basis,
                now.currency,
                value,
                status,
                f"{reason}; canonical values, same name and attribution",
                [now.selected_observation_id, then.selected_observation_id],
                then.period,
                qualifier=method,
            )


def kpi_analytics(values: Sequence[CanonicalKPIValue], builder: Builder) -> None:
    index: dict[tuple[object, ...], CanonicalKPIValue] = {}
    for v in values:
        index[
            (
                v.canonical_kpi,
                v.qualifier,
                v.segment_key,
                v.geography_key,
                v.unit,
                v.currency,
                v.period_kind,
                v.reporting_basis,
                _time(v),
            )
        ] = v
    for v in sorted(values, key=lambda x: str(x.canonical_value_id)):
        code = _KPI_GROWTH.get(v.canonical_kpi)
        if code is None:
            continue
        prior_time = _prior_time(v)
        key = f"{v.canonical_kpi}|{v.segment_key or ''}|{v.geography_key or ''}"
        then = index.get(
            (
                v.canonical_kpi,
                v.qualifier,
                v.segment_key,
                v.geography_key,
                v.unit,
                v.currency,
                v.period_kind,
                v.reporting_basis,
                prior_time,
            )
        )
        if then is None:
            builder.add(
                code,
                Domain.KPI,
                key,
                v.canonical_kpi,
                v.period,
                v.reporting_basis,
                v.currency,
                None,
                AnalyticStatus.INSUFFICIENT_HISTORY,
                f"no comparable value 12 months earlier ({prior_time})",
                [v.selected_observation_id],
                qualifier=v.qualifier,
            )
            continue
        value, status, reason = growth(v.value, then.value)
        builder.add(
            code,
            Domain.KPI,
            key,
            v.canonical_kpi,
            v.period,
            v.reporting_basis,
            v.currency,
            value,
            status,
            f"{reason}; same KPI, qualifier, scope and unit",
            [v.selected_observation_id, then.selected_observation_id],
            then.period,
            qualifier=v.qualifier,
        )


def _time(v: CanonicalKPIValue) -> str:
    if v.period_kind == KPIPeriodKind.POINT_IN_TIME:
        return str(v.period.period_end)
    return f"{v.period.period_type.value}|{v.period.period_end}|{v.period.duration_months}"


def _prior_time(v: CanonicalKPIValue) -> str:
    end = year_earlier(v.period)
    if v.period_kind == KPIPeriodKind.POINT_IN_TIME:
        return str(end)
    return f"{v.period.period_type.value}|{end}|{v.period.duration_months}"


__all__ = [
    "FORMULAS",
    "Builder",
    "geography_analytics",
    "kpi_analytics",
    "kpi_time_key",
    "segment_analytics",
]
