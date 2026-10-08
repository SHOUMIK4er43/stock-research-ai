"""P1-1: hostile XLSX cannot amplify memory or CPU. Every attacker-controlled dimension
(declared range, coordinates, cells, shared strings, sheets, characters) is bounded and
reported; ordinary workbooks are unchanged. Offline, synthetic, in-memory only."""

import io
import time
import tracemalloc
import zipfile

import pytest

from stock_research_ai.adapters.documents import document_parsers
from stock_research_ai.adapters.documents.document_parsers import (
    LocalDocumentParser,
    parse_spreadsheet,
)
from stock_research_ai.domain.document_intelligence import ExtractionLimits, ExtractionStatus
from stock_research_ai.domain.evidence_retrieval import DetectedContentType

NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
REL = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
LIMITS = ExtractionLimits()


def workbook(sheets: list[str], shared: list[str] | None = None, prolog: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        entries = "".join(
            f'<sheet name="S{i}" sheetId="{i}" r:id="rId{i}"/>' for i in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "xl/workbook.xml", f"<workbook {NS} {REL}><sheets>{entries}</sheets></workbook>"
        )
        relations = "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{relations}</Relationships>",
        )
        if shared is not None:
            items = "".join(f"<si><t>{s}</t></si>" for s in shared)
            archive.writestr("xl/sharedStrings.xml", f"<sst {NS}>{items}</sst>")
        for i, body in enumerate(sheets, 1):
            archive.writestr(
                f"xl/worksheets/sheet{i}.xml", f"{prolog}<worksheet {NS}>{body}</worksheet>"
            )
    return buffer.getvalue()


