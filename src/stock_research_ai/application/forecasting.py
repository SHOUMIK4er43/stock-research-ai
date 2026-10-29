"""DETAILED_RESEARCH_V1 Box 10: CanonicalFinancialDataset (+ Box 8 guidance) -> ForecastDataset.

Pipeline:
  basis + currency selection (one basis, its dominant currency; never blended) ->
  contiguous 12-month fiscal-year chain (quarters / TTM / irregular years never used) ->
  actual anchors at the latest fiscal year -> derived driver candidates (registered
  historical methods, each naming its periods and Box 4 values) -> Box 8 numeric guidance
  (deterministic mapping, exact definitions only) -> explicit assumptions (override) ->
  per-scenario projection through the registered formulas -> issues and completeness.

No AI, no network, no clock: `generated_at` is injected and kept out of every ID; IDs are
uuid5 over content; every scenario is projected from immutable inputs by a pure function.
Box 10 never computes enterprise or equity value, a discount rate, a terminal value, a
target multiple, a target price or a recommendation.
"""

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from uuid import UUID, uuid5

from stock_research_ai.application.financial_analytics import (
    frame_currencies,
    select_analytical_basis,
)
from stock_research_ai.application.financial_formulas import cagr
from stock_research_ai.application.financial_metrics import (
    REVENUE,
    Fact,
    Frame,
    Missing,
    Periods,
    shift,
    weakest,
)
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.domain.financial_analytics import (
    AnalyticsLimits,
    CalculationStatus,
    MetricInput,
)
from stock_research_ai.domain.financial_business import RESTRICTED_INDUSTRIES
from stock_research_ai.domain.financial_canonical import (
    BusinessClass,
    CanonicalFinancialDataset,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    PeriodType,
    ReportingBasis,
    Scale,
)
from stock_research_ai.domain.forecasting import (
    ASSUMPTION_QUANTUM,
    CRORE,
    DRIVER_SPECS,
    FORECAST_ENGINE_VERSION,
    FORECAST_FORMULA_VERSION,
    FORECAST_POLICY_VERSION,
    MAX_ABS_FORECAST,
    MAX_ISSUES,
    AssumptionInput,
    AssumptionSource,
    AssumptionUnit,
    Box4Source,
    DerivationMethod,
    Driver,
    ForecastAssumption,
    ForecastDataset,
    ForecastFormula,
    ForecastIssue,
    ForecastLine,
    ForecastManifest,
    ForecastModel,
    ForecastRequest,
    ForecastStatus,
    ForecastValue,
    FormulaInput,
    GuidanceReference,
    HistoricalBase,
    HistoryCompleteness,
    OperatingRoute,
    Scenario,
    ScenarioCompleteness,
    Severity,
    YearToDateBase,
    driver_bounds_error,
)
from stock_research_ai.domain.management_intelligence import Confidence as GuidanceConfidence
from stock_research_ai.domain.management_intelligence import (
    Equivalence,
    GuidanceItem,
    GuidanceUnit,
    Lifecycle,
    ManagementIntelligenceDataset,
    TargetType,
)

NS = UUID("5f0c7d3e-9a41-4b6e-8c2d-1e7f3a9b0c64")
M = CanonicalMetric
L = ForecastLine
D = Driver
SC = Scenario
SRC = AssumptionSource
DM = DerivationMethod
D0 = Decimal(0)
HUNDRED = Decimal(100)
PRECISION = 34
TAX = (M.TOTAL_TAX,)
GROSS_DEBT_REPORTED = (M.TOTAL_DEBT, M.TOTAL_BORROWINGS)
CAPEX = (M.CAPEX, M.PURCHASE_OF_PPE)
EPS_METRICS = (M.BASIC_EPS, M.DILUTED_EPS, M.EPS)  # company basic EPS first
#: an explicit share count further than this factor from the count implied by the latest
#: reported EPS is flagged (shares stated in lakhs / millions instead of ones)
SHARE_COUNT_TOLERANCE = Decimal(2)
#: |PBT - (operating profit + other income - D&A - finance cost)| above this share of |PBT|
#: means the history holds items the forecast does not model (exceptional, associates)
BRIDGE_TOLERANCE = Decimal("0.005")
BRIDGE_TOLERANCE_ABS = Decimal(1)
#: unavailable-line reasons already issued at their root (no duplicate LINE_* issue)
REPORTED_ELSEWHERE = {"UPSTREAM_UNAVAILABLE", "NEGATIVE_DEBT", "OUT_OF_RANGE"}


