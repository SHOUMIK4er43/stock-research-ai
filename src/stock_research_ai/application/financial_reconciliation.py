"""DETAILED_RESEARCH_V1 Box 4: observations -> reconciliation -> canonical financial dataset.

Deterministic throughout (no AI). Rules, in order:

Comparability key: metric, statement family (income / balance / cash), basis, currency,
value kind and exact period (type, end date, duration). Standalone never meets
consolidated; a quarter never meets a YTD/nine-month/FY column; FY never meets TTM.

Eligibility: only numeric, fully interpreted observations (known period, basis, unit).
LOW-confidence observations are kept but only decide a value when nothing better exists
- and even then the group is NOT_COMPARABLE rather than canonical.

Outcome (candidates ordered by authority, then confidence, then filing over aggregator):
  1 candidate                                  -> SINGLE_SOURCE
  all equal                                    -> MATCH
  all within rounding tolerance of the best    -> MATCH_WITHIN_TOLERANCE
  disagreeing values all from LOWER authority  -> SOURCE_PRECEDENCE (they are listed)
  a disagreeing same-authority filing value
    explicitly marked 'restated'               -> RESTATEMENT (restated value selected)
  disagreement inside one document             -> AMBIGUOUS   (no canonical value)
  otherwise                                    -> CONFLICT    (no canonical value)
Nothing is averaged, adjusted or derived.
"""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.financial_parsing import (
    SCREENER_BANK,
    SCREENER_GENERAL,
    ColumnPeriod,
    UnitCaption,
    interpret_columns,
    map_filing_label,
    normalize_label,
    parse_financial_number,
    parse_unit_caption,
    period_from,
)
from stock_research_ai.domain.document_intelligence import (
    BlockType,
    DocumentExtraction,
    EvidenceBundle,
    ExtractedTable,
    ExtractionStatus,
    FinancialStatementKind,
    IssuerBindingStatus,
)
from stock_research_ai.domain.evidence import AUTHORITY_RANK, EvidenceCatalogue, SourceAuthority
from stock_research_ai.domain.financial import ReportingScope, UnitKind
from stock_research_ai.domain.financial_business import CLASSIFICATION_VERSION, classify_business
from stock_research_ai.domain.financial_canonical import (
    CANONICAL_SCALE,
    SCALE_FACTOR,
    TAXONOMY_VERSION,
    CanonicalFinancialDataset,
    CanonicalFinancialValue,
    CanonicalLine,
    CanonicalMetric,
    CanonicalStatement,
    Confidence,
    Currency,
    FinancialLimits,
    FinancialPeriod,
    FinancialStatistics,
    FinancialWarning,
    Industry,
    NumberStatus,
    Outcome,
    ParsedNumber,
    PeriodType,
    ReconciliationRecord,
    ReportingBasis,
    Scale,
    SourceFinancialObservation,
    SourceReference,
    SourceType,
    StatementType,
    TolerancePolicy,
    UnmappedFinancialLine,
    ValidationCheck,
    ValueKind,
)
from stock_research_ai.domain.screener_dataset import DetailedScreenerDataset, ScreenerTableKind

_NS = UUID("3f2b7c1d-6a4e-4e8b-9c5d-1a2b3c4d5e6f")
M = CanonicalMetric
_PER_SHARE = {M.BASIC_EPS, M.DILUTED_EPS, M.EPS, M.DPS}
_CONFIDENCE_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}
_METRIC_ORDER = {m: i for i, m in enumerate(CanonicalMetric)}
_FAMILY = {
    StatementType.PROFIT_AND_LOSS: "INCOME",
    StatementType.QUARTERLY_RESULTS: "INCOME",
    StatementType.BALANCE_SHEET: "BALANCE",
    StatementType.CASH_FLOW: "CASH",
    StatementType.CHANGES_IN_EQUITY: "EQUITY",
    StatementType.SEGMENT_RESULTS: "SEGMENT",
}
_BASIS = {
    ReportingScope.CONSOLIDATED: ReportingBasis.CONSOLIDATED,
    ReportingScope.STANDALONE: ReportingBasis.STANDALONE,
    ReportingScope.UNKNOWN: ReportingBasis.UNKNOWN,
}
_SCREENER_TABLES = {
    ScreenerTableKind.PROFIT_LOSS: StatementType.PROFIT_AND_LOSS,
    ScreenerTableKind.QUARTERLY_RESULTS: StatementType.QUARTERLY_RESULTS,
    ScreenerTableKind.BALANCE_SHEET: StatementType.BALANCE_SHEET,
    ScreenerTableKind.CASH_FLOW: StatementType.CASH_FLOW,
}
_FILING_TABLES = {
    FinancialStatementKind.PROFIT_AND_LOSS: StatementType.PROFIT_AND_LOSS,
    FinancialStatementKind.BALANCE_SHEET: StatementType.BALANCE_SHEET,
    FinancialStatementKind.CASH_FLOW: StatementType.CASH_FLOW,
    FinancialStatementKind.CHANGES_IN_EQUITY: StatementType.CHANGES_IN_EQUITY,
    FinancialStatementKind.FINANCIAL_RESULTS: StatementType.QUARTERLY_RESULTS,
    FinancialStatementKind.SEGMENT_RESULTS: StatementType.SEGMENT_RESULTS,
}
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass
class _Collector:
    company_key: str
    limits: FinancialLimits
    observations: list[SourceFinancialObservation] = field(default_factory=list)
    warnings: list[FinancialWarning] = field(default_factory=list)
    partial: bool = False

    def add(self, observation: SourceFinancialObservation) -> None:
        if len(self.observations) >= self.limits.max_observations:
            if not self.partial:
                self.warnings.append(FinancialWarning(code="OBSERVATION_LIMIT_REACHED"))
            self.partial = True
            return
        self.observations.append(observation)

    def warn(self, code: str, detail: str | None = None, ids: Iterable[UUID] = ()) -> None:
        self.warnings.append(
            FinancialWarning(
                code=code, detail=detail[:500] if detail else None, observation_ids=tuple(ids)
            )
        )


def _normalized(parsed: ParsedNumber, kind: ValueKind, scale: Scale) -> Decimal | None:
    if parsed.value is None:
        return None
    if kind == ValueKind.MONEY:
        if scale == Scale.UNKNOWN:
            return None
        return parsed.value * SCALE_FACTOR[scale] / SCALE_FACTOR[CANONICAL_SCALE]
    return parsed.value


def detect_industry(screeners: Sequence[DetailedScreenerDataset]) -> Industry:
    decisions = {
        classify_business(
            [c.name for c in s.profile.classification],
            description=s.profile.about or "",
            labels=[row.label for table in s.tables for row in table.rows],
        )
        for s in screeners
    }
    return decisions.pop() if len(decisions) == 1 else Industry.UNKNOWN


def fiscal_year_end_month(screeners: Sequence[DetailedScreenerDataset]) -> int | None:
    """From Screener's own annual columns, only when they all end in the same month."""
    months = {
        p.end_date.month
        for s in screeners
        for table in s.tables
        if table.kind == ScreenerTableKind.PROFIT_LOSS
        for p in table.periods
        if p.end_date
    }
    return next(iter(months)) if len(months) == 1 else None


# --- Screener adapter --------------------------------------------------------------------------


