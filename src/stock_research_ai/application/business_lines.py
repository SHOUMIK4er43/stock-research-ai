"""DETAILED_RESEARCH_V1 Box 7: deterministic reading of Box 3 text lines into table frames.

Box 3 keeps every page as ordered text blocks with exact character offsets. pypdf loses
column positions, so a table arrives as lines: '<label> <value> <value> ...'. A frame is
a dense run of such value lines plus the header / unit-caption / footnote lines around
it. Two layouts are interpreted:

  * ROW layout - rows are entities or KPIs, columns are periods from the header;
  * MATRIX layout - columns are entities (segments) named in a wrapped header, rows are
    metrics, each metric printed once per period (the label on the first line only).

Nothing is guessed: numbers broken by pypdf ('57, 45 4') are re-read only when exactly
one reading gives the expected column count of validly grouped numbers; a matrix
header is split only when exactly one split into the expected number of names exists
using names the company itself prints elsewhere; anything else stays unresolved.
"""

import calendar
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID, uuid5

from stock_research_ai.application.business_registry import (
    UnitReading,
    clean_name,
    name_key,
    read_unit,
)
from stock_research_ai.application.financial_parsing import ColumnPeriod, interpret_columns
from stock_research_ai.domain.document_intelligence import (
    BlockType,
    DocumentExtraction,
    SectionKind,
)
from stock_research_ai.domain.financial_canonical import ReportingBasis, StatementType

NS = UUID("6a1f1f0e-7b07-5c3e-9d61-2f0a4c8b7e07")

# --- lines -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    text: str
    page: int
    block_id: UUID
    block_type: BlockType
    line_index: int  # line inside the block
    position: int  # position in the page stream
    table_id: UUID | None  # the Box 3 table holding the block, if any
    block_order: int


_EDGE = 2  # lines at the top / bottom of a page that may be a running header / footer
_RUNNING_MIN_CHARS = 15  # short labels ('Finance cost') are never treated as running text


def running_lines(extraction: DocumentExtraction) -> set[str]:
    """Texts printed at a page edge on three or more pages ('Consolidated Financial
    Statements' / 'for the year ended March 31, 2026'): running headers Box 3 did not flag."""
    seen: dict[str, set[int]] = {}
    by_page: dict[int, list[str]] = {}
    for block in sorted(extraction.blocks, key=lambda b: b.order):
        for text in block.normalized_text.split("\n"):
            stripped = " ".join(text.split())
            if stripped:
                by_page.setdefault(block.page_number, []).append(stripped)
    for page, texts in by_page.items():
        for text in {*texts[:_EDGE], *texts[-_EDGE:]}:
            textual = not trailing_values(text)[1] or is_period_phrase(text)
            if len(text) >= _RUNNING_MIN_CHARS and textual:
                seen.setdefault(text, set()).add(page)
    return {text for text, pages in seen.items() if len(pages) >= 3}


def page_lines(extraction: DocumentExtraction) -> dict[int, list[Line]]:
    """Every block line that is not a running header/footer, per page, in reading order."""
    in_table = {b: t.table_id for t in extraction.tables for b in t.block_ids}
    running = running_lines(extraction)
    pages: dict[int, list[Line]] = {}
    for block in sorted(extraction.blocks, key=lambda b: b.order):
        repeated = block.is_repeated_header or block.is_repeated_footer
        if repeated and caption_unit(block.normalized_text) is None:
            continue  # running headers go; a repeated unit caption ('(In ₹ crore)') stays
        lines = pages.setdefault(block.page_number, [])
        for index, text in enumerate(block.normalized_text.split("\n")):
            stripped = " ".join(text.split())
            if not stripped or (stripped in running and caption_unit(stripped) is None):
                continue
            lines.append(
                Line(
                    stripped,
                    block.page_number,
                    block.block_id,
                    block.block_type,
                    index,
                    len(lines),
                    in_table.get(block.block_id),
                    block.order,
                )
            )
    return pages


# --- tokens ----------------------------------------------------------------------------------

