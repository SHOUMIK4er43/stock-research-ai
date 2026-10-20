"""DETAILED_RESEARCH_V1 Box 9 registries (peer-registry/1).

Every rule is explicit and versioned:
  * classification levels and peer-eligibility reasons
  * benchmark metrics: which trusted Box 4 / Box 5 record, which sector families it applies
    to, and its directionality (ranking only where 'better' is unambiguous)
  * Box 7 KPI directionality
  * company aliases for competitor resolution (exact forms only; shared aliases ambiguous)
  * patterns for competitive context, market-position claims, industry-structure topics and
    demand / pricing direction words
"""

import re
from dataclasses import dataclass
from uuid import UUID

from stock_research_ai.domain.industry_intelligence import ContextType, Directionality

#: Box 9 deterministic ID namespace.
NS = UUID("3d6f2a91-7c4e-5b8d-a1f0-9e2c4b6d8a17")

# --- classification ---------------------------------------------------------------------------

#: Screener classification levels, most specific first.
LEVELS: tuple[str, ...] = ("Industry", "Broad Industry", "Sector", "Broad Sector")
FINANCIAL = "FINANCIAL"
RANKING_CRITERIA: tuple[str, ...] = (
    "eligibility: INCLUDED before INCLUDED_LIMITED",
    "classification depth shared with the target: Industry before Broad Industry",
    "a peer Box 4 dataset is available",
    "order of the source peer table as displayed",
    "company key",
)

# --- benchmark metrics -------------------------------------------------------------------------

H, L, N = (
    Directionality.HIGHER_IS_BETTER,
    Directionality.LOWER_IS_BETTER,
    Directionality.NOT_REGISTERED,
)


@dataclass(frozen=True)
class BenchmarkMetric:
    code: str
    layer: str  # BOX5 (calculated metric) / BOX4 (canonical value)
    families: str  # ALL / NON_FINANCIAL / FINANCIAL
    directionality: Directionality
    dimension: str  # GROWTH / PROFITABILITY / CAPITAL_EFFICIENCY / ...
    money: bool  # absolute money: comparable only within one currency


def _m(
    code: str,
    families: str,
    d: Directionality,
    dimension: str,
    *,
    layer: str = "BOX5",
    money: bool = False,
) -> BenchmarkMetric:
    return BenchmarkMetric(code, layer, families, d, dimension, money)


BENCHMARK_METRICS: tuple[BenchmarkMetric, ...] = (
    # banks: Box 5 calculates revenue growth on the bank's canonical revenue line
    _m("REVENUE_GROWTH_YOY", "ALL", H, "GROWTH"),
    _m("DEPOSITS_GROWTH_YOY", "FINANCIAL", H, "GROWTH"),
    _m("ADVANCES_GROWTH_YOY", "FINANCIAL", H, "GROWTH"),
    _m("PAT_GROWTH_YOY", "ALL", H, "GROWTH"),
    _m("EBITDA_MARGIN", "NON_FINANCIAL", H, "PROFITABILITY"),
    _m("OPERATING_MARGIN", "NON_FINANCIAL", H, "PROFITABILITY"),
    _m("PAT_MARGIN", "NON_FINANCIAL", H, "PROFITABILITY"),
    _m("ROE", "ALL", H, "CAPITAL_EFFICIENCY"),
    _m("ROCE", "NON_FINANCIAL", H, "CAPITAL_EFFICIENCY"),
    _m("ROIC", "NON_FINANCIAL", H, "CAPITAL_EFFICIENCY"),
    _m("ROA", "FINANCIAL", H, "CAPITAL_EFFICIENCY"),
    _m("CFO_TO_PAT", "NON_FINANCIAL", H, "CASH_CONVERSION"),
    _m("FCF_TO_PAT", "NON_FINANCIAL", H, "CASH_CONVERSION"),
    # leverage: context-dependent (a net-cash IT company vs a capital-intensive one)
    _m("DEBT_TO_EQUITY", "NON_FINANCIAL", N, "LEVERAGE"),
    _m("INTEREST_COVERAGE", "NON_FINANCIAL", H, "LEVERAGE"),
    _m("RECEIVABLE_DAYS", "NON_FINANCIAL", N, "WORKING_CAPITAL"),
    _m("TAX_RATE", "ALL", N, "OTHER"),
    _m("DIVIDEND_PAYOUT", "ALL", N, "OTHER"),
    _m("OTHER_INCOME_TO_PBT", "NON_FINANCIAL", N, "OTHER"),
    _m("FINANCING_PROFIT_MARGIN", "FINANCIAL", H, "PROFITABILITY"),
    _m("COST_TO_INCOME", "FINANCIAL", L, "EFFICIENCY"),
    _m("GROSS_NPA_RATIO", "FINANCIAL", L, "ASSET_QUALITY"),
    _m("NET_NPA_RATIO", "FINANCIAL", L, "ASSET_QUALITY"),
    # non-bank financial (lender) analytics: Box 5 computes them for non-bank FINANCIAL
    # companies only, so a bank peer is NOT_CALCULATED, never another metric substituted
    _m("BORROWINGS_TO_EQUITY", "FINANCIAL", N, "LEVERAGE"),
    _m("COST_OF_BORROWINGS", "FINANCIAL", L, "LEVERAGE"),
    _m("PAT_TO_TOTAL_INCOME", "FINANCIAL", H, "PROFITABILITY"),
    _m("BORROWINGS_GROWTH_YOY", "FINANCIAL", N, "GROWTH"),
    # absolute size: shown, never ranked
    _m("REVENUE_FROM_OPERATIONS", "NON_FINANCIAL", N, "SCALE", layer="BOX4", money=True),
    _m("PAT", "ALL", N, "SCALE", layer="BOX4", money=True),
    _m("DEPOSITS", "FINANCIAL", N, "SCALE", layer="BOX4", money=True),
    _m("ADVANCES", "FINANCIAL", N, "SCALE", layer="BOX4", money=True),
)
METRIC_INDEX = {m.code: m for m in BENCHMARK_METRICS}


