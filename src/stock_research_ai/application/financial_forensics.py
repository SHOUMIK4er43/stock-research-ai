"""DETAILED_RESEARCH_V1 Box 5: earnings-quality screens and forensic models.

Every output here is a SCREENING SIGNAL built from explicit calculations: it can say a
number deserves a closer look (WATCH / ELEVATED), never that anything improper happened.
Thresholds are constants of FORENSIC_RULES_VERSION; they are conventions, not universal
truths. Models are scored only when every component is available; an unavailable
component is reported as such and never counted as a pass, a fail or a zero.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
from stock_research_ai.domain.financial_analytics import (
    AnalyticsUnit,
    CalculationStatus,
    ComponentStatus,
    ForensicComponent,
    ForensicModel,
    ForensicModelResult,
    ForensicSignal,
    SignalStatus,
)
from stock_research_ai.domain.financial_canonical import CanonicalMetric, FinancialPeriod

M = CanonicalMetric
U = AnalyticsUnit
S = CalculationStatus
FORENSIC_RULES_VERSION = "forensic-rules/1"
D = Decimal


@dataclass(frozen=True)
class Rule:
    code: str
    metric: str  # a DerivedMetric code at the latest fiscal year
    watch: Decimal
    elevated: Decimal
    higher_is_worse: bool
    unit: AnalyticsUnit
    meaning: str
    banks: bool = False
    gate: str | None = None  # another metric that must be > 0 for the rule to apply
    gate_meaning: str = ""


RULES: tuple[Rule, ...] = (
    Rule(
        "PAT_GROWTH_VS_CFO_GROWTH",
        "PAT_VS_CFO_GROWTH_GAP",
        D(20),
        D(50),
        True,
        U.PERCENTAGE_POINTS,
        "reported profit grew faster than operating cash flow",
    ),
    Rule(
        "CUMULATIVE_PAT_VS_CFO",
        "CUMULATIVE_CFO_TO_PAT_3Y",
        D("0.8"),
        D("0.5"),
        False,
        U.TIMES,
        "three-year operating cash flow covers less of three-year reported profit",
    ),
    Rule(
        "RECEIVABLES_VS_REVENUE",
        "RECEIVABLES_VS_REVENUE_GROWTH",
        D(10),
        D(25),
        True,
        U.PERCENTAGE_POINTS,
        "trade receivables grew faster than revenue",
    ),
    Rule(
        "INVENTORY_VS_REVENUE",
        "INVENTORY_VS_REVENUE_GROWTH",
        D(10),
        D(25),
        True,
        U.PERCENTAGE_POINTS,
        "inventory grew faster than revenue",
    ),
    Rule(
        "OTHER_INCOME_DEPENDENCE",
        "OTHER_INCOME_TO_PBT",
        D(20),
        D(35),
        True,
        U.PERCENT,
        "a larger share of pre-tax profit comes from other income",
    ),
    Rule(
        "EXCEPTIONAL_ITEM_DEPENDENCE",
        "EXCEPTIONAL_TO_PBT",
        D(10),
        D(25),
        True,
        U.PERCENT,
        "exceptional items are large relative to pre-tax profit",
        banks=True,
    ),
    Rule(
        "TAX_RATE_VOLATILITY",
        "TAX_RATE_RANGE_5Y",
        D(10),
        D(20),
        True,
        U.PERCENTAGE_POINTS,
        "the effective tax rate moved widely over five years",
        banks=True,
    ),
    Rule(
        "DEPRECIATION_RATE_SHIFT",
        "DEPRECIATION_RATE_CHANGE_YOY",
        D(-2),
        D(-5),
        False,
        U.PERCENTAGE_POINTS,
        "depreciation fell relative to fixed assets (could flatter profit)",
    ),
    Rule(
        "CAPITALISATION_PROXY",
        "CWIP_TO_FIXED_ASSETS",
        D(25),
        D(50),
        True,
        U.PERCENT,
        "capital work-in-progress is large relative to fixed assets",
    ),
    Rule(
        "CFO_PAT_DETERIORATION",
        "CFO_TO_PAT_CHANGE_YOY",
        D("-0.3"),
        D("-0.6"),
        False,
        U.TIMES,
        "cash conversion of profit weakened from the prior year",
    ),
    Rule(
        "MARGIN_EXPANSION_WITHOUT_CASH",
        "CFO_TO_PAT_CHANGE_YOY",
        D("-0.2"),
        D("-0.4"),
        False,
        U.TIMES,
        "profit margin expanded while cash conversion weakened",
        gate="PAT_MARGIN_CHANGE_YOY",
        gate_meaning="PAT margin expanded",
    ),
    Rule(
        "PROFIT_GROWTH_WITH_WC_DETERIORATION",
        "RECEIVABLE_DAYS_CHANGE_YOY",
        D(10),
        D(20),
        True,
        U.DAYS,
        "profit grew while receivable days lengthened",
        gate="PAT_GROWTH_YOY",
        gate_meaning="PAT grew",
    ),
    Rule(
        "DEBT_GROWTH_DESPITE_PROFITS",
        "GROSS_DEBT_GROWTH_YOY",
        D(25),
        D(50),
        True,
        U.PERCENT,
        "gross debt rose although the company reported a profit",
        gate="PAT_MARGIN",
        gate_meaning="PAT was positive",
    ),
    Rule(
        "EBITDA_PAT_CFO_DIVERGENCE",
        "CFO_TO_EBITDA",
        D("0.5"),
        D("0.3"),
        False,
        U.TIMES,
        "operating cash flow is low relative to EBITDA",
    ),
    Rule(
        "EQUITY_DILUTION",
        "SHARE_CAPITAL_GROWTH_YOY",
        D(5),
        D(15),
        True,
        U.PERCENT,
        "equity share capital increased (bonus issues and splits change capital without "
        "dilution; the canonical data does not distinguish them)",
        banks=True,
    ),
    Rule(
        "BALANCE_SHEET_COMPOSITION_SHIFT",
        "ASSET_MIX_SHIFT",
        D(10),
        D(20),
        True,
        U.PERCENTAGE_POINTS,
        "the mix of assets changed sharply in one year",
    ),
    Rule(
        "ACCRUAL_INTENSITY",
        "ACCRUAL_RATIO",
        D(5),
        D(10),
        True,
        U.PERCENT,
        "profit exceeds operating cash flow by a large share of assets",
    ),
)
NEGATIVE_FCF_WATCH, NEGATIVE_FCF_ELEVATED = 2, 3  # negative FCF years out of the last 3


def _signal_id(e: Engine, code: str) -> UUID:
    return uuid5(NS, f"{e.analytics_key}|{e.frame.basis.value}|forensic|{code}")


def _canonical_ids(*gots: Got) -> tuple[UUID, ...]:
    ids = {i.canonical_value_id for g in gots for i in g.inputs}
    return tuple(sorted(ids, key=str))


def _metric_ids(*gots: Got) -> tuple[UUID, ...]:
    return tuple(sorted({d for g in gots for d in g.derived_from}, key=str))


def _grade(value: Decimal, rule: Rule) -> SignalStatus:
    if rule.higher_is_worse:
        if value >= rule.elevated:
            return SignalStatus.ELEVATED
        return SignalStatus.WATCH if value >= rule.watch else SignalStatus.CLEAR
    if value <= rule.elevated:
        return SignalStatus.ELEVATED
    return SignalStatus.WATCH if value <= rule.watch else SignalStatus.CLEAR


def forensic_signals(e: Engine, latest: date | None) -> list[ForensicSignal]:
    rules = [r for r in RULES if r.banks or not e.financial]
    signals: list[ForensicSignal] = []
    p = e.periods.fy(latest) if latest else None
    for rule in rules:
        signals.append(_evaluate(e, rule, p))
    if not e.financial:
        signals.append(_negative_fcf(e, latest))
    return signals


def _evaluate(e: Engine, rule: Rule, p: FinancialPeriod | None) -> ForensicSignal:
    common: dict[str, Any] = {
        "signal_id": _signal_id(e, rule.code),
        "code": rule.code,
        "reporting_basis": e.frame.basis,
        "period": p,
        "watch_threshold": rule.watch,
        "elevated_threshold": rule.elevated,
        "unit": rule.unit,
        "rule_id": f"{FORENSIC_RULES_VERSION}/{rule.code}",
    }
    if p is None:
        return ForensicSignal(
            **common,
            status=SignalStatus.INSUFFICIENT_DATA,
            observed=None,
            explanation="no fiscal year available",
            metric_ids=(),
            canonical_value_ids=(),
        )
    got = e.got(rule.metric, p)
    gate = e.got(rule.gate, p) if rule.gate else None
    gots = [x for x in (got, gate) if x is not None]
    ids: dict[str, Any] = {
        "metric_ids": _metric_ids(*gots),
        "canonical_value_ids": _canonical_ids(*gots),
    }
    if isinstance(got, Missing):
        return ForensicSignal(
            **common,
            **ids,
            status=SignalStatus.INSUFFICIENT_DATA,
            observed=None,
            explanation=f"{rule.metric} unavailable: {got.what}",
        )
    if isinstance(gate, Missing):
        return ForensicSignal(
            **common,
            **ids,
            status=SignalStatus.INSUFFICIENT_DATA,
            observed=got.value,
            explanation=f"condition '{rule.gate_meaning}' unavailable: {gate.what}",
        )
    if isinstance(gate, Fact) and gate.value <= 0:
        return ForensicSignal(
            **common,
            **ids,
            status=SignalStatus.CLEAR,
            observed=got.value,
            explanation=f"not applicable this year: {rule.gate} = {gate.value} "
            f"(rule applies only when {rule.gate_meaning})",
        )
    status = _grade(got.value, rule)
    direction = ">=" if rule.higher_is_worse else "<="
    return ForensicSignal(
        **common,
        **ids,
        status=status,
        observed=got.value,
        explanation=(
            f"{rule.metric} = {got.value} {rule.unit.value} for {p.label}; WATCH when "
            f"{direction} {rule.watch}, ELEVATED when {direction} {rule.elevated}. Screening "
            f"signal: {rule.meaning}."
        ),
    )


def _negative_fcf(e: Engine, latest: date | None) -> ForensicSignal:
    code = "REPEATED_NEGATIVE_FCF"
    common: dict[str, Any] = {
        "signal_id": _signal_id(e, code),
        "code": code,
        "reporting_basis": e.frame.basis,
        "watch_threshold": Decimal(NEGATIVE_FCF_WATCH),
        "elevated_threshold": Decimal(NEGATIVE_FCF_ELEVATED),
        "unit": U.TIMES,
        "rule_id": f"{FORENSIC_RULES_VERSION}/{code}",
    }
    if latest is None:
        return ForensicSignal(
            **common,
            period=None,
            status=SignalStatus.INSUFFICIENT_DATA,
            observed=None,
            explanation="no fiscal year available",
            metric_ids=(),
            canonical_value_ids=(),
        )
    years = [e.periods.fy(shift(latest, -12 * k)) for k in range(3)]
    gots = [e.got("FCF", y) for y in years]
    ids: dict[str, Any] = {
        "metric_ids": _metric_ids(*gots),
        "canonical_value_ids": _canonical_ids(*gots),
    }
    missing = [g for g in gots if isinstance(g, Missing)]
    if missing:
        return ForensicSignal(
            **common,
            **ids,
            period=years[0],
            status=SignalStatus.INSUFFICIENT_DATA,
            observed=None,
            explanation=f"FCF unavailable for {len(missing)} of the last 3 "
            f"fiscal years: {missing[0].what}",
        )
    negative = sum(1 for g in gots if isinstance(g, Fact) and g.value < 0)
    status = (
        SignalStatus.ELEVATED
        if negative >= NEGATIVE_FCF_ELEVATED
        else SignalStatus.WATCH
        if negative >= NEGATIVE_FCF_WATCH
        else SignalStatus.CLEAR
    )
    return ForensicSignal(
        **common,
        **ids,
        period=years[0],
        status=status,
        observed=Decimal(negative),
        explanation=f"free cash flow was negative in {negative} of the last 3 "
        "fiscal years. Screening signal, not a conclusion.",
    )


# --- models ------------------------------------------------------------------------------------


def _component(
    name: str,
    formula: str,
    parts: Sequence[tuple[str, Got]],
    fn: Callable[[dict[str, Decimal]], Decimal | bool | None],
) -> ForensicComponent:
    gots = [g for _, g in parts]
    ids: dict[str, Any] = {
        "metric_ids": _metric_ids(*gots),
        "canonical_value_ids": _canonical_ids(*gots),
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
    result = fn(values)
    if result is None:
        return ForensicComponent(
            name=name,
            status=ComponentStatus.UNAVAILABLE,
            formula=formula,
            missing_inputs=("a denominator is zero or negative",),
            **ids,
        )
    if isinstance(result, bool):
        return ForensicComponent(
            name=name,
            formula=formula,
            **ids,
            status=ComponentStatus.PASS if result else ComponentStatus.FAIL,
        )
    return ForensicComponent(
        name=name, formula=formula, value=quantize(result), status=ComponentStatus.AVAILABLE, **ids
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
    passed: int | None = None,
    zone: str | None = None,
    warnings: Sequence[str] = (),
) -> ForensicModelResult:
    available = sum(c.status != ComponentStatus.UNAVAILABLE for c in components)
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
        components_total=len(components),
        components_available=available,
        components_passed=passed,
        coverage=quantize(Decimal(available) / len(components)),
        score=score,
        zone=zone,
        applicability=applicability,
        warnings=tuple(sorted(set(warnings))),
    )


def _unsupported(
    e: Engine, model: ForensicModel, variant: str, formula: str, names: Sequence[str]
) -> ForensicModelResult:
    components = [
        ForensicComponent(
            name=n,
            status=ComponentStatus.UNAVAILABLE,
            formula="not evaluated",
            missing_inputs=(f"model not applicable to {e.industry.value}",),
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
        status=S.UNSUPPORTED_INDUSTRY,
        applicability=f"designed for non-financial companies; skipped for {e.industry.value}",
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
    "F-score = number of passed signals out of 9 (Piotroski 2000); scored only when all 9 "
    "signals are available"
)


def piotroski(e: Engine, latest: date | None) -> ForensicModelResult:
    if e.financial:
        return _unsupported(
            e, ForensicModel.PIOTROSKI_F_SCORE, "F9_STANDARD", PIOTROSKI_FORMULA, PIOTROSKI_NAMES
        )
    if latest is None:
        return _insufficient(
            e, ForensicModel.PIOTROSKI_F_SCORE, "F9_STANDARD", PIOTROSKI_FORMULA, PIOTROSKI_NAMES
        )
    P = e.periods
    p, pp = P.fy(latest), P.fy(shift(latest, -12))
    b, bp = P.pit(latest), P.pit(shift(latest, -12))
    d = e.got

    def improved(code: str, lower_is_better: bool = False) -> Callable[[dict[str, Decimal]], bool]:
        def check(v: dict[str, Decimal]) -> bool:
            if lower_is_better:
                return v["now"] < v["before"] or v["now"] == v["before"] == 0
            return v["now"] > v["before"]

        return check

    components = [
        _component("F1_ROA_POSITIVE", "ROA > 0", [("roa", d("ROA", p))], lambda v: v["roa"] > 0),
        _component(
            "F2_CFO_POSITIVE", "CFO > 0", [("cfo", e.canonical([M.CFO], p))], lambda v: v["cfo"] > 0
        ),
        _component(
            "F3_ROA_IMPROVED",
            "ROA_t > ROA_t-1",
            [("now", d("ROA", p)), ("before", d("ROA", pp))],
            improved("ROA"),
        ),
        _component(
            "F4_CFO_EXCEEDS_PAT",
            "CFO > PAT (accruals < 0)",
            [("accruals", d("TOTAL_ACCRUALS", p))],
            lambda v: v["accruals"] < 0,
        ),
        _component(
            "F5_LEVERAGE_DECREASED",
            "gross debt / total assets lower than a year earlier (gross-debt proxy for "
            "long-term debt)",
            [("now", d("DEBT_TO_ASSETS", p)), ("before", d("DEBT_TO_ASSETS", pp))],
            improved("DEBT_TO_ASSETS", lower_is_better=True),
        ),
        _component(
            "F6_CURRENT_RATIO_IMPROVED",
            "current ratio_t > current ratio_t-1",
            [("now", d("CURRENT_RATIO", p)), ("before", d("CURRENT_RATIO", pp))],
            improved("CURRENT_RATIO"),
        ),
        _component(
            "F7_NO_EQUITY_ISSUANCE",
            "equity share capital not higher than a year earlier (share-capital proxy "
            "for shares outstanding)",
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
            improved("GROSS_MARGIN"),
        ),
        _component(
            "F9_ASSET_TURNOVER_IMPROVED",
            "asset turnover_t > asset turnover_t-1",
            [("now", d("ASSET_TURNOVER", p)), ("before", d("ASSET_TURNOVER", pp))],
            improved("ASSET_TURNOVER"),
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
        status=S.CALCULATED if complete else S.INSUFFICIENT_DATA,
        applicability="non-financial companies; F5 and F7 use documented proxies",
        passed=passed,
        zone=zone,
        warnings=["F5_GROSS_DEBT_PROXY", "F7_SHARE_CAPITAL_PROXY"],
    )


def _insufficient(
    e: Engine, model: ForensicModel, variant: str, formula: str, names: Sequence[str]
) -> ForensicModelResult:
    components = [
        ForensicComponent(
            name=n,
            status=ComponentStatus.UNAVAILABLE,
            formula="not evaluated",
            missing_inputs=("no fiscal year available",),
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
        status=S.INSUFFICIENT_DATA,
        applicability="no fiscal year available",
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


def altman(e: Engine, latest: date | None) -> list[ForensicModelResult]:
    names_original = ("X1", "X2", "X3", "X4_MARKET", "X5")
    names_double = ("X1", "X2", "X3", "X4_BOOK")
    if e.financial:
        return [
            _unsupported(
                e, ForensicModel.ALTMAN_Z, "ORIGINAL_1968", ALTMAN_ORIGINAL, names_original
            ),
            _unsupported(
                e, ForensicModel.ALTMAN_Z, "Z_DOUBLE_PRIME_1995", ALTMAN_DOUBLE_PRIME, names_double
            ),
        ]
    if latest is None:
        return [
            _insufficient(
                e, ForensicModel.ALTMAN_Z, "ORIGINAL_1968", ALTMAN_ORIGINAL, names_original
            ),
            _insufficient(
                e, ForensicModel.ALTMAN_Z, "Z_DOUBLE_PRIME_1995", ALTMAN_DOUBLE_PRIME, names_double
            ),
        ]
    P = e.periods
    p, b = P.fy(latest), P.pit(latest)
    g = e.canonical
    assets = g([M.TOTAL_ASSETS], b)
    equity = e.got("SHAREHOLDERS_EQUITY", b)
    liabilities = g([M.TOTAL_LIABILITIES], b)
    if isinstance(liabilities, Missing):
        total = g([M.TOTAL_EQUITY_AND_LIABILITIES], b)
        if isinstance(total, Fact) and isinstance(equity, Fact):
            liabilities = Fact(
                total.value - equity.value,
                total.inputs + equity.inputs,
                total.confidence,
                derived_from=equity.derived_from,
            )
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
        [("equity", equity), ("liabilities", liabilities)],
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
            "market value of equity is not part of the Box 4 canonical data; "
            "Box 5 makes no market-data calls",
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
        status=S.INSUFFICIENT_DATA,
        applicability="public manufacturing companies; needs market capitalisation",
    )
    parts = (x1, x2, x3, x4_book)
    complete = all(c.value is not None for c in parts)
    score = zone = None
    values = [c.value for c in parts if c.value is not None]
    if complete and len(values) == 4:
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
            else ("DISTRESS_ZONE")
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
        status=S.CALCULATED if complete else S.INSUFFICIENT_DATA,
        applicability="non-financial, non-manufacturing variant on book values; the manufacturing "
        "split is not known from canonical data",
        zone=zone,
        warnings=["X2_RETAINED_EARNINGS_PROXY"],
    )
    return [original, double]


BENEISH_FORMULA = (
    "M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI - 0.172 SGAI "
    "+ 4.679 TATA - 0.327 LVGI (Beneish 1999, 8 variables); screening threshold -1.78; scored "
    "only when all 8 indices are available"
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
        return _unsupported(e, ForensicModel.BENEISH_M, "EIGHT_VARIABLE", BENEISH_FORMULA, names)
    if latest is None:
        return _insufficient(e, ForensicModel.BENEISH_M, "EIGHT_VARIABLE", BENEISH_FORMULA, names)
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
            lambda v: idx(
                None if v["ta"] <= 0 else 1 - (v["ca"] + v["ppe"] + v["sec"]) / v["ta"],
                None if v["ta0"] <= 0 else 1 - (v["ca0"] + v["ppe0"] + v["sec0"]) / v["ta0"],
            ),
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
            "(SG&A/sales)_t / (SG&A/sales)_t-1 with SG&A = selling + administrative",
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
            (BENEISH_WEIGHTS[c.name] * c.value for c in components if c.value is not None),
            D(0),
        )
        score = quantize(total)
        zone = (
            "ABOVE_SCREENING_THRESHOLD"
            if score > BENEISH_THRESHOLD
            else ("BELOW_SCREENING_THRESHOLD")
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
        status=S.CALCULATED if complete else S.INSUFFICIENT_DATA,
        applicability="non-financial companies; a statistical screen, not evidence of manipulation",
        zone=zone,
    )
