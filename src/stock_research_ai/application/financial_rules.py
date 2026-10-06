"""DETAILED_RESEARCH_V1 Box 5: the versioned rule registry (severity model).

Earnings-quality checks and anomaly rules grade one calculated metric against three
thresholds (LOW / MEDIUM / HIGH); below the LOW threshold an evaluated check is INFO and an
anomaly does not fire. A rule may carry a condition on another metric (e.g. "revenue
grew"). Trend and health rules are recorded here too. Thresholds are conventions of
RULE_REGISTRY_VERSION, not universal truths; wording stays neutral (signal, divergence,
requires review).
"""

from dataclasses import dataclass
from decimal import Decimal

from stock_research_ai.domain.financial_analytics import (
    AnalyticsUnit,
    RuleDefinition,
    RuleThreshold,
    Sector,
    Severity,
)

RULE_REGISTRY_VERSION = "analytics-rules/2"
U = AnalyticsUnit
D = Decimal
GENERAL = (Sector.GENERAL,)
ALL = (Sector.GENERAL, Sector.FINANCIAL)
FINANCIAL = (Sector.FINANCIAL,)


@dataclass(frozen=True)
class Condition:
    metric: str
    operator: str  # '>' or '<'
    value: Decimal
    meaning: str

    def holds(self, observed: Decimal) -> bool:
        return observed > self.value if self.operator == ">" else observed < self.value


@dataclass(frozen=True)
class GradedRule:
    family: str  # EARNINGS_QUALITY or ANOMALY
    code: str
    metric: str
    comparison: str  # AT_LEAST / AT_MOST / ABSOLUTE_AT_LEAST
    low: Decimal
    medium: Decimal
    high: Decimal
    unit: AnalyticsUnit
    meaning: str
    sectors: tuple[Sector, ...] = GENERAL
    condition: Condition | None = None
    frequency: str = "ANNUAL"
    version: int = 2

    @property
    def rule_id(self) -> str:
        return f"{self.family}/{self.code}/{self.version}"

    def grade(self, value: Decimal) -> Severity:
        x = abs(value) if self.comparison == "ABSOLUTE_AT_LEAST" else value
        for severity, threshold in (
            (Severity.HIGH, self.high),
            (Severity.MEDIUM, self.medium),
            (Severity.LOW, self.low),
        ):
            if (x <= threshold) if self.comparison == "AT_MOST" else (x >= threshold):
                return severity
        return Severity.INFO

    def threshold(self, severity: Severity) -> Decimal:
        return {Severity.HIGH: self.high, Severity.MEDIUM: self.medium}.get(severity, self.low)

    def definition(self) -> RuleDefinition:
        return RuleDefinition(
            rule_id=self.rule_id,
            family=self.family,  # type: ignore[arg-type]
            code=self.code,
            version=self.version,
            description=self.meaning,
            metrics=(self.metric, *((self.condition.metric,) if self.condition else ())),
            comparison=self.comparison,  # type: ignore[arg-type]
            thresholds=(
                RuleThreshold(severity=Severity.LOW, value=self.low),
                RuleThreshold(severity=Severity.MEDIUM, value=self.medium),
                RuleThreshold(severity=Severity.HIGH, value=self.high),
            ),
            unit=self.unit,
            condition=(
                f"{self.condition.metric} {self.condition.operator} {self.condition.value} "
                f"({self.condition.meaning})"
                if self.condition
                else None
            ),
            template=(
                "{metric} = {observed} {unit} for {period}; {severity} at "
                f"{'|x|' if self.comparison == 'ABSOLUTE_AT_LEAST' else 'x'} "
                f"{'<=' if self.comparison == 'AT_MOST' else '>='} {{threshold}}: "
                f"{self.meaning}. A rule-based screening signal that may require review."
            ),
            sectors=self.sectors,
        )


def _eq(
    code: str,
    metric: str,
    comparison: str,
    t: tuple[str, str, str],
    unit: AnalyticsUnit,
    meaning: str,
    sectors: tuple[Sector, ...] = GENERAL,
    condition: Condition | None = None,
) -> GradedRule:
    return GradedRule(
        "EARNINGS_QUALITY",
        code,
        metric,
        comparison,
        D(t[0]),
        D(t[1]),
        D(t[2]),
        unit,
        meaning,
        sectors,
        condition,
    )


