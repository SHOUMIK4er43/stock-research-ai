"""DETAILED_RESEARCH_V1 Box 4: deterministic parsing of reported financial values.

- Numbers: Indian and western digit grouping, decimals, minus signs, parentheses as
  negatives, percentages. Zero, Nil, dashes, n/a, blank and unparseable stay distinct;
  a dash is never silently zero.
- Units: explicit caption patterns only ('(In ₹ crore)', 'Rs. in lakhs', 'US$ million').
  The backtick some filings use as their rupee glyph counts as a rupee marker ONLY inside
  such a caption.
- Periods: stacked column headers are read token by token (see `interpret_columns`).
- Labels: explicit alias tables; unknown labels stay unmapped.
"""

import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from stock_research_ai.domain.financial_canonical import (
    CanonicalMetric,
    Currency,
    FinancialPeriod,
    NumberStatus,
    ParsedNumber,
    PeriodType,
    Scale,
    StatementType,
)

M = CanonicalMetric

# --- numbers ------------------------------------------------------------------------------

_DASHES = {"-", "–", "—", "--", "−"}
_NA = {"na", "n.a.", "n.a", "n/a", "not applicable"}
_GROUPED = re.compile(
    r"\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3}"  # plain | western | Indian
)


def parse_financial_number(raw: str) -> ParsedNumber:
    text = unicodedata.normalize("NFC", raw).strip()
    footnote = False
    while text and text[-1] in "*#^†":
        text, footnote = text[:-1].rstrip(), True

    def special(status: NumberStatus) -> ParsedNumber:
        return ParsedNumber(raw=raw[:500], status=status, footnote_marker=footnote)

    if not text:
        return special(NumberStatus.BLANK)
    lowered = text.lower()
    if lowered == "nil":
        return special(NumberStatus.NIL)
    if text in _DASHES:
        return special(NumberStatus.DASH)
    if lowered in _NA:
        return special(NumberStatus.NOT_APPLICABLE)
    negative = False
    percent = text.endswith("%")
    body = text[:-1].strip() if percent else text
    if body.startswith("(") and body.endswith(")"):
        negative, body = True, body[1:-1].strip()
    if body[:1] in ("-", "–", "−"):
        negative, body = not negative, body[1:].strip()
    whole, _, fraction = body.partition(".")
    if not _GROUPED.fullmatch(whole) or (fraction and not fraction.isdigit()):
        return special(NumberStatus.UNPARSEABLE)
    try:
        value = Decimal(whole.replace(",", "") + (f".{fraction}" if fraction else ""))
    except InvalidOperation:
        return special(NumberStatus.UNPARSEABLE)
    value = -value if negative else value
    return ParsedNumber(
        raw=raw[:500],
        status=NumberStatus.ZERO_REPORTED if value == 0 else NumberStatus.NUMBER,
        value=value,
        is_percent=percent,
        decimals=len(fraction),
        footnote_marker=footnote,
    )


# --- units -------------------------------------------------------------------------------

_CURRENCY = (
    r"(?P<cur>₹|`|\brs\b\.?|\binr\b|\brupees\b|\bus\s?dollars?\b|\bdollars?\b|us\$|\busd\b"
    r"|\$|€|\beur\b|\beuros?\b|£|\bgbp\b)"
)
_SCALE = r"(?P<scale>crores?|cr\b\.?|lakhs?|lacs?|millions?|mn\b|billions?|bn\b|thousands?|'000)"
_CAPTIONS = [
    re.compile(rf"\(\s*(?:all\s+)?(?:amounts?\s+)?in\s+{_CURRENCY}?\s*{_SCALE}"),
    re.compile(rf"{_CURRENCY}\s*in\s+{_SCALE}"),
    re.compile(rf"^\s*(?:all\s+)?(?:amounts?\s+)?in\s+{_CURRENCY}?\s*{_SCALE}"),
]
_SHORT_CAPTION = re.compile(rf"^\s*\(?\s*{_CURRENCY}\s*{_SCALE}\s*\)?\s*$")


@dataclass(frozen=True)
class UnitCaption:
    currency: Currency
    scale: Scale
    caption: str


def _currency(token: str | None) -> Currency:
    if token is None:
        return Currency.UNKNOWN
    token = token.lower()
    if token in ("₹", "`", "rs", "rs.", "inr", "rupees"):
        return Currency.INR
    if token in ("us$", "usd", "$") or re.fullmatch(r"(?:us\s?)?dollars?", token):
        return Currency.USD  # Indian filings use '$'/'dollars' for US dollars
    if token in ("€", "eur", "euro", "euros"):
        return Currency.EUR
    if token in ("£", "gbp"):
        return Currency.GBP
    return Currency.OTHER