class ForecastInputError(ValueError):
    """The request cannot be applied to this company's data (fails closed, never guessed)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


# --- formulas ---------------------------------------------------------------------------------

Fn = Callable[[dict[str, Decimal]], Decimal]
FORMULAS: dict[str, tuple[ForecastFormula, Fn]] = {}


def _formula(
    formula_id: str, line: ForecastLine, expression: str, inputs: tuple[str, ...], fn: Fn
) -> None:
    assert formula_id not in FORMULAS, formula_id
    FORMULAS[formula_id] = (
        ForecastFormula(formula_id=formula_id, line=line, expression=expression, inputs=inputs),
        fn,
    )


def _tax(v: dict[str, Decimal]) -> Decimal:
    return v["PBT"] * v["TAX_RATE"] / HUNDRED if v["PBT"] > 0 else D0


_formula(
    "REVENUE/1",
    L.REVENUE,
    "prior revenue x (1 + revenue growth / 100)",
    ("PRIOR_REVENUE", "REVENUE_GROWTH"),
    lambda v: v["PRIOR_REVENUE"] * (1 + v["REVENUE_GROWTH"] / HUNDRED),
)
_formula(
    "OPERATING_PROFIT/1",
    L.OPERATING_PROFIT,
    "revenue x operating margin / 100",
    ("REVENUE", "OPERATING_MARGIN"),
    lambda v: v["REVENUE"] * v["OPERATING_MARGIN"] / HUNDRED,
)
_formula(
    "OTHER_INCOME/1",
    L.OTHER_INCOME,
    "revenue x other income to revenue / 100",
    ("REVENUE", "OTHER_INCOME_TO_REVENUE"),
    lambda v: v["REVENUE"] * v["OTHER_INCOME_TO_REVENUE"] / HUNDRED,
)
_formula(
    "EBITDA/1/OPERATING_PROFIT_ROUTE",
    L.EBITDA,
    "operating profit + other income (as Box 5 EBITDA = PBT + finance cost + D&A)",
    ("OPERATING_PROFIT", "OTHER_INCOME"),
    lambda v: v["OPERATING_PROFIT"] + v["OTHER_INCOME"],
)
_formula(
    "EBITDA/1/REPORTED_MARGIN",
    L.EBITDA,
    "revenue x EBITDA margin / 100",
    ("REVENUE", "EBITDA_MARGIN"),
    lambda v: v["REVENUE"] * v["EBITDA_MARGIN"] / HUNDRED,
)
_formula(
    "DEPRECIATION_AMORTIZATION/1",
    L.DEPRECIATION_AMORTIZATION,
    "revenue x D&A to revenue / 100",
    ("REVENUE", "DA_TO_REVENUE"),
    lambda v: v["REVENUE"] * v["DA_TO_REVENUE"] / HUNDRED,
)
_formula(
    "EBIT/1",
    L.EBIT,
    "EBITDA - D&A",
    ("EBITDA", "DEPRECIATION_AMORTIZATION"),
    lambda v: v["EBITDA"] - v["DEPRECIATION_AMORTIZATION"],
)
_formula(
    "FINANCE_COST/1/RATE_ON_OPENING_DEBT",
    L.FINANCE_COST,
    "opening gross debt x interest rate on debt / 100",
    ("OPENING_GROSS_DEBT", "INTEREST_RATE_ON_DEBT"),
    lambda v: v["OPENING_GROSS_DEBT"] * v["INTEREST_RATE_ON_DEBT"] / HUNDRED,
)
_formula(
    "FINANCE_COST/1/RATIO_TO_REVENUE",
    L.FINANCE_COST,
    "revenue x finance cost to revenue / 100",
    ("REVENUE", "FINANCE_COST_TO_REVENUE"),
    lambda v: v["REVENUE"] * v["FINANCE_COST_TO_REVENUE"] / HUNDRED,
)
_formula(
    "PBT/1",
    L.PBT,
    "EBIT - finance cost (exceptional items and associates are not forecast)",
    ("EBIT", "FINANCE_COST"),
    lambda v: v["EBIT"] - v["FINANCE_COST"],
)
_formula(
    "TAX/1",
    L.TAX,
    "PBT x tax rate / 100 when PBT > 0, else 0 (no tax credit is assumed on a loss)",
    ("PBT", "TAX_RATE"),
    _tax,
)
_formula("PAT/1", L.PAT, "PBT - tax", ("PBT", "TAX"), lambda v: v["PBT"] - v["TAX"])
_formula(
    "MINORITY_INTEREST/1",
    L.MINORITY_INTEREST,
    "PAT x minority share of PAT / 100",
    ("PAT", "MINORITY_SHARE_OF_PAT"),
    lambda v: v["PAT"] * v["MINORITY_SHARE_OF_PAT"] / HUNDRED,
)
_formula(
    "PAT_ATTRIBUTABLE/1/LESS_MINORITY",
    L.PAT_ATTRIBUTABLE,
    "PAT - minority interest",
    ("PAT", "MINORITY_INTEREST"),
    lambda v: v["PAT"] - v["MINORITY_INTEREST"],
)
_formula(
    "PAT_ATTRIBUTABLE/1/STANDALONE",
    L.PAT_ATTRIBUTABLE,
    "PAT (a standalone entity has no minority interest)",
    ("PAT",),
    lambda v: v["PAT"],
)
_formula(
    "EPS/1",
    L.EPS,
    "attributable PAT (crore) x 10^7 / weighted shares (ones)",
    ("PAT_ATTRIBUTABLE", "SHARE_COUNT"),
    lambda v: v["PAT_ATTRIBUTABLE"] * CRORE / v["SHARE_COUNT"],
)
_formula(
    "CAPEX/1/RATIO_TO_REVENUE",
    L.CAPEX,
    "revenue x capex to revenue / 100 (capex is a positive outflow, as Box 5 |capex|)",
    ("REVENUE", "CAPEX_TO_REVENUE"),
    lambda v: v["REVENUE"] * v["CAPEX_TO_REVENUE"] / HUNDRED,
)
_formula("CAPEX/1/AMOUNT", L.CAPEX, "capex amount", ("CAPEX_AMOUNT",), lambda v: v["CAPEX_AMOUNT"])
_formula(
    "TRADE_WORKING_CAPITAL/1",
    L.TRADE_WORKING_CAPITAL,
    "revenue x trade working capital to revenue / 100",
    ("REVENUE", "WORKING_CAPITAL_TO_REVENUE"),
    lambda v: v["REVENUE"] * v["WORKING_CAPITAL_TO_REVENUE"] / HUNDRED,
)
_formula(
    "WORKING_CAPITAL_CHANGE/1",
    L.WORKING_CAPITAL_CHANGE,
    "trade working capital - prior trade working capital (increase = cash outflow)",
    ("TRADE_WORKING_CAPITAL", "PRIOR_TRADE_WORKING_CAPITAL"),
    lambda v: v["TRADE_WORKING_CAPITAL"] - v["PRIOR_TRADE_WORKING_CAPITAL"],
)
_formula(
    "GROSS_DEBT/1",
    L.GROSS_DEBT,
    "opening gross debt + debt change",
    ("OPENING_GROSS_DEBT", "DEBT_CHANGE"),
    lambda v: v["OPENING_GROSS_DEBT"] + v["DEBT_CHANGE"],
)
_formula(
    "CFO_ESTIMATE/1",
    L.CFO_ESTIMATE,
    "PAT + D&A - working capital change (other non-cash items are not modelled)",
    ("PAT", "DEPRECIATION_AMORTIZATION", "WORKING_CAPITAL_CHANGE"),
    lambda v: v["PAT"] + v["DEPRECIATION_AMORTIZATION"] - v["WORKING_CAPITAL_CHANGE"],
)
_formula(
    "FCF_ESTIMATE/1",
    L.FCF_ESTIMATE,
    "CFO estimate - capex (as Box 5 FCF = CFO - |capex|; before lease payments)",
    ("CFO_ESTIMATE", "CAPEX"),
    lambda v: v["CFO_ESTIMATE"] - v["CAPEX"],
)
# NON_BANK_FINANCIAL (lender) model: balance sheet first, every line from Box 4 facts
_formula(
    "TOTAL_ASSETS/1",
    L.TOTAL_ASSETS,
    "prior total assets x (1 + asset growth / 100)",
    ("PRIOR_TOTAL_ASSETS", "ASSET_GROWTH"),
    lambda v: v["PRIOR_TOTAL_ASSETS"] * (1 + v["ASSET_GROWTH"] / HUNDRED),
)
_formula(
    "PAT/1/ROA_ON_AVERAGE_ASSETS",
    L.PAT,
    "ROA / 100 x average(prior, current total assets) (never an industrial margin)",
    ("PRIOR_TOTAL_ASSETS", "TOTAL_ASSETS", "ROA"),
    lambda v: v["ROA"] / HUNDRED * (v["PRIOR_TOTAL_ASSETS"] + v["TOTAL_ASSETS"]) / 2,
)
_formula(
    "COMMON_EQUITY/1/EQUITY_CHANGE_TO_PAT",
    L.COMMON_EQUITY,
    "prior ordinary equity + PAT x historical (ordinary equity change / PAT) / 100 "
    "(retention, dividends, ordinary capital raised and OCI as one observed relationship; "
    "hybrid instruments are never grown with earnings)",
    ("PRIOR_COMMON_EQUITY", "PAT", "EQUITY_CHANGE_TO_PAT"),
    lambda v: v["PRIOR_COMMON_EQUITY"] + v["PAT"] * v["EQUITY_CHANGE_TO_PAT"] / HUNDRED,
)
_formula(
    "NON_EQUITY_FUNDING/1",
    L.NON_EQUITY_FUNDING,
    "total assets - ordinary equity (the funding identity: borrowings + other liabilities + "
    "any equity-classified hybrid instruments, which rank senior to ordinary equity)",
    ("TOTAL_ASSETS", "COMMON_EQUITY"),
    lambda v: v["TOTAL_ASSETS"] - v["COMMON_EQUITY"],
)
_formula(
    "TOTAL_BORROWINGS/1/SHARE_OF_FUNDING",
    L.TOTAL_BORROWINGS,
    "non-ordinary-equity funding x historical borrowings share of it / 100",
    ("NON_EQUITY_FUNDING", "BORROWINGS_SHARE_OF_FUNDING"),
    lambda v: v["NON_EQUITY_FUNDING"] * v["BORROWINGS_SHARE_OF_FUNDING"] / HUNDRED,
)
_formula(
    "FINANCE_COST/1/COST_OF_BORROWINGS_PROXY",
    L.FINANCE_COST,
    "cost-of-borrowings PROXY (finance cost / average borrowings, historical) / 100 x "
    "average(prior, current borrowings); not a contractual cost of funds",
    ("PRIOR_TOTAL_BORROWINGS", "TOTAL_BORROWINGS", "COST_OF_BORROWINGS"),
    lambda v: v["COST_OF_BORROWINGS"] / HUNDRED
    * (v["PRIOR_TOTAL_BORROWINGS"] + v["TOTAL_BORROWINGS"]) / 2,
)
_formula(
    "TOTAL_INCOME/1/INCOME_ON_AVERAGE_ASSETS",
    L.TOTAL_INCOME,
    "historical total income / average total assets / 100 x average(prior, current assets)",
    ("PRIOR_TOTAL_ASSETS", "TOTAL_ASSETS", "TOTAL_INCOME_TO_AVERAGE_ASSETS"),
    lambda v: v["TOTAL_INCOME_TO_AVERAGE_ASSETS"] / HUNDRED
    * (v["PRIOR_TOTAL_ASSETS"] + v["TOTAL_ASSETS"]) / 2,
)
def _ytd_ratio(ytd: str, driver: str) -> Fn:
    return lambda v: v[ytd] + (v["REVENUE"] - v["YTD_REVENUE"]) * v[driver] / HUNDRED


def _ytd_rate(remaining: int) -> Fn:
    return lambda v: v["YTD_FINANCE_COST"] + v["OPENING_GROSS_DEBT"] * v[
        "INTEREST_RATE_ON_DEBT"] / HUNDRED * remaining / 12


# the first forecast year when Box 4 reports some of its months: reported year-to-date
# (YTD) + a forecast of the REMAINING months only; the reported months are never re-forecast
_formula(
    "REVENUE/2/YTD_PLUS_REMAINDER",
    L.REVENUE,
    "reported year-to-date revenue + prior-year revenue of the remaining months x (1 + revenue "
    "growth / 100) (the growth applies to the remaining months only)",
    ("YTD_REVENUE", "PRIOR_YEAR_REMAINDER_REVENUE", "REVENUE_GROWTH"),
    lambda v: v["YTD_REVENUE"] + v["PRIOR_YEAR_REMAINDER_REVENUE"]
    * (1 + v["REVENUE_GROWTH"] / HUNDRED),
)
for _line, _driver in ((L.OPERATING_PROFIT, "OPERATING_MARGIN"),
                       (L.OTHER_INCOME, "OTHER_INCOME_TO_REVENUE"),
                       (L.EBITDA, "EBITDA_MARGIN"),
                       (L.DEPRECIATION_AMORTIZATION, "DA_TO_REVENUE"),
                       (L.FINANCE_COST, "FINANCE_COST_TO_REVENUE")):
    _formula(
        f"{_line.value}/2/YTD_PLUS_REMAINDER",
        _line,
        f"reported year-to-date {_line.value.lower().replace('_', ' ')} + (revenue - "
        f"year-to-date revenue) x {_driver.lower().replace('_', ' ')} / 100",
        (f"YTD_{_line.value}", "REVENUE", "YTD_REVENUE", _driver),
        _ytd_ratio(f"YTD_{_line.value}", _driver),
    )
for _remaining in (3, 6, 9):
    _formula(
        f"FINANCE_COST/2/RATE_YTD_REMAINING_{_remaining}M",
        L.FINANCE_COST,
        f"reported year-to-date finance cost + opening gross debt x interest rate on debt / 100 "
        f"x {_remaining} / 12 (the remaining months)",
        ("YTD_FINANCE_COST", "OPENING_GROSS_DEBT", "INTEREST_RATE_ON_DEBT"),
        _ytd_rate(_remaining),
    )
_formula(
    "PBT/2/YTD_PLUS_REMAINDER",
    L.PBT,
    "reported year-to-date PBT + remaining-months EBIT - remaining-months finance cost (each "
    "full year less its year-to-date; reported exceptional items stay in the actual months, "
    "none is forecast)",
    ("YTD_PBT", "EBIT", "YTD_EBIT", "FINANCE_COST", "YTD_FINANCE_COST"),
    lambda v: v["YTD_PBT"] + (v["EBIT"] - v["YTD_EBIT"])
    - (v["FINANCE_COST"] - v["YTD_FINANCE_COST"]),
)
_formula(
    "TAX/2/YTD_PLUS_REMAINDER",
    L.TAX,
    "reported year-to-date tax + remaining-months PBT x tax rate / 100 when that PBT > 0, else "
    "nothing more",
    ("YTD_TAX", "PBT", "YTD_PBT", "TAX_RATE"),
    lambda v: v["YTD_TAX"] + max(v["PBT"] - v["YTD_PBT"], D0) * v["TAX_RATE"] / HUNDRED,
)
_formula(
    "PAT/2/YTD_PLUS_REMAINDER",
    L.PAT,
    "reported year-to-date PAT + remaining-months PBT - remaining-months tax",
    ("YTD_PAT", "PBT", "YTD_PBT", "TAX", "YTD_TAX"),
    lambda v: v["YTD_PAT"] + (v["PBT"] - v["YTD_PBT"]) - (v["TAX"] - v["YTD_TAX"]),
)
#: Box 4 quarterly metrics a reported year-to-date sums, per line (TAX and EBIT are built)
YTD_METRICS: dict[ForecastLine, tuple[CanonicalMetric, ...]] = {
    L.REVENUE: tuple(REVENUE),
    L.OPERATING_PROFIT: (M.OPERATING_PROFIT,),
    L.EBITDA: (M.EBITDA,),
    L.OTHER_INCOME: (M.OTHER_INCOME,),
    L.DEPRECIATION_AMORTIZATION: (M.DEPRECIATION_AMORTIZATION,),
    L.FINANCE_COST: (M.FINANCE_COST,),
    L.PBT: (M.PBT,),
    L.PAT: (M.PAT,),
}
YTD_PERIODS = {3: (PeriodType.QUARTER, "Q1"), 6: (PeriodType.HALF_YEAR, "H1"),
               9: (PeriodType.NINE_MONTH, "9M")}

#: actual anchors built from several Box 4 values (recorded, not projected)
ACTUAL_COMPOSITES = {
    "ACTUAL_TRADE_WORKING_CAPITAL/1": (
        L.TRADE_WORKING_CAPITAL,
        "trade receivables + inventory - trade payables (all three required; none assumed 0)",
    ),
    "ACTUAL_GROSS_DEBT/1/COMPONENTS": (
        L.GROSS_DEBT,
        "long-term + short-term borrowings (as Box 5 GROSS_DEBT/COMPONENTS)",
    ),
    "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_RESERVES_LESS_HYBRID": (
        L.COMMON_EQUITY,
        "equity share capital + reserves - equity-classified hybrid instruments (as Box 5 "
        "COMMON_EQUITY: the ordinary shareholders' claim)",
    ),
    "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_RESERVES": (
        L.COMMON_EQUITY,
        "equity share capital + reserves, the filing showing no hybrid instrument at that date "
        "(as Box 5 COMMON_EQUITY)",
    ),
    "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_OTHER_EQUITY": (
        L.COMMON_EQUITY,
        "equity share capital + other equity, any hybrid line excluded (as Box 5 COMMON_EQUITY)",
    ),
    **{f"ACTUAL_YTD/1/{line.value}": (
        line, f"sum of the reported quarters of the year to date ({line.value})")
       for line in YTD_METRICS},
    "ACTUAL_YTD/1/TAX": (L.TAX, "sum of the reported quarters' tax; a quarter without a tax "
                                "line contributes its PBT - PAT (flagged IMPLIED_TAX)"),
    "ACTUAL_YTD/1/EBIT": (L.EBIT, "year-to-date operating profit + other income - D&A (or "
                                  "reported EBITDA - D&A), each the sum of its quarters"),
    "ACTUAL_PRIOR_YEAR_REMAINDER/1/REVENUE": (
        L.REVENUE, "latest fiscal-year revenue - the same months' quarterly revenue a year "
                   "before the year to date (the prior-year revenue of the remaining months)"),
}
#: Box 10 lines per forecast model (the key line decides a scenario's completeness)
LENDER_LINES = (L.TOTAL_ASSETS, L.PAT, L.COMMON_EQUITY, L.NON_EQUITY_FUNDING,
                L.TOTAL_BORROWINGS, L.FINANCE_COST, L.TOTAL_INCOME)
LENDER_ANCHORS = frozenset({L.TOTAL_ASSETS, L.COMMON_EQUITY, L.TOTAL_BORROWINGS, L.PAT,
                            L.FINANCE_COST, L.TOTAL_INCOME})
LENDER_DRIVERS = frozenset({D.ASSET_GROWTH, D.ROA, D.EQUITY_CHANGE_TO_PAT,
                            D.BORROWINGS_SHARE_OF_FUNDING, D.COST_OF_BORROWINGS,
                            D.TOTAL_INCOME_TO_AVERAGE_ASSETS})
POLICIES: tuple[tuple[str, str], ...] = (
    ("HISTORY", "only contiguous 12-month fiscal years ending at the latest fiscal year"),
    ("REVENUE_GROWTH_BASE", "CAGR over the usable window (single YoY when only two years)"),
    ("REVENUE_GROWTH_BULL_BEAR", "highest / lowest year-on-year growth inside the window"),
    ("MARGIN_BASE", "median margin of the window"),
    ("MARGIN_BULL_BEAR", "highest / lowest margin inside the window"),
    ("OTHER_DRIVERS", "median of the window, identical in every scenario"),
    ("GUIDANCE_RANGE_GROWTH", "BASE midpoint, BULL high, BEAR low (Box 8 RANGE)"),
    ("GUIDANCE_RANGE_OTHER", "midpoint in every scenario"),
    ("GUIDANCE_POINT", "the point value in every scenario"),
    ("GUIDANCE_SCOPE", "exact definition, same basis, FY target, not LOW confidence, latest"),
    ("EXPLICIT", "an explicit assumption replaces any derived or guidance value"),
    ("DEBT_CHANGE_FALLBACK", "gross debt held flat (debt change 0), flagged FALLBACK"),
    ("TAX_ON_LOSS", "no tax and no tax credit when PBT <= 0"),
    ("NON_RECURRING", "exceptional items and share of associates are not forecast"),
    ("EPS", "only with an explicit share count and a known attributable PAT"),
    ("FORECAST_MODEL", "chosen from Box 4's business classification: GENERAL -> NON_FINANCIAL; "
     "BANK -> BANK; FINANCIAL class (not a bank) -> NON_BANK_FINANCIAL; else UNKNOWN"),
    ("RESTRICTED_MODELS", "BANK and UNKNOWN: revenue only; no industrial or lender line"),
    ("LENDER_MODEL", "NON_BANK_FINANCIAL: total assets -> PAT (ROA on average assets) -> "
     "ordinary equity -> non-equity funding (assets - ordinary equity) -> borrowings (share of "
     "funding) -> finance cost "
     "(cost-of-borrowings proxy on average borrowings); total income on average assets. No "
     "EBITDA, working capital, capex, revenue or gross-debt driver"),
    ("LENDER_COMMON_EQUITY", "the equity line is ORDINARY shareholders' equity (Box 5 "
     "COMMON_EQUITY): equity-classified hybrid instruments rank senior to it, are never grown "
     "with earnings and sit in non-equity funding; ordinary equity at a date needs primary "
     "evidence of the hybrid amount (a provider's reserves cannot show it), else the equity "
     "chain is unavailable"),
    ("LENDER_GROWTH", "asset growth: CAGR (BASE), highest / lowest YoY in the window (BULL / "
     "BEAR), as revenue growth"),
    ("LENDER_PROFITABILITY", "ROA: median (BASE), highest / lowest (BULL / BEAR) in the window"),
    ("LENDER_OTHER_DRIVERS", "equity change / PAT, borrowings share of funding, cost of "
     "borrowings and income on average assets: median of the window, every scenario"),
    ("LENDER_AVERAGES", "average balances need BOTH the opening and the closing balance"),
    ("YEAR_TO_DATE", "when Box 4 reports quarters of the first forecast year (contiguous from "
     "its first quarter), that year is the reported year-to-date + a forecast of the remaining "
     "months only; later years grow from that full year. A reported year-to-date missing any "
     "modelled line is not used and is flagged YTD_ACTUALS_INCOMPLETE"),
    ("YEAR_TO_DATE_TRAJECTORY", "first forecast year, remaining months: BASE growth is the "
     "year-to-date growth over the same months a year earlier and BASE ratios are the "
     "year-to-date ratios (the current trajectory, not a stale median); BULL / BEAR keep a "
     "historical extreme only on their side of it; non-range drivers use it in every scenario"),
    ("SCENARIO_FADE", "no first-year value is held for the whole horizon: from the second "
     "forecast year the gap between the first-year value (year-to-date trajectory or a BULL / "
     "BEAR extreme) and the historical BASE closes by 1 / horizon each year"),
)


def evaluate(formula_id: str, values: dict[str, Decimal]) -> Decimal:
    """Pure: the registered formula over its named inputs at fixed working precision."""
    definition, fn = FORMULAS[formula_id]
    if set(values) != set(definition.inputs):
        raise ValueError(f"{formula_id} needs exactly {definition.inputs}")
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return +fn(values)


def quantize(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return value.quantize(ASSUMPTION_QUANTUM, rounding=ROUND_HALF_EVEN)


def median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    with localcontext() as ctx:
        ctx.prec = PRECISION
        return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


# --- history ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    period_key: str
    value: Decimal
    inputs: tuple[MetricInput, ...]
    confidence: Confidence
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    """A derived default for one driver and one scenario (constant over the horizon)."""

    value: Decimal
    method: DerivationMethod
    source: AssumptionSource
    observations: tuple[Observation, ...]
    completeness: HistoryCompleteness
    confidence: Confidence
    warnings: tuple[str, ...]


class History:
    """Box 4 values for ONE basis in ONE currency, read only at exact fiscal-year keys."""

    def __init__(
        self, data: CanonicalFinancialDataset, basis: ReportingBasis, currency: Currency
    ) -> None:
        self.frame = Frame(data, basis, currency, AnalyticsLimits().max_canonical_values)
        self.periods = Periods(data.fiscal_year_end_month)

    def flow(self, metrics: Sequence[CanonicalMetric], end: date) -> Fact | Missing:
        return self._role(self.frame.get(metrics, self.periods.fy(end)), "flow")

    def balance(self, metrics: Sequence[CanonicalMetric], end: date) -> Fact | Missing:
        return self._role(self.frame.get(metrics, self.periods.pit(end)), "balance")

    def gross_debt(self, end: date) -> Fact | Missing:
        reported = self.balance(GROSS_DEBT_REPORTED, end)
        if isinstance(reported, Fact):
            return reported
        lt, st = (
            self.balance([M.LONG_TERM_BORROWINGS], end),
            self.balance([M.SHORT_TERM_BORROWINGS], end),
        )
        if isinstance(lt, Fact) and isinstance(st, Fact):
            return Fact(
                value=lt.value + st.value,
                inputs=lt.inputs + st.inputs,
                confidence=weakest([lt.confidence, st.confidence]),
                warnings=lt.warnings + st.warnings,
                formula_id="ACTUAL_GROSS_DEBT/1/COMPONENTS",
            )
        return reported

    def common_equity(self, end: date) -> Fact | Missing:
        """ORDINARY (common) shareholders' equity - Box 5 COMMON_EQUITY's own rule: share
        capital + reserves (a provider aggregate that contains any hybrid equity) less the
        equity-classified hybrid instruments at that date, or share capital + other equity
        (its separately presented hybrid sibling excluded). Unavailable without primary evidence
        of the hybrid amount at that date; never total equity as reported (it may carry
        non-controlling interests and hybrids)."""
        capital = self.balance([M.EQUITY_SHARE_CAPITAL], end)
        if isinstance(capital, Missing):
            return capital
        hybrid = self._role(self.frame.hybrid(self.periods.pit(end)), "balance")
        if isinstance(hybrid, Missing):
            return hybrid
        reserves = self.balance([M.RESERVES], end)
        if isinstance(reserves, Fact):
            facts: tuple[Fact, ...]
            if hybrid.inputs:  # the provider's reserves contain the reported hybrid amount
                facts = (capital, reserves, hybrid)
                value = capital.value + reserves.value - hybrid.value
                formula = "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_RESERVES_LESS_HYBRID"
            else:  # a filing nil or a filing balance sheet without the line: a warning
                facts = (capital, reserves)
                value = capital.value + reserves.value
                formula = "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_RESERVES"
        else:
            other = self.balance([M.OTHER_EQUITY], end)
            if isinstance(other, Missing):
                return other
            facts, value = (capital, other), capital.value + other.value
            formula = "ACTUAL_COMMON_EQUITY/1/CAPITAL_PLUS_OTHER_EQUITY"
        return Fact(
            value=value,
            inputs=tuple(i for f in facts for i in f.inputs),
            confidence=weakest([f.confidence for f in (*facts, hybrid)]),
            warnings=tuple(w for f in (*facts, hybrid) for w in f.warnings),
            formula_id=formula,
        )

    def trade_working_capital(self, end: date) -> Fact | Missing:
        parts = [
            self.balance([M.TRADE_RECEIVABLES], end),
            self.balance([M.INVENTORY], end),
            self.balance([M.TRADE_PAYABLES], end),
        ]
        missing = [p for p in parts if isinstance(p, Missing)]
        if missing:
            return missing[0]
        facts = [p for p in parts if isinstance(p, Fact)]
        return Fact(
            value=facts[0].value + facts[1].value - facts[2].value,
            inputs=tuple(i for f in facts for i in f.inputs),
            confidence=weakest([f.confidence for f in facts]),
            warnings=tuple(w for f in facts for w in f.warnings),
            formula_id="ACTUAL_TRADE_WORKING_CAPITAL/1",
        )

    def tax(self, end: date) -> tuple[Fact | Missing, tuple[str, ...]]:
        total = self.flow(TAX, end)
        if isinstance(total, Fact):
            return total, ()
        current, deferred = self.flow([M.CURRENT_TAX], end), self.flow([M.DEFERRED_TAX], end)
        if isinstance(current, Fact) and isinstance(deferred, Fact):
            return Fact(
                value=current.value + deferred.value,
                inputs=current.inputs + deferred.inputs,
                confidence=weakest([current.confidence, deferred.confidence]),
                warnings=current.warnings + deferred.warnings,
            ), ()
        pbt, pat = self.flow([M.PBT], end), self.flow([M.PAT], end)
        if isinstance(pbt, Fact) and isinstance(pat, Fact):
            return Fact(
                value=pbt.value - pat.value,
                inputs=pbt.inputs + pat.inputs,
                confidence=weakest([pbt.confidence, pat.confidence, Confidence.MEDIUM]),
                warnings=pbt.warnings + pat.warnings,
            ), ("IMPLIED_TAX_FROM_PBT_AND_PAT",)
        return total, ()

    @staticmethod
    def _role(got: Fact | Missing, role: str) -> Fact | Missing:
        if isinstance(got, Missing):
            return got
        return Fact(
            value=got.value,
            inputs=tuple(i.model_copy(update={"role": role}) for i in got.inputs),
            confidence=got.confidence,
            warnings=got.warnings,
        )


@dataclass(frozen=True)
class Chain:
    ends: tuple[date, ...]  # contiguous 12-month fiscal-year ends, oldest first (window)
    excluded: tuple[tuple[str, str], ...]
    fatal: tuple[str, str] | None


def fiscal_chain(
    data: CanonicalFinancialDataset, frame: Frame, years: int
) -> tuple[Chain, dict[str, int]]:
    fy_periods: dict[date, set[int | None]] = {}
    ignored: Counter[str] = Counter()
    for value in frame.values.values():
        period = value.period
        if period.period_type == PeriodType.FY and period.period_end is not None:
            fy_periods.setdefault(period.period_end, set()).add(period.duration_months)
        elif period.period_type != PeriodType.POINT_IN_TIME:
            ignored[period.period_type.value] += 1
    excluded: list[tuple[str, str]] = []
    regular = {end for end, durations in fy_periods.items() if 12 in durations}
    for end, durations in sorted(fy_periods.items()):
        for duration in sorted(d or 0 for d in durations if d != 12):
            excluded.append((f"FY|{end}|{duration or None}", "IRREGULAR_FISCAL_PERIOD"))
    if not fy_periods:
        return Chain((), tuple(excluded), ("NO_ANNUAL_HISTORY", "no fiscal-year values")), dict(
            ignored
        )
    latest = max(fy_periods)
    if latest not in regular:
        return Chain(
            (),
            tuple(excluded),
            (
                "IRREGULAR_LATEST_FISCAL_PERIOD",
                f"the latest fiscal period ending {latest} is not 12 months",
            ),
        ), dict(ignored)
    chain = [latest]
    while shift(chain[-1], -12) in regular:
        chain.append(shift(chain[-1], -12))
    for end in sorted(regular - set(chain)):
        excluded.append((f"FY|{end}|12", "NOT_CONTIGUOUS"))
    window = tuple(sorted(chain)[-years:])
    return Chain(window, tuple(excluded), None), dict(ignored)


# --- derivations ------------------------------------------------------------------------------


class Issues:
    def __init__(self) -> None:
        self.items: list[ForecastIssue] = []
        self.truncated = False

    def add(
        self,
        code: str,
        severity: Severity,
        detail: str | None = None,
        *,
        scenario: Scenario | None = None,
        fiscal_year: int | None = None,
        subject: str | None = None,
    ) -> None:
        if len(self.items) >= MAX_ISSUES - 1:
            self.truncated = True
            return
        self.items.append(
            ForecastIssue(
                code=code,
                severity=severity,
                scenario=scenario,
                fiscal_year=fiscal_year,
                subject=subject,
                detail=detail[:1000] if detail else None,
            )
        )


def _completeness(count: int, expected: int) -> HistoryCompleteness:
    if count <= 1:
        return HistoryCompleteness.SINGLE_POINT
    return HistoryCompleteness.FULL if count >= expected else HistoryCompleteness.SPARSE


def _confidence(
    observations: Iterable[Observation], completeness: HistoryCompleteness
) -> Confidence:
    cap = {
        HistoryCompleteness.FULL: Confidence.HIGH,
        HistoryCompleteness.SPARSE: Confidence.MEDIUM,
        HistoryCompleteness.SINGLE_POINT: Confidence.LOW,
        HistoryCompleteness.NOT_APPLICABLE: Confidence.HIGH,
    }[completeness]
    return weakest([*(o.confidence for o in observations), cap])


def _warnings(observations: Iterable[Observation], *extra: str) -> tuple[str, ...]:
    found = {w for o in observations for w in o.warnings} | set(extra)
    return tuple(sorted(found))


class Deriver:
    def __init__(
        self, history: History, ends: tuple[date, ...], window: int, issues: Issues
    ) -> None:
        self.h = history
        self.ends = ends
        self.window = window  # requested fiscal years: fewer usable ones is SPARSE
        self.issues = issues

    def ratio_series(
        self,
        driver: Driver,
        numerator: Callable[[date], Fact | Missing],
        denominator: Callable[[date], Fact | Missing],
        *,
        what: str,
        absolute: bool = False,
        extra_warnings: Callable[[date], tuple[str, ...]] | None = None,
    ) -> list[Observation]:
        out: list[Observation] = []
        for end in self.ends:
            num, den = numerator(end), denominator(end)
            label = self.h.periods.fy(end).key()
            if isinstance(num, Missing) or isinstance(den, Missing):
                gap = num if isinstance(num, Missing) else den
                assert isinstance(gap, Missing)
                self.issues.add(
                    "HISTORY_POINT_UNAVAILABLE",
                    Severity.INFO,
                    f"{label}: {gap.status.value} {gap.what}",
                    subject=driver.value,
                )
                continue
            if den.value <= 0:
                code = "ZERO_DENOMINATOR" if den.value == 0 else "NEGATIVE_DENOMINATOR"
                self.issues.add(
                    code, Severity.INFO, f"{label}: {what} is {den.value}", subject=driver.value
                )
                continue
            with localcontext() as ctx:
                ctx.prec = PRECISION
                numerator_value = abs(num.value) if absolute else num.value
                value = numerator_value * HUNDRED / den.value
            out.append(
                Observation(
                    label,
                    value,
                    num.inputs + den.inputs,
                    weakest([num.confidence, den.confidence]),
                    num.warnings + den.warnings + (extra_warnings(end) if extra_warnings else ()),
                )
            )
        return out

    def revenue(self, end: date) -> Fact | Missing:
        return self.h.flow(REVENUE, end)

    def growth(
        self,
        driver: Driver = D.REVENUE_GROWTH,
        values: Callable[[date], Fact | Missing] | None = None,
        what: str = "revenue",
    ) -> dict[Scenario, Candidate] | None:
        get = values or self.revenue
        points = [(end, get(end)) for end in self.ends]
        facts = [(end, got) for end, got in points if isinstance(got, Fact)]
        latest_end = self.ends[-1]
        yoy: list[Observation] = []
        for (prev_end, prev), (end, cur) in zip(points, points[1:], strict=False):
            if not isinstance(prev, Fact) or not isinstance(cur, Fact):
                continue
            label = self.h.periods.fy(end).key()
            if prev.value <= 0:
                self.issues.add(
                    "NEGATIVE_DENOMINATOR" if prev.value < 0 else "ZERO_DENOMINATOR",
                    Severity.INFO,
                    f"{label}: prior {what} {prev.value} (FY ending {prev_end})",
                    subject=driver.value,
                )
                continue
            with localcontext() as ctx:
                ctx.prec = PRECISION
                value = (cur.value / prev.value - 1) * HUNDRED
            yoy.append(
                Observation(
                    label,
                    value,
                    prev.inputs + cur.inputs,
                    weakest([prev.confidence, cur.confidence]),
                    prev.warnings + cur.warnings,
                )
            )
        latest = next((got for end, got in facts if end == latest_end), None)
        start = next(
            ((end, got) for end, got in facts if end != latest_end and got.value > 0), None
        )
        expected = self.window - 1
        if latest is None or start is None:
            self.issues.add(
                "INSUFFICIENT_HISTORY",
                Severity.WARNING,
                f"{what} growth needs {what} for two contiguous fiscal years",
                subject=driver.value,
            )
            return None
        start_end, start_fact = start
        years = (
            latest_end.year * 12 + latest_end.month - start_end.year * 12 - start_end.month
        ) // 12
        result = cagr(latest.value, start_fact.value, years)
        if not isinstance(result, Decimal):
            self.issues.add(
                "HISTORY_NOT_APPLICABLE",
                Severity.WARNING,
                f"{what} CAGR refused: {result.status.value} {result.interpretation}",
                subject=driver.value,
            )
            return None
        label_start, label_end = (
            self.h.periods.fy(start_end).key(),
            self.h.periods.fy(latest_end).key(),
        )
        endpoints = (
            Observation(
                label_start,
                start_fact.value,
                start_fact.inputs,
                start_fact.confidence,
                start_fact.warnings,
            ),
            Observation(label_end, latest.value, latest.inputs, latest.confidence, latest.warnings),
        )
        completeness = _completeness(years, expected)
        sparse = () if completeness == HistoryCompleteness.FULL else ("SPARSE_HISTORY",)
        method = DM.HISTORICAL_CAGR if years > 1 else DM.HISTORICAL_SINGLE_YOY
        problem = driver_bounds_error(driver, quantize(result))
        if problem:
            self.issues.add(
                "HISTORY_OUT_OF_BOUNDS",
                Severity.WARNING,
                problem,
                subject=driver.value,
            )
            return None
        base = Candidate(
            quantize(result),
            method,
            SRC.HISTORICAL,
            endpoints,
            completeness,
            _confidence(endpoints, completeness),
            _warnings(endpoints, *sparse),
        )
        if len(yoy) < 2:
            flat = base.warnings + ("NO_HISTORICAL_DISPERSION",)
            same = Candidate(
                base.value,
                base.method,
                base.source,
                base.observations,
                base.completeness,
                base.confidence,
                tuple(sorted(set(flat))),
            )
            return {SC.BASE: base, SC.BULL: same, SC.BEAR: same}
        # a year without revenue inside the CAGR span can put the CAGR outside the observed
        # year-on-year range: the scenario then keeps the base value, never an inverted one
        return self._scenarios(
            driver,
            base,
            yoy,
            (DM.HISTORICAL_MAX_YOY, DM.HISTORICAL_MIN_YOY),
            _completeness(len(yoy), expected),
        )

    def _scenarios(
        self,
        driver: Driver,
        base: Candidate,
        observations: Sequence[Observation],
        methods: tuple[DerivationMethod, DerivationMethod],
        completeness: HistoryCompleteness,
    ) -> dict[Scenario, Candidate]:
        """BASE plus the BULL (highest) and BEAR (lowest) historical extremes.

        Every extreme passes the same driver bounds as any other assumption source: an
        out-of-bounds observation is rejected (never clamped) and the next most extreme one is
        tried; a scenario with no in-bounds candidate on its side of BASE keeps BASE. Both are
        reported as issues and marked on the assumption.
        """
        out = {SC.BASE: base}
        for scenario, method in zip((SC.BULL, SC.BEAR), methods, strict=True):
            bull = scenario == SC.BULL
            rejected: list[str] = []
            chosen: Observation | None = None
            for o in sorted(observations, key=lambda o: (o.value, o.period_key), reverse=bull):
                # the raw value first: an absurd one is never even quantized
                problem = driver_bounds_error(driver, o.value) or driver_bounds_error(
                    driver, quantize(o.value)
                )
                if problem is None:
                    chosen = o
                    break
                rejected.append(f"{o.period_key} = {o.value}")
            mark: tuple[str, ...] = ()
            if rejected:
                mark = ("SCENARIO_EXTREME_OUT_OF_BOUNDS",)
                self.issues.add(
                    "SCENARIO_EXTREME_OUT_OF_BOUNDS",
                    Severity.WARNING,
                    f"{scenario.value} historical candidate(s) rejected, "
                    f"outside {driver.value} bounds: {'; '.join(rejected)}",
                    scenario=scenario,
                    subject=driver.value,
                )
            candidate = (
                None if chosen is None else self._pick(chosen, observations, method, completeness)
            )
            if candidate is None:
                self.issues.add(
                    "SCENARIO_FELL_BACK_TO_BASE",
                    Severity.WARNING,
                    f"no {scenario.value} historical candidate within {driver.value} bounds: "
                    f"BASE {base.value} used",
                    scenario=scenario,
                    subject=driver.value,
                )
            elif (candidate.value < base.value) if bull else (candidate.value > base.value):
                self.issues.add(
                    "SCENARIO_RANGE_INVERTED",
                    Severity.WARNING,
                    f"{scenario.value} {candidate.value} is on the wrong side of BASE "
                    f"{base.value}: BASE used",
                    scenario=scenario,
                    subject=driver.value,
                )
                candidate = None
            if candidate is None:
                mark += ("SCENARIO_FELL_BACK_TO_BASE",) if rejected else ()
                candidate = base
            if mark:
                candidate = replace(candidate, warnings=tuple(sorted({*candidate.warnings, *mark})))
            out[scenario] = candidate
        return out

    @staticmethod
    def _pick(
        chosen: Observation,
        observations: Sequence[Observation],
        method: DerivationMethod,
        completeness: HistoryCompleteness,
    ) -> Candidate:
        sparse = () if completeness == HistoryCompleteness.FULL else ("SPARSE_HISTORY",)
        return Candidate(
            quantize(chosen.value),
            method,
            SRC.HISTORICAL,
            tuple(observations),
            completeness,
            _confidence(observations, completeness),
            _warnings(observations, *sparse),
        )

    def spread(
        self, driver: Driver, observations: list[Observation], *, scenario_range: bool
    ) -> dict[Scenario, Candidate] | None:
        if not observations:
            self.issues.add(
                "INSUFFICIENT_HISTORY",
                Severity.WARNING,
                "no usable fiscal year in the window",
                subject=driver.value,
            )
            return None
        completeness = _completeness(len(observations), self.window)
        extra = [] if completeness == HistoryCompleteness.FULL else ["SPARSE_HISTORY"]
        if len(observations) == 1:
            extra.append("NO_HISTORICAL_DISPERSION")
        obs = tuple(observations)
        value = quantize(median([o.value for o in obs]))
        problem = driver_bounds_error(driver, value)
        if problem:
            self.issues.add(
                "HISTORY_OUT_OF_BOUNDS", Severity.WARNING, problem, subject=driver.value
            )
            return None
        base = Candidate(
            value,
            DM.HISTORICAL_MEDIAN,
            SRC.HISTORICAL,
            obs,
            completeness,
            _confidence(obs, completeness),
            _warnings(obs, *extra),
        )
        if not scenario_range or len(observations) == 1:
            return {SC.BASE: base, SC.BULL: base, SC.BEAR: base}
        return self._scenarios(
            driver, base, obs, (DM.HISTORICAL_MAX, DM.HISTORICAL_MIN), completeness
        )


# --- assumptions ------------------------------------------------------------------------------


def _scenario_candidate(
    found: dict[Scenario, Candidate] | None, scenario: Scenario, k: int, horizon: int,
    trajectory: Candidate | None,
) -> Candidate | None:
    """The default for one driver, scenario and forecast year (k = 0 is the first year).

    First year: with reported months, BASE (and every non-range driver) is the year-to-date
    trajectory and BULL / BEAR keep their historical extreme only on their side of it; without
    them, the historical BASE / extremes. Each later year closes the gap between that first-year
    value and the historical BASE by 1 / horizon: neither an extreme year nor the current
    trajectory is held flat for the whole horizon, and no year snaps back to a stale median."""
    ranged = found is not None and (found[SC.BULL].method != found[SC.BASE].method
                                    or found[SC.BEAR].method != found[SC.BASE].method)
    if trajectory is not None:
        first = trajectory
        if scenario != SC.BASE and ranged and found is not None:
            extreme = found[scenario]
            beyond = (extreme.value > trajectory.value if scenario == SC.BULL
                      else extreme.value < trajectory.value)
            first = extreme if beyond else trajectory
    elif found is not None:
        first = found[scenario]  # BASE itself when the extreme fell back (marked)
    else:
        return None
    if k == 0 or found is None:
        return first
    base = found[SC.BASE]
    if first.value == base.value:
        return found[scenario] if trajectory is None else base
    with localcontext() as ctx:
        ctx.prec = PRECISION
        value = quantize(first.value + (base.value - first.value) * k / horizon)
    origin = ("FROM_YEAR_TO_DATE" if first.method == DM.YEAR_TO_DATE_TRAJECTORY
              else "FROM_HISTORICAL_EXTREME")
    return Candidate(value, DM.HISTORICAL_FADE_TO_BASE, SRC.HISTORICAL,
                     (*first.observations, *(o for o in base.observations
                                             if o not in first.observations)),
                     first.completeness, weakest([first.confidence, base.confidence]),
                     tuple(sorted({*first.warnings, "FADES_TOWARD_BASE", origin})))


def _assumption_id(box4_hash: str, basis: ReportingBasis, a: ForecastAssumption) -> UUID:
    return uuid5(
        NS,
        "|".join(
            (
                box4_hash,
                basis.value,
                "A",
                a.scenario.value,
                a.driver.value,
                str(a.fiscal_year),
                a.source.value,
                a.method.value,
                str(a.value),
                a.model_dump_json(exclude={"assumption_id"}),
            )
        ),
    )


def _from_candidate(
    driver: Driver, scenario: Scenario, fy: int, c: Candidate, currency: Currency
) -> ForecastAssumption:
    unit = DRIVER_SPECS[driver].unit
    return ForecastAssumption(
        assumption_id=UUID(int=0),
        driver=driver,
        scenario=scenario,
        fiscal_year=fy,
        value=c.value,
        unit=unit,
        currency=currency if unit == AssumptionUnit.MONEY else None,
        source=c.source,
        method=c.method,
        historical_periods=tuple(sorted({o.period_key for o in c.observations})),
        canonical_value_ids=tuple(
            sorted({i.canonical_value_id for o in c.observations for i in o.inputs}, key=str)
        ),
        observations=tuple((o.period_key, o.value) for o in c.observations),
        completeness=c.completeness,
        confidence=c.confidence,
        warnings=c.warnings,
    )


def _describe(a: ForecastAssumption) -> str:
    return f"{a.source.value}:{a.method.value}:{a.value}"


GUIDANCE_DRIVERS: dict[str, Driver] = {
    "REVENUE_GROWTH": D.REVENUE_GROWTH,
    "EBITDA_MARGIN": D.EBITDA_MARGIN,
    "TAX_RATE": D.TAX_RATE,
    "CAPEX": D.CAPEX_AMOUNT,
}


def guidance_assumptions(
    items: Sequence[GuidanceItem],
    *,
    management_id: UUID | None,
    basis: ReportingBasis,
    currency: Currency,
    years: dict[int, date],
    drivers: set[Driver],
    scenarios: Sequence[Scenario],
    issues: Issues,
) -> dict[tuple[Driver, Scenario, int], ForecastAssumption]:
    """Box 8 numeric guidance -> assumptions under the documented deterministic policy."""
    latest: dict[tuple[Driver, int], tuple[tuple[date, int], GuidanceItem]] = {}
    for order, g in enumerate(items):
        driver = GUIDANCE_DRIVERS.get(g.subject.subject_code)
        if driver is None:
            continue
        subject = f"{driver.value} guidance {g.guidance_id}"
        period = g.target_period
        if period is None or period.period_type != PeriodType.FY or period.fiscal_year is None:
            issues.add(
                "GUIDANCE_NOT_APPLIED", Severity.INFO, "no fiscal-year target", subject=subject
            )
            continue
        fy = period.fiscal_year
        if fy not in years:
            issues.add(
                "GUIDANCE_OUTSIDE_HORIZON", Severity.INFO, f"targets FY{fy}", subject=subject
            )
            continue
        if period.period_end is not None and period.period_end != years[fy]:
            issues.add(
                "GUIDANCE_NOT_APPLIED",
                Severity.INFO,
                f"target period ends {period.period_end}, forecast FY{fy} ends {years[fy]}",
                subject=subject,
            )
            continue
        key = (driver, fy)
        rank = (g.issued_date or date.min, order)
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, g)
    out: dict[tuple[Driver, Scenario, int], ForecastAssumption] = {}
    for (driver, fy), (_, g) in sorted(latest.items(), key=lambda i: (i[0][0].value, i[0][1])):
        subject = f"{driver.value} guidance {g.guidance_id}"
        reason = _guidance_rejection(g, driver, basis, currency, drivers)
        if reason:
            issues.add(
                "GUIDANCE_NOT_APPLIED", Severity.INFO, reason, subject=subject, fiscal_year=fy
            )
            continue
        scale = g.scale_to_crore if g.unit == GuidanceUnit.MONEY else Decimal(1)
        assert scale is not None
        values: dict[Scenario, tuple[Decimal, DerivationMethod, str]]
        if g.target_type == TargetType.RANGE:
            assert g.low is not None and g.high is not None
            low, high = g.low * scale, g.high * scale
            mid = quantize((low + high) / 2)
            if driver == D.REVENUE_GROWTH:
                policy = "RANGE: BASE midpoint, BULL high, BEAR low"
                values = {
                    SC.BASE: (mid, DM.GUIDANCE_MIDPOINT, policy),
                    SC.BULL: (quantize(high), DM.GUIDANCE_HIGH, policy),
                    SC.BEAR: (quantize(low), DM.GUIDANCE_LOW, policy),
                }
            else:
                policy = "RANGE: midpoint in every scenario"
                values = {s: (mid, DM.GUIDANCE_MIDPOINT, policy) for s in SC}
        else:
            assert g.value is not None
            policy = "POINT: the point value in every scenario"
            values = {s: (quantize(g.value * scale), DM.GUIDANCE_POINT, policy) for s in SC}
        bad = [driver_bounds_error(driver, v) for v, _, _ in values.values()]
        if any(bad):
            issues.add(
                "GUIDANCE_NOT_APPLIED",
                Severity.WARNING,
                next(b for b in bad if b),
                subject=subject,
                fiscal_year=fy,
            )
            continue
        confidence = (
            Confidence.HIGH if g.confidence == GuidanceConfidence.HIGH else Confidence.MEDIUM
        )
        unit = DRIVER_SPECS[driver].unit
        for scenario in scenarios:
            value, method, policy = values[scenario]
            out[(driver, scenario, fy)] = ForecastAssumption(
                assumption_id=UUID(int=0),
                driver=driver,
                scenario=scenario,
                fiscal_year=fy,
                value=value,
                unit=unit,
                currency=currency if unit == AssumptionUnit.MONEY else None,
                source=SRC.MANAGEMENT_GUIDANCE,
                method=method,
                guidance=GuidanceReference(
                    management_dataset_id=management_id,
                    guidance_id=g.guidance_id,
                    event_id=g.event_id,
                    statement_ids=g.statement_ids,
                    subject_code=g.subject.subject_code,
                    target_type=g.target_type.value,
                    low=g.low,
                    high=g.high,
                    value=g.value,
                    raw_target_text=g.raw_target_text,
                    policy=policy,
                ),
                completeness=HistoryCompleteness.NOT_APPLICABLE,
                confidence=confidence,
                warnings=tuple(sorted(set(g.warnings))),
            )
    return out


def _guidance_rejection(
    g: GuidanceItem,
    driver: Driver,
    basis: ReportingBasis,
    currency: Currency,
    drivers: set[Driver],
) -> str | None:
    if driver not in drivers:
        return f"{driver.value} is not a driver of this forecast (definition differs)"
    if g.lifecycle == Lifecycle.WITHDRAWN:
        return "the latest guidance was withdrawn"
    if g.subject.equivalence != Equivalence.EXACT:
        return f"subject equivalence {g.subject.equivalence.value} (definitions may differ)"
    if g.qualifier is not None:
        return f"qualified guidance ({g.qualifier})"
    if g.reporting_basis != basis:
        return f"guidance basis {g.reporting_basis.value} is not the forecast basis {basis.value}"
    if g.confidence == GuidanceConfidence.LOW:
        return "LOW extraction confidence"
    if g.target_type not in (TargetType.RANGE, TargetType.POINT):
        return f"target type {g.target_type.value} has no deterministic mapping"
    expected = (
        GuidanceUnit.MONEY
        if DRIVER_SPECS[driver].unit == AssumptionUnit.MONEY
        else (GuidanceUnit.PERCENT)
    )
    if g.unit != expected:
        return f"unit {g.unit.value}, expected {expected.value}"
    if g.unit == GuidanceUnit.MONEY and (g.currency != currency or g.scale_to_crore is None):
        return "money guidance in another currency or with an unknown scale"
    return None


def _from_input(a: AssumptionInput, currency: Currency) -> ForecastAssumption:
    if a.unit == AssumptionUnit.MONEY and a.currency != currency:
        raise ForecastInputError(
            "CURRENCY_MISMATCH",
            f"{a.driver.value} is in {a.currency}, the forecast is in {currency.value}",
        )
    value = a.value if a.unit != AssumptionUnit.PERCENT else quantize(a.value)
    return ForecastAssumption(
        assumption_id=UUID(int=0),
        driver=a.driver,
        scenario=a.scenario,
        fiscal_year=a.fiscal_year,
        value=value,
        unit=a.unit,
        currency=a.currency,
        source=a.source,
        method=DM.EXPLICIT_VALUE,
        reference=a.reference,
        evidence_ids=a.evidence_ids,
        completeness=HistoryCompleteness.NOT_APPLICABLE,
        confidence=Confidence.HIGH,
        warnings=("QUANTIZED_TO_6DP",) if value != a.value else (),
    )


# --- projection -------------------------------------------------------------------------------


@dataclass(frozen=True)
class YearToDate:
    """The reported months of the first forecast year, as anchors and trajectory drivers."""

    base: YearToDateBase
    actuals: dict[ForecastLine, ForecastValue]  # summed quarters (+ built TAX and EBIT)
    prior_remainder_revenue: ForecastValue  # latest FY revenue less its same months
    trajectory: dict[Driver, Candidate]  # the year-to-date's own growth and ratios

    @property
    def remaining_months(self) -> int:
        return 12 - self.base.months

    def anchors(self) -> tuple[ForecastValue, ...]:
        return (*sorted(self.actuals.values(), key=lambda v: v.line.value),
                self.prior_remainder_revenue)


def _sum_anchor(plan_hash: str, basis: ReportingBasis, currency: Currency, line: ForecastLine,
                period: FinancialPeriod, formula_id: str, facts: Sequence[Fact],
                value: Decimal | None = None, warnings: tuple[str, ...] = ()) -> ForecastValue:
    total = value if value is not None else sum((f.value for f in facts), D0)
    inputs = tuple(i for f in facts for i in f.inputs)
    tag = f"{plan_hash}|{basis.value}|V|ACTUAL|{line.value}|{period.key()}|{formula_id}"
    return ForecastValue(
        value_id=uuid5(NS, f"{tag}|{total}|{[str(i.canonical_value_id) for i in inputs]}"),
        line=line, scenario=None, kind="ACTUAL", period=period, status="AVAILABLE",
        value=total, unit="MONEY", currency=currency, scale=Scale.CRORES,
        formula_id=formula_id, formula_text=ACTUAL_COMPOSITES[formula_id][1],
        actual_components=inputs,
        warnings=tuple(sorted({*warnings, *(w for f in facts for w in f.warnings)})))


def _ytd_period(history: History, months: int, end: date, fiscal_year: int,
                suffix: str) -> FinancialPeriod:
    kind, short = YTD_PERIODS[months]
    return FinancialPeriod(period_type=kind, label=f"{short} FY{fiscal_year} {suffix}",
                           period_start=shift(end, -months) + timedelta(days=1), period_end=end,
                           duration_months=months, fiscal_year=fiscal_year)


def _year_to_date(plan_hash: str, basis: ReportingBasis, currency: Currency, history: History,
                  latest_end: date, route: OperatingRoute,
                  issues: Issues) -> YearToDate | None:
    """The reported quarters of the fiscal year after `latest_end`, contiguous from its first
    quarter, with every modelled P&L line in every quarter and the prior-year revenue of the
    same quarters; None (flagged) when any of that is missing - never a partial sum."""
    if route == OperatingRoute.UNAVAILABLE:
        return None
    quarter = history.periods.quarter
    ends: list[date] = []
    for k in (1, 2, 3):  # a fourth quarter is the next fiscal year itself
        end = shift(latest_end, 3 * k)
        if not isinstance(history.frame.get(REVENUE, quarter(end)), Fact):
            break
        ends.append(end)
    if not ends:
        return None
    fiscal_year = history.periods.fiscal_year(ends[-1])
    months = 3 * len(ends)
    period = _ytd_period(history, months, ends[-1], fiscal_year, "(reported)")
    prior_ends = [shift(e, -12) for e in ends]
    prior_period = _ytd_period(history, months, prior_ends[-1], fiscal_year - 1, "(reported)")
    op_line = L.OPERATING_PROFIT if route == OperatingRoute.OPERATING_PROFIT else L.EBITDA
    lines = [L.REVENUE, op_line, L.DEPRECIATION_AMORTIZATION, L.FINANCE_COST, L.PBT, L.PAT]
    if route == OperatingRoute.OPERATING_PROFIT:
        lines.append(L.OTHER_INCOME)
    facts: dict[ForecastLine, list[Fact]] = {}
    gaps: list[str] = []
    for line in lines:
        for end in ends:
            got = history.frame.get(YTD_METRICS[line], quarter(end))
            if isinstance(got, Fact):
                facts.setdefault(line, []).append(got)
            else:
                gaps.append(got.what)
    prior = [history.frame.get(REVENUE, quarter(e)) for e in prior_ends]
    gaps += [g.what for g in prior if isinstance(g, Missing)]
    annual = history.flow(REVENUE, latest_end)
    if isinstance(annual, Missing):
        gaps.append(annual.what)
    if gaps:
        issues.add("YTD_ACTUALS_INCOMPLETE", Severity.WARNING,
                   f"{period.label}: the first forecast year is forecast in full because the "
                   f"reported months are incomplete ({'; '.join(gaps[:4])})")
        return None
    prior_facts = [g for g in prior if isinstance(g, Fact)]
    assert isinstance(annual, Fact)
    taxes: list[Fact] = []
    implied = False
    for i, end in enumerate(ends):
        got = history.frame.get(TAX, quarter(end))
        if isinstance(got, Fact):
            taxes.append(got)
            continue
        q_pbt, q_pat = facts[L.PBT][i], facts[L.PAT][i]
        implied = True
        taxes.append(Fact(value=q_pbt.value - q_pat.value, inputs=q_pbt.inputs + q_pat.inputs,
                          confidence=weakest([q_pbt.confidence, q_pat.confidence]),
                          warnings=q_pbt.warnings + q_pat.warnings))

    def anchor(line: ForecastLine, parts: Sequence[Fact], formula: str,
               value: Decimal | None = None, warnings: tuple[str, ...] = ()) -> ForecastValue:
        return _sum_anchor(plan_hash, basis, currency, line, period, formula, parts, value,
                           warnings)

    actuals = {line: anchor(line, facts[line], f"ACTUAL_YTD/1/{line.value}") for line in lines}
    actuals[L.TAX] = anchor(L.TAX, taxes, "ACTUAL_YTD/1/TAX",
                            warnings=("IMPLIED_TAX",) if implied else ())
    da = actuals[L.DEPRECIATION_AMORTIZATION].value
    ebit = (actuals[op_line].value or D0) - (da or D0) + (
        actuals[L.OTHER_INCOME].value or D0 if L.OTHER_INCOME in actuals else D0)
    actuals[L.EBIT] = anchor(L.EBIT, [f for line in (op_line, L.OTHER_INCOME,
                                                     L.DEPRECIATION_AMORTIZATION)
                                      for f in facts.get(line, ())], "ACTUAL_YTD/1/EBIT", ebit)
    prior_sum = sum((f.value for f in prior_facts), D0)
    remainder_period = FinancialPeriod(
        period_type=PeriodType.OTHER,
        label=f"FY{fiscal_year - 1} less {prior_period.label.split(' (')[0]} (reported)",
        period_start=prior_ends[-1] + timedelta(days=1), period_end=latest_end,
        duration_months=12 - months, fiscal_year=fiscal_year - 1)
    remainder_value = annual.value - prior_sum
    if remainder_value <= 0 or prior_sum <= 0:
        issues.add("YTD_ACTUALS_INCOMPLETE", Severity.WARNING,
                   f"{period.label}: prior-year revenue of the same / remaining months is not "
                   "positive; the first forecast year is forecast in full")
        return None
    remainder = _sum_anchor(plan_hash, basis, currency, L.REVENUE, remainder_period,
                            "ACTUAL_PRIOR_YEAR_REMAINDER/1/REVENUE", [annual, *prior_facts],
                            remainder_value)
    ytd_revenue = actuals[L.REVENUE].value
    assert ytd_revenue is not None
    key = period.key()
    trajectory: dict[Driver, Candidate] = {}

    def observe(driver: Driver, value: Decimal, observations: tuple[Observation, ...]) -> None:
        q = quantize(value)
        problem = driver_bounds_error(driver, q)
        if problem:
            issues.add("YTD_TRAJECTORY_OUT_OF_BOUNDS", Severity.INFO, problem,
                       subject=driver.value)
            return
        trajectory[driver] = Candidate(
            q, DM.YEAR_TO_DATE_TRAJECTORY, SRC.HISTORICAL, observations,
            HistoryCompleteness.SINGLE_POINT, weakest([o.confidence for o in observations]),
            ("REPORTED_YEAR_TO_DATE",))

    def obs(label: str, value: Decimal, parts: Sequence[Fact]) -> Observation:
        return Observation(label, value, tuple(i for f in parts for i in f.inputs),
                           weakest([f.confidence for f in parts]),
                           tuple(w for f in parts for w in f.warnings))

    with localcontext() as ctx:
        ctx.prec = PRECISION
        observe(D.REVENUE_GROWTH, (ytd_revenue / prior_sum - 1) * HUNDRED, (
            obs(prior_period.key(), prior_sum, prior_facts),
            obs(key, ytd_revenue, facts[L.REVENUE])))
        margin = D.OPERATING_MARGIN if op_line == L.OPERATING_PROFIT else D.EBITDA_MARGIN
        for driver, line in ((margin, op_line), (D.OTHER_INCOME_TO_REVENUE, L.OTHER_INCOME),
                             (D.DA_TO_REVENUE, L.DEPRECIATION_AMORTIZATION),
                             (D.FINANCE_COST_TO_REVENUE, L.FINANCE_COST)):
            if line in actuals and ytd_revenue > 0:
                part = actuals[line].value or D0
                observe(driver, part * HUNDRED / ytd_revenue, (
                    obs(key, part * HUNDRED / ytd_revenue, [*facts[line], *facts[L.REVENUE]]),))
        pbt, tax = actuals[L.PBT].value or D0, actuals[L.TAX].value or D0
        if pbt > 0 and D0 <= tax * HUNDRED / pbt <= HUNDRED:
            observe(D.TAX_RATE, tax * HUNDRED / pbt, (
                obs(key, tax * HUNDRED / pbt, [*taxes, *facts[L.PBT]]),))
    issues.add("YEAR_TO_DATE_USED", Severity.INFO,
               f"FY{fiscal_year}E = {period.label} + a forecast of the remaining "
               f"{12 - months} months (prior-year base: {remainder_period.label})")
    return YearToDate(
        base=YearToDateBase(fiscal_year=fiscal_year, months=months, period=period,
                            prior_period=prior_period,
                            quarter_keys=tuple(quarter(e).key() for e in ends),
                            prior_quarter_keys=tuple(quarter(e).key() for e in prior_ends)),
        actuals=actuals, prior_remainder_revenue=remainder, trajectory=trajectory)


@dataclass(frozen=True)
class Plan:
    """Everything a scenario projection needs: immutable, shared, never written."""

    box4_hash: str
    basis: ReportingBasis
    currency: Currency
    route: OperatingRoute
    finance_method: Driver | None  # INTEREST_RATE_ON_DEBT or FINANCE_COST_TO_REVENUE
    minority: bool | None  # True: consolidated with minority; False: standalone; None: unknown
    years: tuple[FinancialPeriod, ...]
    anchors: dict[ForecastLine, ForecastValue]
    model: ForecastModel = ForecastModel.NON_FINANCIAL
    ytd: YearToDate | None = None  # the reported months of the first forecast year


Ref = ForecastValue | ForecastAssumption | None


def project(
    plan: Plan,
    scenario: Scenario,
    assumptions: dict[tuple[Driver, int], ForecastAssumption],
    issues: Issues,
) -> list[ForecastValue]:
    """Pure: one scenario from the shared plan and that scenario's own assumptions."""
    out: list[ForecastValue] = []
    prior: dict[ForecastLine, ForecastValue] = dict(plan.anchors)
    for period in plan.years:
        fy = period.fiscal_year
        assert fy is not None
        cur: dict[ForecastLine, ForecastValue] = {}

        def a(driver: Driver, fy: int = fy) -> ForecastAssumption | None:
            return assumptions.get((driver, fy))

        def emit(
            line: ForecastLine,
            formula_id: str,
            refs: list[tuple[str, Ref]],
            *,
            period: FinancialPeriod = period,
            fy: int = fy,
            cur: dict[ForecastLine, ForecastValue] = cur,
        ) -> ForecastValue:
            value = _compute(plan, scenario, line, formula_id, refs, period, issues, fy)
            cur[line] = value
            out.append(value)
            return value

        if plan.model == ForecastModel.NON_BANK_FINANCIAL:
            assets = emit(L.TOTAL_ASSETS, "TOTAL_ASSETS/1", [
                ("PRIOR_TOTAL_ASSETS", prior.get(L.TOTAL_ASSETS)),
                ("ASSET_GROWTH", a(D.ASSET_GROWTH))])
            pat = emit(L.PAT, "PAT/1/ROA_ON_AVERAGE_ASSETS", [
                ("PRIOR_TOTAL_ASSETS", prior.get(L.TOTAL_ASSETS)), ("TOTAL_ASSETS", assets),
                ("ROA", a(D.ROA))])
            equity = emit(L.COMMON_EQUITY, "COMMON_EQUITY/1/EQUITY_CHANGE_TO_PAT", [
                ("PRIOR_COMMON_EQUITY", prior.get(L.COMMON_EQUITY)), ("PAT", pat),
                ("EQUITY_CHANGE_TO_PAT", a(D.EQUITY_CHANGE_TO_PAT))])
            funding = emit(L.NON_EQUITY_FUNDING, "NON_EQUITY_FUNDING/1", [
                ("TOTAL_ASSETS", assets), ("COMMON_EQUITY", equity)])
            borrowings = emit(L.TOTAL_BORROWINGS, "TOTAL_BORROWINGS/1/SHARE_OF_FUNDING", [
                ("NON_EQUITY_FUNDING", funding),
                ("BORROWINGS_SHARE_OF_FUNDING", a(D.BORROWINGS_SHARE_OF_FUNDING))])
            emit(L.FINANCE_COST, "FINANCE_COST/1/COST_OF_BORROWINGS_PROXY", [
                ("PRIOR_TOTAL_BORROWINGS", prior.get(L.TOTAL_BORROWINGS)),
                ("TOTAL_BORROWINGS", borrowings), ("COST_OF_BORROWINGS", a(D.COST_OF_BORROWINGS))])
            emit(L.TOTAL_INCOME, "TOTAL_INCOME/1/INCOME_ON_AVERAGE_ASSETS", [
                ("PRIOR_TOTAL_ASSETS", prior.get(L.TOTAL_ASSETS)), ("TOTAL_ASSETS", assets),
                ("TOTAL_INCOME_TO_AVERAGE_ASSETS", a(D.TOTAL_INCOME_TO_AVERAGE_ASSETS))])
            prior = cur
            continue
        # the first forecast year with reported months: those months + the remaining ones
        y = plan.ytd if plan.ytd is not None and period == plan.years[0] else None
        ya = y.actuals if y is not None else {}

        def rest(line: ForecastLine, driver: Driver, revenue: ForecastValue,
                 ya: dict[ForecastLine, ForecastValue] = ya) -> ForecastValue:
            """Year-to-date actual + the remaining months' revenue x the driver ratio."""
            return emit(line, f"{line.value}/2/YTD_PLUS_REMAINDER", [
                (f"YTD_{line.value}", ya.get(line)), ("REVENUE", revenue),
                ("YTD_REVENUE", ya.get(L.REVENUE)), (driver.value, a(driver))])

        if y is not None:
            revenue = emit(L.REVENUE, "REVENUE/2/YTD_PLUS_REMAINDER", [
                ("YTD_REVENUE", ya.get(L.REVENUE)),
                ("PRIOR_YEAR_REMAINDER_REVENUE", y.prior_remainder_revenue),
                ("REVENUE_GROWTH", a(D.REVENUE_GROWTH))])
        else:
            revenue = emit(
                L.REVENUE,
                "REVENUE/1",
                [("PRIOR_REVENUE", prior.get(L.REVENUE)),
                 ("REVENUE_GROWTH", a(D.REVENUE_GROWTH))],
            )
        if plan.route == OperatingRoute.UNAVAILABLE:
            prior = cur
            continue
        if y is not None and plan.route == OperatingRoute.OPERATING_PROFIT:
            op = rest(L.OPERATING_PROFIT, D.OPERATING_MARGIN, revenue)
            oi = rest(L.OTHER_INCOME, D.OTHER_INCOME_TO_REVENUE, revenue)
            ebitda = emit(L.EBITDA, "EBITDA/1/OPERATING_PROFIT_ROUTE",
                          [("OPERATING_PROFIT", op), ("OTHER_INCOME", oi)])
        elif y is not None:
            ebitda = rest(L.EBITDA, D.EBITDA_MARGIN, revenue)
        elif plan.route == OperatingRoute.OPERATING_PROFIT:
            op = emit(
                L.OPERATING_PROFIT,
                "OPERATING_PROFIT/1",
                [("REVENUE", revenue), ("OPERATING_MARGIN", a(D.OPERATING_MARGIN))],
            )
            oi = emit(
                L.OTHER_INCOME,
                "OTHER_INCOME/1",
                [("REVENUE", revenue), ("OTHER_INCOME_TO_REVENUE", a(D.OTHER_INCOME_TO_REVENUE))],
            )
            ebitda = emit(
                L.EBITDA,
                "EBITDA/1/OPERATING_PROFIT_ROUTE",
                [("OPERATING_PROFIT", op), ("OTHER_INCOME", oi)],
            )
        else:
            ebitda = emit(
                L.EBITDA,
                "EBITDA/1/REPORTED_MARGIN",
                [("REVENUE", revenue), ("EBITDA_MARGIN", a(D.EBITDA_MARGIN))],
            )
        da = rest(L.DEPRECIATION_AMORTIZATION, D.DA_TO_REVENUE, revenue) if y is not None \
            else emit(
                L.DEPRECIATION_AMORTIZATION,
                "DEPRECIATION_AMORTIZATION/1",
                [("REVENUE", revenue), ("DA_TO_REVENUE", a(D.DA_TO_REVENUE))],
            )
        ebit = emit(L.EBIT, "EBIT/1", [("EBITDA", ebitda), ("DEPRECIATION_AMORTIZATION", da)])
        rate, ratio = a(D.INTEREST_RATE_ON_DEBT), a(D.FINANCE_COST_TO_REVENUE)
        stated_ratio = ratio is not None and ratio.source != SRC.HISTORICAL
        if y is not None and rate is not None and (
                rate.source != SRC.HISTORICAL or not stated_ratio):
            finance = emit(
                L.FINANCE_COST,
                f"FINANCE_COST/2/RATE_YTD_REMAINING_{y.remaining_months}M",
                [("YTD_FINANCE_COST", ya.get(L.FINANCE_COST)),
                 ("OPENING_GROSS_DEBT", prior.get(L.GROSS_DEBT)), ("INTEREST_RATE_ON_DEBT", rate)],
            )
        elif y is not None:
            finance = rest(L.FINANCE_COST, D.FINANCE_COST_TO_REVENUE, revenue)
        elif rate is not None and (rate.source != SRC.HISTORICAL or not stated_ratio):
            finance = emit(
                L.FINANCE_COST,
                "FINANCE_COST/1/RATE_ON_OPENING_DEBT",
                [("OPENING_GROSS_DEBT", prior.get(L.GROSS_DEBT)), ("INTEREST_RATE_ON_DEBT", rate)],
            )
        else:
            finance = emit(
                L.FINANCE_COST,
                "FINANCE_COST/1/RATIO_TO_REVENUE",
                [("REVENUE", revenue), ("FINANCE_COST_TO_REVENUE", ratio)],
            )
        if y is not None:
            pbt = emit(L.PBT, "PBT/2/YTD_PLUS_REMAINDER", [
                ("YTD_PBT", ya.get(L.PBT)), ("EBIT", ebit), ("YTD_EBIT", ya.get(L.EBIT)),
                ("FINANCE_COST", finance), ("YTD_FINANCE_COST", ya.get(L.FINANCE_COST))])
            tax = emit(L.TAX, "TAX/2/YTD_PLUS_REMAINDER", [
                ("YTD_TAX", ya.get(L.TAX)), ("PBT", pbt), ("YTD_PBT", ya.get(L.PBT)),
                ("TAX_RATE", a(D.TAX_RATE))])
            pat = emit(L.PAT, "PAT/2/YTD_PLUS_REMAINDER", [
                ("YTD_PAT", ya.get(L.PAT)), ("PBT", pbt), ("YTD_PBT", ya.get(L.PBT)),
                ("TAX", tax), ("YTD_TAX", ya.get(L.TAX))])
        else:
            pbt = emit(L.PBT, "PBT/1", [("EBIT", ebit), ("FINANCE_COST", finance)])
            tax = emit(L.TAX, "TAX/1", [("PBT", pbt), ("TAX_RATE", a(D.TAX_RATE))])
            pat = emit(L.PAT, "PAT/1", [("PBT", pbt), ("TAX", tax)])
        if plan.minority is True:
            mi = emit(
                L.MINORITY_INTEREST,
                "MINORITY_INTEREST/1",
                [("PAT", pat), ("MINORITY_SHARE_OF_PAT", a(D.MINORITY_SHARE_OF_PAT))],
            )
            attributable = emit(
                L.PAT_ATTRIBUTABLE,
                "PAT_ATTRIBUTABLE/1/LESS_MINORITY",
                [("PAT", pat), ("MINORITY_INTEREST", mi)],
            )
        elif plan.minority is False:
            attributable = emit(L.PAT_ATTRIBUTABLE, "PAT_ATTRIBUTABLE/1/STANDALONE", [("PAT", pat)])
        else:
            attributable = _unavailable(
                plan,
                scenario,
                L.PAT_ATTRIBUTABLE,
                period,
                "ATTRIBUTION_UNKNOWN",
                ("MINORITY_INTEREST not reported for the consolidated base year",),
            )
            cur[L.PAT_ATTRIBUTABLE] = attributable
            out.append(attributable)
        shares = a(D.SHARE_COUNT)
        if shares is None:
            eps = _unavailable(
                plan,
                scenario,
                L.EPS,
                period,
                "SHARE_COUNT_UNAVAILABLE",
                ("no reliable share count: supply an explicit SHARE_COUNT",),
            )
            out.append(eps)
        else:
            emit(L.EPS, "EPS/1", [("PAT_ATTRIBUTABLE", attributable), ("SHARE_COUNT", shares)])
        amount = a(D.CAPEX_AMOUNT)
        if amount is not None:
            capex = emit(L.CAPEX, "CAPEX/1/AMOUNT", [("CAPEX_AMOUNT", amount)])
        else:
            capex = emit(
                L.CAPEX,
                "CAPEX/1/RATIO_TO_REVENUE",
                [("REVENUE", revenue), ("CAPEX_TO_REVENUE", a(D.CAPEX_TO_REVENUE))],
            )
        twc = emit(
            L.TRADE_WORKING_CAPITAL,
            "TRADE_WORKING_CAPITAL/1",
            [("REVENUE", revenue), ("WORKING_CAPITAL_TO_REVENUE", a(D.WORKING_CAPITAL_TO_REVENUE))],
        )
        change = emit(
            L.WORKING_CAPITAL_CHANGE,
            "WORKING_CAPITAL_CHANGE/1",
            [
                ("TRADE_WORKING_CAPITAL", twc),
                ("PRIOR_TRADE_WORKING_CAPITAL", prior.get(L.TRADE_WORKING_CAPITAL)),
            ],
        )
        debt = emit(
            L.GROSS_DEBT,
            "GROSS_DEBT/1",
            [("OPENING_GROSS_DEBT", prior.get(L.GROSS_DEBT)), ("DEBT_CHANGE", a(D.DEBT_CHANGE))],
        )
        if debt.value is not None and debt.value < 0:
            issues.add(
                "NEGATIVE_DEBT",
                Severity.ERROR,
                f"gross debt would be {debt.value}: debt change exceeds opening debt",
                scenario=scenario,
                fiscal_year=fy,
                subject=L.GROSS_DEBT.value,
            )
            debt = _unavailable(plan, scenario, L.GROSS_DEBT, period, "NEGATIVE_DEBT", ())
            out[-1] = debt
            cur[L.GROSS_DEBT] = debt
        cfo = emit(
            L.CFO_ESTIMATE,
            "CFO_ESTIMATE/1",
            [("PAT", pat), ("DEPRECIATION_AMORTIZATION", da), ("WORKING_CAPITAL_CHANGE", change)],
        )
        emit(L.FCF_ESTIMATE, "FCF_ESTIMATE/1", [("CFO_ESTIMATE", cfo), ("CAPEX", capex)])
        prior = cur
    return out


