"""DETAILED_RESEARCH_V1 Box 8 registries (management-registry/1, management-language/1,
guidance-outcome-rules/1).

Every rule is an explicit, versioned pattern:
  * transcript structure: event titles, participant headings, Q&A start, deferral phrases
  * statement types, topics, forward-looking / historical markers, stated guidance change
  * guidance subjects with sector applicability, the trusted actual each one may be
    compared with, and whether that actual measures EXACTLY the same thing (PROXY links
    are kept for context but never scored)
  * outcome directionality (only where 'higher is better' is unambiguous)
  * descriptive hedging / certainty lexicons
"""

import re
from dataclasses import dataclass
from uuid import UUID

from stock_research_ai.domain.management_intelligence import (
    Equivalence,
    EventType,
    GuidanceUnit,
    Measure,
    StatedChange,
    StatementType,
    SubjectLayer,
)

ST = StatementType

# --- transcript structure ---------------------------------------------------------------------

EVENT_TITLE = re.compile(
    r"(?P<title>(?:Q[1-4]\s*(?:FY)?\s*'?\d{2,4}\s+)?"
    r"(?:media conference call|press conference(?: call)?"
    r"|earnings (?:conference )?call|analyst(?:s)? (?:and investors? )?(?:meet|call|day)"
    r"|investor(?:s)? (?:day|meet)|annual general meeting|conference call))",
    re.I,
)
EVENT_TYPES: tuple[tuple[re.Pattern[str], EventType], ...] = (
    (re.compile(r"media|press", re.I), EventType.MEDIA_CONFERENCE),
    (re.compile(r"earnings", re.I), EventType.EARNINGS_CALL),
    (re.compile(r"analyst", re.I), EventType.ANALYST_MEET),
    (re.compile(r"investor", re.I), EventType.INVESTOR_DAY),
    (re.compile(r"annual general meeting", re.I), EventType.AGM),
)
EXPLICIT_DATE = re.compile(
    r"\b(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>[A-Z][a-z]+),?\s+(?P<y1>\d{4})"
    r"|(?P<m2>[A-Z][a-z]+)\s+(?P<d2>\d{1,2}),\s+(?P<y2>\d{4}))\b"
)
PERIOD_LABEL = re.compile(
    r"\b(?:Q(?P<q>[1-4])\s*(?:FY)?\s*'?(?P<qy>\d{4}|\d{2})\b"
    r"|(?:FY|fiscal(?: year)?|financial year)\s*'?(?P<fy>\d{4}|\d{2})"
    r"(?:\s*[-–/]\s*(?P<fy2>\d{4}|\d{2}))?\b)",
    re.I,
)
PARTICIPANT_HEADINGS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"^(?:corporate|company) participants|^management(?: panel)?$|^company"
            r" representatives",
            re.I,
        ),
        "MANAGEMENT",
    ),
    (re.compile(r"^analysts?|^conference call participants|^participants$", re.I), "ANALYST"),
    (
        re.compile(r"^jou?r?nalists?\b|^media(?: participants)?$", re.I),
        "MEDIA",
    ),  # 'JOUNALISTS' as printed
)
MODERATOR_LABEL = re.compile(r"^(moderator|operator|coordinator|host)\b", re.I)
QA_START = re.compile(
    r"question[- ]and[- ]answer|first question|open (?:the floor|it|the call|the line)(?: up)?"
    r" for questions|begin the question",
    re.I,
)
QUESTIONER_INTRO = re.compile(
    r"question[^.]{0,80}?from(?: the line of)?\s+(?P<name>[A-Z][A-Za-z.’'\- ]{0,60}?[A-Za-z]{2})"
    r"(?=\s+(?:from|of|with)\b|[,.]|$)"
)
DEFERRAL = re.compile(
    r"\b(?:we(?:'|’)ll get back|we will get back|get back to you|take (?:it|this) offline"
    r"|not disclos\w*|cannot disclose|can't disclose|won't (?:be able to )?comment"
    r"|not (?:in a position|able) to (?:comment|share|disclose)|we do not give|we don't give"
    r" (?:specific|that)|would not like to comment)\b",
    re.I,
)
SENTENCE_END = re.compile(r"[.!?][\"'’”)]*\s+(?=[A-Z(\"'‘“])")
ABBREVIATIONS = {"mr", "ms", "mrs", "dr", "rs", "no", "vs", "st", "co", "inc", "ltd", "approx"}