def _an(
    code: str,
    metric: str,
    comparison: str,
    t: tuple[str, str, str],
    unit: AnalyticsUnit,
    meaning: str,
    sectors: tuple[Sector, ...] = GENERAL,
    condition: Condition | None = None,
    frequency: str = "ANNUAL",
) -> GradedRule:
    return GradedRule(
        "ANOMALY",
        code,
        metric,
        comparison,
        D(t[0]),
        D(t[1]),
        D(t[2]),
        unit,
        meaning,
        sectors,
        condition,
        frequency,
    )


PAT_GREW = Condition("PAT_GROWTH_YOY", ">", D(0), "PAT grew")
REVENUE_GREW = Condition("REVENUE_GROWTH_YOY", ">", D(0), "revenue grew")

EARNINGS_QUALITY_RULES: tuple[GradedRule, ...] = (
    _eq(
        "CFO_VS_PAT",
        "CFO_TO_PAT",
        "AT_MOST",
        ("0.9", "0.7", "0.5"),
        U.TIMES,
        "operating cash flow covers less of reported profit",
    ),
    _eq(
        "CUMULATIVE_CFO_VS_PAT",
        "CUMULATIVE_CFO_TO_PAT_3Y",
        "AT_MOST",
        ("0.9", "0.8", "0.5"),
        U.TIMES,
        "three-year operating cash flow covers less of three-year reported profit",
    ),
    _eq(
        "CFO_TO_EBITDA",
        "CFO_TO_EBITDA",
        "AT_MOST",
        ("0.7", "0.5", "0.3"),
        U.TIMES,
        "operating cash flow is low relative to EBITDA",
    ),
    _eq(
        "FCF_CONVERSION",
        "FCF_TO_PAT",
        "AT_MOST",
        ("0.5", "0.25", "0"),
        U.TIMES,
        "free cash flow converts little of reported profit",
    ),
    _eq(
        "ACCRUAL_RATIO",
        "CASH_FLOW_ACCRUAL_RATIO",
        "AT_LEAST",
        ("2.5", "5", "10"),
        U.PERCENT,
        "profit exceeds operating cash flow by a large share of assets",
    ),
    _eq(
        "RECEIVABLES_VS_REVENUE",
        "RECEIVABLES_VS_REVENUE_GROWTH",
        "AT_LEAST",
        ("5", "10", "25"),
        U.PERCENTAGE_POINTS,
        "trade receivables grew faster than revenue",
    ),
    _eq(
        "INVENTORY_VS_REVENUE",
        "INVENTORY_VS_REVENUE_GROWTH",
        "AT_LEAST",
        ("5", "10", "25"),
        U.PERCENTAGE_POINTS,
        "inventory grew faster than revenue",
    ),
    _eq(
        "OTHER_INCOME_DEPENDENCE",
        "OTHER_INCOME_TO_PBT",
        "AT_LEAST",
        ("10", "20", "35"),
        U.PERCENT,
        "a material share of pre-tax profit comes from other income",
    ),
    _eq(
        "EXCEPTIONAL_ITEM_DEPENDENCE",
        "EXCEPTIONAL_TO_PBT",
        "AT_LEAST",
        ("5", "10", "25"),
        U.PERCENT,
        "exceptional items are material relative to pre-tax profit",
        ALL,
    ),
    _eq(
        "CAPITALISED_COST_INDICATOR",
        "CWIP_TO_FIXED_ASSETS",
        "AT_LEAST",
        ("15", "25", "50"),
        U.PERCENT,
        "capital work-in-progress is large relative to fixed assets",
    ),
    _eq(
        "DEPRECIATION_RATE_SHIFT",
        "DEPRECIATION_RATE_CHANGE_YOY",
        "AT_MOST",
        ("-1", "-2", "-5"),
        U.PERCENTAGE_POINTS,
        "depreciation fell relative to fixed assets",
    ),
    _eq(
        "TAX_RATE_INSTABILITY",
        "TAX_RATE_RANGE_5Y",
        "AT_LEAST",
        ("5", "10", "20"),
        U.PERCENTAGE_POINTS,
        "the effective tax rate moved widely over five years",
        ALL,
    ),
    _eq(
        "CASH_TAX_VS_BOOK_TAX",
        "CASH_TAX_VS_BOOK_TAX_GAP",
        "ABSOLUTE_AT_LEAST",
        ("3", "5", "10"),
        U.PERCENTAGE_POINTS,
        "cash taxes paid diverge from the accounting tax charge",
    ),
    _eq(
        "WORKING_CAPITAL_DIVERGENCE",
        "RECEIVABLE_DAYS_CHANGE_YOY",
        "AT_LEAST",
        ("5", "10", "20"),
        U.DAYS,
        "profit grew while receivable days lengthened",
        condition=PAT_GREW,
    ),
    _eq(
        "PROFIT_GROWTH_WITHOUT_CASH",
        "CFO_GROWTH_YOY",
        "AT_MOST",
        ("0", "-10", "-25"),
        U.PERCENT,
        "profit grew while operating cash flow did not",
        condition=PAT_GREW,
    ),
    _eq(
        "PAT_VS_CFO_GROWTH",
        "PAT_VS_CFO_GROWTH_GAP",
        "AT_LEAST",
        ("10", "25", "50"),
        U.PERCENTAGE_POINTS,
        "reported profit grew faster than operating cash flow",
    ),
    _eq(
        "PAT_VS_EPS_DIVERGENCE",
        "PAT_VS_EPS_GROWTH_GAP",
        "AT_LEAST",
        ("2", "5", "15"),
        U.PERCENTAGE_POINTS,
        "EPS grew less than PAT (more shares, or a different EPS basis)",
        ALL,
    ),
    _eq(
        "MARGIN_EXPANSION_WITHOUT_CASH",
        "CFO_TO_PAT_CHANGE_YOY",
        "AT_MOST",
        ("-0.1", "-0.2", "-0.4"),
        U.TIMES,
        "margin expanded while cash conversion weakened",
        condition=Condition("PAT_MARGIN_CHANGE_YOY", ">", D(0), "PAT margin expanded"),
    ),
    _eq(
        "DEBT_GROWTH_DESPITE_PROFITS",
        "GROSS_DEBT_GROWTH_YOY",
        "AT_LEAST",
        ("10", "25", "50"),
        U.PERCENT,
        "gross debt rose although the company reported a profit",
        condition=Condition("PAT_MARGIN", ">", D(0), "PAT was positive"),
    ),
    _eq(
        "EQUITY_DILUTION",
        "SHARE_CAPITAL_GROWTH_YOY",
        "AT_LEAST",
        ("1", "5", "15"),
        U.PERCENT,
        "equity share capital increased (bonus issues and splits change capital without "
        "dilution; canonical data does not distinguish them)",
        ALL,
    ),
    _eq(
        "BALANCE_SHEET_COMPOSITION_SHIFT",
        "ASSET_MIX_SHIFT",
        "AT_LEAST",
        ("5", "10", "20"),
        U.PERCENTAGE_POINTS,
        "the mix of assets changed sharply in one year",
    ),
)
#: count of negative-FCF years among the last three (LOW / MEDIUM / HIGH)
REPEATED_NEGATIVE_FCF = (1, 2, 3)