def screener_observations(
    screener: DetailedScreenerDataset, collector: _Collector, industry: Industry, fy_end: int | None
) -> None:
    mapping: dict[str, CanonicalMetric | None] = dict(SCREENER_GENERAL)
    if industry == Industry.BANK:
        mapping = SCREENER_BANK
    elif industry != Industry.GENERAL:
        # Preserve reported aggregates without interpreting them as industrial operations
        # or silently relabelling a lender's financing profit as a bank-only metric.
        mapping.update({"Revenue": CanonicalMetric.REVENUE, "Operating Profit": None,
                        "Expenses": None, "Interest": None})
    source = screener.source
    for table in screener.tables:
        statement = _SCREENER_TABLES.get(table.kind)
        if statement is None:
            continue  # ratios/shareholding stay in Box 1B; not financial statements
        basis = _BASIS[table.reporting_basis]
        periods = {p.label: p for p in table.periods}
        for row in table.rows:
            if row.label == "Raw PDF":
                continue  # source links, not values (catalogued by Box 1)
            metric = mapping.get(row.label)
            for cell in row.cells:
                screener_period = periods[cell.period]
                end = screener_period.end_date
                if cell.period.upper() == "TTM":
                    period = FinancialPeriod(
                        period_type=PeriodType.TTM, label="TTM", duration_months=12
                    )
                elif statement == StatementType.BALANCE_SHEET:
                    period = period_from(PeriodType.POINT_IN_TIME, None, end, cell.period, fy_end)
                elif statement == StatementType.QUARTERLY_RESULTS:
                    period = period_from(PeriodType.QUARTER, 3, end, cell.period, fy_end)
                else:
                    period = period_from(PeriodType.FY, 12, end, cell.period, fy_end)
                parsed = parse_financial_number(cell.value.displayed)
                unit = cell.value.unit or row.unit
                if unit is None:
                    kind, scale, currency = ValueKind.UNKNOWN, Scale.UNKNOWN, Currency.UNKNOWN
                elif unit.kind == UnitKind.PERCENT:
                    kind, scale, currency = ValueKind.PERCENT, Scale.ONES, Currency.UNKNOWN
                elif unit.kind == UnitKind.MONEY and unit.scale == SCALE_FACTOR[Scale.CRORES]:
                    kind, scale, currency = ValueKind.MONEY, Scale.CRORES, Currency.INR
                elif unit.kind == UnitKind.MONEY:
                    kind, scale, currency = ValueKind.PER_SHARE, Scale.ONES, Currency.INR
                elif unit.kind == UnitKind.NUMBER:
                    kind, scale, currency = ValueKind.COUNT, Scale.ONES, Currency.UNKNOWN
                else:
                    kind, scale, currency = ValueKind.UNKNOWN, Scale.UNKNOWN, Currency.UNKNOWN
                if parsed.status == NumberStatus.BLANK:
                    continue
                collector.add(
                    SourceFinancialObservation(
                        observation_id=uuid5(
                            _NS,
                            f"screener|{collector.company_key}|{source.content_sha256}|{cell.locator}",
                        ),
                        company_key=collector.company_key,
                        metric=metric if kind != ValueKind.PERCENT else None,
                        statement_type=statement,
                        raw_label=row.displayed_label,
                        raw_value=cell.value.displayed,
                        parsed=parsed,
                        value_kind=kind,
                        currency=currency,
                        scale=scale,
                        raw_unit_caption=table.caption,
                        normalized_value=_normalized(parsed, kind, scale),
                        period=period,
                        reporting_basis=basis,
                        authority=SourceAuthority.SECONDARY_AGGREGATOR,
                        source=SourceReference(
                            source_type=SourceType.SCREENER,
                            source_url=source.page_url,
                            retrieved_or_extracted_at=source.retrieved_at,
                            locator=cell.locator,
                            raw_artifact_id=source.raw_artifact_id,
                        ),
                        extraction_method="screener-dataset/box1b",
                        confidence=Confidence.HIGH
                        if end or period.period_type == PeriodType.TTM
                        else Confidence.LOW,
                    )
                )


# --- filing adapter ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _TableFrame:
    value_columns: list[int]
    periods: list[ColumnPeriod]
    unit: UnitCaption | None
    basis: ReportingBasis
    statement: StatementType
    inference: str | None = None  # CONTINUATION_INFERRED / STATEMENT_FROM_PAGE_TITLE
    period_mismatch: bool = False
    notes_declared: bool = False


def _cells(table: ExtractedTable) -> dict[int, dict[int, tuple[str, bool]]]:
    grid: dict[int, dict[int, tuple[str, bool]]] = defaultdict(dict)
    for cell in table.cells:
        grid[cell.row][cell.column] = (cell.raw_text, cell.is_header)
    return grid


_HEADER_WORDS = re.compile(
    r"particulars|year ended|quarter|months? ended|as at|\bnote\b|audited|reviewed|restated|"
    r"^(?:\(?in\b|amount)",
    re.I,
)


def _is_number(text: str) -> bool:
    parsed = parse_financial_number(text)
    return parsed.value is not None and not _YEAR.fullmatch(text.strip())


def _first_data_row(grid: dict[int, dict[int, tuple[str, bool]]]) -> int:
    """Box 3 text tables keep column-header lines as rows. Leading rows are header rows
    while they are flagged as headers, or carry no number other than a year and either
    have header wording or some non-label text (dates, 'Audited', '2026 2025')."""
    for row in sorted(grid):
        cells = grid[row]
        if all(header for _, header in cells.values()):
            continue
        label = cells.get(0, ("", False))[0].strip()
        rest = [text for column, (text, _) in cells.items() if column > 0 and text.strip()]
        if any(_is_number(text) for text in rest):
            return row
        if _HEADER_WORDS.search(label) or (rest and not _HEADER_WORDS.search(label) and not label):
            continue
        if rest and all(_YEAR.search(text) or re.search(r"[A-Za-z]", text) for text in rest):
            continue
        return row
    return max(grid) + 1 if grid else 0


def _numeric_columns(grid: dict[int, dict[int, tuple[str, bool]]], rows: list[int]) -> list[int]:
    """Fallback occupancy only. A decimal's spelling cannot identify a Notes column.

    Explicit period/header structure takes precedence in _frame. In particular 2.08
    and 5.55 are amounts unless their COLUMN, not their spelling, identifies references.
    """
    counts: Counter[int] = Counter()
    numeric_rows = 0
    for r in rows:
        found = False
        for c, (text, _) in grid[r].items():
            token = text.strip()
            if c == 0 or not token:
                continue
            if _valid_value(token):
                counts[c] += 1
                found = True
        numeric_rows += found
    return sorted(
        c for c, n in counts.items() if n * 2 >= numeric_rows and numeric_rows
    )


def _context_lines(
    extraction: DocumentExtraction, table: ExtractedTable, first_row: int
) -> list[str]:
    """Up to 12 page lines above the first data row (header lines included), never
    reaching back into an earlier table on the same page."""
    blocks = {b.block_id: b for b in extraction.blocks}
    page = next(p for p in extraction.pages if p.page_number == table.page_start)
    if not table.block_ids or first_row >= len(table.block_ids):
        return []
    start = blocks[table.block_ids[first_row]].start
    floor = 0
    for other in extraction.tables:
        if (
            other.table_id != table.table_id
            and other.page_start == table.page_start
            and other.block_ids
        ):
            last = blocks[other.block_ids[-1]]
            if last.end <= start:
                floor = max(floor, last.end)
    lines: list[str] = []
    for block in sorted(extraction.blocks, key=lambda b: b.start):
        if block.page_number != table.page_start or block.start < floor or block.end > start:
            continue
        if block.is_repeated_header or block.is_repeated_footer:
            continue  # running headers ('Annual Report 2025-26') are not column headers
        if block.block_type in (BlockType.HEADER, BlockType.FOOTER):
            continue
        lines += [
            line for line in page.raw_text[block.start : block.end].split("\n") if line.strip()
        ]
    return lines[-12:]


_PERIOD_PHRASE = re.compile(
    r"\b(?:(?:three|six|nine|twelve|3|6|9|12)\s+months|quarter|half[- ]year|year)\s+ended\s+"
    r"[A-Za-z]+\.?\s+\d{1,2}\b(?:,?\s+(?:19|20)\d{2}\b)?",
    re.I,
)
_HEADER_VOCABULARY = re.compile(
    r"^(?:quarter|quarters|ended|ending|year|years|months?|three|six|nine|twelve|half|as|at|"
    r"audited|unaudited|reviewed|restated|particulars|note|notes|no|for|the|period|of|in|and|"
    r"condensed|interim|consolidated|standalone|balance|sheet|statement|profit|loss|cash|flows?|"
    r"jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|nov\w*|dec\w*)$",
    re.I,
)


def _header_lines(context: list[str]) -> list[str]:
    """Column-header lines as they are, then - only to fill what they leave open - the
    explicit period phrases of titles and prose (a signature date or a sentence never
    becomes a column period, and a title never overrides the column headers)."""
    header: list[str] = []
    phrases: list[str] = []
    for line in context:
        words = re.findall(r"[A-Za-z]{2,}", line)
        prose = sum(not _HEADER_VOCABULARY.match(w) for w in words) > 3
        if prose or _STATEMENT_TITLE.match(line.strip()):
            phrases += [m.group(0) for m in _PERIOD_PHRASE.finditer(line)]
        else:
            header.append(line)
    return header + phrases


