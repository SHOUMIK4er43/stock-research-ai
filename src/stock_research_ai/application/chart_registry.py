"""DETAILED_RESEARCH_V1 Box 6: the versioned chart catalogue (charts/1).

Each ChartDefinition states its sector applicability, chart type, axis (period kind),
the exact Box 4 canonical metrics or Box 5 outputs it plots, minimum points, title, and
which Box 5 anomaly / trend rules may annotate it. There is no company-specific logic;
later chart families (shareholding, segments, valuation ...) add definitions with new
source kinds without changing the planner's contract.
"""

from dataclasses import dataclass, field

from stock_research_ai.domain.financial_analytics import Sector
from stock_research_ai.domain.financial_canonical import CanonicalMetric
from stock_research_ai.domain.financial_charts import (
    CHART_REGISTRY_VERSION,
    AxisKind,
    ChartCategory,
    ChartType,
)

M = CanonicalMetric
C = ChartCategory
T = ChartType
A = AxisKind
GENERAL = (Sector.GENERAL,)
FINANCIAL = (Sector.FINANCIAL,)
ALL = (Sector.GENERAL, Sector.FINANCIAL)

REVENUE = (M.REVENUE_FROM_OPERATIONS, M.REVENUE)


@dataclass(frozen=True)
class SeriesDefinition:
    label: str
    box4: tuple[CanonicalMetric, ...] = ()  # canonical metrics (one is chosen per series)
    box5: str | None = None  # Box 5 calculated-metric code
    render_as: str = "BAR"  # BAR / LINE


@dataclass(frozen=True)
class ChartDefinition:
    code: str
    category: ChartCategory
    title: str
    chart_type: ChartType
    axis: AxisKind
    sectors: tuple[Sector, ...]
    series: tuple[SeriesDefinition, ...] = ()
    special: str | None = None  # CAGR, PIOTROSKI, ALTMAN, BENEISH, CHECKS, TRENDS, HEALTH,
    # ANOMALIES - planners that read other Box 5 structures
    min_points: int = 2
    preferred_points: int = 3
    anomaly_rules: tuple[str, ...] = ()
    trend_codes: tuple[str, ...] = ()
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def _b4(label: str, *metrics: CanonicalMetric, render: str = "BAR") -> SeriesDefinition:
    return SeriesDefinition(label, box4=metrics, render_as=render)


def _b5(label: str, code: str, render: str = "LINE") -> SeriesDefinition:
    return SeriesDefinition(label, box5=code, render_as=render)