_VALUE_TOKEN = re.compile(
    r"^(?:\(?[-–−]?\d[\d,.]*%?\)?[*#^]?|[\d]{1,3},|,\d+|[-–—−]|nil|n\.?a\.?)$", re.I
)
_MARKER_TOKEN = re.compile(r"^(?:\(\d{1,2}\)|<\d{1,2}[>J)]|\(\d{1,2}[>J])$")
_GROUPED = re.compile(
    r"^\(?[-–−]?(?:\d+|\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?%?\)?[*#^]?$"
)
_SPECIAL = re.compile(r"^(?:[-–—−]|nil|n\.?a\.?)$", re.I)
_SPLIT_LIMIT = 8  # extra fragments beyond the expected column count


def valid_number(token: str) -> bool:
    return bool(_GROUPED.match(token) or _SPECIAL.match(token))


def trailing_values(text: str) -> tuple[str, list[str]]:
    """(label, value tokens) - value tokens are the maximal numeric tail of the line."""
    tokens = text.split()
    cut = len(tokens)
    while cut > 0 and _VALUE_TOKEN.match(tokens[cut - 1]):
        cut -= 1
    values = tokens[cut:]
    label_tokens = tokens[:cut]
    # a footnote marker right after the label ('Services (1) 13,463') belongs to the label
    # ... but '(40)' alone at the end of a line is a negative value, not a marker
    while (
        values
        and label_tokens
        and _MARKER_TOKEN.match(values[0])
        and (len(values) > 1 or re.fullmatch(r"\(\d\)|<\d[>J)]|\(\d[>J]", values[0]))
    ):
        label_tokens.append(values.pop(0))
    return " ".join(label_tokens), values


def rejoin_groups(tokens: list[str], expected: int) -> list[list[str]] | None:
    """The unique way to group the tokens into `expected` validly grouped numbers (each
    group is the printed fragments of one number)."""
    if len(tokens) == expected and all(valid_number(t) for t in tokens):
        return [[t] for t in tokens]
    if len(tokens) < expected or len(tokens) - expected > _SPLIT_LIMIT:
        return None
    found: list[list[list[str]]] = []

    def walk(start: int, parts: list[list[str]]) -> None:
        if len(found) > 1:
            return
        if start == len(tokens):
            if len(parts) == expected:
                found.append(list(parts))
            return
        if len(parts) == expected:
            return
        for end in range(start + 1, min(len(tokens), start + 1 + _SPLIT_LIMIT) + 1):
            if valid_number("".join(tokens[start:end])):
                walk(end, [*parts, tokens[start:end]])

    walk(0, [])
    return found[0] if len(found) == 1 else None


def rejoin(tokens: list[str], expected: int) -> list[str] | None:
    """The unique way to read the tokens as `expected` validly grouped numbers."""
    groups = rejoin_groups(tokens, expected)
    return None if groups is None else ["".join(g) for g in groups]


# --- header / caption lines -------------------------------------------------------------------

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_HEADER_WORDS = {
    "particulars",
    "quarter",
    "quarters",
    "year",
    "years",
    "ended",
    "ending",
    "months",
    "month",
    "three",
    "six",
    "nine",
    "twelve",
    "as",
    "at",
    "on",
    "audited",
    "unaudited",
    "reviewed",
    "restated",
    "reported",
    "cc",
    "growth",
    "yoy",
    "qoq",
    "y",
    "o",
    "q",
    "change",
    "and",
    "the",
    "for",
    "period",
    "half",
    "fy",
    "h",
    "current",
    "previous",
    "corresponding",
    "preceding",
    "of",
    "figures",
    "refer",
    "note",
    "notes",
    "in",
    "constant",
    "currency",
    "vs",
    "versus",
    "ytd",
    "full",
    "financial",
    "end",
    "till",
    "to",
    "date",
    "upto",
}
_PERIOD_HINT = re.compile(
    rf"\b(?:19|20)\d{{2}}\b|\bfy\s?'?\d{{2,4}}\b|\bq[1-4]\b|{_MONTH}\s+\d{{1,2}}\b"
    r"|\b(?:quarter|year|months)\b|\bended\b|\bas at\b|\breported\b|\bgrowth\b|\bparticulars\b",
    re.I,
)


def is_header_line(text: str) -> bool:
    lowered = text.lower()
    if not _PERIOD_HINT.search(lowered):
        return False
    words = re.findall(r"[a-z]+", lowered)
    months = re.compile(rf"^{_MONTH}$")
    return all(
        w in _HEADER_WORDS or months.match(w) or w in ("st", "nd", "rd", "th") for w in words
    )


