"""DETAILED_RESEARCH_V1 Box 3: format parsers producing a format-neutral RawDocument.

PDF uses pypdf (already a project dependency) in its plain text mode: page-by-page,
1-based pages, the document's own page-label table as printed labels, per-page failure
isolation, and pypdf's font-warning logging suppressed while parsing. HTML reuses the
standard-library HTML tree already used for Screener. XLSX is read with zipfile + XML
(no DTDs accepted, bounded sizes). Legacy binary XLS is reported as unsupported.
No OCR, no network, no AI. Warnings carry codes, never local paths or exception text.
"""

import contextlib
import logging
import re
import time
import zipfile
from collections.abc import Iterator
from io import BytesIO
from xml.etree import ElementTree

import pypdf
from pypdf import PdfReader

from stock_research_ai.adapters.screener.html import Node, Page
from stock_research_ai.application.document_structure import (
    RawDocument,
    RawPage,
    RawSpan,
    RawTable,
)
from stock_research_ai.domain.document_intelligence import (
    BlockType,
    DocumentFormat,
    ExtractionLimits,
    ExtractionStatus,
)
from stock_research_ai.domain.evidence_retrieval import DetectedContentType

PDF_PARSER = f"pypdf/{pypdf.__version__}/plain-text"
HTML_PARSER = "stdlib-html/1"
TEXT_PARSER = "utf8-text/1"
XLSX_PARSER = "stdlib-xlsx/1"
_MAX_XLSX_UNCOMPRESSED = 200_000_000
_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@contextlib.contextmanager
def _quiet_pypdf() -> Iterator[None]:
    logger = logging.getLogger("pypdf")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


def parse_document(
    content: bytes, detected: DetectedContentType, limits: ExtractionLimits
) -> RawDocument:
    if detected == DetectedContentType.PDF:
        return parse_pdf(content, limits)
    if detected == DetectedContentType.HTML:
        return parse_html(content, limits)
    if detected == DetectedContentType.TEXT:
        return parse_text(content, limits)
    if detected == DetectedContentType.SPREADSHEET:
        return parse_spreadsheet(content, limits)
    return RawDocument(
        format=None,
        status=ExtractionStatus.UNSUPPORTED,
        parser="none",
        parser_version="none",
        warnings=[("UNSUPPORTED_FORMAT", None, detected.value)],
    )


def _failed(fmt: DocumentFormat, parser: str, status: ExtractionStatus, code: str) -> RawDocument:
    return RawDocument(
        format=fmt,
        status=status,
        parser=parser,
        parser_version=parser,
        warnings=[(code, None, None)],
    )