ANOMALY_RULES: tuple[GradedRule, ...] = (
    _an(
        "REVENUE_UP_CFO_DOWN",
        "CFO_GROWTH_YOY",
        "AT_MOST",
        ("-5", "-15", "-30"),
        U.PERCENT,
        "revenue rose while operating cash flow fell",
        condition=REVENUE_GREW,
    ),
    _an(
        "PAT_OUTGROWING_CFO",
        "PAT_VS_CFO_GROWTH_GAP",
        "AT_LEAST",
        ("15", "25", "50"),
        U.PERCENTAGE_POINTS,
        "reported profit grew materially faster than operating cash flow",
    ),
    _an(
        "RECEIVABLE_DAYS_SPIKE",
        "RECEIVABLE_DAYS_CHANGE_YOY",
        "AT_LEAST",
        ("10", "20", "40"),
        U.DAYS,
        "receivable days rose sharply",
    ),
    _an(
        "INVENTORY_DAYS_SPIKE",
        "INVENTORY_DAYS_CHANGE_YOY",
        "AT_LEAST",
        ("10", "20", "40"),
        U.DAYS,
        "inventory days rose sharply",
    ),
    _an(
        "DEBT_UP_NEGATIVE_FCF",
        "GROSS_DEBT_GROWTH_YOY",
        "AT_LEAST",
        ("5", "20", "50"),
        U.PERCENT,
        "debt rose while free cash flow was negative",
        condition=Condition("FCF", "<", D(0), "FCF was negative"),
    ),
    _an(
        "INTEREST_COVERAGE_DETERIORATION",
        "INTEREST_COVERAGE_CHANGE_YOY",
        "AT_MOST",
        ("-20", "-35", "-50"),
        U.PERCENT,
        "interest coverage deteriorated",
    ),
    _an(
        "OTHER_INCOME_MATERIAL",
        "OTHER_INCOME_TO_PBT",
        "AT_LEAST",
        ("15", "25", "40"),
        U.PERCENT,
        "other income is material to pre-tax profit",
    ),
    _an(
        "EXCEPTIONAL_ITEMS_MATERIAL",
        "EXCEPTIONAL_TO_PBT",
        "AT_LEAST",
        ("10", "20", "35"),
        U.PERCENT,
        "exceptional items are material to pre-tax profit",
        ALL,
    ),
    _an(
        "TAX_RATE_CHANGE",
        "TAX_RATE_CHANGE_YOY",
        "ABSOLUTE_AT_LEAST",
        ("5", "10", "20"),
        U.PERCENTAGE_POINTS,
        "the effective tax rate changed sharply",
        ALL,
    ),
    _an(
        "ROCE_DOWN_DESPITE_GROWTH",
        "ROCE_CHANGE_YOY",
        "AT_MOST",
        ("-2", "-5", "-10"),
        U.PERCENTAGE_POINTS,
        "ROCE fell although revenue grew",
        condition=REVENUE_GREW,
    ),
    _an(
        "CAPEX_UP_WITHOUT_GROWTH",
        "CAPEX_GROWTH_YOY",
        "AT_LEAST",
        ("30", "60", "100"),
        U.PERCENT,
        "capex rose without corresponding revenue growth",
        condition=Condition("REVENUE_GROWTH_YOY", "<", D(5), "revenue grew less than 5 %"),
    ),
    _an(
        "EPS_DILUTION_DIVERGENCE",
        "PAT_VS_EPS_GROWTH_GAP",
        "AT_LEAST",
        ("3", "8", "15"),
        U.PERCENTAGE_POINTS,
        "EPS growth lagged PAT growth",
        ALL,
    ),
    _an(
        "EBITDA_MARGIN_SHIFT",
        "EBITDA_MARGIN_CHANGE_YOY",
        "ABSOLUTE_AT_LEAST",
        ("200", "300", "600"),
        U.BASIS_POINTS,
        "EBITDA margin moved sharply",
    ),
    _an(
        "OPERATING_MARGIN_SHIFT",
        "OPERATING_MARGIN_CHANGE_YOY",
        "ABSOLUTE_AT_LEAST",
        ("200", "300", "600"),
        U.BASIS_POINTS,
        "operating margin moved sharply",
    ),
    _an(
        "PAT_MARGIN_SHIFT",
        "PAT_MARGIN_CHANGE_YOY",
        "ABSOLUTE_AT_LEAST",
        ("200", "300", "600"),
        U.BASIS_POINTS,
        "PAT margin moved sharply",
        ALL,
    ),
    _an(
        "CFO_COLLAPSE",
        "CFO_GROWTH_YOY",
        "AT_MOST",
        ("-30", "-40", "-60"),
        U.PERCENT,
        "operating cash flow fell sharply",
    ),
    _an(
        "BALANCE_SHEET_JUMP",
        "TOTAL_ASSETS_GROWTH_YOY",
        "ABSOLUTE_AT_LEAST",
        ("25", "30", "60"),
        U.PERCENT,
        "total assets changed sharply in one year",
        ALL,
    ),
    _an(
        "DEPOSITS_JUMP",
        "DEPOSITS_GROWTH_YOY",
        "ABSOLUTE_AT_LEAST",
        ("25", "30", "60"),
        U.PERCENT,
        "deposits changed sharply in one year",
        FINANCIAL,
    ),
    _an(
        "QUARTERLY_OPERATING_MARGIN_SHIFT",
        "OPERATING_MARGIN_CHANGE_YOY",
        "ABSOLUTE_AT_LEAST",
        ("200", "300", "600"),
        U.BASIS_POINTS,
        "quarterly operating margin moved sharply versus the same quarter a year earlier",
        frequency="QUARTERLY",
    ),
)
#: |latest quarter revenue YoY - latest FY revenue YoY| in pp (LOW / MEDIUM / HIGH)
ANNUAL_QUARTERLY_DIVERGENCE = (D(10), D(15), D(30))

