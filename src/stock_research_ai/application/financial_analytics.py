"""DETAILED_RESEARCH_V1 Box 5: CanonicalFinancialDataset -> FinancialAnalyticsDataset.

Pipeline per basis (each analysed in its dominant currency only):
  period selection -> building blocks -> growth / margins / returns / DuPont -> leverage /
  liquidity -> working capital / cash flow / accruals -> windows (CAGR, cumulative) ->
  quarters / TTM -> series -> trends -> anomalies -> forensic screens -> health signals.

No AI, no network, no clock: `generated_at` is injected, IDs are uuid5, output ordering
is sorted, and every threshold below is a versioned constant (a convention, not a truth).
"""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.financial_forensics import (
    altman,
    beneish,
    forensic_signals,
    piotroski,
)
from stock_research_ai.application.financial_metrics import (
    NS,
    Engine,
    Fact,
    Frame,
    Periods,
    annual_metrics,
    base_balance_metrics,
    base_flow_metrics,
    quantize,
    quarterly_metrics,
    shift,
    ttm_metrics,
    window_metrics,
)
from stock_research_ai.domain.financial_analytics import (
    ANALYTICS_ENGINE_VERSION,
    AnalyticalPeriod,
    AnalyticalSeries,
    AnalyticsFrame,
    AnalyticsLimits,
    AnalyticsStatistics,
    AnalyticsUnit,
    AnalyticsWarning,
    AnomalySignal,
    CalculationStatus,
    DerivedMetric,
    FinancialAnalyticsDataset,
    FinancialHealthSignal,
    ForensicModel,
    ForensicModelResult,
    ForensicSignal,
    HealthDimension,
    HealthState,
    PeriodRole,
    SeriesPoint,
    SignalStatus,
    TrendAssessment,
    TrendClass,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    Currency,
    FinancialPeriod,
    Outcome,
    PeriodType,
    ReportingBasis,
    ValueKind,
)

S = CalculationStatus
U = AnalyticsUnit
D = Decimal
TREND_RULES_VERSION = "trend-rules/1"
ANOMALY_RULES_VERSION = "anomaly-rules/1"
HEALTH_RULES_VERSION = "health-rules/1"
TREND_WINDOW = 5  # the last 5 fiscal years; at least 3 calculated points are required
TREND_MIN_POINTS = 3
#: (strong, mild) thresholds of a trend's total change, by series kind
TREND_THRESHOLDS: dict[str, tuple[Decimal, Decimal]] = {
    "LEVEL": (D(5), D(1)),  # percentage points for PERCENT series
    "TIMES": (D("0.3"), D("0.1")),
    "DAYS": (D(15), D(5)),
    "MONEY": (D(30), D(10)),  # percent change relative to |first value|
}


@dataclass(frozen=True)
class TrendSpec:
    code: str
    kind: str
    higher_is_better: bool
    banks: bool = False
    industrial: bool = True


TRENDS: tuple[TrendSpec, ...] = (
    TrendSpec("REVENUE_GROWTH_YOY", "LEVEL", True, banks=True),
    TrendSpec("EBITDA_MARGIN", "LEVEL", True),
    TrendSpec("OPERATING_MARGIN", "LEVEL", True),
    TrendSpec("PAT_MARGIN", "LEVEL", True, banks=True),
    TrendSpec("ROE", "LEVEL", True, banks=True),
    TrendSpec("ROA", "LEVEL", True, banks=True),
    TrendSpec("ROCE", "LEVEL", True),
    TrendSpec("ROIC", "LEVEL", True),
    TrendSpec("CFO_TO_PAT", "TIMES", True),
    TrendSpec("FCF", "MONEY", True),
    TrendSpec("GROSS_DEBT", "MONEY", False),
    TrendSpec("RECEIVABLE_DAYS", "DAYS", False),
    TrendSpec("INVENTORY_DAYS", "DAYS", False),
    TrendSpec("RECEIVABLES_TO_REVENUE", "LEVEL", False),
    TrendSpec("INVENTORY_TO_REVENUE", "LEVEL", False),
    TrendSpec("OTHER_INCOME_TO_PBT", "LEVEL", False),
    TrendSpec("ACCRUAL_RATIO", "LEVEL", False),
    TrendSpec("DEPOSITS_GROWTH_YOY", "LEVEL", True, banks=True, industrial=False),
    TrendSpec("ADVANCES_GROWTH_YOY", "LEVEL", True, banks=True, industrial=False),
    TrendSpec("FINANCING_PROFIT_MARGIN", "LEVEL", True, banks=True, industrial=False),
)
#: balance-sheet based building blocks live on point-in-time periods; trends read them at
#: the fiscal year end
BALANCE_SERIES = {"GROSS_DEBT"}
QUARTERLY_SERIES = ("REVENUE_GROWTH_YOY", "OPERATING_MARGIN", "PAT_MARGIN")