_PERIOD_PHRASE = re.compile(
    rf"(?:for the |during the )?(?:quarter|three months|six months|nine months|year|twelve months|"
    rf"half[- ]year)s? ended {_MONTH}\s+\d{{1,2}},?\s+(?:19|20)\d{{2}}"
    rf"(?:\s*(?:and|,)\s*{_MONTH}\s+\d{{1,2}},?\s+(?:19|20)\d{{2}})*",
    re.I,
)


def is_period_phrase(text: str) -> bool:
    return bool(_PERIOD_PHRASE.search(text)) and len(text.split()) <= 24


def caption_unit(text: str) -> UnitReading | None:
    stripped = text.strip().rstrip(":")
    lowered = stripped.lower()
    if len(stripped) > 48:
        return None
    wrapped = stripped.startswith("(") and stripped.endswith(")")
    lead = re.match(
        r"^\(?\s*(?:in|all figures in|figures in|amounts? in|₹|rs\.?|`|us\$|inr)\b", lowered
    ) or lowered.startswith(("₹", "`"))
    if not (wrapped or lead):
        return None
    return read_unit(stripped)


def expand_shorthand(text: str, fy_end_month: int | None) -> str:
    """'FY2026' / 'FY26' -> 'year ended March 31, 2026'; 'Q1 FY27' -> 'quarter ended June
    30, 2026' (needs the fiscal year-end month; untouched otherwise)."""
    if fy_end_month is None:
        return text

    def year(token: str) -> int:
        value = int(token)
        return value + 2000 if value < 100 else value

    def end(fy: int, months_back: int = 0) -> str:
        month_index = fy_end_month - months_back
        y = fy
        while month_index <= 0:
            month_index += 12
            y -= 1
        day = calendar.monthrange(y, month_index)[1]
        return f"{calendar.month_name[month_index]} {day}, {y}"

    def quarter(match: re.Match[str]) -> str:
        q, fy = int(match.group(1)), year(match.group(2))
        return f"quarter ended {end(fy, 3 * (4 - q))}"

    text = re.sub(r"\bQ([1-4])\s*(?:FY|F)\s?'?(\d{2}|\d{4})\b", quarter, text, flags=re.I)
    return re.sub(
        r"\bFY\s?'?(\d{4}|\d{2})\b(?:\s*[-–/]\s*\d{2})?",
        lambda m: f"year ended {end(year(m.group(1)))}",
        text,
        flags=re.I,
    )


# --- frames ----------------------------------------------------------------------------------


@dataclass
class Row:
    label: str
    tokens: list[str]
    line: Line
    label_lines: list[Line]
    group: str | None
    group_line: Line | None
    parents: tuple[str, ...]
    continuation: int = 0  # matrix: k-th unlabelled line after the labelled one
    values: list[str] | None = None  # per value column, after re-reading
    rejoined: bool = False
    printed: list[str] | None = None  # per value column, as printed ('11, 831')


@dataclass
class Frame:
    frame_id: UUID
    page: int
    context: list[Line]  # lines above (titles, captions, period phrases)
    lines: list[Line]  # frame body
    trailing: list[Line]  # footnotes / trailing header lines below
    rows: list[Row] = field(default_factory=list)
    texts: list[Line] = field(default_factory=list)  # label-only lines in the body
    columns: int = 0
    matrix: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def all_lines(self) -> list[Line]:
        return [*self.context, *self.lines, *self.trailing]


_YEAR_TOKEN = re.compile(r"^(?:FY\s?)?(?:19|20)\d{2}$")


def _kind(line: Line) -> str:
    if caption_unit(line.text) is not None:
        return "CAPTION"
    if is_header_line(line.text):
        return "HEADER"
    label, values = trailing_values(line.text)
    if len(values) >= 2 and len(label.split()) <= 3 and all(_YEAR_TOKEN.match(v) for v in values):
        return "HEADER"  # 'Clients 2026 2025': a column header, not a row of numbers
    if values:
        return "VALUE"
    return "TEXT"


