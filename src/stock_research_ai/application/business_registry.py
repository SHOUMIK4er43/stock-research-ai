"""DETAILED_RESEARCH_V1 Box 7 registries (business-registry/1).

Explicit, versioned vocabularies - never fuzzy matching:
  * name normalization (documented, exact after normalization),
  * segment table headings and row roles,
  * geography names with their dimension (no two names are ever assumed identical),
  * universal + sector KPI definitions with sector-aware exact aliases,
  * typed units (raw unit strings are always preserved),
  * sector family resolution from Box 4 industry + Box 1B classification names.
"""

import re
import unicodedata
from dataclasses import dataclass

from stock_research_ai.domain.business_intelligence import (
    Attribution,
    BusinessEvidenceKind,
    CapacityStatus,
    GeographyDimension,
    KPIPeriodKind,
    KPIUnit,
    SectorFamily,
    SegmentMetric,
    SegmentRole,
)
from stock_research_ai.domain.financial_canonical import (
    BusinessClass,
    CanonicalMetric,
    Currency,
    Industry,
    Scale,
    default_business_class,
)

# --- names -----------------------------------------------------------------------------------

_MARKERS = re.compile(
    r"\s*(?:[(<]\s*\d{1,2}\s*[)>J]|\[\s*\d{1,2}\s*\]|[*#^†]+|\(\s*[a-z]\s*\))(?=\s|$|[,.;:])", re.I
)


def clean_name(raw: str) -> str:
    """Display form: footnote markers ('(1)', '<2>', '*') removed, whitespace collapsed."""
    text = unicodedata.normalize("NFKC", raw)
    text = _MARKERS.sub("", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" :-–—")
    return text


