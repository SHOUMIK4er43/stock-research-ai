"""DETAILED_RESEARCH_V1 Box 3: deterministic, page-aware evidence extracted from documents.

Contracts only. Raw extracted text is the source of truth and lives once, on the page;
blocks point into it by character offsets and carry a normalized copy for research use.
Every table, section and evidence item points back to pages and blocks, and every
extraction points back to the exact stored artifact (SHA-256) it was read from. IDs are
derived from content and parser version, never random.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.evidence import EvidenceDocumentType, SourceAuthority

EVIDENCE_BUNDLE_SCHEMA = "EVIDENCE_BUNDLE_V1"

Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Label = Annotated[str, StringConstraints(max_length=500)]
Excerpt = Annotated[str, StringConstraints(max_length=4000)]


class DocumentFormat(StrEnum):
    PDF = "PDF"
    HTML = "HTML"
    TEXT = "TEXT"
    SPREADSHEET = "SPREADSHEET"


class ExtractionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"  # usable, with explicit truncation or degraded parts
    UNSUPPORTED = "UNSUPPORTED"
    ENCRYPTED = "ENCRYPTED"
    NO_TEXT = "NO_TEXT"  # e.g. scanned/image-only: OCR required, not attempted
    MALFORMED = "MALFORMED"
    TOO_LARGE = "TOO_LARGE"
    FAILED = "FAILED"


class BlockType(StrEnum):
    TITLE = "TITLE"
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST = "LIST"
    FOOTNOTE = "FOOTNOTE"
    HEADER = "HEADER"
    FOOTER = "FOOTER"
    TABLE_CAPTION = "TABLE_CAPTION"
    TABLE_ROW = "TABLE_ROW"
    UTTERANCE = "UTTERANCE"  # one speaker turn in a transcript
    OTHER = "OTHER"


class SectionKind(StrEnum):
    # Annual report
    CHAIRMAN_MESSAGE = "CHAIRMAN_MESSAGE"
    CEO_MESSAGE = "CEO_MESSAGE"
    MANAGEMENT_DISCUSSION = "MANAGEMENT_DISCUSSION"
    DIRECTORS_REPORT = "DIRECTORS_REPORT"
    CORPORATE_GOVERNANCE = "CORPORATE_GOVERNANCE"
    REMUNERATION = "REMUNERATION"
    BRSR = "BRSR"
    AUDITOR_REPORT = "AUDITOR_REPORT"
    STANDALONE_FINANCIAL_STATEMENTS = "STANDALONE_FINANCIAL_STATEMENTS"
    CONSOLIDATED_FINANCIAL_STATEMENTS = "CONSOLIDATED_FINANCIAL_STATEMENTS"
    NOTES_TO_ACCOUNTS = "NOTES_TO_ACCOUNTS"
    AGM_NOTICE = "AGM_NOTICE"
    # Financial notes
    SEGMENT_REPORTING = "SEGMENT_REPORTING"
    REVENUE = "REVENUE"
    EMPLOYEE_COSTS = "EMPLOYEE_COSTS"
    FINANCE_COSTS = "FINANCE_COSTS"
    DEPRECIATION = "DEPRECIATION"
    OTHER_INCOME = "OTHER_INCOME"
    EXCEPTIONAL_ITEMS = "EXCEPTIONAL_ITEMS"
    TAX = "TAX"
    PPE = "PPE"
    CWIP = "CWIP"
    GOODWILL_INTANGIBLES = "GOODWILL_INTANGIBLES"
    BORROWINGS = "BORROWINGS"
    LEASES = "LEASES"
    INVESTMENTS = "INVESTMENTS"
    INVENTORY = "INVENTORY"
    TRADE_RECEIVABLES = "TRADE_RECEIVABLES"
    TRADE_PAYABLES = "TRADE_PAYABLES"
    PROVISIONS = "PROVISIONS"
    CASH = "CASH"
    RELATED_PARTY = "RELATED_PARTY"
    CONTINGENT_LIABILITIES = "CONTINGENT_LIABILITIES"
    COMMITMENTS = "COMMITMENTS"
    FINANCIAL_RISK = "FINANCIAL_RISK"
    SUBSIDIARIES = "SUBSIDIARIES"
    # Concall
    PREPARED_REMARKS = "PREPARED_REMARKS"
    QUESTION_AND_ANSWER = "QUESTION_AND_ANSWER"
    # Rating report
    RATING_ACTION = "RATING_ACTION"
    RATING_RATIONALE = "RATING_RATIONALE"
    KEY_STRENGTHS = "KEY_STRENGTHS"
    KEY_RISKS = "KEY_RISKS"
    LIQUIDITY = "LIQUIDITY"
    RATING_OUTLOOK = "RATING_OUTLOOK"
    RATING_SENSITIVITIES = "RATING_SENSITIVITIES"
    # Investor presentation
    BUSINESS_OVERVIEW = "BUSINESS_OVERVIEW"
    OPERATIONAL_KPIS = "OPERATIONAL_KPIS"
    CAPACITY = "CAPACITY"
    ORDER_BOOK = "ORDER_BOOK"
    CAPEX = "CAPEX"
    GUIDANCE = "GUIDANCE"
    OTHER = "OTHER"


class EvidenceKind(StrEnum):
    NARRATIVE = "NARRATIVE"
    TABLE = "TABLE"
    FINANCIAL_STATEMENT = "FINANCIAL_STATEMENT"
    FINANCIAL_NOTE = "FINANCIAL_NOTE"
    OPERATIONAL_KPI = "OPERATIONAL_KPI"
    SEGMENT_DISCLOSURE = "SEGMENT_DISCLOSURE"
    GEOGRAPHIC_DISCLOSURE = "GEOGRAPHIC_DISCLOSURE"
    MANAGEMENT_GUIDANCE = "MANAGEMENT_GUIDANCE"
    MANAGEMENT_CLAIM = "MANAGEMENT_CLAIM"
    RISK_DISCLOSURE = "RISK_DISCLOSURE"
    CAPEX_DISCLOSURE = "CAPEX_DISCLOSURE"
    CAPACITY_DISCLOSURE = "CAPACITY_DISCLOSURE"
    ORDER_BOOK_DISCLOSURE = "ORDER_BOOK_DISCLOSURE"
    DEBT_DISCLOSURE = "DEBT_DISCLOSURE"
    LIQUIDITY_DISCLOSURE = "LIQUIDITY_DISCLOSURE"
    RELATED_PARTY_DISCLOSURE = "RELATED_PARTY_DISCLOSURE"
    CONTINGENT_LIABILITY = "CONTINGENT_LIABILITY"
    AUDITOR_DISCLOSURE = "AUDITOR_DISCLOSURE"
    GOVERNANCE_DISCLOSURE = "GOVERNANCE_DISCLOSURE"
    REMUNERATION_DISCLOSURE = "REMUNERATION_DISCLOSURE"
    ESG_DISCLOSURE = "ESG_DISCLOSURE"
    RATING_DISCLOSURE = "RATING_DISCLOSURE"
    TRANSCRIPT_QUESTION = "TRANSCRIPT_QUESTION"
    TRANSCRIPT_ANSWER = "TRANSCRIPT_ANSWER"
    OTHER = "OTHER"


class FinancialStatementKind(StrEnum):
    PROFIT_AND_LOSS = "PROFIT_AND_LOSS"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"
    CHANGES_IN_EQUITY = "CHANGES_IN_EQUITY"
    FINANCIAL_RESULTS = "FINANCIAL_RESULTS"  # quarterly 'Statement of ... Financial Results'
    SEGMENT_RESULTS = "SEGMENT_RESULTS"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ExtractionWarning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: Code
    page_number: PositiveInt | None = None
    detail: Label | None = None  # bounded, never a local path or exception text


class TextBlock(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    block_id: UUID
    page_number: PositiveInt
    order: NonNegativeInt  # document-wide reading order
    block_type: BlockType
    start: NonNegativeInt  # offsets into the page's raw text
    end: NonNegativeInt
    normalized_text: Excerpt
    is_repeated_header: bool = False
    is_repeated_footer: bool = False
    speaker: Label | None = None  # transcripts only
    locator: Label

    @model_validator(mode="after")
    def span(self) -> Self:
        if self.end < self.start:
            raise ValueError("A block cannot end before it starts")
        return self


class ExtractedPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    page_number: PositiveInt  # 1-based, always
    printed_label: Label | None = None  # only from the document's own page-label table
    raw_text: str
    character_count: NonNegativeInt
    block_ids: tuple[UUID, ...] = ()
    table_ids: tuple[UUID, ...] = ()
    warnings: tuple[ExtractionWarning, ...] = ()

    @model_validator(mode="after")
    def counted(self) -> Self:
        if self.character_count != len(self.raw_text):
            raise ValueError("character_count must equal the raw text length")
        return self


class TableCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    row: NonNegativeInt
    column: NonNegativeInt
    raw_text: Label
    normalized_text: Label
    is_header: bool = False


class ExtractedTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    table_id: UUID
    page_start: PositiveInt
    page_end: PositiveInt
    title: Label | None
    row_count: NonNegativeInt
    column_count: NonNegativeInt
    cells: tuple[TableCell, ...]
    block_ids: tuple[UUID, ...]
    statement_kind: FinancialStatementKind | None = None  # a CANDIDATE label only
    reporting_basis: Literal["standalone", "consolidated"] | None = None
    section_kind: SectionKind | None = None
    continuation_of: UUID | None = None
    method: Label  # e.g. 'text-rows/right-aligned-numbers'
    confidence: Confidence
    locator: Label
    warnings: tuple[ExtractionWarning, ...] = ()

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.page_end < self.page_start:
            raise ValueError("A table cannot end before it starts")
        if any(c.row >= self.row_count or c.column >= self.column_count for c in self.cells):
            raise ValueError("Cells must lie inside the table")
        return self


class DocumentSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    section_id: UUID
    kind: SectionKind
    title: Label  # as found in the document
    page_start: PositiveInt
    page_end: PositiveInt
    heading_block_id: UUID | None
    method: Literal["HEADING", "RUNNING_HEADER", "TRANSCRIPT_STRUCTURE"]


class EvidenceItem(BaseModel):
    """One located piece of evidence. Text is a bounded excerpt; the page/blocks/table
    it references are the full source."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    evidence_id: UUID
    company_key: str
    document_id: UUID
    artifact_sha256: Sha256Hex
    document_type: EvidenceDocumentType
    authority: SourceAuthority
    kind: EvidenceKind
    raw_text: Excerpt
    normalized_text: Excerpt
    page_start: PositiveInt
    page_end: PositiveInt
    section_kind: SectionKind | None = None
    section_id: UUID | None = None
    block_ids: tuple[UUID, ...] = ()
    table_id: UUID | None = None
    locator: Label
    method: Label
    confidence: Confidence
    tags: tuple[Label, ...] = ()

    @model_validator(mode="after")
    def located(self) -> Self:
        if not self.block_ids and self.table_id is None:
            raise ValueError("Evidence must point at source blocks or a table")
        if self.page_end < self.page_start:
            raise ValueError("Evidence cannot end before it starts")
        return self