# --- trends --------------------------------------------------------------------------------

TREND_WINDOW = 5  # fiscal years
TREND_MIN_POINTS = 3
TREND_CONSISTENCY = D("0.6")  # share of steps that must move in the overall direction
#: (strong, mild) thresholds on the total change, by series kind
TREND_THRESHOLDS: dict[str, tuple[Decimal, Decimal]] = {
    "LEVEL": (D(5), D(1)),  # percentage points for PERCENT series
    "TIMES": (D("0.3"), D("0.1")),
    "DAYS": (D(15), D(5)),
    "MONEY": (D(30), D(10)),  # percent of |first value|
}


@dataclass(frozen=True)
class TrendRule:
    code: str
    kind: str
    higher_is_better: bool
    sectors: tuple[Sector, ...] = GENERAL

    @property
    def rule_id(self) -> str:
        return f"TREND/{self.kind}/2"


TREND_RULES: tuple[TrendRule, ...] = (
    TrendRule("REVENUE_GROWTH_YOY", "LEVEL", True, ALL),
    TrendRule("EBITDA_MARGIN", "LEVEL", True),
    TrendRule("OPERATING_MARGIN", "LEVEL", True),
    TrendRule("PAT_MARGIN", "LEVEL", True, ALL),
    TrendRule("ROE", "LEVEL", True, ALL),
    TrendRule("ROA", "LEVEL", True, ALL),
    TrendRule("ROCE", "LEVEL", True),
    TrendRule("ROIC", "LEVEL", True),
    TrendRule("DEBT_TO_EQUITY", "TIMES", False),
    TrendRule("INTEREST_COVERAGE", "MONEY", True),
    TrendRule("CFO_TO_PAT", "TIMES", True),
    TrendRule("FCF", "MONEY", True),
    TrendRule("GROSS_DEBT", "MONEY", False),
    TrendRule("RECEIVABLE_DAYS", "DAYS", False),
    TrendRule("INVENTORY_DAYS", "DAYS", False),
    TrendRule("CASH_CONVERSION_CYCLE", "DAYS", False),
    TrendRule("RECEIVABLES_TO_REVENUE", "LEVEL", False),
    TrendRule("OTHER_INCOME_TO_PBT", "LEVEL", False),
    TrendRule("CASH_FLOW_ACCRUAL_RATIO", "LEVEL", False),
    TrendRule("DEPOSITS_GROWTH_YOY", "LEVEL", True, FINANCIAL),
    TrendRule("ADVANCES_GROWTH_YOY", "LEVEL", True, FINANCIAL),
    TrendRule("FINANCING_PROFIT_MARGIN", "LEVEL", True, FINANCIAL),
)