def parse_pdf(content: bytes, limits: ExtractionLimits) -> RawDocument:
    fmt, parser = DocumentFormat.PDF, PDF_PARSER
    if not content:
        return _failed(fmt, parser, ExtractionStatus.MALFORMED, "EMPTY_DOCUMENT")
    if not content.startswith(b"%PDF-"):
        return _failed(fmt, parser, ExtractionStatus.MALFORMED, "NOT_A_PDF")
    started = time.monotonic()
    with _quiet_pypdf():
        try:
            reader = PdfReader(BytesIO(content), strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                return _failed(fmt, parser, ExtractionStatus.ENCRYPTED, "PDF_ENCRYPTED")
            count = len(reader.pages)
        except Exception:  # pypdf raises many types on damaged input
            return _failed(fmt, parser, ExtractionStatus.MALFORMED, "MALFORMED_PDF")
        if count == 0:
            return _failed(fmt, parser, ExtractionStatus.MALFORMED, "EMPTY_DOCUMENT")
        labels: list[str] | None = None
        try:
            if "/PageLabels" in reader.root_object:
                labels = list(reader.page_labels)
        except Exception:
            labels = None
        document = RawDocument(
            format=fmt,
            status=ExtractionStatus.SUCCESS,
            parser=parser,
            parser_version=parser,
            page_count=count,
        )
        characters = 0
        for index in range(min(count, limits.max_pages)):
            if time.monotonic() - started > limits.max_seconds:
                document.status = ExtractionStatus.PARTIAL
                document.warnings.append(("TIME_LIMIT_REACHED", index + 1, None))
                break
            warnings: list[tuple[str, str | None]] = []
            try:
                text = reader.pages[index].extract_text() or ""
            except Exception:
                text = ""
                warnings.append(("MALFORMED_PAGE", None))
            text = text.replace("\x00", "")
            if characters + len(text) > limits.max_characters:
                document.status = ExtractionStatus.PARTIAL
                document.warnings.append(("CHARACTER_LIMIT_REACHED", index + 1, None))
                break
            characters += len(text)
            if not text.strip():
                warnings.append(("NO_TEXT_ON_PAGE", "OCR required; not attempted"))
            document.pages.append(
                RawPage(
                    number=index + 1,  # pypdf is 0-based; the research domain is 1-based
                    text=text,
                    printed_label=labels[index] if labels and index < len(labels) else None,
                    warnings=warnings,
                )
            )
        if count > limits.max_pages:
            document.status = ExtractionStatus.PARTIAL
            document.warnings.append(("PAGE_LIMIT_REACHED", None, f"{count} pages"))
        return document


_BLOCK_TAGS = {
    "h1": BlockType.HEADING,
    "h2": BlockType.HEADING,
    "h3": BlockType.HEADING,
    "h4": BlockType.HEADING,
    "h5": BlockType.HEADING,
    "h6": BlockType.HEADING,
    "title": BlockType.TITLE,
    "p": BlockType.PARAGRAPH,
    "li": BlockType.LIST,
    "blockquote": BlockType.PARAGRAPH,
    "pre": BlockType.PARAGRAPH,
    "div": BlockType.PARAGRAPH,
    "section": BlockType.PARAGRAPH,
    "caption": BlockType.TABLE_CAPTION,
}
_SKIP_TAGS = {"script", "style", "nav", "noscript", "head", "footer", "header", "form"}


def parse_html(content: bytes, limits: ExtractionLimits) -> RawDocument:
    fmt, parser = DocumentFormat.HTML, HTML_PARSER
    if not content.strip():
        return _failed(fmt, parser, ExtractionStatus.MALFORMED, "EMPTY_DOCUMENT")
    warnings: list[tuple[str, int | None, str | None]] = []
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        decoded = content.decode("cp1252", errors="replace")
        warnings.append(("HTML_NOT_UTF8", 1, "decoded as cp1252"))
    root = Page(decoded).root
    parts: list[str] = []
    spans: list[RawSpan] = []
    tables: list[RawTable] = []
    offset = 0

    def append(text: str) -> tuple[int, int]:
        nonlocal offset
        start = offset
        parts.append(text + "\n")
        offset += len(text) + 1
        return start, start + len(text)

    def visit(node: Node) -> None:
        if node.tag in _SKIP_TAGS and node.tag != "title":
            return
        if node.tag == "table":
            rows: list[list[tuple[str, bool]]] = []
            row_spans: list[tuple[int, int]] = []
            caption = next((c.text for c in node.all("caption") if c.text), None)
            for tr in node.all("tr"):
                cells = [
                    (c.text, c.tag == "th")
                    for c in tr.children
                    if isinstance(c, Node) and c.tag in ("td", "th")
                ]
                if cells:
                    rows.append(cells)
                    row_spans.append(append(" | ".join(text for text, _ in cells)))
            if rows:
                tables.append(RawTable(caption, rows, row_spans, "html-table"))
            return
        block = _BLOCK_TAGS.get(node.tag)
        has_block_children = any(
            isinstance(c, Node) and (c.tag in _BLOCK_TAGS or c.tag == "table") for c in node.all()
        )
        if block is not None and not has_block_children:
            text = node.text
            if text:
                start, end = append(text)
                spans.append(RawSpan(block, start, end))
            return
        for child in node.children:
            if isinstance(child, Node):
                visit(child)

    title = root.all("title")
    if title and title[0].text:
        start, end = append(title[0].text)
        spans.append(RawSpan(BlockType.TITLE, start, end))
    bodies = root.all("body")
    visit(bodies[0] if bodies else root)
    text = "".join(parts)
    if len(text) > limits.max_characters:
        return _failed(fmt, parser, ExtractionStatus.TOO_LARGE, "CHARACTER_LIMIT_REACHED")
    return RawDocument(
        format=fmt,
        status=ExtractionStatus.SUCCESS,
        parser=parser,
        parser_version=parser,
        page_count=1,
        pages=[RawPage(number=1, text=text, spans=spans, tables=tables)],
        warnings=warnings,
    )


def parse_text(content: bytes, limits: ExtractionLimits) -> RawDocument:
    fmt, parser = DocumentFormat.TEXT, TEXT_PARSER
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _failed(fmt, parser, ExtractionStatus.MALFORMED, "TEXT_NOT_UTF8")
    if not text.strip():
        return _failed(fmt, parser, ExtractionStatus.MALFORMED, "EMPTY_DOCUMENT")
    if len(text) > limits.max_characters:
        return _failed(fmt, parser, ExtractionStatus.TOO_LARGE, "CHARACTER_LIMIT_REACHED")
    return RawDocument(
        format=fmt,
        status=ExtractionStatus.SUCCESS,
        parser=parser,
        parser_version=parser,
        page_count=1,
        pages=[RawPage(number=1, text=text)],
    )


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    data = archive.read(name)
    if b"<!DOCTYPE" in data[:2048].upper() or b"<!ENTITY" in data[:2048].upper():
        raise ValueError("DTD not allowed")
    return ElementTree.fromstring(data)


def _column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    value = 0
    for char in letters.group(0) if letters else "A":
        value = value * 26 + ord(char) - 64
    return value - 1


def parse_spreadsheet(content: bytes, limits: ExtractionLimits) -> RawDocument:
    fmt, parser = DocumentFormat.SPREADSHEET, XLSX_PARSER
    if not content.startswith(b"PK"):
        return _failed(fmt, parser, ExtractionStatus.UNSUPPORTED, "UNSUPPORTED_SPREADSHEET")
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if sum(i.file_size for i in archive.infolist()) > _MAX_XLSX_UNCOMPRESSED:
                return _failed(fmt, parser, ExtractionStatus.TOO_LARGE, "SPREADSHEET_TOO_LARGE")
            names = set(archive.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                for item in _xml(archive, "xl/sharedStrings.xml").iter(f"{_MAIN_NS}si"):
                    shared.append("".join(t.text or "" for t in item.iter(f"{_MAIN_NS}t")))
            workbook = _xml(archive, "xl/workbook.xml")
            relations = {
                r.get("Id"): r.get("Target", "")
                for r in _xml(archive, "xl/_rels/workbook.xml.rels")
            }
            document = RawDocument(
                format=fmt, status=ExtractionStatus.SUCCESS, parser=parser, parser_version=parser
            )
            sheets = list(workbook.iter(f"{_MAIN_NS}sheet"))
            document.page_count = len(sheets)
            for number, sheet in enumerate(sheets[: limits.max_spreadsheet_sheets], start=1):
                target = relations.get(sheet.get(f"{_REL_NS}id"), "")
                path = "xl/" + target.lstrip("/").removeprefix("xl/")
                if path not in names:
                    document.warnings.append(("MISSING_SHEET", number, None))
                    continue
                grid: dict[int, dict[int, str]] = {}
                for cell in _xml(archive, path).iter(f"{_MAIN_NS}c"):
                    value = cell.find(f"{_MAIN_NS}v")
                    kind = cell.get("t")
                    if kind == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter(f"{_MAIN_NS}t"))
                    elif value is None or value.text is None:
                        continue
                    elif kind == "s":
                        text = (
                            shared[int(value.text)]
                            if value.text.isdigit() and int(value.text) < len(shared)
                            else ""
                        )
                    else:
                        text = value.text  # the stored value, exactly as written
                    reference = cell.get("r", "A1")
                    row = int(re.sub(r"[A-Z]+", "", reference) or "1") - 1
                    grid.setdefault(row, {})[_column(reference)] = text
                rows_out: list[list[tuple[str, bool]]] = []
                for row in sorted(grid):
                    width = max(grid[row]) + 1
                    rows_out.append(
                        [(grid[row].get(c, ""), row == min(grid)) for c in range(width)]
                    )
                lines = ["\t".join(t for t, _ in r) for r in rows_out]
                text = "\n".join(lines) + ("\n" if lines else "")
                spans, offset = [], 0
                for line in lines:
                    spans.append((offset, offset + len(line)))
                    offset += len(line) + 1
                document.pages.append(
                    RawPage(
                        number=number,
                        text=text,
                        printed_label=sheet.get("name"),
                        spans=[],
                        tables=[RawTable(sheet.get("name"), rows_out, spans, "xlsx-sheet")]
                        if rows_out
                        else [],
                    )
                )
            if len(sheets) > limits.max_spreadsheet_sheets:
                document.status = ExtractionStatus.PARTIAL
                document.warnings.append(("SHEET_LIMIT_REACHED", None, f"{len(sheets)} sheets"))
            return document
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError):
        return _failed(fmt, parser, ExtractionStatus.MALFORMED, "MALFORMED_SPREADSHEET")


class LocalDocumentParser:
    """DocumentParser over the local, dependency-light parsers above."""

    def parse(
        self, content: bytes, detected: DetectedContentType, limits: ExtractionLimits
    ) -> RawDocument:
        return parse_document(content, detected, limits)