def _value_id(
    plan: Plan, scenario: Scenario | None, line: ForecastLine, key: str, body: str
) -> UUID:
    tag = scenario.value if scenario else "ACTUAL"
    return uuid5(NS, f"{plan.box4_hash}|{plan.basis.value}|V|{tag}|{line.value}|{key}|{body}")


def _unavailable(
    plan: Plan,
    scenario: Scenario,
    line: ForecastLine,
    period: FinancialPeriod,
    reason: str,
    missing: tuple[str, ...],
) -> ForecastValue:
    return ForecastValue(
        value_id=_value_id(plan, scenario, line, period.key(), f"{reason}|{missing}"),
        line=line,
        scenario=scenario,
        kind="FORECAST",
        period=period,
        status="UNAVAILABLE",
        value=None,
        unit="PER_SHARE" if line == L.EPS else "MONEY",
        currency=plan.currency,
        scale=Scale.ONES if line == L.EPS else Scale.CRORES,
        reason=reason,
        missing_inputs=missing,
    )


def _compute(
    plan: Plan,
    scenario: Scenario,
    line: ForecastLine,
    formula_id: str,
    refs: list[tuple[str, Ref]],
    period: FinancialPeriod,
    issues: Issues,
    fy: int,
) -> ForecastValue:
    definition, _ = FORMULAS[formula_id]
    inputs: list[FormulaInput] = []
    missing: list[str] = []
    upstream = False
    for role, ref in refs:
        if ref is None:
            missing.append(f"{role}: no assumption or anchor")
        elif isinstance(ref, ForecastAssumption):
            inputs.append(
                FormulaInput(
                    role=role, kind="ASSUMPTION", ref_id=ref.assumption_id, value=ref.value
                )
            )
        elif ref.value is None:
            upstream = True
            missing.append(f"{role}: {ref.reason}")
        else:
            inputs.append(
                FormulaInput(role=role, kind=ref.kind, ref_id=ref.value_id, value=ref.value)
            )
    if missing:
        reason = "UPSTREAM_UNAVAILABLE" if upstream else "ASSUMPTION_MISSING"
        return _unavailable(plan, scenario, line, period, reason, tuple(missing))
    value = evaluate(formula_id, {i.role: i.value for i in inputs})
    if not value.is_finite() or abs(value) > MAX_ABS_FORECAST:
        issues.add(
            "FORECAST_OUT_OF_RANGE",
            Severity.ERROR,
            f"{line.value} exceeds {MAX_ABS_FORECAST} in absolute value",
            scenario=scenario,
            fiscal_year=fy,
            subject=line.value,
        )
        return _unavailable(plan, scenario, line, period, "OUT_OF_RANGE", ())
    warnings: tuple[str, ...] = ()
    if formula_id == "TAX/1" and inputs[0].value <= 0:
        warnings = ("NO_TAX_ON_LOSS",)
    body = "|".join(f"{i.role}={i.ref_id}" for i in inputs) + f"|{value}"
    return ForecastValue(
        value_id=_value_id(plan, scenario, line, period.key(), body),
        line=line,
        scenario=scenario,
        kind="FORECAST",
        period=period,
        status="AVAILABLE",
        value=value,
        unit="PER_SHARE" if line == L.EPS else "MONEY",
        currency=plan.currency,
        scale=Scale.ONES if line == L.EPS else Scale.CRORES,
        formula_id=formula_id,
        formula_text=definition.expression,
        inputs=tuple(inputs),
        warnings=warnings,
    )


