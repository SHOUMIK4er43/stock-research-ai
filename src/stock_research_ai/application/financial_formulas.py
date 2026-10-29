"""DETAILED_RESEARCH_V1 Box 5: the versioned formula registry and its pure arithmetic.

Every calculated metric names exactly one FormulaDefinition (formula_id = CODE/version,
plus /VARIANT when a formula has explicit alternative constructions). The registry states
the expression, required and optional inputs, the periods and sectors it applies to and
its unit. The arithmetic helpers below are pure Decimal functions, tested on their own.
"""

from decimal import Decimal, localcontext

from stock_research_ai.application.numerics import root_growth
from stock_research_ai.domain.financial_analytics import (
    AnalyticsUnit,
    CalculationStatus,
    FormulaDefinition,
    FormulaVariant,
    MetricCategory,
    Sector,
)
from stock_research_ai.domain.financial_canonical import PeriodType

FORMULA_REGISTRY_VERSION = "formulas/2"
S = CalculationStatus
U = AnalyticsUnit
C = MetricCategory
P = PeriodType
GENERAL = (Sector.GENERAL,)
ALL_SECTORS = (Sector.GENERAL, Sector.FINANCIAL)
FY = (P.FY,)
FLOWS = (P.FY, P.QUARTER, P.TTM)
BALANCE = (P.POINT_IN_TIME,)
YOY = (P.FY, P.QUARTER)
DAYS_IN_FISCAL_YEAR = 365
DUPONT_TOLERANCE_PP = Decimal("0.01")


#: lender / general-financial formulas: Box 4 class FINANCIAL (NBFC, insurer, or a financial
#: business whose subtype is unresolved) and NOT a bank, whose funding is deposits, not
#: borrowings; industrial (GENERAL) and UNKNOWN-class companies are not applicable
NON_BANK_FINANCIAL: set[str] = set()
NON_BANK_NOTE = ("FINANCIAL class, not BANK (bank funding is deposits); industrial and "
                 "UNKNOWN classes are not applicable. ")


# --- pure arithmetic ------------------------------------------------------------------------


class Refused:
    """A calculation with inputs but no meaningful number (zero or negative base ...)."""

    __slots__ = ("interpretation", "status")

    def __init__(self, status: CalculationStatus, interpretation: str | None = None) -> None:
        self.status = status
        self.interpretation = interpretation

    def __repr__(self) -> str:
        return f"Refused({self.status.value}, {self.interpretation})"


Result = Decimal | Refused


def ratio(numerator: Decimal, denominator: Decimal, scale: int = 1) -> Result:
    if denominator == 0:
        return Refused(S.DIVISION_BY_ZERO)
    return numerator * scale / denominator


def positive_ratio(
    numerator: Decimal, denominator: Decimal, scale: int = 1, what: str = "DENOMINATOR"
) -> Result:
    if denominator == 0:
        return Refused(S.DIVISION_BY_ZERO)
    if denominator < 0:
        return Refused(S.NOT_APPLICABLE, f"NEGATIVE_{what}")
    return numerator * scale / denominator


def growth(current: Decimal, prior: Decimal) -> Result:
    """(current / prior - 1) x 100; the base must be positive."""
    if prior == 0:
        return Refused(S.DIVISION_BY_ZERO, "ZERO_BASE")
    if prior < 0:
        return Refused(S.NOT_APPLICABLE, "NEGATIVE_BASE")
    return (current / prior - 1) * 100


def cagr(end: Decimal, start: Decimal, years: int) -> Result:
    """((end / start) ^ (1 / years) - 1) x 100; both endpoints must be positive."""
    if years <= 0:
        return Refused(S.INVALID_INPUT, "INVALID_PERIOD_COUNT")
    if start == 0:
        return Refused(S.DIVISION_BY_ZERO, "ZERO_START")
    if start < 0 or end <= 0:
        return Refused(S.NOT_APPLICABLE, "NON_POSITIVE_ENDPOINT")
    with localcontext() as ctx:
        ctx.prec = 34
        return root_growth(end / start, Decimal(years)) * 100


def average(opening: Decimal, closing: Decimal) -> Decimal:
    """Average balance: only ever called with BOTH the opening and the closing value."""
    return (opening + closing) / 2


def days(average_balance: Decimal, annual_flow: Decimal) -> Result:
    """average balance / annual flow x 365."""
    return positive_ratio(average_balance * DAYS_IN_FISCAL_YEAR, annual_flow, 1, "FLOW")


def cash_conversion_cycle(receivable: Decimal, inventory: Decimal, payable: Decimal) -> Decimal:
    return receivable + inventory - payable


def free_cash_flow(cfo: Decimal, capex: Decimal) -> Decimal:
    """FCF/1 = CFO - |capex| (capex is an outflow reported with either sign)."""
    return cfo - abs(capex)


def dupont_roe(
    net_margin_pct: Decimal, asset_turnover: Decimal, equity_multiplier: Decimal
) -> Decimal:
    return net_margin_pct * asset_turnover * equity_multiplier


def nopat(ebit: Decimal, tax_rate_pct: Decimal) -> Decimal:
    return ebit * (1 - tax_rate_pct / 100)


# --- the registry ----------------------------------------------------------------------------

FORMULAS: dict[str, FormulaDefinition] = {}
_req: tuple[str, ...]


def _f(
    code: str,
    name: str,
    category: MetricCategory,
    unit: AnalyticsUnit,
    expression: str,
    required: tuple[str, ...],
    *,
    optional: tuple[str, ...] = (),
    periods: tuple[PeriodType, ...] = FY,
    sectors: tuple[Sector, ...] = GENERAL,
    variants: tuple[tuple[str, str], ...] = (),
    notes: str = "",
    non_bank_financial: bool = False,
) -> None:
    assert code not in FORMULAS, code
    if non_bank_financial:
        NON_BANK_FINANCIAL.add(code)
        sectors = (Sector.FINANCIAL,)
        notes = NON_BANK_NOTE + notes
    FORMULAS[code] = FormulaDefinition(
        formula_id=f"{code}/2",
        code=code,
        version=2,
        name=name,
        category=category,
        unit=unit,
        expression=expression,
        required_inputs=required,
        optional_inputs=optional,
        periods=periods,
        sectors=sectors,
        variants=tuple(FormulaVariant(name=n, expression=e) for n, e in variants),
        notes=("BANK only; other financial businesses and UNKNOWN are not applicable. "
               if sectors == (Sector.FINANCIAL,) and not non_bank_financial else "") + notes,
    )


