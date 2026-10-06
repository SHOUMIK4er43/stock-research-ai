"""DETAILED_RESEARCH_V1 Box 5: earnings-quality checks and forensic models.

Every output here is a SCREENING SIGNAL built from explicit calculations. A check grades one
calculated metric with a versioned rule (financial_rules) into INFO / LOW / MEDIUM / HIGH;
a model is scored only when every component is available, and an unavailable component is
reported as such, never counted as a pass, a fail or a zero. Nothing here says that
anything improper happened; strong readings "require review".
"""

from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from stock_research_ai.application.financial_metrics import (
    FIXED,
    NS,
    REVENUE,
    Engine,
    Fact,
    Got,
    Missing,
    quantize,
    shift,
)
from stock_research_ai.application.financial_rules import (
    EARNINGS_QUALITY_RULES,
    REPEATED_NEGATIVE_FCF,
    GradedRule,
)
from stock_research_ai.domain.financial_analytics import (
    AnalyticsUnit,
    CalculationStatus,
    ComponentStatus,
    EarningsQualityCheck,
    ForensicComponent,
    ForensicModel,
    ForensicModelResult,
    Severity,
)
from stock_research_ai.domain.financial_canonical import CanonicalMetric, FinancialPeriod

M = CanonicalMetric
S = CalculationStatus
D = Decimal


def canonical_ids(*gots: Got) -> tuple[UUID, ...]:
    return tuple(sorted({i.canonical_value_id for g in gots for i in g.inputs}, key=str))


def metric_ids(*gots: Got) -> tuple[UUID, ...]:
    return tuple(sorted({d for g in gots for d in g.derived_from}, key=str))


def grade(rule: GradedRule, observed: Decimal) -> Severity:
    return rule.grade(observed)


# --- earnings-quality checks -----------------------------------------------------------------


def earnings_quality(e: Engine, latest: date | None) -> list[EarningsQualityCheck]:
    p = e.periods.fy(latest) if latest else None
    checks = [_check(e, rule, p) for rule in EARNINGS_QUALITY_RULES if e.sector in rule.sectors]
    if not e.financial:
        checks.append(_negative_fcf(e, latest))
    return checks


def _check(e: Engine, rule: GradedRule, p: FinancialPeriod | None) -> EarningsQualityCheck:
    common: dict[str, Any] = {
        "check_id": uuid5(NS, f"{e.analytics_key}|{e.frame.basis.value}|check|{rule.code}"),
        "rule_id": rule.rule_id,
        "code": rule.code,
        "reporting_basis": e.frame.basis,
        "period": p,
        "unit": rule.unit,
    }
    if p is None:
        return EarningsQualityCheck(
            **common,
            status=S.INSUFFICIENT_HISTORY,
            severity=None,
            observed=None,
            observation="no fiscal year available",
            metric_ids=(),
            canonical_value_ids=(),
        )
    got = e.got(rule.metric, p)
    condition = e.got(rule.condition.metric, p) if rule.condition else None
    gots = [x for x in (got, condition) if x is not None]
    ids: dict[str, Any] = {
        "metric_ids": metric_ids(*gots),
        "canonical_value_ids": canonical_ids(*gots),
    }
    if isinstance(got, Missing):
        return EarningsQualityCheck(
            **common,
            **ids,
            status=got.status,
            severity=None,
            observed=None,
            observation=f"{rule.metric} unavailable: {got.what}",
        )
    if isinstance(condition, Missing) and rule.condition is not None:
        return EarningsQualityCheck(
            **common,
            **ids,
            status=condition.status,
            severity=None,
            observed=got.value,
            observation=f"condition '{rule.condition.meaning}' unavailable: {condition.what}",
        )
    if (
        isinstance(condition, Fact)
        and rule.condition is not None
        and not rule.condition.holds(condition.value)
    ):
        return EarningsQualityCheck(
            **common,
            **ids,
            status=S.CALCULATED,
            severity=Severity.INFO,
            observed=got.value,
            observation=f"rule applies only when {rule.condition.meaning}; "
            f"{rule.condition.metric} = {condition.value}",
        )
    severity = grade(rule, got.value)
    threshold = rule.threshold(severity) if severity != Severity.INFO else rule.low
    return EarningsQualityCheck(
        **common,
        **ids,
        status=S.CALCULATED,
        severity=severity,
        observed=got.value,
        observation=rule.definition().template.format(
            metric=rule.metric,
            observed=got.value,
            unit=rule.unit.value,
            period=p.label,
            severity=severity.value,
            threshold=threshold,
        ),
    )