def _page_period_phrase(extraction: DocumentExtraction, table: ExtractedTable) -> str | None:
    """pypdf sometimes emits a statement's title lines after its rows. When a page has
    exactly one distinct period phrase outside every table, it is that table's period."""
    page = next(p for p in extraction.pages if p.page_number == table.page_start)
    table_blocks = {b for t in extraction.tables for b in t.block_ids}
    found = {
        " ".join(m.group(0).lower().split())
        for block in extraction.blocks
        if block.page_number == table.page_start and block.block_id not in table_blocks
        for m in _PERIOD_PHRASE.finditer(page.raw_text[block.start : block.end])
    }
    return next(iter(found)) if len(found) == 1 else None


def _frame(
    extraction: DocumentExtraction,
    table: ExtractedTable,
    head: "_TableFrame | None",
    fy_end: int | None,
    collector: _Collector,
    titled: "tuple[StatementType, ReportingBasis] | None" = None,
) -> tuple[_TableFrame, int] | None:
    statement = titled[0] if titled is not None else None
    if statement is None and table.statement_kind is not None:
        statement = _FILING_TABLES.get(table.statement_kind)
    if statement is None and head is not None:
        statement = head.statement
    if statement is None:
        return None
    grid = _cells(table)
    first_row = _first_data_row(grid)
    data_rows = [r for r in sorted(grid) if r >= first_row]
    header_rows = [r for r in sorted(grid) if r < first_row]
    year_columns = sorted(
        {
            c
            for r in header_rows
            for c, (text, _) in grid[r].items()
            if c > 0 and (
                _YEAR.search(text) if table.method in {"html-table", "xlsx-sheet"}
                else _YEAR.fullmatch(text.strip())
            )
        }
    )
    context = _context_lines(extraction, table, first_row)
    header = _header_lines(context)
    # Text-table columns are right-aligned TOKEN positions, not physical PDF cells.
    # Establish the number of logical value columns from complete period headers,
    # before considering numeric occupancy (which also counts note references).
    candidates = [
        interpret_columns(header, n, statement, fy_end)
        for n in range(1, min(table.column_count - 1, len(_YEAR.findall(" ".join(header)))) + 1)
    ]
    complete = [p for p in candidates if p and not any(c.ambiguous for c in p)]
    header_periods = complete[-1] if complete else None
    notes_declared = any(_NOTE_HEADER.search(line) for line in context) or bool(
        head and head.notes_declared and not year_columns
    )
    numeric = _numeric_columns(grid, data_rows)
    if header_periods is not None and table.method == "text-rows/right-aligned-numbers":
        value_columns = list(range(table.column_count - len(header_periods), table.column_count))
    elif (head is not None and not year_columns and not header_periods
          and table.column_count - 1 >= len(head.periods)):
        value_columns = list(range(table.column_count - len(head.periods), table.column_count))
    elif year_columns and (table.method in {"html-table", "xlsx-sheet"}
                           or not numeric or numeric == year_columns):
        value_columns = year_columns
    elif numeric and (head is None or year_columns or len(numeric) == len(head.value_columns)):
        value_columns = numeric  # the data's own columns (headers may be merged by pypdf)
    elif head is not None and table.column_count - 1 >= len(head.value_columns):
        k = len(head.value_columns)  # explicit continuation: right-aligned value columns
        value_columns = list(range(table.column_count - k, table.column_count))
    else:
        value_columns = year_columns or numeric
    if header_periods is not None and len(header_periods) == len(value_columns):
        periods = header_periods
    elif head is not None and not year_columns and len(value_columns) == len(head.value_columns):
        periods = head.periods
    else:
        periods = interpret_columns(header, len(value_columns), statement, fy_end)
        phrase = (
            _page_period_phrase(extraction, table) if any(p.ambiguous for p in periods) else None
        )
        if phrase is not None:
            retry = interpret_columns([phrase, *header], len(value_columns), statement, fy_end)
            if not any(p.ambiguous for p in retry):
                collector.warn("PERIOD_FROM_PAGE_TEXT", f"{table.locator}: '{phrase}'")
                periods = [replace(p, explicit=False) for p in retry]
    unit = next((u for u in (parse_unit_caption(line) for line in reversed(context)) if u), None)
    if unit is None:
        page = next(p for p in extraction.pages if p.page_number == table.page_start)
        unit = next(
            (u for u in (parse_unit_caption(line) for line in page.raw_text.split("\n")[:15]) if u),
            head.unit if head else None,
        )
    basis = {
        "consolidated": ReportingBasis.CONSOLIDATED,
        "standalone": ReportingBasis.STANDALONE,
    }.get(
        "" if titled else table.reporting_basis or "",
        titled[1] if titled else head.basis if head else ReportingBasis.UNKNOWN,
    )
    durations = {PeriodType.POINT_IN_TIME, PeriodType.UNKNOWN}
    mismatch = False
    if (
        statement in _FAMILY_CHECKED
        and (statement == StatementType.BALANCE_SHEET)
        == any(p.period.period_type not in durations for p in periods)
        and any(p.period.period_type != PeriodType.UNKNOWN for p in periods)
    ):
        # a balance sheet with duration columns, or a flow statement 'as at' a date
        collector.warn("STATEMENT_PERIOD_MISMATCH", f"{table.locator}: {statement.value}")
        mismatch = True
    if any(p.ambiguous for p in periods):
        collector.warn(
            "AMBIGUOUS_HEADER", f"{table.locator}: period not established for some columns"
        )
    if unit is None:
        collector.warn("UNKNOWN_UNIT", table.locator)
    if basis == ReportingBasis.UNKNOWN:
        collector.warn("UNKNOWN_BASIS", table.locator)
    return _TableFrame(
        value_columns, periods, unit, basis, statement, None, mismatch, notes_declared
    ), first_row


def _has_year_header(table: ExtractedTable) -> bool:
    grid = _cells(table)
    first = _first_data_row(grid)
    return any(_YEAR.search(text) for r in grid if r < first for text, _ in grid[r].values())


_NOTE_HEADER = re.compile(
    r"\b(?:(?:particulars|description|items?).*\bnotes?|notes?\s+no\.?|"
    r"ref(?:erence)?\.?)(?:\b|$)|(?:^|\|)\s*notes?\s*(?:$|\|)", re.I
)
_NOTE = re.compile(r"^\d+(?:\.\d+)*(?:[A-Za-z]|\([A-Za-z0-9]+\))?$")
_CELL_METHOD = "; logical-cells/1"
_LOW_WARNINGS = {
    "UNKNOWN_PERIOD",
    "UNKNOWN_UNIT",
    "UNKNOWN_BASIS",
    "SPLIT_NUMBER_SUSPECT",
    "UNLABELLED_ROW",
    "STATEMENT_PERIOD_MISMATCH",
}
_FAMILY_CHECKED = {
    StatementType.BALANCE_SHEET,
    StatementType.PROFIT_AND_LOSS,
    StatementType.CASH_FLOW,
    StatementType.QUARTERLY_RESULTS,
}
_MAJOR_CAPTION = re.compile(
    r"^(?:earnings per|profit attributable|total comprehensive income attributable|"
    r"other comprehensive income|tax expense|expenses|income|cash flows? (?:from|used))"
)
_MEDIUM_WARNINGS = {"SPLIT_NUMBER_REJOINED", "CONTINUATION_INFERRED", "STATEMENT_FROM_PAGE_TITLE"}


def _valid_value(token: str) -> bool:
    if token.endswith(",") or token.startswith(","):
        return False
    status = parse_financial_number(token).status
    return status in (NumberStatus.NUMBER, NumberStatus.ZERO_REPORTED, NumberStatus.DASH)


