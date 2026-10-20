"""DETAILED_RESEARCH_V1 Box 6: chart planning (selection only - no financial calculation).

For each ChartDefinition and each reporting basis Box 5 analysed, the planner:
  * checks sector applicability (and Box 5's own UNSUPPORTED_SECTOR statuses),
  * builds the axis from Box 5's frame periods for exactly one period kind (FY, balance
    sheets at fiscal year ends, or quarters - never TTM / YTD / half-year),
  * selects each point from a Box 5 output or a Box 4 canonical value in the frame's
    currency, keeping every non-calculated period as a visible gap with its reason,
  * attaches Box 5 anomalies and trends as annotations,
  * decides READY / PARTIAL / NO_DATA / NOT_APPLICABLE / UNSUPPORTED_SECTOR.
Display scaling (crore -> lakh crore, crore -> US$ million) is formatting only; every
point keeps its source value unchanged.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from stock_research_ai.application.chart_registry import (
    CHARTS,
    ChartDefinition,
    SeriesDefinition,
)
from stock_research_ai.application.financial_metrics import Periods
from stock_research_ai.domain.financial_analytics import (
    AnalyticsFrame,
    CalculatedMetric,
    CalculationStatus,
    ComponentStatus,
    FinancialAnalyticsDataset,
    ForensicModel,
    HealthState,
    Severity,
    TrendClass,
    sector_applies,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    Outcome,
    PeriodType,
    ReconciliationRecord,
    ReportingBasis,
    SourceFinancialObservation,
)
from stock_research_ai.domain.financial_charts import (
    CHART_REGISTRY_VERSION,
    AxisKind,
    ChartAnnotation,
    ChartAxis,
    ChartLimits,
    ChartPoint,
    ChartSeries,
    ChartSpec,
    ChartStatus,
    ChartType,
    ChartWarning,
    PointSource,
    PointSourceKind,
    SeriesEncoding,
    SkippedChart,
    SourceLayer,
)

NS = UUID("6c2d4e8f-1a3b-4c5d-9e7f-0a1b2c3d4e5f")
D = Decimal
STANDARD = (1000, 600)
LANDSCAPE = (1200, 675)
MAX_LOCATORS = 12
SEVERITY_RANK = {
    Severity.INFO: D(0),
    Severity.LOW: D(1),
    Severity.MEDIUM: D(2),
    Severity.HIGH: D(3),
}
TREND_RANK = {
    TrendClass.STRONGLY_IMPROVING: D(2),
    TrendClass.IMPROVING: D(1),
    TrendClass.STABLE: D(0),
    TrendClass.DETERIORATING: D(-1),
    TrendClass.STRONGLY_DETERIORATING: D(-2),
}
HEALTH_RANK = {
    HealthState.STRONG: D(2),
    HealthState.HEALTHY: D(1),
    HealthState.NEUTRAL: D(0),
    HealthState.WATCH: D(-1),
    HealthState.WEAK: D(-2),
}
UNIT_LABEL = {
    "PERCENT": "%",
    "PERCENTAGE_POINTS": "pp",
    "BASIS_POINTS": "bps",
    "TIMES": "x",
    "DAYS": "days",
    "PER_SHARE": "per share",
    "COUNT": "count",
    "ORDINAL": "status rank",
}


@dataclass(frozen=True)
class Planned:
    spec: ChartSpec | None
    skipped: SkippedChart | None


class Context:
    def __init__(
        self,
        box4: CanonicalFinancialDataset,
        box5: FinancialAnalyticsDataset,
        frame: AnalyticsFrame,
        limits: ChartLimits,
    ) -> None:
        self.box4, self.box5, self.frame, self.limits = box4, box5, frame, limits
        self.basis: ReportingBasis = frame.reporting_basis
        self.currency: Currency = frame.currency
        self.periods = Periods(box4.fiscal_year_end_month)
        self.metrics: dict[tuple[str, str], CalculatedMetric] = {
            (m.code, m.period.key()): m
            for m in box5.all_metrics()
            if m.reporting_basis == self.basis
        }
        self.by_code: dict[str, list[CalculatedMetric]] = {}
        for m in box5.all_metrics():
            if m.reporting_basis == self.basis:
                self.by_code.setdefault(m.code, []).append(m)
        self.values: dict[tuple[CanonicalMetric, str], CanonicalFinancialValue] = {
            (v.metric, v.period.key()): v
            for v in box4.canonical_values
            if v.reporting_basis == self.basis and v.currency == self.currency
        }
        self.unresolved: dict[tuple[CanonicalMetric, str], ReconciliationRecord] = {
            (r.metric, r.period.key()): r
            for r in box4.reconciliation
            if r.reporting_basis == self.basis and r.selected_observation_id is None
        }
        self.observations: dict[UUID, SourceFinancialObservation] = {
            o.observation_id: o for o in box4.observations
        }
        self.warnings: list[ChartWarning] = []

    def axis_periods(self, axis: AxisKind) -> list[FinancialPeriod]:
        if axis in (AxisKind.ANNUAL_FLOW, AxisKind.TIMELINE):
            ends: Sequence[date] = self.frame.annual_periods
            periods = [self.periods.fy(e) for e in ends]
        elif axis == AxisKind.ANNUAL_BALANCE:
            periods = [self.periods.pit(e) for e in self.frame.annual_periods]
        else:
            periods = [self.periods.quarter(e) for e in self.frame.quarter_periods]
        return periods[-self.limits.max_points_per_series :]


def short_label(period: FinancialPeriod) -> str:
    if period.period_type == PeriodType.QUARTER and period.fiscal_quarter and period.fiscal_year:
        return f"Q{period.fiscal_quarter} FY{str(period.fiscal_year)[2:]}"
    if period.period_type == PeriodType.POINT_IN_TIME and period.period_end:
        return period.period_end.strftime("%b %Y")
    return period.label


def money_display(currency: Currency | None, values: Sequence[Decimal]) -> tuple[str, Decimal]:
    """(display unit, divisor) for MONEY values held at the Box 4 canonical scale (crore)."""
    biggest = max((abs(v) for v in values), default=D(0))
    if currency == Currency.INR:
        return ("₹ lakh crore", D(100_000)) if biggest >= 100_000 else ("₹ crore", D(1))
    if currency == Currency.USD:
        return "US$ million", D("0.1")  # 1 crore = 10 million
    return f"{currency.value if currency else 'unknown currency'} crore", D(1)


def display(value: Decimal | None, unit: str, divisor: Decimal, display_unit: str) -> str | None:
    if value is None:
        return None
    if unit == "MONEY":
        shown = value / divisor
        places = 2 if divisor != 1 else 0
        return f"{shown:,.{places}f} {display_unit}"
    places = {"BASIS_POINTS": 0, "DAYS": 1}.get(unit, 2)
    suffix = {
        "PERCENT": "%",
        "PERCENTAGE_POINTS": " pp",
        "BASIS_POINTS": " bps",
        "TIMES": "x",
        "DAYS": " days",
    }.get(unit, "")
    return f"{value:,.{places}f}{suffix}"


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- point builders ------------------------------------------------------------------------------


def _metric_source(m: CalculatedMetric) -> PointSource:
    locators = tuple(dict.fromkeys(i.source_locator for i in m.provenance))[:MAX_LOCATORS]
    return PointSource(
        layer=SourceLayer.BOX5,
        kind=PointSourceKind.CALCULATED_METRIC,
        ref_id=m.metric_id,
        formula_or_rule_id=m.formula_id,
        canonical_value_ids=m.input_value_ids,
        metric_ids=m.input_metric_ids,
        observation_ids=tuple(dict.fromkeys(i.selected_observation_id for i in m.provenance)),
        source_locators=locators,
    )


def box5_point(
    ctx: Context,
    chart_id: UUID,
    sdef: SeriesDefinition,
    period: FinancialPeriod,
    x_key: str | None = None,
    x_label: str | None = None,
) -> ChartPoint:
    assert sdef.box5 is not None
    m = ctx.metrics.get((sdef.box5, period.key()))
    key = x_key or period.key()
    pid = uuid5(chart_id, f"{sdef.box5}|{key}")
    common: dict[str, Any] = {
        "point_id": pid,
        "x_key": key,
        "x_label": x_label or short_label(period),
        "period_type": period.period_type,
        "period_start": period.period_start,
        "period_end": period.period_end,
    }
    if m is None:
        return ChartPoint(
            **common,
            value=None,
            display_value=None,
            status="NOT_DERIVED",
            confidence=None,
            missing_reason=f"Box 5 has no {sdef.box5} for {period.label}",
            source=PointSource(
                layer=SourceLayer.BOX5, kind=PointSourceKind.CALCULATED_METRIC, ref_id=None
            ),
        )
    if m.status != CalculationStatus.CALCULATED or m.value is None:
        reason = "; ".join(m.missing_inputs) or m.status.value
        return ChartPoint(
            **common,
            value=None,
            display_value=None,
            status=m.status.value,
            confidence=None,
            missing_reason=clip(f"{m.status.value}: {reason}", 500),
            source=_metric_source(m),
            warnings=m.warnings,
        )
    return ChartPoint(
        **common,
        value=m.value,
        display_value=None,
        status=m.status.value,
        confidence=m.confidence,
        source=_metric_source(m),
        warnings=m.warnings,
    )


def choose_box4_metric(
    ctx: Context, metrics: Sequence[CanonicalMetric], periods: Sequence[FinancialPeriod]
) -> CanonicalMetric:
    """One canonical metric per series (never a mix): the one with most points here."""
    keys = {p.key() for p in periods}
    counts = [
        (sum(1 for (m, k) in ctx.values if m == metric and k in keys), -i, metric)
        for i, metric in enumerate(metrics)
    ]
    return max(counts)[2]


def box4_point(
    ctx: Context, chart_id: UUID, metric: CanonicalMetric, period: FinancialPeriod
) -> ChartPoint:
    key = period.key()
    common: dict[str, Any] = {
        "point_id": uuid5(chart_id, f"{metric.value}|{key}"),
        "x_key": key,
        "x_label": short_label(period),
        "period_type": period.period_type,
        "period_start": period.period_start,
        "period_end": period.period_end,
    }
    value = ctx.values.get((metric, key))
    if value is None:
        record = ctx.unresolved.get((metric, key))
        if record is not None and record.outcome in (Outcome.CONFLICT, Outcome.AMBIGUOUS):
            status, reason = "INPUT_CONFLICT", f"Box 4 {record.outcome.value}: no canonical value"
        elif record is not None:
            status, reason = "LOW_CONFIDENCE_INPUT", f"Box 4 {record.outcome.value}"
        else:
            status, reason = "INPUT_MISSING", f"no canonical {metric.value} for {period.label}"
        return ChartPoint(
            **common,
            value=None,
            display_value=None,
            status=status,
            confidence=None,
            missing_reason=reason,
            source=PointSource(
                layer=SourceLayer.BOX4,
                kind=PointSourceKind.CANONICAL_VALUE,
                ref_id=None,
                reconciliation_outcome=record.outcome.value if record else None,
            ),
        )
    observation = ctx.observations[value.selected_observation_id]
    warnings = []
    if value.outcome in (Outcome.SOURCE_PRECEDENCE, Outcome.RESTATEMENT):
        warnings.append(f"INPUT_{value.outcome.value}")
    return ChartPoint(
        **common,
        value=value.value,
        display_value=None,
        status="CALCULATED",
        confidence=value.confidence,
        source=PointSource(
            layer=SourceLayer.BOX4,
            kind=PointSourceKind.CANONICAL_VALUE,
            ref_id=value.canonical_value_id,
            canonical_value_ids=(value.canonical_value_id,),
            observation_ids=(value.selected_observation_id,),
            source_locators=(observation.source.locator,),
            reconciliation_outcome=value.outcome.value,
        ),
        warnings=tuple(warnings),
    )


# --- spec assembly -------------------------------------------------------------------------------


def chart_id(ctx: Context, definition: ChartDefinition) -> UUID:
    return uuid5(
        NS,
        f"{CHART_REGISTRY_VERSION}|{ctx.box4.company_key}|{ctx.box4.dataset_id}|"
        f"{ctx.box5.analytics_id}|{definition.code}|{ctx.basis.value}",
    )


def skip(ctx: Context, definition: ChartDefinition, status: ChartStatus, reason: str) -> Planned:
    return Planned(
        None,
        SkippedChart(
            chart_code=definition.code,
            reporting_basis=ctx.basis,
            category=definition.category,
            status=status,
            reason=clip(reason, 1000),
        ),
    )


def finish_series(
    ctx: Context,
    cid: UUID,
    sdef_label: str,
    code: str,
    layer: SourceLayer,
    unit: str,
    points: list[ChartPoint],
    axis: AxisKind,
    render: str,
    encoding: SeriesEncoding = SeriesEncoding.VALUE,
    note: str | None = None,
    formula: str | None = None,
) -> ChartSeries:
    valid = sum(1 for p in points if p.value is not None)
    confidence = Counter(p.confidence.value for p in points if p.confidence)
    return ChartSeries(
        series_id=uuid5(cid, f"series|{code}|{layer.value}"),
        label=clip(sdef_label, 90),
        metric_code=code,
        source_layer=layer,
        encoding=encoding,
        encoding_note=note,
        unit=unit,
        currency=ctx.currency if unit == "MONEY" else None,
        reporting_basis=ctx.basis,
        frequency=axis,
        render_as=cast(Literal["BAR", "LINE", "MARKER"], render),
        formula_id=formula,
        points=tuple(points),
        valid_points=valid,
        gaps=len(points) - valid,
        confidence_summary=dict(sorted(confidence.items())),
        warnings=tuple(sorted({w for p in points for w in p.warnings})),
    )


def format_points(series: list[ChartSeries], unit: str) -> tuple[list[ChartSeries], str, Decimal]:
    values = [p.value for s in series for p in s.points if p.value is not None]
    if unit == "MONEY":
        display_unit, divisor = money_display(series[0].currency if series else None, values)
    else:
        display_unit, divisor = UNIT_LABEL.get(unit, unit.lower()), D(1)
    out = []
    for s in series:
        points = tuple(
            p
            if p.display_value is not None
            else p.model_copy(
                update={"display_value": display(p.value, unit, divisor, display_unit)}
            )
            for p in s.points
        )
        out.append(s.model_copy(update={"points": points}))
    return out, display_unit, divisor


def annotations(ctx: Context, definition: ChartDefinition, keys: set[str]) -> list[ChartAnnotation]:
    out: list[ChartAnnotation] = []
    for a in ctx.box5.anomalies:
        if (
            a.reporting_basis == ctx.basis
            and a.code in definition.anomaly_rules
            and a.periods
            and a.periods[0].key() in keys
        ):
            out.append(
                ChartAnnotation(
                    kind="ANOMALY",
                    ref_id=a.signal_id,
                    rule_id=a.rule_id,
                    code=a.code,
                    severity=a.severity.value,
                    x_key=a.periods[0].key(),
                    label=clip(f"{a.code.replace('_', ' ').lower()} ({a.severity.value})", 80),
                    metric_ids=a.metric_ids,
                    canonical_value_ids=a.canonical_value_ids,
                )
            )
    for t in ctx.box5.trends:
        if t.reporting_basis == ctx.basis and t.series_code in definition.trend_codes:
            out.append(
                ChartAnnotation(
                    kind="TREND",
                    ref_id=t.trend_id,
                    rule_id=t.rule_id,
                    code=t.series_code,
                    label=f"Box 5 trend: {t.classification.value}",
                    metric_ids=t.metric_ids,
                )
            )
    return out[: ctx.limits.max_annotations_per_chart]


def assemble(
    ctx: Context,
    definition: ChartDefinition,
    cid: UUID,
    series: list[ChartSeries],
    unit: str,
    status: ChartStatus,
    reason: str,
    x_label: str,
    notes: Sequence[str] = (),
    y_ticks: tuple[tuple[Decimal, str], ...] = (),
    extra_annotations: Sequence[ChartAnnotation] = (),
    size: tuple[int, int] = STANDARD,
    categories: Sequence[tuple[str, str]] = (),
) -> ChartSpec:
    series = series[: ctx.limits.max_series_per_chart]
    formatted, display_unit, divisor = format_points(series, unit)
    keys = {p.x_key for s in formatted for p in s.points}
    notes_list = list(notes)
    marks = annotations(ctx, definition, keys) + list(extra_annotations)
    anomaly_by_key: dict[str, list[UUID]] = {}
    for a in marks:
        if a.kind == "ANOMALY" and a.x_key:
            anomaly_by_key.setdefault(a.x_key, []).append(a.ref_id)
    if formatted and anomaly_by_key:
        first = formatted[0]
        formatted[0] = first.model_copy(
            update={
                "points": tuple(
                    p.model_copy(update={"anomaly_ids": tuple(anomaly_by_key.get(p.x_key, ()))})
                    for p in first.points
                )
            }
        )
    axis_categories = list(categories) or list(
        dict.fromkeys((p.x_key, p.x_label) for s in formatted for p in s.points)
    )
    span = ""
    if axis_categories and definition.axis != AxisKind.CATEGORY:
        span = f" · {axis_categories[0][1]}–{axis_categories[-1][1]}"
    trend_text = " · ".join(a.label for a in marks if a.kind == "TREND")
    subtitle = clip(
        " · ".join(
            x
            for x in [
                f"{ctx.basis.value.title()}{span}",
                "" if unit == "ORDINAL" else display_unit,
                *notes_list,
                trend_text,
            ]
            if x
        ),
        300,
    )
    used_box5 = any(s.source_layer == SourceLayer.BOX5 for s in formatted)
    footer = (
        "Source: company filings / Screener (Box 4 canonical values)"
        + (" · Stock Research AI calculations (Box 5)" if used_box5 else "")
        + f" · {definition.code} · {CHART_REGISTRY_VERSION} · n/a = not available"
    )
    kinds = Counter(
        p.source.kind.value for s in formatted for p in s.points if p.source.ref_id is not None
    )
    return ChartSpec(
        chart_id=cid,
        chart_code=definition.code,
        company_key=ctx.box4.company_key,
        title=clip(definition.title, ctx.limits.max_label_chars),
        subtitle=subtitle,
        chart_type=definition.chart_type,
        category=definition.category,
        reporting_basis=ctx.basis,
        frequency=definition.axis,
        currency=ctx.currency if unit == "MONEY" else None,
        unit=unit,
        width=size[0],
        height=size[1],
        series=tuple(formatted),
        x_axis=ChartAxis(label=x_label, display_unit="", categories=tuple(axis_categories)),
        y_axis=ChartAxis(
            label=display_unit,
            display_unit=display_unit,
            scale_divisor=divisor,
            zero_baseline=definition.chart_type
            in (ChartType.BAR, ChartType.GROUPED_BAR, ChartType.HORIZONTAL_BAR),
            tick_labels=y_ticks,
        ),
        annotations=tuple(marks),
        footer=footer,
        provenance_summary=dict(sorted(kinds.items())),
        warnings=tuple(ctx.warnings),
        status=status,
        status_reason=clip(reason, 1000),
        source_dataset_ids=(ctx.box4.dataset_id, ctx.box5.analytics_id),
    )


def grade(definition: ChartDefinition, series: list[ChartSeries]) -> tuple[ChartStatus, str]:
    best = max((s.valid_points for s in series), default=0)
    if best < definition.min_points:
        detail = "; ".join(
            f"{s.metric_code}: {s.valid_points} valid of {len(s.points)}"
            + (f" ({_gap_reasons(s)})" if s.gaps else "")
            for s in series
        )
        return ChartStatus.NO_DATA, f"fewer than {definition.min_points} valid points - {detail}"
    if all(s.gaps == 0 and s.valid_points >= definition.preferred_points for s in series):
        return ChartStatus.READY, "all points available"
    detail = "; ".join(f"{s.metric_code}: {s.valid_points} valid, {s.gaps} gaps" for s in series)
    return ChartStatus.PARTIAL, f"gaps or a short history kept visible - {detail}"


def _gap_reasons(s: ChartSeries) -> str:
    reasons = Counter(p.status for p in s.points if p.value is None)
    first = next((p.missing_reason for p in s.points if p.value is None), "")
    return ", ".join(f"{k} x{v}" for k, v in sorted(reasons.items())) + f"; e.g. {first}"


# --- planners -------------------------------------------------------------------------------------


def plan_time_series(ctx: Context, definition: ChartDefinition) -> Planned:
    cid = chart_id(ctx, definition)
    periods = ctx.axis_periods(definition.axis)
    if not periods:
        return skip(
            ctx,
            definition,
            ChartStatus.NO_DATA,
            f"Box 5 analysed no {definition.axis.value.lower()} periods for this basis",
        )
    for sdef in definition.series:
        if sdef.box5:
            found = ctx.by_code.get(sdef.box5, [])
            if found and all(m.status == CalculationStatus.UNSUPPORTED_SECTOR for m in found):
                return skip(
                    ctx,
                    definition,
                    ChartStatus.UNSUPPORTED_SECTOR,
                    f"Box 5 marks {sdef.box5} UNSUPPORTED_SECTOR for this company",
                )
    series: list[ChartSeries] = []
    units: set[str] = set()
    for sdef in definition.series:
        if sdef.box5:
            points = [box5_point(ctx, cid, sdef, p) for p in periods]
            units_found = {m.unit.value for m in ctx.by_code.get(sdef.box5, [])}
            unit = next(iter(units_found)) if units_found else "PERCENT"
            formula = next(
                (
                    m.formula_id
                    for m in ctx.by_code.get(sdef.box5, [])
                    if m.status == CalculationStatus.CALCULATED
                ),
                None,
            )
            series.append(
                finish_series(
                    ctx,
                    cid,
                    sdef.label,
                    sdef.box5,
                    SourceLayer.BOX5,
                    unit,
                    points,
                    definition.axis,
                    sdef.render_as,
                    formula=formula,
                )
            )
        else:
            metric = choose_box4_metric(ctx, sdef.box4, periods)
            points = [box4_point(ctx, cid, metric, p) for p in periods]
            unit = "MONEY"
            series.append(
                finish_series(
                    ctx,
                    cid,
                    sdef.label,
                    metric.value,
                    SourceLayer.BOX4,
                    unit,
                    points,
                    definition.axis,
                    sdef.render_as,
                )
            )
        units.add(unit)
    if len(units) > 1:
        return skip(
            ctx,
            definition,
            ChartStatus.NOT_APPLICABLE,
            f"series have different units {sorted(units)}; one axis never mixes units",
        )
    status, reason = grade(definition, series)
    if status == ChartStatus.NO_DATA:
        return skip(ctx, definition, status, reason)
    x_label = {
        "ANNUAL_FLOW": "Fiscal year",
        "ANNUAL_BALANCE": "As at fiscal year end",
        "QUARTERLY": "Quarter",
    }.get(definition.axis.value, "")
    return Planned(
        assemble(ctx, definition, cid, series, units.pop(), status, reason, x_label), None
    )


def _category_point(
    cid: UUID,
    key: str,
    label: str,
    value: Decimal | None,
    status: str,
    confidence: Confidence | None,
    reason: str | None,
    source: PointSource,
    display_value: str | None = None,
    warnings: tuple[str, ...] = (),
) -> ChartPoint:
    return ChartPoint(
        point_id=uuid5(cid, f"category|{key}"),
        x_key=key,
        x_label=label,
        period_type=None,
        value=value,
        display_value=display_value,
        status=status,
        confidence=confidence,
        missing_reason=reason,
        source=source,
        warnings=warnings,
    )


def plan_cagr(ctx: Context, definition: ChartDefinition) -> Planned:
    cid = chart_id(ctx, definition)
    latest = ctx.frame.annual_periods[-1] if ctx.frame.annual_periods else None
    if latest is None:
        return skip(ctx, definition, ChartStatus.NO_DATA, "no fiscal year in Box 5")
    p = ctx.periods.fy(latest)
    series = []
    for name in ("REVENUE", "PAT"):
        points = []
        for years in (3, 5, 10):
            sdef = SeriesDefinition(name, box5=f"{name}_CAGR_{years}Y")
            points.append(box5_point(ctx, cid, sdef, p, x_key=f"{years}Y", x_label=f"{years}Y"))
        series.append(
            finish_series(
                ctx,
                cid,
                f"{name.title() if name == 'REVENUE' else name} CAGR",
                f"{name}_CAGR",
                SourceLayer.BOX5,
                "PERCENT",
                points,
                AxisKind.CATEGORY,
                "BAR",
            )
        )
    status, reason = grade(definition, series)
    if status == ChartStatus.NO_DATA:
        return skip(ctx, definition, status, reason)
    return Planned(
        assemble(
            ctx,
            definition,
            cid,
            series,
            "PERCENT",
            status,
            reason,
            f"Window ending {p.label}",
            notes=(f"as of {p.label}",),
        ),
        None,
    )


def _ordinal_ticks(ranks: Mapping[Any, Decimal]) -> tuple[tuple[Decimal, str], ...]:
    return tuple(sorted((v, str(getattr(k, "value", k))) for k, v in ranks.items()))


def plan_checks(ctx: Context, definition: ChartDefinition) -> Planned:
    cid = chart_id(ctx, definition)
    checks = [c for c in ctx.box5.earnings_quality if c.reporting_basis == ctx.basis]
    if not checks:
        return skip(ctx, definition, ChartStatus.NO_DATA, "Box 5 produced no checks for this basis")
    points = []
    for c in checks:
        source = PointSource(
            layer=SourceLayer.BOX5,
            kind=PointSourceKind.EARNINGS_QUALITY_CHECK,
            ref_id=c.check_id,
            formula_or_rule_id=c.rule_id,
            canonical_value_ids=c.canonical_value_ids,
            metric_ids=c.metric_ids,
        )
        if c.severity is None:
            points.append(
                _category_point(
                    cid,
                    c.code,
                    c.code,
                    None,
                    c.status.value,
                    None,
                    clip(f"{c.status.value}: {c.observation}", 500),
                    source,
                )
            )
        else:
            points.append(
                _category_point(
                    cid,
                    c.code,
                    c.code,
                    SEVERITY_RANK[c.severity],
                    c.status.value,
                    None,
                    None,
                    source,
                    display_value=c.severity.value,
                )
            )
    series = [
        finish_series(
            ctx,
            cid,
            "Screening signal severity",
            "EARNINGS_QUALITY_CHECKS",
            SourceLayer.BOX5,
            "ORDINAL",
            points,
            AxisKind.CATEGORY,
            "BAR",
            SeriesEncoding.ORDINAL_STATUS,
            "INFO = 0, LOW = 1, MEDIUM = 2, HIGH = 3 (display rank only)",
        )
    ]
    status, reason = grade(definition, series)
    if status == ChartStatus.NO_DATA:
        return skip(ctx, definition, status, reason)
    period = next((c.period.label for c in checks if c.period), "")
    return Planned(
        assemble(
            ctx,
            definition,
            cid,
            series,
            "ORDINAL",
            status,
            reason,
            "Screening check",
            notes=(f"{period} · signals may require review",),
            y_ticks=_ordinal_ticks(SEVERITY_RANK),
            size=LANDSCAPE,
        ),
        None,
    )


def plan_model(ctx: Context, definition: ChartDefinition) -> Planned:
    cid = chart_id(ctx, definition)
    model, variant = {
        "PIOTROSKI": (ForensicModel.PIOTROSKI_F_SCORE, None),
        "ALTMAN": (ForensicModel.ALTMAN_Z, "Z_DOUBLE_PRIME_1995"),
        "BENEISH": (ForensicModel.BENEISH_M, None),
    }[definition.special or ""]
    result = next(
        (
            r
            for r in ctx.box5.forensic_scores
            if r.reporting_basis == ctx.basis
            and r.model == model
            and (variant is None or r.variant == variant)
        ),
        None,
    )
    if result is None:
        return skip(ctx, definition, ChartStatus.NOT_APPLICABLE, f"Box 5 has no {model.value}")
    if result.status == CalculationStatus.UNSUPPORTED_SECTOR:
        return skip(ctx, definition, ChartStatus.UNSUPPORTED_SECTOR, result.applicability)
    pass_fail = model == ForensicModel.PIOTROSKI_F_SCORE
    points = []
    for c in result.components:
        source = PointSource(
            layer=SourceLayer.BOX5,
            kind=PointSourceKind.FORENSIC_COMPONENT,
            ref_id=result.result_id,
            formula_or_rule_id=c.formula[:200],
            component=c.name,
            canonical_value_ids=c.canonical_value_ids,
            metric_ids=c.metric_ids,
        )
        if c.status == ComponentStatus.UNAVAILABLE:
            points.append(
                _category_point(
                    cid,
                    c.name,
                    c.name,
                    None,
                    c.status.value,
                    None,
                    clip("UNAVAILABLE: " + "; ".join(c.missing_inputs), 500),
                    source,
                )
            )
        elif pass_fail:
            points.append(
                _category_point(
                    cid,
                    c.name,
                    c.name,
                    D(1) if c.status == ComponentStatus.PASS else D(0),
                    c.status.value,
                    None,
                    None,
                    source,
                    display_value=c.status.value,
                )
            )
        else:
            points.append(
                _category_point(cid, c.name, c.name, c.value, c.status.value, None, None, source)
            )
    unit = "ORDINAL" if pass_fail else "TIMES"
    series = [
        finish_series(
            ctx,
            cid,
            "Component",
            f"{model.value}_COMPONENTS",
            SourceLayer.BOX5,
            unit,
            points,
            AxisKind.CATEGORY,
            "BAR",
            SeriesEncoding.ORDINAL_STATUS if pass_fail else SeriesEncoding.VALUE,
            "PASS = 1, FAIL = 0 (display only)" if pass_fail else None,
            formula=result.variant,
        )
    ]
    status, reason = grade(definition, series)
    if status == ChartStatus.NO_DATA:
        return skip(ctx, definition, status, f"no component available - {result.status.value}")
    score = (
        f"score {result.score} ({result.zone})"
        if result.score is not None
        else f"not scored: {result.availability_label}"
    )
    ticks = ((D(0), "FAIL"), (D(1), "PASS")) if pass_fail else ()
    return Planned(
        assemble(
            ctx,
            definition,
            cid,
            series,
            unit,
            status,
            reason,
            "Component",
            notes=(score, "screening model, not a conclusion"),
            y_ticks=ticks,
            size=LANDSCAPE,
        ),
        None,
    )


def plan_trends(ctx: Context, definition: ChartDefinition) -> Planned:
    cid = chart_id(ctx, definition)
    trends = [t for t in ctx.box5.trends if t.reporting_basis == ctx.basis]
    if not trends:
        return skip(ctx, definition, ChartStatus.NO_DATA, "Box 5 produced no trends")
    points = []
    for t in trends:
        source = PointSource(
            layer=SourceLayer.BOX5,
            kind=PointSourceKind.TREND,
            ref_id=t.trend_id,
            formula_or_rule_id=t.rule_id,
            metric_ids=t.metric_ids,
        )
        rank = TREND_RANK.get(t.classification)
        reason = None if rank is not None else f"{t.classification.value}: {t.explanation}"
        points.append(
            _category_point(
                cid,
                t.series_code,
                t.series_code,
                rank,
                t.classification.value,
                None,
                clip(reason, 500) if reason else None,
                source,
                display_value=t.classification.value if rank is not None else None,
            )
        )
    series = [
        finish_series(
            ctx,
            cid,
            "Box 5 trend classification",
            "TRENDS",
            SourceLayer.BOX5,
            "ORDINAL",
            points,
            AxisKind.CATEGORY,
            "BAR",
            SeriesEncoding.ORDINAL_STATUS,
            "STRONGLY_DETERIORATING = -2 ... STRONGLY_IMPROVING = 2; VOLATILE "
            "and INSUFFICIENT_HISTORY have no direction (shown n/a)",
        )
    ]
    status, reason_text = grade(definition, series)
    if status == ChartStatus.NO_DATA:
        return skip(ctx, definition, status, reason_text)
    return Planned(
        assemble(
            ctx,
            definition,
            cid,
            series,
            "ORDINAL",
            status,
            reason_text,
            "Series",
            y_ticks=_ordinal_ticks(TREND_RANK),
            size=LANDSCAPE,
        ),
        None,
    )


def plan_health(ctx: Context, definition: ChartDefinition) -> Planned:
    cid = chart_id(ctx, definition)
    signals = [h for h in ctx.box5.health if h.reporting_basis == ctx.basis]
    if not signals:
        return skip(ctx, definition, ChartStatus.NO_DATA, "Box 5 produced no health signals")
    points = []
    for h in signals:
        source = PointSource(
            layer=SourceLayer.BOX5,
            kind=PointSourceKind.HEALTH_SIGNAL,
            ref_id=uuid5(
                NS, f"{ctx.box5.analytics_id}|{h.reporting_basis.value}|{h.dimension.value}"
            ),
            formula_or_rule_id=h.rule_id,
            component=h.dimension.value,
            metric_ids=h.metric_ids,
        )
        rank = HEALTH_RANK.get(h.state)
        points.append(
            _category_point(
                cid,
                h.dimension.value,
                h.dimension.value,
                rank,
                h.state.value,
                None,
                None if rank is not None else clip(f"{h.state.value}: {'; '.join(h.reasons)}", 500),
                source,
                display_value=h.state.value if rank is not None else None,
            )
        )
    series = [
        finish_series(
            ctx,
            cid,
            "Box 5 health state",
            "HEALTH",
            SourceLayer.BOX5,
            "ORDINAL",
            points,
            AxisKind.CATEGORY,
            "BAR",
            SeriesEncoding.ORDINAL_STATUS,
            "WEAK = -2, WATCH = -1, NEUTRAL = 0, HEALTHY = 1, STRONG = 2",
        )
    ]
    status, reason = grade(definition, series)
    if status == ChartStatus.NO_DATA:
        return skip(ctx, definition, status, reason)
    return Planned(
        assemble(
            ctx,
            definition,
            cid,
            series,
            "ORDINAL",
            status,
            reason,
            "Dimension",
            y_ticks=_ordinal_ticks(HEALTH_RANK),
            size=LANDSCAPE,
        ),
        None,
    )


def plan_anomalies(ctx: Context, definition: ChartDefinition) -> Planned:
    cid = chart_id(ctx, definition)
    periods = {p.key(): p for p in ctx.axis_periods(AxisKind.TIMELINE)}
    anomalies = [
        a
        for a in ctx.box5.anomalies
        if a.reporting_basis == ctx.basis and a.periods and a.periods[0].key() in periods
    ]
    if not anomalies:
        return skip(
            ctx,
            definition,
            ChartStatus.NO_DATA,
            "Box 5 reported no annual anomalies for this basis",
        )
    points = []
    for a in anomalies:
        period = periods[a.periods[0].key()]
        source = PointSource(
            layer=SourceLayer.BOX5,
            kind=PointSourceKind.ANOMALY,
            ref_id=a.signal_id,
            formula_or_rule_id=a.rule_id,
            canonical_value_ids=a.canonical_value_ids,
            metric_ids=a.metric_ids,
        )
        points.append(
            ChartPoint(
                point_id=uuid5(cid, f"anomaly|{a.signal_id}"),
                x_key=period.key(),
                x_label=short_label(period),
                period_type=period.period_type,
                period_start=period.period_start,
                period_end=period.period_end,
                y_category=a.code,
                value=SEVERITY_RANK[a.severity],
                display_value=a.severity.value,
                status="TRIGGERED",
                confidence=None,
                severity=a.severity.value,
                source=source,
                anomaly_ids=(a.signal_id,),
            )
        )
    points.sort(key=lambda p: (p.period_end or date.min, p.y_category or ""))
    series = [
        finish_series(
            ctx,
            cid,
            "Anomaly (rule-based flag)",
            "ANOMALIES",
            SourceLayer.BOX5,
            "ORDINAL",
            points,
            AxisKind.TIMELINE,
            "MARKER",
            SeriesEncoding.ORDINAL_STATUS,
            "marker size: LOW = 1, MEDIUM = 2, HIGH = 3",
        )
    ]
    marks = [
        ChartAnnotation(
            kind="ANOMALY",
            ref_id=a.signal_id,
            rule_id=a.rule_id,
            code=a.code,
            severity=a.severity.value,
            x_key=a.periods[0].key(),
            label=clip(f"{a.code.replace('_', ' ').lower()} ({a.severity.value})", 80),
            metric_ids=a.metric_ids,
            canonical_value_ids=a.canonical_value_ids,
        )
        for a in anomalies
    ][: ctx.limits.max_annotations_per_chart]
    if len(anomalies) > ctx.limits.max_annotations_per_chart:
        ctx.warnings.append(
            ChartWarning(code="ANNOTATION_LIMIT_REACHED", detail=f"{len(anomalies)} anomalies")
        )
    return Planned(
        assemble(
            ctx,
            definition,
            cid,
            series,
            "ORDINAL",
            ChartStatus.READY,
            f"{len(anomalies)} Box 5 anomalies",
            "Fiscal year",
            notes=("rule-based flags that may require review",),
            extra_annotations=marks,
            size=LANDSCAPE,
            categories=[(k, short_label(p)) for k, p in periods.items()],
        ),
        None,
    )


SPECIAL = {
    "CAGR": plan_cagr,
    "CHECKS": plan_checks,
    "PIOTROSKI": plan_model,
    "ALTMAN": plan_model,
    "BENEISH": plan_model,
    "TRENDS": plan_trends,
    "HEALTH": plan_health,
    "ANOMALIES": plan_anomalies,
}


def plan_chart(ctx: Context, definition: ChartDefinition) -> Planned:
    if not sector_applies(ctx.box5.sector, definition.sectors):
        return skip(
            ctx,
            definition,
            ChartStatus.UNSUPPORTED_SECTOR,
            f"defined for {[s.value for s in definition.sectors]}; Box 5 sector is "
            f"{ctx.box5.sector.value}",
        )
    ctx.warnings = []
    if definition.special:
        return SPECIAL[definition.special](ctx, definition)
    return plan_time_series(ctx, definition)


def plan_all(
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset,
    limits: ChartLimits,
    definitions: Sequence[ChartDefinition] = CHARTS,
) -> list[Planned]:
    out = []
    for frame in box5.frames:
        ctx = Context(box4, box5, frame, limits)
        for definition in definitions:
            out.append(plan_chart(ctx, definition))
    return out