# --- the service ------------------------------------------------------------------------------


def _select_basis(
    data: CanonicalFinancialDataset,
    request: ForecastRequest,
) -> tuple[ReportingBasis, str]:
    """The research's analytical basis (financial_analytics.select_analytical_basis), never a
    basis chosen here: a consolidated history without annual revenue gives UNAVAILABLE lines,
    it does not switch the forecast to standalone. An explicit request is honoured (and
    Boxes 13/14 refuse it when it is not the analytical basis)."""
    if request.reporting_basis is not None:
        return request.reporting_basis, "requested"
    chosen = select_analytical_basis(data)
    if chosen is None:
        return ReportingBasis.CONSOLIDATED, "Box 4 has no stated basis"
    return chosen


def _anchor(
    plan_hash: str,
    basis: ReportingBasis,
    currency: Currency,
    line: ForecastLine,
    period: FinancialPeriod,
    got: Fact | Missing,
) -> ForecastValue:
    key = period.key()
    tag = f"{plan_hash}|{basis.value}|V|ACTUAL|{line.value}|{key}"
    if isinstance(got, Missing):
        return ForecastValue(
            value_id=uuid5(NS, f"{tag}|{got.status.value}"),
            line=line,
            scenario=None,
            kind="ACTUAL",
            period=period,
            status="UNAVAILABLE",
            value=None,
            unit="MONEY",
            currency=currency,
            scale=Scale.CRORES,
            reason=got.status.value,
            missing_inputs=(got.what,),
        )
    if abs(got.value) > MAX_ABS_FORECAST:
        return ForecastValue(
            value_id=uuid5(NS, f"{tag}|OUT_OF_RANGE"),
            line=line,
            scenario=None,
            kind="ACTUAL",
            period=period,
            status="UNAVAILABLE",
            value=None,
            unit="MONEY",
            currency=currency,
            scale=Scale.CRORES,
            reason="OUT_OF_RANGE",
            missing_inputs=(f"{line.value} actual exceeds {MAX_ABS_FORECAST}",),
        )
    composite = got.formula_id is not None
    return ForecastValue(
        value_id=uuid5(NS, f"{tag}|{got.value}|{[str(i.canonical_value_id) for i in got.inputs]}"),
        line=line,
        scenario=None,
        kind="ACTUAL",
        period=period,
        status="AVAILABLE",
        value=got.value,
        unit="MONEY",
        currency=currency,
        scale=Scale.CRORES,
        formula_id=got.formula_id,
        formula_text=ACTUAL_COMPOSITES[got.formula_id][1] if got.formula_id else None,
        actual=None if composite else got.inputs[0],
        actual_components=got.inputs if composite else (),
        warnings=got.warnings,
    )


