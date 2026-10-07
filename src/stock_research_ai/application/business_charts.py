"""DETAILED_RESEARCH_V1 Box 7: additive segments/1 charts on the Box 6 renderer.

Charts are drawn only from Box 7 canonical values and analytics: segment revenue /
result trends and margins, segment and geographic revenue mix, geographic revenue
trend, order book, capacity and generic KPI trends. Box 6's chart registry, planner
and existing 56 definitions are untouched; this module builds ChartSpecs directly and
renders them with the same deterministic SVG renderer.

Invariants: a trend never crosses a segment-structure change (only the latest
comparable run is drawn and the break is stated); missing periods stay visible gaps;
one axis never mixes units, currencies, bases or attribution methods; every point
references its Box 7 canonical value / analytic and the observations behind it.
"""

import hashlib
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5

from stock_research_ai.application.business_intelligence import canonical_business_json
from stock_research_ai.application.chart_planner import clip, display, money_display
from stock_research_ai.application.financial_parsing import add_months
from stock_research_ai.application.svg_renderer import render_svg
from stock_research_ai.domain.business_intelligence import (
    SEGMENT_CHART_REGISTRY_VERSION,
    AnalyticStatus,
    BusinessChartBundle,
    BusinessIntelligenceDataset,
    CanonicalGeographyValue,
    CanonicalKPIValue,
    Comparability,
    KPIPeriodKind,
    KPIUnit,
    SegmentAnalytic,
    SegmentMetric,
    SegmentRole,
)
from stock_research_ai.domain.financial_canonical import (
    Confidence,
    Currency,
    FinancialPeriod,
    PeriodType,
    ReportingBasis,
)
from stock_research_ai.domain.financial_charts import (
    RENDERER_VERSION,
    AxisKind,
    ChartArtifact,
    ChartAxis,
    ChartCategory,
    ChartLimits,
    ChartPoint,
    ChartSeries,
    ChartSpec,
    ChartStatistics,
    ChartStatus,
    ChartType,
    ChartWarning,
    PointSource,
    PointSourceKind,
    SeriesEncoding,
    SkippedChart,
    SourceLayer,
)

NS = UUID("0f7f0b6c-5a8e-5d2b-9a41-3b7c1e6d2f90")
SIZE = (1000, 600)
Renderer = Callable[[ChartSpec], str]
KPI_UNIT_LABEL = {
    KPIUnit.PERCENT: "%",
    KPIUnit.PEOPLE: "people",
    KPIUnit.CUSTOMERS: "customers",
    KPIUnit.COUNT: "count",
    KPIUnit.BRANCHES: "branches",
    KPIUnit.STORES: "stores",
    KPIUnit.MTPA: "MTPA",
    KPIUnit.MT: "MT",
    KPIUnit.MW: "MW",
    KPIUnit.GW: "GW",
    KPIUnit.MU: "MU",
    KPIUnit.DAYS: "days",
}


@dataclass(frozen=True)
class Definition:
    code: str
    title: str
    category: ChartCategory
    chart_type: ChartType
    description: str


REGISTRY: tuple[Definition, ...] = (
    Definition(
        "SEGMENT_REVENUE_TREND",
        "Segment revenue",
        ChartCategory.SEGMENTS,
        ChartType.GROUPED_BAR,
        "canonical reportable-segment revenue, latest comparable run",
    ),
    Definition(
        "SEGMENT_REVENUE_TREND_QUARTERLY",
        "Segment revenue (quarterly)",
        ChartCategory.SEGMENTS,
        ChartType.GROUPED_BAR,
        "as above, quarters",
    ),
    Definition(
        "SEGMENT_RESULT_TREND",
        "Segment result",
        ChartCategory.SEGMENTS,
        ChartType.GROUPED_BAR,
        "canonical segment result, latest comparable run",
    ),
    Definition(
        "SEGMENT_RESULT_MARGIN",
        "Segment result margin",
        ChartCategory.SEGMENTS,
        ChartType.LINE,
        "Box 7 SEGMENT_RESULT_MARGIN analytics",
    ),
    Definition(
        "SEGMENT_REVENUE_MIX",
        "Segment revenue mix",
        ChartCategory.SEGMENTS,
        ChartType.HORIZONTAL_BAR,
        "Box 7 SEGMENT_REVENUE_MIX, latest fiscal year",
    ),
    Definition(
        "GEOGRAPHIC_REVENUE_MIX",
        "Geographic revenue mix",
        ChartCategory.GEOGRAPHY,
        ChartType.HORIZONTAL_BAR,
        "Box 7 GEOGRAPHIC_REVENUE_MIX, latest fiscal year",
    ),
    Definition(
        "GEOGRAPHIC_REVENUE_TREND",
        "Revenue by geography",
        ChartCategory.GEOGRAPHY,
        ChartType.GROUPED_BAR,
        "canonical geography revenue, one attribution method",
    ),
    Definition(
        "ORDER_BOOK_TREND",
        "Order book",
        ChartCategory.OPERATING_KPIS,
        ChartType.BAR,
        "canonical ORDER_BOOK as at each date",
    ),
    Definition(
        "CAPACITY_TREND",
        "Capacity",
        ChartCategory.OPERATING_KPIS,
        ChartType.GROUPED_BAR,
        "canonical CAPACITY, one series per status",
    ),
    Definition(
        "KPI_TREND",
        "Operating KPI",
        ChartCategory.OPERATING_KPIS,
        ChartType.BAR,
        "any other canonical KPI with two or more periods",
    ),
)
_BY_CODE = {d.code: d for d in REGISTRY}


