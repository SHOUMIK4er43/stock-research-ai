"""DETAILED_RESEARCH_V1 Box 3: deterministic document structure and evidence (no AI).

Input is a format-neutral RawDocument (pages of raw text, plus any blocks/tables the
format itself declares, e.g. HTML). Output is a DocumentExtraction whose every block,
table, section and evidence item is located by 1-based page and character offsets.

Rules are explicit and conservative:
- Normalization only touches whitespace, Unicode composition (NFC) and lower-case
  hyphenated line wraps. Digits, signs, currency marks and wording are never changed.
- Repeated page headers/footers are MARKED, never removed from the raw text.
- Text tables: runs of >=3 lines ending in >=2 number-like tokens. Number tokens are
  aligned from the RIGHT, so an optional note-reference column never shifts the period
  columns. Ambiguous continuations are linked only when page, columns and context agree.
- Sections come from headings and running page headers matched against fixed patterns.
- Evidence is emitted for tables, disclosure sections and explicit trigger phrases; it
  references blocks/tables instead of copying whole passages.
"""

import bisect
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid5

from stock_research_ai.domain.document_intelligence import (
    BlockType,
    Confidence,
    DocumentExtraction,
    DocumentFormat,
    DocumentSection,
    EvidenceItem,
    EvidenceKind,
    ExtractedPage,
    ExtractedTable,
    ExtractionLimits,
    ExtractionStatistics,
    ExtractionStatus,
    ExtractionWarning,
    FinancialStatementKind,
    SectionKind,
    TableCell,
    TextBlock,
    TranscriptParticipant,
    TranscriptStructure,
)
from stock_research_ai.domain.evidence import EvidenceDocumentType, SourceAuthority

STRUCTURE_VERSION = "structure/1"
_NS = UUID("0b6f5a3e-8d3c-4f43-9a27-5f1c2e9d4b11")
T = EvidenceDocumentType
S = SectionKind
K = EvidenceKind

# --- format-neutral parser output ---------------------------------------------------------


@dataclass(frozen=True)
class RawSpan:
    block_type: BlockType
    start: int
    end: int


@dataclass(frozen=True)
class RawTable:
    title: str | None
    rows: list[list[tuple[str, bool]]]  # (cell text, is_header)
    row_spans: list[tuple[int, int]]  # each row's span in the page text
    method: str


@dataclass
class RawPage:
    number: int  # 1-based
    text: str
    printed_label: str | None = None
    spans: list[RawSpan] | None = None  # None: segment lines here
    tables: list[RawTable] = field(default_factory=list)
    warnings: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass
class RawDocument:
    format: DocumentFormat | None
    status: ExtractionStatus
    parser: str
    parser_version: str
    page_count: int | None = None
    pages: list[RawPage] = field(default_factory=list)
    warnings: list[tuple[str, int | None, str | None]] = field(default_factory=list)


@dataclass(frozen=True)
class DocumentContext:
    document_id: UUID
    company_key: str
    document_type: EvidenceDocumentType
    authority: SourceAuthority
    artifact_sha256: str
    artifact_reference: str
    retrieval_id: UUID
    extracted_at: datetime
    tags: tuple[str, ...] = ()  # e.g. ('agency:crisil',) from the catalogue


# --- normalization ---------------------------------------------------------------------------


def normalize_text(raw: str) -> str:
    """Whitespace/NFC only; joins lower-case hyphenated wraps. Never touches digits."""
    text = unicodedata.normalize("NFC", raw).replace(" ", " ")
    text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{2,}", "\n\n", text).strip()


def _flat(raw: str) -> str:
    return re.sub(r"\s+", " ", normalize_text(raw)).strip()


# --- line classification -----------------------------------------------------------------------

_NUMBER = r"\(?[-–−]?\d[\d,]*(?:\.\d+)?\)?%?|[-–—]"
_NUMBER_TOKEN = re.compile(_NUMBER)
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_HEADER_HINT = re.compile(
    r"particulars|as at|year ended|quarter ended|note|months ended|audited|unaudited", re.I
)
_LIST = re.compile(r"^\s*(?:[•●▪–*-]|\(?[a-z]\)|\(?[ivx]+\)|\d+\))\s+\S")
_NUMBERED_HEADING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[A-Z]\.|[IVX]+\.)\s+[A-Za-z]")
_FOOTNOTE = re.compile(r"^\s*(?:\*|#|†|notes?\s*:)", re.I)


def trailing_numbers(line: str) -> tuple[str, list[str]]:
    """(label, number-like tokens) peeled from the right of a line.

    A token is a whole whitespace-delimited word that fully matches _NUMBER. The scan
    walks right to left once (linear in the line), instead of re-searching the whole
    remaining line per token, which was quadratic on long hostile lines."""
    tokens: list[str] = []
    end = len(line.rstrip())
    while end:
        start = end
        while start and not line[start - 1].isspace():
            start -= 1
        if not _NUMBER_TOKEN.fullmatch(line, start, end):
            break
        tokens.append(line[start:end])
        end = start
        while end and line[end - 1].isspace():
            end -= 1
    tokens.reverse()
    return line[:end].strip(), tokens


def _is_heading(line: str) -> bool:
    text = line.strip()
    words = text.split()
    if not words or len(text) > 90 or len(words) > 12 or text.endswith((".", ",", ";", ":")):
        return False
    if not re.search(r"[A-Za-z]{3}", text) or trailing_numbers(text)[1]:
        return False
    if _NUMBERED_HEADING.match(text):
        return True
    letters = [w for w in words if w[:1].isalpha()]
    capitalised = sum(w[:1].isupper() for w in letters)
    return text.isupper() or (bool(letters) and capitalised / len(letters) >= 0.6)


# --- section patterns ---------------------------------------------------------------------------