def applies(metric: BenchmarkMetric, family: str) -> bool:
    if metric.families == "ALL":
        return True
    if family == "UNKNOWN":
        return False  # an unresolved business gets business-neutral metrics only
    return (metric.families == FINANCIAL) == (family == FINANCIAL)


#: period-end gap (days) still called NEAR_ALIGNED (different fiscal calendars): never pooled
NEAR_ALIGNED_DAYS = 92

# --- Box 7 KPIs ---------------------------------------------------------------------------------

KPI_DIRECTIONALITY: dict[str, Directionality] = {
    "GROSS_NPA": L,
    "NET_NPA": L,
    "CREDIT_COST": L,
    "COST_TO_INCOME": L,
    "CASA_RATIO": H,
    "CAPACITY_UTILIZATION": H,
    "ATTRITION": L,
}

# --- company identity ---------------------------------------------------------------------------

#: exact alias forms by Screener symbol (the symbol in the company URL). Matching is exact
#: and word-bounded; forms of four characters or fewer match case-sensitively.
ALIASES: dict[str, tuple[str, ...]] = {
    "INFY": ("Infosys",),
    "TCS": ("TCS", "Tata Consultancy Services"),
    "HCLTECH": ("HCL Technologies", "HCLTech", "HCL Tech"),
    "WIPRO": ("Wipro",),
    "TECHM": ("Tech Mahindra", "TechM"),
    "LTM": ("LTIMindtree",),
    "PERSISTENT": ("Persistent Systems",),
    "COFORGE": ("Coforge",),
    "YESBANK": ("YES Bank", "Yes Bank"),
    "HDFCBANK": ("HDFC Bank",),
    "ICICIBANK": ("ICICI Bank",),
    "KOTAKBANK": ("Kotak Mahindra Bank", "Kotak Mah. Bank"),
    "AXISBANK": ("Axis Bank",),
    "IDBI": ("IDBI Bank",),
    "FEDERALBNK": ("Federal Bank",),
    "INDUSINDBK": ("IndusInd Bank",),
}
#: forms shared by several companies: never resolved
AMBIGUOUS_ALIASES: dict[str, tuple[str, ...]] = {
    "HDFC": ("HDFCBANK", "HDFCLIFE", "HDFCAMC"),
    "ICICI": ("ICICIBANK", "ICICIPRULI", "ICICIGI"),
    "Kotak": ("KOTAKBANK",),  # also Kotak Securities / Kotak Life
    "Tata": ("TCS", "TATAMOTORS", "TATASTEEL"),
    "L&T": ("LT", "LTM", "LTTS"),
    "Mahindra": ("TECHM", "M&M"),
    # 'LTM' is also 'last twelve months' ('LTM attrition declined ...')
    "LTM": ("LTM", "LAST_TWELVE_MONTHS"),
}
COMPETITION = re.compile(
    r"\bcompetitors?\b|\bcompetition\b|\bcompetitive (?:intensity|pressure|landscape|position)"
    r"|\bpeers?\b|\brivals?\b|\bvendor consolidation\b",
    re.I,
)

