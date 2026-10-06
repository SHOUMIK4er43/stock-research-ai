"""DETAILED_RESEARCH_V1 Box 5: CanonicalFinancialDataset -> FinancialAnalyticsDataset.

Pipeline per reporting basis (each analysed in its dominant currency only):
  period alignment -> building blocks -> growth / profitability / returns / DuPont ->
  leverage / liquidity -> working capital -> cash-flow quality -> capital allocation ->
  accruals -> windows (CAGR, cumulative) -> quarters / TTM -> earnings-quality checks ->
  forensic models -> series -> trends -> anomalies -> health -> unmapped-line references.

No AI, no network, no clock: `generated_at` is injected, IDs are uuid5, output ordering is
sorted, formulas come from financial_formulas and thresholds from financial_rules.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from stock_research_ai.application.financial_forensics import (
    altman,
    beneish,
    earnings_quality,
    piotroski,
)
from stock_research_ai.application.financial_formulas import (
    FORMULA_REGISTRY_VERSION,
    FORMULAS,
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
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.financial_rules import (
    ANNUAL_QUARTERLY_DIVERGENCE,
    ANOMALY_RULES,
    HEALTH_BANDS,
    NET_DEBT_TO_EBITDA_BANDS,
    REPEATED_NEGATIVE_FCF,
    RULE_REGISTRY_VERSION,
    TREND_CONSISTENCY,
    TREND_MIN_POINTS,
    TREND_RULES,
    TREND_THRESHOLDS,
    TREND_WINDOW,
    GradedRule,
    rule_definitions,
)
from stock_research_ai.domain.financial_analytics import (
    ANALYTICS_ENGINE_VERSION,
    CATEGORY_FIELDS,
    AnalyticalPeriod,
    AnalyticalSeries,
    AnalyticsFrame,
    AnalyticsLimits,
    AnalyticsStatistics,
    AnalyticsUnit,
    AnalyticsWarning,
    AnomalySignal,
    Box4Reference,
    CalculatedMetric,
    CalculationManifest,
    CalculationStatus,
    EarningsQualityCheck,
    FinancialAnalyticsDataset,
    FinancialHealthSignal,
    ForensicModelResult,
    HealthDimension,
    HealthState,
    PeriodRole,
    Sector,
    SeriesPoint,
    Severity,
    TrendAssessment,
    TrendClass,
    UnmappedLineReference,
    UnmappedSummary,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalMetric,
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
M = CanonicalMetric
BALANCE_SERIES = {"GROSS_DEBT"}  # building blocks read at the fiscal year end
CALCULATED_SERIES = (
    "REVENUE_GROWTH_YOY",
    "PAT_GROWTH_YOY",
    "EBITDA_MARGIN",
    "OPERATING_MARGIN",
    "PAT_MARGIN",
    "ROE",
    "ROA",
    "ROCE",
    "ROIC",
    "DEBT_TO_EQUITY",
    "INTEREST_COVERAGE",
    "CFO_TO_PAT",
    "FCF",
    "GROSS_DEBT",
    "RECEIVABLE_DAYS",
    "INVENTORY_DAYS",
    "PAYABLE_DAYS",
    "CASH_CONVERSION_CYCLE",
    "RECEIVABLES_TO_REVENUE",
    "OTHER_INCOME_TO_PBT",
    "CASH_FLOW_ACCRUAL_RATIO",
    "DEPOSITS_GROWTH_YOY",
    "ADVANCES_GROWTH_YOY",
    "FINANCING_PROFIT_MARGIN",
)
CANONICAL_SERIES: dict[str, tuple[CanonicalMetric, ...]] = {
    "REVENUE": (M.REVENUE_FROM_OPERATIONS, M.REVENUE),
    "PAT": (M.PAT,),
    "CFO": (M.CFO,),
    "TOTAL_ASSETS": (M.TOTAL_ASSETS,),
    "TRADE_RECEIVABLES": (M.TRADE_RECEIVABLES,),
    "DEPOSITS": (M.DEPOSITS,),
}
CANONICAL_BALANCE = {"TOTAL_ASSETS", "TRADE_RECEIVABLES", "DEPOSITS"}
QUARTERLY_SERIES = ("REVENUE_GROWTH_YOY", "OPERATING_MARGIN", "PAT_MARGIN")
UNMAPPED_HINTS_RULE = "UNMAPPED_HINTS/2"
#: keyword (in the raw label or its caption) -> calculations an analyst may want to revisit
UNMAPPED_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("capital expenditure", "property, plant", "purchase of fixed", "expenditure on"),
        ("CAPEX", "FCF"),
    ),
    (("lease liabilit",), ("LEASE_ADJUSTED_DEBT",)),
    (("exceptional",), ("EXCEPTIONAL_TO_PBT",)),
    (("inventor",), ("INVENTORY_DAYS",)),
    (("cost of material", "raw material"), ("COGS", "GROSS_MARGIN")),
    (("npa", "non-performing"), ("GROSS_NPA_RATIO", "NET_NPA_RATIO")),
    (("interest paid",), ("CASH_INTEREST_COVERAGE",)),
    (("taxes paid", "tax paid"), ("CASH_TAX_RATE",)),
    (("dividend",), ("DIVIDEND_PAYOUT",)),
    (("buyback", "buy-back"), ("BUYBACK_SPEND",)),
    (("borrowing",), ("GROSS_DEBT",)),
)


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


# --- series and trends -------------------------------------------------------------------------


def _series_period(e: Engine, code: str, end: date) -> FinancialPeriod:
    return e.periods.pit(end) if code in BALANCE_SERIES else e.periods.fy(end)


def build_series(
    e: Engine, annual: Sequence[date], quarters: Sequence[date]
) -> list[AnalyticalSeries]:
    out: list[AnalyticalSeries] = []
    basis = e.frame.basis

    def sid(code: str, frequency: str, source: str) -> UUID:
        return uuid5(NS, f"{e.analytics_key}|{basis.value}|series|{code}|{frequency}|{source}")

    for code in sorted(CALCULATED_SERIES):
        points = []
        for end in annual:
            metric = e.find(code, _series_period(e, code, end))
            if metric is not None:
                points.append(
                    SeriesPoint(
                        period=metric.period,
                        status=metric.status,
                        value=metric.value,
                        metric_id=metric.metric_id,
                    )
                )
        if any(p.value is not None for p in points):
            unit = FORMULAS[code].unit
            out.append(
                AnalyticalSeries(
                    series_id=sid(code, "ANNUAL", "CALCULATED"),
                    code=code,
                    reporting_basis=basis,
                    frequency="ANNUAL",
                    source="CALCULATED",
                    unit=unit,
                    points=tuple(points),
                )
            )
    for code in QUARTERLY_SERIES:
        points = []
        for end in quarters:
            metric = e.find(code, e.periods.quarter(end))
            if metric is not None:
                points.append(
                    SeriesPoint(
                        period=metric.period,
                        status=metric.status,
                        value=metric.value,
                        metric_id=metric.metric_id,
                    )
                )
        if any(p.value is not None for p in points):
            out.append(
                AnalyticalSeries(
                    series_id=sid(code, "QUARTERLY", "CALCULATED"),
                    code=code,
                    reporting_basis=basis,
                    frequency="QUARTERLY",
                    source="CALCULATED",
                    unit=FORMULAS[code].unit,
                    points=tuple(points),
                )
            )
    for code, metrics in CANONICAL_SERIES.items():
        points = []
        for end in annual:
            period = e.periods.pit(end) if code in CANONICAL_BALANCE else e.periods.fy(end)
            got = e.canonical(metrics, period)
            if isinstance(got, Fact):
                points.append(
                    SeriesPoint(
                        period=got.inputs[0].period,
                        status=S.CALCULATED,
                        value=got.value,
                        canonical_value_id=got.inputs[0].canonical_value_id,
                    )
                )
        if points:
            out.append(
                AnalyticalSeries(
                    series_id=sid(code, "ANNUAL", "CANONICAL"),
                    code=code,
                    reporting_basis=basis,
                    frequency="ANNUAL",
                    source="CANONICAL",
                    unit=U.MONEY,
                    points=tuple(points),
                )
            )
    return out


def classify_trend(
    values: Sequence[Decimal], kind: str, higher_is_better: bool
) -> tuple[TrendClass, Decimal | None, Decimal | None]:
    """TREND/<kind>/2: change = last - first (MONEY: % of |first|). VOLATILE when the direction
    reverses at least twice with one step >= strong, or when a change >= strong follows an
    inconsistent path. IMPROVING / DETERIORATING (and their strong forms) need >=
    TREND_CONSISTENCY of the steps to move that way; otherwise the series is STABLE.
    Returns (class, change, direction consistency)."""
    if len(values) < TREND_MIN_POINTS:
        return TrendClass.INSUFFICIENT_HISTORY, None, None
    strong, mild = TREND_THRESHOLDS[kind]
    if kind == "MONEY":
        if values[0] == 0:
            return TrendClass.INSUFFICIENT_HISTORY, None, None
        change = (values[-1] - values[0]) / abs(values[0]) * 100
        steps = [
            (b - a) / abs(a) * 100 if a != 0 else D(0)
            for a, b in zip(values, values[1:], strict=False)
        ]
    else:
        change = values[-1] - values[0]
        steps = [b - a for a, b in zip(values, values[1:], strict=False)]
    signs = [1 if s > 0 else -1 for s in steps if s != 0]
    reversals = sum(1 for a, b in zip(signs, signs[1:], strict=False) if a != b)
    overall = 1 if change > 0 else -1 if change < 0 else 0
    consistency = quantize(D(sum(1 for s in signs if s == overall)) / len(steps)) if steps else None
    if reversals >= 2 and max(abs(s) for s in steps) >= strong:
        return TrendClass.VOLATILE, quantize(change), consistency
    directional = change if higher_is_better else -change
    consistent = consistency is not None and consistency >= TREND_CONSISTENCY
    if directional >= mild and consistent:
        cls = TrendClass.STRONGLY_IMPROVING if directional >= strong else TrendClass.IMPROVING
    elif directional <= -mild and consistent:
        cls = (
            TrendClass.STRONGLY_DETERIORATING
            if directional <= -strong
            else TrendClass.DETERIORATING
        )
    elif abs(directional) >= strong:
        cls = TrendClass.VOLATILE  # a large move along an inconsistent path
    else:
        cls = TrendClass.STABLE
    return cls, quantize(change), consistency


def build_trends(e: Engine, annual: Sequence[date]) -> list[TrendAssessment]:
    out = []
    for rule in TREND_RULES:
        if e.sector not in rule.sectors:
            continue
        window = annual[-TREND_WINDOW:]
        metrics = [e.find(rule.code, _series_period(e, rule.code, end)) for end in window]
        used = [m for m in metrics if m is not None and m.value is not None]
        values = [m.value for m in used if m.value is not None]
        classification, change, consistency = classify_trend(
            values, rule.kind, rule.higher_is_better
        )
        strong, mild = TREND_THRESHOLDS[rule.kind]
        unit = "% of first value" if rule.kind == "MONEY" else (used[0].unit.value if used else "")
        if classification == TrendClass.INSUFFICIENT_HISTORY:
            explanation = (
                f"{rule.code}: {len(used)} calculated point(s) in the last "
                f"{TREND_WINDOW} fiscal years; {TREND_MIN_POINTS} needed"
            )
        else:
            explanation = (
                f"{rule.code}: {values[0]} ({used[0].period.label}) -> {values[-1]} "
                f"({used[-1].period.label}); change {change} {unit}; direction consistency "
                f"{consistency}; {'higher' if rule.higher_is_better else 'lower'} is better; "
                f"strong >= {strong}, mild >= {mild}"
            )
        out.append(
            TrendAssessment(
                trend_id=uuid5(NS, f"{e.analytics_key}|{e.frame.basis.value}|trend|{rule.code}"),
                series_code=rule.code,
                reporting_basis=e.frame.basis,
                classification=classification,
                rule_id=rule.rule_id,
                higher_is_better=rule.higher_is_better,
                points_used=len(used),
                direction_consistency=consistency,
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


# --- anomalies ---------------------------------------------------------------------------------


def _anomaly(
    e: Engine,
    code: str,
    rule_id: str,
    severity: Severity,
    threshold: Decimal,
    observed: Decimal,
    unit: AnalyticsUnit,
    periods: Sequence[FinancialPeriod],
    explanation: str,
    metrics: Sequence[CalculatedMetric],
) -> AnomalySignal:
    return AnomalySignal(
        signal_id=uuid5(
            NS,
            f"{e.analytics_key}|{e.frame.basis.value}|anomaly|{code}|"
            f"{'|'.join(p.key() for p in periods)}",
        ),
        rule_id=rule_id,
        code=code,
        severity=severity,
        reporting_basis=e.frame.basis,
        periods=tuple(periods),
        observed=observed,
        threshold=threshold,
        unit=unit,
        explanation=explanation,
        metric_ids=tuple(m.metric_id for m in metrics),
        canonical_value_ids=tuple(
            sorted({i.canonical_value_id for m in metrics for i in m.provenance}, key=str)
        ),
    )


def _graded_anomalies(
    e: Engine, rule: GradedRule, periods: Sequence[FinancialPeriod]
) -> list[AnomalySignal]:
    out = []
    for period in periods:
        metric = e.find(rule.metric, period)
        if metric is None or metric.value is None:
            continue
        used = [metric]
        if rule.condition is not None:
            gate = e.find(rule.condition.metric, period)
            if gate is None or gate.value is None or not rule.condition.holds(gate.value):
                continue
            used.append(gate)
        severity = rule.grade(metric.value)
        if severity == Severity.INFO:
            continue
        threshold = rule.threshold(severity)
        explanation = rule.definition().template.format(
            metric=rule.metric,
            observed=metric.value,
            unit=metric.unit.value,
            period=period.label,
            severity=severity.value,
            threshold=threshold,
        )
        if rule.condition is not None:
            explanation += f" Condition: {rule.condition.meaning} ({used[1].value})."
        out.append(
            _anomaly(
                e,
                rule.code,
                rule.rule_id,
                severity,
                threshold,
                metric.value,
                metric.unit,
                [period],
                explanation,
                used,
            )
        )
    return out


def build_anomalies(
    e: Engine, annual: Sequence[date], quarters: Sequence[date]
) -> list[AnomalySignal]:
    out: list[AnomalySignal] = []
    for rule in ANOMALY_RULES:
        if e.sector not in rule.sectors:
            continue
        periods = (
            [e.periods.fy(end) for end in annual]
            if rule.frequency == "ANNUAL"
            else [e.periods.quarter(end) for end in quarters]
        )
        out += _graded_anomalies(e, rule, periods)
    if annual and quarters:
        qm = e.find("REVENUE_GROWTH_YOY", e.periods.quarter(quarters[-1]))
        ym = e.find("REVENUE_GROWTH_YOY", e.periods.fy(annual[-1]))
        if qm is not None and ym is not None and qm.value is not None and ym.value is not None:
            gap = quantize(qm.value - ym.value)
            low, medium, high = ANNUAL_QUARTERLY_DIVERGENCE
            severity = (
                Severity.HIGH
                if abs(gap) >= high
                else Severity.MEDIUM
                if abs(gap) >= medium
                else Severity.LOW
                if abs(gap) >= low
                else None
            )
            if severity is not None:
                threshold = {Severity.HIGH: high, Severity.MEDIUM: medium}.get(severity, low)
                out.append(
                    _anomaly(
                        e,
                        "ANNUAL_QUARTERLY_DIVERGENCE",
                        "ANOMALY/ANNUAL_QUARTERLY_DIVERGENCE/2",
                        severity,
                        threshold,
                        gap,
                        U.PERCENTAGE_POINTS,
                        [qm.period, ym.period],
                        f"latest-quarter revenue growth {qm.value}% (vs its own prior-year "
                        "quarter) "
                        f"against latest fiscal-year revenue growth {ym.value}%: gap {gap} pp, "
                        f"|gap| >= {threshold} ({severity.value}). No YTD value is mixed in.",
                        [qm, ym],
                    )
                )
    if annual and not e.financial:
        years = [e.periods.fy(shift(annual[-1], -12 * k)) for k in range(3)]
        fcf = [e.find("FCF", y) for y in years]
        values = [m for m in fcf if m is not None and m.value is not None]
        if len(values) == 3:
            negative = sum(1 for m in values if m.value is not None and m.value < 0)
            fcf_medium, fcf_high = REPEATED_NEGATIVE_FCF[1], REPEATED_NEGATIVE_FCF[2]
            severity = (
                Severity.HIGH
                if negative >= fcf_high
                else Severity.MEDIUM
                if negative >= fcf_medium
                else None
            )
            if severity is not None:
                out.append(
                    _anomaly(
                        e,
                        "REPEATED_NEGATIVE_FCF",
                        "EARNINGS_QUALITY/REPEATED_NEGATIVE_FCF/2",
                        severity,
                        D(fcf_high if severity == Severity.HIGH else fcf_medium),
                        D(negative),
                        U.COUNT,
                        years,
                        f"free cash flow was negative in {negative} of the last 3 "
                        "fiscal years. A rule-based signal that may require review.",
                        values,
                    )
                )
    return out


# --- health ------------------------------------------------------------------------------------

TREND_TO_HEALTH = {
    TrendClass.STRONGLY_IMPROVING: HealthState.STRONG,
    TrendClass.IMPROVING: HealthState.HEALTHY,
    TrendClass.STABLE: HealthState.NEUTRAL,
    TrendClass.DETERIORATING: HealthState.WATCH,
    TrendClass.STRONGLY_DETERIORATING: HealthState.WEAK,
    TrendClass.VOLATILE: HealthState.WATCH,
    TrendClass.INSUFFICIENT_HISTORY: HealthState.INSUFFICIENT_DATA,
}


def _band(value: Decimal, cuts: Sequence[Decimal]) -> HealthState:
    states = (HealthState.STRONG, HealthState.HEALTHY, HealthState.NEUTRAL, HealthState.WATCH)
    for cut, state in zip(cuts, states, strict=False):
        if value >= cut:
            return state
    return HealthState.WEAK


def build_health(
    e: Engine,
    latest: date | None,
    trends: Sequence[TrendAssessment],
    checks: Sequence[EarningsQualityCheck],
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
                rule_id=f"HEALTH/{rule}/2",
                reasons=tuple(reasons),
                metric_ids=tuple(ids),
            )
        )

    def level(dim: HealthDimension, band: str) -> None:
        code, cuts = HEALTH_BANDS[band]
        metric = e.find(code, p) if p else None
        if metric is None or metric.value is None:
            add(
                dim,
                HealthState.INSUFFICIENT_DATA,
                band,
                [f"{code} is {metric.status.value if metric else 'NOT_DERIVED'}"],
                [metric.metric_id] if metric else [],
            )
            return
        add(
            dim,
            _band(metric.value, cuts),
            band,
            [
                f"{code} = {metric.value} {metric.unit.value} ({metric.period.label}); bands >= "
                f"{', '.join(str(c) for c in cuts)}"
            ],
            [metric.metric_id],
        )

    def trend(dim: HealthDimension, code: str) -> None:
        t = next((x for x in trends if x.series_code == code), None)
        if t is None:
            add(dim, HealthState.INSUFFICIENT_DATA, f"TREND_{code}", [f"no {code} trend"])
            return
        add(
            dim,
            TREND_TO_HEALTH[t.classification],
            f"TREND_{code}",
            [f"{code} trend {t.classification.value}: {t.explanation}"],
            t.metric_ids,
        )

    level(HealthDimension.GROWTH, "GROWTH")
    if e.financial:
        trend(HealthDimension.MARGINS, "PAT_MARGIN")
        level(HealthDimension.RETURNS, "RETURNS_FINANCIAL")
        why = (
            f"industrial leverage, cash-conversion and working-capital measures do not apply "
            f"to {e.industry.value}; bank capital and asset-quality data are not canonical"
        )
        for dim in (
            HealthDimension.BALANCE_SHEET,
            HealthDimension.CASH_CONVERSION,
            HealthDimension.WORKING_CAPITAL,
        ):
            add(dim, HealthState.NOT_APPLICABLE, "NOT_APPLICABLE", [why])
    else:
        trend(HealthDimension.MARGINS, "EBITDA_MARGIN")
        level(HealthDimension.RETURNS, "RETURNS_GENERAL")
        net = e.find("NET_DEBT", e.periods.pit(latest)) if latest else None
        if net is not None and net.interpretation == "NET_CASH" and net.value is not None:
            add(
                HealthDimension.BALANCE_SHEET,
                HealthState.STRONG,
                "BALANCE_SHEET",
                [f"net cash of {-net.value} (cash exceeds gross debt)"],
                [net.metric_id],
            )
        else:
            metric = e.find("NET_DEBT_TO_EBITDA", p) if p else None
            if metric is None or metric.value is None:
                add(
                    HealthDimension.BALANCE_SHEET,
                    HealthState.INSUFFICIENT_DATA,
                    "BALANCE_SHEET",
                    [f"NET_DEBT_TO_EBITDA is {metric.status.value if metric else 'NOT_DERIVED'}"],
                )
            else:
                healthy, neutral, watch = NET_DEBT_TO_EBITDA_BANDS
                v = metric.value
                state = (
                    HealthState.HEALTHY
                    if v <= healthy
                    else HealthState.NEUTRAL
                    if v <= neutral
                    else HealthState.WATCH
                    if v <= watch
                    else HealthState.WEAK
                )
                add(
                    HealthDimension.BALANCE_SHEET,
                    state,
                    "BALANCE_SHEET",
                    [f"net debt / EBITDA = {v}x"],
                    [metric.metric_id],
                )
        level(HealthDimension.CASH_CONVERSION, "CASH_CONVERSION")
        trend(HealthDimension.WORKING_CAPITAL, "RECEIVABLE_DAYS")
    evaluated = [c for c in checks if c.severity is not None]
    high = [c for c in evaluated if c.severity == Severity.HIGH]
    medium = [c for c in evaluated if c.severity == Severity.MEDIUM]
    if len(evaluated) < 3:
        quality = HealthState.INSUFFICIENT_DATA
    elif len(high) >= 2:
        quality = HealthState.WEAK
    elif high or len(medium) >= 3:
        quality = HealthState.WATCH
    elif medium:
        quality = HealthState.NEUTRAL
    else:
        quality = HealthState.HEALTHY
    add(
        HealthDimension.EARNINGS_QUALITY,
        quality,
        "SIGNAL_COUNTS",
        [
            f"{len(evaluated)} checks evaluated: {len(high)} HIGH "
            f"({', '.join(c.code for c in high) or '-'}), {len(medium)} MEDIUM "
            f"({', '.join(c.code for c in medium) or '-'})"
        ],
        sorted({i for c in evaluated for i in c.metric_ids}, key=str),
    )
    scored = [m for m in models if m.status == S.CALCULATED]
    zones = {m.zone for m in scored}
    if e.financial:
        state = HealthState.NOT_APPLICABLE
    elif not scored:
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
            f"{m.model.value}/{m.variant}: {m.status.value}, {m.availability_label}"
            + (f", score {m.score} ({m.zone})" if m.score is not None else "")
            for m in models
        ],
    )
    return out


# --- unmapped lines ----------------------------------------------------------------------------


def _hint_text(label: str, context: str | None) -> str:
    """The line's own label; its innermost caption only when the label is a fragment
    (e.g. '(Refer to Note' left behind when pypdf wrapped a long label)."""
    lowered = label.lower()
    fragment = (
        re.match(r"^\(?(refer|unlabelled|note)\b", lowered)
        or len(re.findall(r"[a-z]{3,}", lowered)) < 2
    )
    if fragment and context:
        return f"{lowered} {context.split(' > ')[-1].lower()}"
    return lowered


def unmapped_summary(
    data: CanonicalFinancialDataset,
    engines: dict[ReportingBasis, Engine],
    latest: dict[ReportingBasis, date | None],
    limit: int,
) -> UnmappedSummary:
    periods_by_obs = {o.observation_id: o.period.label for o in data.observations}
    references = []
    for line in sorted(data.unmapped_lines, key=lambda u: str(u.line_id)):
        text = _hint_text(line.raw_label, line.label_context)
        hints = sorted(
            {
                code
                for words, codes in UNMAPPED_HINTS
                if any(w in text for w in words)
                for code in codes
            }
        )
        unavailable = []
        engine = engines.get(line.reporting_basis)
        end = latest.get(line.reporting_basis)
        for code in hints:
            period = (
                (
                    engine.periods.pit(end)
                    if code in ("LEASE_ADJUSTED_DEBT", "GROSS_DEBT")
                    else engine.periods.fy(end)
                )
                if engine and end
                else None
            )
            metric = engine.find(code, period) if engine and period else None
            if metric is None or metric.status != S.CALCULATED:
                unavailable.append(code)
        references.append(
            UnmappedLineReference(
                line_id=line.line_id,
                statement_type=line.statement_type,
                reporting_basis=line.reporting_basis,
                source_type=line.source_type,
                raw_label=line.raw_label,
                label_context=line.label_context,
                periods=tuple(
                    sorted({periods_by_obs[i] for i in line.observation_ids if i in periods_by_obs})
                ),
                observation_count=len(line.observation_ids),
                review_hints=tuple(hints),
                hinted_calculations_unavailable=tuple(unavailable),
            )
        )
    flagged = [r for r in references if r.hinted_calculations_unavailable]
    ordered = flagged + [r for r in references if not r.hinted_calculations_unavailable]
    return UnmappedSummary(
        rule_id=UNMAPPED_HINTS_RULE,
        total_lines=len(references),
        by_statement=dict(sorted(Counter(r.statement_type.value for r in references).items())),
        lines_with_unavailable_hints=len(flagged),
        references=tuple(ordered[:limit]),
        truncated=len(ordered) > limit,
    )


# --- assembly ----------------------------------------------------------------------------------


def build_financial_analytics(
    data: CanonicalFinancialDataset,
    *,
    generated_at: datetime,
    limits: AnalyticsLimits | None = None,
) -> FinancialAnalyticsDataset:
    bounds = limits or AnalyticsLimits()
    source_hash = hashlib.sha256(canonical_dataset_json(data).encode()).hexdigest()
    analytics_key = f"{data.dataset_id}|{source_hash}|{ANALYTICS_ENGINE_VERSION}"
    analytics_id = uuid5(NS, f"{analytics_key}|{bounds.model_dump_json()}")
    warnings: list[AnalyticsWarning] = []
    partial = False

    def warn(code: str, detail: str | None = None) -> None:
        warnings.append(
            AnalyticsWarning(
                code=code, detail=detail[: bounds.max_warning_chars] if detail else None
            )
        )

    frames_out: list[AnalyticsFrame] = []
    periods: list[AnalyticalPeriod] = []
    metrics: list[CalculatedMetric] = []
    series: list[AnalyticalSeries] = []
    trends: list[TrendAssessment] = []
    anomalies: list[AnomalySignal] = []
    checks: list[EarningsQualityCheck] = []
    models: list[ForensicModelResult] = []
    health: list[FinancialHealthSignal] = []
    engines: dict[ReportingBasis, Engine] = {}
    latest_by_basis: dict[ReportingBasis, date | None] = {}
    consumed = 0
    annual_count = quarter_count = 0
    unknown_basis = sum(
        1 for v in data.canonical_values if v.reporting_basis == ReportingBasis.UNKNOWN
    )
    if unknown_basis:
        warn("UNKNOWN_BASIS_EXCLUDED", f"{unknown_basis} canonical values")
    unresolved = Counter(
        r.outcome.value
        for r in data.reconciliation
        if r.outcome in (Outcome.CONFLICT, Outcome.AMBIGUOUS)
    )
    if unresolved:
        warn(
            "BOX4_UNRESOLVED_CONFLICTS",
            ", ".join(f"{k}: {v}" for k, v in sorted(unresolved.items()))
            + "; dependent calculations report INPUT_CONFLICT",
        )
    sector = Sector.GENERAL
    for basis, currency in frame_currencies(data).items():
        remaining = max(bounds.max_canonical_values - consumed, 1)
        frame = Frame(data, basis, currency, remaining)
        consumed += frame.consumed
        if frame.truncated:
            partial = True
            warn("CANONICAL_VALUE_LIMIT_REACHED", basis.value)
        if frame.other_currency:
            warn(
                "OTHER_CURRENCY_EXCLUDED",
                f"{basis.value}: {len(frame.other_currency)} canonical "
                f"values not in {currency.value} are not mixed into this frame",
            )
        engine = Engine(
            frame,
            Periods(data.fiscal_year_end_month),
            data.industry,
            analytics_key,
            bounds.max_provenance_refs,
        )
        sector = engine.sector
        annual_all, quarters_all = frame.periods(PeriodType.FY), frame.periods(PeriodType.QUARTER)
        annual, quarters = annual_all[-bounds.max_periods :], quarters_all[-bounds.max_periods :]
        if len(annual) < len(annual_all) or len(quarters) < len(quarters_all):
            partial = True
            warn("PERIOD_LIMIT_REACHED", f"{basis.value}: kept the latest {bounds.max_periods}")
        annual_count += len(annual)
        quarter_count += len(quarters)
        latest = annual[-1] if annual else None
        latest_by_basis[basis] = latest
        engine.latest_fy = engine.periods.fy(latest) if latest else None
        pit_dates = [
            d
            for d in frame.periods(PeriodType.POINT_IN_TIME)
            if not annual or d >= shift(annual[0], -12)
        ]
        for end in sorted(set(pit_dates) | set(annual)):
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
        frame_checks = earnings_quality(engine, latest)
        frame_models = [piotroski(engine, latest), *altman(engine, latest), beneish(engine, latest)]
        frame_trends = build_trends(engine, annual)
        engines[basis] = engine
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
        series += build_series(engine, annual, quarters)
        trends += frame_trends
        anomalies += build_anomalies(engine, annual, quarters)
        checks += frame_checks
        models += frame_models
        health += build_health(engine, latest, frame_trends, frame_checks, frame_models)
        material = sum(
            1 for m in engine.metrics.values() if "MATERIAL_INPUT_CONFLICT" in m.warnings
        )
        if material:
            warn(
                "MATERIAL_INPUT_CONFLICTS",
                f"{basis.value}: {material} metrics use a Box 4 value "
                "whose losing source differs by more than 10 %; their confidence is LOW",
            )
        if engine.provenance_overflows:
            warn(
                "PROVENANCE_LIMIT_REACHED",
                f"{basis.value}: {engine.provenance_overflows} "
                "metrics exceeded the provenance bound and were not calculated",
            )
    if not frames_out:
        warn("NO_ANALYSABLE_VALUES", "no canonical MONEY values with a known basis")

    def order(m: CalculatedMetric) -> tuple[str, ...]:
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
    if len(metrics) > bounds.max_calculated_metrics:
        partial = True
        warn(
            "CALCULATED_METRIC_LIMIT_REACHED",
            f"{len(metrics)} metrics, kept {bounds.max_calculated_metrics}",
        )
        metrics = metrics[: bounds.max_calculated_metrics]
        kept = {m.metric_id for m in metrics}
        metrics = [
            m.model_copy(
                update={"input_metric_ids": tuple(i for i in m.input_metric_ids if i in kept)}
            )
            for m in metrics
        ]
    anomalies.sort(
        key=lambda a: (a.reporting_basis.value, a.code, "|".join(p.key() for p in a.periods))
    )
    if len(anomalies) > bounds.max_anomalies:
        partial = True
        warn("ANOMALY_LIMIT_REACHED")
        anomalies = anomalies[: bounds.max_anomalies]
    checks.sort(key=lambda c: (c.reporting_basis.value, c.code))
    if len(checks) > bounds.max_checks:
        partial = True
        warn("CHECK_LIMIT_REACHED")
        checks = checks[: bounds.max_checks]
    series.sort(key=lambda x: (x.reporting_basis.value, x.source, x.frequency, x.code))
    if len(series) > bounds.max_series:
        partial = True
        warn("SERIES_LIMIT_REACHED")
        series = series[: bounds.max_series]
    unmapped = unmapped_summary(data, engines, latest_by_basis, bounds.max_unmapped_references)
    if unmapped.truncated:
        warn("UNMAPPED_REFERENCE_LIMIT_REACHED", f"{unmapped.total_lines} lines")
    if unmapped.lines_with_unavailable_hints:
        warn(
            "UNMAPPED_LINES_MAY_AFFECT_ANALYTICS",
            f"{unmapped.lines_with_unavailable_hints} unmapped Box 4 lines carry labels related "
            "to calculations that are unavailable; they are listed for analyst review",
        )
    warnings.sort(key=lambda w: (w.code, w.detail or ""))
    if len(warnings) > bounds.max_warnings:
        partial = True
        warnings = [
            *warnings[: bounds.max_warnings - 1],
            AnalyticsWarning(code="WARNING_LIMIT_REACHED"),
        ]
    by_category: dict[str, list[CalculatedMetric]] = {f: [] for f in CATEGORY_FIELDS.values()}
    category_fields: dict[str, Any] = {}
    for m in metrics:
        by_category[CATEGORY_FIELDS[m.category]].append(m)
    category_fields.update({f: tuple(items) for f, items in by_category.items()})
    used_codes = {m.code for m in metrics}
    model_states = Counter(
        "NOT_APPLICABLE"
        if m.status == S.UNSUPPORTED_SECTOR
        else "COMPLETE"
        if m.status == S.CALCULATED
        else "INCOMPLETE"
        for m in models
    )
    return FinancialAnalyticsDataset(
        analytics_id=analytics_id,
        company_key=data.company_key,
        company_name=data.company_name,
        industry=data.industry,
        sector=sector,
        reporting_bases=tuple(f.reporting_basis for f in frames_out),
        generated_at=generated_at,
        source_box4=Box4Reference(
            schema_version=data.schema_version,
            taxonomy_version=data.taxonomy_version,
            dataset_id=data.dataset_id,
            canonical_sha256=source_hash,
            status=data.status,
            canonical_values=len(data.canonical_values),
            unmapped_lines=len(data.unmapped_lines),
        ),
        limits=bounds,
        status="PARTIAL" if partial else "COMPLETE",
        frames=tuple(frames_out),
        periods=tuple(periods),
        **category_fields,
        earnings_quality=tuple(checks),
        forensic_scores=tuple(
            sorted(models, key=lambda m: (m.reporting_basis.value, m.model.value, m.variant))
        ),
        anomalies=tuple(anomalies),
        trends=tuple(sorted(trends, key=lambda t: (t.reporting_basis.value, t.series_code))),
        series=tuple(series),
        health=tuple(sorted(health, key=lambda h: (h.reporting_basis.value, h.dimension.value))),
        unmapped=unmapped,
        warnings=tuple(warnings),
        statistics=AnalyticsStatistics(
            canonical_inputs_considered=consumed,
            canonical_inputs_used=len(
                {i.canonical_value_id for m in metrics for i in m.provenance}
            ),
            calculated_metrics=sum(1 for m in metrics if m.status == S.CALCULATED),
            unavailable_calculations=sum(1 for m in metrics if m.status != S.CALCULATED),
            metrics_by_category=dict(sorted(Counter(m.category.value for m in metrics).items())),
            metrics_by_status=dict(sorted(Counter(m.status.value for m in metrics).items())),
            confidence_counts=dict(
                sorted(Counter(m.confidence.value for m in metrics if m.confidence).items())
            ),
            anomalies_by_severity=dict(
                sorted(Counter(a.severity.value for a in anomalies).items())
            ),
            checks_by_severity=dict(
                sorted(
                    Counter(
                        c.severity.value if c.severity else c.status.value for c in checks
                    ).items()
                )
            ),
            trends_by_class=dict(sorted(Counter(t.classification.value for t in trends).items())),
            forensic_models=dict(sorted(model_states.items())),
            unmapped_lines_referenced=len(unmapped.references),
            annual_periods_analysed=annual_count,
            quarter_periods_analysed=quarter_count,
            series=len(series),
        ),
        manifest=CalculationManifest(
            engine_version=ANALYTICS_ENGINE_VERSION,
            formula_registry_version=FORMULA_REGISTRY_VERSION,
            rule_registry_version=RULE_REGISTRY_VERSION,
            formulas=tuple(FORMULAS[c] for c in sorted(used_codes)),
            rules=rule_definitions(),
        ),
    )


def canonical_analytics_json(dataset: FinancialAnalyticsDataset) -> str:
    return json.dumps(
        dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