def build_forecast(
    data: CanonicalFinancialDataset,
    *,
    request: ForecastRequest | None = None,
    management: ManagementIntelligenceDataset | None = None,
    generated_at: datetime,
) -> ForecastDataset:
    """Deterministic Box 10 forecast. Raises ForecastInputError when the request cannot be
    applied to this company (unused, conflicting or out-of-horizon explicit assumptions)."""
    req = request or ForecastRequest()
    if management is not None and management.company_key != data.company_key:
        raise ForecastInputError("COMPANY_MISMATCH", "Box 8 dataset is for another company")
    box4_hash = hashlib.sha256(canonical_dataset_json(data).encode()).hexdigest()
    issues = Issues()
    currencies = frame_currencies(data)
    basis, selection = _select_basis(data, req)
    currency = currencies.get(basis, Currency.UNKNOWN)
    other = (
        ReportingBasis.STANDALONE
        if basis == ReportingBasis.CONSOLIDATED
        else ReportingBasis.CONSOLIDATED
    )
    history = History(data, basis, currency)
    if history.frame.truncated:
        issues.add("HISTORY_TRUNCATED", Severity.WARNING, "Box 4 value budget reached")
    if history.frame.other_currency:
        issues.add(
            "OTHER_CURRENCY_EXCLUDED",
            Severity.WARNING,
            f"{len(history.frame.other_currency)} values not in {currency.value} were not used",
        )
    chain, ignored = fiscal_chain(data, history.frame, req.history_years)
    for key, reason in chain.excluded:
        issues.add(reason, Severity.WARNING, key)
    fatal = chain.fatal
    if fatal is None and currency == Currency.UNKNOWN:
        fatal = ("CURRENCY_UNKNOWN", "the base currency is unknown")
    latest = history.periods.fy(chain.ends[-1]) if chain.ends else None
    years: tuple[FinancialPeriod, ...] = ()
    if latest is not None and latest.period_end is not None:
        ends = [shift(latest.period_end, 12 * k) for k in range(1, req.horizon + 1)]
        if history.periods.fiscal_year(ends[-1]) > 2100:
            raise ForecastInputError("HORIZON_OUT_OF_RANGE", "forecast beyond FY2100")
        years = tuple(
            history.periods.fy(end).model_copy(
                update={"label": f"FY{history.periods.fiscal_year(end)}E"}
            )
            for end in ends
        )
    year_ends = {p.fiscal_year: p.period_end for p in years if p.fiscal_year and p.period_end}
    for a in req.explicit_assumptions:
        if years and a.fiscal_year not in year_ends:
            raise ForecastInputError(
                "ASSUMPTION_OUTSIDE_HORIZON",
                f"{a.driver.value} FY{a.fiscal_year} is not a forecast year "
                f"({', '.join(p.label for p in years)})",
            )
    anchors: dict[ForecastLine, ForecastValue] = {}
    route = OperatingRoute.UNAVAILABLE
    finance_method: Driver | None = None
    minority: bool | None = None
    candidates: dict[Driver, dict[Scenario, Candidate]] = {}
    ytd: YearToDate | None = None
    model = forecast_model(data)
    lender = model == ForecastModel.NON_BANK_FINANCIAL
    if lender and fatal is None and latest is not None and latest.period_end is not None:
        anchors.update(lender_anchors(box4_hash, basis, currency, history, latest,
                                      latest.period_end))
        for anchor in anchors.values():
            if anchor.value is None:
                issues.add("ACTUAL_UNAVAILABLE", Severity.WARNING,
                           f"{anchor.reason}: {'; '.join(anchor.missing_inputs)}",
                           subject=anchor.line.value)
        if anchors[L.TOTAL_ASSETS].value is None:
            fatal = ("ASSET_BASE_MISSING",
                     f"no usable total assets at {latest.label}: {anchors[L.TOTAL_ASSETS].reason}")
    elif fatal is None and latest is not None and latest.period_end is not None:
        end = latest.period_end
        pit = history.periods.pit(end)
        anchors[L.REVENUE] = _anchor(
            box4_hash, basis, currency, L.REVENUE, latest, history.flow(REVENUE, end)
        )
        anchors[L.GROSS_DEBT] = _anchor(
            box4_hash, basis, currency, L.GROSS_DEBT, pit, history.gross_debt(end)
        )
        anchors[L.TRADE_WORKING_CAPITAL] = _anchor(
            box4_hash,
            basis,
            currency,
            L.TRADE_WORKING_CAPITAL,
            pit,
            history.trade_working_capital(end),
        )
        for anchor in anchors.values():
            if anchor.value is None:
                issues.add(
                    "ACTUAL_UNAVAILABLE",
                    Severity.WARNING,
                    f"{anchor.reason}: {'; '.join(anchor.missing_inputs)}",
                    subject=anchor.line.value,
                )
        if anchors[L.REVENUE].value is None:
            fatal = (
                "REVENUE_BASE_MISSING",
                f"no usable revenue for {latest.label}: {anchors[L.REVENUE].reason}",
            )
    if lender and fatal is None and latest is not None:
        _derive_lender(history, Deriver(history, chain.ends, req.history_years, issues),
                       candidates)
        issues.add("FORECAST_MODEL", Severity.INFO,
                   "NON_BANK_FINANCIAL: balance-sheet lender model (no revenue, EBITDA, "
                   "working-capital, capex or gross-debt driver)")
    elif fatal is None and latest is not None:
        derive = Deriver(history, chain.ends, req.history_years, issues)
        revenue = derive.revenue
        growth = derive.growth()
        if growth:
            candidates[D.REVENUE_GROWTH] = growth
        if data.industry in RESTRICTED_INDUSTRIES:
            issues.add(
                "UNSUPPORTED_SECTOR",
                Severity.WARNING,
                f"{data.industry.value}: only revenue is forecast "
                "(industrial P&L drivers do not apply)",
            )
        else:
            end = chain.ends[-1]
            if isinstance(history.flow([M.OPERATING_PROFIT], end), Fact):
                route = OperatingRoute.OPERATING_PROFIT
            elif isinstance(history.flow([M.EBITDA], end), Fact):
                route = OperatingRoute.REPORTED_EBITDA
            else:
                issues.add(
                    "OPERATING_BASE_MISSING",
                    Severity.ERROR,
                    "neither operating profit nor reported EBITDA for the latest fiscal year",
                )
        if route != OperatingRoute.UNAVAILABLE:
            ytd = _year_to_date(box4_hash, basis, currency, history, chain.ends[-1], route,
                                issues)
            _derive_operating(history, derive, route, candidates, issues, revenue)
            _pbt_bridge(history, route, chain.ends[-1], issues)
            finance_method = _derive_finance(history, derive, candidates, revenue)
            if ytd is not None:  # only the finance-cost driver the projection uses
                unused = ({D.FINANCE_COST_TO_REVENUE} if finance_method
                          == D.INTEREST_RATE_ON_DEBT else set())
                for driver in unused:
                    ytd.trajectory.pop(driver, None)
            if finance_method is None:
                issues.add(
                    "INSUFFICIENT_HISTORY",
                    Severity.WARNING,
                    "no finance-cost history: supply INTEREST_RATE_ON_DEBT or "
                    "FINANCE_COST_TO_REVENUE",
                    subject=L.FINANCE_COST.value,
                )
            tax_obs = derive.ratio_series(
                D.TAX_RATE,
                lambda e: history.tax(e)[0],
                lambda e: history.flow([M.PBT], e),
                what="PBT",
                extra_warnings=lambda e: history.tax(e)[1],
            )
            usable = [o for o in tax_obs if D0 <= o.value <= HUNDRED]
            for o in tax_obs:
                if o not in usable:
                    issues.add(
                        "TAX_RATE_OUT_OF_RANGE",
                        Severity.INFO,
                        f"{o.period_key}: {o.value}",
                        subject=D.TAX_RATE.value,
                    )
            tax = derive.spread(D.TAX_RATE, usable, scenario_range=False) if usable else None
            if tax:
                candidates[D.TAX_RATE] = tax
            else:
                issues.add(
                    "TAX_ASSUMPTION_REQUIRED",
                    Severity.WARNING,
                    "no usable tax history (no fiscal year with positive PBT and a 0-100 % rate)",
                    subject=D.TAX_RATE.value,
                )
            if basis == ReportingBasis.STANDALONE:
                minority = False
            elif isinstance(history.flow([M.MINORITY_INTEREST], end), Fact):
                minority = True
                share = derive.spread(
                    D.MINORITY_SHARE_OF_PAT,
                    derive.ratio_series(
                        D.MINORITY_SHARE_OF_PAT,
                        lambda e: history.flow([M.MINORITY_INTEREST], e),
                        lambda e: history.flow([M.PAT], e),
                        what="PAT",
                    ),
                    scenario_range=False,
                )
                if share:
                    candidates[D.MINORITY_SHARE_OF_PAT] = share
            else:
                issues.add(
                    "ATTRIBUTION_UNKNOWN",
                    Severity.WARNING,
                    "consolidated base year reports no minority interest: attributable PAT and EPS "
                    "are not forecast",
                )
            capex = derive.spread(
                D.CAPEX_TO_REVENUE,
                derive.ratio_series(
                    D.CAPEX_TO_REVENUE,
                    lambda e: history.flow(CAPEX, e),
                    revenue,
                    what="revenue",
                    absolute=True,
                ),
                scenario_range=False,
            )
            if capex:
                candidates[D.CAPEX_TO_REVENUE] = capex
            if anchors[L.TRADE_WORKING_CAPITAL].value is not None:
                wc = derive.spread(
                    D.WORKING_CAPITAL_TO_REVENUE,
                    derive.ratio_series(
                        D.WORKING_CAPITAL_TO_REVENUE,
                        history.trade_working_capital,
                        revenue,
                        what="revenue",
                    ),
                    scenario_range=False,
                )
                if wc:
                    candidates[D.WORKING_CAPITAL_TO_REVENUE] = wc
            if anchors[L.GROSS_DEBT].value is not None:
                fallback = Candidate(
                    D0,
                    DM.FALLBACK_CONSTANT,
                    SRC.FALLBACK,
                    (),
                    HistoryCompleteness.NOT_APPLICABLE,
                    Confidence.LOW,
                    ("DEBT_HELD_FLAT", "FALLBACK_ASSUMPTION"),
                )
                candidates[D.DEBT_CHANGE] = {s: fallback for s in SC}
    drivers = set(LENDER_DRIVERS) if lender else _drivers(
        route, finance_method, minority, data.industry in RESTRICTED_INDUSTRIES)
    for a in req.explicit_assumptions:
        if fatal is None and a.driver not in drivers:
            raise ForecastInputError(
                "UNUSED_EXPLICIT_ASSUMPTION",
                f"{a.driver.value} is not a driver of this forecast ({route.value} route)",
            )
    explicit_keys = {(a.driver, a.scenario, a.fiscal_year) for a in req.explicit_assumptions}
    for driver, scenario, fy in sorted(explicit_keys):
        if driver == D.INTEREST_RATE_ON_DEBT and (
            (D.FINANCE_COST_TO_REVENUE, scenario, fy) in explicit_keys
        ):
            raise ForecastInputError(
                "CONFLICTING_EXPLICIT_ASSUMPTIONS",
                f"both finance-cost drivers given for {scenario.value} FY{fy}",
            )
    guided = (
        guidance_assumptions(
            management.guidance if management else (),
            management_id=management.dataset_id if management else None,
            basis=basis,
            currency=currency,
            years={fy: e for fy, e in year_ends.items() if e is not None},
            drivers=drivers,
            scenarios=req.scenarios,
            issues=issues,
        )
        if fatal is None
        else {}
    )
    for a in req.explicit_assumptions:
        if a.unit == AssumptionUnit.PERCENT and D0 < abs(a.value) < 1:
            issues.add(
                "PERCENT_BELOW_ONE",
                Severity.WARNING,
                f"{a.driver.value} = {a.value} %: percent units (12.5 means 12.5 %, not 0.125)",
                scenario=a.scenario,
                fiscal_year=a.fiscal_year,
                subject=a.driver.value,
            )
    explicit = {
        (a.driver, a.scenario, a.fiscal_year): _from_input(a, currency)
        for a in req.explicit_assumptions
    }
    if ytd is not None and (not years or years[0].fiscal_year != ytd.base.fiscal_year):
        ytd = None  # defensive: the reported months belong to the first forecast year only
    plan = Plan(box4_hash, basis, currency, route, finance_method, minority, years, anchors,
                model, ytd)
    all_assumptions: list[ForecastAssumption] = []
    values: list[ForecastValue] = []
    completeness: list[ScenarioCompleteness] = []
    if fatal is not None:
        issues.add(fatal[0], Severity.ERROR, fatal[1])
    for scenario in req.scenarios:
        chosen: dict[tuple[Driver, int], ForecastAssumption] = {}
        if fatal is None:
            for k, fy in enumerate(year_ends):
                for driver in sorted(drivers, key=lambda d: d.value):
                    default = None
                    picked_candidate = _scenario_candidate(
                        candidates.get(driver), scenario, k, len(year_ends),
                        ytd.trajectory.get(driver) if ytd is not None else None)
                    if picked_candidate is not None:
                        default = _from_candidate(driver, scenario, fy, picked_candidate,
                                                  currency)
                    picked = default
                    for layer in (guided, explicit):
                        replacement = layer.get((driver, scenario, fy))
                        if replacement is not None:
                            if picked is not None:
                                replacement = replacement.model_copy(
                                    update={"overrides": (*picked.overrides, _describe(picked))}
                                )
                            picked = replacement
                    if picked is not None:
                        picked = picked.model_copy(
                            update={"assumption_id": _assumption_id(box4_hash, basis, picked)}
                        )
                        chosen[(driver, fy)] = picked
            _share_check(history, chosen, minority, chain.ends[-1], scenario, issues)
            for (driver, fy), assumption in sorted(
                chosen.items(), key=lambda i: (i[0][1], i[0][0].value)
            ):
                if assumption.source == SRC.FALLBACK:
                    issues.add(
                        "FALLBACK_ASSUMPTION",
                        Severity.WARNING,
                        f"{driver.value} = {assumption.value} is a policy constant, not evidence",
                        scenario=scenario,
                        fiscal_year=fy,
                        subject=driver.value,
                    )
        projected = project(plan, scenario, chosen, issues) if fatal is None else []
        all_assumptions.extend(
            sorted(chosen.values(), key=lambda x: (x.fiscal_year, x.driver.value))
        )
        values.extend(projected)
        completeness.append(_scenario_completeness(
            scenario, projected, chosen.values(), L.TOTAL_ASSETS if lender else L.REVENUE))
        for v in projected:
            if v.status == "UNAVAILABLE" and v.reason not in REPORTED_ELSEWHERE:
                issues.add(
                    f"LINE_{v.reason}" if v.reason else "LINE_UNAVAILABLE",
                    Severity.WARNING,
                    "; ".join(v.missing_inputs)[:1000] or None,
                    scenario=scenario,
                    fiscal_year=v.period.fiscal_year,
                    subject=v.line.value,
                )
    if issues.truncated:
        issues.items.append(ForecastIssue(code="ISSUES_TRUNCATED", severity=Severity.WARNING))
    statuses = {c.status for c in completeness}
    status = (
        ForecastStatus.UNAVAILABLE
        if fatal is not None or statuses == {ForecastStatus.UNAVAILABLE}
        else ForecastStatus.COMPLETE
        if statuses == {ForecastStatus.COMPLETE}
        else ForecastStatus.PARTIAL
    )
    forecast_id = uuid5(
        NS,
        f"{box4_hash}|{req.model_dump_json()}|{management.dataset_id if management else None}"
        f"|{FORECAST_ENGINE_VERSION}|{FORECAST_FORMULA_VERSION}|{FORECAST_POLICY_VERSION}",
    )
    return ForecastDataset(
        forecast_id=forecast_id,
        company_key=data.company_key,
        company_name=data.company_name,
        industry=data.industry,
        generated_at=generated_at,
        source_box4=Box4Source(
            schema_version=data.schema_version,
            dataset_id=data.dataset_id,
            canonical_sha256=box4_hash,
            status=data.status,
        ),
        management_dataset_id=management.dataset_id if management else None,
        request=req,
        status=status,
        base=HistoricalBase(
            reporting_basis=basis,
            basis_selection=selection,
            currency=currency,
            scale=Scale.CRORES,
            fiscal_year_end_month=data.fiscal_year_end_month,
            latest_actual=latest,
            annual_periods=tuple(history.periods.fy(e).key() for e in chain.ends),
            excluded_periods=chain.excluded,
            ignored_period_types=dict(sorted(ignored.items())),
            operating_route=route,
            other_basis_available=other in currencies,
            forecast_model=model,
            year_to_date=ytd.base if ytd is not None else None,
        ),
        forecast_years=years,
        scenarios=req.scenarios,
        actuals=tuple(anchors[line] for line in sorted(anchors, key=lambda x: x.value)),
        assumptions=tuple(all_assumptions),
        values=tuple(values),
        completeness=tuple(completeness),
        issues=tuple(issues.items),
        year_to_date_actuals=ytd.anchors() if ytd is not None else (),
        manifest=ForecastManifest(
            engine_version=FORECAST_ENGINE_VERSION,
            formula_version=FORECAST_FORMULA_VERSION,
            policy_version=FORECAST_POLICY_VERSION,
            formulas=tuple(f for f, _ in FORMULAS.values())
            + tuple(
                ForecastFormula(formula_id=k, line=line, expression=text, inputs=())
                for k, (line, text) in ACTUAL_COMPOSITES.items()
            ),
            policies=POLICIES,
        ),
    )