# building blocks
_f(
    "EBITDA",
    "EBITDA",
    C.BUILDING_BLOCK,
    U.MONEY,
    "reported EBITDA, or PBT + finance cost + D&A",
    ("EBITDA | PBT, FINANCE_COST, DEPRECIATION_AMORTIZATION",),
    periods=FLOWS,
    variants=(
        ("REPORTED", "EBITDA as reported"),
        ("DERIVED", "PBT + finance cost + depreciation & amortization"),
    ),
    notes="never Screener OPERATING_PROFIT; includes other income and exceptional items "
    "when derived from PBT, so it falls below Screener operating profit whenever other income "
    "(which can net exceptional items) is negative (flagged EBITDA_BELOW_OPERATING_PROFIT)",
)
_f(
    "EBIT",
    "EBIT",
    C.BUILDING_BLOCK,
    U.MONEY,
    "reported EBIT, or PBT + finance cost",
    ("EBIT | PBT, FINANCE_COST",),
    periods=FLOWS,
    variants=(("REPORTED", "EBIT as reported"), ("DERIVED", "PBT + finance cost")),
    notes="when derived from PBT it includes other income, exceptional items and associates: "
    "not a company's operating EBIT (see OPERATING_EBIT)",
)
_f(
    "OPERATING_EBIT",
    "Operating EBIT (Screener operating profit - D&A)",
    C.BUILDING_BLOCK,
    U.MONEY,
    "Screener operating profit - depreciation & amortization",
    ("OPERATING_PROFIT", "DEPRECIATION_AMORTIZATION"),
    periods=FLOWS,
    notes="excludes other income and exceptional items: the calculated measure closest to a "
    "company-reported EBIT; the company's own definition is not verified",
)
_f(
    "NOPAT",
    "NOPAT",
    C.BUILDING_BLOCK,
    U.MONEY,
    "EBIT x (1 - effective tax rate / 100)",
    ("EBIT", "TAX_RATE"),
    periods=FLOWS,
)
_f(
    "CAPEX",
    "Capital expenditure",
    C.BUILDING_BLOCK,
    U.MONEY,
    "|CAPEX| or |PURCHASE_OF_PPE|",
    ("CAPEX | PURCHASE_OF_PPE",),
    periods=FLOWS,
    variants=(
        ("CAPEX", "|reported capital expenditure|"),
        ("PURCHASE_OF_PPE", "|purchase of property, plant and equipment|"),
    ),
    notes="never inferred from total investing cash flow; purchases of financial "
    "investments are not capex",
)
_f(
    "FCF",
    "Free cash flow",
    C.BUILDING_BLOCK,
    U.MONEY,
    "CFO - |capex|",
    ("CFO", "CAPEX"),
    periods=FLOWS,
    notes="before lease payments",
)
_f(
    "COGS",
    "Cost of goods sold",
    C.BUILDING_BLOCK,
    U.MONEY,
    "raw materials + purchases of stock-in-trade + change in inventories",
    ("RAW_MATERIAL_COST", "PURCHASES_OF_STOCK_IN_TRADE", "CHANGE_IN_INVENTORIES"),
    periods=FLOWS,
    notes="all three lines are required; none is assumed zero",
)
_f(
    "SHAREHOLDERS_EQUITY",
    "Shareholders' equity",
    C.BUILDING_BLOCK,
    U.MONEY,
    "equity share capital + reserves (or other equity), or total equity",
    ("EQUITY_SHARE_CAPITAL, RESERVES | OTHER_EQUITY | TOTAL_EQUITY",),
    optional=("HYBRID_EQUITY_INSTRUMENTS",),
    periods=BALANCE,
    sectors=ALL_SECTORS,
    variants=(
        ("CAPITAL_PLUS_RESERVES", "equity share capital + reserves / other equity"),
        ("CAPITAL_PLUS_HYBRID_PLUS_OTHER_EQUITY",
         "equity share capital + separately reported hybrid equity instruments + other equity"),
        ("TOTAL_EQUITY", "total equity as reported (may include NCI)"),
    ),
    notes="ACCOUNTING equity attributable to owners: includes equity-classified hybrid "
    "(perpetual) instruments, which a provider's 'Reserves' already contains; the claim used by "
    "net worth, leverage, capital and ROE",
)
_f(
    "COMMON_EQUITY",
    "Ordinary shareholders' equity",
    C.BUILDING_BLOCK,
    U.MONEY,
    "shareholders' equity - equity-classified hybrid instruments",
    ("SHAREHOLDERS_EQUITY", "HYBRID_EQUITY_INSTRUMENTS"),
    periods=BALANCE,
    sectors=ALL_SECTORS,
    variants=(
        ("LESS_HYBRID", "shareholders' equity (capital + reserves / other equity) - hybrid "
         "equity instruments at the same date and basis"),
    ),
    notes="ORDINARY (common) book equity, the claim of the ordinary shares: hybrids rank "
    "senior to it. Hybrid = 0 only on primary evidence at that date and basis: a filing nil on "
    "the line, or a filing balance sheet presenting equity without it. A provider's reserves "
    "alone cannot show hybrids, so no ordinary equity then; never built from total equity "
    "(may include NCI)",
)
_f(
    "GROSS_DEBT",
    "Gross debt (borrowings as reported)",
    C.BUILDING_BLOCK,
    U.MONEY,
    "TOTAL_DEBT, or TOTAL_BORROWINGS, or long-term + short-term borrowings",
    ("TOTAL_DEBT | TOTAL_BORROWINGS | LONG_TERM_BORROWINGS, SHORT_TERM_BORROWINGS",),
    periods=BALANCE,
    variants=(
        ("REPORTED", "total debt / total borrowings as reported in a filing"),
        ("COMPONENTS", "long-term + short-term borrowings"),
        ("PROVIDER_BORROWINGS", "an aggregator's 'Borrowings' line as published (Screener's "
         "includes lease liabilities)"),
    ),
    notes="a filing's borrowings exclude separately reported lease liabilities; an "
    "aggregator's 'Borrowings' line includes them (Screener) and is labelled as such, never "
    "as debt excluding leases",
)
_f(
    "LEASE_ADJUSTED_DEBT",
    "Debt including lease liabilities",
    C.BUILDING_BLOCK,
    U.MONEY,
    "gross debt + lease liabilities",
    ("GROSS_DEBT", "LEASE_LIABILITIES"),
    periods=BALANCE,
    variants=(
        ("LEASE_COMPONENTS", "gross debt + non-current + current lease liabilities"),
    ),
    notes="never on an aggregator's borrowings, which already include lease liabilities",
)
_f(
    "NET_DEBT",
    "Net debt",
    C.BUILDING_BLOCK,
    U.MONEY,
    "gross debt - cash and cash equivalents",
    ("GROSS_DEBT", "CASH_AND_CASH_EQUIVALENTS"),
    periods=BALANCE,
    notes="negative = net cash (interpretation NET_CASH)",
)
_f(
    "CAPITAL_EMPLOYED",
    "Capital employed",
    C.BUILDING_BLOCK,
    U.MONEY,
    "equity + gross debt",
    ("SHAREHOLDERS_EQUITY", "GROSS_DEBT"),
    periods=BALANCE,
)
_f(
    "INVESTED_CAPITAL",
    "Invested capital",
    C.BUILDING_BLOCK,
    U.MONEY,
    "equity + gross debt - cash and cash equivalents",
    ("SHAREHOLDERS_EQUITY", "GROSS_DEBT", "CASH_AND_CASH_EQUIVALENTS"),
    periods=BALANCE,
)
_f(
    "NET_WORKING_CAPITAL",
    "Net working capital",
    C.BUILDING_BLOCK,
    U.MONEY,
    "total current assets - total current liabilities",
    ("TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"),
    periods=BALANCE,
    notes="never built from generic 'other assets / liabilities'",
)
_f(
    "TOTAL_LIABILITIES",
    "Total liabilities",
    C.BUILDING_BLOCK,
    U.MONEY,
    "reported total liabilities, or current + non-current, or (equity + liabilities) - "
    "total equity",
    (
        "TOTAL_LIABILITIES | TOTAL_CURRENT_LIABILITIES, TOTAL_NON_CURRENT_LIABILITIES | "
        "TOTAL_EQUITY_AND_LIABILITIES, TOTAL_EQUITY",
    ),
    periods=BALANCE,
    variants=(
        ("REPORTED", "total liabilities as reported"),
        ("CURRENT_PLUS_NON_CURRENT", "current + non-current liabilities"),
        ("TOTAL_LESS_EQUITY", "total equity and liabilities - total equity"),
    ),
)

