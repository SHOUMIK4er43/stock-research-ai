"""DETAILED_RESEARCH_V1 Box 5: deterministic financial calculations over Box 4 canonical data.

One _Frame = one reporting basis in one currency. A value is looked up by (metric, exact
Box 4 period key), so standalone/consolidated, quarter/YTD, FY/TTM and currencies can never
meet. Every calculation goes through _Engine.emit, which records the formula/version, the
exact canonical inputs (with their Box 4 lineage), the weakest confidence, propagated
reconciliation warnings, and an explicit status when no number can honestly be produced.

Formula conventions (all versioned in SPECS):
  * growth / margins / returns are PERCENT (12.5 = 12.5 %); multiples are TIMES; DAYS = 365
    for a fiscal year; margin changes are BASIS_POINTS.
  * averages use opening and closing balance-sheet values; when the opening balance is
    missing the closing balance is used, flagged ENDING_BALANCE_USED (confidence <= MEDIUM).
  * nothing missing is ever treated as zero.
"""

import calendar
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any
from uuid import UUID, uuid5

from stock_research_ai.application.financial_parsing import period_from
from stock_research_ai.application.numerics import root_growth
from stock_research_ai.domain.financial_analytics import (
    DERIVED_QUANTUM,
    AnalyticsUnit,
    CalculationStatus,
    DerivedMetric,
    DerivedMetricInput,
    MetricCategory,
)
from stock_research_ai.domain.financial_canonical import (
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    Outcome,
    PeriodType,
    ReconciliationRecord,
    ReportingBasis,
    ValueKind,
)

M = CanonicalMetric
S = CalculationStatus
U = AnalyticsUnit
C = MetricCategory
NS = UUID("b6f1d2a4-3c5e-4f7a-8b9c-0d1e2f3a4b5c")
FINANCIAL_INDUSTRIES = {Industry.BANK, Industry.NBFC, Industry.INSURANCE}
#: A SOURCE_PRECEDENCE input whose losing source differs by more than this share of the
#: selected value is a material conflict: the metric is still calculated, flagged
#: MATERIAL_INPUT_CONFLICT, and its confidence drops to LOW.
MATERIAL_CONFLICT_SHARE = Decimal("0.10")
CONFIDENCE_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}
#: When several inputs fail, the reported status is the first in this order.
STATUS_PRIORITY = [
    S.INVALID_INPUT,
    S.INCOMPATIBLE_BASIS,
    S.INCOMPATIBLE_CURRENCY,
    S.INCOMPATIBLE_PERIODS,
    S.CONFLICTED_INPUT,
    S.LOW_CONFIDENCE_INPUT,
    S.UNSUPPORTED_INDUSTRY,
    S.NOT_APPLICABLE,
    S.ZERO_DENOMINATOR,
    S.INSUFFICIENT_DATA,
]
REVENUE = (M.REVENUE_FROM_OPERATIONS, M.REVENUE)
EPS = (M.EPS, M.DILUTED_EPS, M.BASIC_EPS)
FIXED = (M.PROPERTY_PLANT_EQUIPMENT, M.FIXED_ASSETS)


@dataclass(frozen=True)
class Spec:
    name: str
    category: MetricCategory
    unit: AnalyticsUnit
    formula: str
    version: int = 1
    banks: bool = False  # also meaningful for banks / NBFCs / insurers


def _s(
    name: str, cat: MetricCategory, unit: AnalyticsUnit, formula: str, banks: bool = False
) -> Spec:
    return Spec(name, cat, unit, formula, banks=banks)