def _negative_fcf(e: Engine, latest: date | None) -> EarningsQualityCheck:
    code = "REPEATED_NEGATIVE_FCF"
    common: dict[str, Any] = {
        "check_id": uuid5(NS, f"{e.analytics_key}|{e.frame.basis.value}|check|{code}"),
        "rule_id": f"EARNINGS_QUALITY/{code}/2",
        "code": code,
        "reporting_basis": e.frame.basis,
        "unit": AnalyticsUnit.COUNT,
    }
    if latest is None:
        return EarningsQualityCheck(
            **common,
            period=None,
            status=S.INSUFFICIENT_HISTORY,
            severity=None,
            observed=None,
            observation="no fiscal year available",
            metric_ids=(),
            canonical_value_ids=(),
        )
    years = [e.periods.fy(shift(latest, -12 * k)) for k in range(3)]
    gots = [e.got("FCF", y) for y in years]
    ids: dict[str, Any] = {
        "metric_ids": metric_ids(*gots),
        "canonical_value_ids": canonical_ids(*gots),
    }
    missing = [g for g in gots if isinstance(g, Missing)]
    if missing:
        return EarningsQualityCheck(
            **common,
            **ids,
            period=years[0],
            status=missing[0].status,
            severity=None,
            observed=None,
            observation=f"FCF unavailable for {len(missing)} of the last "
            f"3 fiscal years: {missing[0].what}",
        )
    negative = sum(1 for g in gots if isinstance(g, Fact) and g.value < 0)
    low, medium, high = REPEATED_NEGATIVE_FCF
    severity = (
        Severity.HIGH
        if negative >= high
        else Severity.MEDIUM
        if negative >= medium
        else Severity.LOW
        if negative >= low
        else Severity.INFO
    )
    return EarningsQualityCheck(
        **common,
        **ids,
        period=years[0],
        status=S.CALCULATED,
        severity=severity,
        observed=Decimal(negative),
        observation=f"free cash flow was negative in {negative} of the last 3 fiscal years "
        f"({severity.value}). A screening signal that may require review.",
    )


# --- forensic models -------------------------------------------------------------------------


def _component(
    name: str,
    formula: str,
    parts: Sequence[tuple[str, Got]],
    fn: Callable[[dict[str, Decimal]], Decimal | bool | None],
) -> ForensicComponent:
    gots = [g for _, g in parts]
    ids: dict[str, Any] = {
        "metric_ids": metric_ids(*gots),
        "canonical_value_ids": canonical_ids(*gots),
    }
    missing = [f"{role}: {g.what}" for role, g in parts if isinstance(g, Missing)]
    if missing:
        return ForensicComponent(
            name=name,
            status=ComponentStatus.UNAVAILABLE,
            formula=formula,
            missing_inputs=tuple(missing),
            **ids,
        )
    values = {role: g.value for role, g in parts if isinstance(g, Fact)}
    shown = tuple(sorted(values.items()))
    result = fn(values)
    if result is None:
        return ForensicComponent(
            name=name,
            status=ComponentStatus.UNAVAILABLE,
            formula=formula,
            input_values=shown,
            missing_inputs=("a denominator is zero or negative",),
            **ids,
        )
    if isinstance(result, bool):
        return ForensicComponent(
            name=name,
            formula=formula,
            input_values=shown,
            **ids,
            status=ComponentStatus.PASS if result else ComponentStatus.FAIL,
        )
    return ForensicComponent(
        name=name,
        formula=formula,
        value=quantize(result),
        input_values=shown,
        status=ComponentStatus.AVAILABLE,
        **ids,
    )