# growth
_GROWTH = {
    "REVENUE": ("Revenue", ALL_SECTORS),
    "OPERATING_PROFIT": ("Operating profit (Screener)", GENERAL),
    "EBITDA": ("EBITDA", GENERAL),
    "EBIT": ("EBIT", GENERAL),
    "PBT": ("PBT", ALL_SECTORS),
    "PAT": ("PAT", ALL_SECTORS),
    "EPS": ("EPS", ALL_SECTORS),
    "CFO": ("CFO", GENERAL),
    "FCF": ("FCF", GENERAL),
    "TOTAL_ASSETS": ("Total assets", ALL_SECTORS),
    "EQUITY": ("Equity", ALL_SECTORS),
    "GROSS_DEBT": ("Gross debt", GENERAL),
    "RECEIVABLES": ("Trade receivables", GENERAL),
    "INVENTORY": ("Inventory", GENERAL),
    "PAYABLES": ("Trade payables", GENERAL),
    "COGS": ("COGS", GENERAL),
    "CAPEX": ("Capex", GENERAL),
    "SHARE_CAPITAL": ("Equity share capital", ALL_SECTORS),
    "DEPOSITS": ("Deposits", (Sector.FINANCIAL,)),
    "ADVANCES": ("Advances", (Sector.FINANCIAL,)),
}
for _name, (_label, _sectors) in _GROWTH.items():
    _f(
        f"{_name}_GROWTH_YOY",
        f"{_label} growth (YoY)",
        C.GROWTH,
        U.PERCENT,
        "(current / same period one year earlier - 1) x 100",
        (_name, f"{_name} one year earlier"),
        periods=YOY,
        sectors=_sectors,
        notes="the base must be > 0; the same canonical metric at both endpoints",
    )
for _name in ("REVENUE", "OPERATING_PROFIT", "EBITDA", "PAT", "EPS"):
    _label, _sectors = _GROWTH[_name]
    _f(
        f"{_name}_GROWTH_QOQ",
        f"{_label} growth (QoQ)",
        C.GROWTH,
        U.PERCENT,
        "(quarter / previous quarter - 1) x 100",
        (_name, f"{_name} previous quarter"),
        periods=(P.QUARTER,),
        sectors=_sectors,
        notes="not seasonally adjusted",
    )
for _name in (
    "REVENUE",
    "OPERATING_PROFIT",
    "EBITDA",
    "EBIT",
    "PBT",
    "PAT",
    "EPS",
    "CFO",
    "FCF",
    "TOTAL_ASSETS",
    "EQUITY",
    "DEPOSITS",
    "ADVANCES",
):
    _label, _sectors = _GROWTH[_name]
    for _years in (3, 5, 10):
        _f(
            f"{_name}_CAGR_{_years}Y",
            f"{_label} {_years}-year CAGR",
            C.GROWTH,
            U.PERCENT,
            f"((end / start) ^ (1/{_years}) - 1) x 100",
            (_name, f"{_name} {_years} years earlier"),
            sectors=_sectors,
            notes="both endpoints must be > 0",
        )

# profitability
for _code, _name, _num, _sectors, _periods in (
    ("EBITDA_MARGIN", "EBITDA margin (reported, else from PBT: incl. other income and "
     "exceptional items)", "EBITDA", GENERAL, FLOWS),
    (
        "OPERATING_MARGIN",
        "Operating margin before D&A and other income (Screener operating profit)",
        "OPERATING_PROFIT",
        GENERAL,
        FLOWS,
    ),
    ("EBIT_MARGIN", "EBIT margin (reported, else PBT + finance cost: incl. other income and "
     "exceptional items)", "EBIT", GENERAL, FLOWS),
    ("OPERATING_EBIT_MARGIN", "Operating EBIT margin (operating profit - D&A; excludes other "
     "income)", "OPERATING_EBIT", GENERAL, FLOWS),
    ("PBT_MARGIN", "PBT margin", "PBT", ALL_SECTORS, FLOWS),
    ("PAT_MARGIN", "PAT (net) margin", "PAT", ALL_SECTORS, FLOWS),
    ("EMPLOYEE_COST_TO_REVENUE", "Employee cost / revenue", "EMPLOYEE_COST", ALL_SECTORS, FY),
    (
        "RAW_MATERIAL_COST_TO_REVENUE",
        "Raw-material cost / revenue",
        "RAW_MATERIAL_COST",
        GENERAL,
        FY,
    ),
    ("POWER_AND_FUEL_TO_REVENUE", "Power and fuel / revenue", "POWER_AND_FUEL", GENERAL, FY),
    (
        "OTHER_OPERATING_COST_TO_REVENUE",
        "Other operating cost / revenue",
        "OTHER_OPERATING_EXPENSE",
        GENERAL,
        FY,
    ),
    ("OTHER_EXPENSES_TO_REVENUE", "Other expenses / revenue", "OTHER_EXPENSES", GENERAL, FY),
    ("DEPRECIATION_TO_REVENUE", "Depreciation / revenue", "DEPRECIATION_AMORTIZATION", GENERAL, FY),
    ("FINANCE_COST_TO_REVENUE", "Finance cost / revenue", "FINANCE_COST", GENERAL, FY),
):
    _f(
        _code,
        _name,
        C.PROFITABILITY,
        U.PERCENT,
        f"{_num} / revenue x 100",
        (_num, "REVENUE"),
        periods=_periods,
        sectors=_sectors,
        notes="revenue must be > 0",
    )