@dataclass(frozen=True)
class AnomalyRule:
    code: str
    metric: str
    watch: Decimal
    elevated: Decimal
    mode: str  # ABS (|x| >= t), HIGH (x >= t), LOW (x <= t)
    meaning: str
    banks: bool = False
    frequency: str = "ANNUAL"


ANOMALY_RULES: tuple[AnomalyRule, ...] = (
    AnomalyRule(
        "EBITDA_MARGIN_SHIFT",
        "EBITDA_MARGIN_CHANGE_YOY",
        D(300),
        D(600),
        "ABS",
        "EBITDA margin moved sharply in one year",
    ),
    AnomalyRule(
        "OPERATING_MARGIN_SHIFT",
        "OPERATING_MARGIN_CHANGE_YOY",
        D(300),
        D(600),
        "ABS",
        "operating margin moved sharply in one year",
    ),
    AnomalyRule(
        "PAT_MARGIN_SHIFT",
        "PAT_MARGIN_CHANGE_YOY",
        D(300),
        D(600),
        "ABS",
        "PAT margin moved sharply in one year",
        banks=True,
    ),
    AnomalyRule(
        "RECEIVABLES_OUTGROWING_REVENUE",
        "RECEIVABLES_VS_REVENUE_GROWTH",
        D(15),
        D(30),
        "HIGH",
        "receivables grew much faster than revenue",
    ),
    AnomalyRule(
        "INVENTORY_OUTGROWING_REVENUE",
        "INVENTORY_VS_REVENUE_GROWTH",
        D(15),
        D(30),
        "HIGH",
        "inventory grew much faster than revenue",
    ),
    AnomalyRule(
        "PAT_OUTGROWING_CFO",
        "PAT_VS_CFO_GROWTH_GAP",
        D(25),
        D(50),
        "HIGH",
        "reported profit grew much faster than operating cash flow",
    ),
    AnomalyRule(
        "TAX_RATE_CHANGE",
        "TAX_RATE_CHANGE_YOY",
        D(5),
        D(10),
        "ABS",
        "the effective tax rate changed sharply",
        banks=True,
    ),
    AnomalyRule(
        "OTHER_INCOME_SPIKE",
        "OTHER_INCOME_TO_PBT_CHANGE_YOY",
        D(5),
        D(10),
        "HIGH",
        "other income became a much larger share of pre-tax profit",
    ),
    AnomalyRule(
        "DEBT_SPIKE", "GROSS_DEBT_GROWTH_YOY", D(50), D(100), "HIGH", "gross debt rose sharply"
    ),
    AnomalyRule(
        "CFO_COLLAPSE", "CFO_GROWTH_YOY", D(-40), D(-60), "LOW", "operating cash flow fell sharply"
    ),
    AnomalyRule(
        "BALANCE_SHEET_JUMP",
        "TOTAL_ASSETS_GROWTH_YOY",
        D(30),
        D(60),
        "ABS",
        "total assets changed sharply in one year",
        banks=True,
    ),
    AnomalyRule(
        "DEPOSITS_JUMP",
        "DEPOSITS_GROWTH_YOY",
        D(30),
        D(60),
        "ABS",
        "deposits changed sharply in one year",
        banks=True,
    ),
    AnomalyRule(
        "QUARTERLY_OPERATING_MARGIN_SHIFT",
        "OPERATING_MARGIN_CHANGE_YOY",
        D(300),
        D(600),
        "ABS",
        "quarterly operating margin moved sharply versus the same quarter a year earlier",
        frequency="QUARTERLY",
    ),
)
ANNUAL_QUARTERLY_DIVERGENCE = (D(15), D(30))  # |latest quarter YoY - latest FY YoY|, pp


# --- frames and periods ------------------------------------------------------------------------


def frame_currencies(data: CanonicalFinancialDataset) -> dict[ReportingBasis, Currency]:
    """The dominant MONEY currency per basis (ties: alphabetical); others are excluded."""
    counts: dict[ReportingBasis, Counter[str]] = {}
    for value in data.canonical_values:
        if value.value_kind == ValueKind.MONEY and value.reporting_basis != ReportingBasis.UNKNOWN:
            counts.setdefault(value.reporting_basis, Counter())[value.currency.value] += 1
    return {
        basis: Currency(sorted(c.items(), key=lambda i: (-i[1], i[0]))[0][0])
        for basis, c in sorted(counts.items(), key=lambda i: i[0].value)
    }