def _long_text(text: str) -> bool:
    words = len(text.split())
    return words > 22 or (words > 10 and text.rstrip().endswith("."))


def find_frames(extraction: DocumentExtraction, page: int, lines: list[Line]) -> list[Frame]:
    kinds = [_kind(line) for line in lines]
    frames: list[Frame] = []
    i = 0
    consumed_until = 0
    while i < len(lines):
        if kinds[i] != "VALUE":
            i += 1
            continue
        start = i
        end = i
        gap = 0
        j = i + 1
        while j < len(lines):
            kind = kinds[j]
            if kind == "VALUE":
                end, gap = j, 0
            elif kind == "CAPTION" or _long_text(lines[j].text):
                break
            elif kind == "HEADER" and re.search(r"\b(?:19|20)\d{2}\b", lines[j].text):
                # a new dated header followed by values starts another table
                if any(k == "VALUE" for k in kinds[j + 1 : j + 4]):
                    break
                gap += 1
            else:
                gap += 1
            if gap > 4:
                break
            j += 1
        context: list[Line] = []
        k = start - 1
        while k >= consumed_until and len(context) < 24:
            if _long_text(lines[k].text) and not is_period_phrase(lines[k].text):
                break
            context.insert(0, lines[k])
            k -= 1
        trailing: list[Line] = []
        k = end + 1
        while k < len(lines) and len(trailing) < 6 and kinds[k] != "VALUE":
            text = lines[k].text
            if kinds[k] == "HEADER" or re.match(
                r"^(?:[*#^†]|\(\d{1,2}\)|<\d|note\b|\d\))", text, re.I
            ):
                trailing.append(lines[k])
                k += 1
                continue
            break
        body = lines[start : end + 1]
        frame_id = uuid5(
            NS, f"{extraction.extraction_id}|{page}|{body[0].block_id}|{body[0].line_index}"
        )
        frames.append(Frame(frame_id, page, context, body, trailing))
        # trailing lines (e.g. a period phrase) may also head the next table
        consumed_until = end + 1
        i = end + 1 + len(trailing)
    return frames


def build_rows(frame: Frame) -> None:
    """Rows, sub-headers and the expected value-column count of a frame."""
    # label-only lines between the last header/caption line and the first row are the
    # first rows' group heading(s) ('Segment revenue', a parent segment)
    pending: list[Line] = []
    for line in frame.context:
        kind = _kind(line)
        if kind in ("HEADER", "CAPTION"):
            pending = []
        elif kind == "TEXT":
            pending.append(line)
    if not any(_kind(line) in ("HEADER", "CAPTION") for line in frame.context):
        pending = []
    group: str | None = None
    group_line: Line | None = None
    stack: list[str] = []
    rows: list[Row] = []
    previous: Row | None = None
    kinds = [_kind(line) for line in frame.lines]
    for position, line in enumerate(frame.lines):
        kind = kinds[position]
        if kind in ("HEADER", "CAPTION"):
            continue
        if kind == "TEXT":
            pending.append(line)
            continue
        label, tokens = trailing_values(line.text)
        upcoming = next((k for k in kinds[position + 1 :] if k != "HEADER"), "END")
        if not label and pending and upcoming == "TEXT":
            # value-only lines alternating with label-only lines (an infographic): the
            # label could belong to the value above or below it - never guessed
            rows.append(Row("", tokens, line, [], group, group_line, tuple(stack)))
            previous = rows[-1]
            frame.texts.extend(pending)
            pending = []
            continue
        if not label and not pending and previous is not None:
            rows.append(
                Row(
                    "",
                    tokens,
                    line,
                    [],
                    group,
                    group_line,
                    tuple(stack),
                    continuation=previous.continuation + 1,
                )
            )
            previous = rows[-1]
            continue
        label_lines: list[Line] = []
        if pending and not label:
            # the whole label sits on the lines above its values
            label_lines, pending = pending, []
            label = " ".join(p.text for p in label_lines).strip()
        elif pending and label[:1].islower():
            # a wrapped label ('Revenue from' / 'operations 49,908')
            label_lines = pending[-1:]
            pending = pending[:-1]
            label = " ".join([*(p.text for p in label_lines), label]).strip()
        for text_line in pending:
            frame.texts.append(text_line)
            group, group_line = text_line.text, text_line
            stack.append(text_line.text)
        pending = []
        row = Row(label, tokens, line, label_lines, group, group_line, tuple(stack))
        rows.append(row)
        previous = row
        # a 'Total <parent>' row closes that parent
        key = name_key(label)
        if stack and key in (f"total {name_key(stack[-1])}", f"{name_key(stack[-1])} total"):
            stack.pop()
    for text_line in pending:
        frame.texts.append(text_line)
    frame.rows = rows
    counts = Counter(
        len(r.tokens) for r in rows if len(r.tokens) >= 1 and all(valid_number(t) for t in r.tokens)
    )
    # the widest value count printed cleanly on at least two rows (single-value bridge
    # lines in a matrix must not decide the width); else the most common count
    wide = [c for c, n in counts.items() if c >= 2 and n >= 2]
    if wide:
        frame.columns = max(wide)
    else:
        frame.columns = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else 0
    frame.matrix = (
        frame.columns >= 2 and sum(1 for r in rows if r.continuation >= 1 and r.label == "") >= 2
    )


