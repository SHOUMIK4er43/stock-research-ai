"""DETAILED_RESEARCH_V1 Box 13: financial forensics, earnings quality and working capital.

Consumes Box 4 canonical values and Box 5 calculated metrics on ONE basis per calculation (the
analytical basis: consolidated). Box 5 numbers are read, never recomputed; Box 13 adds only the
risk metrics Box 5 does not have (3-year receivables CAGR gap, debtor days against their own
3-year average, cumulative CFO / EBITDA, exceptional-item recurrence, adjusted PAT, Altman Z with
a market value ...), each from a registered formula. Forensic models are Box 5's: a partial
model is never a score.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Literal
from uuid import UUID, uuid5

from stock_research_ai.application.diligence_inputs import conflicted
from stock_research_ai.application.financial_formulas import Refused, cagr
from stock_research_ai.application.financial_metrics import (
    REVENUE,
    Fact,
    Got,
    shift,
)
from stock_research_ai.application.red_flags import (
    FORMULAS,
    NS,
    RULES,
    Ctx,
    Ev,
    T,
    Trace,
    basis_of,
    grade,
    missing_reason,
    periodic,
    q,
    unavailable,
    worse,
)
from stock_research_ai.domain.due_diligence import (
    Box5CheckRef as CheckRef,
)
from stock_research_ai.domain.due_diligence import (
    EarningsQuality,
    FinancialForensics,
    ForensicMetric,
    ForensicModelView,
    Layer,
    MetricKind,
    ModelComponent,
    SignalBasis,
    SignalStatus,
    WorkingCapitalForensics,
    WorkingCapitalSeries,
)
from stock_research_ai.domain.due_diligence import (
    Severity as SV,
)
from stock_research_ai.domain.financial_analytics import (
    CalculationStatus,
    ComponentStatus,
    ForensicModel,
    ForensicModelResult,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalMetric,
    FinancialPeriod,
    ReportingBasis,
)

M = CanonicalMetric
S = SignalStatus
D = Decimal
HUNDRED = D(100)


def fy(ctx: Ctx, end: date) -> FinancialPeriod:
    return ctx.periods.fy(end)


def pit(ctx: Ctx, end: date) -> FinancialPeriod:
    return ctx.periods.pit(end)


def years_back(ctx: Ctx, end: date, n: int) -> list[FinancialPeriod]:
    return [fy(ctx, shift(end, -12 * k)) for k in range(n)]


# --- calculations shared by rules and the forensic table ---------------------------------------


@dataclass
class Calc:
    status: str  # AVAILABLE / UNAVAILABLE / NOT_APPLICABLE
    value: Decimal | None
    reason: str | None
    period: FinancialPeriod | None
    trace: Trace = field(default_factory=Trace)
    issues: list[str] = field(default_factory=list)
    calculation: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "AVAILABLE" and self.value is not None


def from_got(got: Got) -> tuple[str, str | None]:
    if isinstance(got, Fact):
        return "AVAILABLE", None
    status, reason = missing_reason(got)
    return ("NOT_APPLICABLE" if status == S.NOT_APPLICABLE else "UNAVAILABLE"), (
        f"{reason}: {got.what}"
    )


def box5_metric(ctx: Ctx, code: str, period: FinancialPeriod, unit: str,
                basis: ReportingBasis | None = None) -> Calc:
    got = ctx.m5(code, period, basis)
    trace = Trace()
    ctx.add(trace, code, got, unit, Layer.BOX5, basis)
    status, reason = from_got(got)
    return Calc(status, got.value if isinstance(got, Fact) else None, reason, period, trace,
                calculation=f"{code} = {got.value if isinstance(got, Fact) else 'n/a'}")


def canonical_value(ctx: Ctx, metrics: Sequence[CanonicalMetric], period: FinancialPeriod,
                    unit: str = "MONEY", basis: ReportingBasis | None = None) -> Calc:
    got = ctx.canonical(metrics, period, basis)
    trace = Trace()
    ctx.add(trace, metrics[0].value, got, unit, Layer.BOX4, basis)
    status, reason = from_got(got)
    return Calc(status, got.value if isinstance(got, Fact) else None, reason, period, trace)


def combine(*calcs: Calc) -> Trace:
    trace = Trace()
    for c in calcs:
        trace.merge(c.trace)
    return trace


def first_gap(*calcs: Calc) -> Calc | None:
    return next((c for c in calcs if not c.ok), None)


def gap_calc(gap: Calc, period: FinancialPeriod | None, trace: Trace) -> Calc:
    return Calc(gap.status, None, gap.reason, period, trace)


def receivables_cagr(ctx: Ctx, end: date) -> Calc:
    now = canonical_value(ctx, [M.TRADE_RECEIVABLES], pit(ctx, end))
    then = canonical_value(ctx, [M.TRADE_RECEIVABLES], pit(ctx, shift(end, -36)))
    trace = combine(now, then)
    gap = first_gap(now, then)
    if gap or now.value is None or then.value is None:
        return gap_calc(gap or now, fy(ctx, end), trace)
    result = cagr(now.value, then.value, 3)
    if isinstance(result, Refused):
        return Calc("NOT_APPLICABLE", None, f"{result.interpretation or result.status.value}",
                    fy(ctx, end), trace)
    return Calc("AVAILABLE", q(result), None, fy(ctx, end), trace,
                calculation=f"(({now.value} / {then.value}) ^ (1/3) - 1) x 100")


def receivables_gap(ctx: Ctx, end: date) -> Calc:
    rec = receivables_cagr(ctx, end)
    rev = box5_metric(ctx, "REVENUE_CAGR_3Y", fy(ctx, end), "PERCENT")
    trace = combine(rec, rev)
    gap = first_gap(rec, rev)
    if gap or rec.value is None or rev.value is None:
        return gap_calc(gap or rec, fy(ctx, end), trace)
    return Calc("AVAILABLE", q(rec.value - rev.value), None, fy(ctx, end), trace,
                calculation=f"{rec.value} - {rev.value}")


def days_vs_average(ctx: Ctx, code: str, end: date, relative: bool) -> Calc:
    points = [box5_metric(ctx, code, p, "DAYS") for p in years_back(ctx, end, 4)]
    trace = combine(*points)
    gap = first_gap(*points)
    latest, prior = points[0], points[1:]
    if gap:
        return gap_calc(gap, fy(ctx, end), trace)
    values = [p.value for p in prior if p.value is not None]
    avg = sum(values, D(0)) / 3
    assert latest.value is not None
    if relative:
        if avg <= 0:
            return Calc("NOT_APPLICABLE", None, "NON_POSITIVE_3Y_AVERAGE", fy(ctx, end), trace)
        value = (latest.value / avg - 1) * HUNDRED
        text = f"({latest.value} / mean({', '.join(map(str, values))}) - 1) x 100"
    else:
        value = latest.value - avg
        text = f"{latest.value} - mean({', '.join(map(str, values))})"
    return Calc("AVAILABLE", q(value), None, fy(ctx, end), trace, calculation=text)


def year_counts(
    ctx: Ctx, end: date, test: Callable[[list[Got]], bool | None],
    metrics: Sequence[Sequence[CanonicalMetric]], n: int = 5,
) -> tuple[int, int, int, Trace]:
    """(count of years where `test` holds, known years, missing years, trace)."""
    trace, count, known, missing = Trace(), 0, 0, 0
    for p in years_back(ctx, end, n):
        gots = [ctx.canonical(ms, p) for ms in metrics]
        for ms, got in zip(metrics, gots, strict=True):
            ctx.add(trace, f"{ms[0].value} {p.label}", got, "MONEY", Layer.BOX4)
        verdict = test(gots)
        if verdict is None:
            missing += 1
            continue
        known += 1
        count += int(verdict)
    return count, known, missing, trace


def _both(gots: list[Got]) -> tuple[Decimal, Decimal] | None:
    a, b = gots
    if isinstance(a, Fact) and isinstance(b, Fact):
        return a.value, b.value
    return None


def profit_without_cash_years(ctx: Ctx, end: date) -> tuple[int, int, int, Trace]:
    def test(gots: list[Got]) -> bool | None:
        pair = _both(gots)
        return None if pair is None else pair[0] > 0 and pair[1] < 0

    return year_counts(ctx, end, test, [[M.PAT], [M.CFO]])


def exceptional_years(ctx: Ctx, end: date) -> tuple[int, int, int, Trace]:
    def test(gots: list[Got]) -> bool | None:
        (got,) = gots
        return None if not isinstance(got, Fact) else got.value != 0

    return year_counts(ctx, end, test, [[M.EXCEPTIONAL_ITEMS]])


def count_ev(code: str, ctx: Ctx, end: date, counted: tuple[int, int, int, Trace],
             what: str) -> Ev:
    """Grade a count over a 5-year window; incomplete windows are a lower bound only."""
    count, known, missing, trace = counted
    b = basis_of(ctx.analytical)
    p = fy(ctx, end)
    if known == 0:
        return unavailable(b, "NO_YEAR_AVAILABLE", period=p, trace=trace)
    severity = grade(D(count), RULES[code].thresholds)
    ev = Ev(S.EVALUATED, b, severity, D(count), period_label=p.label, period_end=p.period_end,
            trace=trace, calculation=f"{count} of {known} available fiscal years {what}",
            explanation=f"{count} of the last {known + missing} fiscal years {what} "
            f"({missing} not available)")
    if missing:
        if severity == SV.GREEN:
            return unavailable(b, "INCOMPLETE_WINDOW", period=p, trace=trace, value=D(count),
                               explanation=f"{missing} of 5 fiscal years not available: a "
                               "clean reading needs all five")
        ev.issues.append("LOWER_BOUND_INCOMPLETE_WINDOW")
    return ev


def ratio_calc(ctx: Ctx, code: str, numerator: Calc, denominator: Calc, period: FinancialPeriod,
               scale: Decimal = HUNDRED, positive: bool = True,
               absolute_numerator: bool = False) -> Calc:
    trace = combine(numerator, denominator)
    gap = first_gap(numerator, denominator)
    if gap or numerator.value is None or denominator.value is None:
        return gap_calc(gap or numerator, period, trace)
    if denominator.value == 0 or (positive and denominator.value < 0):
        return Calc("NOT_APPLICABLE", None, "NON_POSITIVE_DENOMINATOR", period, trace)
    num = abs(numerator.value) if absolute_numerator else numerator.value
    return Calc("AVAILABLE", q(num * scale / denominator.value), None, period, trace,
                calculation=f"{num} / {denominator.value} x {scale}")


# --- rules 1-10, 17-21 (non-financial) ----------------------------------------------------------


def _single(ctx: Ctx, code: str, calc: Calc, *, extra: str = "") -> Ev:
    b = basis_of(ctx.analytical)
    if not calc.ok or calc.value is None:
        status = S.NOT_APPLICABLE if calc.status == "NOT_APPLICABLE" else S.UNAVAILABLE
        return unavailable(b, (calc.reason or "UNAVAILABLE").split(":")[0], status=status,
                           period=calc.period, trace=calc.trace,
                           explanation=calc.reason or "unavailable")
    severity = grade(calc.value, RULES[code].thresholds)
    return Ev(S.EVALUATED, b, severity, calc.value,
              period_label=calc.period.label if calc.period else None,
              period_end=calc.period.period_end if calc.period else None, trace=calc.trace,
              calculation=calc.calculation or RULES[code].formula_text,
              explanation=f"{RULES[code].name}: {calc.value} {RULES[code].unit}{extra}",
              issues=list(calc.issues))


def weak_cash_conversion(ctx: Ctx, end: date) -> Ev:
    return _single(ctx, "WEAK_CASH_CONVERSION",
                   box5_metric(ctx, "CUMULATIVE_CFO_TO_PAT_5Y", fy(ctx, end), "TIMES"))


def profit_without_cash(ctx: Ctx, end: date) -> Ev:
    return count_ev("PROFIT_WITHOUT_CASH", ctx, end, profit_without_cash_years(ctx, end),
                    "had PAT > 0 and CFO < 0")


def receivables_outrunning(ctx: Ctx, end: date) -> Ev:
    return _single(ctx, "RECEIVABLES_OUTRUNNING_SALES", receivables_gap(ctx, end))


def rising_debtor_days(ctx: Ctx, end: date) -> Ev:
    return _single(ctx, "RISING_DEBTOR_DAYS",
                   days_vs_average(ctx, "RECEIVABLE_DAYS", end, relative=False))


def inventory_buildup(ctx: Ctx, end: date) -> Ev:
    return _single(ctx, "INVENTORY_BUILDUP",
                   days_vs_average(ctx, "INVENTORY_DAYS", end, relative=True))


def other_income_dependence(ctx: Ctx, end: date) -> Ev:
    return _single(ctx, "OTHER_INCOME_DEPENDENCE",
                   box5_metric(ctx, "OTHER_INCOME_TO_PBT", fy(ctx, end), "PERCENT"))


def high_accruals(ctx: Ctx, end: date) -> Ev:
    return _single(ctx, "HIGH_ACCRUALS",
                   box5_metric(ctx, "CASH_FLOW_ACCRUAL_RATIO", fy(ctx, end), "PERCENT"))


def operating_ebitda(ctx: Ctx, end: date) -> Calc:
    p = fy(ctx, end)
    op = canonical_value(ctx, [M.OPERATING_PROFIT], p)
    if op.ok:
        op.calculation = f"operating profit {op.value} (Box 4 OPERATING_PROFIT)"
        return op
    ebitda = box5_metric(ctx, "EBITDA", p, "MONEY")
    other = canonical_value(ctx, [M.OTHER_INCOME], p)
    trace = combine(op, ebitda, other)
    gap = first_gap(ebitda, other)
    if gap or ebitda.value is None or other.value is None:
        return gap_calc(gap or ebitda, p, trace)
    return Calc("AVAILABLE", q(ebitda.value - other.value), None, p, trace,
                issues=["OPERATING_EBITDA_FROM_EBITDA_LESS_OTHER_INCOME"],
                calculation=f"EBITDA {ebitda.value} - other income {other.value}")


def net_debt_to_operating_ebitda(ctx: Ctx, end: date) -> tuple[Calc, Calc, Calc]:
    net = box5_metric(ctx, "NET_DEBT", pit(ctx, end), "MONEY")
    ebitda = operating_ebitda(ctx, end)
    trace = combine(net, ebitda)
    p = fy(ctx, end)
    if not ebitda.ok or ebitda.value is None:
        return gap_calc(ebitda, p, trace), net, ebitda
    if ebitda.value <= 0:
        return Calc("NOT_APPLICABLE", None, "NON_POSITIVE_OPERATING_EBITDA", p, trace), net, ebitda
    if not net.ok or net.value is None:
        return gap_calc(net, p, trace), net, ebitda
    return Calc("AVAILABLE", q(net.value / ebitda.value), None, p, trace,
                issues=list(ebitda.issues),
                calculation=f"net debt {net.value} / operating EBITDA {ebitda.value}"), net, ebitda


def leverage(ctx: Ctx, end: date) -> Ev:
    calc, net, _ = net_debt_to_operating_ebitda(ctx, end)
    ev = _single(ctx, "LEVERAGE", calc)
    override = ctx.request.leverage_override
    if override is not None:
        ev.thresholds = (T(SV.RED, "GT", override.red_above, "sector override"),
                         T(SV.YELLOW, "GT", override.yellow_above, "sector override"))
        ctx.explicit(ev.trace, "leverage override", override.red_above, "TIMES",
                     uuid5(NS, f"input|leverage_override|{override.model_dump_json()}"),
                     override.source)
        if ev.status == S.EVALUATED and ev.value is not None:
            ev.severity = grade(ev.value, ev.thresholds)
    if ev.status == S.EVALUATED and net.value is not None and net.value <= 0:
        ev.severity = SV.GREEN
        ev.explanation = f"net cash ({net.value}): leverage graded GREEN"
    return ev


def weak_interest_cover(ctx: Ctx, end: date) -> Ev:
    calc = box5_metric(ctx, "INTEREST_COVERAGE", fy(ctx, end), "TIMES")
    ev = _single(ctx, "WEAK_INTEREST_COVER", calc)
    if ev.status != S.EVALUATED and calc.reason and "DIVISION_BY_ZERO" in calc.reason:
        ev.reason = "ZERO_FINANCE_COST"
    return ev


def refinancing_risk(ctx: Ctx, end: date) -> Ev:
    b = basis_of(ctx.analytical)
    p = fy(ctx, end)
    de = box5_metric(ctx, "DEBT_TO_EQUITY", p, "TIMES")
    st = canonical_value(ctx, [M.SHORT_TERM_BORROWINGS], pit(ctx, end))
    gross = box5_metric(ctx, "GROSS_DEBT", pit(ctx, end), "MONEY")
    trace = combine(de, st, gross)
    if not de.ok or de.value is None:
        return unavailable(b, (de.reason or "UNAVAILABLE").split(":")[0], period=p, trace=trace,
                           status=S.NOT_APPLICABLE if de.status == "NOT_APPLICABLE"
                           else S.UNAVAILABLE)
    if de.value <= D("0.5"):
        return unavailable(b, "DEBT_TO_EQUITY_AT_MOST_0_5", status=S.NOT_APPLICABLE, period=p,
                           trace=trace, explanation=f"debt / equity {de.value} <= 0.5")
    gap = first_gap(st, gross)
    if gap or st.value is None or gross.value is None:
        return unavailable(b, ((gap or st).reason or "UNAVAILABLE").split(":")[0], period=p,
                           trace=trace)
    if gross.value <= 0 or st.value > gross.value or st.value < 0:
        return unavailable(b, "INCONSISTENT_DEBT_COMPONENTS", period=p, trace=trace)
    share = q(st.value * HUNDRED / gross.value)
    severity = grade(share, RULES["REFINANCING_RISK"].thresholds)
    issues: list[str] = []
    if severity == SV.RED:
        cfo = canonical_value(ctx, [M.CFO], p)
        cfo0 = canonical_value(ctx, [M.CFO], fy(ctx, shift(end, -12)))
        trace.merge(combine(cfo, cfo0))
        if cfo.value is None or cfo0.value is None:
            severity = SV.YELLOW
            issues.append("CFO_TREND_UNAVAILABLE")
        elif not cfo.value < cfo0.value:
            severity = SV.YELLOW
            issues.append("CFO_NOT_DETERIORATING")
    return Ev(S.EVALUATED, b, severity, share, period_label=p.label, period_end=p.period_end,
              trace=trace, issues=issues,
              calculation=f"short-term {st.value} / gross {gross.value} x 100",
              explanation=f"short-term borrowings are {share} % of gross debt "
              f"(debt / equity {de.value})")


def unusual_tax_rate(ctx: Ctx, end: date) -> Ev:
    b = basis_of(ctx.analytical)
    p = fy(ctx, end)
    rates = [box5_metric(ctx, "TAX_RATE", y, "PERCENT") for y in years_back(ctx, end, 5)]
    pbts = [canonical_value(ctx, [M.PBT], y) for y in years_back(ctx, end, 5)]
    trace = combine(*rates, *pbts)
    severity: SV | None = None
    parts: list[str] = []
    issues: list[str] = []
    negative = sum(
        1 for r, x in zip(rates, pbts, strict=True)
        if r.value is not None and x.value is not None and x.value > 0 and r.value < 0
    )
    known = sum(1 for r, x in zip(rates, pbts, strict=True)
                if r.value is not None and x.value is not None)
    rule = RULES["UNUSUAL_TAX_RATE"].thresholds
    if known:
        sev = SV.RED if negative >= 2 else SV.YELLOW if negative >= 1 else SV.GREEN
        severity = sev
        parts.append(f"negative tax while profitable in {negative} of {known} years")
    latest = rates[0].value
    history = [r.value for r in rates[1:] if r.value is not None and 0 <= r.value <= 60]
    value: Decimal | None = None
    if latest is not None and len(history) >= 3:
        mid = D(str(median(history)))
        value = q(abs(latest - mid))
        sev = SV.YELLOW if value > rule[2].value else SV.GREEN
        severity = sev if severity is None else worse(severity, sev)
        parts.append(f"|ETR {latest} - median {mid}| = {value} pp")
    statutory = ctx.request.statutory_tax_rate
    if latest is not None and statutory is not None and (
        p.fiscal_year is None or p.fiscal_year >= statutory.applies_from_fiscal_year
    ):
        ctx.explicit(trace, "statutory tax rate", statutory.rate_pct, "PERCENT",
                     uuid5(NS, f"input|statutory_tax|{statutory.model_dump_json()}"),
                     statutory.source)
        gap = q(abs(latest - statutory.rate_pct))
        value = gap if value is None else max(value, gap)
        sev = SV.YELLOW if gap > rule[2].value else SV.GREEN
        severity = sev if severity is None else worse(severity, sev)
        parts.append(f"|ETR {latest} - statutory {statutory.rate_pct}| = {gap} pp")
    elif statutory is None:
        issues.append("NO_CITED_STATUTORY_RATE")
    if severity is None:
        return unavailable(b, "TAX_RATE_UNAVAILABLE", period=p, trace=trace)
    if known < 5 and severity == SV.GREEN and value is None:
        return unavailable(b, "INCOMPLETE_WINDOW", period=p, trace=trace)
    return Ev(S.EVALUATED, b, severity, value if value is not None else D(negative),
              period_label=p.label, period_end=p.period_end, trace=trace, issues=issues,
              unit="PERCENTAGE_POINTS" if value is not None else "YEARS",
              calculation="; ".join(parts), explanation="; ".join(parts))


def recurring_exceptionals(ctx: Ctx, end: date) -> Ev:
    return count_ev("RECURRING_EXCEPTIONALS", ctx, end, exceptional_years(ctx, end),
                    "reported non-zero exceptional items")


def net_worth(ctx: Ctx, end: date, basis: ReportingBasis | None = None) -> Calc:
    return box5_metric(ctx, "SHAREHOLDERS_EQUITY", pit(ctx, end), "MONEY", basis)


def goodwill_ratio(ctx: Ctx, end: date) -> tuple[Calc, int]:
    b = pit(ctx, end)
    parts = [canonical_value(ctx, [M.GOODWILL], b), canonical_value(ctx, [M.INTANGIBLE_ASSETS], b)]
    nw = net_worth(ctx, end)
    trace = combine(*parts, nw)
    p = fy(ctx, end)
    if not nw.ok or nw.value is None:
        return gap_calc(nw, p, trace), 0
    if nw.value <= 0:
        return Calc("NOT_APPLICABLE", None, "NON_POSITIVE_NET_WORTH", p, trace), 0
    known = [c.value for c in parts if c.value is not None]
    if not known:
        return gap_calc(parts[0], p, trace), 2
    total = sum(known, D(0))
    calc = Calc("AVAILABLE", q(total * HUNDRED / nw.value), None, p, trace,
                calculation=f"({' + '.join(map(str, known))}) / {nw.value} x 100")
    return calc, 2 - len(known)


def goodwill_heavy(ctx: Ctx, end: date) -> Ev:
    calc, missing = goodwill_ratio(ctx, end)
    ev = _single(ctx, "GOODWILL_HEAVY", calc)
    if missing and ev.status == S.EVALUATED:
        if ev.severity == SV.GREEN:
            return unavailable(ev.basis, "COMPONENT_NOT_REPORTED", period=calc.period,
                               trace=calc.trace, value=calc.value,
                               explanation="goodwill or intangibles not reported: a clean "
                               "reading needs both")
        ev.issues.append("LOWER_BOUND_COMPONENT_NOT_REPORTED")
    return ev


def negative_equity(ctx: Ctx, end: date) -> Ev:
    return _single(ctx, "NEGATIVE_EQUITY", net_worth(ctx, end))


# --- positive controls ----------------------------------------------------------------------


def positive_controls(ctx: Ctx, end: date | None) -> dict[str, UUID]:
    out: dict[str, UUID] = {}
    if end is None or ctx.financial:
        return out
    p = fy(ctx, end)
    out["STRONG_CASH_CONVERSION"] = ctx.emit(
        "STRONG_CASH_CONVERSION",
        _single(ctx, "STRONG_CASH_CONVERSION",
                box5_metric(ctx, "CUMULATIVE_CFO_TO_PAT_5Y", p, "TIMES")),
    )
    fcf = [box5_metric(ctx, "FCF", y, "MONEY") for y in years_back(ctx, end, 5)]
    if all(c.ok for c in fcf):
        positive = sum(1 for c in fcf if c.value is not None and c.value > 0)
        out["POSITIVE_FCF_HISTORY"] = ctx.emit("POSITIVE_FCF_HISTORY", Ev(
            S.EVALUATED, basis_of(ctx.analytical),
            grade(D(positive), RULES["POSITIVE_FCF_HISTORY"].thresholds), D(positive),
            period_label=p.label, period_end=p.period_end, trace=combine(*fcf),
            calculation=f"FCF > 0 in {positive} of 5 fiscal years",
            explanation=f"free cash flow positive in {positive} of the last 5 fiscal years"))
    net = box5_metric(ctx, "NET_DEBT", pit(ctx, end), "MONEY")
    out["NET_CASH"] = ctx.emit("NET_CASH", _single(ctx, "NET_CASH", net))
    out["STRONG_INTEREST_COVER"] = ctx.emit(
        "STRONG_INTEREST_COVER",
        _single(ctx, "STRONG_INTEREST_COVER", box5_metric(ctx, "INTEREST_COVERAGE", p, "TIMES")),
    )
    de, de2 = (box5_metric(ctx, "DEBT_TO_EQUITY", y, "TIMES")
               for y in (p, fy(ctx, shift(end, -24))))
    if de.ok and de2.ok and de.value is not None and de2.value is not None:
        out["DECLINING_LEVERAGE"] = ctx.emit("DECLINING_LEVERAGE", _single(
            ctx, "DECLINING_LEVERAGE",
            Calc("AVAILABLE", q(de.value - de2.value), None, p, combine(de, de2),
                 calculation=f"{de.value} - {de2.value}")))
    out["IMPROVING_DEBTOR_DAYS"] = ctx.emit(
        "IMPROVING_DEBTOR_DAYS",
        _single(ctx, "IMPROVING_DEBTOR_DAYS",
                days_vs_average(ctx, "RECEIVABLE_DAYS", end, relative=False)),
    )
    return out


# --- forensic metric table --------------------------------------------------------------------


def metric_of(ctx: Ctx, code: str, calc: Calc, kind: MetricKind, layer: Layer, unit: str,
              label: str, formula_id: str, basis: ReportingBasis | None = None) -> ForensicMetric:
    definition = FORMULAS.get(code) or next(
        (f for f in FORMULAS.values() if f.formula_id == formula_id), None)
    if ctx.financial and definition is not None and definition.applicability != "all sectors":
        calc = Calc("NOT_APPLICABLE", None, "BUSINESS_MODEL_NOT_APPLICABLE", calc.period,
                    Trace())
    b = basis_of(basis or ctx.analytical)
    period = calc.period
    available = calc.ok and bool(calc.trace.record_ids)
    return ForensicMetric(
        metric_id=uuid5(NS, f"{ctx.dd_key}|metric|{code}|{b.value}|"
                        f"{period.key() if period else '-'}"),
        code=code,
        label=label,
        kind=kind,
        layer=layer,
        status="AVAILABLE" if available else (
            "NOT_APPLICABLE" if calc.status == "NOT_APPLICABLE" else "UNAVAILABLE"),
        value=q(calc.value) if available and calc.value is not None else None,
        unit=unit,
        period=period.label if period else None,
        period_end=period.period_end if period else None,
        basis=b,
        formula_id=formula_id,
        record_ids=tuple(sorted(calc.trace.record_ids, key=str)),
        reason=None if available else (calc.reason or "no upstream record")[:1000],
        issues=tuple(sorted(set(calc.issues))),
    )


BOX5_TABLE: tuple[tuple[str, str, str, str], ...] = (
    # code, label, unit, period kind (FY / PIT)
    ("CFO_TO_PAT", "CFO / PAT", "TIMES", "FY"),
    ("CFO_TO_EBITDA", "CFO / EBITDA", "TIMES", "FY"),
    ("CUMULATIVE_CFO_TO_PAT_5Y", "Cumulative CFO / PAT (5Y)", "TIMES", "FY"),
    ("CUMULATIVE_CFO_TO_PAT_3Y", "Cumulative CFO / PAT (3Y)", "TIMES", "FY"),
    ("CASH_FLOW_ACCRUAL_RATIO", "Accrual ratio (PAT - CFO) / avg assets", "PERCENT", "FY"),
    ("RECEIVABLES_VS_REVENUE_GROWTH", "Receivables growth - revenue growth (YoY)",
     "PERCENTAGE_POINTS", "FY"),
    ("INVENTORY_VS_REVENUE_GROWTH", "Inventory growth - revenue growth (YoY)",
     "PERCENTAGE_POINTS", "FY"),
    ("RECEIVABLE_DAYS", "Debtor days", "DAYS", "FY"),
    ("INVENTORY_DAYS", "Inventory days", "DAYS", "FY"),
    ("PAYABLE_DAYS", "Payable days", "DAYS", "FY"),
    ("CASH_CONVERSION_CYCLE", "Working-capital days", "DAYS", "FY"),
    ("NWC_TO_REVENUE", "Working capital / revenue", "PERCENT", "FY"),
    ("OTHER_INCOME_TO_PBT", "Other income / PBT", "PERCENT", "FY"),
    ("EXCEPTIONAL_TO_PBT", "Exceptional items / PBT", "PERCENT", "FY"),
    ("TAX_RATE", "Effective tax rate", "PERCENT", "FY"),
    ("TAX_RATE_RANGE_5Y", "Effective tax rate range (5Y)", "PERCENTAGE_POINTS", "FY"),
    ("DEPRECIATION_RATE", "Depreciation rate", "PERCENT", "FY"),
    ("DEPRECIATION_RATE_CHANGE_YOY", "Depreciation rate change", "PERCENTAGE_POINTS", "FY"),
    ("CWIP_TO_FIXED_ASSETS", "CWIP / fixed assets", "PERCENT", "FY"),
    ("CUMULATIVE_FCF_5Y", "Cumulative FCF (5Y)", "MONEY", "FY"),
    ("DIVIDENDS_TO_FCF", "Dividends / FCF", "PERCENT", "FY"),
    ("DIVIDEND_PAYOUT", "Dividend payout", "PERCENT", "FY"),
    ("SHARE_CAPITAL_GROWTH_YOY", "Share-capital growth (YoY)", "PERCENT", "FY"),
    ("DEBT_TO_EQUITY", "Debt / equity", "TIMES", "FY"),
    ("NET_DEBT_TO_EBITDA", "Net debt / EBITDA (Box 5 EBITDA)", "TIMES", "FY"),
    ("INTEREST_COVERAGE", "Interest cover", "TIMES", "FY"),
)


def box13_table(ctx: Ctx, end: date) -> list[tuple[str, Calc, MetricKind, str]]:
    p, b = fy(ctx, end), pit(ctx, end)
    out: list[tuple[str, Calc, MetricKind, str]] = []

    def add(code: str, calc: Calc, kind: MetricKind = MetricKind.DERIVED) -> None:
        out.append((code, calc, kind, FORMULAS[code].unit))

    add("RECEIVABLES_CAGR_3Y", receivables_cagr(ctx, end))
    add("RECEIVABLES_CAGR_GAP_3Y", receivables_gap(ctx, end))
    add("DEBTOR_DAYS_VS_3Y_AVG", days_vs_average(ctx, "RECEIVABLE_DAYS", end, False))
    add("INVENTORY_DAYS_VS_3Y_AVG", days_vs_average(ctx, "INVENTORY_DAYS", end, True))
    years = years_back(ctx, end, 5)
    cfo = [canonical_value(ctx, [M.CFO], y) for y in years]
    ebitda = [box5_metric(ctx, "EBITDA", y, "MONEY") for y in years]
    trace = combine(*cfo, *ebitda)
    gap = first_gap(*cfo, *ebitda)
    if gap:
        add("CUMULATIVE_CFO_TO_EBITDA_5Y", gap_calc(gap, p, trace))
    else:
        total_e = sum((c.value for c in ebitda if c.value is not None), D(0))
        total_c = sum((c.value for c in cfo if c.value is not None), D(0))
        add("CUMULATIVE_CFO_TO_EBITDA_5Y",
            Calc("AVAILABLE", q(total_c / total_e), None, p, trace,
                 calculation=f"{total_c} / {total_e}") if total_e > 0
            else Calc("NOT_APPLICABLE", None, "NON_POSITIVE_CUMULATIVE_EBITDA", p, trace))
    add("EXCEPTIONAL_TO_PAT", ratio_calc(
        ctx, "EXCEPTIONAL_TO_PAT", canonical_value(ctx, [M.EXCEPTIONAL_ITEMS], p),
        canonical_value(ctx, [M.PAT], p), p, absolute_numerator=True))
    for code, counted in (("EXCEPTIONAL_YEARS_5Y", exceptional_years(ctx, end)),
                          ("PROFIT_WITHOUT_CASH_YEARS_5Y", profit_without_cash_years(ctx, end))):
        count, known, missing, t = counted
        add(code, Calc("AVAILABLE", D(count), None, p, t,
                       issues=["LOWER_BOUND_INCOMPLETE_WINDOW"] if missing else [])
            if known and not missing else Calc("UNAVAILABLE", None,
                                               f"{missing} of 5 fiscal years not available",
                                               p, t))
    debt = [box5_metric(ctx, "GROSS_DEBT", pit(ctx, shift(end, -12 * k)), "MONEY")
            for k in (0, 1)]
    fc = canonical_value(ctx, [M.FINANCE_COST], p)
    gap = first_gap(fc, *debt)
    trace = combine(fc, *debt)
    if gap:
        add("FINANCE_COST_TO_AVERAGE_DEBT", gap_calc(gap, p, trace))
    else:
        avg = sum((c.value for c in debt if c.value is not None), D(0)) / 2
        add("FINANCE_COST_TO_AVERAGE_DEBT",
            Calc("AVAILABLE", q(abs(fc.value or D(0)) * HUNDRED / avg), None, p, trace)
            if avg > 0 else Calc("NOT_APPLICABLE", None, "NO_AVERAGE_DEBT", p, trace))
    add("CWIP_GROWTH_YOY", growth_calc(
        canonical_value(ctx, [M.CWIP], b),
        canonical_value(ctx, [M.CWIP], pit(ctx, shift(end, -12))), p))
    gw, _ = goodwill_ratio(ctx, end)
    add("GOODWILL_INTANGIBLES_TO_NET_WORTH", gw)
    fcf = [box5_metric(ctx, "FCF", y, "MONEY") for y in years]
    gap = first_gap(*fcf)
    add("POSITIVE_FCF_YEARS_5Y", gap_calc(gap, p, combine(*fcf)) if gap else Calc(
        "AVAILABLE", D(sum(1 for c in fcf if c.value is not None and c.value > 0)), None, p,
        combine(*fcf)))
    add("SHARE_CAPITAL_GROWTH_3Y", growth_calc(
        canonical_value(ctx, [M.EQUITY_SHARE_CAPITAL], b),
        canonical_value(ctx, [M.EQUITY_SHARE_CAPITAL], pit(ctx, shift(end, -36))), p))
    gd = box5_metric(ctx, "GROSS_DEBT", b, "MONEY")
    gd0 = box5_metric(ctx, "GROSS_DEBT", pit(ctx, shift(end, -36)), "MONEY")
    ta = canonical_value(ctx, [M.TOTAL_ASSETS], b)
    ta0 = canonical_value(ctx, [M.TOTAL_ASSETS], pit(ctx, shift(end, -36)))
    gap = first_gap(gd, gd0, ta, ta0)
    trace = combine(gd, gd0, ta, ta0)
    if gap:
        add("DEBT_FUNDED_GROWTH_3Y", gap_calc(gap, p, trace))
    else:
        assets = (ta.value or D(0)) - (ta0.value or D(0))
        add("DEBT_FUNDED_GROWTH_3Y",
            Calc("AVAILABLE", q(((gd.value or D(0)) - (gd0.value or D(0))) * HUNDRED / assets),
                 None, p, trace) if assets > 0
            else Calc("NOT_APPLICABLE", None, "NO_ASSET_GROWTH", p, trace))
    add("WORKING_CAPITAL_ABSORPTION", working_capital_absorption(ctx, end))
    add("NET_DEBT_TO_OPERATING_EBITDA", net_debt_to_operating_ebitda(ctx, end)[0])
    return out


def growth_calc(now: Calc, then: Calc, period: FinancialPeriod) -> Calc:
    """(now / then - 1) x 100 over two reported balances; the base must be positive."""
    trace = combine(now, then)
    gap = first_gap(now, then)
    if gap or now.value is None or then.value is None:
        return gap_calc(gap or now, period, trace)
    if then.value <= 0:
        return Calc("NOT_APPLICABLE", None, "NON_POSITIVE_BASE", period, trace)
    return Calc("AVAILABLE", q((now.value / then.value - 1) * HUNDRED), None, period, trace,
                calculation=f"({now.value} / {then.value} - 1) x 100")


def working_capital_absorption(ctx: Ctx, end: date) -> Calc:
    p = fy(ctx, end)
    wc = canonical_value(ctx, [M.WORKING_CAPITAL_CHANGES], p)
    base = canonical_value(ctx, [M.CFO_BEFORE_WORKING_CAPITAL], p)
    trace = combine(wc, base)
    gap = first_gap(wc, base)
    if gap or wc.value is None or base.value is None:
        return gap_calc(gap or wc, p, trace)
    if base.value <= 0:
        return Calc("NOT_APPLICABLE", None, "NON_POSITIVE_CFO_BEFORE_WORKING_CAPITAL", p, trace)
    return Calc("AVAILABLE", q(-wc.value * HUNDRED / base.value), None, p, trace,
                calculation=f"-({wc.value}) / {base.value} x 100")


def adjusted(ctx: Ctx, end: date) -> list[tuple[str, Calc]]:
    p = fy(ctx, end)
    pat = canonical_value(ctx, [M.PAT], p)
    rate = box5_metric(ctx, "TAX_RATE", p, "PERCENT")
    out: list[tuple[str, Calc]] = []
    for code, metric in (("ADJUSTED_PAT_EX_EXCEPTIONALS", M.EXCEPTIONAL_ITEMS),
                         ("ADJUSTED_PAT_EX_OTHER_INCOME", M.OTHER_INCOME)):
        item = canonical_value(ctx, [metric], p)
        trace = combine(pat, item, rate)
        gap = first_gap(pat, item, rate)
        if gap or pat.value is None or item.value is None or rate.value is None:
            out.append((code, gap_calc(gap or pat, p, trace)))
            continue
        if not D(0) <= rate.value <= HUNDRED:
            out.append((code, Calc("NOT_APPLICABLE", None, "TAX_RATE_OUTSIDE_0_100", p, trace)))
            continue
        value = pat.value - item.value * (1 - rate.value / HUNDRED)
        out.append((code, Calc("AVAILABLE", q(value), None, p, trace,
                               calculation=f"{pat.value} - {item.value} x (1 - {rate.value}/100)")))
    pbt = canonical_value(ctx, [M.PBT], p)
    rates = [box5_metric(ctx, "TAX_RATE", y, "PERCENT") for y in years_back(ctx, end, 5)]
    usable = [r.value for r in rates if r.value is not None and 0 <= r.value <= 60]
    trace = combine(pbt, *rates)
    if not pbt.ok or pbt.value is None:
        out.append(("TAX_NORMALIZED_PAT", gap_calc(pbt, p, trace)))
    elif len(usable) < 3:
        out.append(("TAX_NORMALIZED_PAT", Calc("UNAVAILABLE", None,
                                               "FEWER_THAN_3_USABLE_TAX_RATES", p, trace)))
    else:
        mid = D(str(median(usable)))
        out.append(("TAX_NORMALIZED_PAT", Calc(
            "AVAILABLE", q(pbt.value * (1 - mid / HUNDRED)), None, p, trace,
            calculation=f"{pbt.value} x (1 - {mid}/100)")))
    return out


# --- forensic models (Box 5's, plus Altman with a market value) -------------------------------


def model_view(result: ForensicModelResult) -> ForensicModelView:
    complete = result.status == CalculationStatus.CALCULATED and all(
        c.status != ComponentStatus.UNAVAILABLE for c in result.components
    )
    status: Literal["CALCULATED", "UNAVAILABLE", "NOT_APPLICABLE"] = (
        "CALCULATED" if complete and result.score is not None
        else "NOT_APPLICABLE" if result.status == CalculationStatus.UNSUPPORTED_SECTOR
        else "UNAVAILABLE"
    )
    return ForensicModelView(
        model=result.model.value,
        variant=result.variant,
        status=status,
        score=result.score if status == "CALCULATED" else None,
        zone=result.zone if status == "CALCULATED" else None,
        formula=result.formula[:1000],
        components=tuple(
            ModelComponent(name=c.name, status=c.status.value,
                           value=c.value, source=Layer.BOX5,
                           detail="; ".join(c.missing_inputs)[:1000] or None)
            for c in result.components
        ),
        missing_components=tuple(
            c.name for c in result.components if c.status == ComponentStatus.UNAVAILABLE
        ),
        record_ids=(result.result_id,),
        applicability=result.applicability[:1000],
        warnings=result.warnings,
    )


ALTMAN_WEIGHTS = {"X1": D("1.2"), "X2": D("1.4"), "X3": D("3.3"), "X4": D("0.6"), "X5": D("1.0")}


def altman_original(ctx: Ctx, end: date | None, market_cap: tuple[Decimal, UUID, str] | None,
                    box5_result: ForensicModelResult | None) -> ForensicModelView:
    formula = FORMULAS["ALTMAN_Z_ORIGINAL"].formula_text
    names = tuple(ALTMAN_WEIGHTS)

    def view(status: str, why: str, components: Sequence[ModelComponent] = (),
             score: Decimal | None = None, zone: str | None = None,
             records: Sequence[UUID] = (), warnings: Sequence[str] = ()) -> ForensicModelView:
        missing = tuple(c.name for c in components if c.status == "UNAVAILABLE") if components \
            else names
        return ForensicModelView(
            model="ALTMAN_Z", variant="ORIGINAL_1968_MARKET_VALUE", status=status,  # type: ignore[arg-type]
            score=score, zone=zone, formula=formula, components=tuple(components),
            missing_components=() if status == "CALCULATED" else missing,
            record_ids=tuple(sorted(set(records), key=str)), applicability=why[:1000],
            warnings=tuple(sorted(set(warnings))),
        )

    if ctx.financial:
        return view("NOT_APPLICABLE", "not used for banks, NBFCs or insurers")
    if end is None or box5_result is None:
        return view("UNAVAILABLE", "no fiscal year / Box 5 Altman result")
    parts = {c.name: c for c in box5_result.components}
    comps: list[ModelComponent] = []
    records: list[UUID] = [box5_result.result_id]
    warnings: list[str] = []
    for name in ("X1", "X3", "X5"):
        c = parts.get(name)
        comps.append(ModelComponent(
            name=name, status="AVAILABLE" if c and c.value is not None else "UNAVAILABLE",
            value=c.value if c else None, source=Layer.BOX5,
            detail=None if c and c.value is not None else "Box 5 component unavailable"))
    assets = ctx.canonical([M.TOTAL_ASSETS], pit(ctx, end))
    liabilities = ctx.m5("TOTAL_LIABILITIES", pit(ctx, end))
    retained = next((r for r in ctx.request.retained_earnings
                     if r.as_of == end and r.basis == ctx.analytical.value), None)
    x2: Decimal | None = None
    detail = None
    if retained is not None and isinstance(assets, Fact) and assets.value > 0:
        x2 = q(retained.value_crore / assets.value)
        records.append(uuid5(NS, f"input|retained_earnings|{retained.model_dump_json()}"))
        records.extend(i.canonical_value_id for i in assets.inputs)
    elif retained is None and ctx.request.altman_allow_reserves_proxy:
        proxy = parts.get("X2")
        x2 = proxy.value if proxy else None
        warnings.append("X2_RESERVES_PROXY_NOT_RETAINED_EARNINGS")
    elif conflicted(ctx, "retained_earnings",
                    lambda r: r.as_of == end and r.basis == ctx.analytical.value):
        detail = ("cited retained earnings disagree for the latest balance-sheet date and "
                  "basis (see source disagreements); no value is selected")
    else:
        detail = ("retained earnings not supplied for the latest balance-sheet date and basis "
                  "(reserves are not retained earnings; never invented)")
    comps.insert(1, ModelComponent(name="X2", status="AVAILABLE" if x2 is not None else
                                   "UNAVAILABLE", value=x2,
                                   source=Layer.REQUEST if retained else Layer.BOX5,
                                   detail=detail))
    x4: Decimal | None = None
    if market_cap is not None and isinstance(liabilities, Fact) and liabilities.value > 0:
        x4 = q(market_cap[0] / liabilities.value)
        records.append(market_cap[1])
        records.extend(liabilities.derived_from)
        warnings.append(f"MARKET_VALUE_{market_cap[2]}")
    comps.insert(3, ModelComponent(
        name="X4", status="AVAILABLE" if x4 is not None else "UNAVAILABLE", value=x4,
        source=Layer.BOX13,
        detail=None if x4 is not None else "market value of equity / total liabilities "
        "unavailable (cited market cap or Box 12 price x cited share count needed)"))
    if any(c.value is None for c in comps):
        return view("UNAVAILABLE", "public non-financial companies; every component required",
                    comps, records=records, warnings=warnings)
    score = q(sum((ALTMAN_WEIGHTS[c.name] * (c.value or D(0)) for c in comps), D(0)))
    zone = "SAFE" if score > D("2.99") else "GREY" if score >= D("1.81") else "DISTRESS"
    return view("CALCULATED", "public non-financial companies (Altman 1968); X1, X3, X5 from "
                "Box 5", comps, score, zone, records, warnings)


def forensic_models(ctx: Ctx, end: date | None,
                    market_cap: tuple[Decimal, UUID, str] | None) -> list[ForensicModelView]:
    mine = [r for r in ctx.box5.forensic_scores if r.reporting_basis == ctx.analytical]
    views: list[ForensicModelView] = []
    for model in (ForensicModel.PIOTROSKI_F_SCORE, ForensicModel.BENEISH_M):
        result = next((r for r in mine if r.model == model), None)
        if result is not None:
            views.append(model_view(result))
    double = next((r for r in mine if r.model == ForensicModel.ALTMAN_Z
                   and r.variant == "Z_DOUBLE_PRIME_1995"), None)
    if double is not None:
        views.append(model_view(double))
    original = next((r for r in mine if r.model == ForensicModel.ALTMAN_Z
                     and r.variant == "ORIGINAL_1968"), None)
    views.append(altman_original(ctx, end, market_cap, original))
    if ctx.financial:
        views = [v.model_copy(update={"status": "NOT_APPLICABLE", "score": None, "zone": None,
                                      "missing_components": tuple(c.name for c in v.components)
                                      if v.status == "CALCULATED" else v.missing_components})
                 for v in views]
    return views


# --- sections -------------------------------------------------------------------------------


FINANCIAL_RULES: tuple[tuple[str, Callable[[Ctx, date], Ev]], ...] = (
    ("WEAK_CASH_CONVERSION", weak_cash_conversion),
    ("PROFIT_WITHOUT_CASH", profit_without_cash),
    ("OTHER_INCOME_DEPENDENCE", other_income_dependence),
    ("HIGH_ACCRUALS", high_accruals),
)
WORKING_CAPITAL_RULES = (
    ("RECEIVABLES_OUTRUNNING_SALES", receivables_outrunning),
    ("RISING_DEBTOR_DAYS", rising_debtor_days),
    ("INVENTORY_BUILDUP", inventory_buildup),
)
ACCOUNTING_RULES = (
    ("UNUSUAL_TAX_RATE", unusual_tax_rate),
    ("RECURRING_EXCEPTIONALS", recurring_exceptionals),
    ("GOODWILL_HEAVY", goodwill_heavy),
)
LEVERAGE_RULES = (
    ("LEVERAGE", leverage),
    ("WEAK_INTEREST_COVER", weak_interest_cover),
    ("REFINANCING_RISK", refinancing_risk),
    ("NEGATIVE_EQUITY", negative_equity),
)


def run_rules(ctx: Ctx, rules: Sequence[tuple[str, Callable[[Ctx, date], Ev]]],
              end: date | None) -> list[UUID]:
    ids: list[UUID] = []
    for code, fn in rules:
        if not ctx.applies(code):
            ids.append(ctx.emit(code, unavailable(basis_of(ctx.analytical),
                                                  "UNSUPPORTED_SECTOR_RULE",
                                                  status=S.UNSUPPORTED_SECTOR_RULE)))
            continue
        ids.append(periodic(ctx, code, fn, end, shift))
    return ids


def build_forensics(
    ctx: Ctx, end: date | None, market_cap: tuple[Decimal, UUID, str] | None
) -> tuple[FinancialForensics, EarningsQuality, WorkingCapitalForensics, list[UUID],
           list[UUID], list[UUID]]:
    """(forensics, earnings quality, working capital, accounting ids, balance-sheet ids,
    leverage ids)."""
    metrics: list[ForensicMetric] = []
    eq_reported: list[ForensicMetric] = []
    eq_derived: list[ForensicMetric] = []
    eq_adjusted: list[ForensicMetric] = []
    series: list[WorkingCapitalSeries] = []
    disclosures: list[ForensicMetric] = []
    if end is not None:
        p = fy(ctx, end)
        for code, label, unit, kind in BOX5_TABLE:
            period = p if kind == "FY" else pit(ctx, end)
            metric = metric_of(ctx, code, box5_metric(ctx, code, period, unit),
                               MetricKind.DERIVED, Layer.BOX5, unit, label, f"BOX5:{code}")
            metrics.append(metric)
            if code in ("CFO_TO_PAT", "CFO_TO_EBITDA", "CUMULATIVE_CFO_TO_PAT_5Y",
                        "CASH_FLOW_ACCRUAL_RATIO", "OTHER_INCOME_TO_PBT",
                        "RECEIVABLES_VS_REVENUE_GROWTH", "INVENTORY_VS_REVENUE_GROWTH"):
                eq_derived.append(metric)
        for code, calc, kind, unit in box13_table(ctx, end):
            metric = metric_of(ctx, code, calc, kind, Layer.BOX13, unit,
                               FORMULAS[code].name, FORMULAS[code].formula_id)
            metrics.append(metric)
            if code in ("CUMULATIVE_CFO_TO_EBITDA_5Y", "EXCEPTIONAL_TO_PAT",
                        "RECEIVABLES_CAGR_GAP_3Y", "WORKING_CAPITAL_ABSORPTION"):
                eq_derived.append(metric)
        metrics.append(ForensicMetric(
            metric_id=uuid5(NS, f"{ctx.dd_key}|metric|CAPITALIZED_COSTS"), code="CAPITALIZED_COSTS",
            label="Capitalized costs (interest / expenses)", kind=MetricKind.REPORTED,
            layer=Layer.BOX4, status="UNAVAILABLE", value=None, unit="MONEY", period=p.label,
            period_end=p.period_end, basis=basis_of(ctx.analytical), formula_id="NONE",
            record_ids=(), reason="not in the Box 4 canonical taxonomy"))
        for code, metrics_ in (("REVENUE", REVENUE), ("PAT", (M.PAT,)), ("CFO", (M.CFO,)),
                               ("PBT", (M.PBT,)), ("OTHER_INCOME", (M.OTHER_INCOME,)),
                               ("EXCEPTIONAL_ITEMS", (M.EXCEPTIONAL_ITEMS,))):
            eq_reported.append(metric_of(ctx, code, canonical_value(ctx, metrics_, p),
                                         MetricKind.REPORTED, Layer.BOX4, "MONEY",
                                         f"{code} as reported", f"BOX4:{code}"))
        eq_reported.append(metric_of(ctx, "EBITDA", box5_metric(ctx, "EBITDA", p, "MONEY"),
                                     MetricKind.DERIVED, Layer.BOX5, "MONEY", "EBITDA (Box 5)",
                                     "BOX5:EBITDA"))
        for code, calc in adjusted(ctx, end):
            eq_adjusted.append(metric_of(ctx, code, calc, MetricKind.ADJUSTED, Layer.BOX13,
                                         "MONEY", FORMULAS[code].name, FORMULAS[code].formula_id))
        for code, unit in (("RECEIVABLE_DAYS", "DAYS"), ("INVENTORY_DAYS", "DAYS"),
                           ("PAYABLE_DAYS", "DAYS"), ("CASH_CONVERSION_CYCLE", "DAYS"),
                           ("NWC_TO_REVENUE", "PERCENT"), ("RECEIVABLES_TO_REVENUE", "PERCENT")):
            points = [metric_of(ctx, code, box5_metric(ctx, code, y, unit), MetricKind.DERIVED,
                                Layer.BOX5, unit, code, f"BOX5:{code}")
                      for y in reversed(years_back(ctx, end, 5))]
            series.append(WorkingCapitalSeries(code=code, frequency="ANNUAL", points=tuple(points)))
            series.append(WorkingCapitalSeries(
                code=code, frequency="QUARTERLY", points=(),
                reason="Box 4 / Box 5 carry no quarterly balance-sheet working-capital values"))
        disclosures.extend(working_capital_disclosures(ctx, end))
    eq_ids = run_rules(ctx, FINANCIAL_RULES, end)
    wc_ids = run_rules(ctx, WORKING_CAPITAL_RULES, end)
    acc_ids = run_rules(ctx, ACCOUNTING_RULES, end)
    lev_ids = run_rules(ctx, LEVERAGE_RULES, end)
    checks = tuple(
        CheckRef(check_id=c.check_id, code=c.code, status=c.status.value,
                 box5_severity=c.severity.value if c.severity else None, observed=c.observed,
                 period=c.period.label if c.period else None)
        for c in ctx.box5.earnings_quality if c.reporting_basis == ctx.analytical
    )
    models = forensic_models(ctx, end, market_cap)
    return (
        FinancialForensics(metrics=tuple(metrics), models=tuple(models), signal_ids=()),
        EarningsQuality(reported=tuple(eq_reported), derived=tuple(eq_derived),
                        adjusted=tuple(eq_adjusted), box5_checks=checks,
                        signal_ids=ctx.present(eq_ids)),
        WorkingCapitalForensics(series=tuple(series), disclosures=tuple(disclosures),
                                signal_ids=ctx.present(wc_ids)),
        list(ctx.present(acc_ids)),
        list(ctx.present(lev_ids[3:])),
        list(ctx.present(lev_ids[:3])),
    )


def working_capital_disclosures(ctx: Ctx, end: date) -> list[ForensicMetric]:
    out: list[ForensicMetric] = []
    seen: set[tuple[str, str, date]] = set()
    for item in sorted(ctx.request.working_capital,
                       key=lambda w: (w.as_of, w.basis, w.item.value, w.amount_crore)):
        key = (item.item.value, item.basis, item.as_of)
        if key in seen:
            continue
        seen.add(key)
        basis = ReportingBasis(item.basis)
        trace = Trace()
        rid = uuid5(NS, f"input|working_capital|{item.model_dump_json()}")
        ctx.explicit(trace, item.item.value, item.amount_crore, "MONEY", rid, item.source,
                     basis=SignalBasis(item.basis), period_end=item.as_of)
        period = fy(ctx, item.as_of)
        revenue = canonical_value(ctx, REVENUE, period, basis=basis)
        amount = Calc("AVAILABLE", item.amount_crore, None, period, trace)
        calc = ratio_calc(ctx, "CONTRACT_ITEM_TO_REVENUE", amount, revenue, period)
        if item.currency != ctx.currency:
            calc = Calc("UNAVAILABLE", None, "CURRENCY_MISMATCH", period, calc.trace)
        out.append(metric_of(ctx, f"{item.item.value}_TO_REVENUE", calc, MetricKind.DERIVED,
                             Layer.BOX13, "PERCENT", f"{item.item.value} / revenue",
                             FORMULAS["CONTRACT_ITEM_TO_REVENUE"].formula_id, basis))
    return out