_f(
    "GROSS_MARGIN",
    "Gross margin",
    C.PROFITABILITY,
    U.PERCENT,
    "(revenue - COGS) / revenue x 100",
    ("REVENUE", "COGS"),
)
_f(
    "OTHER_INCOME_TO_PBT",
    "Other income / PBT",
    C.PROFITABILITY,
    U.PERCENT,
    "other income / PBT x 100",
    ("OTHER_INCOME", "PBT"),
    notes="PBT must be > 0",
)
_f(
    "EXCEPTIONAL_TO_PBT",
    "Exceptional items / PBT",
    C.PROFITABILITY,
    U.PERCENT,
    "|exceptional items| / PBT x 100",
    ("EXCEPTIONAL_ITEMS", "PBT"),
    sectors=ALL_SECTORS,
    notes="PBT must be > 0",
)
_f(
    "TAX_RATE",
    "Effective tax rate",
    C.PROFITABILITY,
    U.PERCENT,
    "tax / PBT x 100",
    ("TOTAL_TAX | CURRENT_TAX, DEFERRED_TAX | PAT", "PBT"),
    periods=FLOWS,
    sectors=ALL_SECTORS,
    variants=(
        ("TOTAL_TAX", "total tax / PBT"),
        ("COMPONENTS", "(current tax + deferred tax) / PBT"),
        ("IMPLIED", "(PBT - PAT) / PBT [implied; flagged]"),
    ),
    notes="PBT must be > 0",
)
_f(
    "TAX_RATE_CHANGE_YOY",
    "Effective tax rate change (YoY)",
    C.PROFITABILITY,
    U.PERCENTAGE_POINTS,
    "tax rate - tax rate one year earlier",
    ("TAX_RATE", "TAX_RATE prior"),
    sectors=ALL_SECTORS,
)
_f(
    "TAX_RATE_RANGE_5Y",
    "Effective tax rate range (5Y)",
    C.PROFITABILITY,
    U.PERCENTAGE_POINTS,
    "max - min tax rate over the last 5 fiscal years",
    ("TAX_RATE x >= 3 years",),
    sectors=ALL_SECTORS,
)
for _code in ("EBITDA_MARGIN", "OPERATING_MARGIN", "PBT_MARGIN", "PAT_MARGIN"):
    _sectors = ALL_SECTORS if _code in ("PBT_MARGIN", "PAT_MARGIN") else GENERAL
    _f(
        f"{_code}_CHANGE_YOY",
        f"{_code.title().replace('_', ' ')} change (YoY)",
        C.PROFITABILITY,
        U.BASIS_POINTS,
        "(margin - margin one year earlier) x 100",
        (_code, f"{_code} prior"),
        periods=YOY,
        sectors=_sectors,
    )
    for _years in (3, 5):
        _f(
            f"{_code}_CHANGE_{_years}Y",
            f"{_code.title().replace('_', ' ')} change ({_years}Y)",
            C.PROFITABILITY,
            U.BASIS_POINTS,
            f"(margin - margin {_years} years earlier) x 100",
            (_code, f"{_code} {_years} years earlier"),
            sectors=_sectors,
        )
_f(
    "OTHER_INCOME_TO_PBT_CHANGE_YOY",
    "Other income / PBT change (YoY)",
    C.PROFITABILITY,
    U.PERCENTAGE_POINTS,
    "ratio - ratio one year earlier",
    ("OTHER_INCOME_TO_PBT", "OTHER_INCOME_TO_PBT prior"),
)

# returns
_AVG = "opening and closing balances must both exist"
_f(
    "ROE",
    "Return on equity",
    C.RETURNS,
    U.PERCENT,
    "PAT / average equity x 100",
    ("PAT", "SHAREHOLDERS_EQUITY opening", "SHAREHOLDERS_EQUITY closing"),
    optional=("PAT_ATTRIBUTABLE_TO_OWNERS", "MINORITY_INTEREST"),
    sectors=ALL_SECTORS,
    notes=_AVG + "; the owners' accounting-equity claim (incl. hybrid equity) pairs with "
    "PAT attributable to owners when reported; total PAT only when no non-controlling "
    "interest is reported (else unavailable); with total equity as reported, total PAT",
)
_f(
    "ROA",
    "Return on assets",
    C.RETURNS,
    U.PERCENT,
    "PAT / average total assets x 100",
    ("PAT", "TOTAL_ASSETS opening", "TOTAL_ASSETS closing"),
    sectors=ALL_SECTORS,
    notes=_AVG,
)
_f(
    "ROCE",
    "Return on capital employed",
    C.RETURNS,
    U.PERCENT,
    "EBIT / average capital employed x 100",
    ("EBIT", "CAPITAL_EMPLOYED opening", "CAPITAL_EMPLOYED closing"),
    notes=_AVG,
)
_f(
    "ROIC",
    "Return on invested capital",
    C.RETURNS,
    U.PERCENT,
    "NOPAT / average invested capital x 100",
    ("NOPAT", "INVESTED_CAPITAL opening", "INVESTED_CAPITAL closing"),
    notes="NOPAT = EBIT x (1 - effective tax rate); invested capital = equity + gross debt - "
    "cash; " + _AVG,
)
_f(
    "RETURN_ON_TANGIBLE_CAPITAL",
    "Return on tangible capital employed",
    C.RETURNS,
    U.PERCENT,
    "EBIT / average (capital employed - goodwill - intangible assets) x 100",
    ("EBIT", "CAPITAL_EMPLOYED", "GOODWILL", "INTANGIBLE_ASSETS (opening and closing)"),
    notes=_AVG,
)
_f(
    "ASSET_TURNOVER",
    "Asset turnover",
    C.RETURNS,
    U.TIMES,
    "revenue / average total assets",
    ("REVENUE", "TOTAL_ASSETS opening", "TOTAL_ASSETS closing"),
    notes=_AVG,
)
_f(
    "FIXED_ASSET_TURNOVER",
    "Fixed-asset turnover",
    C.RETURNS,
    U.TIMES,
    "revenue / average PPE (or fixed assets)",
    ("REVENUE", "PROPERTY_PLANT_EQUIPMENT | FIXED_ASSETS (opening and closing)"),
    notes=_AVG,
)
_f(
    "INCREMENTAL_ROIC_3Y",
    "Incremental ROIC (3Y)",
    C.RETURNS,
    U.PERCENT,
    "(NOPAT_t - NOPAT_t-3) / (invested capital_t - invested capital_t-3) x 100",
    ("NOPAT", "NOPAT 3 years earlier", "INVESTED_CAPITAL", "INVESTED_CAPITAL 3 years earlier"),
    notes="the capital increase must be > 0",
)
_f(
    "ROE_CHANGE_YOY",
    "ROE change (YoY)",
    C.RETURNS,
    U.PERCENTAGE_POINTS,
    "ROE - ROE one year earlier",
    ("ROE", "ROE prior"),
    sectors=ALL_SECTORS,
)
_f(
    "ROCE_CHANGE_YOY",
    "ROCE change (YoY)",
    C.RETURNS,
    U.PERCENTAGE_POINTS,
    "ROCE - ROCE one year earlier",
    ("ROCE", "ROCE prior"),
)