def _code(text: str) -> str:
    """A Box 6 metric code from a Box 7 series key ('SEGMENT|hi-tech' -> 'SEGMENT_HI_TECH')."""
    code = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
    return (code or "SERIES")[:79]


@dataclass
class Draft:
    """One chart before rendering (or its skip reason)."""

    spec: ChartSpec | None
    skipped: SkippedChart | None


def _x(period: FinancialPeriod, axis: AxisKind) -> tuple[str, str]:
    end = period.period_end
    assert end is not None
    if axis == AxisKind.QUARTERLY:
        label = (
            f"Q{period.fiscal_quarter} FY{str(period.fiscal_year)[2:]}"
            if period.fiscal_quarter and period.fiscal_year
            else end.strftime("%b %Y")
        )
        return f"QUARTER|{end}", label
    if axis == AxisKind.ANNUAL_BALANCE:
        return f"PIT|{end}", end.strftime("%b %Y")
    fiscal = period.fiscal_year or end.year
    return f"FY|{end}", f"FY{str(fiscal)[2:]}"


def _ends(latest: date, earliest: date, step: int = 12) -> list[date]:
    """Every `step`-month period end from earliest to latest; the ones nobody reported
    become visible gaps (never interpolated)."""
    out = [latest]
    while (prior := _month_end(add_months(out[-1], -step))) >= earliest:
        out.append(prior)
    return sorted(out)


def _month_end(day: date) -> date:
    following = add_months(date(day.year, day.month, 1), 1)
    return date.fromordinal(following.toordinal() - 1)


def _gap_period(
    period_type: PeriodType, end: date, reference: FinancialPeriod | None
) -> FinancialPeriod:
    """A placeholder period for a visible gap, labelled in the same fiscal calendar as a
    reported period (never carries a value)."""
    fiscal_year = fiscal_quarter = None
    if reference is not None and reference.period_end and reference.fiscal_year:
        months = (
            (end.year - reference.period_end.year) * 12 + end.month - reference.period_end.month
        )
        if period_type == PeriodType.QUARTER and reference.fiscal_quarter:
            index = reference.fiscal_quarter - 1 + months // 3
            fiscal_year, fiscal_quarter = reference.fiscal_year + index // 4, index % 4 + 1
        elif period_type == PeriodType.FY:
            fiscal_year = reference.fiscal_year + months // 12
    return FinancialPeriod(
        period_type=period_type,
        label=f"not reported ({end})",
        period_end=end,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
    )


def _qualifier_text(qualifier: str) -> str:
    return qualifier.replace("_PLUS", "+").replace("_", " ").lower().replace("usd ", "US$ ")