def name_key(raw: str) -> str:
    """Comparison key: display form, lower case, '&' -> 'and'. Nothing else is merged."""
    text = clean_name(raw).lower().replace("&", " and ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --- segment tables ---------------------------------------------------------------------------

SEGMENT_CONTEXT = re.compile(
    r"\bsegments?\b|\bsegmental\b|\bsegment[- ]wise\b|\bbusiness segments?\b|operating segments?",
    re.I,
)

#: heading / row label (name_key, '-' and ':' stripped) -> segment metric
SEGMENT_METRIC_HEADINGS: dict[str, SegmentMetric] = {
    "segment revenue": SegmentMetric.REVENUE,
    "segment revenues": SegmentMetric.REVENUE,
    "segment revenue from operations": SegmentMetric.REVENUE,
    "revenue by business segment": SegmentMetric.REVENUE,
    "revenue by business segments": SegmentMetric.REVENUE,
    "revenues by business segment": SegmentMetric.REVENUE,
    "revenues by business segments": SegmentMetric.REVENUE,
    "revenue by segment": SegmentMetric.REVENUE,
    "segment wise revenue": SegmentMetric.REVENUE,
    "segment income": SegmentMetric.REVENUE,
    "revenue from operations": SegmentMetric.REVENUE,
    "revenue": SegmentMetric.REVENUE,
    "revenues": SegmentMetric.REVENUE,
    "gross revenue": SegmentMetric.REVENUE,
    "external revenue": SegmentMetric.EXTERNAL_REVENUE,
    "revenue from external customers": SegmentMetric.EXTERNAL_REVENUE,
    "inter segment revenue": SegmentMetric.INTER_SEGMENT_REVENUE,
    "inter-segment revenue": SegmentMetric.INTER_SEGMENT_REVENUE,
    "segment results": SegmentMetric.SEGMENT_RESULT,
    "result": SegmentMetric.SEGMENT_RESULT,
    "results": SegmentMetric.SEGMENT_RESULT,
    "segment result": SegmentMetric.SEGMENT_RESULT,
    "segmental result": SegmentMetric.SEGMENT_RESULT,
    "segmental results": SegmentMetric.SEGMENT_RESULT,
    "segment profit": SegmentMetric.SEGMENT_RESULT,
    "segment profits": SegmentMetric.SEGMENT_RESULT,
    "segment profit/(loss)": SegmentMetric.SEGMENT_RESULT,
    "segment results profit/(loss)": SegmentMetric.SEGMENT_RESULT,
    "segment profit before interest and tax": SegmentMetric.EBIT,
    "segment results (profit before interest and tax)": SegmentMetric.EBIT,
    "profit before interest and tax": SegmentMetric.EBIT,
    "segment ebit": SegmentMetric.EBIT,
    "ebit": SegmentMetric.EBIT,
    "segment ebitda": SegmentMetric.EBITDA,
    "ebitda": SegmentMetric.EBITDA,
    "segment assets": SegmentMetric.ASSETS,
    "segment liabilities": SegmentMetric.LIABILITIES,
    "capital employed": SegmentMetric.CAPITAL_EMPLOYED,
    "segment capital employed": SegmentMetric.CAPITAL_EMPLOYED,
    "capital employed (segment assets - segment liabilities)": SegmentMetric.CAPITAL_EMPLOYED,
    "capital expenditure": SegmentMetric.CAPEX,
    "segment capital expenditure": SegmentMetric.CAPEX,
    "depreciation and amortisation": SegmentMetric.DEPRECIATION,
    "depreciation and amortization": SegmentMetric.DEPRECIATION,
    "segment order book": SegmentMetric.ORDER_BOOK,
    "order book": SegmentMetric.ORDER_BOOK,
    "identifiable operating expenses": SegmentMetric.OTHER,
    "allocated expenses": SegmentMetric.OTHER,
}

#: metrics whose rows form a summable block (components -> total -> bridge)
SUMMABLE = {
    SegmentMetric.REVENUE,
    SegmentMetric.EXTERNAL_REVENUE,
    SegmentMetric.SEGMENT_RESULT,
    SegmentMetric.EBIT,
    SegmentMetric.EBITDA,
    SegmentMetric.ASSETS,
    SegmentMetric.LIABILITIES,
    SegmentMetric.CAPITAL_EMPLOYED,
    SegmentMetric.CAPEX,
    SegmentMetric.DEPRECIATION,
    SegmentMetric.ORDER_BOOK,
    SegmentMetric.REVENUE_SHARE,
}


def heading_key(label: str) -> str:
    key = name_key(label)
    key = re.sub(r"^(?:[a-z]|[ivx]{1,4}|\d{1,2})[.)]\s+", "", key)  # 'A. Segment revenue'
    return key.rstrip(":").strip()


def segment_metric_heading(label: str) -> SegmentMetric | None:
    return SEGMENT_METRIC_HEADINGS.get(heading_key(label))


_TOTAL = re.compile(r"^(?:total|grand total|sub[- ]?total|total segment \w+|segment total)\b")
_ELIM = re.compile(r"inter[- ]*segment|elimination")
_UNALLOC = re.compile(
    r"un-?allocable|un-?allocated|unallocable|corporate (?:and|&) other|corporate"
)
_COMPANY_TOTAL = re.compile(
    r"^(?:net |total )?(?:revenue|income|sales) from operations(?: \(net\))?$"
    r"|^net (?:sales|revenue)|^(?:total )?income from operations$|^revenue net of inter[- ]*segment"
    r"|^profit(?:/\(loss\))? before (?:income )?(?:tax|exceptional)"
)
_BRIDGE = re.compile(
    r"^(?:less|add)\s*[:\-]|finance (?:cost|charge)s?|interest|other income"
    r"|exceptional|tax|net profit|profit after tax"
)


def row_role(label: str, metric: SegmentMetric, after_total: bool) -> SegmentRole:
    key = name_key(label)
    if re.search(r"net of inter[- ]*segment", key):
        return SegmentRole.COMPANY_TOTAL  # 'Revenue net of inter-segment'
    if _ELIM.search(key):
        return SegmentRole.ELIMINATION
    if _UNALLOC.search(key) and (after_total or key.startswith(("less", "add"))):
        return SegmentRole.UNALLOCATED
    if _UNALLOC.search(key):
        return SegmentRole.UNALLOCATED
    if _TOTAL.search(key) and not _COMPANY_TOTAL.search(key.replace("total ", "", 1)):
        return SegmentRole.TOTAL
    if _COMPANY_TOTAL.search(re.sub(r"^(?:less|add)\s*[:\-]\s*", "", key)):
        return SegmentRole.COMPANY_TOTAL
    if after_total and _BRIDGE.search(key):
        return SegmentRole.RECONCILING_ITEM
    if key.startswith(("less", "add")):
        return SegmentRole.RECONCILING_ITEM
    return SegmentRole.REPORTABLE


_SUBTOTAL = re.compile(
    r"^(?:operating profit|ebitda|ebit|profit before (?:interest|finance)"
    r"|total|sub-?total)"
)


def bridge_sign(label: str) -> int | None:
    """+1 / -1 for a bridge line from its own wording; 0 for a printed subtotal (never
    added); None when the wording is silent."""
    key = name_key(label)
    if _SUBTOTAL.search(key):
        return 0
    if key.startswith("less"):
        return -1
    if key.startswith("add"):
        return 1
    if re.search(
        r"expense|expenditure|cost|interest|charges?\b|loss|depreciation|amorti[sz]ation", key
    ):
        return -1
    if re.search(r"income|gain", key):
        return 1
    return None


# --- geography --------------------------------------------------------------------------------

GEOGRAPHY_CONTEXT = re.compile(
    r"geograph|by region|regional|by location|client geography|domestic (?:and|&) export"
    r"|(?:revenue|sales) by (?:market|country)",
    re.I,
)

_REGIONS = {
    "north america",
    "americas",
    "america",
    "latin america",
    "south america",
    "central america",
    "europe",
    "continental europe",
    "rest of europe",
    "emea",
    "europe, middle east and africa",
    "asia pacific",
    "asia-pacific",
    "apac",
    "asia",
    "rest of asia",
    "middle east",
    "africa",
    "middle east and africa",
    "australia and new zealand",
    "anz",
    "asean",
    "saarc",
    "gcc",
    "south east asia",
    "southeast asia",
    "far east",
    "oceania",
    "cis",
}
_COUNTRIES = {
    "india",
    "united states",
    "united states of america",
    "usa",
    "us",
    "u.s.",
    "u.s.a.",
    "united kingdom",
    "uk",
    "germany",
    "france",
    "japan",
    "china",
    "singapore",
    "australia",
    "canada",
    "mexico",
    "brazil",
    "south africa",
    "netherlands",
    "switzerland",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "italy",
    "spain",
    "belgium",
    "ireland",
    "poland",
    "united arab emirates",
    "uae",
    "saudi arabia",
    "qatar",
    "israel",
    "philippines",
    "malaysia",
    "indonesia",
    "thailand",
    "vietnam",
    "south korea",
    "korea",
    "hong kong",
    "taiwan",
    "new zealand",
    "bangladesh",
    "sri lanka",
    "nepal",
    "nigeria",
    "kenya",
    "egypt",
    "turkey",
    "russia",
    "argentina",
    "chile",
    "colombia",
    "peru",
}
_DOMESTIC = {"domestic", "within india", "in india", "domestic market", "domestic sales"}
_EXPORT = {"export", "exports", "outside india", "overseas", "export sales", "international"}
_GROUPS = {
    "rest of the world",
    "rest of world",
    "row",
    "others",
    "other countries",
    "other regions",
    "rest of the countries",
    "other geographies",
}


def geography_dimension(label: str) -> GeographyDimension | None:
    key = name_key(label)
    if key in ("total", "grand total") or key.startswith("total "):
        return GeographyDimension.TOTAL
    if key in _REGIONS:
        return GeographyDimension.REGION
    if key in _COUNTRIES:
        return GeographyDimension.COUNTRY
    if key in _DOMESTIC:
        return GeographyDimension.DOMESTIC
    if key in _EXPORT:
        return GeographyDimension.EXPORT
    if key in _GROUPS:
        return GeographyDimension.MARKET_GROUP
    return None


_ATTRIBUTION = (
    (
        Attribution.CUSTOMER_LOCATION,
        re.compile(
            r"(?:domicile|location|geograph\w*)\s+of\s+(?:the\s+)?(?:customers?|clients?)"
            r"|customer(?:'s|s')?\s+location|client\s+geography|location\s+of\s+customers"
            r"|based on (?:the )?(?:customer|client)",
            re.I,
        ),
    ),
    (
        Attribution.ENTITY_LOCATION,
        re.compile(
            r"location of (?:the )?(?:selling |operating )?(?:entit|subsidiar|compan)"
            r"|domicile of (?:the )?(?:entit|subsidiar|compan)",
            re.I,
        ),
    ),
    (
        Attribution.ASSET_LOCATION,
        re.compile(r"location of (?:the )?assets|assets? (?:are )?located", re.I),
    ),
)


def attribution(texts: list[str]) -> tuple[Attribution, str | None]:
    found = {
        (kind, text.strip()[:300])
        for text in texts
        for kind, pattern in _ATTRIBUTION
        if pattern.search(text)
    }
    kinds = {k for k, _ in found}
    if len(kinds) == 1:
        kind = kinds.pop()
        return kind, sorted(t for k, t in found if k == kind)[0]
    return Attribution.UNKNOWN, None


# --- units ------------------------------------------------------------------------------------

_UNIT_WORDS: tuple[tuple[str, KPIUnit], ...] = (
    (r"%|per ?cent|percent(?:age)?", KPIUnit.PERCENT),
    (r"bps|basis points?", KPIUnit.BASIS_POINTS),
    (r"mtpa", KPIUnit.MTPA),
    (r"mwh", KPIUnit.MWH),
    (r"gwh", KPIUnit.GWH),
    (r"mw", KPIUnit.MW),
    (r"gw", KPIUnit.GW),
    (r"mus?|million units", KPIUnit.MU),
    (r"kt|kilo ?tonnes?|'000 tonnes|000 tonnes|thousand tonnes", KPIUnit.KT),
    (r"mt|mmt", KPIUnit.MT),
    (r"tonnes?|tons?", KPIUnit.TONNES),
    (r"kl|kilo ?lit(?:re|er)s?", KPIUnit.KL),
    (r"lit(?:re|er)s?", KPIUnit.LITRES),
    (r"msf|mn sq\.? ?ft\.?|million sq\.? ?ft\.?|million square feet", KPIUnit.MSF),
    (r"sq\.? ?ft\.?|square feet|sft", KPIUnit.SQ_FT),
    (r"sq\.? ?m(?:tr|etres?)?\.?|sqm|square met(?:re|er)s?", KPIUnit.SQ_M),
    (r"acres?", KPIUnit.ACRES),
    (r"rooms?|keys", KPIUnit.ROOMS),
    (r"vehicles?", KPIUnit.VEHICLES),
    (r"units?", KPIUnit.UNITS),
    (r"days?", KPIUnit.DAYS),
    (r"x|times", KPIUnit.X),
    (r"stores?", KPIUnit.STORES),
    (r"branch(?:es)?", KPIUnit.BRANCHES),
    (r"customers?|clients?", KPIUnit.CUSTOMERS),
    (r"users?", KPIUnit.USERS),
    (r"employees?|people|persons?|headcount", KPIUnit.PEOPLE),
    (r"nos\.?|no\.?|numbers?|count", KPIUnit.COUNT),
)
_SCALE_WORDS = (
    (r"crores?|cr\.?", Scale.CRORES),
    (r"lakhs?|lacs?", Scale.LAKHS),
    (r"millions?|mn|mio", Scale.MILLIONS),
    (r"billions?|bn", Scale.BILLIONS),
    (r"thousands?|'000|000s", Scale.THOUSANDS),
)
_CURRENCY_WORDS = (
    (r"₹|`|rs\.?|inr|rupees?", Currency.INR),
    (r"us\s?\$|usd|\$|us dollars?|dollars?", Currency.USD),
    (r"€|eur|euros?", Currency.EUR),
    (r"£|gbp", Currency.GBP),
)


@dataclass(frozen=True)
class UnitReading:
    unit: KPIUnit
    scale: Scale
    currency: Currency | None
    raw: str


def read_unit(raw: str) -> UnitReading | None:
    """A typed reading of a unit string ('MTPA', '₹ crore', 'in %', '₹ per tonne', 'Nos.',
    'US$ bn', 'in lakhs'). None when nothing in it is a recognised unit."""
    text = unicodedata.normalize("NFKC", raw).strip().strip("()").strip()
    lowered = re.sub(r"^(?:in|all figures in|figures in|amounts? in)\s+", "", text.lower()).strip()
    if not lowered or len(lowered) > 60:
        return None
    currency = next(
        (
            c
            for pattern, c in _CURRENCY_WORDS
            if re.search(rf"(?<![a-z])(?:{pattern})(?![a-z])", lowered)
        ),
        None,
    )
    per_unit = re.search(
        r"\b(?:per|/)\s*(?:tonne|ton|mt|unit|kg|litre|kl|room|sq|night|kwh)", lowered
    )
    scale = next(
        (
            s
            for pattern, s in _SCALE_WORDS
            if re.search(rf"(?<![a-z])(?:{pattern})(?![a-z])", lowered)
        ),
        Scale.ONES,
    )
    if currency is not None:
        unit = KPIUnit.MONEY_PER_UNIT if per_unit else KPIUnit.MONEY
        return UnitReading(unit, scale, currency, text)
    rest = lowered
    for pattern, _ in _SCALE_WORDS:
        rest = re.sub(rf"(?<![a-z])(?:{pattern})(?![a-z])", " ", rest).strip()
    for pattern, unit in _UNIT_WORDS:
        if re.fullmatch(rf"(?:{pattern})", rest):
            return UnitReading(unit, scale, None, text)
    if scale != Scale.ONES and not rest:
        return UnitReading(KPIUnit.COUNT, scale, None, text)  # 'in lakhs' alone
    return None


_PAREN = re.compile(r"\(([^()]{1,80})\)")


@dataclass(frozen=True)
class LabelParts:
    key: str  # alias key: parentheses removed, markers removed, '%' removed
    unit: UnitReading | None
    definitions: tuple[str, ...]  # parenthesised text that is not a unit (kept verbatim)
    percent_sign: bool


def split_label(raw: str) -> LabelParts:
    text = clean_name(raw)
    unit: UnitReading | None = None
    definitions: list[str] = []
    for inner in _PAREN.findall(text):
        reading = read_unit(inner)
        if reading is not None and unit is None:
            unit = reading
        else:
            definitions.append(inner.strip())
    stripped = _PAREN.sub(" ", text)
    percent = "%" in stripped
    stripped = stripped.replace("%", " ")
    for pattern in (r"\bin\s+(?:₹|rs\.?|inr|us\$)\s*\w+$", r"\bper cent\b"):
        stripped = re.sub(pattern, " ", stripped, flags=re.I)
    key = name_key(stripped).strip(" -–:,")
    return LabelParts(key, unit, tuple(definitions), percent)


# --- KPI registry -----------------------------------------------------------------------------

UNIVERSAL = "UNIVERSAL"
INDUSTRIAL = "INDUSTRIAL"  # capacity / production / volume / order book: never for FINANCIAL


@dataclass(frozen=True)
class KPIDefinition:
    code: str
    family: str  # UNIVERSAL, INDUSTRIAL or a SectorFamily value
    aliases: tuple[str, ...]  # exact alias keys (see split_label)
    period_kind: KPIPeriodKind  # FLOW_PERIOD or POINT_IN_TIME
    units: tuple[KPIUnit, ...]  # accepted unit types
    implied_unit: KPIUnit | None  # a count the label itself makes explicit
    evidence_kind: BusinessEvidenceKind
    description: str
    box4_metric: CanonicalMetric | None = None


def _k(
    code: str,
    family: str,
    period: KPIPeriodKind,
    units: tuple[KPIUnit, ...],
    implied: KPIUnit | None,
    kind: BusinessEvidenceKind,
    aliases: str,
    description: str,
    box4: CanonicalMetric | None = None,
) -> KPIDefinition:
    """One registry row; `aliases` is '|'-separated exact alias keys."""
    return KPIDefinition(
        code,
        family,
        tuple(a for a in aliases.split("|") if a),
        period,
        units,
        implied,
        kind,
        description,
        box4,
    )


F, P = KPIPeriodKind.FLOW_PERIOD, KPIPeriodKind.POINT_IN_TIME
U, E = KPIUnit, BusinessEvidenceKind
FIN, IT = SectorFamily.FINANCIAL.value, SectorFamily.IT_SERVICES.value
PWR, RE = SectorFamily.POWER_UTILITIES.value, SectorFamily.REAL_ESTATE.value
HOT, RET = SectorFamily.HOTELS.value, SectorFamily.RETAIL.value
CAPACITY_UNITS = (
    U.MW,
    U.GW,
    U.MTPA,
    U.MT,
    U.KT,
    U.TONNES,
    U.KL,
    U.LITRES,
    U.UNITS,
    U.VEHICLES,
    U.MSF,
    U.SQ_FT,
    U.ROOMS,
    U.OTHER,
)
VOLUME_UNITS = (
    U.MT,
    U.KT,
    U.TONNES,
    U.KL,
    U.LITRES,
    U.UNITS,
    U.VEHICLES,
    U.MU,
    U.MWH,
    U.GWH,
    U.MSF,
    U.SQ_FT,
    U.OTHER,
)

# fmt: off
KPI_DEFINITIONS: tuple[KPIDefinition, ...] = (
    _k("EMPLOYEES", UNIVERSAL, P, (U.PEOPLE, U.COUNT), U.PEOPLE,
       E.EMPLOYEE_COUNT,
       "total employees|employees|number of employees|no. of employees|headcount|"
       "total headcount|employee count|employee strength|total workforce",
       "people employed at the date"),
    _k("CUSTOMERS", UNIVERSAL, P, (U.CUSTOMERS, U.COUNT), U.CUSTOMERS,
       E.CUSTOMER_COUNT,
       "number of customers|total customers|customer base|customers|no. of customers",
       "customers at the date"),
    _k("USERS", UNIVERSAL, P, (U.USERS, U.COUNT), U.USERS,
       E.USER_COUNT,
       "users|number of users|active users|registered users",
       "users at the date"),
    _k("SUBSCRIBERS", UNIVERSAL, P, (U.CUSTOMERS, U.COUNT, U.USERS), U.CUSTOMERS,
       E.SUBSCRIBER_COUNT,
       "subscribers|subscriber base|number of subscribers",
       "subscribers at the date"),
    _k("STORES", UNIVERSAL, P, (U.STORES, U.COUNT), U.STORES,
       E.STORE_COUNT,
       "number of stores|store count|stores|total stores|no. of stores",
       "stores at the date"),
    _k("BRANCHES", UNIVERSAL, P, (U.BRANCHES, U.COUNT), U.BRANCHES,
       E.BRANCH_COUNT,
       "number of branches|branches|branch network|no. of branches|total branches|"
       "banking outlets",
       "branches at the date"),
    _k("DAYS_SALES_OUTSTANDING", UNIVERSAL, P, (U.DAYS,), U.DAYS,
       E.OTHER,
       "days sales outstanding|dso",
       "reported DSO (Box 5 calculates its own receivable days)"),
    _k("TOP_CLIENT_SHARE", UNIVERSAL, F, (U.PERCENT,), None,
       E.CUSTOMER_CONCENTRATION,
       "top client|top customer|largest client|largest customer",
       "% of revenue from the largest client"),
    _k("TOP_5_CLIENTS_SHARE", UNIVERSAL, F, (U.PERCENT,), None,
       E.CUSTOMER_CONCENTRATION,
       "top 5 clients|top 5 customers|top five clients|top five customers",
       "% of revenue from the top 5 clients"),
    _k("TOP_10_CLIENTS_SHARE", UNIVERSAL, F, (U.PERCENT,), None,
       E.CUSTOMER_CONCENTRATION,
       "top 10 clients|top 10 customers|top ten clients|top ten customers",
       "% of revenue from the top 10 clients"),
    _k("TOP_25_CLIENTS_SHARE", UNIVERSAL, F, (U.PERCENT,), None,
       E.CUSTOMER_CONCENTRATION,
       "top 25 clients|top 25 customers",
       "% of revenue from the top 25 clients"),
    _k("CAPACITY", INDUSTRIAL, P, CAPACITY_UNITS, None,
       E.CAPACITY,
       "installed capacity|capacity|operational capacity|operating capacity|"
       "available capacity|capacity under construction|capacity under implementation|"
       "planned capacity|proposed capacity|announced capacity|total capacity|"
       "production capacity|manufacturing capacity|rated capacity",
       "capacity at the date (status kept separate)"),
    _k("CAPACITY_UTILIZATION", INDUSTRIAL, F, (U.PERCENT,), None,
       E.UTILIZATION,
       "capacity utilisation|capacity utilization|plant utilisation|plant utilization",
       "reported capacity utilization for the period"),
    _k("PRODUCTION", INDUSTRIAL, F, VOLUME_UNITS, None,
       E.PRODUCTION,
       "production|total production|output|production volume",
       "quantity produced in the period"),
    _k("SALES_VOLUME", INDUSTRIAL, F, VOLUME_UNITS, None,
       E.VOLUME,
       "sales volume|sales volumes|volume|volumes|volume sold|total sales volume",
       "quantity sold in the period (never = production)"),
    _k("DISPATCH", INDUSTRIAL, F, VOLUME_UNITS, None,
       E.VOLUME,
       "dispatch|dispatches|despatches|despatch",
       "quantity dispatched in the period"),
    _k("REALIZATION", INDUSTRIAL, F, (U.MONEY_PER_UNIT, U.MONEY), None,
       E.REALIZATION,
       "realisation|realization|average realisation|average realization|net realisation|"
       "net realization|blended realisation|blended realization",
       "reported price per unit (never revenue / volume)"),
    _k("ORDER_BOOK", INDUSTRIAL, P, (U.MONEY,), None,
       E.ORDER_BOOK,
       "order book|order backlog|outstanding order book|unexecuted order book|"
       "order book position|closing order book",
       "unexecuted orders at the date (not revenue)"),
    _k("BACKLOG", INDUSTRIAL, P, (U.MONEY,), None,
       E.BACKLOG,
       "backlog",
       "backlog as the source defines it"),
    _k("ORDER_INFLOW", INDUSTRIAL, F, (U.MONEY,), None,
       E.ORDER_BOOK,
       "order inflow|order inflows|order intake|new orders|orders received|"
       "order received",
       "orders received in the period"),
    _k("ATTRITION", IT, F, (U.PERCENT,), None,
       E.EMPLOYEE_COUNT,
       "attrition|voluntary attrition|attrition rate|annualised attrition|ltm attrition",
       "employee attrition (definition kept)"),
    _k("IT_UTILIZATION", IT, F, (U.PERCENT,), None,
       E.UTILIZATION,
       "utilization|utilisation|utilization including trainees|"
       "utilization excluding trainees|utilisation including trainees|"
       "utilisation excluding trainees",
       "billable utilization (qualifier kept)"),
    _k("ACTIVE_CLIENTS", IT, P, (U.CUSTOMERS, U.COUNT), U.CUSTOMERS,
       E.CUSTOMER_COUNT,
       "active clients|number of active clients|active client",
       "active clients at the date"),
    _k("CLIENTS_ADDED", IT, F, (U.CUSTOMERS, U.COUNT), U.CUSTOMERS,
       E.CUSTOMER_COUNT,
       "clients added|new clients|added during the period (gross)|new clients added",
       "clients added in the period"),
    _k("LARGE_DEAL_TCV", IT, F, (U.MONEY,), None,
       E.OTHER,
       "large deal tcv|large deal value|large deals tcv|large deal total contract value",
       "large-deal TCV (never = total contract value)"),
    _k("TCV", IT, F, (U.MONEY,), None,
       E.OTHER,
       "tcv|total contract value|deal tcv|total contract value (tcv)",
       "total contract value as the source defines it"),
    _k("CLIENTS_BY_REVENUE_BAND", IT, P, (U.CUSTOMERS, U.COUNT), U.CUSTOMERS,
       E.CUSTOMER_CONCENTRATION,
       "",
       "clients above a stated revenue band ('100 million dollar +'); band kept as qualifier"),
    _k("ONSITE_EFFORT", IT, F, (U.PERCENT,), None,
       E.OTHER,
       "onsite|onsite effort",
       "% of effort onsite"),
    _k("OFFSHORE_EFFORT", IT, F, (U.PERCENT,), None,
       E.OTHER,
       "offshore|offshore effort",
       "% of effort offshore"),
    _k("DEPOSITS", FIN, P, (U.MONEY,), None,
       E.OTHER,
       "deposits|total deposits",
       "deposits at the date", CanonicalMetric.DEPOSITS),
    _k("CASA_DEPOSITS", FIN, P, (U.MONEY,), None,
       E.OTHER,
       "casa deposits|casa",
       "current + savings deposits at the date"),
    _k("CASA_RATIO", FIN, P, (U.PERCENT,), None,
       E.OTHER,
       "casa ratio|casa share|casa",
       "CASA share of deposits at the date"),
    _k("ADVANCES", FIN, P, (U.MONEY,), None,
       E.LOAN_BOOK,
       "advances|gross advances|net advances|total advances",
       "advances at the date", CanonicalMetric.ADVANCES),
    _k("LOAN_BOOK", FIN, P, (U.MONEY,), None,
       E.LOAN_BOOK,
       "loan book|loan portfolio|gross loan book",
       "loan book at the date"),
    _k("AUM", FIN, P, (U.MONEY,), None,
       E.AUM,
       "aum|assets under management|total aum",
       "assets under management at the date"),
    _k("DISBURSEMENTS", FIN, F, (U.MONEY,), None,
       E.DISBURSEMENT,
       "disbursements|disbursement|total disbursements",
       "loans disbursed in the period"),
    _k("GROSS_NPA", FIN, P, (U.PERCENT, U.MONEY), None,
       E.ASSET_QUALITY,
       "gross npa|gnpa|gross non-performing assets|gross non performing assets|"
       "gross npa ratio|gnpa ratio",
       "gross NPA (ratio or amount by unit)"),
    _k("NET_NPA", FIN, P, (U.PERCENT, U.MONEY), None,
       E.ASSET_QUALITY,
       "net npa|nnpa|net non-performing assets|net non performing assets|net npa ratio|"
       "nnpa ratio",
       "net NPA (ratio or amount by unit)"),
    _k("PROVISION_COVERAGE", FIN, P, (U.PERCENT,), None,
       E.ASSET_QUALITY,
       "provision coverage ratio|pcr|provision coverage",
       "provision coverage at the date"),
    _k("CREDIT_COST", FIN, F, (U.PERCENT, U.BASIS_POINTS), None,
       E.ASSET_QUALITY,
       "credit cost|credit costs",
       "credit cost for the period"),
    _k("NIM", FIN, F, (U.PERCENT,), None,
       E.OTHER,
       "nim|net interest margin",
       "net interest margin for the period"),
    _k("CAPITAL_ADEQUACY", FIN, P, (U.PERCENT,), None,
       E.OTHER,
       "capital adequacy ratio|crar|car|capital to risk weighted assets ratio|"
       "capital adequacy",
       "capital adequacy at the date"),
    _k("CET1_RATIO", FIN, P, (U.PERCENT,), None,
       E.OTHER,
       "cet1 ratio|cet 1 ratio|cet-1|cet1|common equity tier 1 ratio",
       "CET1 at the date"),
    _k("ATMS", FIN, P, (U.COUNT,), U.COUNT,
       E.OTHER,
       "atms|number of atms|no. of atms",
       "ATMs at the date"),
    _k("GENERATION", PWR, F, (U.MU, U.MWH, U.GWH), None,
       E.GENERATION,
       "generation|power generation|gross generation|units generated|net generation",
       "electricity generated in the period"),
    _k("PLF", PWR, F, (U.PERCENT,), None,
       E.UTILIZATION,
       "plf|plant load factor",
       "plant load factor for the period"),
    _k("RENEWABLE_CAPACITY", PWR, P, (U.MW, U.GW), None,
       E.ENERGY_CAPACITY,
       "renewable capacity|renewable energy capacity|re capacity",
       "renewable capacity at the date"),
    _k("AREA_SOLD", RE, F, (U.MSF, U.SQ_FT, U.SQ_M), None,
       E.AREA,
       "area sold|sales area|area booked",
       "area sold in the period"),
    _k("BOOKINGS", RE, F, (U.MONEY,), None,
       E.OTHER,
       "bookings|sales bookings|pre-sales|presales",
       "booking value in the period"),
    _k("COLLECTIONS", RE, F, (U.MONEY,), None,
       E.OTHER,
       "collections|customer collections",
       "collections in the period"),
    _k("LAUNCHES", RE, F, (U.MSF, U.SQ_FT, U.SQ_M), None,
       E.AREA,
       "launches|new launches|area launched",
       "area launched in the period"),
    _k("ROOM_KEYS", HOT, P, (U.ROOMS, U.COUNT), U.ROOMS,
       E.ROOM_KEYS,
       "room keys|keys|rooms|number of rooms|inventory (keys)",
       "rooms / keys at the date"),
    _k("OCCUPANCY", HOT, F, (U.PERCENT,), None,
       E.OCCUPANCY,
       "occupancy|occupancy rate|average occupancy",
       "occupancy for the period"),
    _k("ADR", HOT, F, (U.MONEY, U.MONEY_PER_UNIT), None,
       E.OTHER,
       "adr|average daily rate|average room rate|arr",
       "average daily rate for the period"),
    _k("REVPAR", HOT, F, (U.MONEY, U.MONEY_PER_UNIT), None,
       E.OTHER,
       "revpar|revenue per available room",
       "RevPAR for the period"),
    _k("SAME_STORE_GROWTH", RET, F, (U.PERCENT,), None,
       E.OTHER,
       "same store sales growth|sssg|like-for-like growth|lfl growth|"
       "like for like growth",
       "reported same-store growth (never recomputed)"),
    _k("RETAIL_AREA", RET, P, (U.SQ_FT, U.MSF, U.SQ_M), None,
       E.AREA,
       "retail area|retail space|store area|total retail area",
       "retail area at the date"),
)
# fmt: on

_CAPACITY_STATUS = (
    (
        re.compile(r"under (?:construction|implementation|execution|development)"),
        CapacityStatus.UNDER_CONSTRUCTION,
    ),
    (re.compile(r"\b(?:planned|proposed)\b"), CapacityStatus.PLANNED),
    (re.compile(r"\bannounced\b"), CapacityStatus.ANNOUNCED),
    (re.compile(r"\b(?:operational|operating|commissioned)\b"), CapacityStatus.OPERATIONAL),
    (re.compile(r"\bavailable\b"), CapacityStatus.AVAILABLE),
    (re.compile(r"\b(?:installed|rated)\b"), CapacityStatus.INSTALLED),
)


def capacity_status(key: str) -> CapacityStatus:
    return next((s for p, s in _CAPACITY_STATUS if p.search(key)), CapacityStatus.UNKNOWN)


_BAND = re.compile(r"^(\d+(?:\.\d+)?) (million|billion|mn|bn) (?:us )?(?:dollars?|usd|\$) ?\+?$")


def qualifier(code: str, key: str) -> str | None:
    if code == "CAPACITY":
        return capacity_status(key).value
    band = _BAND.match(key)
    if code == "CLIENTS_BY_REVENUE_BAND" and band:
        amount = band.group(1).replace(".", "_")
        return f"USD_{amount}{'M' if band.group(2).startswith('m') else 'B'}_PLUS"
    if "including trainees" in key:
        return "INCLUDING_TRAINEES"
    if "excluding trainees" in key:
        return "EXCLUDING_TRAINEES"
    if code in ("ADVANCES",) and key.startswith(("gross", "net")):
        return key.split()[0].upper()
    if "voluntary" in key:
        return "VOLUNTARY"
    return None


#: bank-only KPI definitions: a FINANCIAL company needs Box 4 industry BANK for these;
#: a non-bank or unresolved-subtype financial company gets the general lender KPIs only
BANK_ONLY_KPIS = frozenset({"DEPOSITS", "CASA_DEPOSITS", "CASA_RATIO", "ADVANCES",
                            "CET1_RATIO", "ATMS"})


def applies(definition: KPIDefinition, family: SectorFamily, bank: bool = False) -> bool:
    if definition.family == UNIVERSAL:
        return True
    if definition.code in BANK_ONLY_KPIS and not bank:
        return False
    if family == SectorFamily.UNKNOWN:
        return False  # an unresolved business uses no sector-specific KPI definition
    if definition.family == INDUSTRIAL:
        return family not in (SectorFamily.FINANCIAL, SectorFamily.IT_SERVICES)
    return definition.family == family.value


_BY_ALIAS: dict[str, list[KPIDefinition]] = {}
for _definition in KPI_DEFINITIONS:
    for _alias in _definition.aliases:
        _BY_ALIAS.setdefault(_alias, []).append(_definition)


@dataclass(frozen=True)
class KPIMatch:
    definition: KPIDefinition | None
    reason: str  # MAPPED / UNKNOWN_KPI / SECTOR_NOT_APPLICABLE / ALIAS_AMBIGUOUS


def match_kpi(key: str, family: SectorFamily, unit: KPIUnit | None, bank: bool = False
              ) -> KPIMatch:
    """Exact alias lookup, sector aware. One alias may serve several sectors
    ('utilisation' is billable utilization for IT, never plant utilization)."""
    found = _BY_ALIAS.get(key, [])
    if not found and _BAND.match(key):
        found = [kpi_definition("CLIENTS_BY_REVENUE_BAND")]
    if not found:
        return KPIMatch(None, "UNKNOWN_KPI")
    usable = [d for d in found if applies(d, family, bank)]
    if not usable:
        return KPIMatch(None, "SECTOR_NOT_APPLICABLE")
    if len(usable) > 1 and unit is not None:
        by_unit = [d for d in usable if unit in d.units]
        usable = by_unit or usable
    if len(usable) > 1:
        return KPIMatch(None, "ALIAS_AMBIGUOUS")
    return KPIMatch(usable[0], "MAPPED")


def kpi_definition(code: str) -> KPIDefinition:
    return next(d for d in KPI_DEFINITIONS if d.code == code)


# --- sector families --------------------------------------------------------------------------

_FAMILY_NAMES: dict[str, SectorFamily] = {
    **dict.fromkeys(
        (
            "it - software",
            "computers - software & consulting",
            "information technology",
            "it enabled services",
            "it - services",
            "it services & consulting",
            "software services",
        ),
        SectorFamily.IT_SERVICES,
    ),
    **dict.fromkeys(
        (
            "power",
            "power generation",
            "power generation & distribution",
            "integrated power utilities",
            "power - transmission",
            "power distribution",
            "power generation and distribution",
            "utilities",
        ),
        SectorFamily.POWER_UTILITIES,
    ),
    **dict.fromkeys(
        (
            "realty",
            "real estate",
            "residential, commercial projects",
            "construction - real estate",
            "real estate investment trusts (reits)",
        ),
        SectorFamily.REAL_ESTATE,
    ),
    **dict.fromkeys(
        ("hotels & resorts", "hotels", "hotels, resorts & restaurants", "hotels & restaurants"),
        SectorFamily.HOTELS,
    ),
    **dict.fromkeys(
        ("retailing", "retail", "diversified retail", "department stores", "speciality retail"),
        SectorFamily.RETAIL,
    ),
    **dict.fromkeys(
        (
            "cement & cement products",
            "cement",
            "steel",
            "iron & steel",
            "iron & steel products",
            "chemicals",
            "specialty chemicals",
            "commodity chemicals",
            "auto components",
            "auto components & equipments",
            "capital goods",
            "industrial products",
            "industrial machinery",
            "railway wagons",
            "heavy electrical equipment",
            "automobiles",
            "passenger cars & utility vehicles",
            "aluminium",
            "fertilizers",
            "paper",
            "textiles",
            "construction",
            "civil construction",
            "oil",
            "gas transmission/marketing",
            "lpg/cng/png/lng supplier",
            "refineries & marketing",
        ),
        SectorFamily.MANUFACTURING,
    ),
}


def sector_family(
    industry: Industry, classification: list[str], business_class: BusinessClass | None = None
) -> tuple[SectorFamily, str]:
    """A refinement of Box 4's ONE business classification, never a second guess: a
    financial class is FINANCIAL; an unresolved class is UNKNOWN (never a profile family or
    GENERAL); only a GENERAL company is refined by its Box 1B classification names."""
    business_class = business_class or default_business_class(industry)
    if industry in (Industry.BANK, Industry.NBFC, Industry.INSURANCE):
        return SectorFamily.FINANCIAL, f"Box 4 industry {industry.value}"
    if business_class == BusinessClass.FINANCIAL:
        return SectorFamily.FINANCIAL, "Box 4: financial business, subtype unresolved"
    if industry != Industry.GENERAL:
        return SectorFamily.UNKNOWN, "Box 4 business classification unresolved"
    found = {
        (name, _FAMILY_NAMES[name.lower().strip()])
        for name in classification
        if name.lower().strip() in _FAMILY_NAMES
    }
    families = {f for _, f in found} - {SectorFamily.FINANCIAL}
    if len(families) == 1:
        family = families.pop()
        names = sorted(n for n, f in found if f == family)
        return family, f"Box 1B classification {names[0]!r}"
    if len(families) > 1:
        return SectorFamily.GENERAL, "Box 1B classification names disagree"
    return SectorFamily.GENERAL, "Box 4 industry GENERAL; no registered classification name"


# --- business evidence ------------------------------------------------------------------------

EVIDENCE_PATTERNS: tuple[tuple[BusinessEvidenceKind, re.Pattern[str], bool], ...] = (
    # (kind, pattern, specific): specific patterns give MEDIUM confidence, keyword-only LOW
    (
        E.SEGMENT_CHANGE,
        re.compile(
            r"\b(?:re-?named|re-?organi[sz](?:ed|ation)|re-?aligned|realign(?:ed|ment)|"
            r"change[sd]? in (?:the )?(?:composition of (?:its |the )?)?(?:reportable |operating |"
            r"business )?segments?|segments? (?:has|have) been (?:merged|split|combined)|"
            r"(?:merged|split) into)\b",
            re.I,
        ),
        True,
    ),
    (
        E.SEGMENT_DESCRIPTION,
        re.compile(
            r"\bsegments? (?:of the (?:group|company) )?"
            r"(?:are|is|include|includes|comprise|comprises)\b"
            r"|\b(?:include|includes) enterprises in\b|\bsegments? represent\b",
            re.I,
        ),
        True,
    ),
    (
        E.CUSTOMER_CONCENTRATION,
        re.compile(
            r"no (?:single |one )?(?:client|customer)s? (?:individually )?accounted for"
            r"|top (?:\d+|five|ten) (?:clients|customers)|largest (?:client|customer)",
            re.I,
        ),
        True,
    ),
    (
        E.PRODUCT_SERVICE,
        re.compile(
            r"derives? (?:its |our )?revenues? from|(?:our|its|the (?:company|group|bank)'s) "
            r"(?:products|services|offerings|solutions|portfolio) (?:include|includes|comprise|"
            r"comprises|span|spans)\b"
            r"|(?:sale|sales) of (?:its )?products (?:and \w+ )?(?:like|such as)",
            re.I,
        ),
        True,
    ),
    (
        E.END_MARKET,
        re.compile(
            r"(?:industries|sectors|end[- ]markets?|verticals) (?:we |it )?serve[sd]?|"
            r"serv(?:es|ing) (?:clients|customers) (?:in|across)|enterprises in (?:the )?[A-Z]",
            re.I,
        ),
        True,
    ),
    (
        E.CUSTOMER_CHANNEL,
        re.compile(
            r"distribution (?:network|channels?)|\b(?:dealers?|distributors?) network|"
            r"direct[- ]to[- ]consumer|channel partners?|e-?commerce channel",
            re.I,
        ),
        True,
    ),
    (
        E.GEOGRAPHY_DESCRIPTION,
        re.compile(
            r"geographical (?:revenue|segment|information)|revenue by geograph|"
            r"(?:operations|presence|offices) (?:in|across) \d+ countries",
            re.I,
        ),
        True,
    ),
    (E.ORDER_BOOK, re.compile(r"\border (?:book|backlog|inflow)s?\b", re.I), False),
    (
        E.CAPACITY,
        re.compile(r"\b(?:installed |production |manufacturing )?capacity\b", re.I),
        False,
    ),
    (
        E.PROJECT,
        re.compile(
            r"\b(?:greenfield|brownfield|expansion project|new plant|"
            r"commissioned|commissioning)\b",
            re.I,
        ),
        False,
    ),
    (E.CAPEX, re.compile(r"\bcap(?:ital)? ?ex(?:penditure)?\b", re.I), False),
    (E.EMPLOYEE_COUNT, re.compile(r"\b(?:employees|headcount|workforce)\b", re.I), False),
    (E.BRANCH_COUNT, re.compile(r"\bbranch(?:es| network)\b", re.I), False),
)