# DuPont
_f(
    "DUPONT_NET_MARGIN",
    "DuPont net margin",
    C.DUPONT,
    U.PERCENT,
    "PAT / revenue x 100",
    ("PAT", "REVENUE"),
)
_f(
    "DUPONT_ASSET_TURNOVER",
    "DuPont asset turnover",
    C.DUPONT,
    U.TIMES,
    "revenue / average total assets",
    ("REVENUE", "TOTAL_ASSETS opening and closing"),
    notes=_AVG,
)
_f(
    "DUPONT_EQUITY_MULTIPLIER",
    "DuPont equity multiplier",
    C.DUPONT,
    U.TIMES,
    "average total assets / average equity",
    ("TOTAL_ASSETS opening and closing", "SHAREHOLDERS_EQUITY opening and closing"),
    notes=_AVG,
)
_f(
    "DUPONT_IMPLIED_ROE",
    "DuPont implied ROE",
    C.DUPONT,
    U.PERCENT,
    "net margin x asset turnover x equity multiplier",
    ("DUPONT_NET_MARGIN", "DUPONT_ASSET_TURNOVER", "DUPONT_EQUITY_MULTIPLIER"),
)
_f(
    "DUPONT_ROE_DIFFERENCE",
    "DuPont implied ROE - direct ROE",
    C.DUPONT,
    U.PERCENTAGE_POINTS,
    "implied ROE - ROE",
    ("DUPONT_IMPLIED_ROE", "ROE"),
    notes=f"|difference| above {DUPONT_TOLERANCE_PP} pp is flagged DUPONT_TOLERANCE_EXCEEDED "
    "(6-decimal quantization of drivers explains tiny differences)",
)
for _code, _unit in (
    ("DUPONT_NET_MARGIN", U.PERCENTAGE_POINTS),
    ("DUPONT_ASSET_TURNOVER", U.TIMES),
    ("DUPONT_EQUITY_MULTIPLIER", U.TIMES),
):
    _f(
        f"{_code}_CHANGE_YOY",
        f"{_code.title().replace('_', ' ')} change (YoY)",
        C.DUPONT,
        _unit,
        "driver - driver one year earlier",
        (_code, f"{_code} prior"),
    )

# leverage
for _code, _name, _expr, _req, _unit, _sectors in (
    (
        "DEBT_TO_EQUITY",
        "Debt / equity",
        "gross debt / equity",
        ("GROSS_DEBT", "SHAREHOLDERS_EQUITY"),
        U.TIMES,
        GENERAL,
    ),
    (
        "LEASE_ADJUSTED_DEBT_TO_EQUITY",
        "Lease-adjusted debt / equity",
        "(gross debt + lease liabilities) / equity",
        ("LEASE_ADJUSTED_DEBT", "SHAREHOLDERS_EQUITY"),
        U.TIMES,
        GENERAL,
    ),
    (
        "DEBT_TO_ASSETS",
        "Debt / assets",
        "gross debt / total assets",
        ("GROSS_DEBT", "TOTAL_ASSETS"),
        U.TIMES,
        GENERAL,
    ),
    (
        "DEBT_TO_CAPITAL",
        "Debt / capital",
        "gross debt / (gross debt + equity)",
        ("GROSS_DEBT", "SHAREHOLDERS_EQUITY"),
        U.TIMES,
        GENERAL,
    ),
    (
        "LIABILITIES_TO_ASSETS",
        "Liabilities / assets",
        "total liabilities / total assets",
        ("TOTAL_LIABILITIES", "TOTAL_ASSETS"),
        U.TIMES,
        GENERAL,
    ),
    (
        "NET_DEBT_TO_EQUITY",
        "Net debt / equity",
        "net debt / equity (negative = net cash)",
        ("NET_DEBT", "SHAREHOLDERS_EQUITY"),
        U.TIMES,
        GENERAL,
    ),
    (
        "DEBT_TO_EBITDA",
        "Debt / EBITDA",
        "gross debt / EBITDA; EBITDA > 0",
        ("GROSS_DEBT", "EBITDA"),
        U.TIMES,
        GENERAL,
    ),
    (
        "NET_DEBT_TO_EBITDA",
        "Net debt / EBITDA",
        "net debt / EBITDA; EBITDA > 0; NOT_APPLICABLE (NET_CASH) when net debt < 0",
        ("NET_DEBT", "EBITDA"),
        U.TIMES,
        GENERAL,
    ),
    (
        "DEBT_TO_CFO",
        "Debt / CFO",
        "gross debt / CFO; CFO > 0",
        ("GROSS_DEBT", "CFO"),
        U.TIMES,
        GENERAL,
    ),
    ("CFO_TO_DEBT", "CFO / debt", "CFO / gross debt", ("CFO", "GROSS_DEBT"), U.TIMES, GENERAL),
    (
        "CASH_TO_DEBT",
        "Cash / debt",
        "cash and cash equivalents / gross debt",
        ("CASH_AND_CASH_EQUIVALENTS", "GROSS_DEBT"),
        U.TIMES,
        GENERAL,
    ),
    (
        "INTEREST_COVERAGE",
        "Interest coverage",
        "EBIT / finance cost",
        ("EBIT", "FINANCE_COST"),
        U.TIMES,
        GENERAL,
    ),
    (
        "FINANCE_COST_COVERAGE",
        "Finance-cost coverage",
        "EBITDA / finance cost",
        ("EBITDA", "FINANCE_COST"),
        U.TIMES,
        GENERAL,
    ),
    (
        "EQUITY_TO_ASSETS",
        "Equity / assets",
        "equity / total assets x 100",
        ("SHAREHOLDERS_EQUITY", "TOTAL_ASSETS"),
        U.PERCENT,
        ALL_SECTORS,
    ),
):
    _f(
        _code,
        _name,
        C.LEVERAGE,
        _unit,
        _expr,
        _req,
        sectors=_sectors,
        notes="balance-sheet inputs as at the fiscal year end",
    )