def _row_values(
    row: dict[int, tuple[str, bool]], value_columns: list[int], *,
    note_columns: tuple[int, ...] = (),
    cell_groups: list[list[int]] | None = None,
) -> tuple[list[tuple[str, int]], str | None]:
    """Read logical cells, never search for a syntactically convenient partition.

    Ordinarily a Box 3 cell is the boundary. For flattened PDF token tables, the caller
    may supply groups established by the source row's cell separators AND the period /
    Notes header. Without that evidence, extra tokens make the row ambiguous. Joining
    arbitrary adjacent valid numbers is forbidden, even if only one Decimal results.
    """
    values = [(row.get(c, ("", False))[0], c) for c in value_columns]
    groups = [[c] for c in value_columns] if cell_groups is None else cell_groups
    used = [c for group in groups for c in group]
    if (len(groups) != len(value_columns) or any(not g for g in groups)
            or len(used) != len(set(used)) or set(used) & set(note_columns)
            or any(c <= 0 for c in used)):
        return values, "SPLIT_NUMBER_SUSPECT"
    extra = [(c, text.strip()) for c, (text, _) in row.items()
             if c > 0 and c not in used and text.strip()]
    if any(c not in note_columns or not _NOTE.fullmatch(text) for c, text in extra):
        return values, "SPLIT_NUMBER_SUSPECT"
    result = [(" ".join(row.get(c, ("", False))[0] for c in group), group[0])
              for group in groups]
    rejoined = False
    for raw, _ in result:
        if _valid_value(raw) or not raw.strip():
            continue
        # Whitespace is removed only INSIDE a proven cell, never across groups.
        compact = re.sub(r"\s+", "", raw)
        if cell_groups is None or compact == raw or not _valid_value(compact):
            return values, "SPLIT_NUMBER_SUSPECT"
        rejoined = True
    return result, "SPLIT_NUMBER_REJOINED" if rejoined else None


def _row_structure(
    extraction: DocumentExtraction, table: ExtractedTable, row: int, frame: _TableFrame,
    cells: dict[int, tuple[str, bool]],
) -> tuple[tuple[int, ...], list[list[int]] | None]:
    """Keep text-table token positions distinct from logical monetary columns.

    Repeated period columns establish width. A declared Notes column may occupy the
    single preceding token position. Recover split amounts only when original row
    separators preserve exactly those logical cells. Otherwise no regrouping occurs.
    HTML/spreadsheet cells already have boundaries and never take this PDF path.
    """
    notes: tuple[int, ...] = ()
    extras = [c for c, (text, _) in sorted(cells.items())
              if c > 0 and c not in frame.value_columns and text.strip()]
    if (frame.notes_declared and frame.value_columns and len(extras) == 1
            and extras[0] < min(frame.value_columns)):
        notes = (extras[0],)
    if table.method in {"html-table", "xlsx-sheet"}:
        return notes, [[c] for c in frame.value_columns]
    if table.method != "text-rows/right-aligned-numbers" or row >= len(table.block_ids):
        return notes, None
    block = next(b for b in extraction.blocks if b.block_id == table.block_ids[row])
    page = next(p for p in extraction.pages if p.page_number == block.page_number)
    line = page.raw_text[block.start:block.end].rstrip()
    tokens = [(c, text.strip()) for c, (text, _) in sorted(cells.items()) if c > 0 and text.strip()]
    # Locate the exact suffix; don't let spaces/tabs in a label become numeric cells.
    suffix = (
        re.search(r"\s+".join(re.escape(t) for _, t in tokens) + r"\s*$", line)
        if tokens else None
    )
    if suffix is None:
        return notes, None
    parts = re.split(r"(?:[^\S\n]{2,}|\t)", suffix.group().strip())
    parts = [p for p in parts if p.strip()]
    n = len(frame.value_columns)
    has_note = frame.notes_declared and len(parts) == n + 1 and bool(_NOTE.fullmatch(parts[0]))
    # A note attached to the label, followed by separated money cells, cannot
    # become the first amount when one year is missing. Preserve the row as
    # ambiguous rather than right-shifting the note into a monetary column.
    before = line[:suffix.start()]
    attached_note = (frame.notes_declared and bool(_NOTE.fullmatch(parts[0]))
                     and before.endswith(" ") and not before.endswith("  "))
    if attached_note and 1 < len(parts) <= n:
        return (), []
    if len(parts) != n + int(has_note):
        return notes, None
    if len(parts) == 1 and len(tokens) > 1:
        return notes, None  # a flattened single-cell row has no visible column boundary
    groups: list[list[int]] = []
    offset = 0
    for part in parts:
        words = part.split()
        found = tokens[offset:offset + len(words)]
        if [t for _, t in found] != words:
            return notes, None
        groups.append([c for c, _ in found])
        offset += len(words)
    if offset != len(tokens):
        return notes, None
    if has_note:
        notes = tuple(groups.pop(0))
    else:
        notes = ()  # the separators account for every token as monetary-cell content
        if any(len(g) > 1 and all(_valid_value(cells[c][0]) for c in g) for g in groups):
            # Without a separate reference cell, e.g. '31 268.08' could be either a
            # space-grouped amount or note + amount. Header width alone cannot decide.
            return (), []
    return notes, groups


_STATEMENT_TITLE = re.compile(
    r"^(?:\d{1,2}\.\s*)?(?:condensed\s+|audited\s+|unaudited\s+|interim\s+)*"
    r"(?:(?P<basis>consolidated|standalone)\s+)?"
    r"(?:(?P<pl>statement of profit and loss)|(?P<bs>balance sheet)|"
    r"(?P<cf>statement of cash flows?)|(?P<ce>statement of changes in equity)|"
    r"(?P<fr>(?:audited\s+|unaudited\s+)?financial results))\b(?P<rest>.*)$",
    re.I,
)
_TITLE_KINDS = {
    "pl": StatementType.PROFIT_AND_LOSS,
    "bs": StatementType.BALANCE_SHEET,
    "cf": StatementType.CASH_FLOW,
    "ce": StatementType.CHANGES_IN_EQUITY,
    "fr": StatementType.QUARTERLY_RESULTS,
}


def _table_statement_title(
    extraction: DocumentExtraction, table: ExtractedTable
) -> tuple[StatementType, ReportingBasis] | None:
    """A statement title naming its basis, printed directly above this table (inside its
    own header context, so a later table on the same page never inherits it)."""
    context = _context_lines(extraction, table, _first_data_row(_cells(table)))
    found: set[tuple[StatementType, ReportingBasis]] = set()
    for line in context:
        match = _STATEMENT_TITLE.match(line.strip()[:200])
        if match is None:
            continue
        lowered = line.lower()
        if "standalone" in lowered:
            basis = ReportingBasis.STANDALONE
        elif "consolidated" in lowered:
            basis = ReportingBasis.CONSOLIDATED
        else:
            continue
        found.add((next(_TITLE_KINDS[k] for k in _TITLE_KINDS if match.group(k)), basis))
    return next(iter(found)) if len(found) == 1 else None