class TranscriptParticipant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Label  # exactly as the speaker label appears
    role: Literal["MANAGEMENT", "ANALYST", "MODERATOR"]
    first_block_id: UUID


class TranscriptStructure(BaseModel):
    """Deterministic transcript facts. Roles: MODERATOR by label; ANALYST when introduced
    by the moderator as asking a question; everyone else speaking is MANAGEMENT."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    call_date_text: Label | None  # verbatim from the first page, never interpreted
    period_text: Label | None
    participants: tuple[TranscriptParticipant, ...]
    qa_start_block_id: UUID | None


class ExtractionLimits(BaseModel):
    """Hard bounds. Anything cut is reported as a warning and the status becomes PARTIAL."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_pages: PositiveInt = 1500
    max_characters: PositiveInt = 20_000_000
    max_tables: PositiveInt = 5_000
    max_cells: PositiveInt = 500_000
    max_evidence_items: PositiveInt = 20_000
    max_seconds: float = Field(default=180.0, gt=0, le=3600)
    max_spreadsheet_sheets: PositiveInt = 20


class ExtractionStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pages: NonNegativeInt
    characters: NonNegativeInt
    blocks: NonNegativeInt
    tables: NonNegativeInt
    cells: NonNegativeInt
    sections: NonNegativeInt
    evidence_items: NonNegativeInt
    pages_without_text: NonNegativeInt