# --- statements --------------------------------------------------------------------------------

STATEMENT_RULES: tuple[tuple[StatementType, re.Pattern[str]], ...] = (
    (ST.GUIDANCE, re.compile(r"\bguidance\b|\boutlook for\b", re.I)),
    (ST.TARGET, re.compile(r"\btarget(?:ing|s|ed)?\b|\baim(?:ing)? to\b|\bgoal of\b", re.I)),
    (
        ST.DIVIDEND_BUYBACK,
        re.compile(
            r"\bdividend|\bbuy-?back|\bpayout\b|return(?:ing)? cash"
            r" to shareholders",
            re.I,
        ),
    ),
    (
        ST.M_AND_A,
        re.compile(
            r"\bacqui(?:re|red|sition|sitions)\b|\bmerger\b|\bdivest\w*"
            r"|\binorganic\b",
            re.I,
        ),
    ),
    (ST.CAPEX, re.compile(r"\bcap(?:ital)?[- ]?ex(?:penditure)?\b", re.I)),
    (ST.CAPACITY, re.compile(r"\bcapacity\b|\butili[sz]ation\b|\bcommission(?:ing|ed)\b", re.I)),
    (
        ST.ORDER_BOOK,
        re.compile(
            r"\border (?:book|backlog|inflow)s?\b|\bdeal (?:wins|pipeline|tcv)"
            r"|\blarge deals?\b|\bpipeline\b",
            re.I,
        ),
    ),
    (ST.DEBT, re.compile(r"\bdebt\b|\bborrowings?\b|\bleverage\b|\brefinanc\w*", re.I)),
    (
        ST.WORKING_CAPITAL,
        re.compile(r"working capital|\breceivables?\b|\bdso\b|\binventor(?:y|ies)", re.I),
    ),
    (
        ST.CAPITAL_ALLOCATION,
        re.compile(
            r"capital allocation|\bfree cash flow\b|\bfcf\b"
            r"|\braise capital|\bqip\b|rights issue",
            re.I,
        ),
    ),
    (ST.MARGIN, re.compile(r"\bmargins?\b|\bnim\b|\bnet interest margin", re.I)),
    (ST.GROWTH, re.compile(r"\bgrowth\b|\bgrow(?:ing|n)?\b", re.I)),
    (ST.PRICING, re.compile(r"\bpric(?:e|es|ing)\b|\brealis?ation\b|\byields?\b", re.I)),
    (
        ST.COST,
        re.compile(r"\bcosts?\b|\bwage (?:hike|increase)|\bsalary hikes?\b|\bexpenses\b", re.I),
    ),
    (ST.DEMAND, re.compile(r"\bdemand\b|\bspend(?:ing)?\b|\bbudgets?\b|\bdiscretionary\b", re.I)),
    (
        ST.RISK,
        re.compile(r"\buncertain\w*|\brisks?\b|\bheadwinds?\b|\bvolatil\w*|\bmacro\b", re.I),
    ),
    (
        ST.GOVERNANCE,
        re.compile(r"\bboard\b|\bsuccession\b|\bappoint\w*|\bgovernance\b|\baudit", re.I),
    ),
    (ST.STRATEGY, re.compile(r"\bstrateg\w*|\bpriorit\w*|\bfocus (?:on|area)", re.I)),
    (
        ST.EXPLANATION,
        re.compile(
            r"\bbecause\b|\bdue to\b|\bdriven by\b|\bon account of\b"
            r"|\bthe reason\b",
            re.I,
        ),
    ),
    (
        ST.OPERATIONAL_CLAIM,
        re.compile(
            r"\bclients?\b|\bcustomers?\b|\bemployees?\b|\bhired?\b"
            r"|\bfreshers\b|\bheadcount\b|\bbranch(?:es)?\b",
            re.I,
        ),
    ),
)
EXPECTATION = re.compile(
    r"\b(?:we expect|expect(?:ed|s)? to|we anticipate|we see|we believe|should (?:be|see|come)"
    r"|going forward|in the coming|will (?:be|see|continue|remain|grow|improve|decline))\b",
    re.I,
)
FORWARD = re.compile(
    r"\b(?:will|shall|expect\w*|anticipat\w*|plan(?:s|ned|ning)? to|we plan|intend\w*|aim\w*"
    r"|target\w*"
    r"|guidance|going forward|next (?:year|quarter|financial year)|outlook|projected|forecast)\b",
    re.I,
)
HISTORICAL_GUIDANCE = re.compile(
    r"\b(?:had (?:given|said|guided)|we had|earlier guidance|previous guidance|original guidance"
    r"|last (?:quarter|year)(?:'s)? guidance|guidance (?:was|of last)|which was)\b",
    re.I,
)
STATED_CHANGE: tuple[tuple[re.Pattern[str], StatedChange], ...] = (
    (
        re.compile(
            r"\bwithdraw\w*|\bsuspend\w* (?:our |the )?guidance|no longer (?:provid|giv)\w*"
            r" guidance",
            re.I,
        ),
        StatedChange.WITHDRAWN,
    ),
    (
        re.compile(
            r"\b(?:rais(?:e|ed|ing)|upgrad\w*|increas(?:e|ed|ing)) (?:our |the )?(?:[\w-]+ )"
            r"{0,3}guidance|guidance[^.]{0,40}\bupwards?\b",
            re.I,
        ),
        StatedChange.RAISED,
    ),
    (
        re.compile(
            r"\b(?:lower(?:ed|ing)?|cut(?:ting)?|reduc(?:e|ed|ing)) (?:our |the )?(?:[\w-]+ )"
            r"{0,3}guidance|guidance[^.]{0,40}\bdownwards?\b",
            re.I,
        ),
        StatedChange.LOWERED,
    ),
    (
        re.compile(
            r"\b(?:remains? (?:the same|unchanged)|maintain\w*|retain\w*|reiterat\w*"
            r"|unchanged|hold the guidance)\b",
            re.I,
        ),
        StatedChange.MAINTAINED,
    ),
    (
        re.compile(
            r"\b(?:chang(?:e|ed|ing)|revis(?:e|ed|ing)) (?:our |the )?(?:[\w-]+ ){0,3}"
            r"guidance|\bnew (?:[\w-]+ ){0,3}guidance\b",
            re.I,
        ),
        StatedChange.REVISED,
    ),
)