def select_periods(
    e: Engine, annual: Sequence[date], quarters: Sequence[date]
) -> list[AnalyticalPeriod]:
    P = e.periods
    basis = e.frame.basis
    out: list[AnalyticalPeriod] = []

    def add(
        role: PeriodRole,
        period: FinancialPeriod | None,
        reason: str,
        start: FinancialPeriod | None = None,
    ) -> None:
        out.append(
            AnalyticalPeriod(
                role=role,
                reporting_basis=basis,
                status="SELECTED" if period else "UNAVAILABLE",
                period=period,
                start_period=start,
                reason=reason,
            )
        )

    latest = annual[-1] if annual else None
    add(
        PeriodRole.LATEST_FY,
        P.fy(latest) if latest else None,
        "latest fiscal year with canonical values" if latest else "no fiscal-year values",
    )
    if latest:
        prior = shift(latest, -12)
        add(
            PeriodRole.PREVIOUS_FY,
            P.fy(prior) if prior in annual else None,
            "fiscal year ending exactly 12 months earlier"
            + ("" if prior in annual else " is not in the canonical data"),
        )
        for role, years in (
            (PeriodRole.WINDOW_3Y, 3),
            (PeriodRole.WINDOW_5Y, 5),
            (PeriodRole.WINDOW_10Y, 10),
        ):
            start = shift(latest, -12 * years)
            ok = start in annual
            add(
                role,
                P.fy(latest) if ok else None,
                f"{years}-year window from the fiscal year ending {start.isoformat()}"
                + ("" if ok else " (start year not in the canonical data)"),
                P.fy(start) if ok else None,
            )
    else:
        for role in (
            PeriodRole.PREVIOUS_FY,
            PeriodRole.WINDOW_3Y,
            PeriodRole.WINDOW_5Y,
            PeriodRole.WINDOW_10Y,
        ):
            add(role, None, "no fiscal-year values")
    q = quarters[-1] if quarters else None
    add(
        PeriodRole.LATEST_QUARTER,
        P.quarter(q) if q else None,
        "latest quarter with canonical values" if q else "no quarterly values",
    )
    for role, months in ((PeriodRole.PRIOR_YEAR_QUARTER, 12), (PeriodRole.SEQUENTIAL_QUARTER, 3)):
        target = shift(q, -months) if q else None
        ok = target is not None and target in quarters
        add(
            role,
            P.quarter(target) if ok and target else None,
            f"quarter ending {months} months before the latest quarter"
            + ("" if ok else " is not in the canonical data"),
        )
    add(
        PeriodRole.LATEST_TTM,
        P.ttm() if e.frame.has_ttm else None,
        "Screener trailing twelve months (no end date in Box 4)"
        if e.frame.has_ttm
        else "no TTM values",
    )
    add(
        PeriodRole.PREVIOUS_TTM,
        None,
        "Box 4 TTM values carry no end date, so an earlier TTM cannot be identified; FY and "
        "TTM are never compared",
    )
    return out


# --- series, trends, anomalies -------------------------------------------------------------------


def _series_period(e: Engine, code: str, end: date) -> FinancialPeriod:
    return e.periods.pit(end) if code in BALANCE_SERIES else e.periods.fy(end)


def build_series(
    e: Engine, annual: Sequence[date], quarters: Sequence[date], max_points: int
) -> list[AnalyticalSeries]:
    out: list[AnalyticalSeries] = []
    codes = sorted({t.code for t in TRENDS})
    for code in codes:
        points = []
        for end in annual[-max_points:]:
            metric = e.find(code, _series_period(e, code, end))
            if metric is not None and metric.period.period_type != PeriodType.TTM:
                points.append(
                    SeriesPoint(
                        period=metric.period,
                        metric_id=metric.metric_id,
                        status=metric.status,
                        value=metric.value,
                    )
                )
        if points:
            unit = e.metrics[points[0].metric_id].unit
            out.append(
                AnalyticalSeries(
                    code=code,
                    reporting_basis=e.frame.basis,
                    frequency="ANNUAL",
                    unit=unit,
                    points=tuple(points),
                )
            )
    for code in QUARTERLY_SERIES:
        points = []
        for end in quarters[-max_points:]:
            metric = e.find(code, e.periods.quarter(end))
            if metric is not None:
                points.append(
                    SeriesPoint(
                        period=metric.period,
                        metric_id=metric.metric_id,
                        status=metric.status,
                        value=metric.value,
                    )
                )
        if points:
            unit = e.metrics[points[0].metric_id].unit
            out.append(
                AnalyticalSeries(
                    code=code,
                    reporting_basis=e.frame.basis,
                    frequency="QUARTERLY",
                    unit=unit,
                    points=tuple(points),
                )
            )
    return out