_f(
    "INTEREST_COVERAGE_CHANGE_YOY",
    "Interest coverage change (YoY)",
    C.LEVERAGE,
    U.PERCENT,
    "(coverage / coverage one year earlier - 1) x 100",
    ("INTEREST_COVERAGE", "INTEREST_COVERAGE prior"),
)

# liquidity
_f(
    "CURRENT_RATIO",
    "Current ratio",
    C.LIQUIDITY,
    U.TIMES,
    "total current assets / total current liabilities",
    ("TOTAL_CURRENT_ASSETS", "TOTAL_CURRENT_LIABILITIES"),
)
_f(
    "QUICK_RATIO",
    "Quick ratio",
    C.LIQUIDITY,
    U.TIMES,
    "(total current assets - inventory) / total current liabilities",
    ("TOTAL_CURRENT_ASSETS", "INVENTORY", "TOTAL_CURRENT_LIABILITIES"),
    notes="a missing inventory line is never assumed zero",
)
_f(
    "CASH_RATIO",
    "Cash ratio",
    C.LIQUIDITY,
    U.TIMES,
    "cash and cash equivalents / total current liabilities",
    ("CASH_AND_CASH_EQUIVALENTS", "TOTAL_CURRENT_LIABILITIES"),
)

# working capital
_f(
    "RECEIVABLE_DAYS",
    "Receivable days",
    C.WORKING_CAPITAL,
    U.DAYS,
    "average trade receivables / revenue x 365",
    ("TRADE_RECEIVABLES opening and closing", "REVENUE"),
    notes=_AVG,
)
_f(
    "INVENTORY_DAYS",
    "Inventory days",
    C.WORKING_CAPITAL,
    U.DAYS,
    "average inventory / COGS x 365",
    ("INVENTORY opening and closing", "COGS"),
    notes="COGS is required; revenue is never used as a silent substitute",
)
_f(
    "PAYABLE_DAYS",
    "Payable days",
    C.WORKING_CAPITAL,
    U.DAYS,
    "average trade payables / COGS x 365",
    ("TRADE_PAYABLES opening and closing", "COGS"),
    notes="COGS is required; revenue is never used as a silent substitute",
)
_f(
    "CASH_CONVERSION_CYCLE",
    "Cash conversion cycle",
    C.WORKING_CAPITAL,
    U.DAYS,
    "receivable days + inventory days - payable days",
    ("RECEIVABLE_DAYS", "INVENTORY_DAYS", "PAYABLE_DAYS"),
)
for _code in ("RECEIVABLE_DAYS", "INVENTORY_DAYS"):
    _f(
        f"{_code}_CHANGE_YOY",
        f"{_code.title().replace('_', ' ')} change (YoY)",
        C.WORKING_CAPITAL,
        U.DAYS,
        "days - days one year earlier",
        (_code, f"{_code} prior"),
    )
_f(
    "NWC_TO_REVENUE",
    "Net working capital / revenue",
    C.WORKING_CAPITAL,
    U.PERCENT,
    "NWC / revenue x 100",
    ("NET_WORKING_CAPITAL", "REVENUE"),
)
_f(
    "NWC_CHANGE",
    "Working-capital investment (+) / release (-)",
    C.WORKING_CAPITAL,
    U.MONEY,
    "NWC - NWC one year earlier",
    ("NET_WORKING_CAPITAL", "NET_WORKING_CAPITAL prior"),
)
_f(
    "RECEIVABLES_TO_REVENUE",
    "Receivables / revenue",
    C.WORKING_CAPITAL,
    U.PERCENT,
    "closing receivables / revenue x 100",
    ("TRADE_RECEIVABLES", "REVENUE"),
)
_f(
    "INVENTORY_TO_REVENUE",
    "Inventory / revenue",
    C.WORKING_CAPITAL,
    U.PERCENT,
    "closing inventory / revenue x 100",
    ("INVENTORY", "REVENUE"),
)
_f(
    "RECEIVABLES_VS_REVENUE_GROWTH",
    "Receivables growth - revenue growth",
    C.WORKING_CAPITAL,
    U.PERCENTAGE_POINTS,
    "receivables YoY growth - revenue YoY growth",
    ("RECEIVABLES_GROWTH_YOY", "REVENUE_GROWTH_YOY"),
)
_f(
    "INVENTORY_VS_REVENUE_GROWTH",
    "Inventory growth - revenue growth",
    C.WORKING_CAPITAL,
    U.PERCENTAGE_POINTS,
    "inventory YoY growth - revenue YoY growth",
    ("INVENTORY_GROWTH_YOY", "REVENUE_GROWTH_YOY"),
)
_f(
    "PAYABLES_VS_COGS_GROWTH",
    "Payables growth - cost-base growth",
    C.WORKING_CAPITAL,
    U.PERCENTAGE_POINTS,
    "payables YoY growth - COGS YoY growth",
    ("PAYABLES_GROWTH_YOY", "COGS_GROWTH_YOY"),
)

# cash-flow quality
for _code, _name, _expr, _req, _unit in (
    ("CFO_MARGIN", "CFO margin", "CFO / revenue x 100", ("CFO", "REVENUE"), U.PERCENT),
    ("FCF_MARGIN", "FCF margin", "FCF / revenue x 100", ("FCF", "REVENUE"), U.PERCENT),
    ("CFO_TO_PAT", "CFO / PAT", "CFO / PAT; PAT > 0", ("CFO", "PAT"), U.TIMES),
    ("CFO_TO_EBITDA", "CFO / EBITDA", "CFO / EBITDA; EBITDA > 0", ("CFO", "EBITDA"), U.TIMES),
    ("CFO_TO_PBT", "CFO / PBT", "CFO / PBT; PBT > 0", ("CFO", "PBT"), U.TIMES),
    ("FCF_TO_PAT", "FCF conversion (FCF / PAT)", "FCF / PAT; PAT > 0", ("FCF", "PAT"), U.TIMES),
    ("FCF_TO_EBITDA", "FCF / EBITDA", "FCF / EBITDA; EBITDA > 0", ("FCF", "EBITDA"), U.TIMES),
    (
        "CAPEX_TO_REVENUE",
        "Capex / revenue",
        "capex / revenue x 100",
        ("CAPEX", "REVENUE"),
        U.PERCENT,
    ),
    (
        "CASH_TAX_RATE",
        "Cash tax rate",
        "|income taxes paid| / PBT x 100",
        ("TAX_PAID", "PBT"),
        U.PERCENT,
    ),
    (
        "CASH_TAX_VS_BOOK_TAX_GAP",
        "Cash tax rate - effective tax rate",
        "cash tax rate - tax rate",
        ("CASH_TAX_RATE", "TAX_RATE"),
        U.PERCENTAGE_POINTS,
    ),
    (
        "CASH_INTEREST_COVERAGE",
        "Cash interest coverage",
        "(CFO + |interest paid|) / |interest paid|",
        ("CFO", "INTEREST_PAID"),
        U.TIMES,
    ),
    (
        "CFO_TO_PAT_CHANGE_YOY",
        "CFO / PAT change (YoY)",
        "CFO/PAT - CFO/PAT one year earlier",
        ("CFO_TO_PAT", "CFO_TO_PAT prior"),
        U.TIMES,
    ),
    (
        "PAT_VS_CFO_GROWTH_GAP",
        "PAT growth - CFO growth",
        "PAT YoY growth - CFO YoY growth",
        ("PAT_GROWTH_YOY", "CFO_GROWTH_YOY"),
        U.PERCENTAGE_POINTS,
    ),
    (
        "PAT_VS_EPS_GROWTH_GAP",
        "PAT growth - EPS growth",
        "PAT YoY growth - EPS YoY growth",
        ("PAT_GROWTH_YOY", "EPS_GROWTH_YOY"),
        U.PERCENTAGE_POINTS,
    ),
):
    _f(_code, _name, C.CASH_FLOW_QUALITY, _unit, _expr, _req)