def filing_observations(
    bundle: EvidenceBundle,
    catalogue: EvidenceCatalogue,
    collector: _Collector,
    industry: Industry,
    fy_end: int | None,
) -> None:
    if bundle.company_key != collector.company_key or catalogue.company.company_key != \
            collector.company_key:
        collector.warn(
            "ISSUER_IDENTITY_MISMATCH",
            "filing bundle and catalogue must belong to the canonical dataset company",
        )
        return
    urls = {d.document_id: d.source_url for d in catalogue.documents}
    documents = {d.document_id: d for d in catalogue.documents}
    tables_seen = 0
    for extraction in bundle.extractions:
        if extraction.status not in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL):
            identity_warnings = {
                warning.code for warning in extraction.warnings
                if warning.code in {"ISSUER_IDENTITY_MISMATCH", "ISSUER_IDENTITY_UNVERIFIED"}
            }
            for code in sorted(identity_warnings):
                collector.warn(
                    code,
                    f"document {extraction.document_id} has no usable issuer-bound evidence",
                )
            continue
        document = documents.get(extraction.document_id)
        if (
            extraction.issuer_binding_status != IssuerBindingStatus.VERIFIED
            or extraction.issuer_binding_target_company_key != collector.company_key
            or extraction.company_key != collector.company_key
            or document is None
            or document.company_key != collector.company_key
        ):
            code = (
                "ISSUER_IDENTITY_MISMATCH"
                if extraction.issuer_binding_status == IssuerBindingStatus.MISMATCH
                else "ISSUER_IDENTITY_UNVERIFIED"
            )
            collector.warn(code, f"document {extraction.document_id} excluded from Box 4")
            continue
        frames: dict[UUID, _TableFrame] = {}
        previous: ExtractedTable | None = None
        for table in extraction.tables:
            head = frames.get(table.continuation_of) if table.continuation_of else None
            inferred = False
            titled = None
            # Box 3 copies the previous table's kind onto a continuation, so only a table
            # that is not a continuation has a kind of its own
            if table.statement_kind is None or table.continuation_of is not None:
                titled = _table_statement_title(extraction, table)
            if table.continuation_of is not None and titled is not None:
                linked = (
                    head.statement
                    if head
                    else _FILING_TABLES.get(table.statement_kind)
                    if table.statement_kind
                    else None
                )
                if titled[0] != linked:
                    # the table carries a different statement's title: not a continuation
                    collector.warn("CONTINUATION_REJECTED", table.locator)
                    head = None
                else:
                    titled = None
            if (
                table.statement_kind is None
                and head is None
                and previous is not None
                and previous.table_id in frames
                and previous.page_start == table.page_start
                and not _has_year_header(table)
                and titled is None
            ):
                # Box 3 links only a page's first table; a headerless table right after a
                # statement table on the same page continues it (pypdf broke the rows).
                head, inferred = frames[previous.table_id], True
            previous = table
            if table.statement_kind is None and head is None and titled is None:
                continue
            tables_seen += 1
            if tables_seen > collector.limits.max_tables:
                collector.warn("TABLE_LIMIT_REACHED")
                collector.partial = True
                return
            use_title = titled if head is None else None
            framed = _frame(extraction, table, head, fy_end, collector, use_title)
            if framed is None:
                continue
            frame, first_row = framed
            if inferred:
                frame = replace(frame, inference="CONTINUATION_INFERRED")
                collector.warn("CONTINUATION_INFERRED", table.locator)
            elif use_title is not None:
                frame = replace(frame, inference="STATEMENT_FROM_PAGE_TITLE")
                collector.warn("STATEMENT_FROM_PAGE_TITLE", table.locator)
            elif head is not None and head.inference:
                frame = replace(frame, inference=head.inference)
            frames[table.table_id] = frame
            _table_observations(extraction, table, frame, first_row, urls, collector, industry)


def _label_currency(label: str) -> Currency | None:
    """A per-share line often states its own currency: 'Basic (Rs.)', 'Diluted (in ₹)'."""
    marker = re.search(r"\((?:in\s+)?(₹|`|rs\.?|inr|us\$|\$)", label, re.I)
    if marker is None:
        return None
    return Currency.USD if "$" in marker.group(1) else Currency.INR


def _table_observations(
    extraction: DocumentExtraction,
    table: ExtractedTable,
    frame: _TableFrame,
    first_row: int,
    urls: dict[UUID, str],
    collector: _Collector,
    industry: Industry,
) -> None:
    grid = _cells(table)
    context: list[str] = []
    bank = industry == Industry.BANK
    for row in sorted(grid):
        if row < first_row:
            continue
        label = grid[row].get(0, ("", False))[0].strip()
        note_columns, groups = _row_structure(extraction, table, row, frame, grid[row])
        values, row_warning = _row_values(
            grid[row], frame.value_columns, note_columns=note_columns, cell_groups=groups
        )
        if label and not any(v.strip() for v, _ in values):
            caption = normalize_label(label)
            if caption in ("assets", "equity and liabilities", "liabilities") or (
                frame.statement != StatementType.BALANCE_SHEET and _MAJOR_CAPTION.match(caption)
            ):
                context = [caption]
            else:
                # e.g. 'non-current liabilities' > 'financial liabilities'
                context = (context + [caption])[-3:]
            continue
        if not any(v.strip() for v, _ in values):
            continue  # an empty line inside the table
        statement = frame.statement
        metric = None
        segment = None
        if not re.search(r"[A-Za-z]", label):
            # an unlabelled subtotal, or a number fragment pypdf pushed into the label:
            # kept (unmapped, low confidence), never attached to the line above
            values = [(grid[row].get(c, ("", False))[0], c) for c in frame.value_columns]
            row_warning = "UNLABELLED_ROW"
            label = label or "(unlabelled row)"
        elif statement == StatementType.SEGMENT_RESULTS:
            segment = label
        elif statement != StatementType.CHANGES_IN_EQUITY:
            metric = map_filing_label(statement, label, context, bank)
        innermost = f"{label} {context[-1] if context else ''}"
        share_count = bool(
            re.search(r"\bin shares\b|number of shares|weighted average", innermost, re.I)
        )
        if share_count:
            metric = None  # share counts are not EPS, whatever caption they sit under
        per_share = not share_count and (
            metric in _PER_SHARE
            or bool(re.search(r"per (equity )?share|\beps\b", f"{label} {' '.join(context)}", re.I))
        )
        for position, (raw, column) in enumerate(values):
            rejoined = row_warning == "SPLIT_NUMBER_REJOINED"
            parsed = parse_financial_number(re.sub(r"\s+", "", raw) if rejoined else raw)
            if parsed.status == NumberStatus.BLANK:
                continue
            column_period = frame.periods[position] if position < len(frame.periods) else None
            period = (
                column_period.period
                if column_period
                else FinancialPeriod(period_type=PeriodType.UNKNOWN, label="unlabelled column")
            )
            unit = frame.unit
            if parsed.is_percent:
                kind, scale, currency = ValueKind.PERCENT, Scale.ONES, Currency.UNKNOWN
            elif share_count:
                kind, scale, currency = ValueKind.COUNT, Scale.ONES, Currency.UNKNOWN
            elif per_share:
                kind, scale = ValueKind.PER_SHARE, Scale.ONES
                currency = _label_currency(label) or (unit.currency if unit else Currency.UNKNOWN)
            else:
                kind = ValueKind.MONEY
                scale = unit.scale if unit else Scale.UNKNOWN
                currency = unit.currency if unit else Currency.UNKNOWN
            warnings: list[str] = []
            if period.period_type == PeriodType.UNKNOWN:
                warnings.append("UNKNOWN_PERIOD")
            if kind == ValueKind.MONEY and (scale == Scale.UNKNOWN or currency == Currency.UNKNOWN):
                warnings.append("UNKNOWN_UNIT")
            if frame.basis == ReportingBasis.UNKNOWN:
                warnings.append("UNKNOWN_BASIS")
            if metric is None and statement not in (
                StatementType.SEGMENT_RESULTS,
                StatementType.CHANGES_IN_EQUITY,
            ):
                warnings.append("UNMAPPED_LABEL")
            if row_warning:
                warnings.append(row_warning)
            if frame.inference:
                warnings.append(frame.inference)
            if frame.period_mismatch:
                warnings.append("STATEMENT_PERIOD_MISMATCH")
            if _LOW_WARNINGS.intersection(warnings):
                confidence = Confidence.LOW
            elif (
                column_period is not None
                and column_period.explicit
                and frame.unit is not None
                and not _MEDIUM_WARNINGS.intersection(warnings)
            ):
                confidence = Confidence.HIGH
            else:
                confidence = Confidence.MEDIUM
            collector.add(
                SourceFinancialObservation(
                    observation_id=uuid5(
                        _NS, f"filing|{extraction.artifact_sha256}|{table.table_id}|{row}|{column}"
                    ),
                    company_key=collector.company_key,
                    metric=metric,
                    statement_type=statement,
                    raw_label=label[:500],
                    label_context=" > ".join(context)[:500] or None,
                    raw_value=raw[:500],
                    parsed=parsed,
                    value_kind=kind,
                    currency=currency,
                    scale=scale,
                    raw_unit_caption=unit.caption[:500] if unit else None,
                    normalized_value=_normalized(parsed, kind, scale),
                    period=period,
                    reporting_basis=frame.basis,
                    authority=extraction.authority,
                    source=SourceReference(
                        source_type=SourceType.FILING,
                        source_url=urls.get(extraction.document_id, "unknown")[:500],
                        retrieved_or_extracted_at=extraction.extracted_at,
                        document_id=extraction.document_id,
                        artifact_sha256=extraction.artifact_sha256,
                        extraction_id=extraction.extraction_id,
                        page_number=table.page_start,
                        table_id=table.table_id,
                        row_index=row,
                        column_index=column,
                        locator=f"{table.locator}/r{row}/c{column}",
                    ),
                    extraction_method=f"box3-table/{table.method}" + (
                        _CELL_METHOD if row_warning in (None, "SPLIT_NUMBER_REJOINED") else ""
                    ),
                    confidence=confidence,
                    audited=column_period.audited if column_period else None,
                    restated=True if column_period and column_period.restated else None,
                    segment=segment,
                    warnings=tuple(warnings),
                )
            )