def _div(a: Decimal, b: Decimal) -> Decimal | None:
    return None if b <= 0 else a / b


def _result(
    e: Engine,
    model: ForensicModel,
    variant: str,
    formula: str,
    p: FinancialPeriod | None,
    pp: FinancialPeriod | None,
    components: Sequence[ForensicComponent],
    *,
    score: Decimal | None,
    status: CalculationStatus,
    applicability: str,
    zone: str | None = None,
    warnings: Sequence[str] = (),
    pass_fail: bool = False,
) -> ForensicModelResult:
    available = sum(c.status != ComponentStatus.UNAVAILABLE for c in components)
    passed = sum(c.status == ComponentStatus.PASS for c in components) if pass_fail else None
    total = len(components)
    label = (
        f"{passed} / {available} available"
        if pass_fail and available < total
        else f"{passed} / {total}"
        if pass_fail
        else f"{available} / {total} components available"
    )
    return ForensicModelResult(
        result_id=uuid5(
            NS, f"{e.analytics_key}|{e.frame.basis.value}|model|{model.value}|{variant}"
        ),
        model=model,
        variant=variant,
        formula=formula,
        status=status,
        reporting_basis=e.frame.basis,
        period=p,
        comparison_period=pp,
        components=tuple(components),
        components_total=total,
        components_available=available,
        components_passed=passed,
        score_numerator=passed,
        score_denominator=available if pass_fail else None,
        availability_label=label,
        coverage=quantize(Decimal(available) / total),
        score=score,
        zone=zone,
        applicability=applicability,
        warnings=tuple(sorted(set(warnings))),
    )


def _unavailable(
    e: Engine,
    model: ForensicModel,
    variant: str,
    formula: str,
    names: Sequence[str],
    status: CalculationStatus,
    why: str,
    pass_fail: bool = False,
) -> ForensicModelResult:
    components = [
        ForensicComponent(
            name=n,
            status=ComponentStatus.UNAVAILABLE,
            formula="not evaluated",
            missing_inputs=(why,),
        )
        for n in names
    ]
    return _result(
        e,
        model,
        variant,
        formula,
        None,
        None,
        components,
        score=None,
        status=status,
        applicability=why,
        pass_fail=pass_fail,
    )


PIOTROSKI_NAMES = (
    "F1_ROA_POSITIVE",
    "F2_CFO_POSITIVE",
    "F3_ROA_IMPROVED",
    "F4_CFO_EXCEEDS_PAT",
    "F5_LEVERAGE_DECREASED",
    "F6_CURRENT_RATIO_IMPROVED",
    "F7_NO_EQUITY_ISSUANCE",
    "F8_GROSS_MARGIN_IMPROVED",
    "F9_ASSET_TURNOVER_IMPROVED",
)
PIOTROSKI_FORMULA = (
    "F-score = passed signals out of 9 (Piotroski 2000); a score is reported only when all 9 "
    "signals are available; otherwise 'passed / available' is shown, never 'passed / 9'"
)
SKIPPED_FOR_FINANCIALS = "designed for non-financial companies; not applied to {}"