for _years in (3, 5):
    _f(
        f"CUMULATIVE_CFO_TO_PAT_{_years}Y",
        f"Cumulative CFO / cumulative PAT ({_years}Y)",
        C.CASH_FLOW_QUALITY,
        U.TIMES,
        f"sum CFO / sum PAT over {_years} consecutive fiscal years",
        ("CFO", "PAT", f"all {_years} years"),
    )
    _f(
        f"CUMULATIVE_FCF_{_years}Y",
        f"Cumulative FCF ({_years}Y)",
        C.CASH_FLOW_QUALITY,
        U.MONEY,
        f"sum FCF over {_years} consecutive fiscal years",
        ("FCF", f"all {_years} years"),
    )

# capital allocation
for _code, _name, _expr, _req, _unit, _sectors in (
    ("CAPEX_TO_CFO", "Capex / CFO", "capex / CFO; CFO > 0", ("CAPEX", "CFO"), U.TIMES, GENERAL),
    (
        "CAPEX_TO_DEPRECIATION",
        "Capex / depreciation",
        "capex / D&A",
        ("CAPEX", "DEPRECIATION_AMORTIZATION"),
        U.TIMES,
        GENERAL,
    ),
    (
        "ACQUISITION_SPEND",
        "Acquisitions",
        "|acquisitions| (cash flow)",
        ("ACQUISITIONS",),
        U.MONEY,
        GENERAL,
    ),
    (
        "DIVIDENDS_PAID",
        "Dividends paid",
        "|dividends paid| (cash flow)",
        ("DIVIDENDS_PAID",),
        U.MONEY,
        ALL_SECTORS,
    ),
    (
        "BUYBACK_SPEND",
        "Share buybacks",
        "|buyback| (cash flow)",
        ("BUYBACK",),
        U.MONEY,
        ALL_SECTORS,
    ),
    ("DEBT_RAISED", "Debt raised", "|debt raised| (cash flow)", ("DEBT_RAISED",), U.MONEY, GENERAL),
    ("DEBT_REPAID", "Debt repaid", "|debt repaid| (cash flow)", ("DEBT_REPAID",), U.MONEY, GENERAL),
    (
        "DIVIDEND_PAYOUT",
        "Dividend payout",
        "|dividends paid| / PAT x 100; PAT > 0",
        ("DIVIDENDS_PAID", "PAT"),
        U.PERCENT,
        ALL_SECTORS,
    ),
    (
        "DIVIDENDS_TO_FCF",
        "Dividends / FCF",
        "|dividends paid| / FCF; FCF > 0",
        ("DIVIDENDS_PAID", "FCF"),
        U.TIMES,
        GENERAL,
    ),
    (
        "SHAREHOLDER_DISTRIBUTION_TO_FCF",
        "Dividends + buybacks / FCF",
        "(|dividends| + |buybacks|) / FCF; FCF > 0",
        ("DIVIDENDS_PAID", "BUYBACK", "FCF"),
        U.TIMES,
        GENERAL,
    ),
    (
        "DIVIDENDS_TO_CFO",
        "Dividends / CFO",
        "|dividends paid| / CFO; CFO > 0",
        ("DIVIDENDS_PAID", "CFO"),
        U.TIMES,
        GENERAL,
    ),
    (
        "BUYBACK_TO_CFO",
        "Buybacks / CFO",
        "|buyback| / CFO; CFO > 0",
        ("BUYBACK", "CFO"),
        U.TIMES,
        GENERAL,
    ),
    (
        "REINVESTMENT_RATE",
        "Reinvestment rate",
        "(capex - D&A + change in NWC) / NOPAT x 100; NOPAT > 0",
        ("CAPEX", "DEPRECIATION_AMORTIZATION", "NWC_CHANGE", "NOPAT"),
        U.PERCENT,
        GENERAL,
    ),
):
    _f(_code, _name, C.CAPITAL_ALLOCATION, _unit, _expr, _req, sectors=_sectors)
_f(
    "NET_DEBT_CHANGE",
    "Net debt change",
    C.CAPITAL_ALLOCATION,
    U.MONEY,
    "net debt - net debt one year earlier",
    ("NET_DEBT", "NET_DEBT prior"),
)
_f(
    "CASH_CHANGE",
    "Cash accumulation",
    C.CAPITAL_ALLOCATION,
    U.MONEY,
    "cash and cash equivalents - one year earlier",
    ("CASH_AND_CASH_EQUIVALENTS", "CASH_AND_CASH_EQUIVALENTS prior"),
    sectors=ALL_SECTORS,
)