# --- reconciliation ------------------------------------------------------------------------------


def _half_unit(observation: SourceFinancialObservation, policy: TolerancePolicy) -> Decimal:
    unit = Decimal(1).scaleb(-observation.parsed.decimals)
    if observation.value_kind == ValueKind.MONEY:
        unit = unit * SCALE_FACTOR[observation.scale] / SCALE_FACTOR[CANONICAL_SCALE]
    return unit * policy.rounding_half_units


def _agree(
    a: SourceFinancialObservation, b: SourceFinancialObservation, policy: TolerancePolicy
) -> bool:
    assert a.normalized_value is not None and b.normalized_value is not None
    band = max(_half_unit(a, policy), _half_unit(b, policy)) + policy.relative * max(
        abs(a.normalized_value), abs(b.normalized_value)
    )
    return abs(a.normalized_value - b.normalized_value) <= band


def _key(o: SourceFinancialObservation) -> str:
    return "|".join(
        [
            o.metric.value if o.metric else "-",
            _FAMILY[o.statement_type],
            o.reporting_basis.value,
            o.currency.value,
            o.value_kind.value,
            o.period.key(),
        ]
    )


def _comparable(o: SourceFinancialObservation) -> bool:
    return (
        o.period.period_type != PeriodType.UNKNOWN
        and (o.period.period_end is not None or o.period.period_type == PeriodType.TTM)
        and o.reporting_basis != ReportingBasis.UNKNOWN
        and o.value_kind != ValueKind.UNKNOWN
        and not (o.value_kind == ValueKind.MONEY and o.scale == Scale.UNKNOWN)
        and not (
            o.value_kind in (ValueKind.MONEY, ValueKind.PER_SHARE)
            and o.currency == Currency.UNKNOWN
        )
    )


def _order(o: SourceFinancialObservation) -> tuple[object, ...]:
    return (
        AUTHORITY_RANK[o.authority],
        _CONFIDENCE_RANK[o.confidence],
        o.source.source_type != SourceType.FILING,
        -(o.parsed.decimals),
        str(o.observation_id),
    )


def extraction_integrity_issue(o: SourceFinancialObservation) -> str | None:
    """Authority ranks evidence only AFTER structural and scalar integrity checks.

    Legacy text-table/rejoined observations have no logical-cell attestation and must
    be re-extracted, not promoted by their existing Decimal or confidence. Evidence is
    retained with an explicit exclusion; a valid independent fallback may be selected.
    """
    structural = {"SPLIT_NUMBER_SUSPECT", "UNLABELLED_ROW", "STATEMENT_PERIOD_MISMATCH"}
    if structural.intersection(o.warnings):
        reasons = ", ".join(sorted(structural.intersection(o.warnings)))
        return "ambiguous table structure: " + reasons
    bounded = o.extraction_method.endswith(_CELL_METHOD)
    if o.source.source_type == SourceType.FILING and not bounded and (
        "text-rows/right-aligned-numbers" in o.extraction_method
        or "SPLIT_NUMBER_REJOINED" in o.warnings
    ):
        return "numeric cell boundaries not verified; re-extraction required"
    raw = (re.sub(r"\s+", "", o.raw_value)
           if bounded and "SPLIT_NUMBER_REJOINED" in o.warnings else o.raw_value)
    parsed = parse_financial_number(raw)
    if parsed.value != o.parsed.value or parsed.status != o.parsed.status:
        return "raw cell and parsed number disagree"
    if _normalized(parsed, o.value_kind, o.scale) != o.normalized_value:
        return "parsed number and normalized value disagree"
    return None


def reconcile(
    observations: Sequence[SourceFinancialObservation],
    policy: TolerancePolicy,
    collector: _Collector,
) -> list[ReconciliationRecord]:
    groups: dict[str, list[SourceFinancialObservation]] = defaultdict(list)
    for o in observations:
        if o.metric is not None:
            groups[_key(o)].append(o)
    if len(groups) > collector.limits.max_reconciliation_groups:
        collector.warn("RECONCILIATION_GROUP_LIMIT_REACHED")
        collector.partial = True
        groups = dict(sorted(groups.items())[: collector.limits.max_reconciliation_groups])
    records: list[ReconciliationRecord] = []
    for key in sorted(groups):
        members = sorted(groups[key], key=_order)
        records.append(_decide(key, members, policy, collector))
    return records


def _record(
    key: str,
    sample: SourceFinancialObservation,
    outcome: Outcome,
    reason: str,
    selected: SourceFinancialObservation | None = None,
    supporting: Sequence[SourceFinancialObservation] = (),
    conflicting: Sequence[SourceFinancialObservation] = (),
    excluded: Sequence[SourceFinancialObservation] = (),
    difference: Decimal | None = None,
) -> ReconciliationRecord:
    assert sample.metric is not None
    return ReconciliationRecord(
        record_id=uuid5(_NS, f"record|{sample.company_key}|{key}"),
        key=key,
        metric=sample.metric,
        statement_type=sample.statement_type,
        period=sample.period,
        reporting_basis=sample.reporting_basis,
        currency=sample.currency,
        value_kind=sample.value_kind,
        outcome=outcome,
        selected_observation_id=selected.observation_id if selected else None,
        supporting_observation_ids=tuple(o.observation_id for o in supporting),
        conflicting_observation_ids=tuple(o.observation_id for o in conflicting),
        excluded_observation_ids=tuple(o.observation_id for o in excluded),
        max_difference=difference,
        reason=reason[:500],
    )