CONTEXT_RULES: tuple[tuple[ContextType, re.Pattern[str]], ...] = (
    (ContextType.MARKET_SHARE, re.compile(r"\bmarket share\b|\bshare of\b|\bgained share", re.I)),
    (
        ContextType.CUSTOMER_WIN,
        re.compile(
            r"\bwon\b|\bwins?\b|\bselected\b|\bvendor consolidation\b"
            r"|\btook (?:over|share)",
            re.I,
        ),
    ),
    (ContextType.CUSTOMER_LOSS, re.compile(r"\blost\b|\bloss of\b|\binsourc\w*", re.I)),
    (ContextType.PRICING, re.compile(r"\bpric(?:e|es|ing)\b|\brates?\b|\bdiscount\w*", re.I)),
    (
        ContextType.TECHNOLOGY,
        re.compile(r"\bai\b|\bgenai\b|\bautomation\b|\bcloud\b|\bplatforms?\b", re.I),
    ),
    (
        ContextType.TALENT,
        re.compile(r"\bhir(?:e|ed|ing)\b|\btalent\b|\battrition\b|\bwages?\b", re.I),
    ),
    (ContextType.M_AND_A, re.compile(r"\bacqui(?:re|red|sition)\w*|\bmerger\b", re.I)),
    (ContextType.CAPACITY, re.compile(r"\bcapacity\b|\butili[sz]ation\b", re.I)),
    (ContextType.REGULATION, re.compile(r"\bregulat\w*|\brbi\b|\bsebi\b|\bcompliance\b", re.I)),
    (ContextType.DEMAND, re.compile(r"\bdemand\b|\bspend(?:ing)?\b|\bbudgets?\b", re.I)),
    (ContextType.PRODUCT, re.compile(r"\bproducts?\b|\bofferings?\b|\bservices? line", re.I)),
)

# --- market position ------------------------------------------------------------------------------

SHARE_NUMERIC = re.compile(
    r"(?<![\w.])(?P<v1>\d+(?:\.\d+)?)\s*%\s*(?:of the )?(?:market share|share of the market"
    r"|(?:market )?share (?:in|of))"
    r"|\bmarket share (?:of|at|to|is|was|stood at)\s*(?:about |around |approximately |~)?"
    r"(?P<v2>\d+(?:\.\d+)?)\s*%",
    re.I,
)
SHARE_QUALITATIVE = re.compile(
    r"\bmarket leader\w*|\bleadership position\b|\bnumber one (?:player|provider|position)\b"
    r"|\bno\. ?1 (?:player|provider|position)\b"
    r"|\blargest (?:player|provider|lender|bank|private (?:sector )?bank|company|vendor"
    r"|(?:it )?services (?:company|provider|firm))\b"
    r"|\bleading (?:player|position|provider|share)\b|\bgain(?:ed|ing)? (?:market )?share\b"
    r"|\bmarket share gains?\b",
    re.I,
)
SHARE_CATEGORY = re.compile(
    r"\b(?:market share|share|leader\w*|largest \w+|leading \w+)"
    r"(?: of (?:about |around |approximately )?\d+(?:\.\d+)? ?%)? in (?:the )?"
    r"(?P<cat>[A-Za-z][A-Za-z &/-]{2,40}?)(?=[,.;]| market\b| segment\b| space\b|$)",
    re.I,
)
FIRST_PERSON = re.compile(r"\b(?:we|our|us)\b", re.I)
SHARE_TOLERANCE_PP = "0.5"  # percentage points: equal-share corroboration tolerance

# --- industry structure ---------------------------------------------------------------------