def _scale(token: str) -> Scale:
    token = token.lower().rstrip(".")
    if token.startswith(("crore", "cr")):
        return Scale.CRORES
    if token.startswith(("lakh", "lac")):
        return Scale.LAKHS
    if token.startswith(("million", "mn")):
        return Scale.MILLIONS
    if token.startswith(("billion", "bn")):
        return Scale.BILLIONS
    if token.startswith(("thousand", "'000")):
        return Scale.THOUSANDS
    return Scale.UNKNOWN


def parse_unit_caption(line: str) -> UnitCaption | None:
    lowered = unicodedata.normalize("NFC", line).lower().strip()
    if len(lowered) > 120:
        return None
    for pattern in _CAPTIONS:
        match = pattern.search(lowered)
        if match:
            return UnitCaption(
                _currency(match.group("cur")), _scale(match.group("scale")), line.strip()
            )
    match = _SHORT_CAPTION.match(lowered)
    if match:
        return UnitCaption(
            _currency(match.group("cur")), _scale(match.group("scale")), line.strip()
        )
    return None


# --- periods -----------------------------------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_TOKENS = re.compile(
    r"(?P<fy_year>F\.?\s*Y\.?\s*(?:19|20)\d{2})"
    r"|(?P<month_year>\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\.?\s+(?:19|20)\d{2}\b)"
    r"|(?P<kind>quarter|three months|3 months|six months|6 months|half[- ]year|nine months"
    r"|9 months|twelve months|12 months|year)"
    r"|(?P<ended>\bended\b|\bending\b)"
    r"|(?P<asat>\bas (?:at|on)\b)"
    r"|(?P<date>\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b)"
    r"|(?P<year>\b(?:19|20)\d{2}\b)"
    r"|(?P<audit>\bunaudited\b|\baudited\b|\breviewed\b)"
    r"|(?P<restated>\brestated\b)",
    re.I,
)
_EXPLICIT_PERIOD_YEAR = re.compile(
    r"\b(?:year|quarter|three months|six months|nine months|twelve months)\s+"
    r"(?:ended|ending)\s+(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|"
    r"nov|dec)[a-z]*\.?\s*,?\s*)?(?:19|20)\d{2}\b|"
    r"\byear\s+(?:19|20)\d{2}\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+"
    r"\d{1,2}(?:st|nd|rd|th)?\s*,?\s*(?:19|20)\d{2}\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|"
    r"oct|nov|dec)[a-z]*\.?\s*,?\s*(?:19|20)\d{2}\b|"
    r"\bas\s+(?:at|on)\s+(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|"
    r"dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s*)?(?:19|20)\d{2}\b",
    re.I,
)
_PERIOD_HEADER_CUE = re.compile(
    r"^\s*(?:(?:(?:particulars|note|no)\s+)*(?:year|quarter|three months|six months|"
    r"nine months|twelve months)\s+(?:ended|ending)(?:\s+(?:jan|feb|mar|apr|may|jun|"
    r"jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?)?|"
    r"as\s+(?:at|on)(?:\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?)?)\s*$",
    re.I,
)
_KINDS = {
    "quarter": (PeriodType.QUARTER, 3),
    "three months": (PeriodType.QUARTER, 3),
    "3 months": (PeriodType.QUARTER, 3),
    "six months": (PeriodType.HALF_YEAR, 6),
    "6 months": (PeriodType.HALF_YEAR, 6),
    "half year": (PeriodType.HALF_YEAR, 6),
    "half-year": (PeriodType.HALF_YEAR, 6),
    "nine months": (PeriodType.NINE_MONTH, 9),
    "9 months": (PeriodType.NINE_MONTH, 9),
    "twelve months": (PeriodType.FY, 12),
    "12 months": (PeriodType.FY, 12),
    "year": (PeriodType.FY, 12),
}


@dataclass
class ColumnHeader:
    kind: str | None = None
    month_day: str | None = None
    month_year: tuple[int, int] | None = None
    year: int | None = None
    as_at: bool = False
    audited: bool | None = None
    restated: bool = False
    words: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ColumnPeriod:
    period: FinancialPeriod
    audited: bool | None
    restated: bool
    explicit: bool  # kind and full date came from the header itself
    ambiguous: bool


def _month_day(text: str) -> tuple[int, int] | None:
    parts = re.findall(r"[a-z]+|\d+", text.lower())
    month = next((_MONTHS[p[:3]] for p in parts if p[:3] in _MONTHS), None)
    day = next((int(p) for p in parts if p.isdigit()), None)
    return (month, day) if month and day else None