def _decide(
    key: str,
    members: list[SourceFinancialObservation],
    policy: TolerancePolicy,
    collector: _Collector,
) -> ReconciliationRecord:
    sample = members[0]
    rejected = {o.observation_id: issue for o in members
                if (issue := extraction_integrity_issue(o)) is not None}
    for oid, issue in rejected.items():
        collector.warn("EXTRACTION_INTEGRITY_REJECTED", issue, [oid])
    numeric = [o for o in members if o.observation_id not in rejected
               and o.normalized_value is not None and _comparable(o)]
    excluded = [o for o in members if o not in numeric]
    if not numeric:
        return _record(
            key,
            sample,
            Outcome.NOT_COMPARABLE,
            "extraction integrity rejected; no eligible observation" if rejected else
            "no numeric observation with a known period, basis and unit",
            excluded=excluded,
        )
    usable = [o for o in numeric if o.confidence != Confidence.LOW]
    if not usable:
        return _record(
            key,
            sample,
            Outcome.NOT_COMPARABLE,
            "only low-confidence observations: preserved, not canonical",
            excluded=members,
        )
    excluded += [o for o in numeric if o.confidence == Confidence.LOW]
    best = usable[0]
    assert best.normalized_value is not None
    if len(usable) == 1:
        return _record(
            key,
            sample,
            Outcome.SINGLE_SOURCE,
            "single usable observation; invalid extraction excluded" if rejected else
            "single usable observation",
            best,
            [best],
            excluded=excluded,
        )
    difference = max(
        abs((o.normalized_value or Decimal(0)) - best.normalized_value) for o in usable
    )
    if all(o.normalized_value == best.normalized_value for o in usable):
        return _record(
            key,
            sample,
            Outcome.MATCH,
            "all sources report the same value",
            best,
            usable,
            excluded=excluded,
            difference=difference,
        )
    agreeing = [o for o in usable if _agree(o, best, policy)]
    disagreeing = [o for o in usable if o not in agreeing]
    if not disagreeing:
        return _record(
            key,
            sample,
            Outcome.MATCH_WITHIN_TOLERANCE,
            "all sources agree within rounding tolerance; highest authority/precision selected",
            best,
            usable,
            excluded=excluded,
            difference=difference,
        )
    if all(AUTHORITY_RANK[o.authority] > AUTHORITY_RANK[best.authority] for o in disagreeing):
        collector.warn(
            "RECONCILIATION_CONFLICT",
            f"{key}: {best.authority.value} source selected over lower-authority sources",
            [best.observation_id, *(o.observation_id for o in disagreeing)],
        )
        return _record(
            key,
            sample,
            Outcome.SOURCE_PRECEDENCE,
            f"{best.authority.value} {best.source.source_type.value} outranks the disagreeing "
            "lower-authority source(s); both values kept",
            best,
            agreeing,
            disagreeing,
            excluded,
            difference,
        )
    same_rank = [o for o in usable if AUTHORITY_RANK[o.authority] == AUTHORITY_RANK[best.authority]]
    restated = [o for o in same_rank if o.restated]
    documents = {o.source.document_id for o in same_rank}
    if restated and len(documents) > 1:
        chosen = restated[0]
        others = [o for o in usable if not _agree(o, chosen, policy)]
        return _record(
            key,
            sample,
            Outcome.RESTATEMENT,
            "a later filing explicitly marks this comparative as restated; original values kept",
            chosen,
            [o for o in usable if o not in others],
            others,
            excluded,
            difference,
        )
    if len(documents) == 1 and all(o.source.source_type == SourceType.FILING for o in same_rank):
        collector.warn(
            "AMBIGUOUS_VALUE",
            f"{key}: one document reports different values",
            [o.observation_id for o in usable],
        )
        return _record(
            key,
            sample,
            Outcome.AMBIGUOUS,
            "the same document reports different values for this key; none selected",
            None,
            [],
            usable,
            excluded,
            difference,
        )
    if len(documents) > 1:
        collector.warn("POSSIBLE_RESTATEMENT", f"{key}: filings differ without a 'restated' marker")
    collector.warn(
        "RECONCILIATION_CONFLICT",
        f"{key}: equal-authority sources disagree",
        [o.observation_id for o in usable],
    )
    return _record(
        key,
        sample,
        Outcome.CONFLICT,
        "equal-authority sources disagree beyond tolerance; no value selected",
        None,
        [],
        usable,
        excluded,
        difference,
    )


# --- canonical statements and validation --------------------------------------------------------


def canonical_values(
    records: Sequence[ReconciliationRecord], observations: dict[UUID, SourceFinancialObservation]
) -> list[CanonicalFinancialValue]:
    values: list[CanonicalFinancialValue] = []
    for record in records:
        if record.selected_observation_id is None:
            continue
        selected = observations[record.selected_observation_id]
        if extraction_integrity_issue(selected) is not None:
            continue  # defense against a stale/inconsistent externally supplied selection
        assert selected.normalized_value is not None
        values.append(
            CanonicalFinancialValue(
                canonical_value_id=uuid5(_NS, f"canonical|{record.record_id}"),
                metric=record.metric,
                value=selected.normalized_value,
                value_kind=record.value_kind,
                currency=record.currency,
                scale=CANONICAL_SCALE if record.value_kind == ValueKind.MONEY else Scale.ONES,
                period=selected.period,
                reporting_basis=record.reporting_basis,
                statement_type=selected.statement_type,
                selected_observation_id=selected.observation_id,
                supporting_observation_ids=record.supporting_observation_ids,
                conflicting_observation_ids=record.conflicting_observation_ids,
                reconciliation_record_id=record.record_id,
                outcome=record.outcome,
                selection_reason=record.reason,
                confidence=selected.confidence,
            )
        )
    return values


def _frequency(value: CanonicalFinancialValue) -> str:
    if value.period.period_type == PeriodType.POINT_IN_TIME:
        return "POINT_IN_TIME"
    if value.period.period_type in (PeriodType.FY, PeriodType.TTM):
        return "ANNUAL"
    return "QUARTERLY"


def build_statements(values: Sequence[CanonicalFinancialValue]) -> list[CanonicalStatement]:
    buckets: dict[tuple[str, ReportingBasis, str], list[CanonicalFinancialValue]] = defaultdict(
        list
    )
    for value in values:
        family = _FAMILY[value.statement_type]
        buckets[(family, value.reporting_basis, _frequency(value))].append(value)
    statements: list[CanonicalStatement] = []
    for (family, basis, frequency), members in sorted(
        buckets.items(), key=lambda i: (i[0][0], i[0][1].value, i[0][2])
    ):
        statement_type = {
            "INCOME": StatementType.QUARTERLY_RESULTS
            if frequency == "QUARTERLY"
            else StatementType.PROFIT_AND_LOSS,
            "BALANCE": StatementType.BALANCE_SHEET,
            "CASH": StatementType.CASH_FLOW,
        }.get(family)
        if statement_type is None:
            continue

        def period_order(p: FinancialPeriod) -> tuple[object, ...]:
            return (p.period_end or date.max, p.duration_months or 0, p.period_type.value)

        periods = sorted({v.period.key(): v.period for v in members}.values(), key=period_order)
        by_metric: dict[CanonicalMetric, list[CanonicalFinancialValue]] = defaultdict(list)
        for value in members:
            by_metric[value.metric].append(value)
        statements.append(
            CanonicalStatement(
                statement_type=statement_type,
                reporting_basis=basis,
                frequency=frequency,  # type: ignore[arg-type]
                periods=tuple(periods),
                lines=tuple(
                    CanonicalLine(
                        metric=metric,
                        values=tuple(sorted(items, key=lambda v: period_order(v.period))),
                    )
                    for metric, items in sorted(
                        by_metric.items(), key=lambda i: _METRIC_ORDER[i[0]]
                    )
                ),
            )
        )
    return statements


def validate(
    values: Sequence[CanonicalFinancialValue],
    observations: dict[UUID, SourceFinancialObservation],
    policy: TolerancePolicy,
    collector: _Collector,
) -> list[ValidationCheck]:
    index: dict[tuple[ReportingBasis, str, CanonicalMetric], CanonicalFinancialValue] = {}
    for value in values:
        if value.value_kind == ValueKind.MONEY:
            # one currency per check: IFRS-USD and Ind AS-INR statements never mix
            group = f"{value.currency.value}|{value.period.key()}"
            index[(value.reporting_basis, group, value.metric)] = value
    groups = sorted({(b, p) for b, p, _ in index}, key=lambda i: (i[0].value, i[1]))
    checks: list[ValidationCheck] = []

    def check(
        name: str,
        basis: ReportingBasis,
        period: str,
        expected_parts: list[tuple[CanonicalMetric, int]],
        actual: CanonicalMetric,
    ) -> None:
        parts = [(index.get((basis, period, m)), sign) for m, sign in expected_parts]
        target = index.get((basis, period, actual))
        if target is None or any(p is None for p, _ in parts):
            return
        expected = sum((p.value * sign for p, sign in parts if p is not None), Decimal(0))
        used = [p for p, _ in parts if p is not None] + [target]
        band = sum(
            (_half_unit(observations[v.selected_observation_id], policy) for v in used), Decimal(0)
        )
        band += policy.validation_relative * abs(expected)
        difference = target.value - expected
        status = "PASS" if abs(difference) <= band else "FAIL"
        if status == "FAIL":
            collector.warn(
                "ACCOUNTING_EQUATION_MISMATCH",
                f"{name} {basis.value} {period}: difference {difference}",
                [v.selected_observation_id for v in used],
            )
        checks.append(
            ValidationCheck(
                name=name,  # type: ignore[arg-type]
                reporting_basis=basis,
                period=target.period,
                status=status,  # type: ignore[arg-type]
                expected=expected,
                actual=target.value,
                difference=difference,
                canonical_value_ids=tuple(v.canonical_value_id for v in used),
            )
        )

    for basis, period in groups:
        if (basis, period, M.TOTAL_EQUITY_AND_LIABILITIES) in index:
            check(
                "BALANCE_SHEET_EQUATION",
                basis,
                period,
                [(M.TOTAL_EQUITY_AND_LIABILITIES, 1)],
                M.TOTAL_ASSETS,
            )
        else:
            check(
                "BALANCE_SHEET_EQUATION",
                basis,
                period,
                [(M.TOTAL_EQUITY, 1), (M.TOTAL_LIABILITIES, 1)],
                M.TOTAL_ASSETS,
            )
        cash_parts = [(M.OPENING_CASH, 1), (M.NET_CHANGE_IN_CASH, 1)]
        if (basis, period, M.FX_EFFECT_ON_CASH) in index:
            cash_parts.append((M.FX_EFFECT_ON_CASH, 1))
        check("CASH_RECONCILIATION", basis, period, cash_parts, M.CLOSING_CASH)
        check("PBT_TAX_PAT", basis, period, [(M.PBT, 1), (M.TOTAL_TAX, -1)], M.PAT)
    return checks