class Planner:
    def __init__(self, data: BusinessIntelligenceDataset, limits: ChartLimits) -> None:
        self.data = data
        self.limits = limits
        self.drafts: list[Draft] = []
        observations = {
            **{o.observation_id: o.source.locator for o in data.segment_observations},
            **{o.observation_id: o.source.locator for o in data.geography_observations},
            **{o.kpi_observation_id: o.source.locator for o in data.operating_kpi_observations},
        }
        self.locators = observations

    # --- assembly ----------------------------------------------------------------------

    def chart_id(self, code: str, *parts: str) -> UUID:
        return uuid5(NS, "|".join([str(self.data.dataset_id), code, *parts]))

    def skip(self, code: str, basis: ReportingBasis, status: ChartStatus, reason: str) -> None:
        definition = _BY_CODE[code]
        self.drafts.append(
            Draft(
                None,
                SkippedChart(
                    chart_code=code,
                    reporting_basis=basis,
                    category=definition.category,
                    status=status,
                    reason=clip(reason, 1000),
                ),
            )
        )

    def source(
        self,
        kind: PointSourceKind,
        ref: UUID,
        observations: Sequence[UUID],
        outcome: str | None = None,
        formula: str | None = None,
    ) -> PointSource:
        unique = tuple(dict.fromkeys(observations))
        return PointSource(
            layer=SourceLayer.BOX7,
            kind=kind,
            ref_id=ref,
            formula_or_rule_id=formula,
            observation_ids=unique,
            source_locators=tuple(
                dict.fromkeys(self.locators[o] for o in unique if o in self.locators)
            )[:12],
            reconciliation_outcome=outcome,
        )

    def point(
        self,
        cid: UUID,
        series: str,
        x_key: str,
        x_label: str,
        period_type: PeriodType | None,
        end: date | None,
        value: Decimal | None,
        confidence: Confidence | None,
        source: PointSource,
        missing: str | None = None,
        status: str = "CANONICAL",
    ) -> ChartPoint:
        return ChartPoint(
            point_id=uuid5(cid, f"{series}|{x_key}"),
            x_key=x_key,
            x_label=x_label,
            period_type=period_type,
            period_end=end,
            value=value,
            display_value=None,
            status=status if value is not None else "NOT_AVAILABLE",
            confidence=confidence,
            missing_reason=missing if value is None else None,
            source=source,
        )

    def emit(
        self,
        code: str,
        cid: UUID,
        title: str,
        basis: ReportingBasis,
        axis: AxisKind,
        unit: str,
        currency: Currency | None,
        series: list[tuple[str, str, list[ChartPoint]]],
        categories: list[tuple[str, str]],
        notes: Sequence[str],
        warnings: Sequence[ChartWarning] = (),
        render: Literal["BAR", "LINE", "MARKER"] = "BAR",
        formula: str | None = None,
    ) -> None:
        definition = _BY_CODE[code]
        values = [p.value for _, _, pts in series for p in pts if p.value is not None]
        if unit == "MONEY":
            display_unit, divisor = money_display(currency, values)
        elif unit == "PERCENT":
            display_unit, divisor = "%", Decimal(1)
        else:
            display_unit, divisor = KPI_UNIT_LABEL.get(KPIUnit(unit), unit.lower()), Decimal(1)
        built: list[ChartSeries] = []
        for number, (label, raw_metric, points) in enumerate(
            series[: self.limits.max_series_per_chart]
        ):
            metric = _code(raw_metric)
            shown = [
                p.model_copy(
                    update={
                        "display_value": (
                            display(p.value, unit, divisor, display_unit)
                            if unit in ("MONEY", "PERCENT")
                            else (f"{p.value:,} {display_unit}" if p.value is not None else None)
                        )
                    }
                )
                for p in points[: self.limits.max_points_per_series]
            ]
            valid = sum(1 for p in shown if p.value is not None)
            built.append(
                ChartSeries(
                    series_id=uuid5(cid, f"series|{number}|{raw_metric}"),
                    label=clip(label, self.limits.max_label_chars),
                    metric_code=metric,
                    source_layer=SourceLayer.BOX7,
                    encoding=SeriesEncoding.VALUE,
                    unit=unit,
                    currency=currency if unit == "MONEY" else None,
                    reporting_basis=basis,
                    frequency=axis,
                    render_as=render,
                    formula_id=formula,
                    points=tuple(shown),
                    valid_points=valid,
                    gaps=len(shown) - valid,
                    confidence_summary=dict(
                        sorted(Counter(p.confidence.value for p in shown if p.confidence).items())
                    ),
                )
            )
        gaps = sum(s.gaps for s in built)
        status = ChartStatus.PARTIAL if gaps or len(series) > len(built) else ChartStatus.READY
        all_warnings = list(warnings)
        if len(series) > len(built):
            all_warnings.append(
                ChartWarning(code="SERIES_LIMIT", detail=f"{len(series) - len(built)} not drawn")
            )
        span = f" · {categories[0][1]}–{categories[-1][1]}" if axis != AxisKind.CATEGORY else ""
        subtitle = clip(
            " · ".join(x for x in [f"{basis.value.title()}{span}", display_unit, *notes] if x), 300
        )
        kinds = Counter(p.source.kind.value for s in built for p in s.points if p.value is not None)
        self.drafts.append(
            Draft(
                ChartSpec(
                    chart_id=cid,
                    chart_code=code,
                    company_key=self.data.company_key,
                    title=clip(title, self.limits.max_label_chars),
                    subtitle=subtitle,
                    chart_type=definition.chart_type,
                    category=definition.category,
                    reporting_basis=basis,
                    frequency=axis,
                    currency=currency if unit == "MONEY" else None,
                    unit=unit,
                    width=SIZE[0],
                    height=SIZE[1],
                    series=tuple(built),
                    x_axis=ChartAxis(label="", display_unit="", categories=tuple(categories)),
                    y_axis=ChartAxis(
                        label=display_unit,
                        display_unit=display_unit,
                        scale_divisor=divisor,
                        zero_baseline=definition.chart_type != ChartType.LINE,
                    ),
                    annotations=(),
                    footer=(
                        "Source: company filings (Box 7 business intelligence) · "
                        f"{code} · {SEGMENT_CHART_REGISTRY_VERSION} · n/a = not available"
                    ),
                    provenance_summary=dict(sorted(kinds.items())),
                    warnings=tuple(all_warnings),
                    status=status,
                    status_reason=(
                        "all points available"
                        if status == ChartStatus.READY
                        else f"{gaps} gap(s) kept visible"
                    ),
                    source_dataset_ids=(self.data.dataset_id,),
                    registry_version=SEGMENT_CHART_REGISTRY_VERSION,
                ),
                None,
            )
        )

    # --- segment trends ------------------------------------------------------------------

    def comparable_run(self, basis: ReportingBasis, frequency: str) -> tuple[list[date], str]:
        """Period ends of the latest run of comparable structures, newest last."""
        structures = sorted(
            s.period_end
            for s in self.data.segment_structures
            if s.reporting_basis == basis and s.frequency == frequency
        )
        if not structures:
            return [], ""
        status = {
            (a.from_period_end, a.to_period_end): a
            for a in self.data.segment_comparability
            if a.reporting_basis == basis and a.frequency == frequency
        }
        run = [structures[-1]]
        note = ""
        for earlier in reversed(structures[:-1]):
            link = status.get((earlier, run[0]))
            if link is None or link.status not in (
                Comparability.COMPARABLE,
                Comparability.RESTATED_COMPARABLE,
            ):
                note = f"history before {run[0]} not drawn: segment structure changed"
                break
            run.insert(0, earlier)
        return run, note

    def segment_trend(self, code: str, metric: SegmentMetric, frequency: str) -> None:
        axis = AxisKind.ANNUAL_FLOW if frequency == "FY" else AxisKind.QUARTERLY
        period_type = PeriodType.FY if frequency == "FY" else PeriodType.QUARTER
        bases = sorted(
            {s.reporting_basis for s in self.data.segment_structures if s.frequency == frequency},
            key=lambda b: b.value,
        )
        if not bases:
            self.skip(
                code,
                ReportingBasis.UNKNOWN,
                ChartStatus.NO_DATA,
                f"no {frequency} segment structure",
            )
            return
        for basis in bases:
            run, note = self.comparable_run(basis, frequency)
            values = [
                v
                for v in self.data.canonical_segment_values
                if v.reporting_basis == basis
                and v.metric == metric
                and v.segment_role == SegmentRole.REPORTABLE
                and v.period.period_type == period_type
                and v.period.period_end in run
            ]
            for currency in sorted({v.currency for v in values}, key=lambda c: c.value):
                chosen = [v for v in values if v.currency == currency]
                latest = max(run)
                structure = next(
                    set(s.segment_keys)
                    for s in self.data.segment_structures
                    if s.reporting_basis == basis
                    and s.frequency == frequency
                    and s.period_end == latest
                )
                # only the latest structure's segments: an older definition never joins
                keys = sorted(
                    {v.segment_key for v in chosen} & structure,
                    key=lambda k: (
                        -max(
                            (
                                v.value
                                for v in chosen
                                if v.segment_key == k and v.period.period_end == latest
                            ),
                            default=Decimal(0),
                        ),
                        k,
                    ),
                )
                ends = _ends(max(run), min(run), 12 if frequency == "FY" else 3)
                if len(ends) < 2 or not keys:
                    self.skip(
                        code,
                        basis,
                        ChartStatus.NO_DATA,
                        f"{currency.value}: fewer than two comparable periods",
                    )
                    continue
                by = {(v.segment_key, v.period.period_end): v for v in chosen}
                reference = next((v.period for v in chosen if v.period.fiscal_year), None)
                sample = {
                    e: next(
                        (v.period for v in chosen if v.period.period_end == e),
                        _gap_period(period_type, e, reference),
                    )
                    for e in ends
                }
                chunks = [
                    keys[i : i + self.limits.max_series_per_chart]
                    for i in range(0, len(keys), self.limits.max_series_per_chart)
                ]
                for index, chunk in enumerate(chunks, start=1):
                    cid = self.chart_id(code, basis.value, currency.value, str(index))
                    categories: list[tuple[str, str]] = []
                    series = []
                    for key in chunk:
                        points = []
                        for end in ends:
                            period = sample[end]
                            x_key, x_label = _x(period, axis)
                            if (x_key, x_label) not in categories:
                                categories.append((x_key, x_label))
                            v = by.get((key, end))
                            points.append(
                                self.point(
                                    cid,
                                    key,
                                    x_key,
                                    x_label,
                                    period_type,
                                    end,
                                    v.value if v else None,
                                    v.confidence if v else None,
                                    self.source(
                                        PointSourceKind.SEGMENT_VALUE,
                                        v.canonical_value_id,
                                        [v.selected_observation_id, *v.supporting_observation_ids],
                                        v.outcome.value,
                                    )
                                    if v
                                    else PointSource(
                                        layer=SourceLayer.BOX7,
                                        kind=PointSourceKind.SEGMENT_VALUE,
                                        ref_id=None,
                                    ),
                                    missing="not presented for this period",
                                )
                            )
                        name = next(v.segment_name for v in chosen if v.segment_key == key)
                        series.append((name, f"SEGMENT|{key}", points))
                    part = f" ({index}/{len(chunks)})" if len(chunks) > 1 else ""
                    if len({v.currency for v in values}) > 1:
                        part = f" · {currency.value}{part}"
                    warnings = (
                        [ChartWarning(code="SEGMENT_STRUCTURE_CHANGED", detail=note)]
                        if (note)
                        else []
                    )
                    self.emit(
                        code,
                        cid,
                        f"{_BY_CODE[code].title}{part}",
                        basis,
                        axis,
                        "MONEY",
                        currency,
                        series,
                        categories,
                        [note] if note else [],
                        warnings,
                    )

    # --- analytics-based charts --------------------------------------------------------------

    def analytics(self, code: str, basis: ReportingBasis) -> list[SegmentAnalytic]:
        return [
            a for a in self.data.segment_analytics if a.code == code and a.reporting_basis == basis
        ]

    def analytic_source(self, a: SegmentAnalytic) -> PointSource:
        return self.source(
            PointSourceKind.SEGMENT_ANALYTIC,
            a.analytic_id,
            a.input_observation_ids,
            None,
            a.formula_id,
        )

    def margin(self) -> None:
        code = "SEGMENT_RESULT_MARGIN"
        bases = sorted(
            {a.reporting_basis for a in self.data.segment_analytics if a.code == code},
            key=lambda b: b.value,
        )
        if not bases:
            self.skip(code, ReportingBasis.UNKNOWN, ChartStatus.NO_DATA, "no segment margins")
            return
        for basis in bases:
            run, note = self.comparable_run(basis, "FY")
            found = [
                a
                for a in self.analytics(code, basis)
                if a.period.period_type == PeriodType.FY
                and a.period.period_end in run
                and a.status == AnalyticStatus.CALCULATED
            ]
            ends = sorted({a.period.period_end for a in found if a.period.period_end})
            if len(ends) < 2:
                self.skip(code, basis, ChartStatus.NO_DATA, "fewer than two fiscal years")
                continue
            ends = _ends(ends[-1], ends[0])
            keys = sorted({a.subject_key for a in found})
            by = {(a.subject_key, a.period.period_end): a for a in found}
            size = self.limits.max_series_per_chart
            total = (len(keys) + size - 1) // size
            for index in range(0, len(keys), size):
                chunk = keys[index : index + size]
                cid = self.chart_id(code, basis.value, str(index))
                part = f" ({index // size + 1}/{total})" if total > 1 else ""
                categories: list[tuple[str, str]] = []
                series = []
                for key in chunk:
                    points = []
                    for end in ends:
                        a = by.get((key, end))
                        period = (
                            a.period
                            if a
                            else FinancialPeriod(
                                period_type=PeriodType.FY, label=str(end), period_end=end
                            )
                        )
                        x_key, x_label = _x(period, AxisKind.ANNUAL_FLOW)
                        if (x_key, x_label) not in categories:
                            categories.append((x_key, x_label))
                        points.append(
                            self.point(
                                cid,
                                key,
                                x_key,
                                x_label,
                                PeriodType.FY,
                                end,
                                a.value if a else None,
                                None,
                                self.analytic_source(a)
                                if a
                                else PointSource(
                                    layer=SourceLayer.BOX7,
                                    kind=PointSourceKind.SEGMENT_ANALYTIC,
                                    ref_id=None,
                                ),
                                missing="no margin for this period",
                                status="CALCULATED",
                            )
                        )
                    series.append(
                        (
                            by[next(k for k in by if k[0] == key)].subject_name,
                            f"MARGIN|{key}",
                            points,
                        )
                    )
                self.emit(
                    code,
                    cid,
                    f"{_BY_CODE[code].title}{part}",
                    basis,
                    AxisKind.ANNUAL_FLOW,
                    "PERCENT",
                    None,
                    series,
                    categories,
                    [note] if note else [],
                    render="LINE",
                    formula=found[0].formula_id,
                )

    def mix(self, code: str, qualifier_split: bool) -> None:
        found = [
            a
            for a in self.data.segment_analytics
            if a.code == code
            and a.status == AnalyticStatus.CALCULATED
            and a.period.period_type == PeriodType.FY
        ]
        if not found:
            self.skip(code, ReportingBasis.UNKNOWN, ChartStatus.NO_DATA, "no calculated mix")
            return
        groups: dict[tuple[ReportingBasis, str], list[SegmentAnalytic]] = {}
        for a in found:
            groups.setdefault(
                (a.reporting_basis, a.qualifier or "" if qualifier_split else ""), []
            ).append(a)
        for (basis, qualifier), items in sorted(
            groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1])
        ):
            latest = max(a.period.period_end for a in items if a.period.period_end)
            rows = sorted(
                [a for a in items if a.period.period_end == latest],
                key=lambda a: (-(a.value or 0), a.subject_key),
            )
            if len(rows) < 2:
                self.skip(code, basis, ChartStatus.NO_DATA, "fewer than two components")
                continue
            cid = self.chart_id(code, basis.value, qualifier, str(latest))
            points = [
                self.point(
                    cid,
                    "mix",
                    a.subject_key,
                    a.subject_name,
                    None,
                    None,
                    a.value,
                    None,
                    self.analytic_source(a),
                    status="CALCULATED",
                )
                for a in rows[: self.limits.max_points_per_series]
            ]
            denominator = rows[0].metadata.get("denominator", "")
            notes = [f"FY ending {latest}", f"denominator: {denominator.lower()}"]
            if qualifier:
                notes.append(f"attribution: {qualifier.lower()}")
            self.emit(
                code,
                cid,
                _BY_CODE[code].title,
                basis,
                AxisKind.CATEGORY,
                "PERCENT",
                None,
                [("Share of revenue", f"{code}", points)],
                [(p.x_key, p.x_label) for p in points],
                notes,
                formula=rows[0].formula_id,
            )

    def geography_trend(self) -> None:
        code = "GEOGRAPHIC_REVENUE_TREND"
        values = [
            v
            for v in self.data.canonical_geography_values
            if v.metric.value == "REVENUE"
            and v.period.period_type == PeriodType.FY
            and not v.is_total
        ]
        groups: dict[tuple[ReportingBasis, str, Currency], list[CanonicalGeographyValue]] = {}
        for v in values:
            groups.setdefault((v.reporting_basis, v.attribution.value, v.currency), []).append(v)
        if not groups:
            self.skip(code, ReportingBasis.UNKNOWN, ChartStatus.NO_DATA, "no geography revenue")
        for (basis, method, currency), items in sorted(
            groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1], kv[0][2].value)
        ):
            ends_found = sorted({v.period.period_end for v in items if v.period.period_end})
            if len(ends_found) < 2:
                self.skip(
                    code,
                    basis,
                    ChartStatus.NO_DATA,
                    f"{method} {currency.value}: fewer than two fiscal years",
                )
                continue
            ends = _ends(ends_found[-1], ends_found[0])
            by = {(v.geography_key, v.period.period_end): v for v in items}
            latest = ends_found[-1]
            keys = sorted(
                {v.geography_key for v in items},
                key=lambda k: (-(by[(k, latest)].value if (k, latest) in by else Decimal(0)), k),
            )
            cid = self.chart_id(code, basis.value, method, currency.value)
            categories: list[tuple[str, str]] = []
            series = []
            for key in keys:
                points = []
                for end in ends:
                    hit = by.get((key, end))
                    period = (
                        hit.period
                        if hit
                        else FinancialPeriod(
                            period_type=PeriodType.FY, label=str(end), period_end=end
                        )
                    )
                    x_key, x_label = _x(period, AxisKind.ANNUAL_FLOW)
                    if (x_key, x_label) not in categories:
                        categories.append((x_key, x_label))
                    points.append(
                        self.point(
                            cid,
                            key,
                            x_key,
                            x_label,
                            PeriodType.FY,
                            end,
                            hit.value if hit else None,
                            hit.confidence if hit else None,
                            self.source(
                                PointSourceKind.GEOGRAPHY_VALUE,
                                hit.canonical_value_id,
                                [hit.selected_observation_id, *hit.supporting_observation_ids],
                                hit.outcome.value,
                            )
                            if hit
                            else PointSource(
                                layer=SourceLayer.BOX7,
                                kind=PointSourceKind.GEOGRAPHY_VALUE,
                                ref_id=None,
                            ),
                            missing="not presented for this period",
                        )
                    )
                name = next(v.geography_name for v in items if v.geography_key == key)
                series.append((name, f"GEOGRAPHY|{key}", points))
            self.emit(
                code,
                cid,
                _BY_CODE[code].title,
                basis,
                AxisKind.ANNUAL_FLOW,
                "MONEY",
                currency,
                series,
                categories,
                [f"attribution: {method.lower()}"],
            )

    # --- KPI trends ---------------------------------------------------------------------

    def kpi_trends(self) -> None:
        groups: dict[tuple[object, ...], list[CanonicalKPIValue]] = {}
        key: tuple[object, ...]
        for v in self.data.canonical_kpi_values:
            if v.period_kind == KPIPeriodKind.POINT_IN_TIME:
                axis = AxisKind.ANNUAL_BALANCE
            elif v.period.period_type == PeriodType.FY:
                axis = AxisKind.ANNUAL_FLOW
            elif v.period.period_type == PeriodType.QUARTER:
                axis = AxisKind.QUARTERLY
            else:
                continue
            if (
                axis == AxisKind.ANNUAL_BALANCE
                and v.period.period_end
                and (v.period.period_type == PeriodType.QUARTER)
            ):
                continue  # quarter-end stocks are not fiscal-year balances
            code = {"ORDER_BOOK": "ORDER_BOOK_TREND", "CAPACITY": "CAPACITY_TREND"}.get(
                v.canonical_kpi, "KPI_TREND"
            )
            key = (
                code,
                axis,
                v.unit,
                v.currency,
                v.reporting_basis,
                v.segment_key,
                v.geography_key,
                v.canonical_kpi if code != "CAPACITY_TREND" else "",
                v.qualifier if code != "CAPACITY_TREND" else "",
            )
            groups.setdefault(key, []).append(v)
        emitted = {"ORDER_BOOK_TREND": False, "CAPACITY_TREND": False}
        for key, items in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
            code, chosen_axis = str(key[0]), key[1]
            assert isinstance(chosen_axis, AxisKind)
            axis = chosen_axis
            ends_found = sorted({v.period.period_end for v in items if v.period.period_end})
            if len(ends_found) < 2:
                continue
            emitted[code] = True
            ends = _ends(ends_found[-1], ends_found[0], 3 if axis == AxisKind.QUARTERLY else 12)
            series_keys = (
                sorted({v.qualifier or "" for v in items}) if code == "CAPACITY_TREND" else [""]
            )
            cid = self.chart_id(code, *map(str, key[1:]))
            categories: list[tuple[str, str]] = []
            series = []
            for qualifier in series_keys:
                points = []
                for end in ends:
                    hit = next(
                        (
                            x
                            for x in items
                            if x.period.period_end == end
                            and (code != "CAPACITY_TREND" or (x.qualifier or "") == qualifier)
                        ),
                        None,
                    )
                    ptype = (
                        PeriodType.POINT_IN_TIME
                        if axis == AxisKind.ANNUAL_BALANCE
                        else (PeriodType.FY if axis == AxisKind.ANNUAL_FLOW else PeriodType.QUARTER)
                    )
                    period = FinancialPeriod(
                        period_type=ptype,
                        label=str(end),
                        period_end=end,
                        fiscal_year=hit.period.fiscal_year if hit else None,
                        fiscal_quarter=hit.period.fiscal_quarter if hit else None,
                    )
                    x_key, x_label = _x(period, axis)
                    if (x_key, x_label) not in categories:
                        categories.append((x_key, x_label))
                    points.append(
                        self.point(
                            cid,
                            qualifier or "kpi",
                            x_key,
                            x_label,
                            ptype,
                            end,
                            hit.value if hit else None,
                            hit.confidence if hit else None,
                            self.source(
                                PointSourceKind.KPI_VALUE,
                                hit.canonical_value_id,
                                [hit.selected_observation_id, *hit.supporting_observation_ids],
                                hit.outcome.value,
                            )
                            if hit
                            else PointSource(
                                layer=SourceLayer.BOX7, kind=PointSourceKind.KPI_VALUE, ref_id=None
                            ),
                            missing="not reported for this date",
                        )
                    )
                label = (
                    _qualifier_text(qualifier)
                    if qualifier
                    else items[0].canonical_kpi.replace("_", " ").lower()
                )
                series.append((label, f"KPI|{items[0].canonical_kpi}|{qualifier}", points))
            sample = items[0]
            unit = sample.unit.value if sample.unit != KPIUnit.MONEY else "MONEY"
            title = (
                _BY_CODE[code].title
                if code != "KPI_TREND"
                else (
                    sample.canonical_kpi.replace("_", " ").capitalize()
                    + (f" ({_qualifier_text(sample.qualifier)})" if sample.qualifier else "")
                )
            )
            scope = sample.segment_key or sample.geography_key
            self.emit(
                code,
                cid,
                title,
                sample.reporting_basis,
                axis,
                unit,
                sample.currency,
                series,
                categories,
                [f"scope: {scope}"] if scope else [],
            )
        for code, done in emitted.items():
            if not done:
                self.skip(
                    code, ReportingBasis.UNKNOWN, ChartStatus.NO_DATA, "fewer than two dated values"
                )

    def plan(self) -> list[Draft]:
        self.segment_trend("SEGMENT_REVENUE_TREND", SegmentMetric.REVENUE, "FY")
        self.segment_trend("SEGMENT_REVENUE_TREND_QUARTERLY", SegmentMetric.REVENUE, "QUARTER")
        self.segment_trend("SEGMENT_RESULT_TREND", SegmentMetric.SEGMENT_RESULT, "FY")
        self.margin()
        self.mix("SEGMENT_REVENUE_MIX", False)
        self.mix("GEOGRAPHIC_REVENUE_MIX", True)
        self.geography_trend()
        self.kpi_trends()
        return self.drafts