def classify_trend(
    values: Sequence[Decimal], kind: str, higher_is_better: bool
) -> tuple[TrendClass, Decimal | None]:
    """Explicit, versioned trend rule (TREND_RULES_VERSION):
    change = last - first (MONEY: % of |first|); VOLATILE when the direction reverses at
    least twice and one step is as large as the 'strong' threshold; otherwise the change
    (signed so that improvement is positive) is graded against (strong, mild)."""
    if len(values) < TREND_MIN_POINTS:
        return TrendClass.INSUFFICIENT_DATA, None
    strong, mild = TREND_THRESHOLDS[kind]
    if kind == "MONEY":
        if values[0] == 0:
            return TrendClass.INSUFFICIENT_DATA, None
        change = (values[-1] - values[0]) / abs(values[0]) * 100
        steps = [
            (b - a) / abs(a) * 100 if a != 0 else Decimal(0)
            for a, b in zip(values, values[1:], strict=False)
        ]
    else:
        change = values[-1] - values[0]
        steps = [b - a for a, b in zip(values, values[1:], strict=False)]
    signs = [1 if s > 0 else -1 for s in steps if s != 0]
    reversals = sum(1 for a, b in zip(signs, signs[1:], strict=False) if a != b)
    if reversals >= 2 and max(abs(s) for s in steps) >= strong:
        return TrendClass.VOLATILE, quantize(change)
    directional = change if higher_is_better else -change
    if directional >= strong:
        return TrendClass.STRONGLY_IMPROVING, quantize(change)
    if directional >= mild:
        return TrendClass.IMPROVING, quantize(change)
    if directional <= -strong:
        return TrendClass.STRONGLY_DETERIORATING, quantize(change)
    if directional <= -mild:
        return TrendClass.DETERIORATING, quantize(change)
    return TrendClass.STABLE, quantize(change)


def build_trends(e: Engine, annual: Sequence[date]) -> list[TrendAssessment]:
    out = []
    for spec in TRENDS:
        if (e.financial and not spec.banks) or (not e.financial and not spec.industrial):
            continue
        window = annual[-TREND_WINDOW:]
        metrics = [e.find(spec.code, _series_period(e, spec.code, end)) for end in window]
        used = [m for m in metrics if m is not None and m.value is not None]
        values = [m.value for m in used if m.value is not None]
        classification, change = classify_trend(values, spec.kind, spec.higher_is_better)
        strong, mild = TREND_THRESHOLDS[spec.kind]
        unit = "% of first value" if spec.kind == "MONEY" else (used[0].unit.value if used else "")
        if classification == TrendClass.INSUFFICIENT_DATA:
            explanation = (
                f"{spec.code}: {len(used)} calculated point(s) in the last {TREND_WINDOW} fiscal "
                f"years; {TREND_MIN_POINTS} needed"
            )
        else:
            explanation = (
                f"{spec.code}: {values[0]} ({used[0].period.label}) -> {values[-1]} "
                f"({used[-1].period.label}); change {change} {unit}; "
                f"{'higher' if spec.higher_is_better else 'lower'} is better; strong >= "
                f"{strong}, mild >= {mild}"
            )
        out.append(
            TrendAssessment(
                trend_id=uuid5(NS, f"{e.analytics_key}|{e.frame.basis.value}|trend|{spec.code}"),
                series_code=spec.code,
                reporting_basis=e.frame.basis,
                classification=classification,
                rule_id=f"{TREND_RULES_VERSION}/{spec.kind}",
                higher_is_better=spec.higher_is_better,
                points_used=len(used),
                first_period=used[0].period if used else None,
                last_period=used[-1].period if used else None,
                first_value=values[0] if values else None,
                last_value=values[-1] if values else None,
                change=change,
                explanation=explanation,
                metric_ids=tuple(m.metric_id for m in used),
            )
        )
    return out


def _severity(value: Decimal, watch: Decimal, elevated: Decimal, mode: str) -> str | None:
    if mode == "ABS":
        value = abs(value)
    if mode == "LOW":
        return "ELEVATED" if value <= elevated else "WATCH" if value <= watch else None
    return "ELEVATED" if value >= elevated else "WATCH" if value >= watch else None


def _anomaly(
    e: Engine,
    code: str,
    metric: DerivedMetric,
    severity: str,
    threshold: Decimal,
    explanation: str,
    extra: Sequence[DerivedMetric] = (),
) -> AnomalySignal:
    metrics = [metric, *extra]
    return AnomalySignal(
        signal_id=uuid5(
            NS, f"{e.analytics_key}|{e.frame.basis.value}|anomaly|{code}|{metric.period.key()}"
        ),
        code=code,
        severity=severity,  # type: ignore[arg-type]
        reporting_basis=e.frame.basis,
        period=metric.period,
        observed=metric.value if metric.value is not None else Decimal(0),
        threshold=threshold,
        unit=metric.unit,
        rule_id=f"{ANOMALY_RULES_VERSION}/{code}",
        explanation=explanation,
        metric_ids=tuple(m.metric_id for m in metrics),
        canonical_value_ids=tuple(
            sorted({i.canonical_value_id for m in metrics for i in m.inputs}, key=str)
        ),
    )