def add_months(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    last = calendar.monthrange(year, month + 1)[1]
    return date(year, month + 1, min(day.day, last))


#: FinancialPeriod.fiscal_year's own bounds. A header year outside them is not a financial
#: period (e.g. the statute year in "Income Tax Act, 1961"): such a column is ambiguous.
MIN_FISCAL_YEAR, MAX_FISCAL_YEAR = 1990, 2100


def _fiscal_year(end: date, fy_end_month: int | None) -> int:
    if fy_end_month is None:
        return end.year
    return end.year if end.month <= fy_end_month else end.year + 1


def in_fiscal_range(end: date, fy_end_month: int | None) -> bool:
    return MIN_FISCAL_YEAR <= _fiscal_year(end, fy_end_month) <= MAX_FISCAL_YEAR


def period_from(
    period_type: PeriodType,
    duration: int | None,
    end: date | None,
    label: str,
    fy_end_month: int | None,
) -> FinancialPeriod:
    if end is not None and not in_fiscal_range(end, fy_end_month):
        # not a financial period: kept as an UNKNOWN column, never a fiscal period
        period_type, duration, end = PeriodType.UNKNOWN, None, None
    start = (
        add_months(end + timedelta(days=1), -duration)
        if end is not None and duration is not None
        else None
    )
    fiscal_year = fiscal_quarter = None
    if end is not None and fy_end_month is not None and period_type != PeriodType.TTM:
        fiscal_year = end.year if end.month <= fy_end_month else end.year + 1
        if period_type == PeriodType.QUARTER:
            fiscal_quarter = ((end.month - fy_end_month - 1) % 12) // 3 + 1
    return FinancialPeriod(
        period_type=period_type,
        label=label[:500],
        period_start=start,
        period_end=end,
        duration_months=duration,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
    )


def interpret_columns(
    header_lines: list[str],
    count: int,
    statement: StatementType,
    fy_end_month: int | None,
) -> list[ColumnPeriod]:
    """Period per value column from stacked header lines (reading order, positions lost).

    Per line: when the line has exactly one column-token per value column, tokens are
    assigned positionally. Otherwise, per attribute: as many items as columns -> by
    position; as many as columns still lacking it -> fill in order. A lone year binds to at
    most one column; period and audit labels may still apply across columns.
    """
    columns = [ColumnHeader() for _ in range(count)]
    tokenized = [
        (line, [(m.lastgroup or "", m.group(0)) for m in _TOKENS.finditer(line)])
        for line in header_lines
    ]
    standalone_years = [
        (index, int(tokens[0][1]))
        for index, (line, tokens) in enumerate(tokenized)
        if len(tokens) == 1
        and tokens[0][0] == "year"
        and re.fullmatch(r"\s*(?:19|20)\d{2}\s*", line)
    ]
    standalone_values = [year for _, year in standalone_years]
    first_standalone_index = standalone_years[0][0] if standalone_years else 0
    preceding_header_line = next(
        (
            header_lines[index].strip()
            for index in range(first_standalone_index - 1, -1, -1)
            if header_lines[index].strip()
        ),
        "",
    )
    stacked_year_indices = {
        index
        for index, _ in standalone_years
    } if (
        count > 1
        and len(standalone_years) == count
        and len(set(standalone_values)) == count
        and (
            standalone_values == sorted(standalone_values)
            or standalone_values == sorted(standalone_values, reverse=True)
        )
        and _PERIOD_HEADER_CUE.search(preceding_header_line)
    ) else set()

    def put(column: ColumnHeader, group: str, text: str) -> None:
        lowered = text.lower()
        column.words.append(text)
        if group == "kind" and column.kind is None:
            column.kind = lowered.replace("-", " ") if "half" in lowered else lowered
        elif group == "fy_year":
            year_match = re.search(r"(?:19|20)\d{2}", text)
            if year_match is None:
                return
            year = int(year_match.group(0))
            column.year = column.year if column.year is not None else year
            if column.kind is None:
                column.kind = "year"
        elif group == "month_year":
            match = re.fullmatch(
                r"([a-z]+)\.?\s+((?:19|20)\d{2})", lowered
            )
            if match:
                month = _MONTHS.get(match.group(1)[:3])
                year = int(match.group(2))
                if month is not None:
                    column.month_year = (month, year)
                    column.year = column.year if column.year is not None else year
        elif group == "date" and column.month_day is None:
            column.month_day = text
        elif group == "year" and column.year is None:
            column.year = int(text)
        elif group == "asat":
            column.as_at = True
        elif group == "audit" and column.audited is None:
            column.audited = lowered == "audited"  # 'reviewed' is a limited review
        elif group == "restated":
            column.restated = True

    attribute = {
        "kind": "kind",
        "date": "month_day",
        "fy_year": "year",
        "month_year": "month_year",
        "year": "year",
        "audit": "audited",
    }
    for line_index, (line, original_tokens) in enumerate(tokenized):
        tokens = list(original_tokens)
        if not tokens:
            continue
        years = [text for group, text in tokens if group == "year"]
        pure_year_sequence = (
            count > 1
            and len(years) == count
            and re.fullmatch(
                r"\s*(?:(?:19|20)\d{2})(?:[\s,;/|]+(?:19|20)\d{2})+[ ,;/|]*",
                line,
            )
            is not None
        )
        stacked_single_year = line_index in stacked_year_indices
        explicit_year_spans = {
            (match.end() - 4, match.end())
            for match in _EXPLICIT_PERIOD_YEAR.finditer(line)
        }
        explicit_year_indices = {
            index for index, match in enumerate(_TOKENS.finditer(line))
            if match.lastgroup == "year" and (match.start(), match.end()) in explicit_year_spans
        }
        # A bare numeric year is useful only as a complete multi-column year row or when
        # it is attached to an explicit period heading. This keeps unrelated text such as
        # "Companies Act 2013" out of the date-position fill logic.
        if pure_year_sequence or stacked_single_year:
            tokens = [("fy_year", text) if group == "year" else (group, text)
                      for group, text in tokens]
        else:
            tokens = [
                (group, text)
                for index, (group, text) in enumerate(tokens)
                if group != "year"
                or index in explicit_year_indices
            ]
        if not tokens:
            continue
        groups = [g for g, _ in tokens]
        repeated = len(set(groups)) == 1 or max(groups.count(g) for g in groups) >= 2
        if len(tokens) == count and repeated:
            # one token per column (e.g. 'ended ended ended March 31,'): by position
            for column, (group, text) in zip(columns, tokens, strict=True):
                put(column, group, text)
            continue
        for group in (
            "kind", "ended", "date", "fy_year", "month_year", "year", "audit", "asat",
            "restated",
        ):
            items = [text for g, text in tokens if g == group]
            if not items:
                continue
            if group == "ended":
                for column in columns:
                    if column.kind is not None and not any(
                        w.lower() in ("ended", "ending") for w in column.words
                    ):
                        column.words.append(items[0])
                continue
            if group in ("asat", "restated"):
                if len(items) in (1, count):  # a flag phrase applies to every column
                    for column in columns:
                        put(column, group, items[0])
                continue
            missing = [c for c in columns if getattr(c, attribute[group]) is None]
            if group in ("fy_year", "month_year", "year"):
                if len(items) == count:
                    for column, text in zip(columns, items, strict=True):
                        put(column, group, text)
                elif items and len(items) == len(missing):
                    for column, text in zip(missing, items, strict=True):
                        put(column, group, text)
                elif len(items) == 1 and missing:
                    # A single year identifies at most one value column. Replicating it
                    # across columns is how unrelated years can silently become periods.
                    put(missing[0], group, items[0])
                continue
            if len(items) == count:
                for column, text in zip(columns, items, strict=True):
                    put(column, group, text)
            elif missing and len(items) == len(missing):
                for column, text in zip(missing, items, strict=True):
                    put(column, group, text)
            elif len(items) == 1:
                for column in missing:
                    put(column, group, items[0])
    result: list[ColumnPeriod] = []
    for column in columns:
        kind = _KINDS.get(column.kind or "")
        end: date | None = None
        explicit = False
        month_day = _month_day(column.month_day) if column.month_day else None
        if column.month_year is not None:
            month, year = column.month_year
            end = date(year, month, calendar.monthrange(year, month)[1])
            if statement == StatementType.BALANCE_SHEET:
                period_type, duration = PeriodType.POINT_IN_TIME, None
            elif statement == StatementType.QUARTERLY_RESULTS:
                period_type, duration = PeriodType.QUARTER, 3
            elif fy_end_month == month:
                period_type, duration = PeriodType.FY, 12
            else:
                period_type, duration = PeriodType.UNKNOWN, None
            explicit = True
        elif column.as_at or (kind is None and statement == StatementType.BALANCE_SHEET):
            period_type, duration = PeriodType.POINT_IN_TIME, None
        elif kind is not None:
            period_type, duration = kind
        else:
            period_type, duration = PeriodType.UNKNOWN, None
        if end is None and month_day and column.year:
            try:
                end = date(column.year, *month_day)
                explicit = kind is not None or column.as_at
            except ValueError:
                end = None
        elif column.year and period_type == PeriodType.FY and fy_end_month is not None:
            end = date(column.year, fy_end_month, calendar.monthrange(column.year, fy_end_month)[1])
        if period_type == PeriodType.UNKNOWN:
            end, explicit = None, False
        if end is not None and not in_fiscal_range(end, fy_end_month):
            end, explicit = None, False  # a non-financial year header (see MIN_FISCAL_YEAR)
        ambiguous = period_type == PeriodType.UNKNOWN or end is None
        label = " ".join(column.words) or "unlabelled column"
        result.append(
            ColumnPeriod(
                period=period_from(
                    PeriodType.UNKNOWN if ambiguous else period_type,
                    None if ambiguous else duration,
                    end,
                    label,
                    fy_end_month,
                ),
                audited=column.audited,
                restated=column.restated,
                explicit=explicit,
                ambiguous=ambiguous,
            )
        )
    return result


# --- labels and aliases ------------------------------------------------------------------


def normalize_label(label: str) -> str:
    text = unicodedata.normalize("NFC", label).replace("’", "'").strip()
    # a drop-cap initial pypdf split from its word: 'C ash and Cash Equivalents'
    text = re.sub(r"(?<![A-Za-z])([A-Z]) (?=[a-z]{2,})", r"\1", text).lower()
    text = re.sub(r"^(?:\(?\s*(?:[a-z]{1,4}|\d{1,2})\s*\)|[a-z]{1,4}\.|\d{1,2}[.)]|[•\-–*])\s+",
                  "", text)
    # Schedule III line numbering and its arithmetic: 'viii profit before tax (vi-vii)',
    # 'iii total income (i +ii)', and a trailing note reference such as 'item 48c'
    text = re.sub(r"^(?:x{0,2}(?:ix|iv|v?i{1,3}|v|x))\s+(?=[a-z(])", "", text)
    text = re.sub(r"\s*\((?:\s*[ivx]+\s*[-+]\s*)+[ivx]+\s*\)\s*$", "", text)
    text = re.sub(r"\s+\d{1,3}[a-z]\s*$", "", text)
    text = re.sub(r"\bnon controlling\b", "non-controlling", text)
    text = re.sub(
        r"\s*\((?:(?:in )?(?:₹|`|rs\.?|inr)(?: per (?:equity )?share)?|refer note[^)]*)\)\s*$",
        "",
        text,
    )
    text = re.sub(r"\s*\+\s*$", "", text)
    # trailing note references pypdf left in the label: '... expenses 2.2, 2.4.2 and'
    text = re.sub(r"(?:\s+\d{1,2}(?:\.\d{1,2}){1,3}(?:,|\s+and)?)+(?:\s+and)?\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" :,;")
    return text


_PL: dict[str, CanonicalMetric] = {
    "revenue from operations": M.REVENUE_FROM_OPERATIONS,
    "revenue from operations (net)": M.REVENUE_FROM_OPERATIONS,
    "income from operations": M.REVENUE_FROM_OPERATIONS,
    "revenue": M.REVENUE,
    "revenues": M.REVENUE,
    "other operating income": M.OTHER_OPERATING_INCOME,
    "other income": M.OTHER_INCOME,
    "other income, net": M.OTHER_INCOME,
    "other income (net)": M.OTHER_INCOME,
    "total income": M.TOTAL_INCOME,
    "cost of materials consumed": M.RAW_MATERIAL_COST,
    "cost of raw materials consumed": M.RAW_MATERIAL_COST,
    "purchases of stock-in-trade": M.PURCHASES_OF_STOCK_IN_TRADE,
    "purchase of stock-in-trade": M.PURCHASES_OF_STOCK_IN_TRADE,
    "purchases of stock in trade": M.PURCHASES_OF_STOCK_IN_TRADE,
    "changes in inventories of finished goods, work-in-progress and stock-in-trade": (
        M.CHANGE_IN_INVENTORIES
    ),
    "changes in inventories of finished goods, stock-in-trade and work-in-progress": (
        M.CHANGE_IN_INVENTORIES
    ),
    "power and fuel": M.POWER_AND_FUEL,
    "employee benefit expenses": M.EMPLOYEE_COST,
    "employee benefits expense": M.EMPLOYEE_COST,
    "employee benefit expense": M.EMPLOYEE_COST,
    "employee benefits expenses": M.EMPLOYEE_COST,
    "employee cost": M.EMPLOYEE_COST,
    "employee costs": M.EMPLOYEE_COST,
    "cost of technical sub-contractors": M.SUBCONTRACTING_COST,
    "sub-contracting expenses": M.SUBCONTRACTING_COST,
    "subcontracting expenses": M.SUBCONTRACTING_COST,
    "cost of software packages and others": M.SOFTWARE_COST,
    "selling and distribution expenses": M.SELLING_EXPENSE,
    "finance costs": M.FINANCE_COST,
    "finance cost": M.FINANCE_COST,
    "interest expense": M.FINANCE_COST,
    "depreciation and amortization expenses": M.DEPRECIATION_AMORTIZATION,
    "depreciation and amortization expense": M.DEPRECIATION_AMORTIZATION,
    "depreciation and amortisation expenses": M.DEPRECIATION_AMORTIZATION,
    "depreciation and amortisation expense": M.DEPRECIATION_AMORTIZATION,
    "depreciation and amortisation": M.DEPRECIATION_AMORTIZATION,
    "depreciation and amortization": M.DEPRECIATION_AMORTIZATION,
    "other expenses": M.OTHER_EXPENSES,
    "total expenses": M.TOTAL_EXPENSES,
    "ebitda": M.EBITDA,
    "earnings before interest, tax, depreciation and amortization (ebitda)": M.EBITDA,
    "exceptional items": M.EXCEPTIONAL_ITEMS,
    "exceptional item": M.EXCEPTIONAL_ITEMS,
    "profit before exceptional items and tax": M.PROFIT_BEFORE_EXCEPTIONAL_ITEMS_AND_TAX,
    "profit before exceptional item and tax": M.PROFIT_BEFORE_EXCEPTIONAL_ITEMS_AND_TAX,
    "share of profit/(loss) of associates and joint ventures": M.SHARE_OF_ASSOCIATES_JV,
    "share in net profit / (loss) of associate": M.SHARE_OF_ASSOCIATES_JV,
    "share in net profit/(loss) of associate": M.SHARE_OF_ASSOCIATES_JV,
    "profit before tax": M.PBT,
    "current tax": M.CURRENT_TAX,
    "deferred tax": M.DEFERRED_TAX,
    "tax expense": M.TOTAL_TAX,
    "total tax expense": M.TOTAL_TAX,
    "income tax expense": M.TOTAL_TAX,
    "profit for the year": M.PAT,
    "profit for the period": M.PAT,
    "net profit": M.PAT,
    "profit after tax": M.PAT,
    "net profit after tax": M.PAT,
    "total other comprehensive income / (loss), net of tax": M.OTHER_COMPREHENSIVE_INCOME,
    "total other comprehensive income/(loss), net of tax": M.OTHER_COMPREHENSIVE_INCOME,
    "other comprehensive income": M.OTHER_COMPREHENSIVE_INCOME,
    "total comprehensive income for the year": M.TOTAL_COMPREHENSIVE_INCOME,
    "total comprehensive income for the period": M.TOTAL_COMPREHENSIVE_INCOME,
    "basic eps": M.BASIC_EPS,
    "diluted eps": M.DILUTED_EPS,
    "dividend per share": M.DPS,
}
#: Labels whose meaning depends on the caption above them.
_PL_CONTEXT: list[tuple[str, str, CanonicalMetric]] = [
    ("per", "basic", M.BASIC_EPS),
    ("per", "diluted", M.DILUTED_EPS),
    ("eps", "basic", M.BASIC_EPS),
    ("eps", "diluted", M.DILUTED_EPS),
    ("profit attributable", "owners of the company", M.PAT_ATTRIBUTABLE_TO_OWNERS),
    ("profit attributable", "equity holders of the company", M.PAT_ATTRIBUTABLE_TO_OWNERS),
    ("profit attributable", "owners of the parent", M.PAT_ATTRIBUTABLE_TO_OWNERS),
    ("profit attributable", "non-controlling interests", M.MINORITY_INTEREST),
    ("profit attributable", "non-controlling interest", M.MINORITY_INTEREST),
]
_BS: dict[str, CanonicalMetric] = {
    "property, plant and equipment": M.PROPERTY_PLANT_EQUIPMENT,
    "right-of-use assets": M.RIGHT_OF_USE_ASSETS,
    "capital work-in-progress": M.CWIP,
    "capital work in progress": M.CWIP,
    "goodwill": M.GOODWILL,
    "other intangible assets": M.INTANGIBLE_ASSETS,
    "intangible assets": M.INTANGIBLE_ASSETS,
    "investment property": M.INVESTMENT_PROPERTY,
    "inventories": M.INVENTORY,
    "cash and cash equivalents": M.CASH_AND_CASH_EQUIVALENTS,
    "bank balances other than cash and cash equivalents": M.BANK_BALANCES,
    "other bank balances": M.BANK_BALANCES,
    "total current assets": M.TOTAL_CURRENT_ASSETS,
    "total non-current assets": M.TOTAL_NON_CURRENT_ASSETS,
    "total assets": M.TOTAL_ASSETS,
    "equity share capital": M.EQUITY_SHARE_CAPITAL,
    "other equity": M.OTHER_EQUITY,
    "instruments entirely equity in nature": M.HYBRID_EQUITY_INSTRUMENTS,
    "total equity": M.TOTAL_EQUITY,
    "total current liabilities": M.TOTAL_CURRENT_LIABILITIES,
    "total non-current liabilities": M.TOTAL_NON_CURRENT_LIABILITIES,
    "total liabilities": M.TOTAL_LIABILITIES,
    "total equity and liabilities": M.TOTAL_EQUITY_AND_LIABILITIES,
    "deferred tax liabilities (net)": M.DEFERRED_TAX_LIABILITY,
}
#: (major caption, label) for lines that appear under both current and non-current.
_BS_CONTEXT: dict[tuple[str, str], CanonicalMetric] = {
    ("non-current liabilities", "borrowings"): M.LONG_TERM_BORROWINGS,
    ("current liabilities", "borrowings"): M.SHORT_TERM_BORROWINGS,
    ("non-current assets", "investments"): M.NON_CURRENT_INVESTMENTS,
    ("current assets", "investments"): M.CURRENT_INVESTMENTS,
    ("current assets", "trade receivables"): M.TRADE_RECEIVABLES,
    ("current liabilities", "trade payables"): M.TRADE_PAYABLES,
    ("current assets", "other current assets"): M.OTHER_CURRENT_ASSETS,
    ("non-current assets", "other non-current assets"): M.OTHER_NON_CURRENT_ASSETS,
    ("equity", "non-controlling interests"): M.MINORITY_INTEREST,
    ("non-current liabilities", "lease liabilities"): M.NON_CURRENT_LEASE_LIABILITIES,
    ("current liabilities", "lease liabilities"): M.CURRENT_LEASE_LIABILITIES,
}
#: (enclosing caption, line) -> metric, for a line whose meaning is its caption's
#: ('Trade receivables' > 'Billed'; the unbilled line is a contract asset, never mapped)
_BS_SUBLINE: dict[tuple[str, str], CanonicalMetric] = {
    ("trade receivables", "billed"): M.TRADE_RECEIVABLES,
}
_BS_MAJOR = (
    "non-current assets",
    "current assets",
    "non-current liabilities",
    "current liabilities",
    "equity",
)
_CF: dict[str, CanonicalMetric] = {
    "income taxes paid": M.TAX_PAID,
    "income tax paid": M.TAX_PAID,
    "interest received": M.INTEREST_RECEIVED,
    "dividends received": M.DIVIDENDS_RECEIVED,
    "dividend received": M.DIVIDENDS_RECEIVED,
    "payment of dividends": M.DIVIDENDS_PAID,
    "payment of dividends (including dividend distribution tax)": M.DIVIDENDS_PAID,
    "dividends paid": M.DIVIDENDS_PAID,
    "dividend paid": M.DIVIDENDS_PAID,
    "buyback of equity shares": M.BUYBACK,
    "buyback of shares": M.BUYBACK,
    "payment of lease liabilities": M.LEASE_PAYMENTS,
    "interest paid": M.INTEREST_PAID,
}
_CF_PATTERNS: list[tuple[str, CanonicalMetric]] = [
    (
        r"^net cash (?:generated|used|from|\(used in\)|flows?|provided)\b.*\boperating activities$",
        M.CFO,
    ),
    (
        r"^net cash (?:generated|used|from|\(used in\)|flows?|provided)\b.*\binvesting activities$",
        M.CFI,
    ),
    (
        r"^net cash (?:generated|used|from|\(used in\)|flows?|provided)\b.*\bfinancing activities$",
        M.CFF,
    ),
    (
        r"^(?:payments? (?:for|towards)|purchase of|expenditure on)"
        r" (?:property,? plant and equipment|ppe)\b.*$",
        M.PURCHASE_OF_PPE,
    ),
    (
        r"^proceeds from (?:sale|disposal) of (?:property,? plant and equipment|ppe)\b.*$",
        M.SALE_OF_PPE,
    ),
    (
        r"^net (?:increase|decrease|\(decrease\)|increase\s*/\s*\(decrease\)"
        r"|\(decrease\)\s*/\s*increase|increase/\(decrease\)|\(decrease\)/increase)\b"
        r".*\bcash and cash equivalents$",
        M.NET_CHANGE_IN_CASH,
    ),
    (
        r"^effect of exchange (?:rate )?(?:changes|differences)\b.*\bcash and cash equivalents.*$",
        M.FX_EFFECT_ON_CASH,
    ),
    (r"^cash and cash equivalents at the beginning of the (?:year|period)$", M.OPENING_CASH),
    (r"^cash and cash equivalents at the end of the (?:year|period)$", M.CLOSING_CASH),
]
_BANK: dict[str, CanonicalMetric] = {
    "interest earned": M.INTEREST_EARNED,
    "interest expended": M.INTEREST_EXPENDED,
    "net interest income": M.NET_INTEREST_INCOME,
    "operating expenses": M.BANK_OPERATING_EXPENSES,
    "provisions (other than tax) and contingencies": M.PROVISIONS_AND_CONTINGENCIES,
    "deposits": M.DEPOSITS,
    "advances": M.ADVANCES,
}


#: Schedule III (Division III) top-line rows keep their section / composition markers in
#: the statement ('B Other Income', 'Total Revenue from Operations (A)', 'Total Income
#: I (A+B)'). Only these exact top-line concepts are recognised through the markers; a
#: component such as '(a) Interest Income' is NOT revenue and stays unmapped.
_SCHEDULE_TOPLINE: dict[str, CanonicalMetric] = {
    "revenue from operations": M.REVENUE_FROM_OPERATIONS,
    "total revenue from operations": M.REVENUE_FROM_OPERATIONS,
    "other income": M.OTHER_INCOME,
    "total income": M.TOTAL_INCOME,
}
_SECTION_MARKER = re.compile(r"^(?:[A-H]|[IVX]{1,4})\s+(?=[A-Z])")
_COMPOSITION_MARKER = re.compile(r"\s+(?:[IVX]{1,4}\s*)?\(\s*[A-H](?:\s*\+\s*[A-H])*\s*\)\s*$")


def _schedule_topline(label: str) -> CanonicalMetric | None:
    text = unicodedata.normalize("NFC", label).strip()
    stripped = _COMPOSITION_MARKER.sub("", _SECTION_MARKER.sub("", text))
    if stripped == text:
        return None  # no Schedule III marker: the explicit aliases above already decided
    return _SCHEDULE_TOPLINE.get(normalize_label(stripped))


def _context_match(needle: str, joined: str) -> bool:
    """'profit attributable' also matches the Schedule III caption 'profit/ (loss) for the
    year attributable to'; every other needle is a plain substring."""
    if needle == "profit attributable":
        return re.search(r"\bprofit\b[^|]*\battributable\b", joined) is not None
    return needle in joined


def map_filing_label(
    statement: StatementType, label: str, context: list[str], bank: bool
) -> CanonicalMetric | None:
    """Explicit alias lookup. `context` holds the enclosing captions, outermost first."""
    norm = normalize_label(label)
    joined = " | ".join(context)
    if bank and norm in _BANK:
        return _BANK[norm]
    if statement in (StatementType.PROFIT_AND_LOSS, StatementType.QUARTERLY_RESULTS):
        for needle, line, metric in _PL_CONTEXT:
            if norm == line and _context_match(needle, joined) and "comprehensive" not in joined:
                return metric
        if bank and norm in ("finance costs", "finance cost", "interest expense"):
            return None  # a bank's interest expense is not an industrial finance cost
        return _PL.get(norm) or _schedule_topline(label)
    if statement == StatementType.BALANCE_SHEET:
        if bank and norm == "borrowings":
            return M.TOTAL_BORROWINGS
        major = next((c for c in reversed(context) if c in _BS_MAJOR), None)
        if context and (context[-1], norm) in _BS_SUBLINE and major in (
                "current assets", "non-current assets"):
            return (_BS_SUBLINE[(context[-1], norm)] if major == "current assets"
                    else None)  # a non-current receivable is not working capital
        if major is not None and (major, norm) in _BS_CONTEXT:
            return _BS_CONTEXT[(major, norm)]
        return _BS.get(norm)
    if statement == StatementType.CASH_FLOW:
        if norm in _CF:
            return _CF[norm]
        return next((m for pattern, m in _CF_PATTERNS if re.match(pattern, norm)), None)
    return None


#: Box 1B Screener row labels (as cleaned by Box 1B) -> metric. Screener's own aggregates
#: keep their own metrics (e.g. 'Operating Profit' is not EBITDA, 'Total Liabilities' is
#: equity + liabilities).
SCREENER_GENERAL: dict[str, CanonicalMetric] = {
    "Sales": M.REVENUE_FROM_OPERATIONS,
    "Revenue": M.REVENUE_FROM_OPERATIONS,
    "Expenses": M.TOTAL_OPERATING_EXPENSE,
    "Operating Profit": M.OPERATING_PROFIT,
    "Other Income": M.OTHER_INCOME,
    "Interest": M.FINANCE_COST,
    "Depreciation": M.DEPRECIATION_AMORTIZATION,
    "Profit before tax": M.PBT,
    "Net Profit": M.PAT,
    "EPS in Rs": M.EPS,
    "Equity Capital": M.EQUITY_SHARE_CAPITAL,
    "Reserves": M.RESERVES,
    "Borrowings": M.TOTAL_BORROWINGS,
    "Borrowing": M.TOTAL_BORROWINGS,
    "Other Liabilities": M.OTHER_LIABILITIES,
    "Total Liabilities": M.TOTAL_EQUITY_AND_LIABILITIES,
    "Fixed Assets": M.FIXED_ASSETS,
    "CWIP": M.CWIP,
    "Investments": M.TOTAL_INVESTMENTS,
    "Other Assets": M.OTHER_ASSETS,
    "Total Assets": M.TOTAL_ASSETS,
    "Cash from Operating Activity": M.CFO,
    "Cash from Investing Activity": M.CFI,
    "Cash from Financing Activity": M.CFF,
    "Net Cash Flow": M.NET_CHANGE_IN_CASH,
}
SCREENER_BANK: dict[str, CanonicalMetric | None] = {
    **SCREENER_GENERAL,
    "Revenue": M.REVENUE,  # a bank's 'Revenue' is Screener's own aggregate
    "Interest": M.INTEREST_EXPENDED,
    "Financing Profit": M.BANK_FINANCING_PROFIT,
    "Deposits": M.DEPOSITS,
    "Operating Profit": None,
}