_DOCUMENT_SECTIONS: list[tuple[SectionKind, str]] = [
    (
        S.CHAIRMAN_MESSAGE,
        r"chair(man|person|woman)'?s?\b.*(message|letter|statement)|message from the chair",
    ),
    (S.CEO_MESSAGE, r"\b(ceo|managing director)\b.*(message|letter)|letter to the shareholders?"),
    (S.MANAGEMENT_DISCUSSION, r"management[’']?s?\s+discussion"),
    (S.DIRECTORS_REPORT, r"(board[’']?s|directors[’']?)\s+report"),
    (S.CORPORATE_GOVERNANCE, r"corporate governance"),
    (S.BRSR, r"business responsibility|sustainability report|\bbrsr\b"),
    (S.AUDITOR_REPORT, r"independent auditor[’']?s[’']? report|auditor[’']?s[’']? report"),
    (
        S.NOTES_TO_ACCOUNTS,
        r"notes (to|forming part of) (the )?(standalone |consolidated )?financial statements",
    ),
    (S.STANDALONE_FINANCIAL_STATEMENTS, r"standalone financial (statements?|results)"),
    (S.CONSOLIDATED_FINANCIAL_STATEMENTS, r"consolidated financial (statements?|results)"),
    (S.AGM_NOTICE, r"notice of (the )?.*annual general meeting"),
    (S.REMUNERATION, r"remuneration"),
]
_NOTE_SECTIONS: list[tuple[SectionKind, str]] = [
    (S.SEGMENT_REPORTING, r"segment (information|reporting|results)|operating segments"),
    (S.RELATED_PARTY, r"related part(y|ies)"),
    (S.CONTINGENT_LIABILITIES, r"contingent liabilit"),
    (S.COMMITMENTS, r"\bcommitments\b"),
    (S.REVENUE, r"^(\d+(\.\d+)*\.?\s+)?revenue\b"),
    (S.EMPLOYEE_COSTS, r"employee benefit"),
    (S.FINANCE_COSTS, r"finance costs?"),
    (S.DEPRECIATION, r"depreciation"),
    (S.OTHER_INCOME, r"other income"),
    (S.EXCEPTIONAL_ITEMS, r"exceptional items?"),
    (S.TAX, r"income tax|taxes on income|deferred tax"),
    (S.CWIP, r"capital work[- ]in[- ]progress"),
    (S.PPE, r"property,? plant and equipment"),
    (S.GOODWILL_INTANGIBLES, r"goodwill|intangible assets"),
    (S.BORROWINGS, r"\bborrowings\b"),
    (S.LEASES, r"\bleases?\b"),
    (S.INVESTMENTS, r"^(\d+(\.\d+)*\.?\s+)?investments?\b"),
    (S.INVENTORY, r"inventor(y|ies)"),
    (S.TRADE_RECEIVABLES, r"trade receivables"),
    (S.TRADE_PAYABLES, r"trade payables"),
    (S.PROVISIONS, r"^(\d+(\.\d+)*\.?\s+)?provisions\b"),
    (S.CASH, r"cash and cash equivalents"),
    (S.FINANCIAL_RISK, r"financial risk|risk management"),
    (S.SUBSIDIARIES, r"subsidiar(y|ies)|associates|joint ventures?"),
]
_RATING_SECTIONS: list[tuple[SectionKind, str]] = [
    (S.RATING_SENSITIVITIES, r"rating sensitivit|what could change the rating"),
    (S.RATING_ACTION, r"rating action|instrument details|ratings? (assigned|reaffirmed)"),
    (S.KEY_STRENGTHS, r"strengths?"),
    (S.KEY_RISKS, r"weakness|credit challenges|\brisks?\b"),
    (S.RATING_RATIONALE, r"rationale|key rating drivers"),
    (S.LIQUIDITY, r"^liquidity"),
    (S.RATING_OUTLOOK, r"^outlook"),
]
_PRESENTATION_SECTIONS: list[tuple[SectionKind, str]] = [
    (S.BUSINESS_OVERVIEW, r"(business|company) overview|about (us|the company)"),
    (S.OPERATIONAL_KPIS, r"(key|operational|operating) (metrics|kpis|highlights)"),
    (S.ORDER_BOOK, r"order (book|backlog|inflow)"),
    (S.CAPACITY, r"\bcapacity\b"),
    (S.CAPEX, r"\bcapex\b|capital expenditure"),
    (S.GUIDANCE, r"guidance|outlook"),
    (S.SEGMENT_REPORTING, r"segment"),
]
_FINANCIAL_TYPES = {
    T.ANNUAL_REPORT,
    T.QUARTERLY_RESULT,
    T.CORPORATE_GOVERNANCE_REPORT,
    T.BRSR,
    T.AGM_NOTICE,
    T.REGULATION_30_FILING,
    T.EXCHANGE_ANNOUNCEMENT,
    T.OTHER_COMPANY_DOCUMENT,
    T.SHAREHOLDER_LETTER,
}


def _section_patterns(document_type: EvidenceDocumentType) -> list[tuple[SectionKind, str]]:
    if document_type == T.CREDIT_RATING_REPORT:
        return _RATING_SECTIONS
    if document_type == T.INVESTOR_PRESENTATION:
        return _PRESENTATION_SECTIONS + _DOCUMENT_SECTIONS
    if document_type in _FINANCIAL_TYPES:
        return _DOCUMENT_SECTIONS + _NOTE_SECTIONS
    return _DOCUMENT_SECTIONS


def classify_section(text: str, document_type: EvidenceDocumentType) -> SectionKind | None:
    lowered = _flat(text).lower()
    for kind, pattern in _section_patterns(document_type):
        if re.search(pattern, lowered):
            return kind
    return None


_STATEMENTS: list[tuple[FinancialStatementKind, str]] = [
    (FinancialStatementKind.FINANCIAL_RESULTS, r"financial results"),
    (FinancialStatementKind.SEGMENT_RESULTS, r"\bsegments?\b"),
    (FinancialStatementKind.CHANGES_IN_EQUITY, r"changes in equity"),
    (FinancialStatementKind.CASH_FLOW, r"statement of cash flows?|cash flow statement"),
    (
        FinancialStatementKind.PROFIT_AND_LOSS,
        r"statement of profit and loss|profit and loss account|income statement",
    ),
    (FinancialStatementKind.BALANCE_SHEET, r"balance sheet|statement of assets and liabilities"),
]


def statement_kind(title: str | None) -> FinancialStatementKind | None:
    lowered = (title or "").lower()
    return next((kind for kind, p in _STATEMENTS if re.search(p, lowered)), None)


def is_statement_title(line: str) -> bool:
    """A title, not prose that mentions one: it must START with statement wording
    (after an optional number and basis/audit qualifiers), begin with a capital letter,
    contain no sentence break and carry no trailing figures."""
    text = line.strip()
    if not text or len(text) > 100 or text.endswith((".", ":", ",", ";")):
        return False
    figures = [t for t in trailing_numbers(text)[1] if not _YEAR.fullmatch(t)]
    if ". " in text or len(figures) >= 2:  # a date ('March 31, 2026') is fine; figures are not
        return False
    body = re.sub(r"^\d+(\.\d+)*\.?\s+", "", text)
    if not body[:1].isupper():
        return False
    return (
        re.match(
            r"(?:(?:standalone|consolidated|audited|unaudited|condensed|interim|abridged)\s+)*"
            r"(?:statements? of|balance sheet|cash flow|profit and loss|financial results"
            r"|segment(?:s|al)? (?:information|results|reporting))",
            body.lower(),
        )
        is not None
        and statement_kind(body) is not None
    )


# --- evidence triggers -------------------------------------------------------------------------