# --- health --------------------------------------------------------------------------------

HEALTH_BANDS: dict[str, tuple[str, tuple[Decimal, Decimal, Decimal, Decimal]]] = {
    # metric, (strong, healthy, neutral, watch) lower bounds - higher is better
    "GROWTH": ("REVENUE_CAGR_3Y", (D(15), D(8), D(0), D(-5))),
    "RETURNS_GENERAL": ("ROCE", (D(20), D(15), D(10), D(5))),
    "RETURNS_FINANCIAL": ("ROA", (D("1.5"), D(1), D("0.5"), D(0))),
    "CASH_CONVERSION": ("CUMULATIVE_CFO_TO_PAT_3Y", (D(1), D("0.8"), D("0.6"), D("0.4"))),
}
NET_DEBT_TO_EBITDA_BANDS = (D(1), D("2.5"), D(4))  # healthy / neutral / watch upper bounds


def _trend_definitions() -> list[RuleDefinition]:
    out = []
    for kind, (strong, mild) in TREND_THRESHOLDS.items():
        out.append(
            RuleDefinition(
                rule_id=f"TREND/{kind}/2",
                family="TREND",
                code=kind,
                version=2,
                description=(
                    f"last {TREND_WINDOW} fiscal years, >= {TREND_MIN_POINTS} calculated "
                    f"points; change = last - first"
                    + (" as % of |first|" if kind == "MONEY" else "")
                    + f"; strong >= {strong}, mild >= {mild}; IMPROVING/DETERIORATING need "
                    f">= {TREND_CONSISTENCY} of steps in that direction; VOLATILE when the "
                    "direction reverses at least twice with one step >= strong, or a change "
                    ">= strong follows an inconsistent path"
                ),
                metrics=tuple(r.code for r in TREND_RULES if r.kind == kind),
                comparison="CLASSIFY",
                thresholds=(
                    RuleThreshold(severity=Severity.HIGH, value=strong),
                    RuleThreshold(severity=Severity.LOW, value=mild),
                ),
                unit=None,
                template="{series}: {first} -> {last}; change {change}",
                sectors=ALL,
            )
        )
    return out