BANK_GROWTH = {
    "REVENUE",
    "PBT",
    "PAT",
    "EPS",
    "TOTAL_ASSETS",
    "EQUITY",
    "DEPOSITS",
    "ADVANCES",
    "SHARE_CAPITAL",
}
SPECS: dict[str, Spec] = {
    # --- building blocks -----------------------------------------------------------------
    "EBITDA": _s("EBITDA", C.BASE, U.MONEY, "reported EBITDA; else PBT + finance cost + D&A"),
    "EBIT": _s("EBIT", C.BASE, U.MONEY, "reported EBIT; else PBT + finance cost"),
    "NOPAT": _s("NOPAT", C.BASE, U.MONEY, "EBIT x (1 - effective tax rate)"),
    "CAPEX": _s("Capital expenditure", C.BASE, U.MONEY, "|CAPEX| else |purchase of PPE|"),
    "FCF": _s("Free cash flow", C.BASE, U.MONEY, "CFO - capex (FCF/1, before leases)"),
    "COGS": _s(
        "Cost of goods sold",
        C.BASE,
        U.MONEY,
        "raw materials + purchases of stock-in-trade + change in inventories (all three)",
    ),
    "SHAREHOLDERS_EQUITY": _s(
        "Shareholders' equity",
        C.BASE,
        U.MONEY,
        "equity share capital + reserves (or other equity); else total equity",
        banks=True,
    ),
    "GROSS_DEBT": _s(
        "Gross debt",
        C.BASE,
        U.MONEY,
        "total debt; else total borrowings; else long-term + short-term borrowings",
    ),
    "NET_DEBT": _s("Net debt", C.BASE, U.MONEY, "gross debt - cash and cash equivalents"),
    "CAPITAL_EMPLOYED": _s("Capital employed", C.BASE, U.MONEY, "equity + gross debt"),
    "INVESTED_CAPITAL": _s(
        "Invested capital", C.BASE, U.MONEY, "equity + gross debt - cash and cash equivalents"
    ),
    "NET_WORKING_CAPITAL": _s(
        "Net working capital", C.BASE, U.MONEY, "total current assets - total current liabilities"
    ),
    "TOTAL_ACCRUALS": _s("Total accruals", C.ACCRUALS, U.MONEY, "PAT - CFO"),
    # --- growth ----------------------------------------------------------------------------
    **{
        f"{name}_GROWTH_YOY": _s(
            f"{label} growth (YoY)",
            C.GROWTH,
            U.PERCENT,
            "(current / same period one year earlier - 1) x 100; base must be > 0",
            banks=name in BANK_GROWTH,
        )
        for name, label in (
            ("REVENUE", "Revenue"),
            ("OPERATING_PROFIT", "Operating profit (Screener definition)"),
            ("EBITDA", "EBITDA"),
            ("PBT", "PBT"),
            ("PAT", "PAT"),
            ("EPS", "EPS"),
            ("CFO", "CFO"),
            ("FCF", "FCF"),
            ("TOTAL_ASSETS", "Total assets"),
            ("EQUITY", "Equity"),
            ("GROSS_DEBT", "Gross debt"),
            ("RECEIVABLES", "Trade receivables"),
            ("INVENTORY", "Inventory"),
            ("PAYABLES", "Trade payables"),
            ("CAPEX", "Capex"),
            ("SHARE_CAPITAL", "Equity share capital"),
            ("DEPOSITS", "Deposits"),
            ("ADVANCES", "Advances"),
        )
    },
    **{
        f"{name}_GROWTH_QOQ": _s(
            f"{label} growth (QoQ, not seasonally adjusted)",
            C.GROWTH,
            U.PERCENT,
            "(quarter / previous quarter - 1) x 100; base must be > 0",
            banks=True,
        )
        for name, label in (("REVENUE", "Revenue"), ("PAT", "PAT"))
    },
    **{
        f"{name}_CAGR_{years}Y": _s(
            f"{label} {years}-year CAGR",
            C.GROWTH,
            U.PERCENT,
            f"((end / start) ^ (1/{years}) - 1) x 100; both endpoints must be > 0",
            banks=name
            in ("REVENUE", "PAT", "EPS", "TOTAL_ASSETS", "EQUITY", "DEPOSITS", "ADVANCES"),
        )
        for name, label in (
            ("REVENUE", "Revenue"),
            ("OPERATING_PROFIT", "Operating profit"),
            ("EBITDA", "EBITDA"),
            ("PBT", "PBT"),
            ("PAT", "PAT"),
            ("EPS", "EPS"),
            ("CFO", "CFO"),
            ("FCF", "FCF"),
            ("TOTAL_ASSETS", "Total assets"),
            ("EQUITY", "Equity"),
            ("DEPOSITS", "Deposits"),
            ("ADVANCES", "Advances"),
        )
        for years in (3, 5, 10)
    },
    # --- margins ---------------------------------------------------------------------------
    "GROSS_MARGIN": _s("Gross margin", C.MARGIN, U.PERCENT, "(revenue - COGS) / revenue x 100"),
    "EBITDA_MARGIN": _s("EBITDA margin", C.MARGIN, U.PERCENT, "EBITDA / revenue x 100"),
    "OPERATING_MARGIN": _s(
        "Operating margin (Screener operating profit; not EBITDA)",
        C.MARGIN,
        U.PERCENT,
        "OPERATING_PROFIT / revenue x 100",
    ),
    "EBIT_MARGIN": _s("EBIT margin", C.MARGIN, U.PERCENT, "EBIT / revenue x 100"),
    "PBT_MARGIN": _s("PBT margin", C.MARGIN, U.PERCENT, "PBT / revenue x 100", banks=True),
    "PAT_MARGIN": _s("PAT margin", C.MARGIN, U.PERCENT, "PAT / revenue x 100", banks=True),
    "CFO_MARGIN": _s("CFO margin", C.MARGIN, U.PERCENT, "CFO / revenue x 100"),
    "FCF_MARGIN": _s("FCF margin", C.MARGIN, U.PERCENT, "FCF / revenue x 100"),
    "EMPLOYEE_COST_TO_REVENUE": _s(
        "Employee cost / revenue", C.MARGIN, U.PERCENT, "employee cost / revenue x 100", banks=True
    ),
    "RAW_MATERIAL_COST_TO_REVENUE": _s(
        "Raw-material cost / revenue", C.MARGIN, U.PERCENT, "raw materials / revenue x 100"
    ),
    "POWER_AND_FUEL_TO_REVENUE": _s(
        "Power and fuel / revenue", C.MARGIN, U.PERCENT, "power and fuel / revenue x 100"
    ),
    "OTHER_EXPENSES_TO_REVENUE": _s(
        "Other expenses / revenue", C.MARGIN, U.PERCENT, "other expenses / revenue x 100"
    ),
    "DEPRECIATION_TO_REVENUE": _s(
        "Depreciation / revenue", C.MARGIN, U.PERCENT, "D&A / revenue x 100"
    ),
    "FINANCE_COST_TO_REVENUE": _s(
        "Finance cost / revenue", C.MARGIN, U.PERCENT, "finance cost / revenue x 100"
    ),
    "OTHER_INCOME_TO_PBT": _s(
        "Other income / PBT", C.MARGIN, U.PERCENT, "other income / PBT x 100; PBT must be > 0"
    ),
    "EXCEPTIONAL_TO_PBT": _s(
        "Exceptional items / PBT",
        C.MARGIN,
        U.PERCENT,
        "|exceptional items| / PBT x 100; PBT must be > 0",
        banks=True,
    ),
    "TAX_RATE": _s(
        "Effective tax rate",
        C.MARGIN,
        U.PERCENT,
        "total tax / PBT; else (current + deferred tax) / PBT; else (PBT - PAT) / PBT [implied]",
        banks=True,
    ),
    "TAX_RATE_CHANGE_YOY": _s(
        "Effective tax rate change (YoY)",
        C.MARGIN,
        U.PERCENTAGE_POINTS,
        "tax rate - tax rate one year earlier",
        banks=True,
    ),
    "TAX_RATE_RANGE_5Y": _s(
        "Effective tax rate range over 5 years",
        C.MARGIN,
        U.PERCENTAGE_POINTS,
        "max - min effective tax rate over the last 5 fiscal years (>= 3 calculated)",
        banks=True,
    ),
    **{
        f"{name}_CHANGE_YOY": _s(
            f"{name.replace('_', ' ').title()} change (YoY)",
            C.MARGIN,
            U.BASIS_POINTS,
            "(margin - margin one year earlier) x 100",
            banks=name in ("PAT_MARGIN", "PBT_MARGIN"),
        )
        for name in ("EBITDA_MARGIN", "OPERATING_MARGIN", "PBT_MARGIN", "PAT_MARGIN")
    },
    **{
        f"{name}_CHANGE_{years}Y": _s(
            f"{name.replace('_', ' ').title()} change over {years} years",
            C.MARGIN,
            U.BASIS_POINTS,
            f"(margin - margin {years} years earlier) x 100",
            banks=name in ("PAT_MARGIN", "PBT_MARGIN"),
        )
        for name in ("EBITDA_MARGIN", "OPERATING_MARGIN", "PBT_MARGIN", "PAT_MARGIN")
        for years in (3, 5)
    },
    "OTHER_INCOME_TO_PBT_CHANGE_YOY": _s(
        "Other income / PBT change (YoY)",
        C.MARGIN,
        U.PERCENTAGE_POINTS,
        "other income / PBT - same ratio one year earlier",
    ),
    # --- returns ---------------------------------------------------------------------------
    "ROE": _s("Return on equity", C.RETURN, U.PERCENT, "PAT / average equity x 100", banks=True),
    "ROA": _s(
        "Return on assets", C.RETURN, U.PERCENT, "PAT / average total assets x 100", banks=True
    ),
    "ROCE": _s(
        "Return on capital employed", C.RETURN, U.PERCENT, "EBIT / average capital employed x 100"
    ),
    "ROIC": _s(
        "Return on invested capital",
        C.RETURN,
        U.PERCENT,
        "NOPAT / average invested capital x 100 (invested capital = equity + gross debt - cash)",
    ),
    "RETURN_ON_TANGIBLE_CAPITAL": _s(
        "Return on tangible capital employed",
        C.RETURN,
        U.PERCENT,
        "EBIT / average (capital employed - goodwill - intangible assets) x 100",
    ),
    "ASSET_TURNOVER": _s("Asset turnover", C.RETURN, U.TIMES, "revenue / average total assets"),
    "FIXED_ASSET_TURNOVER": _s(
        "Fixed-asset turnover", C.RETURN, U.TIMES, "revenue / average PPE (or fixed assets)"
    ),
    "INCREMENTAL_ROIC_3Y": _s(
        "Incremental ROIC over 3 years",
        C.RETURN,
        U.PERCENT,
        "(NOPAT_t - NOPAT_t-3) / (invested capital_t - invested capital_t-3) x 100; "
        "the capital increase must be > 0",
    ),
    "ROE_CHANGE_YOY": _s(
        "ROE change (YoY)", C.RETURN, U.PERCENTAGE_POINTS, "ROE - ROE one year earlier", banks=True
    ),
    # --- DuPont ----------------------------------------------------------------------------
    "DUPONT_NET_MARGIN": _s("DuPont net profit margin", C.DUPONT, U.PERCENT, "PAT / revenue x 100"),
    "DUPONT_ASSET_TURNOVER": _s(
        "DuPont asset turnover", C.DUPONT, U.TIMES, "revenue / average total assets"
    ),
    "DUPONT_EQUITY_MULTIPLIER": _s(
        "DuPont equity multiplier", C.DUPONT, U.TIMES, "average total assets / average equity"
    ),
    "DUPONT_ROE": _s(
        "DuPont ROE",
        C.DUPONT,
        U.PERCENT,
        "net margin x asset turnover x equity multiplier (equals ROE by construction)",
    ),
    **{
        f"{name}_CHANGE_YOY": _s(
            f"{name.replace('_', ' ').title()} change (YoY)",
            C.DUPONT,
            unit,
            "driver - same driver one year earlier",
        )
        for name, unit in (
            ("DUPONT_NET_MARGIN", U.PERCENTAGE_POINTS),
            ("DUPONT_ASSET_TURNOVER", U.TIMES),
            ("DUPONT_EQUITY_MULTIPLIER", U.TIMES),
        )
    },
    # --- leverage & solvency -----------------------------------------------------------------
    "DEBT_TO_EQUITY": _s("Debt / equity", C.LEVERAGE, U.TIMES, "gross debt / equity"),
    "DEBT_TO_ASSETS": _s("Debt / assets", C.LEVERAGE, U.TIMES, "gross debt / total assets"),
    "NET_DEBT_TO_EQUITY": _s(
        "Net debt / equity", C.LEVERAGE, U.TIMES, "net debt / equity (negative = net cash)"
    ),
    "DEBT_TO_EBITDA": _s("Debt / EBITDA", C.LEVERAGE, U.TIMES, "gross debt / EBITDA; EBITDA > 0"),
    "NET_DEBT_TO_EBITDA": _s(
        "Net debt / EBITDA",
        C.LEVERAGE,
        U.TIMES,
        "net debt / EBITDA; EBITDA > 0; not a multiple when the company holds net cash",
    ),
    "INTEREST_COVERAGE": _s("Interest coverage", C.LEVERAGE, U.TIMES, "EBIT / finance cost"),
    "FINANCE_COST_COVERAGE": _s(
        "Finance-cost coverage (EBITDA)", C.LEVERAGE, U.TIMES, "EBITDA / finance cost"
    ),
    "DEBT_TO_CFO": _s("Debt / CFO", C.LEVERAGE, U.TIMES, "gross debt / CFO; CFO > 0"),
    "CFO_TO_DEBT": _s("CFO / debt", C.LEVERAGE, U.TIMES, "CFO / gross debt"),
    "CASH_TO_DEBT": _s(
        "Cash / debt", C.LEVERAGE, U.TIMES, "cash and cash equivalents / gross debt"
    ),
    "EQUITY_TO_ASSETS": _s(
        "Equity / assets", C.LEVERAGE, U.PERCENT, "equity / total assets x 100", banks=True
    ),
    # --- liquidity -------------------------------------------------------------------------
    "CURRENT_RATIO": _s(
        "Current ratio", C.LIQUIDITY, U.TIMES, "total current assets / total current liabilities"
    ),
    "QUICK_RATIO": _s(
        "Quick ratio",
        C.LIQUIDITY,
        U.TIMES,
        "(total current assets - inventory) / total current liabilities (inventory required)",
    ),
    "CASH_RATIO": _s(
        "Cash ratio", C.LIQUIDITY, U.TIMES, "cash and cash equivalents / total current liabilities"
    ),
    # --- working capital -------------------------------------------------------------------
    "RECEIVABLE_DAYS": _s(
        "Receivable days", C.WORKING_CAPITAL, U.DAYS, "average trade receivables / revenue x 365"
    ),
    "INVENTORY_DAYS": _s(
        "Inventory days",
        C.WORKING_CAPITAL,
        U.DAYS,
        "average inventory / COGS x 365 (COGS required)",
    ),
    "PAYABLE_DAYS": _s(
        "Payable days",
        C.WORKING_CAPITAL,
        U.DAYS,
        "average trade payables / COGS x 365 (COGS required)",
    ),
    "CASH_CONVERSION_CYCLE": _s(
        "Cash conversion cycle",
        C.WORKING_CAPITAL,
        U.DAYS,
        "receivable days + inventory days - payable days (all three required)",
    ),
    "RECEIVABLE_DAYS_CHANGE_YOY": _s(
        "Receivable days change (YoY)", C.WORKING_CAPITAL, U.DAYS, "days - days one year earlier"
    ),
    "NWC_TO_REVENUE": _s(
        "Net working capital / revenue", C.WORKING_CAPITAL, U.PERCENT, "NWC / revenue x 100"
    ),
    "NWC_CHANGE": _s(
        "Net working capital change", C.WORKING_CAPITAL, U.MONEY, "NWC - NWC one year earlier"
    ),
    "RECEIVABLES_TO_REVENUE": _s(
        "Receivables / revenue", C.WORKING_CAPITAL, U.PERCENT, "closing receivables / revenue x 100"
    ),
    "INVENTORY_TO_REVENUE": _s(
        "Inventory / revenue", C.WORKING_CAPITAL, U.PERCENT, "closing inventory / revenue x 100"
    ),
    **{
        f"{name}_VS_REVENUE_GROWTH": _s(
            f"{name.title()} growth minus revenue growth",
            C.WORKING_CAPITAL,
            U.PERCENTAGE_POINTS,
            f"{name.lower()} YoY growth - revenue YoY growth",
        )
        for name in ("RECEIVABLES", "INVENTORY", "PAYABLES")
    },
    # --- cash flow -------------------------------------------------------------------------
    "CFO_TO_PAT": _s("CFO / PAT", C.CASH_FLOW, U.TIMES, "CFO / PAT; PAT > 0"),
    "CFO_TO_EBITDA": _s("CFO / EBITDA", C.CASH_FLOW, U.TIMES, "CFO / EBITDA; EBITDA > 0"),
    "CFO_TO_PBT": _s("CFO / PBT", C.CASH_FLOW, U.TIMES, "CFO / PBT; PBT > 0"),
    "FCF_TO_PAT": _s("FCF / PAT", C.CASH_FLOW, U.TIMES, "FCF / PAT; PAT > 0"),
    "FCF_TO_EBITDA": _s("FCF / EBITDA", C.CASH_FLOW, U.TIMES, "FCF / EBITDA; EBITDA > 0"),
    "CAPEX_TO_REVENUE": _s("Capex / revenue", C.CASH_FLOW, U.PERCENT, "capex / revenue x 100"),
    "CAPEX_TO_CFO": _s("Capex / CFO", C.CASH_FLOW, U.TIMES, "capex / CFO; CFO > 0"),
    "DIVIDENDS_TO_CFO": _s(
        "Dividends paid / CFO", C.CASH_FLOW, U.TIMES, "|dividends paid| / CFO; CFO > 0"
    ),
    "BUYBACK_TO_CFO": _s("Buyback / CFO", C.CASH_FLOW, U.TIMES, "|buyback| / CFO; CFO > 0"),
    "CFO_TO_PAT_CHANGE_YOY": _s(
        "CFO / PAT change (YoY)", C.CASH_FLOW, U.TIMES, "CFO/PAT - CFO/PAT one year earlier"
    ),
    "PAT_VS_CFO_GROWTH_GAP": _s(
        "PAT growth minus CFO growth",
        C.CASH_FLOW,
        U.PERCENTAGE_POINTS,
        "PAT YoY growth - CFO YoY growth",
    ),
    **{
        f"CUMULATIVE_CFO_TO_PAT_{years}Y": _s(
            f"Cumulative CFO / cumulative PAT over {years} years",
            C.CASH_FLOW,
            U.TIMES,
            f"sum of CFO / sum of PAT over the last {years} consecutive fiscal years "
            "(all required)",
        )
        for years in (3, 5)
    },
    **{
        f"CUMULATIVE_FCF_{years}Y": _s(
            f"Cumulative FCF over {years} years",
            C.CASH_FLOW,
            U.MONEY,
            f"sum of FCF over the last {years} consecutive fiscal years (all required)",
        )
        for years in (3, 5)
    },
    # --- accruals --------------------------------------------------------------------------
    "ACCRUAL_RATIO": _s(
        "Accrual ratio (cash-flow based)",
        C.ACCRUALS,
        U.PERCENT,
        "(PAT - CFO) / average total assets x 100",
    ),
    "BALANCE_SHEET_ACCRUAL_RATIO": _s(
        "Accrual ratio (balance-sheet based)",
        C.ACCRUALS,
        U.PERCENT,
        "((d current assets - d cash) - d current liabilities - D&A) / average total assets x 100",
    ),
    "ACCRUAL_RATIO_CHANGE_YOY": _s(
        "Accrual ratio change (YoY)",
        C.ACCRUALS,
        U.PERCENTAGE_POINTS,
        "accrual ratio - accrual ratio one year earlier",
    ),
    # --- forensic building blocks ----------------------------------------------------------
    "DEPRECIATION_RATE": _s(
        "Depreciation rate", C.ACCRUALS, U.PERCENT, "D&A / average PPE (or fixed assets) x 100"
    ),
    "DEPRECIATION_RATE_CHANGE_YOY": _s(
        "Depreciation rate change (YoY)",
        C.ACCRUALS,
        U.PERCENTAGE_POINTS,
        "depreciation rate - depreciation rate one year earlier",
    ),
    "CWIP_TO_FIXED_ASSETS": _s(
        "CWIP / fixed assets", C.ACCRUALS, U.PERCENT, "CWIP / (PPE or fixed assets) x 100"
    ),
    "ASSET_MIX_SHIFT": _s(
        "Largest one-year shift in asset mix",
        C.ACCRUALS,
        U.PERCENTAGE_POINTS,
        "max |share of total assets now - one year earlier| over receivables, inventory, "
        "cash, investments, fixed assets (components present in both years)",
    ),
    # --- banking ---------------------------------------------------------------------------
    "COST_TO_INCOME": _s(
        "Cost-to-income ratio",
        C.BANKING,
        U.PERCENT,
        "bank operating expenses / (net interest income + other income) x 100",
        banks=True,
    ),
    "FINANCING_PROFIT_MARGIN": _s(
        "Financing profit / revenue (Screener financing profit)",
        C.BANKING,
        U.PERCENT,
        "BANK_FINANCING_PROFIT / revenue x 100",
        banks=True,
    ),
    "PROVISIONS_TO_REVENUE": _s(
        "Provisions and contingencies / revenue",
        C.BANKING,
        U.PERCENT,
        "provisions and contingencies / revenue x 100",
        banks=True,
    ),
    "GROSS_NPA_RATIO": _s(
        "Gross NPA ratio",
        C.BANKING,
        U.PERCENT,
        "reported gross NPA % (not part of the Box 4 canonical taxonomy)",
        banks=True,
    ),
    "NET_NPA_RATIO": _s(
        "Net NPA ratio",
        C.BANKING,
        U.PERCENT,
        "reported net NPA % (not part of the Box 4 canonical taxonomy)",
        banks=True,
    ),
}
BANK_ONLY = {
    "COST_TO_INCOME",
    "FINANCING_PROFIT_MARGIN",
    "PROVISIONS_TO_REVENUE",
    "GROSS_NPA_RATIO",
    "NET_NPA_RATIO",
    "DEPOSITS_GROWTH_YOY",
    "ADVANCES_GROWTH_YOY",
}