TOPICS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GUIDANCE", re.compile(r"\bguidance\b", re.I)),
    (
        "REVENUE_GROWTH",
        re.compile(r"revenue growth|\btop[- ]line|grow(?:th)? in (?:revenue|cc)", re.I),
    ),
    ("MARGINS", re.compile(r"\bmargins?\b", re.I)),
    ("DEMAND", re.compile(r"\bdemand\b|\bspend(?:ing)?\b|\bbudgets?\b|\bdiscretionary\b", re.I)),
    ("PRICING", re.compile(r"\bpric(?:e|es|ing)\b", re.I)),
    ("DEALS", re.compile(r"\bdeals?\b|\bpipeline\b|\btcv\b|\border (?:book|inflow)", re.I)),
    ("AI", re.compile(r"\bai\b|\bgenai\b|\bgenerative ai\b|\bagentic\b|\bagents?\b", re.I)),
    (
        "TALENT",
        re.compile(
            r"\bhir(?:e|ed|ing)\b|\bfreshers\b|\battrition\b|\bheadcount\b"
            r"|\bwage|\bsalary",
            re.I,
        ),
    ),
    ("ACQUISITIONS", re.compile(r"\bacqui(?:re|red|sition|sitions)\b", re.I)),
    ("CAPITAL_RETURN", re.compile(r"\bdividend|\bbuy-?back|\bpayout\b", re.I)),
    ("CAPEX", re.compile(r"\bcap(?:ital)?[- ]?ex(?:penditure)?\b", re.I)),
    ("CAPACITY", re.compile(r"\bcapacity\b|\butili[sz]ation\b", re.I)),
    ("MACRO", re.compile(r"\bmacro\b|\btariff\w*|\bgeopolitic\w*|\buncertain\w*", re.I)),
    ("CLIENT_SPECIFIC", re.compile(r"\b(?:one|a) (?:large )?client\b|\bclient decision", re.I)),
    ("TAX", re.compile(r"\btax rate\b|\beffective tax\b", re.I)),
    ("CASH_FLOW", re.compile(r"\bfree cash flow\b|\bfcf\b|\bcash conversion\b", re.I)),
    (
        "ASSET_QUALITY",
        re.compile(
            r"\bnpa\b|\basset quality\b|\bslippage\w*|\bcredit cost\w*"
            r"|\bprovision\w*",
            re.I,
        ),
    ),
    ("DEPOSITS", re.compile(r"\bdeposits?\b|\bcasa\b", re.I)),
    (
        "CREDIT_GROWTH",
        re.compile(
            r"\bloan (?:book|growth)\b|\badvances\b|\bcredit growth"
            r"|\bdisburs\w*",
            re.I,
        ),
    ),
    ("RETURNS", re.compile(r"\broa\b|\broe\b|\breturn on (?:assets|equity)", re.I)),
    (
        "CAPITAL_ADEQUACY",
        re.compile(r"\bcapital adequacy\b|\bcrar\b|\bcet ?1\b|\btier[- ]?1", re.I),
    ),
    ("REGULATION", re.compile(r"\bregulat\w*|\brbi\b|\bsebi\b|\blabou?r codes?\b", re.I)),
    ("LEADERSHIP", re.compile(r"\bceo\b|\bsuccession\b|\bappoint\w*", re.I)),
)