def piotroski(e: Engine, latest: date | None) -> ForensicModelResult:
    if e.financial:
        return _unavailable(
            e,
            ForensicModel.PIOTROSKI_F_SCORE,
            "F9_STANDARD",
            PIOTROSKI_FORMULA,
            PIOTROSKI_NAMES,
            S.UNSUPPORTED_SECTOR,
            SKIPPED_FOR_FINANCIALS.format(e.industry.value),
            pass_fail=True,
        )
    if latest is None:
        return _unavailable(
            e,
            ForensicModel.PIOTROSKI_F_SCORE,
            "F9_STANDARD",
            PIOTROSKI_FORMULA,
            PIOTROSKI_NAMES,
            S.INSUFFICIENT_HISTORY,
            "no fiscal year available",
            pass_fail=True,
        )
    P = e.periods
    p, pp = P.fy(latest), P.fy(shift(latest, -12))
    b, bp = P.pit(latest), P.pit(shift(latest, -12))
    d = e.got

    def higher(v: dict[str, Decimal]) -> bool:
        return v["now"] > v["before"]

    def lower(v: dict[str, Decimal]) -> bool:
        return v["now"] < v["before"] or v["now"] == v["before"] == 0

    components = [
        _component("F1_ROA_POSITIVE", "ROA > 0", [("roa", d("ROA", p))], lambda v: v["roa"] > 0),
        _component(
            "F2_CFO_POSITIVE", "CFO > 0", [("cfo", e.canonical([M.CFO], p))], lambda v: v["cfo"] > 0
        ),
        _component(
            "F3_ROA_IMPROVED",
            "ROA_t > ROA_t-1",
            [("now", d("ROA", p)), ("before", d("ROA", pp))],
            higher,
        ),
        _component(
            "F4_CFO_EXCEEDS_PAT",
            "CFO > PAT (total accruals < 0)",
            [("accruals", d("TOTAL_ACCRUALS", p))],
            lambda v: v["accruals"] < 0,
        ),
        _component(
            "F5_LEVERAGE_DECREASED",
            "gross debt / total assets lower than a year earlier (proxy for long-term "
            "debt / average assets)",
            [("now", d("DEBT_TO_ASSETS", p)), ("before", d("DEBT_TO_ASSETS", pp))],
            lower,
        ),
        _component(
            "F6_CURRENT_RATIO_IMPROVED",
            "current ratio_t > current ratio_t-1",
            [("now", d("CURRENT_RATIO", p)), ("before", d("CURRENT_RATIO", pp))],
            higher,
        ),
        _component(
            "F7_NO_EQUITY_ISSUANCE",
            "equity share capital not higher than a year earlier (proxy for shares outstanding)",
            [
                ("now", e.canonical([M.EQUITY_SHARE_CAPITAL], b)),
                ("before", e.canonical([M.EQUITY_SHARE_CAPITAL], bp)),
            ],
            lambda v: v["now"] <= v["before"],
        ),
        _component(
            "F8_GROSS_MARGIN_IMPROVED",
            "gross margin_t > gross margin_t-1",
            [("now", d("GROSS_MARGIN", p)), ("before", d("GROSS_MARGIN", pp))],
            higher,
        ),
        _component(
            "F9_ASSET_TURNOVER_IMPROVED",
            "asset turnover_t > asset turnover_t-1",
            [("now", d("ASSET_TURNOVER", p)), ("before", d("ASSET_TURNOVER", pp))],
            higher,
        ),
    ]
    passed = sum(c.status == ComponentStatus.PASS for c in components)
    complete = all(c.status != ComponentStatus.UNAVAILABLE for c in components)
    zone = None
    if complete:
        zone = "HIGH_F_SCORE" if passed >= 8 else "LOW_F_SCORE" if passed <= 2 else "MID_F_SCORE"
    return _result(
        e,
        ForensicModel.PIOTROSKI_F_SCORE,
        "F9_STANDARD",
        PIOTROSKI_FORMULA,
        p,
        pp,
        components,
        score=Decimal(passed) if complete else None,
        status=S.CALCULATED if complete else S.INPUT_MISSING,
        applicability="non-financial companies; F5 and F7 use documented proxies",
        zone=zone,
        warnings=["F5_GROSS_DEBT_PROXY", "F7_SHARE_CAPITAL_PROXY"],
        pass_fail=True,
    )