# accruals
_f("TOTAL_ACCRUALS", "Total accruals", C.ACCRUALS, U.MONEY, "PAT - CFO", ("PAT", "CFO"))
_f(
    "CASH_FLOW_ACCRUAL_RATIO",
    "Accrual ratio (cash-flow variant)",
    C.ACCRUALS,
    U.PERCENT,
    "(PAT - CFO) / average total assets x 100",
    ("PAT", "CFO", "TOTAL_ASSETS opening and closing"),
    notes=_AVG,
)
_f(
    "BALANCE_SHEET_ACCRUAL_RATIO",
    "Accrual ratio (balance-sheet variant)",
    C.ACCRUALS,
    U.PERCENT,
    "((d current assets - d cash) - d current liabilities - D&A) / average total assets x 100",
    (
        "TOTAL_CURRENT_ASSETS",
        "CASH_AND_CASH_EQUIVALENTS",
        "TOTAL_CURRENT_LIABILITIES",
        "DEPRECIATION_AMORTIZATION",
        "TOTAL_ASSETS (opening and closing)",
    ),
    notes=_AVG,
)
_f(
    "CASH_FLOW_ACCRUAL_RATIO_CHANGE_YOY",
    "Cash-flow accrual ratio change (YoY)",
    C.ACCRUALS,
    U.PERCENTAGE_POINTS,
    "ratio - ratio one year earlier",
    ("CASH_FLOW_ACCRUAL_RATIO", "CASH_FLOW_ACCRUAL_RATIO prior"),
)
_f(
    "NON_CASH_EARNINGS_SHARE",
    "Non-cash earnings share",
    C.ACCRUALS,
    U.PERCENT,
    "(PAT - CFO) / PAT x 100; PAT > 0",
    ("PAT", "CFO"),
)
_f(
    "DEPRECIATION_RATE",
    "Depreciation rate",
    C.ACCRUALS,
    U.PERCENT,
    "D&A / average PPE (or fixed assets) x 100",
    ("DEPRECIATION_AMORTIZATION", "PROPERTY_PLANT_EQUIPMENT | FIXED_ASSETS opening and closing"),
    notes=_AVG,
)
_f(
    "DEPRECIATION_RATE_CHANGE_YOY",
    "Depreciation rate change (YoY)",
    C.ACCRUALS,
    U.PERCENTAGE_POINTS,
    "rate - rate one year earlier",
    ("DEPRECIATION_RATE", "DEPRECIATION_RATE prior"),
)
_f(
    "CWIP_TO_FIXED_ASSETS",
    "CWIP / fixed assets",
    C.ACCRUALS,
    U.PERCENT,
    "CWIP / (PPE or fixed assets) x 100",
    ("CWIP", "PROPERTY_PLANT_EQUIPMENT | FIXED_ASSETS"),
)
_f(
    "ASSET_MIX_SHIFT",
    "Largest one-year asset-mix shift",
    C.ACCRUALS,
    U.PERCENTAGE_POINTS,
    "max |share of total assets - share one year earlier| over components present in both years",
    ("TOTAL_ASSETS", "TOTAL_ASSETS prior"),
    optional=(
        "TRADE_RECEIVABLES",
        "INVENTORY",
        "CASH_AND_CASH_EQUIVALENTS",
        "TOTAL_INVESTMENTS",
        "PROPERTY_PLANT_EQUIPMENT | FIXED_ASSETS",
    ),
)

# banking
_BANK = (Sector.FINANCIAL,)
_f(
    "COST_TO_INCOME",
    "Cost-to-income",
    C.BANKING,
    U.PERCENT,
    "bank operating expenses / (net interest income + other income) x 100",
    ("BANK_OPERATING_EXPENSES", "NET_INTEREST_INCOME", "OTHER_INCOME"),
    sectors=_BANK,
)
_f(
    "FINANCING_PROFIT_MARGIN",
    "Financing profit / revenue",
    C.BANKING,
    U.PERCENT,
    "BANK_FINANCING_PROFIT / revenue x 100",
    ("BANK_FINANCING_PROFIT", "REVENUE"),
    sectors=_BANK,
    notes="Screener financing profit (revenue - interest - expenses)",
)
_f(
    "PROVISIONS_TO_REVENUE",
    "Provisions / revenue",
    C.BANKING,
    U.PERCENT,
    "provisions and contingencies / revenue x 100",
    ("PROVISIONS_AND_CONTINGENCIES", "REVENUE"),
    sectors=_BANK,
)
for _code, _name in (("GROSS_NPA_RATIO", "Gross NPA ratio"), ("NET_NPA_RATIO", "Net NPA ratio")):
    _f(
        _code,
        _name,
        C.BANKING,
        U.PERCENT,
        "reported NPA %",
        (f"{_code} (canonical)",),
        sectors=_BANK,
        notes="NPA % is not part of the Box 4 canonical taxonomy; it is never "
        "reconstructed from raw lines",
    )


def formula(code: str) -> FormulaDefinition:
    return FORMULAS[code]


# --- non-bank financial (lender) analytics: canonical inputs only, no KPI inference --------
_f("BORROWINGS_TO_EQUITY", "Borrowings / equity (lender leverage)", C.LEVERAGE, U.TIMES,
   "total borrowings / shareholders' equity (both at the period end)",
   ("TOTAL_BORROWINGS", "SHAREHOLDERS_EQUITY"), non_bank_financial=True,
   notes="Box 4 TOTAL_BORROWINGS as reported, not the industrial gross-debt construction")
_f("BORROWINGS_TO_EQUITY_CHANGE_YOY", "Borrowings / equity change (YoY)", C.LEVERAGE,
   U.TIMES, "borrowings / equity - borrowings / equity one year earlier",
   ("BORROWINGS_TO_EQUITY", "BORROWINGS_TO_EQUITY prior"), non_bank_financial=True)
_f("BORROWINGS_GROWTH_YOY", "Borrowings growth (YoY)", C.GROWTH, U.PERCENT,
   "(borrowings / borrowings one year earlier - 1) x 100",
   ("TOTAL_BORROWINGS", "TOTAL_BORROWINGS one year earlier"), non_bank_financial=True,
   notes="the base must be > 0; the same canonical metric at both endpoints")
_f("COST_OF_BORROWINGS", "Finance cost / average borrowings", C.LEVERAGE, U.PERCENT,
   "finance cost / average(opening, closing total borrowings) x 100",
   ("FINANCE_COST", "TOTAL_BORROWINGS opening", "TOTAL_BORROWINGS closing"),
   non_bank_financial=True,
   notes="a cost-of-borrowings proxy (finance cost may include non-borrowing interest); "
   "needs both the opening and the closing balance")
_f("FINANCE_COST_TO_TOTAL_INCOME", "Finance cost / total income", C.PROFITABILITY,
   U.PERCENT, "finance cost / total income x 100", ("FINANCE_COST", "TOTAL_INCOME"),
   non_bank_financial=True, notes="TOTAL_INCOME as reported (never revenue substituted)")
_f("PAT_TO_TOTAL_INCOME", "PAT / total income", C.PROFITABILITY, U.PERCENT,
   "PAT / total income x 100", ("PAT", "TOTAL_INCOME"), non_bank_financial=True,
   notes="TOTAL_INCOME as reported (never revenue substituted)")
_f("ASSETS_VS_EQUITY_GROWTH", "Asset growth less equity growth", C.LEVERAGE,
   U.PERCENTAGE_POINTS, "total assets growth (YoY) - equity growth (YoY)",
   ("TOTAL_ASSETS_GROWTH_YOY", "EQUITY_GROWTH_YOY"), non_bank_financial=True,
   notes="positive: the balance sheet grows faster than equity (rising leverage)")