async def build_forecast_async(
    data: CanonicalFinancialDataset,
    *,
    request: ForecastRequest | None = None,
    management: ManagementIntelligenceDataset | None = None,
    generated_at: datetime,
) -> ForecastDataset:
    """For async callers: the CPU work (and the Box 4 hash) runs off the event loop."""
    return await asyncio.to_thread(
        build_forecast, data, request=request, management=management, generated_at=generated_at
    )


def canonical_forecast_json(dataset: ForecastDataset) -> str:
    """Deterministic serialization; generated_at is excluded so equal inputs compare equal."""
    return json.dumps(
        dataset.model_dump(mode="json", exclude={"generated_at"}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


# --- helpers ----------------------------------------------------------------------------------


def _drivers(
    route: OperatingRoute, finance: Driver | None, minority: bool | None, financial: bool
) -> set[Driver]:
    drivers = {D.REVENUE_GROWTH, D.SHARE_COUNT}
    if financial or route == OperatingRoute.UNAVAILABLE:
        return drivers
    drivers |= {
        D.DA_TO_REVENUE,
        D.TAX_RATE,
        D.CAPEX_TO_REVENUE,
        D.CAPEX_AMOUNT,
        D.WORKING_CAPITAL_TO_REVENUE,
        D.DEBT_CHANGE,
        D.INTEREST_RATE_ON_DEBT,
        D.FINANCE_COST_TO_REVENUE,
    }
    if route == OperatingRoute.OPERATING_PROFIT:
        drivers |= {D.OPERATING_MARGIN, D.OTHER_INCOME_TO_REVENUE}
    else:
        drivers.add(D.EBITDA_MARGIN)
    if minority:
        drivers.add(D.MINORITY_SHARE_OF_PAT)
    return drivers


def _flow(history: History, metric: CanonicalMetric) -> Callable[[date], Fact | Missing]:
    return lambda end: history.flow([metric], end)


def _derive_operating(
    history: History,
    derive: Deriver,
    route: OperatingRoute,
    candidates: dict[Driver, dict[Scenario, Candidate]],
    issues: Issues,
    revenue: Callable[[date], Fact | Missing],
) -> None:
    margin_driver, margin_metric = (
        (D.OPERATING_MARGIN, M.OPERATING_PROFIT)
        if route == OperatingRoute.OPERATING_PROFIT
        else (D.EBITDA_MARGIN, M.EBITDA)
    )
    margin = derive.spread(
        margin_driver,
        derive.ratio_series(
            margin_driver, lambda e: history.flow([margin_metric], e), revenue, what="revenue"
        ),
        scenario_range=True,
    )
    if margin:
        candidates[margin_driver] = margin
    ratios = [(D.DA_TO_REVENUE, M.DEPRECIATION_AMORTIZATION)]
    if route == OperatingRoute.OPERATING_PROFIT:
        ratios.append((D.OTHER_INCOME_TO_REVENUE, M.OTHER_INCOME))
    for driver, metric in ratios:
        found = derive.spread(
            driver,
            derive.ratio_series(driver, _flow(history, metric), revenue, what="revenue"),
            scenario_range=False,
        )
        if found:
            candidates[driver] = found


def _rate_denominator(history: History, end: date) -> Fact | Missing:
    """Opening gross debt for an interest rate on debt. Finance cost includes lease interest:
    filed borrowings that exclude reported lease liabilities are not its denominator (the rate
    would be overstated), so that year gives no rate; a provider's borrowings that already
    include leases, or a company without leases, are used as they are."""
    debt = history.gross_debt(end)
    if isinstance(debt, Missing) or "INPUT_AGGREGATOR_ONLY" in debt.warnings:
        return debt
    leases = [history.balance([m], end) for m in (M.LEASE_LIABILITIES,
                                                  M.NON_CURRENT_LEASE_LIABILITIES,
                                                  M.CURRENT_LEASE_LIABILITIES)]
    if any(isinstance(x, Fact) and x.value > 0 for x in leases):
        return Missing(CalculationStatus.NOT_APPLICABLE,
                       f"gross debt @ {end} excludes reported lease liabilities while finance "
                       "cost includes lease interest (FINANCE_COST_INCLUDES_LEASE_INTEREST)")
    return debt


def _derive_finance(
    history: History,
    derive: Deriver,
    candidates: dict[Driver, dict[Scenario, Candidate]],
    revenue: Callable[[date], Fact | Missing],
) -> Driver | None:
    rate_obs = derive.ratio_series(
        D.INTEREST_RATE_ON_DEBT,
        lambda e: history.flow([M.FINANCE_COST], e),
        lambda e: _rate_denominator(history, shift(e, -12)),
        what="opening gross debt",
    )
    rate = (
        derive.spread(D.INTEREST_RATE_ON_DEBT, rate_obs, scenario_range=False) if rate_obs else None
    )
    if rate:
        candidates[D.INTEREST_RATE_ON_DEBT] = rate
        return D.INTEREST_RATE_ON_DEBT
    ratio = derive.spread(
        D.FINANCE_COST_TO_REVENUE,
        derive.ratio_series(
            D.FINANCE_COST_TO_REVENUE,
            lambda e: history.flow([M.FINANCE_COST], e),
            revenue,
            what="revenue",
        ),
        scenario_range=False,
    )
    if ratio:
        candidates[D.FINANCE_COST_TO_REVENUE] = ratio
        return D.FINANCE_COST_TO_REVENUE
    return None


def _pbt_bridge(history: History, route: OperatingRoute, end: date, issues: Issues) -> None:
    """Flag a latest-year PBT the modelled lines do not explain (exceptional / associates)."""
    if route != OperatingRoute.OPERATING_PROFIT:
        return
    parts = [
        history.flow([m], end)
        for m in (
            M.PBT,
            M.OPERATING_PROFIT,
            M.OTHER_INCOME,
            M.DEPRECIATION_AMORTIZATION,
            M.FINANCE_COST,
        )
    ]
    if not all(isinstance(p, Fact) for p in parts):
        return
    pbt, op, oi, da, fc = (p.value for p in parts if isinstance(p, Fact))
    residual = pbt - (op + oi - da - fc)
    if abs(residual) > max(BRIDGE_TOLERANCE_ABS, abs(pbt) * BRIDGE_TOLERANCE):
        issues.add(
            "PBT_BRIDGE_RESIDUAL",
            Severity.WARNING,
            f"latest PBT differs from operating profit + other income - D&A - finance cost by "
            f"{residual}: items such as exceptional items are not forecast",
            subject=L.PBT.value,
        )


def _share_check(
    history: History,
    chosen: dict[tuple[Driver, int], ForecastAssumption],
    minority: bool | None,
    end: date,
    scenario: Scenario,
    issues: Issues,
) -> None:
    shares = sorted(
        ((fy, a) for (d, fy), a in chosen.items() if d == D.SHARE_COUNT), key=lambda i: i[0]
    )
    if not shares or minority is not False:
        return
    eps, pat = history.flow(EPS_METRICS, end), history.flow([M.PAT], end)
    if not (isinstance(eps, Fact) and isinstance(pat, Fact)) or eps.value <= 0 or pat.value <= 0:
        return
    with localcontext() as ctx:
        ctx.prec = PRECISION
        implied = pat.value * CRORE / eps.value
        fy, first = shares[0]
        factor = first.value / implied
    if factor > SHARE_COUNT_TOLERANCE or factor < 1 / SHARE_COUNT_TOLERANCE:
        issues.add(
            "SHARE_COUNT_INCONSISTENT_WITH_HISTORY",
            Severity.WARNING,
            f"explicit share count {first.value} vs ~{implied.quantize(Decimal(1))} implied by the "
            "latest reported EPS (shares are stated in ones)",
            scenario=scenario,
            fiscal_year=fy,
            subject=D.SHARE_COUNT.value,
        )


def _scenario_completeness(
    scenario: Scenario, values: Sequence[ForecastValue], assumptions: Iterable[ForecastAssumption],
    key_line: ForecastLine = L.REVENUE,
) -> ScenarioCompleteness:
    lines: dict[ForecastLine, bool] = {}
    for v in values:
        lines[v.line] = lines.get(v.line, True) and v.status == "AVAILABLE"
    available = tuple(sorted((k for k, ok in lines.items() if ok), key=lambda x: x.value))
    unavailable = tuple(sorted((k for k, ok in lines.items() if not ok), key=lambda x: x.value))
    sources = Counter(a.source.value for a in assumptions)
    revenue_ok = lines.get(key_line, False)  # the model's key line
    status = (
        ForecastStatus.UNAVAILABLE
        if not revenue_ok
        else ForecastStatus.COMPLETE
        if not unavailable
        else ForecastStatus.PARTIAL
    )
    return ScenarioCompleteness(
        scenario=scenario,
        status=status,
        lines_available=available,
        lines_unavailable=unavailable,
        sources=dict(sorted(sources.items())),
        fallback_assumptions=sources.get(SRC.FALLBACK.value, 0),
    )


# --- NON_BANK_FINANCIAL (lender) model --------------------------------------------------------


def forecast_model(data: CanonicalFinancialDataset) -> ForecastModel:
    """Box 4's ONE business classification decides the model (never a Box 10 guess)."""
    if data.industry == Industry.BANK:
        return ForecastModel.BANK
    if data.industry == Industry.GENERAL:
        return ForecastModel.NON_FINANCIAL
    if data.classification == BusinessClass.FINANCIAL:
        return ForecastModel.NON_BANK_FINANCIAL
    return ForecastModel.UNKNOWN


def _combined(a: Fact | Missing, b: Fact | Missing, fn: Callable[[Decimal, Decimal], Decimal]
              ) -> Fact | Missing:
    if isinstance(a, Missing):
        return a
    if isinstance(b, Missing):
        return b
    with localcontext() as ctx:
        ctx.prec = PRECISION
        value = fn(a.value, b.value)
    return Fact(value=value, inputs=a.inputs + b.inputs,
                confidence=weakest([a.confidence, b.confidence]),
                warnings=a.warnings + b.warnings)


def lender_anchors(box4_hash: str, basis: ReportingBasis, currency: Currency, history: History,
                   latest: FinancialPeriod, end: date) -> dict[ForecastLine, ForecastValue]:
    """The lender model's actual anchors at the latest fiscal year (balances and flows)."""
    pit = history.periods.pit(end)
    return {
        L.TOTAL_ASSETS: _anchor(box4_hash, basis, currency, L.TOTAL_ASSETS, pit,
                                history.balance([M.TOTAL_ASSETS], end)),
        L.COMMON_EQUITY: _anchor(box4_hash, basis, currency, L.COMMON_EQUITY, pit,
                                 history.common_equity(end)),
        L.TOTAL_BORROWINGS: _anchor(box4_hash, basis, currency, L.TOTAL_BORROWINGS, pit,
                                    history.balance([M.TOTAL_BORROWINGS], end)),
        L.PAT: _anchor(box4_hash, basis, currency, L.PAT, latest, history.flow([M.PAT], end)),
        L.FINANCE_COST: _anchor(box4_hash, basis, currency, L.FINANCE_COST, latest,
                                history.flow([M.FINANCE_COST], end)),
        L.TOTAL_INCOME: _anchor(box4_hash, basis, currency, L.TOTAL_INCOME, latest,
                                history.flow([M.TOTAL_INCOME], end)),
    }


def _derive_lender(history: History, derive: Deriver,
                   candidates: dict[Driver, dict[Scenario, Candidate]]) -> None:
    """Every lender driver from the SAME basis' Box 4 facts at contiguous fiscal years;
    averages need the opening AND the closing balance; a driver without history is absent
    (its lines become ASSUMPTION_MISSING), never a constant."""
    def assets(e: date) -> Fact | Missing:
        return history.balance([M.TOTAL_ASSETS], e)

    def borrowings(e: date) -> Fact | Missing:
        return history.balance([M.TOTAL_BORROWINGS], e)

    def average(get: Callable[[date], Fact | Missing]) -> Callable[[date], Fact | Missing]:
        return lambda e: _combined(get(shift(e, -12)), get(e), lambda a, b: (a + b) / 2)

    pat, finance = _flow(history, M.PAT), _flow(history, M.FINANCE_COST)
    income = _flow(history, M.TOTAL_INCOME)
    growth = derive.growth(D.ASSET_GROWTH, assets, "total assets")
    if growth:
        candidates[D.ASSET_GROWTH] = growth
    series: tuple[tuple[Driver, Callable[[date], Fact | Missing],
                        Callable[[date], Fact | Missing], str, bool], ...] = (
        (D.ROA, pat, average(assets), "average total assets", True),
        (D.EQUITY_CHANGE_TO_PAT,
         lambda e: _combined(history.common_equity(e), history.common_equity(shift(e, -12)),
                             lambda a, b: a - b), pat, "PAT", False),
        (D.BORROWINGS_SHARE_OF_FUNDING, borrowings,
         lambda e: _combined(assets(e), history.common_equity(e), lambda a, b: a - b),
         "total assets - ordinary equity", False),
        (D.COST_OF_BORROWINGS, finance, average(borrowings), "average borrowings", False),
        (D.TOTAL_INCOME_TO_AVERAGE_ASSETS, income, average(assets), "average total assets",
         False),
    )
    for driver, numerator, denominator, what, scenarios in series:
        extra = (lambda _e: ("COST_OF_BORROWINGS_PROXY",)) \
            if driver == D.COST_OF_BORROWINGS else None
        found = derive.spread(driver, derive.ratio_series(
            driver, numerator, denominator, what=what, extra_warnings=extra),
            scenario_range=scenarios)
        if found:
            candidates[driver] = found