ALTMAN_ORIGINAL = (
    "Z = 1.2 X1 + 1.4 X2 + 3.3 X3 + 0.6 X4 + 1.0 X5 (Altman 1968, public manufacturers; X4 = "
    "market value of equity / total liabilities)"
)
ALTMAN_DOUBLE_PRIME = (
    "Z'' = 6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4 (Altman 1995, non-manufacturers; X1 = working "
    "capital / TA, X2 = retained earnings / TA, X3 = EBIT / TA, X4 = book equity / total "
    "liabilities). Zones: > 2.60 safe, 1.10-2.60 grey, < 1.10 distress"
)
ORIGINAL_NAMES = ("X1", "X2", "X3", "X4_MARKET", "X5")
DOUBLE_PRIME_NAMES = ("X1", "X2", "X3", "X4_BOOK")


def altman(e: Engine, latest: date | None) -> list[ForensicModelResult]:
    variants = (
        ("ORIGINAL_1968", ALTMAN_ORIGINAL, ORIGINAL_NAMES),
        ("Z_DOUBLE_PRIME_1995", ALTMAN_DOUBLE_PRIME, DOUBLE_PRIME_NAMES),
    )
    if e.financial:
        why = SKIPPED_FOR_FINANCIALS.format(e.industry.value)
        return [
            _unavailable(e, ForensicModel.ALTMAN_Z, v, f, n, S.UNSUPPORTED_SECTOR, why)
            for v, f, n in variants
        ]
    if latest is None:
        return [
            _unavailable(
                e,
                ForensicModel.ALTMAN_Z,
                v,
                f,
                n,
                S.INSUFFICIENT_HISTORY,
                "no fiscal year available",
            )
            for v, f, n in variants
        ]
    P = e.periods
    p, b = P.fy(latest), P.pit(latest)
    g = e.canonical
    assets = g([M.TOTAL_ASSETS], b)
    x1 = _component(
        "X1",
        "net working capital / total assets",
        [("nwc", e.got("NET_WORKING_CAPITAL", b)), ("assets", assets)],
        lambda v: _div(v["nwc"], v["assets"]),
    )
    x2 = _component(
        "X2",
        "retained earnings proxy (reserves / other equity) / total assets",
        [("retained", g([M.RESERVES, M.OTHER_EQUITY], b)), ("assets", assets)],
        lambda v: _div(v["retained"], v["assets"]),
    )
    x3 = _component(
        "X3",
        "EBIT / total assets",
        [("ebit", e.got("EBIT", p)), ("assets", assets)],
        lambda v: _div(v["ebit"], v["assets"]),
    )
    x4_book = _component(
        "X4_BOOK",
        "book equity / total liabilities",
        [
            ("equity", e.got("SHAREHOLDERS_EQUITY", b)),
            ("liabilities", e.got("TOTAL_LIABILITIES", b)),
        ],
        lambda v: _div(v["equity"], v["liabilities"]),
    )
    x5 = _component(
        "X5",
        "revenue / total assets",
        [("revenue", g(REVENUE, p)), ("assets", assets)],
        lambda v: _div(v["revenue"], v["assets"]),
    )
    x4_market = ForensicComponent(
        name="X4_MARKET",
        status=ComponentStatus.UNAVAILABLE,
        formula="market value of equity / total liabilities",
        missing_inputs=(
            "market value of equity is not part of the Box 4 canonical data; Box 5 "
            "makes no market-data calls",
        ),
    )
    original = _result(
        e,
        ForensicModel.ALTMAN_Z,
        "ORIGINAL_1968",
        ALTMAN_ORIGINAL,
        p,
        None,
        [x1, x2, x3, x4_market, x5],
        score=None,
        status=S.INPUT_MISSING,
        applicability="public manufacturers; needs the market value of equity",
    )
    parts = (x1, x2, x3, x4_book)
    values = [c.value for c in parts if c.value is not None]
    score = zone = None
    if len(values) == 4:
        score = quantize(
            D("6.56") * values[0]
            + D("3.26") * values[1]
            + D("6.72") * values[2]
            + D("1.05") * values[3]
        )
        zone = (
            "SAFE_ZONE"
            if score > D("2.60")
            else "GREY_ZONE"
            if score >= D("1.10")
            else "DISTRESS_ZONE"
        )
    double = _result(
        e,
        ForensicModel.ALTMAN_Z,
        "Z_DOUBLE_PRIME_1995",
        ALTMAN_DOUBLE_PRIME,
        p,
        None,
        parts,
        score=score,
        status=S.CALCULATED if score is not None else S.INPUT_MISSING,
        applicability="non-financial, non-manufacturing variant on book values (the "
        "manufacturing split is not known from canonical data)",
        zone=zone,
        warnings=["X2_RETAINED_EARNINGS_PROXY"],
    )
    return [original, double]