def matrix_rows(frame: Frame) -> None:
    """In a matrix every label wraps: all text lines since the previous value group are the
    label of the next labelled line."""
    pending: list[Line] = []
    rows: list[Row] = []
    previous: Row | None = None
    for line in frame.lines:
        kind = _kind(line)
        if kind in ("HEADER", "CAPTION"):
            continue
        if kind == "TEXT":
            pending.append(line)
            continue
        label, tokens = trailing_values(line.text)
        if not label and not pending and previous is not None:
            rows.append(Row("", tokens, line, [], None, None, (), previous.continuation + 1))
            previous = rows[-1]
            continue
        full = " ".join([*(p.text for p in pending), label]).strip()
        rows.append(Row(full, tokens, line, list(pending), None, None, ()))
        previous = rows[-1]
        pending = []
    frame.rows = rows
    frame.texts = pending


def read_values(frame: Frame, row: Row, expected: int) -> None:
    groups = rejoin_groups(row.tokens, expected)
    if groups is not None:
        row.values = ["".join(g) for g in groups]
        row.printed = [" ".join(g) for g in groups]
        row.rejoined = any(len(g) > 1 for g in groups)


# --- periods ---------------------------------------------------------------------------------


_TRAILING_KIND = re.compile(
    r"(?:for the )?(?:year|quarter|half[- ]year|(?:three|six|nine|twelve) months) ended$|as at$",
    re.I,
)


def frame_periods(
    frame: Frame,
    count: int,
    fy_end_month: int | None,
    statement: StatementType,
    annual_years: bool = False,
) -> tuple[list[ColumnPeriod | None], list[str]]:
    """Period per value column from the header lines around the frame. When the header
    names fewer dated columns than there are value columns, the dated ones come first
    (e.g. three quarter-ends, then 'Reported / CC' growth columns) and the rest are
    returned as None (preserved as extra columns, never given a period)."""

    def wanted(line: Line, where: str) -> bool:
        if _kind(line) == "HEADER":
            return True
        if where == "body":
            return False  # prose inside the table run never names its columns
        shorthand = re.search(r"\bFY\s?'?\d{2,4}\b|\bQ[1-4]\s*FY", line.text, re.I)
        phrase = is_period_phrase(line.text) and (
            where == "context" or len(line.text.split()) <= 14
        )
        return bool(phrase or (shorthand and where == "context"))

    headers: list[str] = []
    for where, lines in (("context", frame.context), ("body", frame.lines),
                         ("trailing", frame.trailing)):  # fmt: skip
        for line in lines:
            if wanted(line, where):
                headers.append(expand_shorthand(line.text, fy_end_month))
            elif where == "context" and (tail := _TRAILING_KIND.search(line.text)):
                headers.append(tail.group(0))  # 'Digital Banking (...) For the year ended'
    if not headers or count == 0:
        return [None] * count, []
    periods = interpret_columns(headers, count, statement, fy_end_month)
    if not all(p.ambiguous for p in periods) and _distinct(periods):
        return [None if p.ambiguous else p for p in periods], headers
    # a header stacked one fragment per line ('As at' / 'March 31,' / '2026' / ...) is
    # read as one line, with 'April 01, 2025 to March 31, 2026' ranges named by length
    headers = [_name_ranges(" ".join(headers))]
    periods = interpret_columns(headers, count, statement, fy_end_month)
    if not all(p.ambiguous for p in periods) and _distinct(periods):
        return [None if p.ambiguous else p for p in periods], headers
    dated = max(
        len(re.findall(rf"{_MONTH}\s+\d{{1,2}},?\s+(?:19|20)\d{{2}}", h, re.I)) for h in headers
    )
    years_only = max(len(re.findall(r"\b(?:19|20)\d{2}\b", h)) for h in headers)
    for k in (dated, years_only):
        if 0 < k < count:
            partial = interpret_columns(headers, k, statement, fy_end_month)
            if not any(p.ambiguous for p in partial):
                return [*partial, *([None] * (count - k))], headers
    if annual_years and fy_end_month is not None and years_only == count:
        # an annual report's bare year columns are its fiscal years
        fiscal = [
            f"year ended {h}" for h in headers if len(re.findall(r"\b(?:19|20)\d{2}\b", h)) == count
        ]
        if fiscal:
            periods = interpret_columns(fiscal[:1], count, statement, fy_end_month)
            if not any(p.ambiguous for p in periods):
                return list(periods), headers
    return [None] * count, headers