class DocumentExtraction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    extraction_id: UUID
    document_id: UUID
    company_key: str
    document_type: EvidenceDocumentType
    authority: SourceAuthority
    artifact_sha256: Sha256Hex
    artifact_reference: Label
    retrieval_id: UUID
    parser: Label
    parser_version: Label
    extracted_at: AwareDatetime
    format: DocumentFormat | None
    status: ExtractionStatus
    page_count: NonNegativeInt | None
    warnings: tuple[ExtractionWarning, ...] = ()
    pages: tuple[ExtractedPage, ...] = ()
    blocks: tuple[TextBlock, ...] = ()
    tables: tuple[ExtractedTable, ...] = ()
    sections: tuple[DocumentSection, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    transcript: TranscriptStructure | None = None
    statistics: ExtractionStatistics

    @model_validator(mode="after")
    def internally_linked(self) -> Self:
        pages = {p.page_number for p in self.pages}
        blocks = {b.block_id for b in self.blocks}
        tables = {t.table_id for t in self.tables}
        sections = {s.section_id for s in self.sections}
        if any(b.page_number not in pages for b in self.blocks):
            raise ValueError("Every block must sit on an extracted page")
        if any(not ({t.page_start, t.page_end} <= pages) for t in self.tables):
            raise ValueError("Every table must sit on extracted pages")
        for item in self.evidence:
            if item.document_id != self.document_id or item.artifact_sha256 != self.artifact_sha256:
                raise ValueError("Evidence must belong to this extraction's document and bytes")
            if not set(item.block_ids) <= blocks or (
                item.table_id is not None and item.table_id not in tables
            ):
                raise ValueError("Evidence must reference blocks/tables of this extraction")
            if item.section_id is not None and item.section_id not in sections:
                raise ValueError("Evidence section must exist")
            if not ({item.page_start, item.page_end} <= pages):
                raise ValueError("Evidence pages must exist")
        return self


class CountByKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key: Label
    count: PositiveInt


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["EVIDENCE_BUNDLE_V1"] = "EVIDENCE_BUNDLE_V1"
    bundle_id: UUID
    company_key: str
    retrieval_run_id: UUID
    extracted_at: AwareDatetime
    parser_version: Label
    limits: ExtractionLimits
    documents_attempted: NonNegativeInt
    documents_extracted: NonNegativeInt
    documents_partial: NonNegativeInt
    documents_failed: NonNegativeInt
    documents_skipped: tuple[CountByKey, ...] = ()
    total_pages: NonNegativeInt
    total_characters: NonNegativeInt
    total_tables: NonNegativeInt
    total_evidence_items: NonNegativeInt
    by_document_type: tuple[CountByKey, ...]
    by_evidence_kind: tuple[CountByKey, ...]
    warnings: tuple[CountByKey, ...]
    extractions: tuple[DocumentExtraction, ...]

    @model_validator(mode="after")
    def unique(self) -> Self:
        ids = [e.document_id for e in self.extractions]
        if len(ids) != len(set(ids)):
            raise ValueError("One extraction per document")
        if any(e.company_key != self.company_key for e in self.extractions):
            raise ValueError("Every extraction must belong to the bundle company")
        return self