def cell(ref: str, text: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def rows(*cells: str) -> str:
    return f"<sheetData>{''.join(cells)}</sheetData>"


def materialised(document) -> int:
    return sum(len(r) for page in document.pages for t in page.tables for r in t.rows)


def codes(document) -> set[str]:
    return {code for code, _, _ in document.warnings}


ORDINARY = workbook(
    [
        rows(
            '<row r="1">' + cell("A1", "Particulars") + cell("B1", "FY26") + "</row>",
            '<row r="2">' + cell("A2", "Revenue") + '<c r="B2"><v>1234.5</v></c></row>',
            '<row r="3">' + cell("A3", "Net profit") + '<c r="C3"><v>-12.3</v></c></row>',
        )
    ]
)


def test_ordinary_workbook_is_unchanged_and_successful():
    document = parse_spreadsheet(ORDINARY, LIMITS)
    assert document.status is ExtractionStatus.SUCCESS and document.warnings == []
    (table,) = document.pages[0].tables
    assert table.rows == [
        [("Particulars", True), ("FY26", True)],
        [("Revenue", False), ("1234.5", False)],
        [("Net profit", False), ("", False), ("-12.3", False)],
    ]
    assert document.pages[0].text == "Particulars\tFY26\nRevenue\t1234.5\nNet profit\t\t-12.3\n"


def test_huge_declared_dimension_allocates_nothing():
    """The audit fixture shape: tiny file declaring A1:XFD1048576."""
    content = workbook(['<dimension ref="A1:XFD1048576"/>' + rows(cell("A1", "x"))])
    assert len(content) < 1_000
    document = parse_spreadsheet(content, LIMITS)
    assert document.status is ExtractionStatus.SUCCESS
    assert materialised(document) == 1


def test_sparse_far_column_is_cut_not_padded():
    """Before P1 one XFD cell per row padded each row to 16,384 cells (an 8 KB file
    became 16.4M cells / ~1 GB). Now the far cell is dropped with a warning."""
    body = "".join(
        f'<row r="{i}">{cell(f"A{i}", "x")}{cell(f"XFD{i}", "y")}</row>' for i in range(1, 1001)
    )
    content = workbook(['<dimension ref="A1:XFD1048576"/>' + rows(body)])
    assert len(content) < 20_000
    document = parse_spreadsheet(content, LIMITS)
    assert document.status is ExtractionStatus.PARTIAL
    assert "SPREADSHEET_COLUMN_LIMIT_REACHED" in codes(document)
    assert materialised(document) == 1000  # one real cell per row, no padding to XFD
    assert max(len(r) for r in document.pages[0].tables[0].rows) == 1


def test_sparse_far_row_is_cut():
    content = workbook([rows(cell("A1", "x"), cell("A1048576", "far"))])
    document = parse_spreadsheet(content, LIMITS)
    assert document.status is ExtractionStatus.PARTIAL
    assert "SPREADSHEET_ROW_LIMIT_REACHED" in codes(document)
    assert materialised(document) == 1


def test_too_many_rows():
    limits = ExtractionLimits(max_spreadsheet_rows=50)
    content = workbook([rows(*(cell(f"A{i}", str(i)) for i in range(1, 101)))])
    document = parse_spreadsheet(content, limits)
    assert document.status is ExtractionStatus.PARTIAL
    assert "SPREADSHEET_ROW_LIMIT_REACHED" in codes(document)
    assert len(document.pages[0].tables[0].rows) == 50


def test_too_many_columns():
    limits = ExtractionLimits(max_spreadsheet_columns=10)
    content = workbook([rows(cell("A1", "a"), cell("J1", "j"), cell("K1", "k"), cell("Z1", "z"))])
    document = parse_spreadsheet(content, limits)
    assert document.status is ExtractionStatus.PARTIAL
    assert [w for w in document.warnings if w[0] == "SPREADSHEET_COLUMN_LIMIT_REACHED"] == [
        ("SPREADSHEET_COLUMN_LIMIT_REACHED", 1, None)
    ]  # reported once per sheet, not once per cell
    assert len(document.pages[0].tables[0].rows[0]) == 10


def test_too_many_actual_cells_stops_scanning_early(monkeypatch):
    limits = ExtractionLimits(max_cells=100)
    content = workbook([rows(*(cell(f"A{i}", "x") for i in range(1, 5001)))])
    scanned = 0
    original = document_parsers._stream

    def counting(*args):
        nonlocal scanned
        for element in original(*args):
            scanned += 1
            yield element

    monkeypatch.setattr(document_parsers, "_stream", counting)
    document = parse_spreadsheet(content, limits)
    assert document.status is ExtractionStatus.PARTIAL
    assert "SPREADSHEET_CELL_LIMIT_REACHED" in codes(document)
    assert scanned == 101  # stopped at the first cell over budget, not after 5,000
    assert materialised(document) == 100


def test_padding_counts_against_the_cell_budget():
    limits = ExtractionLimits(max_cells=50, max_spreadsheet_columns=1_024)
    body = "".join(f'<row r="{i}">{cell(f"Z{i}", "z")}</row>' for i in range(1, 5))  # 26 wide
    document = parse_spreadsheet(workbook([rows(body)]), limits)
    assert document.status is ExtractionStatus.PARTIAL
    assert ("SPREADSHEET_CELL_LIMIT_REACHED", 1, "cells materialised") in document.warnings
    assert materialised(document) <= 50


def test_oversized_shared_strings_are_bounded():
    limits = ExtractionLimits(max_spreadsheet_shared_strings=10)
    content = workbook(
        [rows('<row r="1"><c r="A1" t="s"><v>3</v></c><c r="B1" t="s"><v>500</v></c></row>')],
        shared=[f"s{i}" for i in range(1000)],
    )
    document = parse_spreadsheet(content, limits)
    assert document.status is ExtractionStatus.PARTIAL
    assert ("SHARED_STRING_LIMIT_REACHED", None, "entries") in document.warnings
    assert document.pages[0].tables[0].rows[0] == [("s3", True), ("", True)]


def test_shared_string_characters_are_bounded():
    limits = ExtractionLimits(max_characters=1_000)
    content = workbook([rows(cell("A1", "x"))], shared=["y" * 600, "z" * 600])
    document = parse_spreadsheet(content, limits)
    assert ("SHARED_STRING_LIMIT_REACHED", None, "characters") in document.warnings
    assert document.status is ExtractionStatus.PARTIAL


def test_excessive_worksheet_count():
    content = workbook([rows(cell("A1", str(i))) for i in range(30)])
    document = parse_spreadsheet(content, LIMITS)
    assert document.status is ExtractionStatus.PARTIAL
    assert ("SHEET_LIMIT_REACHED", None, "30 sheets") in document.warnings
    assert document.page_count == 30 and len(document.pages) == LIMITS.max_spreadsheet_sheets


def test_extracted_characters_are_bounded():
    limits = ExtractionLimits(max_characters=500)
    sheets = [rows(*(cell(f"A{r}", "w" * 50) for r in range(1, 5))) for _ in range(5)]
    document = parse_spreadsheet(workbook(sheets), limits)
    assert document.status is ExtractionStatus.PARTIAL
    assert "CHARACTER_LIMIT_REACHED" in codes(document)
    assert sum(len(p.text) for p in document.pages) <= 500


def test_bounded_parsing_is_deterministic_in_and_out_of_process():
    body = "".join(
        f'<row r="{i}">{cell(f"A{i}", "x")}{cell(f"XFD{i}", "y")}</row>' for i in range(1, 300)
    )
    content = workbook([rows(body), rows(cell("B2", "b"))], shared=["a"])
    first = parse_spreadsheet(content, LIMITS)
    assert first == parse_spreadsheet(content, LIMITS)
    isolated = LocalDocumentParser().parse(content, DetectedContentType.SPREADSHEET, LIMITS)
    assert isolated == first


def test_audit_fixture_has_no_memory_amplification():
    """Structural bounds above are the guarantee; this checks the outcome too: the
    1,000-row audit-style workbook stays far below the >100 MB it used to need."""
    body = "".join(
        f'<row r="{i}">{cell(f"A{i}", "x")}{cell(f"XFD{i}", "y")}</row>' for i in range(1, 1001)
    )
    content = workbook(['<dimension ref="A1:XFD1048576"/>' + rows(body)])
    tracemalloc.start()
    try:
        parse_spreadsheet(content, LIMITS)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 10_000_000


@pytest.mark.parametrize("prolog", ["<!DOCTYPE x>", '<!DOCTYPE x [<!ENTITY a "b">]>'])
def test_streamed_parts_still_reject_dtds(prolog):
    document = parse_spreadsheet(workbook([rows(cell("A1", "x"))], prolog=prolog), LIMITS)
    assert document.status is ExtractionStatus.MALFORMED
    assert codes(document) == {"MALFORMED_SPREADSHEET"}


# --- P1 re-audit R2: characters are charged before a cell is stored ---------------------


def shared_refs(count: int, start: int = 1) -> str:
    """`count` rows, each one cell referencing shared string 0."""
    return rows(
        *(f'<row r="{i}"><c r="A{i}" t="s"><v>0</v></c></row>' for i in range(start, start + count))
    )


BIG = "s" * 1_000  # each reference renders as 1,000 characters plus a newline


@pytest.mark.parametrize("references", [1, 5, 10])
def test_repeated_shared_string_within_budget_is_complete(references):
    limits = ExtractionLimits(max_characters=10 * 1_001)  # exactly 10 references
    document = parse_spreadsheet(workbook([shared_refs(references)], shared=[BIG]), limits)
    assert document.status is ExtractionStatus.SUCCESS and document.warnings == []
    assert document.pages[0].text == (BIG + "\n") * references
    assert len(document.pages[0].text) <= limits.max_characters


def test_each_reference_is_charged_so_the_next_one_is_cut():
    limits = ExtractionLimits(max_characters=10 * 1_001)
    document = parse_spreadsheet(workbook([shared_refs(11)], shared=[BIG]), limits)
    assert document.status is ExtractionStatus.PARTIAL
    assert document.warnings == [("CHARACTER_LIMIT_REACHED", 1, None)]
    assert document.pages == []  # an over-budget sheet is never emitted, as before


def test_budget_spans_sheets():
    limits = ExtractionLimits(max_characters=10 * 1_001)
    content = workbook([shared_refs(6), shared_refs(5)], shared=[BIG])
    document = parse_spreadsheet(content, limits)
    assert [p.number for p in document.pages] == [1]
    assert document.warnings == [("CHARACTER_LIMIT_REACHED", 2, None)]


def test_many_references_to_one_large_string_do_not_amplify_memory():
    # ~40 KB compressed; previously the grid and joined text reached ~160 MB before the
    # character check. Now scanning stops at the first reference past the budget.
    limits = ExtractionLimits(max_characters=100_000)
    content = workbook([shared_refs(8_000)], shared=["s" * 10_000])
    tracemalloc.start()
    try:
        document = parse_spreadsheet(content, limits)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert document.status is ExtractionStatus.PARTIAL
    assert ("CHARACTER_LIMIT_REACHED", 1, None) in document.warnings
    assert peak < 5_000_000


def test_inline_and_numeric_values_are_charged_too():
    limits = ExtractionLimits(max_characters=20)
    body = rows('<row r="1">' + cell("A1", "abcdefghij") + '<c r="B1"><v>1234567890</v></c></row>')
    document = parse_spreadsheet(workbook([body]), limits)
    assert ("CHARACTER_LIMIT_REACHED", 1, None) in document.warnings  # 10+1 + 10+1 > 20


# --- P1 re-audit R3: streaming is linear in nesting depth -----------------------------------


def test_deep_irrelevant_nesting_streams_linearly():
    depth = 50_000  # the old per-event stack scan needed tens of seconds here
    content = workbook(["<x>" * depth + "</x>" * depth + rows(cell("A1", "a"))])
    started = time.monotonic()
    document = parse_spreadsheet(content, LIMITS)
    assert time.monotonic() - started < 5.0
    assert document.status is ExtractionStatus.SUCCESS
    assert document.pages[0].text == "a\n"
    assert parse_spreadsheet(content, LIMITS) == document  # deterministic


def test_deep_nesting_inside_a_target_cell_is_kept_with_the_cell():
    depth = 20_000
    inner = "<x>" * depth + "<t>deep</t>" + "</x>" * depth
    target = f'<c r="A1" t="inlineStr"><is>{inner}</is></c>'
    body = rows(f'<row r="1">{target}{cell("B1", "b")}</row>')
    started = time.monotonic()
    document = parse_spreadsheet(workbook([body]), LIMITS)
    assert time.monotonic() - started < 5.0
    assert document.pages[0].tables[0].rows == [[("deep", True), ("b", True)]]


def test_ordinary_shared_strings_and_nested_targets_stream_as_before():
    content = workbook(
        [rows('<row r="1"><c r="A1" t="s"><v>1</v></c><c r="B1" t="s"><v>0</v></c></row>')],
        shared=["zero", "one"],
    )
    document = parse_spreadsheet(content, LIMITS)
    assert document.status is ExtractionStatus.SUCCESS
    assert document.pages[0].tables[0].rows == [[("one", True), ("zero", True)]]


def test_malformed_sheet_xml_is_still_malformed():
    content = workbook([rows(cell("A1", "x")) + "<unclosed>"])
    document = parse_spreadsheet(content, LIMITS)
    assert document.status is ExtractionStatus.MALFORMED
    assert codes(document) == {"MALFORMED_SPREADSHEET"}