def build_anomalies(
    e: Engine, annual: Sequence[date], quarters: Sequence[date]
) -> list[AnomalySignal]:
    out: list[AnomalySignal] = []
    for rule in ANOMALY_RULES:
        if e.financial and not rule.banks:
            continue
        periods = (
            [e.periods.fy(end) for end in annual]
            if rule.frequency == "ANNUAL"
            else [e.periods.quarter(end) for end in quarters]
        )
        for period in periods:
            metric = e.find(rule.metric, period)
            if metric is None or metric.value is None:
                continue
            severity = _severity(metric.value, rule.watch, rule.elevated, rule.mode)
            if severity is None:
                continue
            threshold = rule.elevated if severity == "ELEVATED" else rule.watch
            out.append(
                _anomaly(
                    e,
                    rule.code,
                    metric,
                    severity,
                    threshold,
                    f"{rule.metric} = {metric.value} {metric.unit.value} for {period.label} "
                    f"({'|x|' if rule.mode == 'ABS' else 'x'} "
                    f"{'<=' if rule.mode == 'LOW' else '>='} {threshold}): {rule.meaning}. "
                    "A rule-based flag, not a conclusion.",
                )
            )
    if annual and quarters:
        q = e.find("REVENUE_GROWTH_YOY", e.periods.quarter(quarters[-1]))
        y = e.find("REVENUE_GROWTH_YOY", e.periods.fy(annual[-1]))
        if q is not None and y is not None and q.value is not None and y.value is not None:
            gap = q.value - y.value
            watch, elevated = ANNUAL_QUARTERLY_DIVERGENCE
            severity = _severity(gap, watch, elevated, "ABS")
            if severity is not None:
                threshold = elevated if severity == "ELEVATED" else watch
                out.append(
                    _anomaly(
                        e,
                        "ANNUAL_QUARTERLY_DIVERGENCE",
                        q.model_copy(update={"value": quantize(gap)}),
                        severity,
                        threshold,
                        f"latest quarter revenue growth {q.value}% vs latest fiscal-year revenue "
                        f"growth {y.value}% (gap {quantize(gap)} pp, |gap| >= {threshold}). The "
                        "quarter is compared with its own prior-year quarter; no YTD is mixed in.",
                        [y],
                    )
                )
    return out


# --- health -------------------------------------------------------------------------------------


def _band(value: Decimal, cuts: Sequence[Decimal]) -> HealthState:
    """cuts = (strong, healthy, neutral, watch) lower bounds, higher is better."""
    states = (HealthState.STRONG, HealthState.HEALTHY, HealthState.NEUTRAL, HealthState.WATCH)
    for cut, state in zip(cuts, states, strict=False):
        if value >= cut:
            return state
    return HealthState.WEAK


TREND_TO_HEALTH = {
    TrendClass.STRONGLY_IMPROVING: HealthState.STRONG,
    TrendClass.IMPROVING: HealthState.HEALTHY,
    TrendClass.STABLE: HealthState.NEUTRAL,
    TrendClass.DETERIORATING: HealthState.WATCH,
    TrendClass.STRONGLY_DETERIORATING: HealthState.WEAK,
    TrendClass.VOLATILE: HealthState.WATCH,
    TrendClass.INSUFFICIENT_DATA: HealthState.INSUFFICIENT_DATA,
}