# --- guidance subjects --------------------------------------------------------------------------

UNIVERSAL = "UNIVERSAL"
NON_FINANCIAL = "NON_FINANCIAL"
FINANCIAL = "FINANCIAL"


@dataclass(frozen=True)
class SubjectRule:
    code: str
    pattern: re.Pattern[str]
    measure: Measure
    unit: GuidanceUnit
    sectors: str  # UNIVERSAL / NON_FINANCIAL / FINANCIAL
    layer: SubjectLayer
    target_code: str | None
    equivalence: Equivalence
    note: str
    higher_is_better: bool | None  # None = directionality not registered


def _s(
    code: str,
    pattern: str,
    measure: Measure,
    unit: GuidanceUnit,
    sectors: str,
    layer: SubjectLayer,
    target: str | None,
    equivalence: Equivalence,
    note: str,
    higher: bool | None,
) -> SubjectRule:
    return SubjectRule(
        code,
        re.compile(pattern, re.I),
        measure,
        unit,
        sectors,
        layer,
        target,
        equivalence,
        note,
        higher,
    )


M, U, L, E = Measure, GuidanceUnit, SubjectLayer, Equivalence
# Order matters: the first matching rule names the subject.
SUBJECTS: tuple[SubjectRule, ...] = (
    _s(
        "EBITDA_MARGIN",
        r"\bebitda margins?\b",
        M.MARGIN,
        U.PERCENT,
        NON_FINANCIAL,
        L.BOX5_METRIC,
        "EBITDA_MARGIN",
        E.EXACT,
        "EBITDA / revenue",
        True,
    ),
    _s(
        "EBIT_MARGIN",
        r"\bebit margins?\b",
        M.MARGIN,
        U.PERCENT,
        NON_FINANCIAL,
        L.BOX5_METRIC,
        "EBIT_MARGIN",
        E.EXACT,
        "EBIT / revenue",
        True,
    ),
    _s(
        "PAT_MARGIN",
        r"\b(?:pat|net profit|net) margins?\b",
        M.MARGIN,
        U.PERCENT,
        UNIVERSAL,
        L.BOX5_METRIC,
        "PAT_MARGIN",
        E.EXACT,
        "PAT / revenue",
        True,
    ),
    _s(
        "OPERATING_MARGIN",
        r"\boperating margins?\b|\bmargin guidance\b",
        M.MARGIN,
        U.PERCENT,
        NON_FINANCIAL,
        L.BOX5_METRIC,
        "OPERATING_MARGIN",
        E.PROXY,
        "a company's 'operating margin' is its own definition; Box 5 OPERATING_MARGIN is built "
        "from Screener operating profit (before depreciation)",
        True,
    ),
    _s(
        "NIM",
        r"\bnim\b|\bnet interest margins?\b",
        M.MARGIN,
        U.PERCENT,
        FINANCIAL,
        L.UNLINKED,
        None,
        E.NONE,
        "no trusted net-interest-margin actual exists upstream",
        True,
    ),
    _s(
        "TAX_RATE",
        r"\beffective tax rates?\b|\betr\b",
        M.RATIO,
        U.PERCENT,
        UNIVERSAL,
        L.BOX5_METRIC,
        "TAX_RATE",
        E.EXACT,
        "total tax / PBT",
        None,
    ),
    _s(
        "FCF_CONVERSION",
        r"\bfree cash flows?\b[^.]{0,40}\b(?:of|to) (?:net profit|pat)\b",
        M.RATIO,
        U.PERCENT,
        NON_FINANCIAL,
        L.BOX5_METRIC,
        "FCF_TO_PAT",
        E.PROXY,
        "company FCF definitions vary",
        True,
    ),
    _s(
        "ADVANCES_GROWTH",
        r"\badvances growth\b|\bgrowth in advances\b",
        M.GROWTH,
        U.PERCENT,
        FINANCIAL,
        L.UNLINKED,
        None,
        E.NONE,
        "Box 5 calculates no advances growth metric",
        True,
    ),
    _s(
        "LOAN_GROWTH",
        r"\b(?:loan|credit|loan book) growth\b",
        M.GROWTH,
        U.PERCENT,
        FINANCIAL,
        L.UNLINKED,
        None,
        E.NONE,
        "Box 5 calculates no loan growth metric",
        True,
    ),
    _s(
        "DEPOSIT_GROWTH",
        r"\bdeposits? growth\b|\bgrowth in deposits\b",
        M.GROWTH,
        U.PERCENT,
        FINANCIAL,
        L.UNLINKED,
        None,
        E.NONE,
        "Box 5 calculates no deposit growth metric",
        True,
    ),
    _s(
        "ROA",
        r"\b(?:core )?roa\b|\breturn on (?:average )?assets\b",
        M.RATIO,
        U.PERCENT,
        FINANCIAL,
        L.BOX5_METRIC,
        "ROA",
        E.PROXY,
        "company ROA may be 'core' or annualised",
        True,
    ),
    _s(
        "CREDIT_COST",
        r"\bcredit costs?\b",
        M.RATIO,
        U.PERCENT,
        FINANCIAL,
        L.BOX7_KPI,
        "CREDIT_COST",
        E.EXACT,
        "reported credit cost",
        False,
    ),
    _s(
        "GROSS_NPA",
        r"\bgross npa\b|\bgnpa\b",
        M.RATIO,
        U.PERCENT,
        FINANCIAL,
        L.BOX5_METRIC,
        "GROSS_NPA_RATIO",
        E.EXACT,
        "gross NPA / gross advances",
        False,
    ),
    _s(
        "NET_NPA",
        r"\bnet npa\b|\bnnpa\b",
        M.RATIO,
        U.PERCENT,
        FINANCIAL,
        L.BOX5_METRIC,
        "NET_NPA_RATIO",
        E.EXACT,
        "net NPA / net advances",
        False,
    ),
    _s(
        "COST_TO_INCOME",
        r"\bcost[- ]to[- ]income\b",
        M.RATIO,
        U.PERCENT,
        FINANCIAL,
        L.BOX5_METRIC,
        "COST_TO_INCOME",
        E.EXACT,
        "operating expenses / total income",
        False,
    ),
    _s(
        "CAPITAL_ADEQUACY",
        r"\bcapital adequacy\b|\bcrar\b|\bcet ?1\b",
        M.RATIO,
        U.PERCENT,
        FINANCIAL,
        L.UNLINKED,
        None,
        E.NONE,
        "no trusted capital-adequacy actual upstream",
        True,
    ),
    _s(
        "CASA_RATIO",
        r"\bcasa ratio\b",
        M.RATIO,
        U.PERCENT,
        FINANCIAL,
        L.BOX7_KPI,
        "CASA_RATIO",
        E.EXACT,
        "reported CASA ratio",
        True,
    ),
    _s(
        "REVENUE_GROWTH",
        r"\brevenue growth\b|\b(?:growth|grow) (?:in|of) (?:our )?revenues?\b"
        r"|\brevenue guidance\b|\bgrowth guidance\b|\bguidance for growth\b|\bexpect [^.]{0,20}"
        r"\bgrowth to be\b",
        M.GROWTH,
        U.PERCENT,
        NON_FINANCIAL,
        L.BOX5_METRIC,
        "REVENUE_GROWTH_YOY",
        E.EXACT,
        "revenue, year on year (reported currency)",
        True,
    ),
    _s(
        "CAPEX",
        r"\bcap(?:ital)?[- ]?ex(?:penditure)?\b",
        M.LEVEL,
        U.MONEY,
        UNIVERSAL,
        L.BOX4_METRIC,
        "CAPEX",
        E.EXACT,
        "capital expenditure (cash flow)",
        None,
    ),
    _s(
        "ORDER_BOOK",
        r"\border (?:book|backlog)\b",
        M.LEVEL,
        U.MONEY,
        NON_FINANCIAL,
        L.BOX7_KPI,
        "ORDER_BOOK",
        E.EXACT,
        "unexecuted order book",
        True,
    ),
    _s(
        "CAPACITY",
        r"\b(?:installed )?capacity\b",
        M.LEVEL,
        U.UNKNOWN,
        NON_FINANCIAL,
        L.BOX7_KPI,
        "CAPACITY",
        E.PROXY,
        "capacity status (installed / operational) may differ",
        True,
    ),
    _s(
        "HEADCOUNT",
        r"\bheadcount\b|\btotal employees\b",
        M.LEVEL,
        U.COUNT,
        UNIVERSAL,
        L.BOX7_KPI,
        "EMPLOYEES",
        E.EXACT,
        "employees at the date",
        None,
    ),
    _s(
        "DIVIDEND_PAYOUT",
        r"\b(?:dividend )?payout ratio\b",
        M.RATIO,
        U.PERCENT,
        UNIVERSAL,
        L.BOX5_METRIC,
        "DIVIDEND_PAYOUT",
        E.PROXY,
        "company payout policies use their own base",
        None,
    ),
)
SUBJECT_INDEX = {rule.code: rule for rule in SUBJECTS}
CONSTANT_CURRENCY = re.compile(r"\bconstant currency\b|\bcc terms\b|\bin cc\b", re.I)
REPORTED_CURRENCY = re.compile(
    r"\breported currency\b|\b(?:inr|rupee|reported) terms\b|\breported (?:revenue )?growth\b",
    re.I,
)
#: subjects whose actual is in reported currency: guidance counts as EXACT only when the
#: sentence itself says reported / rupee terms (constant currency or unstated -> PROXY)
CURRENCY_SENSITIVE = {"REVENUE_GROWTH"}
#: the number describes an impact / contribution, not the guided level
IMPACT = re.compile(
    r"\bimpact\b|\beffect\b|\bheadwinds?\b|\btailwinds?\b|\bbenefit\b|\bcontribution\b"
    r"|\bbaked in\b|\bincrease of\b|\bdecrease of\b",
    re.I,
)
#: direction-only guidance: '<subject> will / should / to improve | decline ...'
DIRECTION_AFTER_SUBJECT = re.compile(
    r"^\W*(?:[\w'-]+\s+){0,4}?(?:will|should|would|is expected to|are expected to|to)\s+"
    r"(?:be\s+|further\s+|continue to\s+)*(?P<verb>improve|increase|expand|grow|rise|accelerate"
    r"|decline|decrease|reduce|moderate|contract|fall|soften|remain stable|stabili[sz]e)\b",
    re.I,
)