def _distinct(periods: list[ColumnPeriod]) -> bool:
    """No two dated columns resolve to the same period (a symptom of a mis-read header)."""
    keys = [p.period.key() for p in periods if not p.ambiguous]
    return len(keys) == len(set(keys))


_DATE = rf"{_MONTH}\s+\d{{1,2}},?\s+(?:19|20)\d{{2}}"
_RANGE = re.compile(rf"(?P<a>{_DATE})\s+to\s+(?P<b>{_DATE})", re.I)
_RANGE_KIND = ((85, 95, "quarter"), (175, 190, "six months"), (265, 280, "nine months"),
               (360, 370, "year"))  # fmt: skip


def _parse_date(text: str) -> date | None:
    m = re.match(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text.strip(), re.I)
    if not m or m.group(1)[:3].lower() not in _MONTH_NUMBER:
        return None
    try:
        return date(int(m.group(3)), _MONTH_NUMBER[m.group(1)[:3].lower()], int(m.group(2)))
    except ValueError:
        return None


_MONTH_NUMBER = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def _name_ranges(text: str) -> str:
    def name(m: re.Match[str]) -> str:
        start, end = _parse_date(m.group("a")), _parse_date(m.group("b"))
        if start is None or end is None:
            return m.group(0)
        days = (end - start).days
        kind = next((k for lo, hi, k in _RANGE_KIND if lo <= days <= hi), None)
        return f"{kind} ended {m.group('b')}" if kind else m.group(0)

    return _RANGE.sub(name, text)


def page_period_phrases(lines: list[Line]) -> list[str]:
    """Distinct period phrases on a page ('for the' / 'during the' prefixes ignored)."""
    return sorted(
        {
            re.sub(r"^(?:for|during) the ", "", " ".join(m.group(0).lower().split()))
            for line in lines
            for m in _PERIOD_PHRASE.finditer(line.text)
        }
    )


# --- matrix header ---------------------------------------------------------------------------


_HEADER_WORD_LIMIT = 80


def split_matrix_header(
    header_tokens: list[str], expected: int, vocabulary: set[str]
) -> list[str] | None:
    """The unique split of the header words into `expected` names, each of which the
    company prints elsewhere (vocabulary of name_key values; 'total' always included).
    Memoised: at most two solutions are ever tracked per position."""
    words = [t for t in header_tokens if t]
    if not words or len(words) > _HEADER_WORD_LIMIT:
        return None
    keys = vocabulary | {"total"}
    memo: dict[tuple[int, int], list[list[str]]] = {}

    def solve(start: int, remaining: int) -> list[list[str]]:
        """Up to two ways to split words[start:] into `remaining` names."""
        if start == len(words):
            return [[]] if remaining == 0 else []
        if remaining == 0 or _MARKER_TOKEN.match(words[start]):
            return []  # a footnote marker closes the name before it, never opens one
        cached = memo.get((start, remaining))
        if cached is not None:
            return cached
        found: list[list[str]] = []
        for end in range(start + 1, min(len(words), start + 12) + 1):
            candidate = " ".join(words[start:end])
            if name_key(candidate) not in keys:
                continue
            for rest in solve(end, remaining - 1):
                found.append([clean_name(candidate), *rest])
                if len(found) > 1:
                    break
            if len(found) > 1:
                break
        memo[(start, remaining)] = found
        return found

    solutions = solve(0, expected)
    return solutions[0] if len(solutions) == 1 else None