def _health_definitions() -> list[RuleDefinition]:
    out = []
    for code, (metric, bands) in HEALTH_BANDS.items():
        out.append(
            RuleDefinition(
                rule_id=f"HEALTH/{code}/2",
                family="HEALTH",
                code=code,
                version=2,
                description=f"{metric} banded STRONG >= {bands[0]}, HEALTHY >= {bands[1]}, NEUTRAL "
                f">= {bands[2]}, WATCH >= {bands[3]}, else WEAK",
                metrics=(metric,),
                comparison="BANDS",
                thresholds=tuple(
                    RuleThreshold(severity=s, value=v)
                    for s, v in zip(
                        (Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO),
                        bands,
                        strict=True,
                    )
                ),
                unit=None,
                template="{metric} = {value}",
                sectors=ALL,
            )
        )
    out.append(
        RuleDefinition(
            rule_id="HEALTH/BALANCE_SHEET/2",
            family="HEALTH",
            code="BALANCE_SHEET",
            version=2,
            description="net cash -> STRONG; else net debt / EBITDA <= 1 HEALTHY, <= 2.5 NEUTRAL, "
            "<= 4 WATCH, else WEAK",
            metrics=("NET_DEBT", "NET_DEBT_TO_EBITDA"),
            comparison="BANDS",
            thresholds=tuple(
                RuleThreshold(severity=Severity.INFO, value=v) for v in NET_DEBT_TO_EBITDA_BANDS
            ),
            unit=U.TIMES,
            template="{metric} = {value}",
            sectors=GENERAL,
        )
    )
    return out


def rule_definitions() -> tuple[RuleDefinition, ...]:
    graded = [r.definition() for r in (*EARNINGS_QUALITY_RULES, *ANOMALY_RULES)]
    special = [
        RuleDefinition(
            rule_id="EARNINGS_QUALITY/REPEATED_NEGATIVE_FCF/2",
            family="EARNINGS_QUALITY",
            code="REPEATED_NEGATIVE_FCF",
            version=2,
            description="number of negative-FCF fiscal years among the last three",
            metrics=("FCF",),
            comparison="AT_LEAST",
            thresholds=tuple(
                RuleThreshold(severity=s, value=D(v))
                for s, v in zip(
                    (Severity.LOW, Severity.MEDIUM, Severity.HIGH),
                    REPEATED_NEGATIVE_FCF,
                    strict=True,
                )
            ),
            unit=U.COUNT,
            template="free cash flow was negative in {observed} of the last 3 fiscal years",
            sectors=GENERAL,
        ),
        RuleDefinition(
            rule_id="ANOMALY/ANNUAL_QUARTERLY_DIVERGENCE/2",
            family="ANOMALY",
            code="ANNUAL_QUARTERLY_DIVERGENCE",
            version=2,
            description="latest-quarter revenue YoY (vs its own prior-year quarter) minus latest "
            "fiscal-year revenue YoY",
            metrics=("REVENUE_GROWTH_YOY",),
            comparison="ABSOLUTE_AT_LEAST",
            thresholds=tuple(
                RuleThreshold(severity=s, value=v)
                for s, v in zip(
                    (Severity.LOW, Severity.MEDIUM, Severity.HIGH),
                    ANNUAL_QUARTERLY_DIVERGENCE,
                    strict=True,
                )
            ),
            unit=U.PERCENTAGE_POINTS,
            template="quarter {q}% vs year {y}% (gap {gap} pp)",
            sectors=ALL,
        ),
    ]
    return tuple(
        sorted(
            [*graded, *special, *_trend_definitions(), *_health_definitions()],
            key=lambda r: r.rule_id,
        )
    )