# --- numbers in guidance ------------------------------------------------------------------------

_NUM = r"\d+(?:\.\d+)?"
_CUR = r"(?:₹|rs\.?|inr|us\$|\$|usd)"
_SCALE = r"(?:crores?|cr\b|bn\b|billions?|millions?|mn\b|lakhs?)"
PERCENT_RANGE = re.compile(
    rf"(?<![\w.'’])(?:between\s+(?P<blo>{_NUM})\s*%?\s*and\s*(?P<bhi>{_NUM})\s*%"
    rf"|(?P<lo>{_NUM})\s*%?\s*(?:to|-|–)\s*(?P<hi>{_NUM})\s*%)",
    re.I,
)
PERCENT_POINT = re.compile(
    rf"(?:(?P<prefix>at least|minimum of|north of|upwards of|over|above|more than|up to|upto"
    rf"|maximum of|below|less than|around|about|approximately|~)\s*)?(?<![\w.'’])(?P<v>{_NUM})\s*%",
    re.I,
)
BPS = re.compile(
    rf"(?<![\w.'’])(?P<lo>{_NUM})\s*(?:(?:to|-|–)\s*(?P<hi>{_NUM})\s*)?(?:basis points|bps)\b", re.I
)
MONEY = re.compile(
    rf"(?P<cur>{_CUR})\s*(?P<lo>\d+(?:,\d+)*(?:\.\d+)?)\s*(?P<s1>{_SCALE})?"
    rf"(?:\s*(?:to|-|–)\s*{_CUR}?\s*(?P<hi>\d+(?:,\d+)*(?:\.\d+)?)\s*(?P<s2>{_SCALE})?)?",
    re.I,
)
CURRENCY_CODES = {
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "us$": "USD",
    "$": "USD",
    "usd": "USD",
}
SCALE_TO_CRORE = {
    "crore": "1",
    "crores": "1",
    "cr": "1",
    "lakh": "0.01",
    "lakhs": "0.01",
    "million": "0.1",
    "millions": "0.1",
    "mn": "0.1",
    "billion": "100",
    "billions": "100",
    "bn": "100",
}
MINIMUM_WORDS = {"at least", "minimum of", "north of", "upwards of", "over", "above", "more than"}
MAXIMUM_WORDS = {"up to", "upto", "maximum of", "below", "less than"}
APPROXIMATE_WORDS = {"around", "about", "approximately", "~"}
DIRECTION_UP = re.compile(r"\b(?:improve|increase|expand|accelerat|higher|grow|rise|up)\w*", re.I)
DIRECTION_DOWN = re.compile(r"\b(?:declin|decreas|reduc|lower|contract|moderat|down)\w*", re.I)
CONDITION = re.compile(
    r"\b(?:subject to|assuming|provided (?:that)?|depending on|if the)\b"
    r"[^.]{0,160}",
    re.I,
)
TURN_PERIOD = re.compile(
    r"\b(?:for|in|during|of) the (?:rest of the |full |current |remaining )?"
    r"(?:financial|fiscal) year\b|\b(?:for|in|during) the (?:full |whole )?year\b"
    r"|\bthis (?:financial |fiscal )?year\b",
    re.I,
)

# --- descriptive language lexicons (management-language/1) ---------------------------------------

HEDGING: tuple[str, ...] = (
    "may",
    "might",
    "could",
    "uncertain",
    "uncertainty",
    "challenging",
    "depending on",
    "subject to",
    "volatile",
    "cautious",
    "difficult to predict",
    "we hope",
    "possibly",
    "headwinds",
    "softer",
)
CERTAINTY: tuple[str, ...] = (
    "confident",
    "we will",
    "we are committed",
    "remain committed",
    "clearly",
    "strong",
    "certainly",
    "definitely",
    "on track",
    "expect to achieve",
)


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    # 'may' is matched in lower case only, so the month 'May' is not counted as hedging
    flags = 0 if phrase == "may" else re.I
    return re.compile(rf"(?<![A-Za-z]){re.escape(phrase)}(?![A-Za-z])", flags)


HEDGING_PATTERNS = {p: phrase_pattern(p) for p in HEDGING}
CERTAINTY_PATTERNS = {p: phrase_pattern(p) for p in CERTAINTY}

#: Box 8 deterministic ID namespace.
NS = UUID("8b1c6a2e-4d3f-5e7a-9c0b-1d2e3f4a5b6c")