def build_health(
    e: Engine,
    latest: date | None,
    trends: Sequence[TrendAssessment],
    signals: Sequence[ForensicSignal],
    models: Sequence[ForensicModelResult],
) -> list[FinancialHealthSignal]:
    basis = e.frame.basis
    out: list[FinancialHealthSignal] = []
    p = e.periods.fy(latest) if latest else None

    def add(
        dim: HealthDimension,
        state: HealthState,
        rule: str,
        reasons: Sequence[str],
        ids: Sequence[UUID] = (),
    ) -> None:
        out.append(
            FinancialHealthSignal(
                dimension=dim,
                state=state,
                reporting_basis=basis,
                rule_id=f"{HEALTH_RULES_VERSION}/{rule}",
                reasons=tuple(reasons),
                metric_ids=tuple(ids),
            )
        )

    def level(dim: HealthDimension, code: str, cuts: Sequence[Decimal], rule: str) -> None:
        metric = e.find(code, p) if p else None
        if metric is None or metric.value is None:
            status = metric.status.value if metric else "NOT_DERIVED"
            add(
                dim,
                HealthState.INSUFFICIENT_DATA,
                rule,
                [f"{code} is {status}"],
                [metric.metric_id] if metric else [],
            )
            return
        add(
            dim,
            _band(metric.value, cuts),
            rule,
            [
                f"{code} = {metric.value} {metric.unit.value} ({metric.period.label}); bands "
                f">= {', '.join(str(c) for c in cuts)}"
            ],
            [metric.metric_id],
        )

    def trend(dim: HealthDimension, code: str) -> None:
        t = next((x for x in trends if x.series_code == code), None)
        if t is None:
            add(dim, HealthState.INSUFFICIENT_DATA, f"TREND/{code}", [f"no {code} trend"])
            return
        add(
            dim,
            TREND_TO_HEALTH[t.classification],
            f"TREND/{code}",
            [f"{code} trend {t.classification.value}: {t.explanation}"],
            t.metric_ids,
        )

    def not_applicable(dim: HealthDimension, why: str) -> None:
        add(dim, HealthState.INSUFFICIENT_DATA, "NOT_APPLICABLE", [why])

    level(HealthDimension.GROWTH, "REVENUE_CAGR_3Y", (D(15), D(8), D(0), D(-5)), "REVENUE_CAGR_3Y")
    if e.financial:
        trend(HealthDimension.MARGINS, "PAT_MARGIN")
        level(HealthDimension.RETURNS, "ROA", (D("1.5"), D(1), D("0.5"), D(0)), "ROA")
        why = (
            f"industrial leverage, cash-conversion and working-capital measures do not apply "
            f"to {e.industry.value}; bank capital and asset-quality data are not canonical"
        )
        for dim in (
            HealthDimension.BALANCE_SHEET,
            HealthDimension.CASH_CONVERSION,
            HealthDimension.WORKING_CAPITAL,
        ):
            not_applicable(dim, why)
    else:
        trend(HealthDimension.MARGINS, "EBITDA_MARGIN")
        level(HealthDimension.RETURNS, "ROCE", (D(20), D(15), D(10), D(5)), "ROCE")
        net = e.find("NET_DEBT", e.periods.pit(latest)) if latest else None
        if net is not None and net.interpretation == "NET_CASH":
            add(
                HealthDimension.BALANCE_SHEET,
                HealthState.STRONG,
                "NET_CASH",
                [f"net cash of {-(net.value or 0)} (cash exceeds gross debt)"],
                [net.metric_id],
            )
        else:
            metric = e.find("NET_DEBT_TO_EBITDA", p) if p else None
            if metric is None or metric.value is None:
                add(
                    HealthDimension.BALANCE_SHEET,
                    HealthState.INSUFFICIENT_DATA,
                    "NET_DEBT_TO_EBITDA",
                    [f"NET_DEBT_TO_EBITDA is {metric.status.value if metric else 'NOT_DERIVED'}"],
                )
            else:
                value = metric.value
                state = (
                    HealthState.HEALTHY
                    if value <= 1
                    else HealthState.NEUTRAL
                    if value <= D("2.5")
                    else HealthState.WATCH
                    if value <= 4
                    else HealthState.WEAK
                )
                add(
                    HealthDimension.BALANCE_SHEET,
                    state,
                    "NET_DEBT_TO_EBITDA",
                    [f"net debt / EBITDA = {value}x; <= 1 healthy, <= 2.5 neutral, <= 4 watch"],
                    [metric.metric_id],
                )
        level(
            HealthDimension.CASH_CONVERSION,
            "CUMULATIVE_CFO_TO_PAT_3Y",
            (D(1), D("0.8"), D("0.6"), D("0.4")),
            "CUMULATIVE_CFO_TO_PAT_3Y",
        )
        trend(HealthDimension.WORKING_CAPITAL, "RECEIVABLE_DAYS")
    evaluated = [s for s in signals if s.status != SignalStatus.INSUFFICIENT_DATA]
    elevated = [s for s in evaluated if s.status == SignalStatus.ELEVATED]
    watch = [s for s in evaluated if s.status == SignalStatus.WATCH]
    if len(evaluated) < 3:
        quality = HealthState.INSUFFICIENT_DATA
    elif len(elevated) >= 2:
        quality = HealthState.WEAK
    elif elevated or len(watch) >= 3:
        quality = HealthState.WATCH
    elif watch:
        quality = HealthState.NEUTRAL
    else:
        quality = HealthState.HEALTHY
    add(
        HealthDimension.EARNINGS_QUALITY,
        quality,
        "SIGNAL_COUNTS",
        [
            f"{len(evaluated)} screens evaluated: {len(elevated)} ELEVATED "
            f"({', '.join(s.code for s in elevated) or '-'}), {len(watch)} WATCH "
            f"({', '.join(s.code for s in watch) or '-'})"
        ],
        sorted({i for s in evaluated for i in s.metric_ids}, key=str),
    )
    scored = [m for m in models if m.status == S.CALCULATED]
    zones = {m.zone for m in scored}
    if not scored:
        state = HealthState.INSUFFICIENT_DATA
    elif "DISTRESS_ZONE" in zones and "ABOVE_SCREENING_THRESHOLD" in zones:
        state = HealthState.WEAK
    elif zones & {"DISTRESS_ZONE", "ABOVE_SCREENING_THRESHOLD", "LOW_F_SCORE"}:
        state = HealthState.WATCH
    elif "SAFE_ZONE" in zones and "HIGH_F_SCORE" in zones:
        state = HealthState.STRONG
    elif "SAFE_ZONE" in zones:
        state = HealthState.HEALTHY
    else:
        state = HealthState.NEUTRAL
    add(
        HealthDimension.FORENSIC_SCREEN,
        state,
        "MODEL_ZONES",
        [
            f"{m.model.value}/{m.variant}: {m.status.value}"
            + (
                f" score {m.score} ({m.zone})"
                if m.score is not None
                else f" ({m.components_available}/{m.components_total} components)"
            )
            for m in models
        ],
    )
    return out


