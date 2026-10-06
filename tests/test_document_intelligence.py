"""DETAILED_RESEARCH_V1 Box 3: deterministic document intelligence (offline, synthetic)."""

import ast
import io
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pypdf import PdfWriter

from stock_research_ai.adapters.documents import document_parsers
from stock_research_ai.adapters.documents.document_parsers import (
    LocalDocumentParser,
    parse_document,
)
from stock_research_ai.adapters.documents.evidence_fetcher import (
    RawArtifactEvidenceReader,
    RawArtifactEvidenceWriter,
    SafeEvidenceFetcher,
)
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.application import document_intelligence, document_structure
from stock_research_ai.application.document_intelligence import (
    canonical_bundle_json,
    extract_evidence,
    verify_provenance,
)
from stock_research_ai.application.document_structure import (
    DocumentContext,
    analyse_document,
    normalize_text,
    trailing_numbers,
)
from stock_research_ai.application.evidence_catalogue import assemble_catalogue
from stock_research_ai.application.evidence_retrieval import retrieve_evidence
from stock_research_ai.domain.document_intelligence import (
    BlockType,
    DocumentExtraction,
    EvidenceBundle,
    EvidenceKind,
    ExtractionLimits,
    ExtractionStatus,
    FinancialStatementKind,
    SectionKind,
)
from stock_research_ai.domain.evidence import (
    ClassificationConfidence,
    DocumentCandidate,
    DocumentDiscovery,
    EvidenceCompany,
    EvidenceDocumentType,
    SourceAuthority,
    SourceDiscovery,
    SourceOutcome,
)
from stock_research_ai.domain.evidence_retrieval import DetectedContentType, RetrievalPolicy
from stock_research_ai.domain.financial import ReportingScope

T = EvidenceDocumentType
K = EvidenceKind
S = SectionKind
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
RUN = UUID("22222222-3333-4444-8555-666666666666")
LIMITS = ExtractionLimits()


# --- tiny deterministic fixture builders ------------------------------------------------------