STRUCTURE_TOPICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "FRAGMENTATION",
        re.compile(
            r"\bfragmented\b|\bconcentrated (?:market|industry)\b"
            r"|\btop (?:three|five|\d) players\b",
            re.I,
        ),
    ),
    (
        "PRICING_ENVIRONMENT",
        re.compile(
            r"\bpricing (?:environment|pressure|power)\b"
            r"|\bprice (?:war|competition)\b",
            re.I,
        ),
    ),
    (
        "DEMAND_ENVIRONMENT",
        re.compile(
            r"\bdemand (?:environment|outlook|scenario)\b"
            r"|\bdiscretionary spend",
            re.I,
        ),
    ),
    (
        "CAPACITY_CONDITIONS",
        re.compile(r"\bovercapacity\b|\bcapacity (?:addition|constraint)", re.I),
    ),
    # regulator names alone (e.g. RBI in a monetary-policy sentence) are not structure evidence
    ("REGULATORY_STRUCTURE", re.compile(r"\bregulat(?:ory|ion|or)\b|\blicen[cs]e", re.I)),
    (
        "CUSTOMER_CONCENTRATION",
        re.compile(
            r"\btop (?:5|10|five|ten|25) clients?\b|\bclient "
            r"concentration\b|\blargest client\b",
            re.I,
        ),
    ),
    (
        "SUPPLIER_DEPENDENCE",
        re.compile(
            r"\bsupplier dependence\b|\bsingle supplier\b"
            r"|\bkey suppliers?\b",
            re.I,
        ),
    ),
    (
        "SWITCHING_COSTS",
        re.compile(
            r"\bswitching costs?\b|\bvendor consolidation\b"
            r"|\bsticky\b",
            re.I,
        ),
    ),
    ("BARRIERS_TO_ENTRY", re.compile(r"\bbarriers? to entry\b|\bentry barriers?\b", re.I)),
    ("CYCLICALITY", re.compile(r"\bcyclical\w*|\bseasonal\w*|\bfurloughs?\b", re.I)),
    (
        "TECHNOLOGY_CHANGE",
        re.compile(
            r"\bgenerative ai\b|\bgenai\b|\bagentic\b|\bautomation\b"
            r"|\bai-led\b|\bdisruption\b",
            re.I,
        ),
    ),
    (
        "CONSOLIDATION_MA",
        re.compile(
            r"\bindustry consolidation\b|\bconsolidation in the "
            r"(?:industry|market|sector)\b",
            re.I,
        ),
    ),
    (
        "GEOGRAPHIC_EXPOSURE",
        re.compile(
            r"\bnorth america\b|\beurope\b|\bunited states\b"
            r"|\bgeograph\w+",
            re.I,
        ),
    ),
)

# --- demand / pricing -----------------------------------------------------------------------

DEMAND_SUBJECT = re.compile(r"\bdemand\b|\bspend(?:ing)?\b|\bbudgets?\b", re.I)
PRICING_SUBJECT = re.compile(r"\bpric(?:e|es|ing)\b", re.I)
DIRECTION_WORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "INCREASING",
        re.compile(
            r"^(?:improv\w*|increas\w*|strong(?:er)?|pick(?:ed|ing)? ?up|grow(?:s|ing|n)?|"
            r"accelerat\w*|recover\w*|ris(?:e|es|ing)|robust|better|higher|"
            r"up(?:tick|turn))$",
            re.I,
        ),
    ),
    (
        "DECREASING",
        re.compile(
            r"^(?:declin\w*|decreas\w*|weak\w*|soft(?:er|ening|ened)?|slow\w*|"
            r"deteriorat\w*|lower|pressure[sd]?|cuts?|reduc\w*|muted|tepid)$",
            re.I,
        ),
    ),
    ("STABLE", re.compile(r"^(?:stable|steady|unchanged|flat|stabili[sz]\w*|consistent)$", re.I)),
)
SIGNAL_WINDOW = 6  # words between the subject and an explicit direction word
#: a negation within NEGATION_WINDOW words before a direction word cancels it
NEGATION = re.compile(
    r"^(?:not|no|never|neither|nor|without|isn't|aren't|wasn't|don't|"
    r"doesn't|didn't|won't|cannot|can't)$",
    re.I,
)
NEGATION_WINDOW = SIGNAL_WINDOW