# --- dataset ---------------------------------------------------------------------------


def _unmapped(
    observations: Sequence[SourceFinancialObservation], collector: _Collector
) -> list[UnmappedFinancialLine]:
    lines: dict[tuple[str, ...], list[SourceFinancialObservation]] = defaultdict(list)
    for o in observations:
        if o.metric is None:
            lines[
                (
                    o.statement_type.value,
                    o.reporting_basis.value,
                    o.source.source_type.value,
                    o.raw_label,
                    o.label_context or "",
                )
            ].append(o)
    result = [
        UnmappedFinancialLine(
            line_id=uuid5(_NS, "unmapped|" + "|".join(key)),
            statement_type=StatementType(key[0]),
            reporting_basis=ReportingBasis(key[1]),
            raw_label=key[3],
            label_context=key[4] or None,
            source_type=SourceType(key[2]),
            observation_ids=tuple(o.observation_id for o in members),
        )
        for key, members in sorted(lines.items())
    ]
    if len(result) > collector.limits.max_unmapped_lines:
        collector.warn(
            "UNMAPPED_LINE_LIMIT_REACHED", f"{len(result)} lines; observations are all kept"
        )
        collector.partial = True
        result = result[: collector.limits.max_unmapped_lines]
    if result:
        collector.warn(
            "UNMAPPED_LABEL",
            f"{len(result)} distinct financial lines are not in taxonomy {TAXONOMY_VERSION}",
        )
    return result


def build_canonical_dataset(
    *,
    company_key: str,
    company_name: str,
    screeners: Sequence[DetailedScreenerDataset],
    bundle: EvidenceBundle | None,
    catalogue: EvidenceCatalogue | None,
    generated_at: datetime,
    tolerance: TolerancePolicy | None = None,
    limits: FinancialLimits | None = None,
) -> CanonicalFinancialDataset:
    policy = tolerance or TolerancePolicy()
    bounds = limits or FinancialLimits()
    collector = _Collector(company_key, bounds)
    if len({s.company_key for s in screeners}) > 1:
        raise ValueError("Screener datasets belong to different companies")
    if any(s.company_key != company_key for s in screeners):
        collector.warn(
            "COMPANY_IDENTITY_MISMATCH",
            "Screener dataset company key does not match the canonical dataset company",
        )
        screeners = ()
    industry = detect_industry(screeners)
    if industry == Industry.UNKNOWN and screeners:
        collector.warn("BUSINESS_CLASSIFICATION_UNRESOLVED",
                       f"{CLASSIFICATION_VERSION}: absent, ambiguous or conflicting business "
                       "evidence; industrial and bank-only metrics are restricted")
    fy_end = fiscal_year_end_month(screeners)
    for screener in screeners:
        screener_observations(screener, collector, industry, fy_end)
    if not screeners:
        collector.warn("SOURCE_MISSING", "no Screener dataset supplied")
    if bundle is not None and catalogue is not None:
        filing_observations(bundle, catalogue, collector, industry, fy_end)
    else:
        collector.warn("SOURCE_MISSING", "no filing evidence supplied")
    observations = sorted(collector.observations, key=lambda o: str(o.observation_id))
    index = {o.observation_id: o for o in observations}
    records = reconcile(observations, policy, collector)
    values = canonical_values(records, index)
    statements = build_statements(values)
    validations = validate(values, index, policy, collector)
    unmapped = _unmapped(observations, collector)
    outcomes = Counter(r.outcome.value for r in records)
    return CanonicalFinancialDataset(
        dataset_id=uuid5(
            _NS,
            "|".join(
                [
                    company_key,
                    *sorted(s.source.content_sha256 for s in screeners),
                    str(bundle.bundle_id) if bundle else "-",
                    TAXONOMY_VERSION,
                    CLASSIFICATION_VERSION,
                    policy.model_dump_json(),
                    bounds.model_dump_json(),
                ]
            ),
        ),
        company_key=company_key,
        company_name=company_name,
        industry=industry,
        fiscal_year_end_month=fy_end,
        generated_at=generated_at,
        screener_source_sha256s=tuple(sorted(s.source.content_sha256 for s in screeners)),
        evidence_bundle_id=bundle.bundle_id if bundle else None,
        tolerance=policy,
        limits=bounds,
        status="PARTIAL" if collector.partial else "COMPLETE",
        observations=tuple(observations),
        unmapped_lines=tuple(unmapped),
        reconciliation=tuple(records),
        canonical_values=tuple(values),
        statements=tuple(statements),
        validations=tuple(validations),
        warnings=tuple(sorted(collector.warnings, key=lambda w: (w.code, w.detail or ""))),
        statistics=FinancialStatistics(
            total_observations=len(observations),
            observations_by_source=dict(
                sorted(Counter(o.source.source_type.value for o in observations).items())
            ),
            observations_by_statement=dict(
                sorted(Counter(o.statement_type.value for o in observations).items())
            ),
            mapped_observations=sum(o.metric is not None for o in observations),
            unmapped_observations=sum(o.metric is None for o in observations),
            unmapped_lines=len(unmapped),
            canonical_values=len(values),
            outcomes=dict(sorted(outcomes.items())),
            validation_pass=sum(c.status == "PASS" for c in validations),
            validation_fail=sum(c.status == "FAIL" for c in validations),
        ),
    )


def canonical_dataset_json(dataset: CanonicalFinancialDataset) -> str:
    return json.dumps(
        dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def verify_financial_provenance(
    dataset: CanonicalFinancialDataset,
    screeners: Sequence[DetailedScreenerDataset],
    bundle: EvidenceBundle | None,
) -> list[str]:
    """Every canonical value -> observation; every filing observation -> Box 3 table cell
    on its page in its artifact; every Screener observation -> the Box 1B page artifact."""
    problems: list[str] = []
    observations = {o.observation_id: o for o in dataset.observations}
    for value in dataset.canonical_values:
        if value.selected_observation_id not in observations:
            problems.append(f"canonical {value.canonical_value_id} has no observation")
    extractions = {e.document_id: e for e in bundle.extractions} if bundle else {}
    for o in dataset.observations:
        src = o.source
        if src.source_type == SourceType.SCREENER:
            if src.raw_artifact_id not in {s.source.raw_artifact_id for s in screeners}:
                problems.append(f"screener observation {o.observation_id} lost its page artifact")
            continue
        extraction = extractions.get(src.document_id) if src.document_id else None
        if extraction is None or extraction.artifact_sha256 != src.artifact_sha256:
            problems.append(f"filing observation {o.observation_id} lost its document/artifact")
            continue
        table = next((t for t in extraction.tables if t.table_id == src.table_id), None)
        if table is None or table.page_start != src.page_number:
            problems.append(f"filing observation {o.observation_id} lost its table/page")
            continue
        cell = next(
            (c for c in table.cells if c.row == src.row_index and c.column == src.column_index),
            None,
        )
        if "SPLIT_NUMBER_REJOINED" in o.warnings and (cell is None or cell.raw_text != o.raw_value):
            row = [
                c.raw_text.strip()
                for c in sorted(table.cells, key=lambda c: c.column)
                if c.row == src.row_index and c.raw_text.strip()
            ]
            pieces = o.raw_value.split(" ")
            if not any(row[i : i + len(pieces)] == pieces for i in range(len(row))):
                problems.append(f"filing observation {o.observation_id} fragments not in its row")
        elif cell is None or cell.raw_text != o.raw_value:
            problems.append(f"filing observation {o.observation_id} does not match its cell")
    return problems