# --- assembly -----------------------------------------------------------------------------------


def build_financial_analytics(
    data: CanonicalFinancialDataset,
    *,
    generated_at: datetime,
    limits: AnalyticsLimits | None = None,
) -> FinancialAnalyticsDataset:
    bounds = limits or AnalyticsLimits()
    analytics_key = f"{data.dataset_id}|{ANALYTICS_ENGINE_VERSION}"
    analytics_id = uuid5(NS, f"{analytics_key}|{bounds.model_dump_json()}")
    warnings: list[AnalyticsWarning] = []
    partial = False
    frames_out: list[AnalyticsFrame] = []
    periods: list[AnalyticalPeriod] = []
    metrics: list[DerivedMetric] = []
    series: list[AnalyticalSeries] = []
    trends: list[TrendAssessment] = []
    anomalies: list[AnomalySignal] = []
    signals: list[ForensicSignal] = []
    models: list[ForensicModelResult] = []
    health: list[FinancialHealthSignal] = []
    consumed = 0
    unknown_basis = sum(
        1 for v in data.canonical_values if v.reporting_basis == ReportingBasis.UNKNOWN
    )
    if unknown_basis:
        warnings.append(
            AnalyticsWarning(
                code="UNKNOWN_BASIS_EXCLUDED", detail=f"{unknown_basis} canonical values"
            )
        )
    unresolved = Counter(
        r.outcome.value
        for r in data.reconciliation
        if r.outcome in (Outcome.CONFLICT, Outcome.AMBIGUOUS)
    )
    if unresolved:
        warnings.append(
            AnalyticsWarning(
                code="BOX4_UNRESOLVED_CONFLICTS",
                detail=", ".join(f"{k}: {v}" for k, v in sorted(unresolved.items()))
                + "; such inputs make dependent calculations CONFLICTED_INPUT",
            )
        )
    for basis, currency in frame_currencies(data).items():
        remaining = max(bounds.max_canonical_values - consumed, 1)
        frame = Frame(data, basis, currency, remaining)
        consumed += frame.consumed
        if frame.truncated:
            partial = True
            warnings.append(
                AnalyticsWarning(code="CANONICAL_VALUE_LIMIT_REACHED", detail=basis.value)
            )
        if frame.other_currency:
            warnings.append(
                AnalyticsWarning(
                    code="OTHER_CURRENCY_EXCLUDED",
                    detail=f"{basis.value}: {len(frame.other_currency)} canonical values not in "
                    f"{currency.value} are not mixed into this frame",
                )
            )
        engine = Engine(frame, Periods(data.fiscal_year_end_month), data.industry, analytics_key)
        annual = frame.periods(PeriodType.FY)
        quarters = frame.periods(PeriodType.QUARTER)
        latest = annual[-1] if annual else None
        engine.latest_fy = engine.periods.fy(latest) if latest else None
        for end in sorted(set(frame.periods(PeriodType.POINT_IN_TIME)) | set(annual)):
            base_balance_metrics(engine, engine.periods.pit(end))
        for end in annual:
            base_flow_metrics(engine, engine.periods.fy(end))
        for end in annual:
            annual_metrics(engine, end)
        if latest:
            window_metrics(engine, latest)
        for end in quarters:
            quarterly_metrics(engine, end)
        if frame.has_ttm:
            ttm_metrics(engine)
        frame_signals = forensic_signals(engine, latest)
        frame_models = [piotroski(engine, latest), *altman(engine, latest), beneish(engine, latest)]
        frame_trends = build_trends(engine, annual)
        frames_out.append(
            AnalyticsFrame(
                reporting_basis=basis,
                currency=currency,
                annual_periods=tuple(annual),
                quarter_periods=tuple(quarters),
                has_ttm=frame.has_ttm,
                excluded_other_currency_values=len(frame.other_currency),
            )
        )
        periods += select_periods(engine, annual, quarters)
        metrics += engine.metrics.values()
        series += build_series(engine, annual, quarters, bounds.max_periods_per_series)
        trends += frame_trends
        anomalies += build_anomalies(engine, annual, quarters)
        signals += frame_signals
        models += frame_models
        health += build_health(engine, latest, frame_trends, frame_signals, frame_models)
        material = sum(
            1 for m in engine.metrics.values() if "MATERIAL_INPUT_CONFLICT" in m.warnings
        )
        if material:
            warnings.append(
                AnalyticsWarning(
                    code="MATERIAL_INPUT_CONFLICTS",
                    detail=f"{basis.value}: {material} metrics use a Box 4 value whose losing "
                    "source differs by more than 10 %; their confidence is LOW",
                )
            )
    if not frames_out:
        warnings.append(
            AnalyticsWarning(
                code="NO_ANALYSABLE_VALUES", detail="no canonical MONEY values with a known basis"
            )
        )

    def order(m: DerivedMetric) -> tuple[str, ...]:
        p = m.period
        return (
            m.reporting_basis.value,
            m.category.value,
            m.code,
            str(p.period_end),
            p.period_type.value,
            m.comparison_period.key() if m.comparison_period else "",
        )

    metrics.sort(key=order)
    if len(metrics) > bounds.max_derived_metrics:
        partial = True
        warnings.append(
            AnalyticsWarning(
                code="DERIVED_METRIC_LIMIT_REACHED",
                detail=f"{len(metrics)} metrics, kept {bounds.max_derived_metrics}",
            )
        )
        metrics = metrics[: bounds.max_derived_metrics]
        kept = {m.metric_id for m in metrics}
        metrics = [
            m.model_copy(update={"derived_from": tuple(d for d in m.derived_from if d in kept)})
            for m in metrics
        ]
    anomalies.sort(key=lambda a: (a.reporting_basis.value, a.code, str(a.period.period_end)))
    if len(anomalies) > bounds.max_anomaly_signals:
        partial = True
        warnings.append(AnalyticsWarning(code="ANOMALY_LIMIT_REACHED"))
        anomalies = anomalies[: bounds.max_anomaly_signals]
    signals.sort(key=lambda s: (s.reporting_basis.value, s.code))
    if len(signals) > bounds.max_forensic_signals:
        partial = True
        warnings.append(AnalyticsWarning(code="FORENSIC_SIGNAL_LIMIT_REACHED"))
        signals = signals[: bounds.max_forensic_signals]
    warnings.sort(key=lambda w: (w.code, w.detail or ""))
    if len(warnings) > bounds.max_warnings:
        partial = True
        warnings = [
            *warnings[: bounds.max_warnings - 1],
            AnalyticsWarning(code="WARNING_LIMIT_REACHED"),
        ]
    return FinancialAnalyticsDataset(
        analytics_id=analytics_id,
        source_dataset_id=data.dataset_id,
        company_key=data.company_key,
        company_name=data.company_name,
        industry=data.industry,
        generated_at=generated_at,
        limits=bounds,
        status="PARTIAL" if partial else "COMPLETE",
        frames=tuple(frames_out),
        periods=tuple(periods),
        metrics=tuple(metrics),
        series=tuple(sorted(series, key=lambda x: (x.reporting_basis.value, x.frequency, x.code))),
        trends=tuple(sorted(trends, key=lambda t: (t.reporting_basis.value, t.series_code))),
        anomalies=tuple(anomalies),
        forensic_signals=tuple(signals),
        forensic_models=tuple(
            sorted(models, key=lambda m: (m.reporting_basis.value, m.model.value, m.variant))
        ),
        health=tuple(sorted(health, key=lambda h: (h.reporting_basis.value, h.dimension.value))),
        warnings=tuple(warnings),
        statistics=AnalyticsStatistics(
            canonical_values_consumed=consumed,
            derived_metrics=len(metrics),
            metrics_by_status=dict(sorted(Counter(m.status.value for m in metrics).items())),
            metrics_by_category=dict(sorted(Counter(m.category.value for m in metrics).items())),
            trends=len(trends),
            trends_by_class=dict(sorted(Counter(t.classification.value for t in trends).items())),
            anomalies=len(anomalies),
            forensic_signals_by_status=dict(
                sorted(Counter(s.status.value for s in signals).items())
            ),
            models_by_status=dict(sorted(Counter(m.status.value for m in models).items())),
            health_by_state=dict(sorted(Counter(h.state.value for h in health).items())),
        ),
    )


def canonical_analytics_json(dataset: FinancialAnalyticsDataset) -> str:
    return json.dumps(
        dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


__all__ = [
    "build_financial_analytics",
    "canonical_analytics_json",
    "classify_trend",
    "Fact",
    "ForensicModel",
]