def matrix_header(frame: Frame) -> tuple[list[str], list[Line]]:
    """Header words between 'Particulars' (or the unit caption) and the first row, with
    the lines they came from."""
    lines: list[Line] = []
    started = False
    for line in frame.context:
        text = line.text
        if re.match(r"^particulars\b", text, re.I):
            started = True
            rest = text[len("particulars") :].strip()
            if rest:
                lines.append(
                    Line(
                        rest,
                        line.page,
                        line.block_id,
                        line.block_type,
                        line.line_index,
                        line.position,
                        line.table_id,
                        line.block_order,
                    )
                )
            continue
        if not started:
            if caption_unit(text) is not None or is_period_phrase(text):
                started = True
            continue
        if caption_unit(text) is not None or is_period_phrase(text) or is_header_line(text):
            continue
        lines.append(line)
    if lines:  # 'Business Segments Treasury Corporate /': the stub header names no column
        first = lines[0]
        stub = re.match(r"^(?:business\s+segments?|segments?|particulars)\b\s*", first.text, re.I)
        if stub:
            rest = first.text[stub.end() :]
            lines[0] = Line(rest, first.page, first.block_id, first.block_type, first.line_index,
                            first.position, first.table_id, first.block_order)  # fmt: skip
            if not rest:
                lines = lines[1:]
    words = [w for line in lines for w in line.text.split()]
    return words, lines


# --- basis / sections ------------------------------------------------------------------------

_BASIS_WORD = re.compile(r"\b(consolidated|standalone|stand-alone)\b", re.I)


def explicit_basis(texts: list[str]) -> ReportingBasis | None:
    found = {
        ReportingBasis.CONSOLIDATED
        if m.group(1).lower() == "consolidated"
        else ReportingBasis.STANDALONE
        for text in texts
        for m in _BASIS_WORD.finditer(text)
    }
    return found.pop() if len(found) == 1 else None


def section_basis(extraction: DocumentExtraction, page: int) -> ReportingBasis | None:
    """The latest financial-statement section (by its heading position) that started on
    or before this page and names its basis."""
    order = {b.block_id: b.order for b in extraction.blocks}
    best: tuple[int, int, ReportingBasis] | None = None
    for section in extraction.sections:
        if section.page_start > page:
            continue
        title = section.title.lower()
        if section.kind == SectionKind.CONSOLIDATED_FINANCIAL_STATEMENTS or (
            "consolidated" in title and section.kind == SectionKind.NOTES_TO_ACCOUNTS
        ):
            basis = ReportingBasis.CONSOLIDATED
        elif section.kind == SectionKind.STANDALONE_FINANCIAL_STATEMENTS or (
            "standalone" in title and section.kind == SectionKind.NOTES_TO_ACCOUNTS
        ):
            basis = ReportingBasis.STANDALONE
        else:
            continue
        if section.kind in (
            SectionKind.CONSOLIDATED_FINANCIAL_STATEMENTS,
            SectionKind.STANDALONE_FINANCIAL_STATEMENTS,
        ) and ("consolidated" not in title and "standalone" not in title):
            continue  # a heading that does not itself name the basis
        rank = (
            section.page_start,
            order.get(section.heading_block_id, -1) if section.heading_block_id else -1,
        )
        if best is None or rank > best[:2]:
            best = (rank[0], rank[1], basis)
    return best[2] if best else None


def section_kind_at(extraction: DocumentExtraction, page: int) -> SectionKind | None:
    covering = [s for s in extraction.sections if s.page_start <= page <= s.page_end]
    if not covering:
        return None
    narrow = min(covering, key=lambda s: (s.page_end - s.page_start, -s.page_start, s.kind.value))
    return narrow.kind


def fiscal_end(month: int, year: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])