def build_business_charts(
    data: BusinessIntelligenceDataset,
    *,
    generated_at: datetime,
    limits: ChartLimits | None = None,
    renderer: Renderer = render_svg,
) -> BusinessChartBundle:
    bounds = limits or ChartLimits()
    sha = hashlib.sha256(canonical_business_json(data).encode("utf-8")).hexdigest()
    drafts = Planner(data, bounds).plan()
    charts: list[ChartSpec] = []
    skipped = [d.skipped for d in drafts if d.skipped is not None]
    artifacts: dict[str, ChartArtifact] = {}
    warnings: list[ChartWarning] = []
    partial = False
    for draft in drafts:
        spec = draft.spec
        if spec is None:
            continue
        if len(charts) >= bounds.max_charts:
            partial = True
            warnings.append(ChartWarning(code="CHART_LIMIT_REACHED", detail=spec.chart_code))
            continue
        try:
            svg = renderer(spec)
            size = len(svg.encode("utf-8"))
            if size > bounds.max_svg_bytes:
                raise ValueError(f"SVG of {size} bytes exceeds {bounds.max_svg_bytes}")
        except Exception as exc:  # isolation: one chart never breaks the bundle
            partial = True
            reason = f"{type(exc).__name__}: {str(exc)[:300]}"
            warnings.append(
                ChartWarning(code="RENDER_FAILED", detail=f"{spec.chart_code}: {reason}")
            )
            charts.append(
                spec.model_copy(
                    update={"status": ChartStatus.RENDER_FAILED, "status_reason": reason}
                )
            )
            continue
        digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()
        artifacts.setdefault(
            digest,
            ChartArtifact(
                sha256=digest,
                byte_size=size,
                width=spec.width,
                height=spec.height,
                name=f"{spec.chart_code.lower()}-{spec.reporting_basis.value.lower()}-{digest[:12]}.svg",
                content_reference=f"sha256/{digest[:2]}/{digest}.svg",
                svg=svg,
            ),
        )
        charts.append(spec.model_copy(update={"artifact_sha256": digest}))
    charts.sort(key=lambda c: (c.category.value, c.chart_code, str(c.chart_id)))
    skipped.sort(key=lambda s: (s.category.value, s.chart_code, s.reporting_basis.value))
    by_status = Counter(c.status for c in charts) + Counter(s.status for s in skipped)
    points = [p for c in charts for s in c.series for p in s.points]
    return BusinessChartBundle(
        bundle_id=uuid5(
            NS,
            f"{sha}|{SEGMENT_CHART_REGISTRY_VERSION}|{RENDERER_VERSION}|{bounds.model_dump_json()}",
        ),
        company_key=data.company_key,
        company_name=data.company_name,
        generated_at=generated_at,
        source_box7_dataset_id=data.dataset_id,
        source_box7_sha256=sha,
        renderer_version=RENDERER_VERSION,
        limits=bounds,
        status="PARTIAL" if partial else "COMPLETE",
        charts=tuple(charts),
        skipped_charts=tuple(skipped),
        artifacts=tuple(sorted(artifacts.values(), key=lambda a: a.sha256)),
        warnings=tuple(sorted(warnings, key=lambda w: (w.code, w.detail or ""))),
        statistics=ChartStatistics(
            definitions_considered=len(drafts),
            charts_ready=by_status[ChartStatus.READY],
            charts_partial=by_status[ChartStatus.PARTIAL],
            no_data=by_status[ChartStatus.NO_DATA],
            not_applicable=by_status[ChartStatus.NOT_APPLICABLE],
            unsupported_sector=by_status[ChartStatus.UNSUPPORTED_SECTOR],
            render_failed=by_status[ChartStatus.RENDER_FAILED],
            svg_artifacts=len(artifacts),
            svg_bytes=sum(a.byte_size for a in artifacts.values()),
            total_points=len(points),
            total_gaps=sum(1 for p in points if p.value is None),
            anomaly_annotations=0,
            charts_by_category=dict(sorted(Counter(c.category.value for c in charts).items())),
        ),
    )
