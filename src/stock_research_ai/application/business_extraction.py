"""DETAILED_RESEARCH_V1 Box 7: Box 3 evidence -> business observations and facts.

Reads every Box 3 extraction as table frames (see business_lines) and turns each frame
into segment, geography or operating-KPI observations, each pointing at its exact
page / block / line / value column. Narrative text becomes BusinessEvidence snippets
only - numbers are never scraped from paragraphs. Labelled 'Key: value' project blocks
become ProjectFacts. Anything Box 7 cannot interpret deterministically is preserved as
an UnmappedOperatingLine with its reason.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid5

from stock_research_ai.application.business_lines import (
    NS,
    Frame,
    Line,
    Row,
    build_rows,
    caption_unit,
    explicit_basis,
    find_frames,
    frame_periods,
    is_header_line,
    is_period_phrase,
    matrix_header,
    matrix_rows,
    page_lines,
    page_period_phrases,
    read_values,
    section_basis,
    section_kind_at,
    split_matrix_header,
    trailing_values,
)
from stock_research_ai.application.business_registry import (
    EVIDENCE_PATTERNS,
    GEOGRAPHY_CONTEXT,
    SEGMENT_CONTEXT,
    SUMMABLE,
    KPIDefinition,
    KPIMatch,
    LabelParts,
    UnitReading,
    attribution,
    clean_name,
    geography_dimension,
    heading_key,
    match_kpi,
    name_key,
    qualifier,
    read_unit,
    row_role,
    segment_metric_heading,
    split_label,
)
from stock_research_ai.application.financial_parsing import (
    ColumnPeriod,
    interpret_columns,
    parse_financial_number,
)
from stock_research_ai.domain.business_intelligence import (
    BusinessEvidence,
    BusinessEvidenceKind,
    BusinessLimits,
    BusinessWarning,
    Domain,
    GeographyDimension,
    GeographyMetric,
    GeographyObservation,
    KPIPeriodKind,
    KPIUnit,
    OperatingKPIObservation,
    ProjectFact,
    ProjectStatus,
    SectorFamily,
    SegmentLevel,
    SegmentMetric,
    SegmentObservation,
    SegmentRole,
    Severity,
    SourceKind,
    SourceLocation,
    UnmappedOperatingLine,
)
from stock_research_ai.domain.document_intelligence import (
    DocumentExtraction,
    EvidenceBundle,
    EvidenceItem,
    EvidenceKind,
    ExtractionStatus,
    FinancialStatementKind,
    SectionKind,
)
from stock_research_ai.domain.evidence import EvidenceCatalogue
from stock_research_ai.domain.financial_canonical import (
    CANONICAL_SCALE,
    SCALE_FACTOR,
    CanonicalFinancialDataset,
    Confidence,
    Currency,
    FinancialPeriod,
    ParsedNumber,
    PeriodType,
    ReportingBasis,
    Scale,
    StatementType,
    ValueKind,
)

_STATEMENT_TABLES = {
    FinancialStatementKind.PROFIT_AND_LOSS,
    FinancialStatementKind.BALANCE_SHEET,
    FinancialStatementKind.CASH_FLOW,
    FinancialStatementKind.CHANGES_IN_EQUITY,
    FinancialStatementKind.FINANCIAL_RESULTS,
}
_OPERATIONAL_TITLE = re.compile(
    r"operational (?:highlights|metrics|performance|data|statistics|parameters)|operating "
    r"(?:highlights|metrics|performance|data|statistics)|key (?:performance )?(?:indicators|"
    r"metrics|operating|operational)|\bkpis?\b|business highlights|capacity (?:and|&) "
    r"(?:utili[sz]ation|production)|order book|client data|employee metrics|effort (?:and|&) "
    r"utili[sz]ation|production (?:and|&) sales|key statistics",
    re.I,
)
#: titles that make a table operational only for one sector family
_FAMILY_TITLE = {
    SectorFamily.FINANCIAL: re.compile(
        r"asset quality|business (?:performance|parameters|highlights)|key ratios|branch network"
        r"|deposits? (?:and|&) advances|capital adequacy",
        re.I,
    ),
}
_FINANCIAL_LABEL = re.compile(
    r"\b(?:revenues?|profit|ebitda|ebit|eps|earnings per share|market capitali[sz]ation|"
    r"dividends?|cash flows?|margin|net worth|return on|income|expenses?|tax|assets|"
    r"liabilities|equity|borrowings|debt)\b",
    re.I,
)
#: matrix metrics whose single-value lines below form a bridge to the company figure
_BRIDGED = {
    SegmentMetric.REVENUE,
    SegmentMetric.SEGMENT_RESULT,
    SegmentMetric.EBIT,
    SegmentMetric.ASSETS,
    SegmentMetric.LIABILITIES,
}
_ENUMERATOR = re.compile(r"^(?:[a-z]|[ivx]{1,4}|\d{1,2})[.)]\s+", re.I)
_DEFINITION_NAME = re.compile(
    r"^\W{0,3}\s*(?P<name>[A-Z][\w&,\-/ ]{1,60}?)\s*:\s*(?:includes?|comprises?|consists? of"
    r"|represents?)\b",
    re.I,
)
_SUB_SEGMENT = re.compile(
    r"^(?P<name>[A-Z][\w&,\-/ ]{1,60}?)\s*\((?:a|the)?\s*sub-?segment of (?:the )?"
    r"(?P<parent>[\w&,\-/ ]{1,60}?)\s*(?:segment)?\)",
    re.I,
)
_FOOTNOTE_NAME = re.compile(
    r"^(?:\(\d{1,2}\)|<\d{1,2}[>J)]|\(\d{1,2}[>J]|\d{1,2}\))\s*(?P<name>[A-Z][\w&,\-/ ]{1,80}?)\s+"
    r"(?:include|includes|comprise|comprises|represent|represents|consist|consists)\b"
)


def _kind_text(text: str) -> bool:
    return not is_header_line(text) and caption_unit(text) is None


def entity_key(label: str) -> str:
    return name_key(_ENUMERATOR.sub("", clean_name(label)))


def entity_name(label: str) -> str:
    return _ENUMERATOR.sub("", clean_name(label))


def normalized(parsed: ParsedNumber, kind: ValueKind, scale: Scale) -> Decimal | None:
    if parsed.value is None:
        return None
    if kind == ValueKind.MONEY:
        if scale == Scale.UNKNOWN:
            return None
        return parsed.value * SCALE_FACTOR[scale] / SCALE_FACTOR[CANONICAL_SCALE]
    return parsed.value


def _period_kind(period: FinancialPeriod | None) -> KPIPeriodKind:
    if period is None or period.period_type == PeriodType.UNKNOWN:
        return KPIPeriodKind.UNKNOWN
    return {
        PeriodType.FY: KPIPeriodKind.FY,
        PeriodType.QUARTER: KPIPeriodKind.QUARTER,
        PeriodType.POINT_IN_TIME: KPIPeriodKind.POINT_IN_TIME,
    }.get(period.period_type, KPIPeriodKind.FLOW_PERIOD)


@dataclass
class Collected:
    segments: list[SegmentObservation] = field(default_factory=list)
    geographies: list[GeographyObservation] = field(default_factory=list)
    kpis: list[OperatingKPIObservation] = field(default_factory=list)
    projects: list[ProjectFact] = field(default_factory=list)
    evidence: list[BusinessEvidence] = field(default_factory=list)
    unmapped: list[UnmappedOperatingLine] = field(default_factory=list)
    warnings: list[BusinessWarning] = field(default_factory=list)
    frames: int = 0
    partial: bool = False
    documents: int = 0
    restated_frames: set[UUID] = field(default_factory=set)
    box4_considered: int = 0
    box4_accepted: int = 0

    def warn(self, code: str, severity: Severity, detail: str | None = None) -> None:
        self.warnings.append(BusinessWarning(code=code, severity=severity, detail=detail))


@dataclass(frozen=True)
class Candidate:
    index: int
    row: Row
    scope_geo: str | None
    scope_segment: str | None
    parts: LabelParts
    unit: UnitReading | None
    match: KPIMatch


@dataclass
class FrameContext:
    extraction: DocumentExtraction
    frame: Frame
    url: str
    basis: ReportingBasis
    basis_method: str
    unit: UnitReading | None
    section: SectionKind | None
    titles: list[str]
    statement_table: bool


class Extractor:
    def __init__(
        self,
        *,
        company_key: str,
        family: SectorFamily,
        fy_end: int | None,
        catalogue: EvidenceCatalogue,
        limits: BusinessLimits,
    ) -> None:
        self.company_key = company_key
        self.family = family
        self.fy_end = fy_end
        self.urls = {d.document_id: str(d.source_url) for d in catalogue.documents}
        self.limits = limits
        self.out = Collected()
        self.vocabulary: set[str] = set()
        self.blocks: dict[UUID, tuple[int, int]] = {}
        self._limit_hit: set[str] = set()

    # --- bookkeeping ---------------------------------------------------------------------

    def room(self, what: str, current: int, limit: int) -> bool:
        if current < limit:
            return True
        if what not in self._limit_hit:
            self._limit_hit.add(what)
            self.out.partial = True
            self.out.warn("RESOURCE_LIMIT_REACHED", Severity.MEDIUM, f"{what} limit {limit}")
        return False

    def location(
        self,
        ex: DocumentExtraction,
        line: Line,
        extra: list[Line] | None = None,
        row: int | None = None,
        column: int | None = None,
    ) -> SourceLocation:
        lines = [*(extra or []), line]
        blocks = tuple(dict.fromkeys(item.block_id for item in lines))
        start, end = self.blocks.get(line.block_id, (0, 0))
        locator = f"page:{line.page}/chars:{start}-{end}/line:{line.line_index}"
        if row is not None:
            locator += f"/r{row}"
        if column is not None:
            locator += f"/c{column}"
        return SourceLocation(
            source_kind=SourceKind.BOX3_BLOCK,
            document_id=ex.document_id,
            extraction_id=ex.extraction_id,
            document_type=ex.document_type,
            source_url=self.urls.get(ex.document_id, ex.artifact_reference),
            artifact_sha256=ex.artifact_sha256,
            authority=ex.authority,
            page_start=min(item.page for item in lines),
            page_end=max(item.page for item in lines),
            block_ids=blocks,
            table_id=line.table_id,
            line_index=line.line_index,
            row=row,
            column=column,
            locator=locator,
        )

    def unmapped(
        self,
        ctx: FrameContext | None,
        ex: DocumentExtraction,
        domain: Domain,
        label: str,
        values: list[str],
        reason: str,
        line: Line,
        context: str | None = None,
    ) -> None:
        if not self.room("unmapped", len(self.out.unmapped), self.limits.max_unmapped):
            return
        self.out.unmapped.append(
            UnmappedOperatingLine(
                line_id=uuid5(
                    NS, f"unmapped|{ex.extraction_id}|{line.block_id}|{line.line_index}|{reason}"
                ),
                company_key=self.company_key,
                domain=domain,
                raw_label=label[:1000] or "(unlabelled row)",
                raw_values=tuple(v[:100] for v in values[:40]),
                reason=reason,
                context=(context or (" / ".join(ctx.titles[-3:]) if ctx else None) or None),
                sector_family=self.family,
                document_type=ex.document_type,
                source=self.location(ex, line),
            )
        )

    # --- pipeline --------------------------------------------------------------------------

    def run(self, bundle: EvidenceBundle) -> Collected:
        usable = [
            e
            for e in sorted(bundle.extractions, key=lambda e: str(e.document_id))
            if e.status in (ExtractionStatus.SUCCESS, ExtractionStatus.PARTIAL)
        ]
        if len(usable) > self.limits.max_documents:
            self.out.partial = True
            self.out.warn(
                "RESOURCE_LIMIT_REACHED",
                Severity.MEDIUM,
                f"documents limit {self.limits.max_documents} of {len(usable)}",
            )
            usable = usable[: self.limits.max_documents]
        self.out.documents = len(usable)
        prepared: list[tuple[DocumentExtraction, dict[int, list[Line]], list[Frame]]] = []
        for ex in usable:
            self.blocks.update({b.block_id: (b.start, b.end) for b in ex.blocks})
            pages = page_lines(ex)
            frames: list[Frame] = []
            for page in sorted(pages):
                for frame in find_frames(ex, page, pages[page]):
                    if not self.room("tables", self.out.frames, self.limits.max_tables):
                        break
                    build_rows(frame)
                    frames.append(frame)
                    self.out.frames += 1
            prepared.append((ex, pages, frames))
            self.harvest_vocabulary(ex, pages, frames)
        for ex, pages, frames in prepared:
            for frame in frames:
                self.interpret(ex, pages, frame)
            self.projects(ex, pages)
            self.narrative(ex)
        return self.out

    # --- vocabulary (company-printed segment names) ---------------------------------------

    def harvest_vocabulary(
        self, ex: DocumentExtraction, pages: dict[int, list[Line]], frames: list[Frame]
    ) -> None:
        for page, lines in pages.items():
            in_segment_section = section_kind_at(ex, page) == SectionKind.SEGMENT_REPORTING
            collecting = False
            for line in lines:
                text = line.text
                match = _FOOTNOTE_NAME.match(text) or _DEFINITION_NAME.match(text)
                if match and (
                    in_segment_section or SEGMENT_CONTEXT.search(" ".join(x.text for x in lines))
                ):
                    self.vocabulary.add(entity_key(match.group("name")))
                metric = segment_metric_heading(text)
                if metric in (SegmentMetric.REVENUE, SegmentMetric.SEGMENT_RESULT) and (
                    in_segment_section or SEGMENT_CONTEXT.search(text)
                ):
                    collecting = True
                    continue
                if not collecting:
                    continue
                key = entity_key(text)
                if key.startswith("total") or is_header_line(text) or len(text.split()) > 8:
                    collecting = False
                    continue
                if re.search(r"\d", re.sub(r"[(<]\s*\d{1,2}\s*[)>J]", "", text)):
                    collecting = False
                    continue
                if key and not key.startswith(("less", "add")):
                    self.vocabulary.add(key)
        wanted = (SegmentMetric.REVENUE, SegmentMetric.SEGMENT_RESULT)
        for frame in frames:
            if frame.matrix or not self.explicit_segment_context(frame):
                continue
            nearest = next(
                (segment_metric_heading(line.text) for line in reversed(frame.context)
                 if segment_metric_heading(line.text) is not None),
                None,
            )  # fmt: skip
            for row in frame.rows:
                heading = segment_metric_heading(row.group) if row.group else nearest
                if (
                    row.label
                    and heading in wanted
                    and row_role(row.label, SegmentMetric.REVENUE, False) == SegmentRole.REPORTABLE
                ):
                    self.vocabulary.add(entity_key(row.label))

    # --- frame classification ---------------------------------------------------------------

    def context(
        self, ex: DocumentExtraction, pages: dict[int, list[Line]], frame: Frame
    ) -> FrameContext:
        titles = [
            line.text
            for line in [*frame.context, *frame.texts, *frame.trailing]
            if caption_unit(line.text) is None and not is_header_line(line.text)
        ]
        unit = next(
            (u for line in reversed(frame.context) if (u := caption_unit(line.text)) is not None),
            None,
        ) or next(
            (u for line in frame.trailing if (u := caption_unit(line.text)) is not None), None
        )
        basis = explicit_basis([*titles, *(line.text for line in frame.context)])
        method = "EXPLICIT_CAPTION"
        if basis is None:
            head = [line.text for line in pages.get(frame.page, [])[:6]]
            basis, method = explicit_basis(head), "PAGE_TITLE"
        if basis is None:
            basis, method = section_basis(ex, frame.page), "SECTION_SCOPE"
        if basis is None:
            basis, method = ReportingBasis.UNKNOWN, "UNKNOWN"
        tables = {t.table_id: t for t in ex.tables}
        statement = any(
            line.table_id in tables and tables[line.table_id].statement_kind in _STATEMENT_TABLES
            for line in frame.lines
        )
        return FrameContext(
            ex,
            frame,
            self.urls.get(ex.document_id, ex.artifact_reference),
            basis,
            method,
            unit,
            section_kind_at(ex, frame.page),
            titles,
            statement,
        )

    @staticmethod
    def explicit_segment_context(frame: Frame) -> bool:
        texts = [line.text for line in [*frame.context[-8:], *frame.texts]]
        return any(SEGMENT_CONTEXT.search(t) for t in texts)

    def is_segment_frame(self, ex: DocumentExtraction, frame: Frame) -> bool:
        """Segment wording around the table, a Box 3 SEGMENT_RESULTS table, or - inside a
        segment-reporting section - rows naming at least two company segments."""
        if self.explicit_segment_context(frame):
            return True
        tables = {t.table_id: t for t in ex.tables}
        if any(
            line.table_id in tables
            and tables[line.table_id].statement_kind == FinancialStatementKind.SEGMENT_RESULTS
            for line in frame.lines
        ):
            return True
        if section_kind_at(ex, frame.page) != SectionKind.SEGMENT_REPORTING:
            return False
        if frame.matrix:
            return True
        named = {entity_key(r.label) for r in frame.rows if r.label} & self.vocabulary
        return len(named) >= 2

    def is_geography_frame(self, frame: Frame) -> bool:
        texts = [line.text for line in [*frame.context[-6:], *frame.texts]]
        labelled = [r for r in frame.rows if r.label and r.continuation == 0]
        geos = [r for r in labelled if geography_dimension(entity_name(r.label)) is not None]
        real = [
            r
            for r in geos
            if geography_dimension(entity_name(r.label)) not in (GeographyDimension.TOTAL,)
        ]
        if any(GEOGRAPHY_CONTEXT.search(t) for t in texts):
            # geography wording plus at least one recognised geography (or no labels at
            # all - an unresolved layout is still reported as geography)
            return bool(real) or not labelled
        return len(real) >= 2 and len(geos) * 10 >= len(labelled) * 7

    def interpret(self, ex: DocumentExtraction, pages: dict[int, list[Line]], frame: Frame) -> None:
        ctx = self.context(ex, pages, frame)
        if ctx.statement_table:
            return  # Box 4 owns financial statements
        if self.is_geography_frame(frame):
            self.geography_frame(ctx)
        elif self.is_segment_frame(ex, frame) and (frame.matrix or self.has_segment_metric(ctx)):
            if not frame.matrix and self.metric_rows(frame) >= 2 and frame.columns >= 2:
                frame.matrix = True  # one line per metric across segment columns
            wide = [
                len(r.tokens)
                for r in frame.rows
                if r.label and len(r.tokens) >= 2 and segment_metric_heading(self.own_label(r))
            ]
            if not frame.matrix and frame.columns <= 1 and wide:
                # metric lines across segment columns, the rest of the table on the next page
                frame.columns, frame.matrix = max(wide), True
            if frame.matrix:
                self.matrix_frame(ctx, pages)
            else:
                self.segment_frame(ctx)
        else:
            self.kpi_frame(ctx)

    @staticmethod
    def own_label(row: Row) -> str:
        """The label printed on the row's own line (without wrapped lines above it)."""
        return trailing_values(row.line.text)[0]

    def metric_rows(self, frame: Frame) -> int:
        """Labelled rows that name a segment metric and carry every value column."""
        return sum(
            1
            for r in frame.rows
            if r.label
            and len(r.tokens) == frame.columns
            and (segment_metric_heading(r.label) or segment_metric_heading(self.own_label(r)))
        )

    def has_segment_metric(self, ctx: FrameContext) -> bool:
        """A row-layout segment table names its metric (title, sub-heading or row)."""
        texts = [
            *ctx.titles,
            *(r.group or "" for r in ctx.frame.rows),
            *(r.label for r in ctx.frame.rows),
        ]
        return any(segment_metric_heading(t) is not None for t in texts if t)

    # --- shared value handling ------------------------------------------------------------

    def periods(self, ctx: FrameContext, statement: StatementType) -> list[ColumnPeriod | None]:
        annual = ctx.extraction.document_type.value == "ANNUAL_REPORT"
        columns, _headers = frame_periods(
            ctx.frame, ctx.frame.columns, self.fy_end, statement, annual
        )
        return columns

    def value_kind(self, ctx: FrameContext, raw: str) -> tuple[ValueKind, Currency, Scale]:
        if raw.strip().endswith("%") or (ctx.unit is not None and ctx.unit.unit == KPIUnit.PERCENT):
            return ValueKind.PERCENT, Currency.UNKNOWN, Scale.ONES
        if ctx.unit is not None and ctx.unit.unit == KPIUnit.MONEY and ctx.unit.currency:
            return ValueKind.MONEY, ctx.unit.currency, ctx.unit.scale
        return ValueKind.UNKNOWN, Currency.UNKNOWN, Scale.UNKNOWN

    def confidence(self, warnings: list[str]) -> Confidence:
        low = {
            "UNKNOWN_PERIOD",
            "UNKNOWN_UNIT",
            "UNKNOWN_BASIS",
            "UNKNOWN_GEOGRAPHY",
            "UNKNOWN_KPI_PERIOD",
            "UNKNOWN_KPI_UNIT",
            "UNPARSEABLE_VALUE",
        }
        if any(w in low for w in warnings):
            return Confidence.LOW
        if warnings:
            return Confidence.MEDIUM
        return Confidence.HIGH

    def unresolved_rows(self, ctx: FrameContext, domain: Domain, reason: str) -> None:
        rows = [r for r in ctx.frame.rows if not r.label]
        if not rows:
            return
        values = [v for r in rows for v in r.tokens]
        self.unmapped(ctx, ctx.extraction, domain, "", values, reason, rows[0].line)

    # --- segments: row layout ---------------------------------------------------------------

    def segment_frame(self, ctx: FrameContext) -> None:
        frame, ex = ctx.frame, ctx.extraction
        count = frame.columns
        if count == 0:
            return
        if count == 1 and self.metric_rows(frame) >= 2:
            self.entity_metric_frame(ctx)
            return
        periods = self.periods(ctx, StatementType.SEGMENT_RESULTS)
        metric: SegmentMetric | None = None
        above = [line.text for line in frame.context]  # the heading nearest the first row
        for title in reversed(above):
            found = segment_metric_heading(title)
            if found is not None:
                metric = found
                break
        if (
            ctx.unit is not None
            and ctx.unit.unit == KPIUnit.PERCENT
            and metric == SegmentMetric.REVENUE
        ):
            metric = SegmentMetric.REVENUE_SHARE
        after_total = False
        group: str | None = None
        parents: list[str] = []
        for index, row in enumerate(frame.rows):
            if row.group != group:
                group = row.group
                if group is not None:
                    found = segment_metric_heading(group)
                    if found is not None:
                        metric = (
                            SegmentMetric.REVENUE_SHARE
                            if found == SegmentMetric.REVENUE
                            and ctx.unit is not None
                            and ctx.unit.unit == KPIUnit.PERCENT
                            else found
                        )
                        after_total, parents = False, []
                    elif metric is not None and not re.search(r"\d", group):
                        parents = [group]  # a parent segment printed above its parts
            if not row.label:
                continue
            heading = segment_metric_heading(row.label)
            if heading is not None and metric is None:
                metric = heading
            if metric is None:
                self.unmapped(
                    ctx,
                    ex,
                    Domain.SEGMENT,
                    row.label,
                    row.tokens,
                    "SEGMENT_METRIC_UNKNOWN",
                    row.line,
                )
                continue
            read_values(frame, row, count)
            if row.values is None:
                self.unmapped(
                    ctx,
                    ex,
                    Domain.SEGMENT,
                    row.label,
                    row.tokens,
                    "COLUMN_COUNT_MISMATCH",
                    row.line,
                )
                continue
            role = row_role(row.label, metric, after_total)
            parent_key: str | None = None
            level = SegmentLevel.PRIMARY
            key = entity_key(row.label)
            if parents and role == SegmentRole.REPORTABLE:
                parent_key, level, role = (
                    entity_key(parents[0]),
                    SegmentLevel.SUBSEGMENT,
                    (SegmentRole.SUBSEGMENT),
                )
            if role == SegmentRole.TOTAL:
                if parents and key in (
                    f"total {entity_key(parents[0])}",
                    f"{entity_key(parents[0])} total",
                ):
                    key, role, level = (
                        entity_key(parents[0]),
                        SegmentRole.REPORTABLE,
                        (SegmentLevel.PRIMARY),
                    )  # the parent's own line, not a child
                    parent_key, parents = None, []
                else:
                    key = "total"
                    after_total = True
            self.emit_segment(
                ctx,
                row,
                index,
                metric,
                role,
                key,
                entity_name(row.label),
                parent_key,
                level,
                periods,
            )

    def entity_metric_frame(self, ctx: FrameContext) -> None:
        """One segment, metrics as rows: 'Digital Banking (a sub-segment of Retail banking
        segment)' / 'Segment assets 15.90' / 'Result (16.88)'. The entity (and its parent,
        when the title states it) come from the title line; nothing is inferred."""
        frame, ex = ctx.frame, ctx.extraction
        title = next(
            (line.text for line in reversed(frame.context)
             if _kind_text(line.text) and _SUB_SEGMENT.match(line.text)),
            None,
        )  # fmt: skip
        match = _SUB_SEGMENT.match(title) if title else None
        if match is None:
            for row in frame.rows:
                if row.label:
                    self.unmapped(ctx, ex, Domain.SEGMENT, row.label, row.tokens,
                                  "SEGMENT_ENTITY_UNKNOWN", row.line)  # fmt: skip
            return
        name, parent = entity_name(match.group("name")), entity_key(match.group("parent"))
        periods = self.periods(ctx, StatementType.SEGMENT_RESULTS)
        for index, row in enumerate(frame.rows):
            metric = segment_metric_heading(row.label) or segment_metric_heading(
                self.own_label(row)
            )
            if not row.label or metric is None:
                continue
            read_values(frame, row, 1)
            if row.values is None:
                continue
            self.emit_segment(
                ctx, row, index, metric, SegmentRole.SUBSEGMENT, entity_key(name), name, parent,
                SegmentLevel.SUBSEGMENT, periods, raw_metric=row.label,
            )  # fmt: skip

    def emit_segment(
        self,
        ctx: FrameContext,
        row: Row,
        index: int,
        metric: SegmentMetric,
        role: SegmentRole,
        key: str,
        name: str,
        parent_key: str | None,
        level: SegmentLevel,
        periods: list[ColumnPeriod | None],
        columns: list[int] | None = None,
        extra_lines: list[Line] | None = None,
        method_warnings: tuple[str, ...] = (),
        raw_metric: str | None = None,
    ) -> None:
        ex = ctx.extraction
        assert row.values is not None
        targets = columns if columns is not None else list(range(len(row.values)))
        extras: list[str] = []
        for position, column in enumerate(targets):
            raw = row.values[position]
            printed = row.printed[position] if row.printed else raw
            period = periods[column] if column < len(periods) else None
            if period is None:
                extras.append(printed)
                continue
            if not self.room(
                "segment observations", len(self.out.segments), self.limits.max_segment_observations
            ):
                return
            parsed = parse_financial_number(raw)
            kind, currency, scale = self.value_kind(ctx, raw)
            effective = metric
            if kind == ValueKind.PERCENT and metric == SegmentMetric.REVENUE:
                effective = SegmentMetric.REVENUE_SHARE
            warnings = list(method_warnings)
            if printed != raw:
                warnings.append("SPLIT_NUMBER_REJOINED")
            if ctx.basis == ReportingBasis.UNKNOWN:
                warnings.append("UNKNOWN_BASIS")
            if kind == ValueKind.UNKNOWN:
                warnings.append("UNKNOWN_UNIT")
            if parsed.value is None and parsed.status.value == "UNPARSEABLE":
                warnings.append("UNPARSEABLE_VALUE")
            if period.restated:
                self.out.restated_frames.add(ctx.frame.frame_id)
            observation_id = uuid5(
                NS,
                f"segment|{ex.extraction_id}|{row.line.block_id}|{row.line.line_index}|"
                f"{column}|{effective.value}|{key}|{period.period.key()}",
            )
            self.out.segments.append(
                SegmentObservation(
                    observation_id=observation_id,
                    company_key=self.company_key,
                    segment_raw_name=name[:1000] or "(unnamed)",
                    segment_key=key[:300] or "unnamed",
                    segment_parent_key=parent_key,
                    segment_level=level,
                    segment_role=role,
                    metric=effective,
                    raw_metric_label=(raw_metric or row.group or metric.value)[:1000],
                    raw_value=printed,
                    parsed=parsed,
                    value_kind=kind,
                    normalized_value=normalized(parsed, kind, scale),
                    currency=currency,
                    scale=scale,
                    raw_unit=ctx.unit.raw if ctx.unit else None,
                    period=period.period,
                    reporting_basis=ctx.basis,
                    basis_method=ctx.basis_method,
                    audited=period.audited,
                    restated=period.restated,
                    frame_id=ctx.frame.frame_id,
                    source=self.location(
                        ex,
                        row.line,
                        extra_lines if extra_lines is not None else row.label_lines,
                        index,
                        column,
                    ),
                    confidence=self.confidence(warnings),
                    warnings=tuple(sorted(set(warnings))),
                    metadata={"layout": "MATRIX" if ctx.frame.matrix else "ROW"},
                )
            )
        if extras:
            self.unmapped(
                ctx,
                ex,
                Domain.SEGMENT,
                row.label or name,
                extras,
                "EXTRA_COLUMN_WITHOUT_PERIOD",
                row.line,
            )

    # --- segments: matrix layout ------------------------------------------------------------

    def matrix_frame(self, ctx: FrameContext, pages: dict[int, list[Line]]) -> None:
        frame, ex = ctx.frame, ctx.extraction
        words, header_lines = matrix_header(frame)
        names: list[str] | None = None
        leftover: list[Line] = []
        for cut in range(len(header_lines), 0, -1):
            cut_words = [w for line in header_lines[:cut] for w in line.text.split()]
            names = split_matrix_header(cut_words, frame.columns, self.vocabulary)
            if names is not None:
                leftover = header_lines[cut:]
                break
        if names is None:
            self.unmapped(
                ctx,
                ex,
                Domain.TABLE,
                " ".join(words)[:1000],
                [t for r in frame.rows for t in r.tokens][:40],
                "MATRIX_HEADER_UNRESOLVED",
                frame.lines[0],
            )
            return
        matrix_rows(frame)
        if leftover and frame.rows:
            first = frame.rows[0]
            first.label = " ".join([*(line.text for line in leftover), first.label]).strip()
            first.label_lines = [*leftover, *first.label_lines]
        sizes = Counter(
            1 + sum(1 for _ in self._followers(frame.rows, i))
            for i, r in enumerate(frame.rows)
            if r.label
        )
        group_size = max(sizes.items(), key=lambda kv: (kv[1], kv[0]))[0] if sizes else 1
        phrases = [
            line.text for line in [*frame.context, *frame.trailing] if is_period_phrase(line.text)
        ]
        method: tuple[str, ...] = ("MATRIX_HEADER_FROM_COMPANY_VOCABULARY",)
        if not phrases:
            # a phrase printed below the table (never a running header above it)
            last = frame.lines[-1].position
            below = [line for line in pages.get(frame.page, []) if line.position > last]
            found = page_period_phrases(below)
            if len(found) == 1:
                phrases, method = found, (*method, "PAGE_PERIOD_PHRASE")
        periods = (
            interpret_columns(phrases, group_size, StatementType.SEGMENT_RESULTS, self.fy_end)
            if phrases
            else []
        )
        if not periods or any(p.ambiguous for p in periods):
            self.unmapped(
                ctx,
                ex,
                Domain.SEGMENT,
                " | ".join(names),
                [],
                "MATRIX_PERIOD_UNRESOLVED",
                frame.lines[0],
            )
            return
        keys = [entity_key(n) for n in names]
        metric: SegmentMetric | None = None
        bridge_open = False
        for index, row in enumerate(frame.rows):
            if not row.label:
                continue
            group = [row, *self._followers(frame.rows, index)]
            if len(group) != group_size:
                self.unmapped(
                    ctx,
                    ex,
                    Domain.SEGMENT,
                    row.label,
                    row.tokens,
                    "MATRIX_PERIOD_COUNT_MISMATCH",
                    row.line,
                )
                continue
            heading = segment_metric_heading(row.label) or segment_metric_heading(
                self.own_label(row)
            )
            single = all(len(g.tokens) == 1 for g in group)
            for g in group:
                read_values(frame, g, 1 if single else frame.columns)
            if any(g.values is None for g in group):
                self.unmapped(
                    ctx,
                    ex,
                    Domain.SEGMENT,
                    row.label,
                    row.tokens,
                    "COLUMN_COUNT_MISMATCH",
                    row.line,
                )
                continue
            if not single:
                metric = heading or SegmentMetric.OTHER
                bridge_open = metric in _BRIDGED
                for offset, g in enumerate(group):
                    for column, (key, name) in enumerate(zip(keys, names, strict=True)):
                        role = SegmentRole.TOTAL if key == "total" else SegmentRole.REPORTABLE
                        self.emit_segment(
                            ctx,
                            self._single(g, column),
                            index + offset,
                            metric,
                            role,
                            key,
                            name,
                            None,
                            SegmentLevel.PRIMARY,
                            [periods[offset]],
                            [0],
                            row.label_lines if offset == 0 else [],
                            method,
                            row.label,
                        )
                continue
            role = row_role(row.label, SegmentMetric.SEGMENT_RESULT, True)
            bridge_metric = metric if bridge_open and metric is not None else SegmentMetric.OTHER
            if role == SegmentRole.REPORTABLE:
                role = SegmentRole.RECONCILING_ITEM
            if role == SegmentRole.TOTAL:
                role = SegmentRole.COMPANY_TOTAL  # 'Total assets' after unallocated items
            if role == SegmentRole.COMPANY_TOTAL and bridge_open:
                bridge_open = False
            elif role == SegmentRole.COMPANY_TOTAL:
                bridge_metric = SegmentMetric.OTHER
                role = SegmentRole.RECONCILING_ITEM
            for offset, g in enumerate(group):
                self.emit_segment(
                    ctx,
                    g,
                    index + offset,
                    bridge_metric,
                    role,
                    entity_key(row.label),
                    entity_name(row.label),
                    None,
                    SegmentLevel.PRIMARY,
                    [periods[offset]],
                    [0],
                    row.label_lines if offset == 0 else [],
                    method,
                    row.label,
                )
            if not bridge_open:
                metric = None

    @staticmethod
    def _followers(rows: list[Row], index: int) -> list[Row]:
        out: list[Row] = []
        for row in rows[index + 1 :]:
            if row.label:
                break
            out.append(row)
        return out

    @staticmethod
    def _single(row: Row, column: int) -> Row:
        assert row.values is not None
        return Row(
            row.label,
            row.tokens,
            row.line,
            row.label_lines,
            row.group,
            row.group_line,
            row.parents,
            row.continuation,
            [row.values[column]],
            row.rejoined,
            [row.printed[column]] if row.printed else None,
        )

    # --- geography ------------------------------------------------------------------------

    def geography_frame(self, ctx: FrameContext) -> None:
        frame, ex = ctx.frame, ctx.extraction
        count = frame.columns
        if count == 0:
            return
        if any(not r.label for r in frame.rows):
            self.unresolved_rows(ctx, Domain.GEOGRAPHY, "LABEL_VALUE_ALIGNMENT_UNRESOLVED")
        periods = self.periods(ctx, StatementType.SEGMENT_RESULTS)
        page_notes = [line.text for line in frame.trailing] + ctx.titles
        method, method_text = attribution(page_notes)
        context_text = " ".join(ctx.titles).lower()
        for index, row in enumerate(frame.rows):
            if not row.label:
                continue
            name = entity_name(row.label)
            dimension = geography_dimension(name)
            if dimension is None and (
                GEOGRAPHY_CONTEXT.search(name) or name_key(name) in ("particulars",)
            ):
                continue
            read_values(frame, row, count)
            if row.values is None:
                self.unmapped(
                    ctx,
                    ex,
                    Domain.GEOGRAPHY,
                    row.label,
                    row.tokens,
                    "COLUMN_COUNT_MISMATCH",
                    row.line,
                )
                continue
            extras: list[str] = []
            for column, raw in enumerate(row.values):
                printed = row.printed[column] if row.printed else raw
                period = periods[column] if column < len(periods) else None
                if period is None:
                    extras.append(printed)
                    continue
                if not self.room(
                    "geography observations",
                    len(self.out.geographies),
                    self.limits.max_geography_observations,
                ):
                    return
                parsed = parse_financial_number(raw)
                kind, currency, scale = self.value_kind(ctx, raw)
                if kind == ValueKind.PERCENT:
                    metric = GeographyMetric.REVENUE_SHARE
                elif "asset" in context_text:
                    metric = GeographyMetric.ASSETS
                elif re.search(r"revenue|sales|income|turnover", context_text) or (
                    kind == ValueKind.MONEY
                ):
                    metric = GeographyMetric.REVENUE
                else:
                    metric = GeographyMetric.OTHER
                warnings: list[str] = []
                if dimension is None:
                    warnings.append("UNKNOWN_GEOGRAPHY")
                if method.value == "UNKNOWN":
                    warnings.append("GEOGRAPHY_ATTRIBUTION_UNKNOWN")
                if ctx.basis == ReportingBasis.UNKNOWN:
                    warnings.append("UNKNOWN_BASIS")
                if kind == ValueKind.UNKNOWN:
                    warnings.append("UNKNOWN_UNIT")
                if printed != raw:
                    warnings.append("SPLIT_NUMBER_REJOINED")
                key = entity_key(name)
                self.out.geographies.append(
                    GeographyObservation(
                        observation_id=uuid5(
                            NS,
                            f"geo|{ex.extraction_id}|{row.line.block_id}|{row.line.line_index}|"
                            f"{column}|{metric.value}|{key}|{period.period.key()}",
                        ),
                        company_key=self.company_key,
                        geography_raw_name=name[:1000],
                        geography_key=key[:300] or "unnamed",
                        dimension=dimension or GeographyDimension.UNKNOWN,
                        attribution=method,
                        attribution_text=method_text,
                        metric=metric,
                        raw_metric_label=(
                            row.group or (ctx.titles[-1] if ctx.titles else metric.value)
                        )[:1000],
                        raw_value=printed,
                        parsed=parsed,
                        value_kind=kind,
                        normalized_value=normalized(parsed, kind, scale),
                        currency=currency,
                        scale=scale,
                        raw_unit=ctx.unit.raw if ctx.unit else None,
                        period=period.period,
                        reporting_basis=ctx.basis,
                        basis_method=ctx.basis_method,
                        is_total=dimension == GeographyDimension.TOTAL,
                        frame_id=frame.frame_id,
                        source=self.location(ex, row.line, row.label_lines, index, column),
                        confidence=self.confidence(
                            [w for w in warnings if w != "GEOGRAPHY_ATTRIBUTION_UNKNOWN"]
                        ),
                        warnings=tuple(sorted(set(warnings))),
                    )
                )
            if extras:
                self.unmapped(
                    ctx,
                    ex,
                    Domain.GEOGRAPHY,
                    row.label,
                    extras,
                    "EXTRA_COLUMN_WITHOUT_PERIOD",
                    row.line,
                )

    # --- operating KPIs ----------------------------------------------------------------------

    def kpi_frame(self, ctx: FrameContext) -> None:
        frame, ex = ctx.frame, ctx.extraction
        count = frame.columns
        if count == 0:
            return
        periods = self.periods(ctx, StatementType.QUARTERLY_RESULTS)
        family_title = _FAMILY_TITLE.get(self.family)
        operational = any(
            _OPERATIONAL_TITLE.search(t) or (family_title is not None and family_title.search(t))
            for t in ctx.titles[-6:]
        )
        candidates: list[Candidate] = []
        for index, row in enumerate(frame.rows):
            if not row.label:
                continue
            label, scope_geo, scope_segment = self.scope(row)
            parts = split_label(label)
            unit = parts.unit
            if unit is None and (parts.percent_sign or all(t.endswith("%") for t in row.tokens)):
                unit = UnitReading(KPIUnit.PERCENT, Scale.ONES, None, "%")
            match = match_kpi(parts.key, self.family, unit.unit if unit else None)
            if unit is None and ctx.unit is not None:
                # a table caption ('Rs. in crore') types only the rows it can apply to
                accepts = match.definition is None or ctx.unit.unit in match.definition.units
                if accepts:
                    unit = ctx.unit
                    match = match_kpi(parts.key, self.family, unit.unit)
            candidates.append(Candidate(index, row, scope_geo, scope_segment, parts, unit, match))
        mapped = {
            (c.match.definition.code, qualifier(c.match.definition.code, c.parts.key))
            for c in candidates
            if c.match.definition is not None
        }
        refused = {c.parts.key for c in candidates if c.match.reason == "SECTOR_NOT_APPLICABLE"}
        if not operational and len(mapped) + len(refused) < 2:
            return  # not an operating table (one stray alias is not enough)
        if any(not r.label for r in frame.rows):
            self.unresolved_rows(ctx, Domain.KPI, "LABEL_VALUE_ALIGNMENT_UNRESOLVED")
        for c in candidates:
            row, unit, definition = c.row, c.unit, c.match.definition
            if definition is None:
                if len(row.label.split()) > 14:
                    continue  # prose caught in a table run, not a KPI label
                reason = c.match.reason
                if reason == "UNKNOWN_KPI" and _FINANCIAL_LABEL.search(row.label):
                    reason = "BOX4_FINANCIAL_METRIC"  # Box 4 / 5 own company financials
                self.unmapped(ctx, ex, Domain.KPI, row.label, row.tokens, reason, row.line)
                continue
            implied = unit is None and definition.implied_unit is not None
            if unit is None and definition.implied_unit is not None:
                unit = UnitReading(definition.implied_unit, Scale.ONES, None, "")
            if unit is not None and unit.unit not in definition.units:
                self.unmapped(
                    ctx, ex, Domain.KPI, row.label, row.tokens, "UNIT_INCOMPATIBLE", row.line
                )
                continue
            read_values(frame, row, count)
            if row.values is None:
                self.unmapped(
                    ctx, ex, Domain.KPI, row.label, row.tokens, "COLUMN_COUNT_MISMATCH", row.line
                )
                continue
            self.emit_kpi(
                ctx,
                c.index,
                row,
                definition,
                c.parts,
                unit,
                implied,
                periods,
                c.scope_geo,
                c.scope_segment,
            )

    def scope(self, row: Row) -> tuple[str, str | None, str | None]:
        """(label without scope suffix, geography scope, segment scope)."""
        label = row.label
        geo = segment = None
        parts = re.split(r"\s+[-–]\s+", clean_name(label))
        if len(parts) == 2:
            tail = re.sub(r"\([^()]*\)", " ", parts[1]).strip()
            units = " ".join(re.findall(r"\([^()]*\)", parts[1]))
            if geography_dimension(tail) is not None:
                label, geo = f"{parts[0]} {units}".strip(), tail
            elif entity_key(tail) in self.vocabulary:
                label, segment = f"{parts[0]} {units}".strip(), tail
        if row.group:
            if geo is None and geography_dimension(row.group) is not None:
                geo = clean_name(row.group)
            elif segment is None and entity_key(row.group) in self.vocabulary:
                segment = clean_name(row.group)
        return label, geo, segment

    def emit_kpi(
        self,
        ctx: FrameContext,
        index: int,
        row: Row,
        definition: KPIDefinition,
        parts: LabelParts,
        unit: UnitReading | None,
        implied: bool,
        periods: list[ColumnPeriod | None],
        scope_geo: str | None,
        scope_segment: str | None,
    ) -> None:
        ex = ctx.extraction
        assert row.values is not None
        no_periods = all(p is None for p in periods)
        extras: list[str] = []
        for column, raw in enumerate(row.values):
            printed = row.printed[column] if row.printed else raw
            period = periods[column] if column < len(periods) else None
            if period is None and not (no_periods and column == 0):
                extras.append(printed)
                continue
            if not self.room(
                "KPI observations", len(self.out.kpis), self.limits.max_kpi_observations
            ):
                return
            parsed = parse_financial_number(raw)
            warnings: list[str] = []
            if printed != raw:
                warnings.append("SPLIT_NUMBER_REJOINED")
            if implied:
                warnings.append("UNIT_FROM_DEFINITION")
            kpi_unit = unit.unit if unit else KPIUnit.UNKNOWN
            if kpi_unit == KPIUnit.UNKNOWN:
                warnings.append("UNKNOWN_KPI_UNIT")
            fp = period.period if period else None
            period_kind = definition.period_kind
            as_of: date | None = None
            if fp is None:
                warnings.append("UNKNOWN_KPI_PERIOD")
                period_kind = KPIPeriodKind.UNKNOWN
            elif definition.period_kind == KPIPeriodKind.POINT_IN_TIME:
                as_of = fp.period_end
                if as_of is None:
                    warnings.append("UNKNOWN_KPI_PERIOD")
            else:
                period_kind = _period_kind(fp)
                if period_kind in (KPIPeriodKind.POINT_IN_TIME, KPIPeriodKind.UNKNOWN):
                    warnings.append("UNKNOWN_KPI_PERIOD")  # a flow KPI needs a duration
            scale = unit.scale if unit else Scale.UNKNOWN
            currency = unit.currency if unit else None
            if kpi_unit == KPIUnit.MONEY and (currency is None or scale == Scale.UNKNOWN):
                warnings.append("UNKNOWN_KPI_UNIT")
            value = parsed.value
            norm: Decimal | None = None
            if value is not None:
                if kpi_unit == KPIUnit.MONEY:
                    norm = value * SCALE_FACTOR[scale] / SCALE_FACTOR[CANONICAL_SCALE]
                elif kpi_unit not in (
                    KPIUnit.PERCENT,
                    KPIUnit.BASIS_POINTS,
                    KPIUnit.MONEY_PER_UNIT,
                ):
                    norm = (
                        value * SCALE_FACTOR.get(scale, Decimal(1))
                        if scale != Scale.UNKNOWN
                        else value
                    )
                else:
                    norm = value
            key = parts.key
            qual = qualifier(definition.code, key)
            observation_id = uuid5(
                NS,
                f"kpi|{ex.extraction_id}|{row.line.block_id}|{row.line.line_index}|{column}|"
                f"{definition.code}|{qual}|{fp.key() if fp else 'none'}",
            )
            self.out.kpis.append(
                OperatingKPIObservation(
                    kpi_observation_id=observation_id,
                    company_key=self.company_key,
                    canonical_kpi=definition.code,
                    kpi_family=(
                        SectorFamily(definition.family)
                        if definition.family in SectorFamily.__members__
                        else SectorFamily.GENERAL
                    ),
                    raw_label=row.label[:1000],
                    qualifier=qual,
                    raw_value=printed,
                    parsed=parsed,
                    unit=kpi_unit,
                    raw_unit=(unit.raw or None) if unit else None,
                    currency=currency,
                    scale=scale,
                    normalized_value=norm,
                    period=fp,
                    period_kind=period_kind,
                    as_of=as_of,
                    reporting_basis=ctx.basis,
                    segment_key=entity_key(scope_segment) if scope_segment else None,
                    segment_raw_name=scope_segment,
                    geography_key=entity_key(scope_geo) if scope_geo else None,
                    geography_raw_name=scope_geo,
                    definition_text="; ".join(parts.definitions)[:1000] or None,
                    frame_id=ctx.frame.frame_id,
                    source=self.location(ex, row.line, row.label_lines, index, column),
                    confidence=self.confidence(warnings),
                    warnings=tuple(sorted(set(warnings))),
                    metadata={"registry_family": definition.family},
                )
            )
        if extras:
            self.unmapped(
                ctx, ex, Domain.KPI, row.label, extras, "EXTRA_COLUMN_WITHOUT_PERIOD", row.line
            )

    # --- projects ------------------------------------------------------------------------

    _KV = re.compile(r"^(?P<key>[A-Za-z][A-Za-z /&()'.-]{1,40}?)\s*[:–-]\s+(?P<value>\S.{0,300})$")
    _PROJECT_KEYS = {
        "name": ("project", "project name", "name of the project"),
        "location": ("location", "site", "state", "plant location"),
        "segment": ("segment", "business", "business segment", "division"),
        "project_type": ("type", "project type", "nature", "nature of project"),
        "capacity": (
            "capacity",
            "capacity addition",
            "additional capacity",
            "proposed capacity",
            "capacity added",
        ),
        "announced": (
            "announced",
            "announcement date",
            "board approval",
            "approved on",
            "date of announcement",
        ),
        "expected": (
            "expected completion",
            "scheduled completion",
            "target completion",
            "expected commissioning",
            "scheduled commissioning",
            "cod",
            "expected cod",
            "commissioning timeline",
        ),
        "actual": (
            "actual completion",
            "completion date",
            "commissioned on",
            "commissioning date",
            "date of commissioning",
        ),
        "capex": (
            "capex",
            "project cost",
            "estimated cost",
            "investment",
            "capital outlay",
            "estimated capex",
            "total capex",
        ),
        "status": ("status", "current status", "project status"),
    }
    _STATUS = (
        (
            re.compile(
                r"under (?:construction|implementation|execution)|in progress|ongoing", re.I
            ),
            ProjectStatus.UNDER_CONSTRUCTION,
        ),
        (re.compile(r"\bcommissioned\b|\boperational\b", re.I), ProjectStatus.COMMISSIONED),
        (re.compile(r"\bcompleted\b", re.I), ProjectStatus.COMPLETED),
        (re.compile(r"on hold|deferred|suspended", re.I), ProjectStatus.ON_HOLD),
        (re.compile(r"\bplanned\b|\bproposed\b", re.I), ProjectStatus.PLANNED),
        (re.compile(r"\bannounced\b|approved by (?:the )?board", re.I), ProjectStatus.ANNOUNCED),
    )

    def projects(self, ex: DocumentExtraction, pages: dict[int, list[Line]]) -> None:
        lookup = {
            alias: field for field, aliases in self._PROJECT_KEYS.items() for alias in aliases
        }
        for page in sorted(pages):
            lines = pages[page]
            i = 0
            while i < len(lines):
                match = self._KV.match(lines[i].text)
                if not match or lookup.get(match.group("key").strip().lower()) != "name":
                    i += 1
                    continue
                fields: list[tuple[str, str]] = []
                used: list[Line] = []
                j = i
                while j < len(lines):
                    m = self._KV.match(lines[j].text)
                    if not m:
                        break
                    key = m.group("key").strip()
                    if j > i and lookup.get(key.lower()) == "name":
                        break
                    fields.append((key, m.group("value").strip()))
                    used.append(lines[j])
                    j += 1
                i = j
                if len(fields) < 2:
                    continue
                if not self.room(
                    "project facts", len(self.out.projects), self.limits.max_project_facts
                ):
                    return
                self.out.projects.append(self.project(ex, fields, used, lookup))

    def project(
        self,
        ex: DocumentExtraction,
        fields: list[tuple[str, str]],
        used: list[Line],
        lookup: dict[str, str],
    ) -> ProjectFact:
        found: dict[str, str] = {}
        for key, value in fields:
            name = lookup.get(key.lower())
            if name and name not in found:
                found[name] = value
        capacity_raw = found.get("capacity")
        capacity_value: Decimal | None = None
        capacity_unit: KPIUnit | None = None
        if capacity_raw:
            m = re.match(r"^\s*([\d,]+(?:\.\d+)?)\s*(.*)$", capacity_raw)
            if m:
                parsed = parse_financial_number(m.group(1))
                reading = read_unit(m.group(2)) if m.group(2) else None
                if parsed.value is not None and reading is not None:
                    capacity_value, capacity_unit = parsed.value, reading.unit
        capex_raw = found.get("capex")
        capex_value = capex_norm = None
        capex_currency: Currency | None = None
        capex_scale: Scale | None = None
        if capex_raw:
            m = re.match(
                r"^\s*(₹|rs\.?|inr|us\$|usd|\$)?\s*([\d,]+(?:\.\d+)?)\s*(.*)$", capex_raw, re.I
            )
            if m:
                parsed = parse_financial_number(m.group(2))
                reading = read_unit(f"{m.group(1) or ''} {m.group(3)}".strip())
                if parsed.value is not None and reading is not None and reading.currency:
                    capex_value, capex_currency, capex_scale = (
                        parsed.value,
                        reading.currency,
                        reading.scale,
                    )
                    capex_norm = (
                        parsed.value * SCALE_FACTOR[reading.scale] / SCALE_FACTOR[CANONICAL_SCALE]
                    )
        raw_status = found.get("status")
        status = next(
            (s for pattern, s in self._STATUS if raw_status and pattern.search(raw_status)),
            ProjectStatus.UNKNOWN,
        )
        warnings = [] if capex_raw is None or capex_value is not None else ["CAPEX_UNPARSED"]
        return ProjectFact(
            project_id=uuid5(
                NS, f"project|{ex.extraction_id}|{used[0].block_id}|{used[0].line_index}"
            ),
            name=found.get("name", "")[:1000],
            segment=found.get("segment"),
            location=found.get("location"),
            project_type=found.get("project_type"),
            capacity_addition_raw=capacity_raw,
            capacity_addition=capacity_value,
            capacity_unit=capacity_unit,
            announced_raw=found.get("announced"),
            expected_completion_raw=found.get("expected"),
            actual_completion_raw=found.get("actual"),
            capex_raw=capex_raw,
            capex_value=capex_value,
            capex_currency=capex_currency,
            capex_scale=capex_scale,
            capex_normalized=capex_norm,
            status=status,
            raw_status=raw_status,
            fields=tuple((k[:1000], v[:1000]) for k, v in fields),
            source=self.location(ex, used[-1], used[:-1]),
            confidence=Confidence.MEDIUM if warnings else Confidence.HIGH,
        )

    # --- narrative evidence --------------------------------------------------------------

    _BOX3_KINDS = {
        EvidenceKind.SEGMENT_DISCLOSURE: BusinessEvidenceKind.SEGMENT_DESCRIPTION,
        EvidenceKind.GEOGRAPHIC_DISCLOSURE: BusinessEvidenceKind.GEOGRAPHY_DESCRIPTION,
        EvidenceKind.CAPACITY_DISCLOSURE: BusinessEvidenceKind.CAPACITY,
        EvidenceKind.ORDER_BOOK_DISCLOSURE: BusinessEvidenceKind.ORDER_BOOK,
        EvidenceKind.CAPEX_DISCLOSURE: BusinessEvidenceKind.CAPEX,
        EvidenceKind.OPERATIONAL_KPI: BusinessEvidenceKind.OTHER,
    }
    _BOX3_KEYWORDS = {
        EvidenceKind.SEGMENT_DISCLOSURE: re.compile(r"segment", re.I),
        EvidenceKind.GEOGRAPHIC_DISCLOSURE: re.compile(r"geograph|region|countr", re.I),
        EvidenceKind.CAPACITY_DISCLOSURE: re.compile(r"capacit", re.I),
        EvidenceKind.ORDER_BOOK_DISCLOSURE: re.compile(r"order", re.I),
        EvidenceKind.CAPEX_DISCLOSURE: re.compile(r"cap(?:ital)? ?ex|capital expenditure", re.I),
        EvidenceKind.OPERATIONAL_KPI: re.compile(r"(?!)"),  # never a default kind
    }
    _SPECIFIC_SECTIONS = {
        SectionKind.MANAGEMENT_DISCUSSION,
        SectionKind.SEGMENT_REPORTING,
        SectionKind.REVENUE,
        SectionKind.DIRECTORS_REPORT,
    }

    def classify_text(
        self, text: str, default: BusinessEvidenceKind | None, relevant: bool
    ) -> tuple[BusinessEvidenceKind, bool, str] | None:
        for kind, pattern, specific in EVIDENCE_PATTERNS:
            m = pattern.search(text)
            if m and (specific or relevant):
                return kind, specific, m.group(0)
        if default is not None:
            return default, False, ""
        return None

    def narrative(self, ex: DocumentExtraction) -> None:
        seen: set[tuple[UUID, str]] = set()
        for item in sorted(ex.evidence, key=lambda e: str(e.evidence_id)):
            if item.table_id is not None or item.kind not in self._BOX3_KINDS:
                continue
            default: BusinessEvidenceKind | None = self._BOX3_KINDS[item.kind]
            keyword = self._BOX3_KEYWORDS.get(item.kind)
            if keyword is not None and not keyword.search(item.raw_text):
                default = None  # Box 3 tagged the page, but this excerpt says nothing of it
            found = self.classify_text(item.raw_text, default, True)
            if found is None:
                continue
            kind, specific, matched = found
            key = (item.block_ids[0] if item.block_ids else item.evidence_id, kind.value)
            if key in seen:
                continue
            seen.add(key)
            self.add_evidence(ex, item, kind, specific, matched)
        blocks = sorted(ex.blocks, key=lambda b: b.order)
        for block in blocks:
            if block.block_type.value not in ("PARAGRAPH", "LIST") or block.is_repeated_header:
                continue
            text = block.normalized_text
            if len(text) < 40:
                continue
            section = section_kind_at(ex, block.page_number)
            relevant = section in self._SPECIFIC_SECTIONS or (
                ex.document_type.value in ("INVESTOR_PRESENTATION", "CREDIT_RATING_REPORT")
            )
            found = self.classify_text(text, None, relevant)
            if found is None:
                continue
            kind, specific, matched = found
            if (block.block_id, kind.value) in seen:
                continue
            seen.add((block.block_id, kind.value))
            self.add_block_evidence(ex, block, kind, specific, matched, section)

    def add_evidence(
        self,
        ex: DocumentExtraction,
        item: EvidenceItem,
        kind: BusinessEvidenceKind,
        specific: bool,
        matched: str,
    ) -> None:
        if not self.room(
            "business evidence", len(self.out.evidence), self.limits.max_business_evidence
        ):
            return
        self.out.evidence.append(
            BusinessEvidence(
                evidence_id=uuid5(NS, f"evidence|{item.evidence_id}|{kind.value}"),
                company_key=self.company_key,
                evidence_kind=kind,
                raw_text=item.raw_text[:4000],
                normalized_text=" ".join(item.normalized_text.split())[:4000],
                section=item.section_kind.value if item.section_kind else None,
                source=SourceLocation(
                    source_kind=SourceKind.BOX3_EVIDENCE,
                    document_id=item.document_id,
                    extraction_id=ex.extraction_id,
                    document_type=item.document_type,
                    source_url=self.urls.get(item.document_id, ex.artifact_reference),
                    artifact_sha256=item.artifact_sha256,
                    authority=item.authority,
                    page_start=item.page_start,
                    page_end=item.page_end,
                    block_ids=item.block_ids,
                    table_id=item.table_id,
                    box3_evidence_id=item.evidence_id,
                    locator=item.locator,
                ),
                confidence=Confidence.MEDIUM if specific else Confidence.LOW,
                tags=tuple(sorted({kind.value, f"BOX3_{item.kind.value}"})),
                metadata={"matched": matched[:200]} if matched else {},
            )
        )

    def add_block_evidence(
        self,
        ex: DocumentExtraction,
        block: object,
        kind: BusinessEvidenceKind,
        specific: bool,
        matched: str,
        section: SectionKind | None,
    ) -> None:
        from stock_research_ai.domain.document_intelligence import TextBlock

        assert isinstance(block, TextBlock)
        if not self.room(
            "business evidence", len(self.out.evidence), self.limits.max_business_evidence
        ):
            return
        tables = {b: t.table_id for t in ex.tables for b in t.block_ids}
        self.out.evidence.append(
            BusinessEvidence(
                evidence_id=uuid5(NS, f"evidence|block|{block.block_id}|{kind.value}"),
                company_key=self.company_key,
                evidence_kind=kind,
                raw_text=block.normalized_text[:4000],
                normalized_text=" ".join(block.normalized_text.split())[:4000],
                section=section.value if section else None,
                source=SourceLocation(
                    source_kind=SourceKind.BOX3_BLOCK,
                    document_id=ex.document_id,
                    extraction_id=ex.extraction_id,
                    document_type=ex.document_type,
                    source_url=self.urls.get(ex.document_id, ex.artifact_reference),
                    artifact_sha256=ex.artifact_sha256,
                    authority=ex.authority,
                    page_start=block.page_number,
                    page_end=block.page_number,
                    block_ids=(block.block_id,),
                    table_id=tables.get(block.block_id),
                    locator=block.locator,
                ),
                confidence=Confidence.MEDIUM if specific else Confidence.LOW,
                tags=(kind.value,) if specific else (kind.value, "KEYWORD_ONLY"),
                metadata={"matched": matched[:200]},
            )
        )