CHARTS: tuple[ChartDefinition, ...] = (
    # --- A. growth & earnings -----------------
    ChartDefinition(
        "REVENUE_ANNUAL",
        C.GROWTH,
        "Revenue",
        T.BAR,
        A.ANNUAL_FLOW,
        ALL,
        (_b4("Revenue", *REVENUE),),
    ),
    ChartDefinition(
        "PAT_ANNUAL", C.GROWTH, "Profit after tax", T.BAR, A.ANNUAL_FLOW, ALL, (_b4("PAT", M.PAT),)
    ),
    ChartDefinition(
        "REVENUE_AND_PAT",
        C.GROWTH,
        "Revenue and profit after tax",
        T.GROUPED_BAR,
        A.ANNUAL_FLOW,
        ALL,
        (_b4("Revenue", *REVENUE), _b4("PAT", M.PAT)),
    ),
    ChartDefinition(
        "REVENUE_GROWTH",
        C.GROWTH,
        "Revenue growth (YoY)",
        T.BAR,
        A.ANNUAL_FLOW,
        ALL,
        (_b5("Revenue growth", "REVENUE_GROWTH_YOY", "BAR"),),
        trend_codes=("REVENUE_GROWTH_YOY",),
    ),
    ChartDefinition(
        "PAT_GROWTH",
        C.GROWTH,
        "PAT growth (YoY)",
        T.BAR,
        A.ANNUAL_FLOW,
        ALL,
        (_b5("PAT growth", "PAT_GROWTH_YOY", "BAR"),),
    ),
    ChartDefinition(
        "CAGR_SUMMARY",
        C.GROWTH,
        "Revenue and PAT CAGR",
        T.GROUPED_BAR,
        A.CATEGORY,
        ALL,
        special="CAGR",
        min_points=1,
        preferred_points=1,
    ),
    # --- B. margins -----------------
    ChartDefinition(
        "OPERATING_MARGIN",
        C.MARGINS,
        "Operating margin (Screener operating profit)",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Operating margin", "OPERATING_MARGIN"),),
        anomaly_rules=("OPERATING_MARGIN_SHIFT",),
        trend_codes=("OPERATING_MARGIN",),
    ),
    ChartDefinition(
        "EBITDA_MARGIN",
        C.MARGINS,
        "EBITDA margin",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("EBITDA margin", "EBITDA_MARGIN"),),
        anomaly_rules=("EBITDA_MARGIN_SHIFT",),
        trend_codes=("EBITDA_MARGIN",),
    ),
    ChartDefinition(
        "EBIT_MARGIN",
        C.MARGINS,
        "EBIT margin",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("EBIT margin", "EBIT_MARGIN"),),
    ),
    ChartDefinition(
        "PAT_MARGIN",
        C.MARGINS,
        "PAT margin",
        T.LINE,
        A.ANNUAL_FLOW,
        ALL,
        (_b5("PAT margin", "PAT_MARGIN"),),
        anomaly_rules=("PAT_MARGIN_SHIFT",),
        trend_codes=("PAT_MARGIN",),
    ),
    ChartDefinition(
        "MARGIN_COMPARISON",
        C.MARGINS,
        "Margin comparison",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (
            _b5("EBITDA margin", "EBITDA_MARGIN"),
            _b5("Operating margin (Screener)", "OPERATING_MARGIN"),
            _b5("PAT margin", "PAT_MARGIN"),
        ),
    ),
    # --- C. quarterly momentum -----------------
    ChartDefinition(
        "QUARTERLY_REVENUE",
        C.QUARTERLY,
        "Quarterly revenue",
        T.BAR,
        A.QUARTERLY,
        ALL,
        (_b4("Revenue", *REVENUE),),
        preferred_points=4,
    ),
    ChartDefinition(
        "QUARTERLY_PAT",
        C.QUARTERLY,
        "Quarterly profit after tax",
        T.BAR,
        A.QUARTERLY,
        ALL,
        (_b4("PAT", M.PAT),),
        preferred_points=4,
    ),
    ChartDefinition(
        "QUARTERLY_MARGINS",
        C.QUARTERLY,
        "Quarterly margins",
        T.LINE,
        A.QUARTERLY,
        GENERAL,
        (
            _b5("Operating margin (Screener)", "OPERATING_MARGIN"),
            _b5("EBITDA margin", "EBITDA_MARGIN"),
        ),
        preferred_points=4,
        anomaly_rules=("QUARTERLY_OPERATING_MARGIN_SHIFT",),
    ),
    ChartDefinition(
        "QUARTERLY_REVENUE_GROWTH",
        C.QUARTERLY,
        "Quarterly revenue growth (YoY vs same quarter)",
        T.BAR,
        A.QUARTERLY,
        ALL,
        (_b5("Revenue growth", "REVENUE_GROWTH_YOY", "BAR"),),
        preferred_points=4,
    ),
    ChartDefinition(
        "QUARTERLY_PAT_GROWTH",
        C.QUARTERLY,
        "Quarterly PAT growth (YoY vs same quarter)",
        T.BAR,
        A.QUARTERLY,
        ALL,
        (_b5("PAT growth", "PAT_GROWTH_YOY", "BAR"),),
        preferred_points=4,
    ),
    # --- D. returns -----------------
    ChartDefinition(
        "ROE",
        C.RETURNS,
        "Return on equity",
        T.LINE,
        A.ANNUAL_FLOW,
        ALL,
        (_b5("ROE", "ROE"),),
        trend_codes=("ROE",),
    ),
    ChartDefinition(
        "ROA",
        C.RETURNS,
        "Return on assets",
        T.LINE,
        A.ANNUAL_FLOW,
        ALL,
        (_b5("ROA", "ROA"),),
        trend_codes=("ROA",),
    ),
    ChartDefinition(
        "ROCE",
        C.RETURNS,
        "Return on capital employed",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("ROCE", "ROCE"),),
        anomaly_rules=("ROCE_DOWN_DESPITE_GROWTH",),
        trend_codes=("ROCE",),
    ),
    ChartDefinition(
        "ROIC",
        C.RETURNS,
        "Return on invested capital",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("ROIC", "ROIC"),),
        trend_codes=("ROIC",),
    ),
    ChartDefinition(
        "RETURNS_COMPARISON",
        C.RETURNS,
        "ROE, ROCE and ROIC",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("ROE", "ROE"), _b5("ROCE", "ROCE"), _b5("ROIC", "ROIC")),
    ),
    ChartDefinition(
        "DUPONT_MARGIN_AND_ROE",
        C.RETURNS,
        "DuPont: net margin and ROE",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (
            _b5("Net margin", "DUPONT_NET_MARGIN"),
            _b5("ROE (direct)", "ROE"),
            _b5("ROE (DuPont implied)", "DUPONT_IMPLIED_ROE"),
        ),
    ),
    ChartDefinition(
        "DUPONT_TURNOVER_AND_LEVERAGE",
        C.RETURNS,
        "DuPont: asset turnover and equity multiplier",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (
            _b5("Asset turnover", "DUPONT_ASSET_TURNOVER"),
            _b5("Equity multiplier", "DUPONT_EQUITY_MULTIPLIER"),
        ),
    ),
    # --- E. cash flow -----------------
    ChartDefinition(
        "CFO_ANNUAL",
        C.CASH_FLOW,
        "Cash flow from operations",
        T.BAR,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b4("CFO", M.CFO),),
        anomaly_rules=("CFO_COLLAPSE",),
    ),
    ChartDefinition(
        "PAT_VS_CFO",
        C.CASH_FLOW,
        "PAT and cash flow from operations",
        T.GROUPED_BAR,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b4("PAT", M.PAT), _b4("CFO", M.CFO)),
        anomaly_rules=("PAT_OUTGROWING_CFO", "REVENUE_UP_CFO_DOWN"),
    ),
    ChartDefinition(
        "CFO_TO_PAT",
        C.CASH_FLOW,
        "Cash conversion (CFO / PAT)",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("CFO / PAT", "CFO_TO_PAT"),),
        trend_codes=("CFO_TO_PAT",),
    ),
    ChartDefinition(
        "FCF_ANNUAL",
        C.CASH_FLOW,
        "Free cash flow (CFO - capex)",
        T.BAR,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("FCF", "FCF", "BAR"),),
    ),
    ChartDefinition(
        "CFO_VS_FCF",
        C.CASH_FLOW,
        "CFO and free cash flow",
        T.GROUPED_BAR,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b4("CFO", M.CFO), _b5("FCF", "FCF", "BAR")),
    ),
    ChartDefinition(
        "CAPEX_ANNUAL",
        C.CASH_FLOW,
        "Capital expenditure",
        T.BAR,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Capex", "CAPEX", "BAR"),),
    ),
    # --- F. balance sheet / leverage -----------------
    ChartDefinition(
        "GROSS_DEBT",
        C.BALANCE_SHEET,
        "Gross debt (excl. separately reported leases)",
        T.BAR,
        A.ANNUAL_BALANCE,
        GENERAL,
        (_b5("Gross debt", "GROSS_DEBT", "BAR"),),
    ),
    ChartDefinition(
        "CASH_BALANCE",
        C.BALANCE_SHEET,
        "Cash and cash equivalents",
        T.BAR,
        A.ANNUAL_BALANCE,
        ALL,
        (_b4("Cash", M.CASH_AND_CASH_EQUIVALENTS),),
    ),
    ChartDefinition(
        "DEBT_VS_CASH",
        C.BALANCE_SHEET,
        "Gross debt and cash",
        T.GROUPED_BAR,
        A.ANNUAL_BALANCE,
        GENERAL,
        (_b5("Gross debt", "GROSS_DEBT", "BAR"), _b4("Cash", M.CASH_AND_CASH_EQUIVALENTS)),
    ),
    ChartDefinition(
        "NET_DEBT",
        C.BALANCE_SHEET,
        "Net debt (negative = net cash)",
        T.BAR,
        A.ANNUAL_BALANCE,
        GENERAL,
        (_b5("Net debt", "NET_DEBT", "BAR"),),
    ),
    ChartDefinition(
        "DEBT_TO_EQUITY",
        C.BALANCE_SHEET,
        "Debt / equity",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Debt / equity", "DEBT_TO_EQUITY"),),
        trend_codes=("DEBT_TO_EQUITY",),
    ),
    ChartDefinition(
        "INTEREST_COVERAGE",
        C.BALANCE_SHEET,
        "Interest coverage (EBIT / finance cost)",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Interest coverage", "INTEREST_COVERAGE"),),
        anomaly_rules=("INTEREST_COVERAGE_DETERIORATION",),
        trend_codes=("INTEREST_COVERAGE",),
    ),
    # --- G. working capital -----------------
    ChartDefinition(
        "RECEIVABLE_DAYS",
        C.WORKING_CAPITAL,
        "Receivable days",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Receivable days", "RECEIVABLE_DAYS"),),
        anomaly_rules=("RECEIVABLE_DAYS_SPIKE",),
        trend_codes=("RECEIVABLE_DAYS",),
    ),
    ChartDefinition(
        "INVENTORY_DAYS",
        C.WORKING_CAPITAL,
        "Inventory days",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Inventory days", "INVENTORY_DAYS"),),
        anomaly_rules=("INVENTORY_DAYS_SPIKE",),
    ),
    ChartDefinition(
        "PAYABLE_DAYS",
        C.WORKING_CAPITAL,
        "Payable days",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Payable days", "PAYABLE_DAYS"),),
    ),
    ChartDefinition(
        "CASH_CONVERSION_CYCLE",
        C.WORKING_CAPITAL,
        "Cash conversion cycle",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Cash conversion cycle", "CASH_CONVERSION_CYCLE"),),
    ),
    ChartDefinition(
        "NWC_TO_REVENUE",
        C.WORKING_CAPITAL,
        "Net working capital / revenue",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("NWC / revenue", "NWC_TO_REVENUE"),),
    ),
    # --- H. earnings quality -----------------
    ChartDefinition(
        "OTHER_INCOME_TO_PBT",
        C.EARNINGS_QUALITY,
        "Other income / PBT",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Other income / PBT", "OTHER_INCOME_TO_PBT"),),
        anomaly_rules=("OTHER_INCOME_MATERIAL",),
        trend_codes=("OTHER_INCOME_TO_PBT",),
    ),
    ChartDefinition(
        "ACCRUAL_RATIOS",
        C.EARNINGS_QUALITY,
        "Accrual ratios",
        T.LINE,
        A.ANNUAL_FLOW,
        GENERAL,
        (
            _b5("Cash-flow accrual ratio", "CASH_FLOW_ACCRUAL_RATIO"),
            _b5("Balance-sheet accrual ratio", "BALANCE_SHEET_ACCRUAL_RATIO"),
        ),
    ),
    ChartDefinition(
        "RECEIVABLES_VS_REVENUE_GROWTH",
        C.EARNINGS_QUALITY,
        "Receivables growth minus revenue growth",
        T.BAR,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Receivables - revenue growth", "RECEIVABLES_VS_REVENUE_GROWTH", "BAR"),),
        anomaly_rules=("RECEIVABLES_OUTGROWING_REVENUE",),
    ),
    ChartDefinition(
        "INVENTORY_VS_REVENUE_GROWTH",
        C.EARNINGS_QUALITY,
        "Inventory growth minus revenue growth",
        T.BAR,
        A.ANNUAL_FLOW,
        GENERAL,
        (_b5("Inventory - revenue growth", "INVENTORY_VS_REVENUE_GROWTH", "BAR"),),
    ),
    ChartDefinition(
        "EARNINGS_QUALITY_SIGNALS",
        C.EARNINGS_QUALITY,
        "Earnings-quality screening signals",
        T.HORIZONTAL_BAR,
        A.CATEGORY,
        ALL,
        special="CHECKS",
        min_points=1,
        preferred_points=1,
    ),
    # --- I. forensic models -----------------
    ChartDefinition(
        "PIOTROSKI_COMPONENTS",
        C.FORENSIC_MODELS,
        "Piotroski F-score components",
        T.HORIZONTAL_BAR,
        A.CATEGORY,
        GENERAL,
        special="PIOTROSKI",
        min_points=1,
        preferred_points=9,
    ),
    ChartDefinition(
        "ALTMAN_COMPONENTS",
        C.FORENSIC_MODELS,
        "Altman Z'' components (book values)",
        T.HORIZONTAL_BAR,
        A.CATEGORY,
        GENERAL,
        special="ALTMAN",
        min_points=1,
        preferred_points=4,
    ),
    ChartDefinition(
        "BENEISH_COMPONENTS",
        C.FORENSIC_MODELS,
        "Beneish M-score indices",
        T.HORIZONTAL_BAR,
        A.CATEGORY,
        GENERAL,
        special="BENEISH",
        min_points=1,
        preferred_points=8,
    ),
    # --- J. analytical signals -----------------
    ChartDefinition(
        "TREND_SUMMARY",
        C.SIGNALS,
        "Trend classifications (last 5 fiscal years)",
        T.HORIZONTAL_BAR,
        A.CATEGORY,
        ALL,
        special="TRENDS",
        min_points=1,
        preferred_points=1,
    ),
    ChartDefinition(
        "ANOMALY_TIMELINE",
        C.SIGNALS,
        "Anomaly timeline (rule-based flags)",
        T.SCATTER,
        A.TIMELINE,
        ALL,
        special="ANOMALIES",
        min_points=1,
        preferred_points=1,
    ),
    ChartDefinition(
        "HEALTH_SIGNALS",
        C.SIGNALS,
        "Financial-health signals",
        T.HORIZONTAL_BAR,
        A.CATEGORY,
        ALL,
        special="HEALTH",
        min_points=1,
        preferred_points=1,
    ),
    # --- banks / NBFCs ---------------------------------------------------------------------------
    ChartDefinition(
        "DEPOSITS",
        C.BANKING,
        "Deposits",
        T.BAR,
        A.ANNUAL_BALANCE,
        FINANCIAL,
        (_b4("Deposits", M.DEPOSITS),),
    ),
    ChartDefinition(
        "ADVANCES",
        C.BANKING,
        "Advances",
        T.BAR,
        A.ANNUAL_BALANCE,
        FINANCIAL,
        (_b4("Advances", M.ADVANCES),),
    ),
    ChartDefinition(
        "DEPOSIT_GROWTH",
        C.BANKING,
        "Deposit growth (YoY)",
        T.BAR,
        A.ANNUAL_FLOW,
        FINANCIAL,
        (_b5("Deposit growth", "DEPOSITS_GROWTH_YOY", "BAR"),),
        anomaly_rules=("DEPOSITS_JUMP",),
        trend_codes=("DEPOSITS_GROWTH_YOY",),
    ),
    ChartDefinition(
        "ADVANCES_GROWTH",
        C.BANKING,
        "Advances growth (YoY)",
        T.BAR,
        A.ANNUAL_FLOW,
        FINANCIAL,
        (_b5("Advances growth", "ADVANCES_GROWTH_YOY", "BAR"),),
    ),
    ChartDefinition(
        "FINANCING_PROFIT",
        C.BANKING,
        "Financing profit (Screener)",
        T.BAR,
        A.ANNUAL_FLOW,
        FINANCIAL,
        (_b4("Financing profit", M.BANK_FINANCING_PROFIT),),
    ),
    ChartDefinition(
        "FINANCING_PROFIT_MARGIN",
        C.BANKING,
        "Financing profit / revenue",
        T.LINE,
        A.ANNUAL_FLOW,
        FINANCIAL,
        (_b5("Financing profit / revenue", "FINANCING_PROFIT_MARGIN"),),
        trend_codes=("FINANCING_PROFIT_MARGIN",),
    ),
    ChartDefinition(
        "NPA_RATIOS",
        C.BANKING,
        "Gross and net NPA ratios",
        T.LINE,
        A.ANNUAL_FLOW,
        FINANCIAL,
        (_b5("Gross NPA", "GROSS_NPA_RATIO"), _b5("Net NPA", "NET_NPA_RATIO")),
    ),
)

REGISTRY_VERSION = CHART_REGISTRY_VERSION
assert len({c.code for c in CHARTS}) == len(CHARTS), "chart codes are unique"