_FORENSIC: list[tuple[EvidenceKind, str, str]] = [
    (
        K.AUDITOR_DISCLOSURE,
        "audit-qualification",
        r"qualified opinion|adverse opinion|disclaimer of opinion",
    ),
    (K.AUDITOR_DISCLOSURE, "emphasis-of-matter", r"emphasis of matter"),
    (K.AUDITOR_DISCLOSURE, "key-audit-matter", r"key audit matters?"),
    (
        K.AUDITOR_DISCLOSURE,
        "auditor-change",
        r"auditors?\b.*\b(resign|appoint|casual vacancy)|(resign|resignation).*\bauditors?\b",
    ),
    (K.GOVERNANCE_DISCLOSURE, "pledge", r"\bpledg(e|ed|ing)\b"),
    (
        K.DEBT_DISCLOSURE,
        "default",
        r"\bdefault(s|ed)?\b.*\b(loan|interest|debt|repayment|borrowing)s?\b",
    ),
    (K.DEBT_DISCLOSURE, "covenant", r"\bcovenants?\b"),
    (K.CONTINGENT_LIABILITY, "guarantee", r"\bguarantees?\b"),
    (K.CONTINGENT_LIABILITY, "contingent-liability", r"contingent liabilit"),
    (
        K.RISK_DISCLOSURE,
        "litigation-regulatory",
        r"litigation|legal proceedings|show[- ]cause|penalt(y|ies)|regulatory action",
    ),
    (K.FINANCIAL_NOTE, "accounting-policy-change", r"change(s)? in (the )?accounting polic"),
    (K.RELATED_PARTY_DISCLOSURE, "related-party", r"related part(y|ies)"),
]
_GUIDANCE_TERMS = (
    r"\b(expect\w*|target\w*|guidance|anticipat\w*|aim\w*|plan\w*|capex|capacity"
    r"|margins?|growth|order book)\b"
)
_OPERATIONAL: list[tuple[EvidenceKind, str]] = [
    (K.CAPEX_DISCLOSURE, r"\bcapex\b|capital expenditure"),
    (K.CAPACITY_DISCLOSURE, r"\bcapacity\b|\bmtpa\b|\bmw\b|\bgw\b"),
    (K.ORDER_BOOK_DISCLOSURE, r"order (book|backlog|inflow)"),
]
_RATING_SYMBOL = re.compile(
    r"(?:CRISIL|\[ICRA\]|ICRA|CARE|IND|BWR|ACUITE|IVR)\s?(?:AAA|AA|A|BBB|BB|B|C|D)[+-]?\d?\+?"
    r"(?:\s*\(?(?:/|\()?\s*(?:Stable|Positive|Negative|Developing)\)?)?"
)
_RATING_ACTIONS = r"\b(reaffirmed|upgraded|downgraded|assigned|withdrawn|revised|placed on)\b"
_AMOUNT = re.compile(
    r"(?:Rs\.?|INR|₹|`)\s?[\d,]+(?:\.\d+)?\s?(?:crore|cr|lakh|million|billion)\b", re.I
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")

_SECTION_EVIDENCE: dict[SectionKind, EvidenceKind] = {
    S.RELATED_PARTY: K.RELATED_PARTY_DISCLOSURE,
    S.CONTINGENT_LIABILITIES: K.CONTINGENT_LIABILITY,
    S.COMMITMENTS: K.CONTINGENT_LIABILITY,
    S.AUDITOR_REPORT: K.AUDITOR_DISCLOSURE,
    S.CORPORATE_GOVERNANCE: K.GOVERNANCE_DISCLOSURE,
    S.REMUNERATION: K.REMUNERATION_DISCLOSURE,
    S.BRSR: K.ESG_DISCLOSURE,
    S.BORROWINGS: K.DEBT_DISCLOSURE,
    S.SEGMENT_REPORTING: K.SEGMENT_DISCLOSURE,
    S.FINANCIAL_RISK: K.RISK_DISCLOSURE,
    S.RATING_ACTION: K.RATING_DISCLOSURE,
    S.RATING_RATIONALE: K.RATING_DISCLOSURE,
    S.KEY_STRENGTHS: K.RATING_DISCLOSURE,
    S.KEY_RISKS: K.RISK_DISCLOSURE,
    S.LIQUIDITY: K.LIQUIDITY_DISCLOSURE,
    S.RATING_OUTLOOK: K.RATING_DISCLOSURE,
    S.RATING_SENSITIVITIES: K.RATING_DISCLOSURE,
    S.OPERATIONAL_KPIS: K.OPERATIONAL_KPI,
    S.CAPACITY: K.CAPACITY_DISCLOSURE,
    S.ORDER_BOOK: K.ORDER_BOOK_DISCLOSURE,
    S.CAPEX: K.CAPEX_DISCLOSURE,
    S.GUIDANCE: K.MANAGEMENT_GUIDANCE,
    S.CHAIRMAN_MESSAGE: K.MANAGEMENT_CLAIM,
    S.CEO_MESSAGE: K.MANAGEMENT_CLAIM,
    S.MANAGEMENT_DISCUSSION: K.NARRATIVE,
    S.BUSINESS_OVERVIEW: K.NARRATIVE,
}
_NOTE_TABLE_EVIDENCE: dict[SectionKind, EvidenceKind] = {
    S.RELATED_PARTY: K.RELATED_PARTY_DISCLOSURE,
    S.CONTINGENT_LIABILITIES: K.CONTINGENT_LIABILITY,
    S.COMMITMENTS: K.CONTINGENT_LIABILITY,
    S.BORROWINGS: K.DEBT_DISCLOSURE,
    S.SEGMENT_REPORTING: K.SEGMENT_DISCLOSURE,
    S.OPERATIONAL_KPIS: K.OPERATIONAL_KPI,
}
_NOTE_KINDS = {kind for kind, _ in _NOTE_SECTIONS}
_NOTE_HEADING = re.compile(r"^\s*(?:note\s*)?\d+(?:\.\d+)*\.?\s+[A-Za-z]", re.I)
_TRANSCRIPTS = {T.CONCALL_TRANSCRIPT, T.AGM_TRANSCRIPT}
_SPEAKER = re.compile(r"^(?P<name>[A-Z][\w.’'\- ]{1,60}?)\s*:\s*(?P<rest>\S.*)?$")
_MODERATOR = re.compile(r"^(moderator|operator|coordinator|host)\b", re.I)
_NOT_SPEAKERS = re.compile(
    r"^(note|notes|date|time|source|subject|sub|place|venue|ref|re|encl|enclosure|attn"
    r"|kind attn|scrip code|scrip id|symbol|cc|website|email|tel|fax)$",
    re.I,
)
_ANALYST_INTRO = re.compile(
    r"question[^.]{0,80}?from(?: the line of)?\s+(?P<name>[A-Z][A-Za-z.’'\- ]{1,60}?)"
    r"(?=\s+(?:from|of|with)\b|[,.]|$)"
)
_ROLE_HEADING = re.compile(
    r"^(corporate participants|company participants|management|analysts?|journalists|"
    r"participants|conference call participants)\s*:?\s*$",
    re.I,
)
_NAME_LINE = re.compile(
    r"(?:[A-Z][a-z'’\-]+|[A-Z]\.(?:[A-Z]\.)*)(?:\s+(?:[A-Z][a-z'’\-]+|[A-Z]\.(?:[A-Z]\.)*)){1,3}"
)
_DESIGNATION = re.compile(
    r"\b(officer|director|head|president|chairman|chairperson|analyst|manager|limited|ltd|"
    r"securities|capital|bank|research|partners|investments?|chief|vice|global|group)\b",
    re.I,
)
_CALL_DATE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+,?\s+\d{4}|[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\b"
)
_CALL_PERIOD = re.compile(
    r"\bQ[1-4]\s*(?:FY)?\s*'?\d{2,4}\b|\b(?:first|second|third|fourth) quarter\b", re.I
)


class _Limit(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Analyzer:
    def __init__(
        self, raw: RawDocument, context: DocumentContext, limits: ExtractionLimits
    ) -> None:
        self.raw = raw
        self.ctx = context
        self.limits = limits
        self.version = f"{raw.parser_version};{STRUCTURE_VERSION}"
        self.started = time.monotonic()
        self.warnings: list[ExtractionWarning] = [
            ExtractionWarning(code=code, page_number=page, detail=detail)
            for code, page, detail in raw.warnings
        ]
        self.blocks: list[TextBlock] = []
        self.tables: list[ExtractedTable] = []
        self.sections: list[DocumentSection] = []
        self.evidence: list[EvidenceItem] = []
        self.page_blocks: dict[int, list[UUID]] = {}
        self.page_tables: dict[int, list[UUID]] = {}
        self.texts: dict[int, str] = {p.number: p.text for p in raw.pages}
        self.cells = 0
        self.partial = False
        self.transcript: TranscriptStructure | None = None
        self.block_index: dict[UUID, TextBlock] = {}
        self.heading_orders: list[int] = []
        self.heading_sections: list[DocumentSection | None] = []
        self.terminators: list[TextBlock] = []
        self.running_by_page: dict[int, DocumentSection] = {}
        self.qa_order: int | None = None
        self.speakers: dict[str, str] = {}

    def uid(self, *parts: object) -> UUID:
        return uuid5(_NS, "|".join([self.ctx.artifact_sha256, self.version, *map(str, parts)]))

    def warn(self, code: str, page: int | None = None, detail: str | None = None) -> None:
        self.warnings.append(ExtractionWarning(code=code, page_number=page, detail=detail))

    def check_time(self) -> None:
        if time.monotonic() - self.started > self.limits.max_seconds:
            raise _Limit("TIME_LIMIT_REACHED")

    # --- blocks ------------------------------------------------------------------------------

    def run(self) -> DocumentExtraction:
        pages = self.raw.pages
        repeated = self.repeated_lines(pages)
        self.speakers = self.participant_lists() if self.ctx.document_type in _TRANSCRIPTS else {}
        try:
            for page in pages:
                self.check_time()
                if page.spans is not None:
                    self.declared_blocks(page)
                else:
                    self.segment_page(page, repeated)
            self.detect_sections()
            if self.ctx.document_type in _TRANSCRIPTS:
                self.transcript_structure()
                self.index_sections()
            self.link_continuations()
            self.emit_evidence()
        except _Limit as limit:
            self.partial = True
            self.warn(limit.code)
        return self.build()

    def add_block(
        self,
        page: RawPage,
        kind: BlockType,
        start: int,
        end: int,
        *,
        header: bool = False,
        footer: bool = False,
        speaker: str | None = None,
    ) -> TextBlock:
        order = len(self.blocks)
        normalized = normalize_text(page.text[start:end])[:4000]
        block = TextBlock(
            block_id=self.uid("block", page.number, start, end),
            page_number=page.number,
            order=order,
            block_type=kind,
            start=start,
            end=end,
            normalized_text=normalized,
            is_repeated_header=header,
            is_repeated_footer=footer,
            speaker=speaker,
            locator=f"page:{page.number}/chars:{start}-{end}",
        )
        self.blocks.append(block)
        self.page_blocks.setdefault(page.number, []).append(block.block_id)
        return block

    def declared_blocks(self, page: RawPage) -> None:
        for span in page.spans or []:
            text = page.text[span.start : span.end]
            if not text.strip():
                continue
            kind = span.block_type
            if (
                self.ctx.document_type in _TRANSCRIPTS
                and kind in (BlockType.PARAGRAPH, BlockType.OTHER)
                and self.speaker_of(text.split("\n", 1)[0], self.speakers)
            ):
                kind = BlockType.UTTERANCE
            self.add_block(page, kind, span.start, span.end)
        for index, table in enumerate(page.tables):
            row_blocks = [
                self.add_block(page, BlockType.TABLE_ROW, start, end)
                for start, end in table.row_spans
            ]
            self.add_table(
                page.number,
                page.number,
                table.title,
                table.rows,
                [b.block_id for b in row_blocks],
                table.method,
                Confidence.HIGH,
                f"page:{page.number}/table:{index}",
            )

    @staticmethod
    def lines(text: str) -> list[tuple[int, int, str]]:
        result, offset = [], 0
        for line in text.split("\n"):
            end = offset + len(line)
            if line.strip():
                result.append((offset, end, line))
            offset = end + 1
        return result

    @staticmethod
    def boilerplate_key(line: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", line.strip().lower()))

    def repeated_lines(self, pages: list[RawPage]) -> dict[str, str]:
        """Top-2/bottom-2 line keys repeated on >=3 pages that are either >=30% of all pages
        or a run of >=3 pages at most 2 apart (a section's own running header)."""
        textual = [p for p in pages if p.spans is None and p.text.strip()]
        tops: dict[str, list[int]] = {}
        bottoms: dict[str, list[int]] = {}
        for page in textual:
            lines = [line for _, _, line in self.lines(page.text)]
            for key in {self.boilerplate_key(line) for line in lines[:2]}:
                tops.setdefault(key, []).append(page.number)
            for key in {self.boilerplate_key(line) for line in lines[-2:]}:
                bottoms.setdefault(key, []).append(page.number)
        share = max(3, int(0.3 * len(textual) + 0.999))

        def repeated(numbers: list[int]) -> bool:
            if len(numbers) >= share:
                return True
            run = best = 1
            for previous, current in zip(numbers, numbers[1:], strict=False):
                # Facing pages alternate their headers, so a run may step by 2 pages.
                run = run + 1 if current - previous <= 2 else 1
                best = max(best, run)
            return len(numbers) >= 3 and best >= 3

        result = {key: "header" for key, numbers in tops.items() if repeated(numbers)}
        result.update(
            {
                key: "footer"
                for key, numbers in bottoms.items()
                if repeated(numbers) and key not in result
            }
        )
        return result

    def segment_page(self, page: RawPage, repeated: dict[str, str]) -> None:
        lines = self.lines(page.text)
        if not lines:
            return
        kinds: list[str] = []
        for index, (_, _, line) in enumerate(lines):
            key = self.boilerplate_key(line)
            edge = index < 2 or index >= len(lines) - 2
            if edge and key in repeated:
                kinds.append(repeated[key])
            elif len(trailing_numbers(line)[1]) >= 2:
                kinds.append("row")
            else:
                kinds.append("text")
        table_runs = self.table_runs(lines, kinds)
        in_table = {i for run in table_runs for i in range(run[0], run[1] + 1)}
        index = 0
        paragraph: list[int] = []

        def flush() -> None:
            if paragraph:
                first, last = paragraph[0], paragraph[-1]
                self.add_block(page, BlockType.PARAGRAPH, lines[first][0], lines[last][1])
                paragraph.clear()

        while index < len(lines):
            start, end, line = lines[index]
            kind = kinds[index]
            run = next((r for r in table_runs if r[0] == index), None)
            if run is not None:
                flush()
                self.text_table(page, lines, run, kinds)
                index = run[1] + 1
                continue
            if index in in_table:
                index += 1
                continue
            if kind in ("header", "footer"):
                flush()
                block_type = BlockType.HEADER if kind == "header" else BlockType.FOOTER
                self.add_block(
                    page, block_type, start, end, header=kind == "header", footer=kind == "footer"
                )
            elif self.ctx.document_type in _TRANSCRIPTS and self.speaker_of(line, self.speakers):
                flush()
                self.add_block(page, BlockType.UTTERANCE, start, end)
            elif _is_heading(line):
                flush()
                self.add_block(page, BlockType.HEADING, start, end)
            elif _LIST.match(line):
                flush()
                self.add_block(page, BlockType.LIST, start, end)
            elif _FOOTNOTE.match(line):
                flush()
                self.add_block(page, BlockType.FOOTNOTE, start, end)
            else:
                paragraph.append(index)
                if line.rstrip().endswith((".", "?", "!", ":")):
                    flush()
            index += 1
        flush()

    # --- tables ------------------------------------------------------------------------------

    @staticmethod
    def table_runs(lines: list[tuple[int, int, str]], kinds: list[str]) -> list[tuple[int, int]]:
        """[first, last] line indexes of runs with >=3 numeric rows; short label-only lines
        inside a run (e.g. 'Expenses') are kept; header lines just above are included."""
        runs: list[tuple[int, int]] = []
        index = 0
        while index < len(lines):
            if kinds[index] != "row":
                index += 1
                continue
            first = last = index
            rows = 1
            gap = 0
            probe = index + 1
            while probe < len(lines) and gap <= 2:
                if kinds[probe] == "row":
                    last, rows, gap = probe, rows + 1, 0
                elif (
                    kinds[probe] == "text"
                    and len(lines[probe][2].strip()) <= 90
                    and not lines[probe][2].rstrip().endswith(".")
                ):
                    gap += 1
                else:
                    break
                probe += 1
            if rows >= 3:
                head = first
                while head - 1 >= 0 and first - (head - 1) <= 3 and kinds[head - 1] == "text":
                    candidate = lines[head - 1][2]
                    if is_statement_title(candidate) or not (
                        _YEAR.search(candidate) or _HEADER_HINT.search(candidate)
                    ):
                        break  # a statement title stays the title, never a header row
                    head -= 1
                runs.append((head, last))
            index = last + 1
        return runs

    def text_table(
        self,
        page: RawPage,
        lines: list[tuple[int, int, str]],
        run: tuple[int, int],
        kinds: list[str],
    ) -> None:
        first, last = run
        parsed: list[tuple[str, list[str], bool]] = []
        for _, _, line in lines[first : last + 1]:
            label, numbers = trailing_numbers(line)
            header = not numbers or (
                bool(numbers) and all(_YEAR.fullmatch(n) for n in numbers) and len(label) < 60
            )
            if header and parsed and any(p[1] and not p[2] for p in parsed):
                header = False  # header rows only lead the table
            parsed.append((label, numbers, header))
        width = 1 + max(len(numbers) for _, numbers, _ in parsed)
        rows: list[list[tuple[str, bool]]] = []
        for label, numbers, header in parsed:
            cells = [(label, header)] + [("", header)] * (width - 1 - len(numbers))
            rows.append(cells + [(n, header) for n in numbers])
        blocks = [
            self.add_block(page, BlockType.TABLE_ROW, s, e) for s, e, _ in lines[first : last + 1]
        ]
        title = self.title_above(lines, first, kinds)
        if title is None:
            # Some layouts place the statement title after the table in reading order:
            # use the page's statement title only when there is exactly one.
            page_titles = {
                line.strip()
                for index, (_, _, line) in enumerate(lines)
                if kinds[index] not in ("header", "footer") and is_statement_title(line)
            }
            if len(page_titles) == 1 and not self.page_tables.get(page.number):
                title = next(iter(page_titles))[:500]
        self.add_table(
            page.number,
            page.number,
            title,
            rows,
            [b.block_id for b in blocks],
            "text-rows/right-aligned-numbers",
            Confidence.MEDIUM,
            f"page:{page.number}/lines:{first}-{last}",
        )

    def title_above(
        self, lines: list[tuple[int, int, str]], first: int, kinds: list[str]
    ) -> str | None:
        """Nearest statement title (else heading) within 6 lines above the table, never a
        repeated page header/footer."""
        window = [
            line.strip()
            for index, (_, _, line) in enumerate(lines)
            if max(0, first - 6) <= index < first and kinds[index] not in ("header", "footer")
        ]
        for text in reversed(window):
            if is_statement_title(text):
                return text[:500]
        for text in reversed(window):
            if _is_heading(text):
                return text[:500]
        return None

    def add_table(
        self,
        page_start: int,
        page_end: int,
        title: str | None,
        rows: list[list[tuple[str, bool]]],
        block_ids: list[UUID],
        method: str,
        confidence: Confidence,
        locator: str,
    ) -> None:
        if len(self.tables) >= self.limits.max_tables:
            raise _Limit("TABLE_LIMIT_REACHED")
        width = max((len(r) for r in rows), default=0)
        cells = [
            TableCell(
                row=r,
                column=c,
                raw_text=text[:500],
                normalized_text=_flat(text)[:500],
                is_header=is_header,
            )
            for r, row in enumerate(rows)
            for c, (text, is_header) in enumerate(row)
        ]
        self.cells += len(cells)
        if self.cells > self.limits.max_cells:
            raise _Limit("CELL_LIMIT_REACHED")
        table = ExtractedTable(
            table_id=self.uid("table", locator),
            page_start=page_start,
            page_end=page_end,
            title=title,
            row_count=len(rows),
            column_count=width,
            cells=tuple(cells),
            block_ids=tuple(block_ids),
            statement_kind=statement_kind(title),
            method=method,
            confidence=confidence,
            locator=locator,
        )
        self.tables.append(table)
        self.page_tables.setdefault(page_start, []).append(table.table_id)

    def link_continuations(self) -> None:
        """Link a table to the previous page's last table only when columns, section and
        statement agree and the later table has no competing title of its own."""
        linked: list[ExtractedTable] = []
        for table in self.tables:
            first_on_page = not any(t.page_start == table.page_start for t in linked)
            previous = (
                None
                if not first_on_page
                else next(
                    (
                        t
                        for t in reversed(linked)
                        if t.page_end == table.page_start - 1
                        and (
                            t.column_count == table.column_count
                            # right-aligned text tables: a narrower continuation (no note
                            # column) still lines up with the previous table's value columns
                            or (
                                t.method == table.method == "text-rows/right-aligned-numbers"
                                and 2 <= table.column_count <= t.column_count
                            )
                        )
                    ),
                    None,
                )
            )
            continues = (
                previous is not None
                and (
                    table.title is None
                    or re.search(r"cont(inue)?d|\(contd", table.title, re.I) is not None
                    or (
                        table.statement_kind is not None
                        and table.statement_kind == previous.statement_kind
                    )
                )
                and previous.section_kind == table.section_kind
            )
            if continues and previous is not None:
                table = table.model_copy(
                    update={
                        "continuation_of": previous.table_id,
                        "statement_kind": table.statement_kind or previous.statement_kind,
                        "reporting_basis": table.reporting_basis or previous.reporting_basis,
                    }
                )
            linked.append(table)
        self.tables = linked

    # --- sections ------------------------------------------------------------------------------

    def detect_sections(self) -> None:
        pages = sorted(self.texts)
        last_page = pages[-1] if pages else 1
        # Running headers: consecutive pages whose repeated header names a section.
        running: dict[int, tuple[SectionKind, str, UUID]] = {}
        for block in self.blocks:
            if block.is_repeated_header or block.is_repeated_footer:
                kind = classify_section(block.normalized_text, self.ctx.document_type)
                if kind is not None and block.page_number not in running:
                    running[block.page_number] = (kind, block.normalized_text, block.block_id)
        page_iter = sorted(running)
        index = 0
        while index < len(page_iter):
            start = page_iter[index]
            kind, title, block_id = running[start]
            end = start
            while (
                index + 1 < len(page_iter)
                and page_iter[index + 1] <= end + 2  # facing pages alternate headers
                and running[page_iter[index + 1]][0] == kind
            ):
                index += 1
                end = page_iter[index]
            self.sections.append(
                DocumentSection(
                    section_id=self.uid("section", "running", start, kind),
                    kind=kind,
                    title=title[:500],
                    page_start=start,
                    page_end=end,
                    heading_block_id=block_id,
                    method="RUNNING_HEADER",
                )
            )
            index += 1
        # Headings: each runs until the next heading-section (or the end of the document).
        found: list[tuple[TextBlock, SectionKind]] = []
        for block in self.blocks:
            if block.block_type in (BlockType.HEADING, BlockType.TITLE):
                kind = classify_section(block.normalized_text, self.ctx.document_type)
                if kind in _NOTE_KINDS and not _NOTE_HEADING.match(block.normalized_text):
                    kind = None  # a note section must start at a numbered note heading
                if kind is not None:
                    found.append((block, kind))
        # Any numbered heading also ends the previous heading-section ('9. ...' ends '8. ...').
        found_ids = {block.block_id for block, _ in found}
        self.terminators = [
            b
            for b in self.blocks
            if b.block_type == BlockType.HEADING
            and b.block_id not in found_ids
            and _NUMBERED_HEADING.match(b.normalized_text)
        ]
        boundaries = sorted([block for block, _ in found] + self.terminators, key=lambda b: b.order)
        for block, kind in found:
            following = next(
                (b.page_number for b in boundaries if b.order > block.order), last_page
            )
            self.sections.append(
                DocumentSection(
                    section_id=self.uid("section", "heading", block.block_id),
                    kind=kind,
                    title=block.normalized_text[:500],
                    page_start=block.page_number,
                    page_end=max(block.page_number, min(following, block.page_number + 4)),
                    heading_block_id=block.block_id,
                    method="HEADING",
                )
            )
        self.index_sections()
        self.tables = [t.model_copy(update=self.table_context(t)) for t in self.tables]

    def index_sections(self) -> None:
        self.block_index = {b.block_id: b for b in self.blocks}
        headed: list[tuple[int, DocumentSection | None]] = sorted(
            [
                (self.block_index[s.heading_block_id].order, s)
                for s in self.sections
                if s.method == "HEADING" and s.heading_block_id in self.block_index
            ]
            + [(b.order, None) for b in self.terminators],
            key=lambda pair: pair[0],
        )
        self.heading_orders = [order for order, _ in headed]
        self.heading_sections = [section for _, section in headed]
        self.running_by_page = {
            page: s
            for s in self.sections
            if s.method in ("RUNNING_HEADER", "TRANSCRIPT_STRUCTURE")
            for page in range(s.page_start, s.page_end + 1)
        }

    def section_for(self, page: int, order: int) -> DocumentSection | None:
        """Most specific section: the latest heading-section at or before the block, if
        its range covers the page; otherwise the running-header section of the page."""
        position = bisect.bisect_right(self.heading_orders, order) - 1
        if position >= 0:
            heading = self.heading_sections[position]
            if heading is not None and heading.page_start <= page <= heading.page_end:
                return heading
        return self.running_by_page.get(page)

    def table_context(self, table: ExtractedTable) -> dict[str, object]:
        first_block = self.block_index.get(table.block_ids[0]) if table.block_ids else None
        section = self.section_for(table.page_start, first_block.order if first_block else 0)
        running = self.running_by_page.get(table.page_start)
        basis: Literal["standalone", "consolidated"] | None = None
        # The table's own title decides first, then its section, then the running header.
        for source in (
            table.title,
            section.title if section else None,
            running.title if running else None,
        ):
            lowered = (source or "").lower()
            has_standalone, has_consolidated = "standalone" in lowered, "consolidated" in lowered
            if has_standalone != has_consolidated:
                basis = "standalone" if has_standalone else "consolidated"
                break
        kind = section.kind if section else None
        if statement_kind(table.title) is not None and basis is not None:
            # A titled statement belongs to the financial statements of its basis.
            kind = (
                S.STANDALONE_FINANCIAL_STATEMENTS
                if basis == "standalone"
                else S.CONSOLIDATED_FINANCIAL_STATEMENTS
            )
        return {"section_kind": kind, "reporting_basis": basis}

    # --- transcripts ----------------------------------------------------------------------------

    @staticmethod
    def speaker_of(line: str, known: dict[str, str] | None = None) -> str | None:
        """'Name: text' labels, or a known participant's name alone on its line."""
        text = line.strip()
        if known and text in known:
            return text
        match = _SPEAKER.match(text)
        if not match:
            return None
        name = match.group("name").strip()
        if len(name.split()) > 6 or _NOT_SPEAKERS.match(name) or trailing_numbers(name)[1]:
            return None
        return name

    def participant_lists(self) -> dict[str, str]:
        """Names under 'CORPORATE PARTICIPANTS' / 'MANAGEMENT' (MANAGEMENT) or
        'ANALYSTS' / 'JOURNALISTS' / 'PARTICIPANTS' (ANALYST) headings, plus name-like
        lines standing alone >=3 times. Role keywords in designations are ignored."""
        names: dict[str, str] = {}
        standalone: Counter[str] = Counter()
        for page in self.raw.pages:
            role: str | None = None
            for _, _, line in self.lines(page.text):
                text = line.strip()
                heading = _ROLE_HEADING.match(text)
                if heading:
                    role = (
                        "ANALYST"
                        if heading.group(1)
                        .lower()
                        .startswith(("analyst", "journalist", "participant"))
                        and "corporate" not in heading.group(1).lower()
                        else "MANAGEMENT"
                    )
                    continue
                if _NAME_LINE.fullmatch(text) and not _DESIGNATION.search(text):
                    standalone[text] += 1
                    if role is not None:
                        names.setdefault(text, role)
                elif len(text) > 80:
                    role = None
        for name, count in standalone.items():
            if count >= 3:
                names.setdefault(name, "")
        return names

    def transcript_structure(self) -> None:
        """Speaker turns, moderator-introduced analysts and the start of Q&A."""
        current: str | None = None
        updated: list[TextBlock] = []
        for block in self.blocks:
            if block.block_type == BlockType.UTTERANCE:
                current = self.speaker_of(
                    self.texts[block.page_number][block.start : block.end], self.speakers
                )
                block = block.model_copy(update={"speaker": current})
            elif block.block_type == BlockType.PARAGRAPH and current is not None:
                block = block.model_copy(
                    update={"block_type": BlockType.UTTERANCE, "speaker": current}
                )
            updated.append(block)
        self.blocks = updated
        utterances = [b for b in self.blocks if b.block_type == BlockType.UTTERANCE and b.speaker]
        analysts: set[str] = set()
        qa_start: TextBlock | None = None
        for block in utterances:
            speaker = block.speaker or ""
            text = block.normalized_text
            # The host may be a 'Moderator' or a company spokesperson: the phrases decide.
            if qa_start is None and re.search(
                r"question[- ]and[- ]answer|first question|open the floor for questions"
                r"|open (it|the call) up for questions",
                text,
                re.I,
            ):
                qa_start = block
            if _MODERATOR.match(speaker) or "question" in text.lower():
                for match in _ANALYST_INTRO.finditer(text):
                    analysts.add(match.group("name").strip())

        def role(name: str) -> Literal["MANAGEMENT", "ANALYST", "MODERATOR"]:
            if _MODERATOR.match(name):
                return "MODERATOR"
            listed = self.speakers.get(name)
            if listed == "ANALYST":
                return "ANALYST"
            if listed == "MANAGEMENT":
                return "MANAGEMENT"
            if any(name == a or name.startswith(a) or a.startswith(name) for a in analysts):
                return "ANALYST"
            return "MANAGEMENT"

        first_seen: dict[str, UUID] = {}
        for block in utterances:
            first_seen.setdefault(block.speaker or "", block.block_id)
        opening = "\n".join(self.texts.get(n, "") for n in sorted(self.texts)[:3])
        titled = re.search(
            r"(?:call|conference)[^\n]*\n(?:[^\n]*\n){0,2}?[^\n]*?" + _CALL_DATE.pattern,
            opening,
            re.I,
        )
        date = _CALL_DATE.search(titled.group(0)) if titled else _CALL_DATE.search(opening)
        period = _CALL_PERIOD.search(opening)
        self.transcript = TranscriptStructure(
            call_date_text=date.group(0) if date else None,
            period_text=period.group(0) if period else None,
            participants=tuple(
                TranscriptParticipant(name=name, role=role(name), first_block_id=block_id)
                for name, block_id in first_seen.items()
            ),
            qa_start_block_id=qa_start.block_id if qa_start else None,
        )
        if utterances:
            last_page = max(self.texts)
            boundary = qa_start.page_number if qa_start else last_page
            prepared_end = (
                boundary
                if qa_start is None
                else max(utterances[0].page_number, qa_start.page_number)
            )
            self.sections.append(
                DocumentSection(
                    section_id=self.uid("section", "prepared"),
                    kind=S.PREPARED_REMARKS,
                    title="Prepared remarks",
                    page_start=utterances[0].page_number,
                    page_end=prepared_end,
                    heading_block_id=utterances[0].block_id,
                    method="TRANSCRIPT_STRUCTURE",
                )
            )
            if qa_start is not None:
                self.qa_order = qa_start.order
                self.sections.append(
                    DocumentSection(
                        section_id=self.uid("section", "qa"),
                        kind=S.QUESTION_AND_ANSWER,
                        title="Question and answer session",
                        page_start=qa_start.page_number,
                        page_end=last_page,
                        heading_block_id=qa_start.block_id,
                        method="TRANSCRIPT_STRUCTURE",
                    )
                )

    def transcript_section(self, block: TextBlock) -> DocumentSection | None:
        if self.transcript is None:
            return None
        wanted = (
            S.QUESTION_AND_ANSWER
            if self.qa_order is not None and block.order > self.qa_order
            else S.PREPARED_REMARKS
        )
        return next((s for s in self.sections if s.kind == wanted), None)

    # --- evidence -------------------------------------------------------------------------------

    def add_evidence(
        self,
        kind: EvidenceKind,
        raw_text: str,
        page_start: int,
        page_end: int,
        *,
        section: DocumentSection | None,
        block_ids: tuple[UUID, ...] = (),
        table_id: UUID | None = None,
        locator: str,
        method: str,
        confidence: Confidence,
        tags: tuple[str, ...] = (),
    ) -> None:
        if len(self.evidence) >= self.limits.max_evidence_items:
            raise _Limit("EVIDENCE_LIMIT_REACHED")
        self.evidence.append(
            EvidenceItem(
                evidence_id=self.uid("evidence", kind, locator, method),
                company_key=self.ctx.company_key,
                document_id=self.ctx.document_id,
                artifact_sha256=self.ctx.artifact_sha256,
                document_type=self.ctx.document_type,
                authority=self.ctx.authority,
                kind=kind,
                raw_text=raw_text[:4000],
                normalized_text=_flat(raw_text)[:4000],
                page_start=page_start,
                page_end=page_end,
                section_kind=section.kind if section else None,
                section_id=section.section_id if section else None,
                block_ids=block_ids,
                table_id=table_id,
                locator=locator,
                method=method,
                confidence=confidence,
                tags=tuple(sorted(set(tags + self.ctx.tags))),
            )
        )

    def emit_evidence(self) -> None:
        sections = {s.section_id: s for s in self.sections}
        for table in self.tables:
            self.check_time()
            if table.statement_kind == FinancialStatementKind.SEGMENT_RESULTS:
                kind = K.SEGMENT_DISCLOSURE
            elif table.statement_kind is not None:
                kind = K.FINANCIAL_STATEMENT
            elif table.section_kind in _NOTE_TABLE_EVIDENCE:
                kind = _NOTE_TABLE_EVIDENCE[table.section_kind]
            elif table.section_kind in _NOTE_KINDS:
                kind = K.FINANCIAL_NOTE
            else:
                kind = K.TABLE
            first_rows = [
                " | ".join(c.raw_text for c in table.cells if c.row == r)
                for r in range(min(table.row_count, 8))
            ]
            text = "\n".join(filter(None, [table.title, *first_rows]))
            section = next(
                (
                    s
                    for s in sections.values()
                    if s.kind == table.section_kind
                    and s.page_start <= table.page_start <= s.page_end
                ),
                None,
            )
            tags = ["candidate"] if table.statement_kind else []
            if table.statement_kind:
                tags.append(f"statement:{table.statement_kind.value}")
            if table.reporting_basis:
                tags.append(f"basis:{table.reporting_basis}")
            self.add_evidence(
                kind,
                text,
                table.page_start,
                table.page_end,
                section=section,
                table_id=table.table_id,
                block_ids=table.block_ids[:1],
                locator=table.locator,
                method=table.method,
                confidence=table.confidence,
                tags=tuple(tags),
            )
        for block in self.blocks:
            self.check_time()
            if block.block_type not in (BlockType.PARAGRAPH, BlockType.LIST, BlockType.UTTERANCE):
                continue
            raw = self.texts[block.page_number][block.start : block.end]
            if block.block_type == BlockType.UTTERANCE:
                section = self.transcript_section(block)
            else:
                section = self.section_for(block.page_number, block.order)
            self.block_evidence(block, raw, section)

    def block_evidence(self, block: TextBlock, raw: str, section: DocumentSection | None) -> None:
        def emit(
            kind: EvidenceKind,
            text: str,
            method: str,
            confidence: Confidence,
            tags: tuple[str, ...],
            suffix: str,
        ) -> None:
            self.add_evidence(
                kind,
                text,
                block.page_number,
                block.page_number,
                section=section,
                block_ids=(block.block_id,),
                locator=f"{block.locator}{suffix}",
                method=method,
                confidence=confidence,
                tags=tags,
            )

        doc_type = self.ctx.document_type
        role = None
        if block.block_type == BlockType.UTTERANCE and _flat(raw).rstrip(":") == block.speaker:
            return  # a speaker's name line on its own carries no content
        if (
            block.block_type == BlockType.UTTERANCE
            and self.transcript is not None
            and block.speaker
        ):
            role = next(
                (p.role for p in self.transcript.participants if p.name == block.speaker), None
            )
            if (
                section is not None
                and section.kind == S.QUESTION_AND_ANSWER
                and role in ("ANALYST", "MANAGEMENT")
            ):
                emit(
                    K.TRANSCRIPT_QUESTION if role == "ANALYST" else K.TRANSCRIPT_ANSWER,
                    raw,
                    "transcript-turn",
                    Confidence.MEDIUM,
                    (f"speaker:{block.speaker}",),
                    "",
                )
        elif (
            section is not None
            and section.kind in _SECTION_EVIDENCE
            and len(block.normalized_text) >= 40
        ):
            emit(
                _SECTION_EVIDENCE[section.kind],
                raw,
                f"section:{section.kind.value}",
                Confidence.MEDIUM,
                (),
                "",
            )
        for index, sentence in enumerate(_SENTENCE.split(raw)):
            lowered = sentence.lower()
            guidance_scope = (
                doc_type in _TRANSCRIPTS and role == "MANAGEMENT"
            ) or doc_type == T.INVESTOR_PRESENTATION
            if guidance_scope:
                terms = sorted({m.group(1).lower() for m in re.finditer(_GUIDANCE_TERMS, lowered)})
                if terms:
                    emit(
                        K.MANAGEMENT_GUIDANCE,
                        sentence,
                        "phrase:guidance",
                        Confidence.LOW,
                        ("candidate-guidance", *(f"term:{t}" for t in terms)),
                        f"/sentence:{index}",
                    )
                for kind, pattern in _OPERATIONAL:
                    if re.search(pattern, lowered):
                        emit(
                            kind,
                            sentence,
                            "phrase:operational",
                            Confidence.LOW,
                            ("candidate",),
                            f"/sentence:{index}/{kind.value}",
                        )
            for kind, tag, pattern in _FORENSIC:
                if re.search(pattern, lowered):
                    emit(
                        kind,
                        sentence,
                        "phrase:forensic",
                        Confidence.LOW,
                        (f"forensic:{tag}",),
                        f"/sentence:{index}/{tag}",
                    )
            if doc_type == T.CREDIT_RATING_REPORT:
                symbols = [m.group(0) for m in _RATING_SYMBOL.finditer(sentence)]
                actions = sorted(
                    {m.group(1).lower() for m in re.finditer(_RATING_ACTIONS, lowered)}
                )
                if symbols or actions or _AMOUNT.search(sentence):
                    tags = (
                        *(f"rating:{s}" for s in symbols),
                        *(f"action:{a}" for a in actions),
                        *(("amount-mention",) if _AMOUNT.search(sentence) else ()),
                    )
                    emit(
                        K.RATING_DISCLOSURE,
                        sentence,
                        "phrase:rating",
                        Confidence.LOW,
                        tags,
                        f"/sentence:{index}/rating",
                    )

    # --- assembly --------------------------------------------------------------------------------

    def build(self) -> DocumentExtraction:
        pages = []
        characters = 0
        for page in self.raw.pages:
            characters += len(page.text)
            pages.append(
                ExtractedPage(
                    page_number=page.number,
                    printed_label=page.printed_label,
                    raw_text=page.text,
                    character_count=len(page.text),
                    block_ids=tuple(self.page_blocks.get(page.number, [])),
                    table_ids=tuple(self.page_tables.get(page.number, [])),
                    warnings=tuple(
                        ExtractionWarning(code=code, page_number=page.number, detail=detail)
                        for code, detail in page.warnings
                    ),
                )
            )
        empty_pages = sum(not p.raw_text.strip() for p in pages)
        status = self.raw.status
        if status == ExtractionStatus.SUCCESS and pages and empty_pages == len(pages):
            status = ExtractionStatus.NO_TEXT
            self.warn("NO_EXTRACTABLE_TEXT", detail="OCR required; not attempted")
        elif status == ExtractionStatus.SUCCESS and (self.partial or empty_pages):
            status = ExtractionStatus.PARTIAL
        if empty_pages and status != ExtractionStatus.NO_TEXT:
            self.warn("PAGES_WITHOUT_TEXT", detail=f"{empty_pages} page(s); OCR required")
        return DocumentExtraction(
            extraction_id=uuid5(
                _NS,
                f"{self.ctx.document_id}|{self.ctx.artifact_sha256}|{self.version}|{self.limits.model_dump_json()}",
            ),
            document_id=self.ctx.document_id,
            company_key=self.ctx.company_key,
            document_type=self.ctx.document_type,
            authority=self.ctx.authority,
            artifact_sha256=self.ctx.artifact_sha256,
            artifact_reference=self.ctx.artifact_reference,
            retrieval_id=self.ctx.retrieval_id,
            parser=self.raw.parser,
            parser_version=self.version,
            extracted_at=self.ctx.extracted_at,
            format=self.raw.format,
            status=status,
            page_count=self.raw.page_count,
            warnings=tuple(self.warnings),
            pages=tuple(pages),
            blocks=tuple(self.blocks),
            tables=tuple(self.tables),
            sections=tuple(self.sections),
            evidence=tuple(self.evidence),
            transcript=self.transcript,
            statistics=ExtractionStatistics(
                pages=len(pages),
                characters=characters,
                blocks=len(self.blocks),
                tables=len(self.tables),
                cells=self.cells,
                sections=len(self.sections),
                evidence_items=len(self.evidence),
                pages_without_text=empty_pages,
            ),
        )


def analyse_document(
    raw: RawDocument, context: DocumentContext, limits: ExtractionLimits
) -> DocumentExtraction:
    """Pure and deterministic for the same bytes, parser version and limits."""
    return _Analyzer(raw, context, limits).run()