# --- Box 4 segment observations --------------------------------------------------------------


def box4_segments(
    box4: CanonicalFinancialDataset, extractor: Extractor
) -> list[SegmentObservation]:
    """Box 4 keeps segment-table cells as SEGMENT_RESULTS observations (metric None,
    segment = row label). They are accepted only when fully interpreted (numeric, known
    period, basis and unit, a labelled segment under a recognised segment metric heading)
    and when Box 7's own reading of the same page does not already hold the same value."""
    out: list[SegmentObservation] = []
    own = {
        (o.source.document_id, o.source.page_start, o.period.key(), o.normalized_value)
        for o in extractor.out.segments
    }
    for o in box4.observations:
        if o.statement_type != StatementType.SEGMENT_RESULTS:
            continue
        extractor.out.box4_considered += 1
        limit = extractor.limits.max_segment_observations
        if not extractor.room("segment observations", len(extractor.out.segments) + len(out),
                              limit):  # fmt: skip
            continue
        metric = segment_metric_heading(o.label_context or "") if o.label_context else None
        if (
            o.segment is None
            or o.normalized_value is None
            or o.reporting_basis == ReportingBasis.UNKNOWN
            or o.period.period_type == PeriodType.UNKNOWN
            or o.value_kind != ValueKind.MONEY
            or metric is None
            or o.confidence == Confidence.LOW
        ):
            continue
        if (o.source.document_id, o.source.page_number, o.period.key(), o.normalized_value) in own:
            continue
        role = row_role(o.segment, metric, False)
        key = "total" if role == SegmentRole.TOTAL else entity_key(o.segment)
        src = o.source
        out.append(
            SegmentObservation(
                observation_id=uuid5(NS, f"box4-segment|{o.observation_id}"),
                company_key=o.company_key,
                segment_raw_name=o.segment[:1000],
                segment_key=key[:300] or "unnamed",
                segment_level=SegmentLevel.PRIMARY,
                segment_role=role,
                metric=metric if metric in SUMMABLE or metric == SegmentMetric.OTHER else metric,
                raw_metric_label=o.label_context or "",
                raw_value=o.raw_value,
                parsed=o.parsed,
                value_kind=o.value_kind,
                normalized_value=o.normalized_value,
                currency=o.currency,
                scale=o.scale,
                raw_unit=o.raw_unit_caption,
                period=o.period,
                reporting_basis=o.reporting_basis,
                basis_method="BOX4",
                audited=o.audited,
                restated=bool(o.restated),
                frame_id=uuid5(NS, f"box4-table|{src.table_id}"),
                source=SourceLocation(
                    source_kind=SourceKind.BOX4_OBSERVATION,
                    document_id=src.document_id,
                    extraction_id=src.extraction_id,
                    document_type=None,
                    source_url=src.source_url,
                    artifact_sha256=src.artifact_sha256 or "0" * 64,
                    authority=o.authority,
                    page_start=src.page_number,
                    page_end=src.page_number,
                    table_id=src.table_id,
                    row=src.row_index,
                    column=src.column_index,
                    box4_observation_id=o.observation_id,
                    locator=src.locator or "box4",
                ),
                confidence=o.confidence,
                warnings=tuple(sorted({*o.warnings, "FROM_BOX4"})),
            )
        )
        extractor.out.box4_accepted += 1
    return out


def heading_of(label: str) -> str:
    return heading_key(label)