def make_pdf(pages: list[list[str]], *, labels: bool = False) -> bytes:
    """A minimal, valid text PDF (Helvetica), one text line per entry."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    body: dict[int, str] = {}
    kids: list[int] = []
    next_id = 4
    for lines in pages:
        content = (
            "BT /F1 10 Tf 14 TL 50 800 Td "
            + " ".join(f"({escape(line)}) Tj T*" for line in lines)
            + " ET"
            if lines
            else ""
        )
        body[next_id] = f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"
        body[next_id + 1] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {next_id} 0 R >>"
        )
        kids.append(next_id + 1)
        next_id += 2
    page_labels = " /PageLabels << /Nums [0 << /S /r >> 2 << /S /D /St 1 >>] >>" if labels else ""
    body[1] = f"<< /Type /Catalog /Pages 2 0 R{page_labels} >>"
    body[2] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    body[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for number in sorted(body):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{body[number]}\nendobj\n".encode("latin-1")
    xref = len(out)
    size = max(body) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offsets[i]:010d} 00000 n \n".encode() for i in range(1, size))
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


HEADER = "Example Industries Annual Report 2025-26"
MDA = "Management's discussion and analysis"
FS = "Standalone financial statements"


def annual_report_pdf() -> bytes:
    def page(section: str, number: int, lines: list[str]) -> list[str]:
        return [HEADER, section, *lines, f"Page {number}"]

    return make_pdf(
        [
            page(
                MDA,
                1,
                [
                    "Business Overview",
                    "Example Industries makes specialty chemicals for global customers.",
                    "Revenue grew because volumes rose in all three segments.",
                    "The promoters have pledged 12.5% of their shareholding with lenders.",
                ],
            ),
            page(
                MDA,
                2,
                [
                    "Outlook For The Year",
                    "Demand remained steady across export markets during the year.",
                    "- New plant commissioned in Gujarat",
                ],
            ),
            page(
                MDA,
                3,
                [
                    "Risks And Concerns",
                    "Input prices are volatile and may affect profitability.",
                ],
            ),
            page(
                FS,
                4,
                [
                    "Standalone Statement of Profit and Loss",
                    "in Rs. crore",
                    "Particulars Note 2026 2025",
                    "Revenue from operations 2.1 1,234.50 1,100.00",
                    "Other income 45.20 (12.30)",
                    "Expenses",
                    "Employee benefit expenses 2.2 400.00 380.00",
                    "Effective tax rate 25.2% 24.8%",
                    "Exceptional items - (5.00)",
                    "Profit for the year -120.40 98.10",
                ],
            ),
            page(
                FS,
                5,
                [
                    "Earnings per share basic 12.40 10.10",
                    "Earnings per share diluted 12.30 10.00",
                    "Weighted shares outstanding 100.00 100.00",
                ],
            ),
            page(
                FS,
                6,
                [
                    "2.23 Related Party Transactions",
                    "The Company entered into transactions with related parties at arm's length.",
                    "2.24 Contingent Liabilities",
                    "Contingent liabilities not provided for amount to Rs. 120 crore.",
                    "The auditor issued a qualified opinion on inventory valuation.",
                ],
            ),
        ],
        labels=True,
    )


TRANSCRIPT = (
    b'"<html><head><title>Example Industries Q1 FY27 Earnings Conference'
    b" Call</title></head>\n"
    b"<body><h1>Example Industries Q1 FY27 Earnings Conference Call</h1><p>July 24,"
    b" 2026</p>\n"
    b"<p>Moderator: Good evening and welcome to the call. I now hand over to Mr. Ravi"
    b" Kumar.</p>\n"
    b"<p>Ravi Kumar: Thank you. Revenue grew 12% this quarter. We expect margins of"
    b" 18% to 20% for FY27 and plan capex of Rs. 500 crore. Our order book stands at"
    b" Rs. 4,200 crore.</p>\n"
    b"<p>Moderator: We will now begin the question-and-answer session. The first"
    b" question is from the line of Priya Shah from Alpha Capital.</p>\n"
    b"<p>Priya Shah: What is the capacity utilisation?</p>\n"
    b"<p>Ravi Kumar: Utilisation was 82% and we target 90% by March 2027.</p>\n"
    b"</body></html>"
)

RATING = (
    b'"<html><body><h1>Rating Rationale</h1>\n'
    b"<h2>Rating Action</h2><p>CRISIL Ratings has reaffirmed its CRISIL AA+/Stable"
    b" rating on the Rs.500 crore non-convertible debentures of Example Industries"
    b" Limited.</p>\n"
    b"<h2>Key Rating Drivers - Strengths</h2><p>Strong market position in specialty"
    b" chemicals with diversified customers.</p>\n"
    b"<h2>Weaknesses</h2><p>Exposure to volatile input prices could weigh on margins"
    b" over the medium term.</p>\n"
    b"<h2>Liquidity: Strong</h2><p>Cash and equivalents of Rs.900 crore comfortably"
    b" cover debt obligations of the coming year.</p>\n"
    b"<h2>Outlook: Stable</h2><p>The outlook reflects expected steady cash generation"
    b" from operations.</p>\n"
    b"</body></html>"
)

PRESENTATION = (
    b'"<html><body><h1>Investor Presentation Q1 FY27</h1>\n'
    b"<h2>Capacity Expansion</h2><p>Capacity will increase to 12 MTPA by FY28 with"
    b" planned capex of Rs. 800 crore.</p>\n"
    b"<table><caption>Key metrics</caption><tr><th>Metric</th><th>Q1"
    b" FY27</th></tr><tr><td>Utilisation</td><td>82%</td></tr></table>\n"
    b"</body></html>"
)


def make_xlsx() -> bytes:
    buffer = io.BytesIO()
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f"<workbook {ns} {rel}><sheets>"
            '<sheet name="Results" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/>'
            "</Relationships>",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            f"<sst {ns}><si><t>Particulars</t></si><si><t>Q1 FY27</t></si><si><t>Revenue</t></si>"
            "<si><t>Net profit</t></si></sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {ns}><sheetData>"
            '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1234.5</v></c></row>'
            '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>-12.3</v></c></row>'
            "</sheetData></worksheet>",
        )
    return buffer.getvalue()


def context(kind: EvidenceDocumentType, sha: str = "a" * 64, tags=()) -> DocumentContext:
    return DocumentContext(
        document_id=UUID(int=1),
        company_key="screener:9999",
        document_type=kind,
        authority=SourceAuthority.PRIMARY_REGULATORY,
        artifact_sha256=sha,
        artifact_reference=f"sha256/{sha[:2]}/{sha}.pdf",
        retrieval_id=UUID(int=2),
        extracted_at=NOW,
        tags=tags,
    )


def extract(content: bytes, kind, detected=DetectedContentType.PDF, limits=LIMITS, tags=()):
    return analyse_document(
        parse_document(content, detected, limits), context(kind, tags=tags), limits
    )


def page_text(extraction: DocumentExtraction, block) -> str:
    page = next(p for p in extraction.pages if p.page_number == block.page_number)
    return page.raw_text[block.start : block.end]


# --- PDF pages, blocks, headers, normalization ------------------------------------------------


def test_pdf_pages_are_one_based_with_printed_labels():
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    assert extraction.status is ExtractionStatus.SUCCESS
    assert [p.page_number for p in extraction.pages] == [1, 2, 3, 4, 5, 6]
    assert [p.printed_label for p in extraction.pages] == ["i", "ii", "1", "2", "3", "4"]
    assert all(p.character_count == len(p.raw_text) for p in extraction.pages)
    assert "Revenue from operations 2.1 1,234.50 1,100.00" in extraction.pages[3].raw_text


def test_repeated_headers_and_footers_are_marked_not_removed():
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    headers = [b for b in extraction.blocks if b.is_repeated_header]
    footers = [b for b in extraction.blocks if b.is_repeated_footer]
    # Helvetica's standard encoding renders byte 0x27 as a right single quote.
    assert {b.normalized_text for b in headers} >= {HEADER, MDA.replace("'", "\u2019"), FS}
    assert {b.page_number for b in footers} == {1, 2, 3, 4, 5, 6}  # 'Page N'
    for block in headers + footers:
        assert page_text(extraction, block).strip() == block.normalized_text  # still in raw text


def test_blocks_are_ordered_and_located():
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    assert [b.order for b in extraction.blocks] == list(range(len(extraction.blocks)))
    pages = [b.page_number for b in extraction.blocks]
    assert pages == sorted(pages)
    kinds = Counter(b.block_type for b in extraction.blocks)
    assert kinds[BlockType.HEADING] >= 3 and kinds[BlockType.LIST] == 1
    assert kinds[BlockType.PARAGRAPH] >= 5 and kinds[BlockType.TABLE_ROW] >= 10
    for block in extraction.blocks:
        assert block.locator == f"page:{block.page_number}/chars:{block.start}-{block.end}"
        assert normalize_text(page_text(extraction, block)) == block.normalized_text


def test_normalization_never_changes_numbers_or_wording():
    raw = "Revenue  of Rs. (1,234.50)\tcrore -12.3% devel-\nopment ₹ 5\n\n\n  Next"
    assert normalize_text(raw) == "Revenue of Rs. (1,234.50) crore -12.3% development ₹ 5\n\nNext"
    assert normalize_text("Q1-\nFY27") == "Q1-\nFY27"  # only lower-case word wraps are joined


def test_trailing_number_tokens():
    assert trailing_numbers("Revenue from operations 2.1 1,234.50 1,100.00") == (
        "Revenue from operations",
        ["2.1", "1,234.50", "1,100.00"],
    )
    assert trailing_numbers("Exceptional items - (5.00)") == ("Exceptional items", ["-", "(5.00)"])
    assert trailing_numbers("Depreciation 2.2 & 2.3.2 2,011 1,863")[1] == ["2,011", "1,863"]


# --- tables, statements, continuations, sections ----------------------------------------------


def test_financial_table_cells_preserve_signs_percentages_and_parentheses():
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    table = next(t for t in extraction.tables if t.page_start == 4)
    assert table.title == "Standalone Statement of Profit and Loss"
    assert table.statement_kind is FinancialStatementKind.PROFIT_AND_LOSS
    assert table.reporting_basis == "standalone"
    assert table.column_count == 4  # label | note | 2026 | 2025, right-aligned
    rows: dict[int, list[str]] = {}
    for cell in table.cells:
        rows.setdefault(cell.row, ["?"] * table.column_count)[cell.column] = cell.raw_text
    values = list(rows.values())
    assert values[0] == ["Particulars Note", "", "2026", "2025"]
    assert all(c.is_header for c in table.cells if c.row == 0)
    assert ["Revenue from operations", "2.1", "1,234.50", "1,100.00"] in values
    assert ["Other income", "", "45.20", "(12.30)"] in values
    assert ["Effective tax rate", "", "25.2%", "24.8%"] in values
    assert ["Exceptional items", "", "-", "(5.00)"] in values
    assert ["Profit for the year", "", "-120.40", "98.10"] in values
    assert ["Expenses", "", "", ""] in values
    assert table.confidence.value == "MEDIUM" and table.method == "text-rows/right-aligned-numbers"


def test_continuation_is_linked_only_when_unambiguous():
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    first = next(t for t in extraction.tables if t.page_start == 4)
    second = next(t for t in extraction.tables if t.page_start == 5)
    assert second.continuation_of == first.table_id
    assert second.statement_kind is FinancialStatementKind.PROFIT_AND_LOSS
    assert first.continuation_of is None


def test_sections_from_running_headers_and_headings():
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    running = {
        (s.kind, s.page_start, s.page_end)
        for s in extraction.sections
        if s.method == "RUNNING_HEADER"
    }
    assert (S.MANAGEMENT_DISCUSSION, 1, 3) in running
    assert (S.STANDALONE_FINANCIAL_STATEMENTS, 4, 6) in running
    headed = {s.kind: s for s in extraction.sections if s.method == "HEADING"}
    assert headed[S.RELATED_PARTY].title == "2.23 Related Party Transactions"
    assert headed[S.CONTINGENT_LIABILITIES].page_start == 6


def test_annual_report_evidence_kinds_and_forensic_candidates():
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    kinds = Counter(e.kind for e in extraction.evidence)
    assert kinds[K.FINANCIAL_STATEMENT] == 2
    assert kinds[K.RELATED_PARTY_DISCLOSURE] >= 1
    assert kinds[K.CONTINGENT_LIABILITY] >= 1
    assert kinds[K.NARRATIVE] >= 1  # MD&A paragraphs
    tags = {tag for e in extraction.evidence for tag in e.tags}
    assert {
        "forensic:pledge",
        "forensic:audit-qualification",
        "forensic:contingent-liability",
    } <= tags
    assert "candidate-guidance" not in tags  # guidance phrases are scoped to calls/decks
    pledge = next(e for e in extraction.evidence if "forensic:pledge" in e.tags)
    assert pledge.page_start == 1 and pledge.kind is K.GOVERNANCE_DISCLOSURE
    assert pledge.raw_text == "The promoters have pledged 12.5% of their shareholding with lenders."


# --- transcripts ------------------------------------------------------------------------------


def test_transcript_speakers_roles_qa_and_guidance():
    extraction = extract(TRANSCRIPT, T.CONCALL_TRANSCRIPT, DetectedContentType.HTML)
    transcript = extraction.transcript
    assert transcript is not None
    assert transcript.call_date_text == "July 24, 2026"
    assert transcript.period_text == "Q1 FY27"
    roles = {p.name: p.role for p in transcript.participants}
    assert roles == {"Moderator": "MODERATOR", "Ravi Kumar": "MANAGEMENT", "Priya Shah": "ANALYST"}
    assert transcript.qa_start_block_id is not None
    kinds = {s.kind for s in extraction.sections}
    assert {S.PREPARED_REMARKS, S.QUESTION_AND_ANSWER} <= kinds
    questions = [e for e in extraction.evidence if e.kind is K.TRANSCRIPT_QUESTION]
    answers = [e for e in extraction.evidence if e.kind is K.TRANSCRIPT_ANSWER]
    assert [q.normalized_text for q in questions] == [
        "Priya Shah: What is the capacity utilisation?"
    ]
    assert len(answers) == 1 and "target 90%" in answers[0].raw_text
    guidance = [e for e in extraction.evidence if e.kind is K.MANAGEMENT_GUIDANCE]
    texts = {g.raw_text for g in guidance}
    assert "We expect margins of 18% to 20% for FY27 and plan capex of Rs. 500 crore." in texts
    assert all("candidate-guidance" in g.tags for g in guidance)
    assert not any("Priya Shah" in t for t in texts)  # analysts' words are never guidance
    assert any(e.kind is K.ORDER_BOOK_DISCLOSURE for e in extraction.evidence)
    utterances = [b for b in extraction.blocks if b.block_type is BlockType.UTTERANCE]
    assert [b.speaker for b in utterances] == [
        "Moderator",
        "Ravi Kumar",
        "Moderator",
        "Priya Shah",
        "Ravi Kumar",
    ]


# --- rating reports, presentations, spreadsheets ----------------------------------------------


def test_rating_report_sections_and_candidates():
    extraction = extract(
        RATING, T.CREDIT_RATING_REPORT, DetectedContentType.HTML, tags=("agency:crisil",)
    )
    kinds = {s.kind for s in extraction.sections}
    assert {
        S.RATING_ACTION,
        S.RATING_RATIONALE,
        S.KEY_STRENGTHS,
        S.KEY_RISKS,
        S.LIQUIDITY,
        S.RATING_OUTLOOK,
    } <= kinds
    action = next(e for e in extraction.evidence if "action:reaffirmed" in e.tags)
    assert "rating:CRISIL AA+/Stable" in action.tags and "amount-mention" in action.tags
    assert "agency:crisil" in action.tags
    assert any(e.kind is K.LIQUIDITY_DISCLOSURE for e in extraction.evidence)


def test_presentation_capacity_and_tables():
    extraction = extract(PRESENTATION, T.INVESTOR_PRESENTATION, DetectedContentType.HTML)
    assert S.CAPACITY in {s.kind for s in extraction.sections}
    kinds = Counter(e.kind for e in extraction.evidence)
    assert kinds[K.CAPACITY_DISCLOSURE] >= 1 and kinds[K.CAPEX_DISCLOSURE] >= 1
    (table,) = extraction.tables
    assert table.title == "Key metrics" and table.method == "html-table"
    assert [c.raw_text for c in table.cells] == ["Metric", "Q1 FY27", "Utilisation", "82%"]
    assert [c.is_header for c in table.cells] == [True, True, False, False]


def test_xlsx_sheet_becomes_a_table_with_stored_values():
    extraction = extract(make_xlsx(), T.OTHER_COMPANY_DOCUMENT, DetectedContentType.SPREADSHEET)
    assert extraction.status is ExtractionStatus.SUCCESS
    (table,) = extraction.tables
    assert table.title == "Results" and extraction.pages[0].printed_label == "Results"
    assert [c.raw_text for c in table.cells] == [
        "Particulars",
        "Q1 FY27",
        "Revenue",
        "1234.5",
        "Net profit",
        "-12.3",
    ]


# --- failures, scanned documents, bounds ------------------------------------------------------


@pytest.mark.parametrize(
    "content,detected,status,code",
    [
        (b"", DetectedContentType.PDF, ExtractionStatus.MALFORMED, "EMPTY_DOCUMENT"),
        (
            b"%PDF-1.7 not really a pdf",
            DetectedContentType.PDF,
            ExtractionStatus.MALFORMED,
            "MALFORMED_PDF",
        ),
        (b"<html>", DetectedContentType.PDF, ExtractionStatus.MALFORMED, "NOT_A_PDF"),
        (
            b"\xd0\xcf\x11\xe0legacy",
            DetectedContentType.SPREADSHEET,
            ExtractionStatus.UNSUPPORTED,
            "UNSUPPORTED_SPREADSHEET",
        ),
        (
            b"PK\x03\x04broken",
            DetectedContentType.SPREADSHEET,
            ExtractionStatus.MALFORMED,
            "MALFORMED_SPREADSHEET",
        ),
        (b"\xff\xfe\x00bad", DetectedContentType.TEXT, ExtractionStatus.MALFORMED, "TEXT_NOT_UTF8"),
        (b"   ", DetectedContentType.HTML, ExtractionStatus.MALFORMED, "EMPTY_DOCUMENT"),
        (b"x", DetectedContentType.UNKNOWN, ExtractionStatus.UNSUPPORTED, "UNSUPPORTED_FORMAT"),
    ],
)
def test_malformed_and_unsupported_documents(content, detected, status, code):
    extraction = extract(content, T.ANNUAL_REPORT, detected)
    assert extraction.status is status
    assert code in {w.code for w in extraction.warnings}
    assert extraction.evidence == ()
    assert all(
        "/" not in (w.detail or "") or "OCR" in (w.detail or "") for w in extraction.warnings
    )


def test_encrypted_pdf():
    writer = PdfWriter(clone_from=io.BytesIO(make_pdf([["Secret page"]])))
    writer.encrypt(user_password="secret", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    extraction = extract(buffer.getvalue(), T.ANNUAL_REPORT)
    assert extraction.status is ExtractionStatus.ENCRYPTED
    assert [w.code for w in extraction.warnings] == ["PDF_ENCRYPTED"]


def test_image_only_pdf_is_no_text_with_page_count_kept():
    extraction = extract(make_pdf([[], [], []]), T.ANNUAL_REPORT)
    assert extraction.status is ExtractionStatus.NO_TEXT
    assert extraction.page_count == 3 and len(extraction.pages) == 3
    assert extraction.statistics.pages_without_text == 3
    codes = {w.code for w in extraction.warnings}
    assert "NO_EXTRACTABLE_TEXT" in codes
    assert all(p.warnings[0].code == "NO_TEXT_ON_PAGE" for p in extraction.pages)


def test_partly_scanned_pdf_is_partial():
    extraction = extract(make_pdf([["Some text here."], []]), T.ANNUAL_REPORT)
    assert extraction.status is ExtractionStatus.PARTIAL
    assert "PAGES_WITHOUT_TEXT" in {w.code for w in extraction.warnings}


@pytest.mark.parametrize(
    "limits,code",
    [
        (ExtractionLimits(max_pages=2), "PAGE_LIMIT_REACHED"),
        (ExtractionLimits(max_evidence_items=1), "EVIDENCE_LIMIT_REACHED"),
        (ExtractionLimits(max_tables=1), "TABLE_LIMIT_REACHED"),
        (ExtractionLimits(max_cells=5), "CELL_LIMIT_REACHED"),
        (ExtractionLimits(max_characters=200), "CHARACTER_LIMIT_REACHED"),
    ],
)
def test_resource_bounds_are_explicit(limits, code):
    extraction = extract(annual_report_pdf(), T.ANNUAL_REPORT, limits=limits)
    assert extraction.status is ExtractionStatus.PARTIAL
    assert code in {w.code for w in extraction.warnings}
    assert extraction.statistics.evidence_items <= limits.max_evidence_items


# --- determinism ------------------------------------------------------------------------------


def test_extraction_is_deterministic():
    first = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    second = extract(annual_report_pdf(), T.ANNUAL_REPORT)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    other = analyse_document(
        parse_document(annual_report_pdf(), DetectedContentType.PDF, LIMITS),
        context(T.ANNUAL_REPORT, sha="b" * 64),
        LIMITS,
    )
    assert {e.evidence_id for e in other.evidence}.isdisjoint(
        {e.evidence_id for e in first.evidence}
    )


# --- end to end: catalogue -> retrieval -> bundle -> provenance -------------------------------

HOST = "https://docs.example.com"
COMPANY = EvidenceCompany(
    company_key="screener:9999",
    name="Example Industries Ltd",
    source_company_key="9999",
    reporting_scope=ReportingScope.STANDALONE,
)
FILES = {
    "/ar.pdf": (T.ANNUAL_REPORT, annual_report_pdf(), "application/pdf"),
    "/call.html": (T.CONCALL_TRANSCRIPT, TRANSCRIPT, "text/html"),
    "/rating.html": (T.CREDIT_RATING_REPORT, RATING, "text/html"),
    "/deck.html": (T.INVESTOR_PRESENTATION, PRESENTATION, "text/html"),
    "/broken.pdf": (T.QUARTERLY_RESULT, b"%PDF-1.7 broken bytes", "application/pdf"),
    "/scan.pdf": (T.BRSR, make_pdf([[], []]), "application/pdf"),
    "/copy.pdf": (T.OTHER_COMPANY_DOCUMENT, annual_report_pdf(), "application/pdf"),
}


async def lookup(host: str) -> list[str]:
    return ["93.184.216.34"]


def serve(request: httpx.Request) -> httpx.Response:
    _kind, body, mime = FILES[request.url.path]
    return httpx.Response(200, content=body, headers={"content-type": mime})


async def pipeline(root: Path) -> tuple[EvidenceBundle, object, object]:
    candidates = [
        DocumentCandidate(
            document_type=kind,
            confidence=ClassificationConfidence.HIGH,
            title=path,
            url=f"{HOST}{path}",
            is_direct_document=True,
            discovery=DocumentDiscovery(
                source_name="test",
                discovered_via_url="https://www.screener.in/company/EX/",
                locator=path,
                classified_as=kind,
                discovered_at=NOW,
            ),
            metadata={"rating_agency_label": "crisil"} if kind is T.CREDIT_RATING_REPORT else {},
        )
        for path, (kind, _body, _mime) in FILES.items()
    ]
    catalogue = assemble_catalogue(
        COMPANY,
        [SourceDiscovery(source_name="test", candidates=tuple(candidates))],
        [SourceOutcome(source_name="test", status="SUCCESS", candidates_found=len(candidates))],
        catalogued_at=NOW,
    )
    manifest = await retrieve_evidence(
        catalogue,
        SafeEvidenceFetcher(httpx.MockTransport(serve), lookup),
        RawArtifactEvidenceWriter(FileRawArtifactStore(root)),
        RetrievalPolicy(concurrency=1),
        run_id=RUN,
        clock=lambda: NOW,
    )
    bundle = await extract_evidence(
        catalogue,
        manifest,
        RawArtifactEvidenceReader(root),
        LocalDocumentParser(),
        LIMITS,
        clock=lambda: NOW,
    )
    return bundle, manifest, catalogue


async def test_bundle_counts_failure_isolation_and_provenance(tmp_path):
    bundle, manifest, catalogue = await pipeline(tmp_path / "store")
    statuses = {e.document_type: e.status for e in bundle.extractions}
    assert statuses == {
        T.ANNUAL_REPORT: ExtractionStatus.SUCCESS,
        T.CONCALL_TRANSCRIPT: ExtractionStatus.SUCCESS,
        T.CREDIT_RATING_REPORT: ExtractionStatus.SUCCESS,
        # '%PDF-'-prefixed junk passes the retrieval signature check; Box 3 reports it.
        T.QUARTERLY_RESULT: ExtractionStatus.MALFORMED,
        T.BRSR: ExtractionStatus.NO_TEXT,
    }
    # Box 2 accepts only PDFs for presentations, so the HTML deck was never retrieved;
    # the 'copy' shares the annual report's exact bytes.
    skipped = {c.key: c.count for c in bundle.documents_skipped}
    assert skipped == {"DUPLICATE_CONTENT": 1, "NOT_RETRIEVED": 1}
    assert (
        bundle.documents_attempted,
        bundle.documents_extracted,
        bundle.documents_partial,
        bundle.documents_failed,
    ) == (5, 3, 0, 2)
    assert bundle.total_evidence_items == sum(len(e.evidence) for e in bundle.extractions)
    assert bundle.total_pages == sum(e.statistics.pages for e in bundle.extractions)
    kinds = {c.key: c.count for c in bundle.by_evidence_kind}
    assert kinds["FINANCIAL_STATEMENT"] == 2 and kinds["TRANSCRIPT_QUESTION"] == 1
    assert verify_provenance(bundle, manifest, catalogue) == []
    documents = {d.document_id: d for d in catalogue.documents}
    records = {r.document_id: r for r in manifest.artifacts}
    for extraction in bundle.extractions:
        for item in extraction.evidence:
            record = records[item.document_id]
            assert record.sha256 == item.artifact_sha256 == extraction.artifact_sha256
            assert documents[item.document_id].source_url.startswith(HOST)


async def test_bundle_is_deterministic(tmp_path):
    first, _m1, _c1 = await pipeline(tmp_path / "a")
    second, _m2, _c2 = await pipeline(tmp_path / "b")
    encoded = canonical_bundle_json(first)
    assert encoded == canonical_bundle_json(second)
    assert EvidenceBundle.model_validate_json(encoded) == first


async def test_provenance_check_catches_orphans_and_tampering(tmp_path):
    bundle, manifest, catalogue = await pipeline(tmp_path / "store")
    extraction = bundle.extractions[0]
    forged = extraction.model_copy(update={"artifact_sha256": "f" * 64, "evidence": ()})
    tampered = bundle.model_copy(update={"extractions": (forged, *bundle.extractions[1:])})
    assert verify_provenance(tampered, manifest, catalogue)
    empty_catalogue = catalogue.model_copy(update={"documents": ()})
    assert verify_provenance(bundle, manifest, empty_catalogue)


async def test_tampered_artifact_fails_only_that_document(tmp_path):
    root = tmp_path / "store"
    bundle, manifest, catalogue = await pipeline(root)
    ar = next(r for r in manifest.artifacts if r.document_type is T.ANNUAL_REPORT)
    (root / ar.artifact.reference).write_bytes(b"tampered")
    again = await extract_evidence(
        catalogue,
        manifest,
        RawArtifactEvidenceReader(root),
        LocalDocumentParser(),
        LIMITS,
        clock=lambda: NOW,
    )
    statuses = {
        e.document_type: (e.status, [w.code for w in e.warnings]) for e in again.extractions
    }
    assert statuses[T.ANNUAL_REPORT] == (ExtractionStatus.FAILED, ["ARTIFACT_INTEGRITY_FAILED"])
    assert statuses[T.CONCALL_TRANSCRIPT][0] is ExtractionStatus.SUCCESS


def test_no_ai_or_network_dependencies_in_document_intelligence():
    forbidden = ("httpx", "anthropic", "aicredits", "openai", "summary_generation", "socket")
    for module in (document_structure, document_intelligence, document_parsers):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                assert not any(word in name for word in forbidden), (module.__name__, name)
    assert "pypdf" in sys.modules


def test_standalone_speaker_transcript_with_participant_lists():
    pdf = make_pdf(
        [
            ["TO ALL STOCK EXCHANGES", "July 29, 2026", "Sub: Transcript of the earnings call"],
            [
                "Example Industries Q1 FY27 Earnings Call",
                "July 22, 2026",
                "CORPORATE PARTICIPANTS",
                "Anita Rao",
                "Chief Financial Officer",
                "ANALYSTS",
                "Vikram Mehta",
                "Beta Securities",
            ],
            [
                "Anita Rao",
                "Revenue grew 10% this quarter. We expect margins to improve next year.",
                "Anita Rao",
                "With that, let us open the floor for questions.",
                "Vikram Mehta",
                "How is demand in exports?",
                "Anita Rao",
                "Demand is strong and we plan to add capacity.",
            ],
        ]
    )
    extraction = extract(pdf, T.CONCALL_TRANSCRIPT)
    transcript = extraction.transcript
    assert transcript is not None
    assert transcript.call_date_text == "July 22, 2026"  # the call, not the cover letter
    assert transcript.period_text == "Q1 FY27"
    assert {p.name: p.role for p in transcript.participants} == {
        "Anita Rao": "MANAGEMENT",
        "Vikram Mehta": "ANALYST",
    }
    questions = [e.normalized_text for e in extraction.evidence if e.kind is K.TRANSCRIPT_QUESTION]
    answers = [e.normalized_text for e in extraction.evidence if e.kind is K.TRANSCRIPT_ANSWER]
    assert questions == ["How is demand in exports?"]  # never the bare name line
    assert answers == [
        "Demand is strong and we plan to add capacity."
    ]  # Q&A starts after hand-over
    guidance = {e.normalized_text for e in extraction.evidence if e.kind is K.MANAGEMENT_GUIDANCE}
    assert "We expect margins to improve next year." in guidance


def test_alternating_facing_page_headers_form_one_section():
    pages = []
    for number in range(1, 7):
        side = (
            f"{number} | Corporate governance report"
            if number % 2 == 0
            else f"Corporate governance report | {number}"
        )
        pages.append([side, "Example Industries Annual Report 2025-26", f"Board meeting {number}."])
    extraction = extract(make_pdf(pages), T.ANNUAL_REPORT)
    running = [
        (s.kind, s.page_start, s.page_end)
        for s in extraction.sections
        if s.method == "RUNNING_HEADER"
    ]
    assert running == [(S.CORPORATE_GOVERNANCE, 1, 6)]


def test_unnumbered_note_words_do_not_open_note_sections():
    pdf = make_pdf(
        [
            [HEADER, MDA, "Related Party Matters", "The board reviews such matters every year."],
            [HEADER, MDA, "Market Overview", "Demand was steady."],
            [HEADER, MDA, "Our People", "Hiring continued."],
        ]
    )
    extraction = extract(pdf, T.ANNUAL_REPORT)
    assert S.RELATED_PARTY not in {s.kind for s in extraction.sections}
    assert all(e.section_kind is not S.RELATED_PARTY for e in extraction.evidence)


def test_table_title_basis_wins_and_numbered_headings_end_note_sections():
    consolidated = "Consolidated financial statements"
    rows = [
        "Revenue from operations 39,957 38,641",
        "Profit before tax 10,161 9,956",
        "Tax 2,912 2,575",
    ]
    pdf = make_pdf(
        [
            [HEADER, consolidated, "Audited financial results (Standalone Information)", *rows],
            [
                HEADER,
                consolidated,
                "8. Related party transactions",
                "Transactions were at arm's length.",
            ],
            [
                HEADER,
                consolidated,
                "9. Key Metrics",
                "Clients 2026 2025",
                "Active clients 1,965 1,869",
                "Large clients 41 39",
                "New clients 120 110",
            ],
            [
                HEADER,
                consolidated,
                "Client Segmentation Overview",
                "Clients 2026 2025",
                "100 million dollar + 41 39",
                "50 million dollar + 88 85",
                "10 million dollar + 328 309",
            ],
        ]
    )
    extraction = extract(pdf, T.ANNUAL_REPORT)
    by_page = {t.page_start: t for t in extraction.tables}
    assert by_page[1].statement_kind is FinancialStatementKind.FINANCIAL_RESULTS
    assert by_page[1].reporting_basis == "standalone"  # its own title beats the page header
    assert by_page[3].section_kind is not S.RELATED_PARTY  # '9. ...' ended the '8. ...' note
    assert by_page[4].statement_kind is None  # 'segmentation' is not segment reporting


def test_statement_titles_must_look_like_titles():
    rows = [
        "Within one year 1,200 1,100",
        "One to five years 3,400 3,000",
        "After five years 900 800",
    ]
    pdf = make_pdf(
        [
            [
                HEADER,
                FS,
                "Lease liabilities due over the remaining period as at the Balance Sheet date:",
                *rows,
            ],
            [
                HEADER,
                FS,
                "Particulars Note 2026 2025",
                "Assets 2.1 9,000 8,000",
                "Cash 2.2 1,000 900",
                "Total assets 10,000 8,900",
                "Standalone Balance Sheet as at March 31, 2026",
            ],
            [HEADER, FS, "Investments 300 200", "Inventories 120 100", "Receivables 450 400"],
        ]
    )
    extraction = extract(pdf, T.ANNUAL_REPORT)
    by_page = {t.page_start: t for t in extraction.tables}
    assert by_page[1].statement_kind is None  # prose mentioning a statement is not a title
    assert by_page[2].title == "Standalone Balance Sheet as at March 31, 2026"  # printed below
    assert by_page[2].statement_kind is FinancialStatementKind.BALANCE_SHEET
    assert by_page[2].reporting_basis == "standalone"


def test_only_the_first_table_on_a_page_can_continue_the_previous_page():
    block = ["Alpha 1,000 900", "Beta 2,000 1,800", "Gamma 3,000 2,700"]
    pdf = make_pdf(
        [
            [HEADER, FS, "Standalone Statement of Cash Flows", *block],
            [HEADER, FS, *block, "The following table relates to leases.", *block],
            [HEADER, FS, "Plain closing note."],
        ]
    )
    extraction = extract(pdf, T.ANNUAL_REPORT)
    first, second, third = extraction.tables
    assert (first.page_start, second.page_start, third.page_start) == (1, 2, 2)
    assert second.continuation_of == first.table_id
    assert second.statement_kind is FinancialStatementKind.CASH_FLOW
    assert third.continuation_of is None and third.statement_kind is None