BENEISH_FORMULA = (
    "M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI - 0.172 SGAI "
    "+ 4.679 TATA - 0.327 LVGI (Beneish 1999, 8 variables); screening threshold -1.78; scored "
    "only when all 8 indices are available; never a conclusion about manipulation"
)
BENEISH_WEIGHTS = {
    "DSRI": D("0.920"),
    "GMI": D("0.528"),
    "AQI": D("0.404"),
    "SGI": D("0.892"),
    "DEPI": D("0.115"),
    "SGAI": D("-0.172"),
    "TATA": D("4.679"),
    "LVGI": D("-0.327"),
}
BENEISH_THRESHOLD = D("-1.78")


def beneish(e: Engine, latest: date | None) -> ForensicModelResult:
    names = tuple(BENEISH_WEIGHTS)
    if e.financial:
        return _unavailable(
            e,
            ForensicModel.BENEISH_M,
            "EIGHT_VARIABLE",
            BENEISH_FORMULA,
            names,
            S.UNSUPPORTED_SECTOR,
            SKIPPED_FOR_FINANCIALS.format(e.industry.value),
        )
    if latest is None:
        return _unavailable(
            e,
            ForensicModel.BENEISH_M,
            "EIGHT_VARIABLE",
            BENEISH_FORMULA,
            names,
            S.INSUFFICIENT_HISTORY,
            "no fiscal year available",
        )
    P = e.periods
    p, pp = P.fy(latest), P.fy(shift(latest, -12))
    b, bp = P.pit(latest), P.pit(shift(latest, -12))
    g = e.canonical

    def both(
        metrics: tuple[CanonicalMetric, ...], now: FinancialPeriod, before: FinancialPeriod
    ) -> list[Got]:
        first = g(metrics, now)
        same = (first.inputs[0].metric,) if isinstance(first, Fact) else metrics
        return [first, g(same, before)]

    rec, rec0 = both((M.TRADE_RECEIVABLES,), b, bp)
    sales, sales0 = both(REVENUE, p, pp)
    cogs, cogs0 = e.got("COGS", p), e.got("COGS", pp)
    ca, ca0 = both((M.TOTAL_CURRENT_ASSETS,), b, bp)
    ppe, ppe0 = both(FIXED, b, bp)
    sec, sec0 = both((M.NON_CURRENT_INVESTMENTS,), b, bp)
    ta, ta0 = both((M.TOTAL_ASSETS,), b, bp)
    dep, dep0 = both((M.DEPRECIATION_AMORTIZATION,), p, pp)
    sell, sell0 = both((M.SELLING_EXPENSE,), p, pp)
    adm, adm0 = both((M.ADMINISTRATIVE_EXPENSE,), p, pp)
    cl, cl0 = both((M.TOTAL_CURRENT_LIABILITIES,), b, bp)
    ltd, ltd0 = both((M.LONG_TERM_BORROWINGS,), b, bp)

    def idx(a: Decimal | None, z: Decimal | None) -> Decimal | None:
        return None if a is None or z is None or z == 0 else a / z

    def quality(v: dict[str, Decimal], s: str) -> Decimal | None:
        if v[f"ta{s}"] <= 0:
            return None
        return 1 - (v[f"ca{s}"] + v[f"ppe{s}"] + v[f"sec{s}"]) / v[f"ta{s}"]

    components = [
        _component(
            "DSRI",
            "(receivables/sales)_t / (receivables/sales)_t-1",
            [("rec", rec), ("sales", sales), ("rec0", rec0), ("sales0", sales0)],
            lambda v: idx(_div(v["rec"], v["sales"]), _div(v["rec0"], v["sales0"])),
        ),
        _component(
            "GMI",
            "gross margin_t-1 / gross margin_t",
            [("sales", sales), ("cogs", cogs), ("sales0", sales0), ("cogs0", cogs0)],
            lambda v: idx(
                _div(v["sales0"] - v["cogs0"], v["sales0"]),
                _div(v["sales"] - v["cogs"], v["sales"]),
            ),
        ),
        _component(
            "AQI",
            "[1 - (current assets + PPE + securities)/TA]_t / same_t-1",
            [
                ("ca", ca),
                ("ppe", ppe),
                ("sec", sec),
                ("ta", ta),
                ("ca0", ca0),
                ("ppe0", ppe0),
                ("sec0", sec0),
                ("ta0", ta0),
            ],
            lambda v: idx(quality(v, ""), quality(v, "0")),
        ),
        _component(
            "SGI",
            "sales_t / sales_t-1",
            [("sales", sales), ("sales0", sales0)],
            lambda v: _div(v["sales"], v["sales0"]),
        ),
        _component(
            "DEPI",
            "[dep/(dep+PPE)]_t-1 / [dep/(dep+PPE)]_t",
            [("dep", dep), ("ppe", ppe), ("dep0", dep0), ("ppe0", ppe0)],
            lambda v: idx(
                _div(v["dep0"], v["dep0"] + v["ppe0"]), _div(v["dep"], v["dep"] + v["ppe"])
            ),
        ),
        _component(
            "SGAI",
            "(SG&A/sales)_t / (SG&A/sales)_t-1; SG&A = selling + administrative",
            [
                ("sell", sell),
                ("adm", adm),
                ("sales", sales),
                ("sell0", sell0),
                ("adm0", adm0),
                ("sales0", sales0),
            ],
            lambda v: idx(
                _div(v["sell"] + v["adm"], v["sales"]), _div(v["sell0"] + v["adm0"], v["sales0"])
            ),
        ),
        _component(
            "TATA",
            "(PAT - CFO) / total assets_t",
            [("pat", g([M.PAT], p)), ("cfo", g([M.CFO], p)), ("ta", ta)],
            lambda v: _div(v["pat"] - v["cfo"], v["ta"]),
        ),
        _component(
            "LVGI",
            "[(current liabilities + long-term debt)/TA]_t / same_t-1",
            [("cl", cl), ("ltd", ltd), ("ta", ta), ("cl0", cl0), ("ltd0", ltd0), ("ta0", ta0)],
            lambda v: idx(_div(v["cl"] + v["ltd"], v["ta"]), _div(v["cl0"] + v["ltd0"], v["ta0"])),
        ),
    ]
    complete = all(c.value is not None for c in components)
    score = zone = None
    if complete:
        total = D("-4.84") + sum(
            (BENEISH_WEIGHTS[c.name] * c.value for c in components if c.value is not None), D(0)
        )
        score = quantize(total)
        zone = (
            "ABOVE_SCREENING_THRESHOLD"
            if score > BENEISH_THRESHOLD
            else "BELOW_SCREENING_THRESHOLD"
        )
    return _result(
        e,
        ForensicModel.BENEISH_M,
        "EIGHT_VARIABLE",
        BENEISH_FORMULA,
        p,
        pp,
        components,
        score=score,
        status=S.CALCULATED if complete else S.INPUT_MISSING,
        applicability="non-financial companies; a statistical screening signal that may require "
        "review, never evidence of manipulation",
        zone=zone,
    )