# --- facts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    value: Decimal
    inputs: tuple[DerivedMetricInput, ...]
    confidence: Confidence
    warnings: tuple[str, ...] = ()
    derived_from: tuple[UUID, ...] = ()
    formula_id: str | None = None


@dataclass(frozen=True)
class Missing:
    status: CalculationStatus
    what: str
    inputs: tuple[DerivedMetricInput, ...] = ()
    derived_from: tuple[UUID, ...] = ()


Got = Fact | Missing


@dataclass(frozen=True)
class Refused:
    """A calculation that has inputs but no meaningful number (zero or negative base ...)."""

    status: CalculationStatus
    interpretation: str | None = None


Values = dict[str, Decimal]
Formula = Callable[[Values], "Decimal | Refused"]


def weakest(confidences: Iterable[Confidence]) -> Confidence:
    return max(confidences, key=lambda c: CONFIDENCE_RANK[c], default=Confidence.HIGH)


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def shift(end: date, months: int) -> date:
    """The month-end `months` months away (quarter and fiscal year ends are month-ends)."""
    total = end.year * 12 + end.month - 1 + months
    return month_end(total // 12, total % 12 + 1)


def compare_periods(
    current: FinancialPeriod, prior: FinancialPeriod, months: int
) -> CalculationStatus | None:
    """None when `prior` is the same kind of period exactly `months` earlier."""
    if (
        current.period_type != prior.period_type
        or current.duration_months != prior.duration_months
        or current.period_type in (PeriodType.TTM, PeriodType.UNKNOWN, PeriodType.OTHER)
        or current.period_end is None
        or prior.period_end is None
        or shift(current.period_end, -months) != prior.period_end
    ):
        return S.INCOMPATIBLE_PERIODS
    return None


class Periods:
    """Normalised analytical periods; their keys equal the Box 4 period keys."""

    def __init__(self, fy_end_month: int | None) -> None:
        self.fy_end = fy_end_month

    def fiscal_year(self, end: date) -> int:
        if self.fy_end is None:
            return end.year
        return end.year if end.month <= self.fy_end else end.year + 1

    def fy(self, end: date) -> FinancialPeriod:
        return period_from(PeriodType.FY, 12, end, f"FY{self.fiscal_year(end)}", self.fy_end)

    def quarter(self, end: date) -> FinancialPeriod:
        period = period_from(PeriodType.QUARTER, 3, end, "quarter", self.fy_end)
        label = (
            f"Q{period.fiscal_quarter} FY{period.fiscal_year}"
            if period.fiscal_quarter
            else f"Quarter ended {end.isoformat()}"
        )
        return period.model_copy(update={"label": label})

    def pit(self, end: date) -> FinancialPeriod:
        return period_from(
            PeriodType.POINT_IN_TIME, None, end, f"As at {end.isoformat()}", self.fy_end
        )

    @staticmethod
    def ttm() -> FinancialPeriod:
        return FinancialPeriod(period_type=PeriodType.TTM, label="TTM", duration_months=12)


class Frame:
    """Box 4 canonical values for one basis in one currency, indexed by exact period."""

    def __init__(
        self,
        data: CanonicalFinancialDataset,
        basis: ReportingBasis,
        currency: Currency,
        max_values: int,
    ) -> None:
        self.basis = basis
        self.currency = currency
        self.truncated = False
        sources = {o.observation_id: o.source.source_type for o in data.observations}
        self.records: dict[UUID, ReconciliationRecord] = {
            r.record_id: r for r in data.reconciliation
        }
        self.values: dict[tuple[CanonicalMetric, str], CanonicalFinancialValue] = {}
        self.other_currency: set[tuple[CanonicalMetric, str]] = set()
        consumed = 0
        for value in sorted(data.canonical_values, key=lambda v: str(v.canonical_value_id)):
            if value.reporting_basis != basis or value.value_kind not in (
                ValueKind.MONEY,
                ValueKind.PER_SHARE,
            ):
                continue
            key = (value.metric, value.period.key())
            if value.currency != currency:
                self.other_currency.add(key)
                continue
            if consumed >= max_values:
                self.truncated = True
                continue
            consumed += 1
            self.values[key] = value
        self.consumed = consumed
        self.unresolved: dict[tuple[CanonicalMetric, str], ReconciliationRecord] = {
            (r.metric, r.period.key()): r
            for r in data.reconciliation
            if r.reporting_basis == basis
            and r.selected_observation_id is None
            and r.currency in (currency, Currency.UNKNOWN)
        }
        self.sources = sources

    def periods(self, period_type: PeriodType) -> list[date]:
        ends = {
            v.period.period_end
            for v in self.values.values()
            if v.period.period_type == period_type and v.period.period_end is not None
        }
        return sorted(ends)

    @property
    def has_ttm(self) -> bool:
        return any(v.period.period_type == PeriodType.TTM for v in self.values.values())

    def get(self, metrics: Sequence[CanonicalMetric], period: FinancialPeriod) -> Got:
        key = period.key()
        for metric in metrics:
            value = self.values.get((metric, key))
            if value is not None:
                return self._fact(value)
        names = "/".join(m.value for m in metrics)
        what = f"{names} @ {period.label}"
        for metric in metrics:
            record = self.unresolved.get((metric, key))
            if record is not None and record.outcome in (Outcome.CONFLICT, Outcome.AMBIGUOUS):
                return Missing(S.CONFLICTED_INPUT, f"{what} (Box 4 {record.outcome.value})")
            if record is not None:
                return Missing(S.LOW_CONFIDENCE_INPUT, f"{what} (Box 4 {record.outcome.value})")
        if any((metric, key) in self.other_currency for metric in metrics):
            return Missing(S.INCOMPATIBLE_CURRENCY, f"{what} (only in another currency)")
        return Missing(S.INSUFFICIENT_DATA, f"{what} (not reported)")

    def _fact(self, value: CanonicalFinancialValue) -> Got:
        if value.confidence == Confidence.LOW:
            return Missing(S.LOW_CONFIDENCE_INPUT, f"{value.metric.value} @ {value.period.label}")
        warnings: list[str] = []
        confidence: Confidence = value.confidence
        if value.outcome in (Outcome.SOURCE_PRECEDENCE, Outcome.RESTATEMENT):
            warnings.append(f"INPUT_{value.outcome.value}")
            confidence = weakest([confidence, Confidence.MEDIUM])
            record = self.records.get(value.reconciliation_record_id)
            if (
                record is not None
                and record.max_difference is not None
                and value.value != 0
                and record.max_difference / abs(value.value) > MATERIAL_CONFLICT_SHARE
            ):
                warnings.append("MATERIAL_INPUT_CONFLICT")
                confidence = Confidence.LOW
        source = self.sources[value.selected_observation_id]
        return Fact(
            value=value.value,
            inputs=(
                DerivedMetricInput(
                    role="",
                    canonical_value_id=value.canonical_value_id,
                    metric=value.metric,
                    value=value.value,
                    value_kind=value.value_kind,
                    currency=value.currency,
                    period=value.period,
                    reporting_basis=value.reporting_basis,
                    outcome=value.outcome,
                    confidence=value.confidence,
                    reconciliation_record_id=value.reconciliation_record_id,
                    selected_observation_id=value.selected_observation_id,
                    selected_source=source,
                    supporting_observation_ids=value.supporting_observation_ids,
                    conflicting_observation_ids=value.conflicting_observation_ids,
                ),
            ),
            confidence=confidence,
            warnings=tuple(warnings),
        )


def first_available(*gots: Got) -> Got:
    """The first fact; when all are missing, the most informative failure."""
    for got in gots:
        if isinstance(got, Fact):
            return got
    missing = [g for g in gots if isinstance(g, Missing)]
    return min(missing, key=lambda m: STATUS_PRIORITY.index(m.status))


def quantize(value: Decimal) -> Decimal:
    return value.quantize(DERIVED_QUANTUM, rounding=ROUND_HALF_EVEN)


def ratio(numerator: Decimal, denominator: Decimal, scale: int = 1) -> Decimal | Refused:
    if denominator == 0:
        return Refused(S.ZERO_DENOMINATOR)
    return numerator * scale / denominator


def positive_ratio(
    numerator: Decimal, denominator: Decimal, scale: int = 1, what: str = "DENOMINATOR"
) -> Decimal | Refused:
    if denominator == 0:
        return Refused(S.ZERO_DENOMINATOR)
    if denominator < 0:
        return Refused(S.NOT_APPLICABLE, f"NEGATIVE_{what}")
    return numerator * scale / denominator


def growth(current: Decimal, prior: Decimal) -> Decimal | Refused:
    if prior == 0:
        return Refused(S.ZERO_DENOMINATOR, "ZERO_BASE")
    if prior < 0:
        return Refused(S.NOT_APPLICABLE, "NEGATIVE_BASE")
    return (current / prior - 1) * 100


def cagr(end: Decimal, start: Decimal, years: int) -> Decimal | Refused:
    if start == 0:
        return Refused(S.ZERO_DENOMINATOR, "ZERO_START")
    if start < 0 or end <= 0:
        return Refused(S.NOT_APPLICABLE, "NON_POSITIVE_ENDPOINT")
    with localcontext() as ctx:
        ctx.prec = 34
        return root_growth(end / start, Decimal(years)) * 100


def average(v: Values, name: str) -> Decimal:
    close = v[f"{name}_close"]
    opening = v.get(f"{name}_open")
    return close if opening is None else (close + opening) / 2


# --- the engine --------------------------------------------------------------------------------


@dataclass
class Engine:
    frame: Frame
    periods: Periods
    industry: Industry
    analytics_key: str
    latest_fy: FinancialPeriod | None = None
    metrics: dict[UUID, DerivedMetric] = field(default_factory=dict)
    index: dict[tuple[str, str], DerivedMetric] = field(default_factory=dict)

    @property
    def financial(self) -> bool:
        return self.industry in FINANCIAL_INDUSTRIES

    def applicable(self, code: str) -> bool:
        if self.financial:
            return SPECS[code].banks
        return code not in BANK_ONLY

    def find(self, code: str, period: FinancialPeriod) -> DerivedMetric | None:
        return self.index.get((code, period.key()))

    def got(self, code: str, period: FinancialPeriod) -> Got:
        metric = self.find(code, period)
        if metric is None:
            return Missing(S.INSUFFICIENT_DATA, f"{code} @ {period.label} (not derived)")
        if metric.value is None or metric.confidence is None:
            return Missing(
                metric.status,
                f"{code} @ {period.label} ({metric.status.value})",
                metric.inputs,
                (metric.metric_id,),
            )
        return Fact(
            metric.value,
            metric.inputs,
            metric.confidence,
            metric.warnings,
            (metric.metric_id,),
            metric.formula_id,
        )

    def canonical(self, metrics: Sequence[CanonicalMetric], period: FinancialPeriod) -> Got:
        return self.frame.get(metrics, period)

    def emit(
        self,
        code: str,
        period: FinancialPeriod,
        parts: Sequence[tuple[str, Got]],
        formula: Formula,
        *,
        comparison: FinancialPeriod | None = None,
        variant: str | None = None,
        warnings: Sequence[str] = (),
        cap: Confidence | None = None,
        missing_note: str | None = None,
    ) -> DerivedMetric:
        spec = SPECS[code]
        formula_id = f"{code}/{spec.version}" + (f"/{variant}" if variant else "")
        metric_id = uuid5(
            NS,
            f"{self.analytics_key}|{self.frame.basis.value}|{self.frame.currency.value}|{code}|"
            f"{period.key()}|{comparison.key() if comparison else '-'}",
        )
        currency = self.frame.currency if spec.unit in (U.MONEY, U.PER_SHARE) else None
        common: dict[str, Any] = {
            "metric_id": metric_id,
            "code": code,
            "name": spec.name,
            "category": spec.category,
            "unit": spec.unit,
            "currency": currency,
            "reporting_basis": self.frame.basis,
            "period": period,
            "comparison_period": comparison,
            "formula_id": formula_id,
            "formula": spec.formula,
        }
        if not self.applicable(code):
            metric = DerivedMetric(
                **common,
                status=S.NOT_APPLICABLE,
                value=None,
                inputs=(),
                confidence=None,
                interpretation="INDUSTRY_NOT_APPLICABLE",
                missing_inputs=(f"not meaningful for industry {self.industry.value}",),
            )
            if self.latest_fy is None or period.key() == self.latest_fy.key():
                self._store(metric)
            return metric
        facts: dict[str, Fact] = {}
        missing: list[tuple[str, Missing]] = []
        for role, got in parts:
            if isinstance(got, Fact):
                facts[role] = got
            else:
                missing.append((role, got))
        inputs: list[DerivedMetricInput] = []
        derived: list[UUID] = []
        items: list[tuple[str, Fact | Missing]] = [*facts.items(), *missing]
        for role, item in items:
            for i in item.inputs:
                inputs.append(
                    i.model_copy(update={"role": role if not i.role else f"{role}.{i.role}"})
                )
            derived.extend(d for d in item.derived_from if d in self.metrics)
        all_warnings = sorted({w for f in facts.values() for w in f.warnings} | set(warnings))
        status = S.CALCULATED
        value: Decimal | None = None
        interpretation: str | None = None
        notes = [f"{role}: {m.what}" for role, m in missing]
        if missing_note:
            notes.append(missing_note)
        if missing:
            status = min((m.status for _, m in missing), key=STATUS_PRIORITY.index)
            if status in (S.NOT_APPLICABLE, S.ZERO_DENOMINATOR):
                status = S.INSUFFICIENT_DATA  # an upstream refusal leaves this one unsupported
        else:
            mixed = self._mixing(inputs)
            if mixed is not None:
                status = mixed
            else:
                result = formula({role: fact.value for role, fact in facts.items()})
                if isinstance(result, Refused):
                    status, interpretation = result.status, result.interpretation
                else:
                    value = result if spec.unit == U.MONEY else quantize(result)
        by_code: dict[str, set[str]] = {}
        for f in facts.values():
            if f.formula_id and f.formula_id.count("/") >= 2:
                code_part, _, variant_part = f.formula_id.rpartition("/")
                by_code.setdefault(code_part, set()).add(variant_part)
        if any(len(found) > 1 for found in by_code.values()):
            # the same building block computed two different ways at the two endpoints
            all_warnings.append("MIXED_FORMULA_VARIANTS")
        confidence: Confidence | None = None
        if status == S.CALCULATED:
            confidence = weakest([f.confidence for f in facts.values()] + ([cap] if cap else []))
        metric = DerivedMetric(
            **common,
            status=status,
            value=value,
            inputs=tuple(inputs) if status == S.CALCULATED or inputs else (),
            derived_from=tuple(sorted(set(derived), key=str)),
            missing_inputs=tuple(notes),
            confidence=confidence,
            interpretation=interpretation,
            warnings=tuple(sorted(set(all_warnings))),
        )
        self._store(metric)
        return metric

    def _store(self, metric: DerivedMetric) -> None:
        self.metrics[metric.metric_id] = metric
        self.index[(metric.code, metric.period.key())] = metric

    def _mixing(self, inputs: Sequence[DerivedMetricInput]) -> CalculationStatus | None:
        if len({i.reporting_basis for i in inputs}) > 1:
            return S.INCOMPATIBLE_BASIS
        if len({i.currency for i in inputs}) > 1:
            return S.INCOMPATIBLE_CURRENCY
        return None

    # --- generic shapes ----------------------------------------------------------------------

    def ratio_metric(
        self,
        code: str,
        period: FinancialPeriod,
        numerator: tuple[str, Got],
        denominator: tuple[str, Got],
        *,
        scale: int = 100,
        positive_denominator: bool = False,
        absolute_numerator: bool = False,
    ) -> DerivedMetric:
        n_role, d_role = numerator[0], denominator[0]

        def compute(v: Values) -> Decimal | Refused:
            top = abs(v[n_role]) if absolute_numerator else v[n_role]
            if positive_denominator:
                return positive_ratio(top, v[d_role], scale, d_role.upper())
            return ratio(top, v[d_role], scale)

        return self.emit(code, period, [numerator, denominator], compute)

    def averaged_ratio(
        self,
        code: str,
        period: FinancialPeriod,
        numerator: tuple[str, Got],
        balance: str,
        close: Got,
        opening: Got,
        *,
        scale: int = 100,
        extra: Sequence[tuple[str, Got]] = (),
        numerator_fn: Callable[[Values], Decimal] | None = None,
    ) -> DerivedMetric:
        """numerator / average(balance); falls back to the closing balance, flagged."""
        parts: list[tuple[str, Got]] = [numerator, *extra, (f"{balance}_close", close)]
        warnings: list[str] = []
        cap = None
        variant = "AVERAGE"
        if isinstance(opening, Fact):
            parts.append((f"{balance}_open", opening))
        else:
            warnings.append("ENDING_BALANCE_USED")
            cap = Confidence.MEDIUM
            variant = "ENDING"

        def compute(v: Values) -> Decimal | Refused:
            top = numerator_fn(v) if numerator_fn else v[numerator[0]]
            return positive_ratio(top, average(v, balance), scale, balance.upper())

        return self.emit(code, period, parts, compute, variant=variant, warnings=warnings, cap=cap)

    def yoy(
        self,
        code: str,
        current: Got,
        prior: Got,
        period: FinancialPeriod,
        prior_period: FinancialPeriod,
        months: int = 12,
    ) -> DerivedMetric:
        mismatch = compare_periods(period, prior_period, months)

        def compute(v: Values) -> Decimal | Refused:
            if mismatch is not None:
                return Refused(mismatch)
            return growth(v["current"], v["prior"])

        return self.emit(
            code, period, [("current", current), ("prior", prior)], compute, comparison=prior_period
        )

    def difference(
        self,
        code: str,
        current: Got,
        prior: Got,
        period: FinancialPeriod,
        prior_period: FinancialPeriod,
        scale: int = 1,
    ) -> DerivedMetric:
        return self.emit(
            code,
            period,
            [("current", current), ("prior", prior)],
            lambda v: (v["current"] - v["prior"]) * scale,
            comparison=prior_period,
        )


# --- building blocks ---------------------------------------------------------------------------


def base_flow_metrics(e: Engine, p: FinancialPeriod) -> None:
    """EBITDA, EBIT, tax rate, NOPAT, capex, FCF and COGS for one flow period."""
    g = e.canonical
    reported = g([M.EBITDA], p)
    if isinstance(reported, Fact):
        e.emit("EBITDA", p, [("ebitda", reported)], lambda v: v["ebitda"], variant="REPORTED")
    else:
        e.emit(
            "EBITDA",
            p,
            [
                ("pbt", g([M.PBT], p)),
                ("finance_cost", g([M.FINANCE_COST], p)),
                ("depreciation", g([M.DEPRECIATION_AMORTIZATION], p)),
            ],
            lambda v: v["pbt"] + v["finance_cost"] + v["depreciation"],
            variant="DERIVED",
        )
    reported = g([M.EBIT], p)
    if isinstance(reported, Fact):
        e.emit("EBIT", p, [("ebit", reported)], lambda v: v["ebit"], variant="REPORTED")
    else:
        e.emit(
            "EBIT",
            p,
            [("pbt", g([M.PBT], p)), ("finance_cost", g([M.FINANCE_COST], p))],
            lambda v: v["pbt"] + v["finance_cost"],
            variant="DERIVED",
        )
    pbt = g([M.PBT], p)
    total_tax = g([M.TOTAL_TAX], p)
    current_tax, deferred_tax = g([M.CURRENT_TAX], p), g([M.DEFERRED_TAX], p)
    if isinstance(total_tax, Fact):
        e.emit(
            "TAX_RATE",
            p,
            [("tax", total_tax), ("pbt", pbt)],
            lambda v: positive_ratio(v["tax"], v["pbt"], 100, "PBT"),
            variant="TOTAL_TAX",
        )
    elif isinstance(current_tax, Fact) and isinstance(deferred_tax, Fact):
        e.emit(
            "TAX_RATE",
            p,
            [("current_tax", current_tax), ("deferred_tax", deferred_tax), ("pbt", pbt)],
            lambda v: positive_ratio(v["current_tax"] + v["deferred_tax"], v["pbt"], 100, "PBT"),
            variant="COMPONENTS",
        )
    else:
        e.emit(
            "TAX_RATE",
            p,
            [("pbt", pbt), ("pat", g([M.PAT], p))],
            lambda v: positive_ratio(v["pbt"] - v["pat"], v["pbt"], 100, "PBT"),
            variant="IMPLIED",
            warnings=["IMPLIED_FROM_PBT_AND_PAT"],
            cap=Confidence.MEDIUM,
        )
    e.emit(
        "NOPAT",
        p,
        [("ebit", e.got("EBIT", p)), ("tax_rate", e.got("TAX_RATE", p))],
        lambda v: quantize(v["ebit"] * (1 - v["tax_rate"] / 100)),
    )
    capex = g([M.CAPEX], p)
    variant = "CAPEX"
    if not isinstance(capex, Fact):
        capex, variant = first_available(g([M.PURCHASE_OF_PPE], p), capex), "PURCHASE_OF_PPE"
    e.emit("CAPEX", p, [("capex", capex)], lambda v: abs(v["capex"]), variant=variant)
    e.emit(
        "FCF",
        p,
        [("cfo", g([M.CFO], p)), ("capex", e.got("CAPEX", p))],
        lambda v: v["cfo"] - v["capex"],
    )
    e.emit(
        "COGS",
        p,
        [
            ("raw_materials", g([M.RAW_MATERIAL_COST], p)),
            ("purchases", g([M.PURCHASES_OF_STOCK_IN_TRADE], p)),
            ("inventory_change", g([M.CHANGE_IN_INVENTORIES], p)),
        ],
        lambda v: v["raw_materials"] + v["purchases"] + v["inventory_change"],
    )


def base_balance_metrics(e: Engine, b: FinancialPeriod) -> None:
    g = e.canonical
    share_capital, reserves = g([M.EQUITY_SHARE_CAPITAL], b), g([M.RESERVES, M.OTHER_EQUITY], b)
    if isinstance(share_capital, Fact) and isinstance(reserves, Fact):
        e.emit(
            "SHAREHOLDERS_EQUITY",
            b,
            [("share_capital", share_capital), ("reserves", reserves)],
            lambda v: v["share_capital"] + v["reserves"],
            variant="CAPITAL_PLUS_RESERVES",
        )
    else:
        e.emit(
            "SHAREHOLDERS_EQUITY",
            b,
            [("total_equity", g([M.TOTAL_EQUITY], b))],
            lambda v: v["total_equity"],
            variant="TOTAL_EQUITY",
        )
    debt = first_available(g([M.TOTAL_DEBT], b), g([M.TOTAL_BORROWINGS], b))
    if isinstance(debt, Fact):
        e.emit("GROSS_DEBT", b, [("debt", debt)], lambda v: v["debt"], variant="REPORTED")
    else:
        e.emit(
            "GROSS_DEBT",
            b,
            [
                ("long_term", g([M.LONG_TERM_BORROWINGS], b)),
                ("short_term", g([M.SHORT_TERM_BORROWINGS], b)),
            ],
            lambda v: v["long_term"] + v["short_term"],
            variant="COMPONENTS",
        )
    cash = g([M.CASH_AND_CASH_EQUIVALENTS], b)
    net = e.emit(
        "NET_DEBT",
        b,
        [("debt", e.got("GROSS_DEBT", b)), ("cash", cash)],
        lambda v: v["debt"] - v["cash"],
    )
    if net.value is not None and net.value < 0:
        e._store(net.model_copy(update={"interpretation": "NET_CASH"}))
    equity, debt_got = e.got("SHAREHOLDERS_EQUITY", b), e.got("GROSS_DEBT", b)
    e.emit(
        "CAPITAL_EMPLOYED",
        b,
        [("equity", equity), ("debt", debt_got)],
        lambda v: v["equity"] + v["debt"],
    )
    e.emit(
        "INVESTED_CAPITAL",
        b,
        [("equity", equity), ("debt", debt_got), ("cash", cash)],
        lambda v: v["equity"] + v["debt"] - v["cash"],
    )
    e.emit(
        "NET_WORKING_CAPITAL",
        b,
        [
            ("current_assets", g([M.TOTAL_CURRENT_ASSETS], b)),
            ("current_liabilities", g([M.TOTAL_CURRENT_LIABILITIES], b)),
        ],
        lambda v: v["current_assets"] - v["current_liabilities"],
    )


# --- annual analytics --------------------------------------------------------------------------

GROWTH_FLOWS: dict[str, tuple[CanonicalMetric, ...] | str] = {
    "REVENUE": REVENUE,
    "OPERATING_PROFIT": (M.OPERATING_PROFIT,),
    "EBITDA": "EBITDA",
    "PBT": (M.PBT,),
    "PAT": (M.PAT,),
    "EPS": EPS,
    "CFO": (M.CFO,),
    "FCF": "FCF",
    "CAPEX": "CAPEX",
}
GROWTH_BALANCES: dict[str, tuple[CanonicalMetric, ...] | str] = {
    "TOTAL_ASSETS": (M.TOTAL_ASSETS,),
    "EQUITY": "SHAREHOLDERS_EQUITY",
    "GROSS_DEBT": "GROSS_DEBT",
    "RECEIVABLES": (M.TRADE_RECEIVABLES,),
    "INVENTORY": (M.INVENTORY,),
    "PAYABLES": (M.TRADE_PAYABLES,),
    "SHARE_CAPITAL": (M.EQUITY_SHARE_CAPITAL,),
    "DEPOSITS": (M.DEPOSITS,),
    "ADVANCES": (M.ADVANCES,),
}


def pair(
    e: Engine,
    source: tuple[CanonicalMetric, ...] | str,
    current: FinancialPeriod,
    prior: FinancialPeriod,
) -> tuple[Got, Got]:
    """Both endpoints of a comparison, always the SAME canonical metric (never basic EPS
    against diluted EPS, never revenue against total income)."""
    if isinstance(source, str):
        return e.got(source, current), e.got(source, prior)
    now = e.canonical(source, current)
    if isinstance(now, Fact):
        same = (now.inputs[0].metric,)
        return now, e.canonical(same, prior)
    return now, e.canonical(source, prior)


def annual_metrics(e: Engine, end: date) -> None:
    P = e.periods
    p, pp = P.fy(end), P.fy(shift(end, -12))
    b, bp = P.pit(end), P.pit(shift(end, -12))
    g = e.canonical
    revenue = g(REVENUE, p)

    for name, source in GROWTH_FLOWS.items():
        current, prior = pair(e, source, p, pp)
        e.yoy(f"{name}_GROWTH_YOY", current, prior, p, pp)
    for name, source in GROWTH_BALANCES.items():
        current, prior = pair(e, source, b, bp)
        e.yoy(f"{name}_GROWTH_YOY", current, prior, p, pp)

    flow_margins = {
        "EBITDA_MARGIN": e.got("EBITDA", p),
        "OPERATING_MARGIN": g([M.OPERATING_PROFIT], p),
        "EBIT_MARGIN": e.got("EBIT", p),
        "PBT_MARGIN": g([M.PBT], p),
        "PAT_MARGIN": g([M.PAT], p),
        "CFO_MARGIN": g([M.CFO], p),
        "FCF_MARGIN": e.got("FCF", p),
        "EMPLOYEE_COST_TO_REVENUE": g([M.EMPLOYEE_COST], p),
        "RAW_MATERIAL_COST_TO_REVENUE": g([M.RAW_MATERIAL_COST], p),
        "POWER_AND_FUEL_TO_REVENUE": g([M.POWER_AND_FUEL], p),
        "OTHER_EXPENSES_TO_REVENUE": g([M.OTHER_EXPENSES], p),
        "DEPRECIATION_TO_REVENUE": g([M.DEPRECIATION_AMORTIZATION], p),
        "FINANCE_COST_TO_REVENUE": g([M.FINANCE_COST], p),
    }
    for code, numerator in flow_margins.items():
        e.ratio_metric(
            code, p, ("numerator", numerator), ("revenue", revenue), positive_denominator=True
        )
    e.emit(
        "GROSS_MARGIN",
        p,
        [("revenue", revenue), ("cogs", e.got("COGS", p))],
        lambda v: positive_ratio(v["revenue"] - v["cogs"], v["revenue"], 100, "REVENUE"),
    )
    pbt = g([M.PBT], p)
    e.ratio_metric(
        "OTHER_INCOME_TO_PBT",
        p,
        ("other_income", g([M.OTHER_INCOME], p)),
        ("pbt", pbt),
        positive_denominator=True,
    )
    e.ratio_metric(
        "EXCEPTIONAL_TO_PBT",
        p,
        ("exceptional", g([M.EXCEPTIONAL_ITEMS], p)),
        ("pbt", pbt),
        positive_denominator=True,
        absolute_numerator=True,
    )
    for code in ("EBITDA_MARGIN", "OPERATING_MARGIN", "PBT_MARGIN", "PAT_MARGIN"):
        e.difference(f"{code}_CHANGE_YOY", e.got(code, p), e.got(code, pp), p, pp, scale=100)
    e.difference("TAX_RATE_CHANGE_YOY", e.got("TAX_RATE", p), e.got("TAX_RATE", pp), p, pp)
    e.difference(
        "OTHER_INCOME_TO_PBT_CHANGE_YOY",
        e.got("OTHER_INCOME_TO_PBT", p),
        e.got("OTHER_INCOME_TO_PBT", pp),
        p,
        pp,
    )

    # returns (averages of opening and closing balances)
    pat = g([M.PAT], p)
    assets, assets_open = g([M.TOTAL_ASSETS], b), g([M.TOTAL_ASSETS], bp)
    equity, equity_open = e.got("SHAREHOLDERS_EQUITY", b), e.got("SHAREHOLDERS_EQUITY", bp)
    e.averaged_ratio("ROE", p, ("pat", pat), "equity", equity, equity_open)
    e.averaged_ratio("ROA", p, ("pat", pat), "assets", assets, assets_open)
    e.averaged_ratio(
        "ROCE",
        p,
        ("ebit", e.got("EBIT", p)),
        "capital_employed",
        e.got("CAPITAL_EMPLOYED", b),
        e.got("CAPITAL_EMPLOYED", bp),
    )
    e.averaged_ratio(
        "ROIC",
        p,
        ("nopat", e.got("NOPAT", p)),
        "invested_capital",
        e.got("INVESTED_CAPITAL", b),
        e.got("INVESTED_CAPITAL", bp),
    )
    tangible = [("goodwill", g([M.GOODWILL], b)), ("intangibles", g([M.INTANGIBLE_ASSETS], b))]
    e.emit(
        "RETURN_ON_TANGIBLE_CAPITAL",
        p,
        [("ebit", e.got("EBIT", p)), ("capital_employed", e.got("CAPITAL_EMPLOYED", b)), *tangible],
        lambda v: positive_ratio(
            v["ebit"],
            v["capital_employed"] - v["goodwill"] - v["intangibles"],
            100,
            "TANGIBLE_CAPITAL",
        ),
        variant="ENDING",
        warnings=["ENDING_BALANCE_USED"],
        cap=Confidence.MEDIUM,
    )
    e.averaged_ratio(
        "ASSET_TURNOVER", p, ("revenue", revenue), "assets", assets, assets_open, scale=1
    )
    fixed, fixed_open = pair(e, FIXED, b, bp)
    e.averaged_ratio(
        "FIXED_ASSET_TURNOVER", p, ("revenue", revenue), "fixed_assets", fixed, fixed_open, scale=1
    )
    e.difference("ROE_CHANGE_YOY", e.got("ROE", p), e.got("ROE", pp), p, pp)

    # DuPont (both balances averaged, so the product equals ROE)
    e.ratio_metric(
        "DUPONT_NET_MARGIN", p, ("pat", pat), ("revenue", revenue), positive_denominator=True
    )
    e.averaged_ratio(
        "DUPONT_ASSET_TURNOVER", p, ("revenue", revenue), "assets", assets, assets_open, scale=1
    )
    dupont_parts: list[tuple[str, Got]] = [("assets_close", assets), ("equity_close", equity)]
    if isinstance(assets_open, Fact) and isinstance(equity_open, Fact):
        dupont_parts += [("assets_open", assets_open), ("equity_open", equity_open)]
    e.emit(
        "DUPONT_EQUITY_MULTIPLIER",
        p,
        dupont_parts,
        lambda v: positive_ratio(average(v, "assets"), average(v, "equity"), 1, "EQUITY"),
        variant="AVERAGE" if len(dupont_parts) == 4 else "ENDING",
        warnings=[] if len(dupont_parts) == 4 else ["ENDING_BALANCE_USED"],
        cap=None if len(dupont_parts) == 4 else Confidence.MEDIUM,
    )
    e.emit(
        "DUPONT_ROE",
        p,
        [
            ("net_margin", e.got("DUPONT_NET_MARGIN", p)),
            ("asset_turnover", e.got("DUPONT_ASSET_TURNOVER", p)),
            ("equity_multiplier", e.got("DUPONT_EQUITY_MULTIPLIER", p)),
        ],
        lambda v: v["net_margin"] * v["asset_turnover"] * v["equity_multiplier"],
    )
    for code in ("DUPONT_NET_MARGIN", "DUPONT_ASSET_TURNOVER", "DUPONT_EQUITY_MULTIPLIER"):
        e.difference(f"{code}_CHANGE_YOY", e.got(code, p), e.got(code, pp), p, pp)

    # leverage & solvency
    debt, net_debt = e.got("GROSS_DEBT", b), e.got("NET_DEBT", b)
    ebitda, cfo = e.got("EBITDA", p), g([M.CFO], p)
    cash = g([M.CASH_AND_CASH_EQUIVALENTS], b)
    e.ratio_metric(
        "DEBT_TO_EQUITY", p, ("debt", debt), ("equity", equity), scale=1, positive_denominator=True
    )
    e.ratio_metric(
        "DEBT_TO_ASSETS", p, ("debt", debt), ("assets", assets), scale=1, positive_denominator=True
    )
    nde = e.ratio_metric(
        "NET_DEBT_TO_EQUITY",
        p,
        ("net_debt", net_debt),
        ("equity", equity),
        scale=1,
        positive_denominator=True,
    )
    if nde.value is not None and nde.value < 0:
        e._store(nde.model_copy(update={"interpretation": "NET_CASH"}))
    e.ratio_metric(
        "DEBT_TO_EBITDA", p, ("debt", debt), ("ebitda", ebitda), scale=1, positive_denominator=True
    )

    def net_debt_to_ebitda(v: Values) -> Decimal | Refused:
        if v["net_debt"] < 0:
            return Refused(S.NOT_APPLICABLE, "NET_CASH")
        return positive_ratio(v["net_debt"], v["ebitda"], 1, "EBITDA")

    e.emit(
        "NET_DEBT_TO_EBITDA", p, [("net_debt", net_debt), ("ebitda", ebitda)], net_debt_to_ebitda
    )
    finance_cost = g([M.FINANCE_COST], p)
    e.ratio_metric(
        "INTEREST_COVERAGE", p, ("ebit", e.got("EBIT", p)), ("finance_cost", finance_cost), scale=1
    )
    e.ratio_metric(
        "FINANCE_COST_COVERAGE", p, ("ebitda", ebitda), ("finance_cost", finance_cost), scale=1
    )
    e.ratio_metric(
        "DEBT_TO_CFO", p, ("debt", debt), ("cfo", cfo), scale=1, positive_denominator=True
    )
    e.ratio_metric("CFO_TO_DEBT", p, ("cfo", cfo), ("debt", debt), scale=1)
    e.ratio_metric("CASH_TO_DEBT", p, ("cash", cash), ("debt", debt), scale=1)
    e.ratio_metric(
        "EQUITY_TO_ASSETS", p, ("equity", equity), ("assets", assets), positive_denominator=True
    )

    # liquidity (as at the fiscal year end)
    current_assets, current_liabilities = (
        g([M.TOTAL_CURRENT_ASSETS], b),
        g([M.TOTAL_CURRENT_LIABILITIES], b),
    )
    e.ratio_metric(
        "CURRENT_RATIO",
        p,
        ("current_assets", current_assets),
        ("current_liabilities", current_liabilities),
        scale=1,
    )
    e.emit(
        "QUICK_RATIO",
        p,
        [
            ("current_assets", current_assets),
            ("inventory", g([M.INVENTORY], b)),
            ("current_liabilities", current_liabilities),
        ],
        lambda v: ratio(v["current_assets"] - v["inventory"], v["current_liabilities"]),
    )
    e.ratio_metric(
        "CASH_RATIO", p, ("cash", cash), ("current_liabilities", current_liabilities), scale=1
    )

    # working capital (days on a 365-day fiscal year, average balances)
    receivables, receivables_open = g([M.TRADE_RECEIVABLES], b), g([M.TRADE_RECEIVABLES], bp)
    cogs = e.got("COGS", p)
    _days(
        e, "RECEIVABLE_DAYS", p, ("revenue", revenue), "receivables", receivables, receivables_open
    )
    _days(
        e,
        "INVENTORY_DAYS",
        p,
        ("cogs", cogs),
        "inventory",
        g([M.INVENTORY], b),
        g([M.INVENTORY], bp),
    )
    _days(
        e,
        "PAYABLE_DAYS",
        p,
        ("cogs", cogs),
        "payables",
        g([M.TRADE_PAYABLES], b),
        g([M.TRADE_PAYABLES], bp),
    )
    e.emit(
        "CASH_CONVERSION_CYCLE",
        p,
        [
            ("receivable_days", e.got("RECEIVABLE_DAYS", p)),
            ("inventory_days", e.got("INVENTORY_DAYS", p)),
            ("payable_days", e.got("PAYABLE_DAYS", p)),
        ],
        lambda v: v["receivable_days"] + v["inventory_days"] - v["payable_days"],
    )
    e.difference(
        "RECEIVABLE_DAYS_CHANGE_YOY",
        e.got("RECEIVABLE_DAYS", p),
        e.got("RECEIVABLE_DAYS", pp),
        p,
        pp,
    )
    nwc = e.got("NET_WORKING_CAPITAL", b)
    e.ratio_metric(
        "NWC_TO_REVENUE", p, ("nwc", nwc), ("revenue", revenue), positive_denominator=True
    )
    e.emit(
        "NWC_CHANGE",
        p,
        [("current", nwc), ("prior", e.got("NET_WORKING_CAPITAL", bp))],
        lambda v: v["current"] - v["prior"],
        comparison=pp,
    )
    e.ratio_metric(
        "RECEIVABLES_TO_REVENUE",
        p,
        ("receivables", receivables),
        ("revenue", revenue),
        positive_denominator=True,
    )
    e.ratio_metric(
        "INVENTORY_TO_REVENUE",
        p,
        ("inventory", g([M.INVENTORY], b)),
        ("revenue", revenue),
        positive_denominator=True,
    )
    for name in ("RECEIVABLES", "INVENTORY", "PAYABLES"):
        e.difference(
            f"{name}_VS_REVENUE_GROWTH",
            e.got(f"{name}_GROWTH_YOY", p),
            e.got("REVENUE_GROWTH_YOY", p),
            p,
            pp,
        )

    # cash-flow quality
    fcf = e.got("FCF", p)
    for code, top, bottom in (
        ("CFO_TO_PAT", ("cfo", cfo), ("pat", pat)),
        ("CFO_TO_EBITDA", ("cfo", cfo), ("ebitda", ebitda)),
        ("CFO_TO_PBT", ("cfo", cfo), ("pbt", pbt)),
        ("FCF_TO_PAT", ("fcf", fcf), ("pat", pat)),
        ("FCF_TO_EBITDA", ("fcf", fcf), ("ebitda", ebitda)),
        ("CAPEX_TO_CFO", ("capex", e.got("CAPEX", p)), ("cfo", cfo)),
    ):
        e.ratio_metric(code, p, top, bottom, scale=1, positive_denominator=True)
    e.ratio_metric(
        "CAPEX_TO_REVENUE",
        p,
        ("capex", e.got("CAPEX", p)),
        ("revenue", revenue),
        positive_denominator=True,
    )
    e.ratio_metric(
        "DIVIDENDS_TO_CFO",
        p,
        ("dividends", g([M.DIVIDENDS_PAID], p)),
        ("cfo", cfo),
        scale=1,
        positive_denominator=True,
        absolute_numerator=True,
    )
    e.ratio_metric(
        "BUYBACK_TO_CFO",
        p,
        ("buyback", g([M.BUYBACK], p)),
        ("cfo", cfo),
        scale=1,
        positive_denominator=True,
        absolute_numerator=True,
    )
    e.difference("CFO_TO_PAT_CHANGE_YOY", e.got("CFO_TO_PAT", p), e.got("CFO_TO_PAT", pp), p, pp)
    e.difference(
        "PAT_VS_CFO_GROWTH_GAP", e.got("PAT_GROWTH_YOY", p), e.got("CFO_GROWTH_YOY", p), p, pp
    )

    # accruals
    e.emit("TOTAL_ACCRUALS", p, [("pat", pat), ("cfo", cfo)], lambda v: v["pat"] - v["cfo"])
    e.averaged_ratio(
        "ACCRUAL_RATIO", p, ("accruals", e.got("TOTAL_ACCRUALS", p)), "assets", assets, assets_open
    )
    bs_parts: list[tuple[str, Got]] = [
        ("ca", current_assets),
        ("ca_prior", g([M.TOTAL_CURRENT_ASSETS], bp)),
        ("cash", cash),
        ("cash_prior", g([M.CASH_AND_CASH_EQUIVALENTS], bp)),
        ("cl", current_liabilities),
        ("cl_prior", g([M.TOTAL_CURRENT_LIABILITIES], bp)),
        ("depreciation", g([M.DEPRECIATION_AMORTIZATION], p)),
        ("assets_close", assets),
        ("assets_open", assets_open),
    ]
    e.emit(
        "BALANCE_SHEET_ACCRUAL_RATIO",
        p,
        bs_parts,
        lambda v: positive_ratio(
            (v["ca"] - v["ca_prior"])
            - (v["cash"] - v["cash_prior"])
            - (v["cl"] - v["cl_prior"])
            - v["depreciation"],
            average(v, "assets"),
            100,
            "ASSETS",
        ),
        comparison=pp,
    )
    e.difference(
        "ACCRUAL_RATIO_CHANGE_YOY", e.got("ACCRUAL_RATIO", p), e.got("ACCRUAL_RATIO", pp), p, pp
    )
    e.averaged_ratio(
        "DEPRECIATION_RATE",
        p,
        ("depreciation", g([M.DEPRECIATION_AMORTIZATION], p)),
        "fixed_assets",
        fixed,
        fixed_open,
    )
    e.difference(
        "DEPRECIATION_RATE_CHANGE_YOY",
        e.got("DEPRECIATION_RATE", p),
        e.got("DEPRECIATION_RATE", pp),
        p,
        pp,
    )
    e.ratio_metric(
        "CWIP_TO_FIXED_ASSETS",
        p,
        ("cwip", g([M.CWIP], b)),
        ("fixed_assets", fixed),
        positive_denominator=True,
    )
    _asset_mix(e, p, pp, b, bp)

    # banking
    e.emit(
        "COST_TO_INCOME",
        p,
        [
            ("operating_expenses", g([M.BANK_OPERATING_EXPENSES], p)),
            ("net_interest_income", g([M.NET_INTEREST_INCOME], p)),
            ("other_income", g([M.OTHER_INCOME], p)),
        ],
        lambda v: positive_ratio(
            v["operating_expenses"], v["net_interest_income"] + v["other_income"], 100, "INCOME"
        ),
    )
    e.ratio_metric(
        "FINANCING_PROFIT_MARGIN",
        p,
        ("financing_profit", g([M.BANK_FINANCING_PROFIT], p)),
        ("revenue", revenue),
        positive_denominator=True,
    )
    e.ratio_metric(
        "PROVISIONS_TO_REVENUE",
        p,
        ("provisions", g([M.PROVISIONS_AND_CONTINGENCIES], p)),
        ("revenue", revenue),
        positive_denominator=True,
    )
    for code in ("GROSS_NPA_RATIO", "NET_NPA_RATIO"):
        if e.financial:
            e.emit(
                code,
                p,
                [],
                lambda v: Refused(S.INSUFFICIENT_DATA),
                missing_note="NPA % is preserved by Box 4 as an unmapped percentage line, "
                "not as a canonical value; Box 5 does not reinterpret raw lines",
            )


def _days(
    e: Engine,
    code: str,
    p: FinancialPeriod,
    flow: tuple[str, Got],
    balance: str,
    close: Got,
    opening: Got,
) -> DerivedMetric:
    """average balance / annual flow x 365."""
    parts: list[tuple[str, Got]] = [flow, (f"{balance}_close", close)]
    warnings: list[str] = []
    cap = None
    if isinstance(opening, Fact):
        parts.append((f"{balance}_open", opening))
    else:
        warnings.append("ENDING_BALANCE_USED")
        cap = Confidence.MEDIUM
    return e.emit(
        code,
        p,
        parts,
        lambda v: positive_ratio(average(v, balance) * 365, v[flow[0]], 1, flow[0].upper()),
        variant="AVERAGE" if cap is None else "ENDING",
        warnings=warnings,
        cap=cap,
    )


def _asset_mix(
    e: Engine, p: FinancialPeriod, pp: FinancialPeriod, b: FinancialPeriod, bp: FinancialPeriod
) -> None:
    g = e.canonical
    components = {
        "receivables": (M.TRADE_RECEIVABLES,),
        "inventory": (M.INVENTORY,),
        "cash": (M.CASH_AND_CASH_EQUIVALENTS,),
        "investments": (M.TOTAL_INVESTMENTS,),
        "fixed_assets": FIXED,
    }
    parts: list[tuple[str, Got]] = [
        ("assets", g([M.TOTAL_ASSETS], b)),
        ("assets_prior", g([M.TOTAL_ASSETS], bp)),
    ]
    present = []
    for name, metrics in components.items():
        now, before = g(metrics, b), g(metrics, bp)
        if isinstance(now, Fact) and isinstance(before, Fact):
            parts += [(name, now), (f"{name}_prior", before)]
            present.append(name)

    def compute(v: Values) -> Decimal | Refused:
        if not present:
            return Refused(S.INSUFFICIENT_DATA)
        if v["assets"] <= 0 or v["assets_prior"] <= 0:
            return Refused(S.ZERO_DENOMINATOR)
        return max(
            abs(v[n] / v["assets"] - v[f"{n}_prior"] / v["assets_prior"]) * 100 for n in present
        )

    e.emit("ASSET_MIX_SHIFT", p, parts, compute, comparison=pp)


def _cagr_formula(years: int) -> Formula:
    def compute(v: Values) -> Decimal | Refused:
        return cagr(v["end"], v["start"], years)

    return compute


def _cumulative_ratio(years: int) -> Formula:
    def compute(v: Values) -> Decimal | Refused:
        cfo = sum((v[f"cfo_{k}"] for k in range(years)), Decimal(0))
        pat = sum((v[f"pat_{k}"] for k in range(years)), Decimal(0))
        return positive_ratio(cfo, pat, 1, "CUMULATIVE_PAT")

    return compute


def _cumulative_sum(name: str, years: int) -> Formula:
    def compute(v: Values) -> Decimal | Refused:
        return sum((v[f"{name}_{k}"] for k in range(years)), Decimal(0))

    return compute


def window_metrics(e: Engine, latest: date) -> None:
    """CAGR, multi-year margin change, cumulative cash flows, incremental ROIC (latest FY)."""
    P = e.periods
    p = P.fy(latest)
    cagr_sources: dict[str, tuple[CanonicalMetric, ...] | str] = {
        "REVENUE": REVENUE,
        "OPERATING_PROFIT": (M.OPERATING_PROFIT,),
        "EBITDA": "EBITDA",
        "PBT": (M.PBT,),
        "PAT": (M.PAT,),
        "EPS": EPS,
        "CFO": (M.CFO,),
        "FCF": "FCF",
        "TOTAL_ASSETS": (M.TOTAL_ASSETS,),
        "EQUITY": "SHAREHOLDERS_EQUITY",
        "DEPOSITS": (M.DEPOSITS,),
        "ADVANCES": (M.ADVANCES,),
    }
    balance = {"TOTAL_ASSETS", "EQUITY", "DEPOSITS", "ADVANCES"}
    for years in (3, 5, 10):
        start_end = shift(latest, -12 * years)
        sp = P.fy(start_end)
        for name, source in cagr_sources.items():
            now_p, then_p = (P.pit(latest), P.pit(start_end)) if name in balance else (p, sp)
            current, start = pair(e, source, now_p, then_p)
            e.emit(
                f"{name}_CAGR_{years}Y",
                p,
                [("end", current), ("start", start)],
                _cagr_formula(years),
                comparison=sp,
            )
        if years in (3, 5):
            for code in ("EBITDA_MARGIN", "OPERATING_MARGIN", "PBT_MARGIN", "PAT_MARGIN"):
                e.difference(
                    f"{code}_CHANGE_{years}Y", e.got(code, p), e.got(code, sp), p, sp, scale=100
                )
            years_back = [P.fy(shift(latest, -12 * k)) for k in range(years)]
            cfo_parts = [(f"cfo_{k}", e.canonical([M.CFO], q)) for k, q in enumerate(years_back)]
            pat_parts = [(f"pat_{k}", e.canonical([M.PAT], q)) for k, q in enumerate(years_back)]
            e.emit(
                f"CUMULATIVE_CFO_TO_PAT_{years}Y",
                p,
                [*cfo_parts, *pat_parts],
                _cumulative_ratio(years),
                comparison=years_back[-1],
            )
            fcf_parts = [(f"fcf_{k}", e.got("FCF", q)) for k, q in enumerate(years_back)]
            e.emit(
                f"CUMULATIVE_FCF_{years}Y",
                p,
                fcf_parts,
                _cumulative_sum("fcf", years),
                comparison=years_back[-1],
            )
    sp3 = P.fy(shift(latest, -36))
    e.emit(
        "INCREMENTAL_ROIC_3Y",
        p,
        [
            ("nopat", e.got("NOPAT", p)),
            ("nopat_start", e.got("NOPAT", sp3)),
            ("invested_capital", e.got("INVESTED_CAPITAL", P.pit(latest))),
            ("invested_capital_start", e.got("INVESTED_CAPITAL", P.pit(shift(latest, -36)))),
        ],
        lambda v: (
            Refused(S.NOT_APPLICABLE, "NO_CAPITAL_INCREASE")
            if v["invested_capital"] - v["invested_capital_start"] <= 0
            else (v["nopat"] - v["nopat_start"])
            * 100
            / (v["invested_capital"] - v["invested_capital_start"])
        ),
        comparison=sp3,
    )
    tax_points = [e.got("TAX_RATE", P.fy(shift(latest, -12 * k))) for k in range(5)]
    available = [t for t in tax_points if isinstance(t, Fact)]
    parts = [(f"tax_rate_{k}", t) for k, t in enumerate(tax_points) if isinstance(t, Fact)]
    e.emit(
        "TAX_RATE_RANGE_5Y",
        p,
        parts
        if len(available) >= 3
        else [
            *parts,
            (
                "tax_rate_history",
                Missing(
                    S.INSUFFICIENT_DATA,
                    f"{len(available)} of 5 fiscal years have a tax rate (3 needed)",
                ),
            ),
        ],
        lambda v: max(v.values()) - min(v.values()),
        comparison=P.fy(shift(latest, -48)),
    )


def quarterly_metrics(e: Engine, end: date) -> None:
    P = e.periods
    q, yq, sq = P.quarter(end), P.quarter(shift(end, -12)), P.quarter(shift(end, -3))
    g = e.canonical
    base_flow_metrics(e, q)
    for name, source in (
        ("REVENUE", REVENUE),
        ("OPERATING_PROFIT", (M.OPERATING_PROFIT,)),
        ("PBT", (M.PBT,)),
        ("PAT", (M.PAT,)),
        ("EPS", EPS),
    ):
        current, prior = pair(e, source, q, yq)
        e.yoy(f"{name}_GROWTH_YOY", current, prior, q, yq)
    for name, source in (("REVENUE", REVENUE), ("PAT", (M.PAT,))):
        current, prior = pair(e, source, q, sq)
        e.yoy(f"{name}_GROWTH_QOQ", current, prior, q, sq, months=3)
    revenue = g(REVENUE, q)
    for code, numerator in (
        ("EBITDA_MARGIN", e.got("EBITDA", q)),
        ("OPERATING_MARGIN", g([M.OPERATING_PROFIT], q)),
        ("PBT_MARGIN", g([M.PBT], q)),
        ("PAT_MARGIN", g([M.PAT], q)),
    ):
        e.ratio_metric(
            code, q, ("numerator", numerator), ("revenue", revenue), positive_denominator=True
        )
    for code in ("OPERATING_MARGIN", "PAT_MARGIN"):
        e.difference(f"{code}_CHANGE_YOY", e.got(code, q), e.got(code, yq), q, yq, scale=100)


def ttm_metrics(e: Engine) -> None:
    t = e.periods.ttm()
    g = e.canonical
    base_flow_metrics(e, t)
    revenue = g(REVENUE, t)
    for code, numerator in (
        ("EBITDA_MARGIN", e.got("EBITDA", t)),
        ("OPERATING_MARGIN", g([M.OPERATING_PROFIT], t)),
        ("PBT_MARGIN", g([M.PBT], t)),
        ("PAT_MARGIN", g([M.PAT], t)),
    ):
        e.ratio_metric(
            code, t, ("numerator", numerator), ("revenue", revenue), positive_denominator=True
        )
