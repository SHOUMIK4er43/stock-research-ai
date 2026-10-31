"""DETAILED_RESEARCH_V1 Box 3: format parsers producing a format-neutral RawDocument.

PDF uses pypdf (already a project dependency) in its plain text mode: page-by-page,
1-based pages, the document's own page-label table as printed labels, per-page failure
isolation, and pypdf's font-warning logging suppressed while parsing. HTML reuses the
standard-library HTML tree already used for Screener. XLSX is read with zipfile + XML
(no DTDs accepted, streamed, every coordinate/cell/string count bounded). Legacy binary XLS
is reported as unsupported. `LocalDocumentParser` runs PDF/HTML/XLSX parsing in a
killable child process with hard time and memory bounds (`isolation`).
No OCR, no network, no AI. Warnings carry codes, never local paths or exception text.
"""

import contextlib
import logging
import re
import time
import zipfile
from collections.abc import Iterator
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

import pypdf
from pypdf import PdfReader

from stock_research_ai.adapters.documents.isolation import (
    DEFAULT_MEMORY_LIMIT_BYTES,
    IsolationFailure,
    IsolationLimits,
    run_isolated,
)
from stock_research_ai.adapters.screener.html import HtmlLimitExceeded, Node, Page
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
_MAX_XLSX_INDEX_PART = 10_000_000  # workbook.xml / relationships, read whole
_XML_CHUNK = 65_536
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
    try:
        root = Page(decoded).root
    except HtmlLimitExceeded:
        return _failed(fmt, parser, ExtractionStatus.TOO_LARGE, "HTML_STRUCTURE_LIMIT_REACHED")
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
        if block is not None and not any(
            c.tag in _BLOCK_TAGS or c.tag == "table" for c in node.descendants()
        ):
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


class _SpreadsheetTooLarge(ValueError):
    """A workbook index part exceeds its size bound."""


class XmlForbidden(ValueError):
    """The XML declares a DTD (and so could declare entities): never parsed further."""


def _reject_dtd(head: bytes) -> None:
    """Cheap early refusal of an obvious prolog DTD. Not the guard: a long prolog
    comment or a UTF-16 part slips past a byte sniff - `_GuardedBuilder` is the guard."""
    upper = head[:2048].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise XmlForbidden("DTD not allowed")


class _GuardedBuilder(ElementTree.TreeBuilder):
    """A TreeBuilder that refuses any DTD and records start/end events.

    expat reports `<!DOCTYPE` to `doctype()` before it reads the internal subset, so no
    entity is ever declared - internal expansion (billion laughs) and external
    file/network entities are impossible; an undeclared `&name;` is a ParseError. This is
    defusedxml's `forbid_dtd`, in the standard library, independent of encoding and of
    where in the prolog the DTD appears."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, ElementTree.Element]] = []

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise XmlForbidden("DTD not allowed")

    def start(self, tag: str, attrs: dict[str, str]) -> ElementTree.Element:
        element = super().start(tag, attrs)
        self.events.append(("start", element))
        return element

    def end(self, tag: str) -> ElementTree.Element:
        element = super().end(tag)
        self.events.append(("end", element))
        return element


def _guarded_parser() -> tuple[ElementTree.XMLParser, _GuardedBuilder]:
    builder = _GuardedBuilder()
    return ElementTree.XMLParser(target=builder), builder


def parse_untrusted_xml(data: bytes) -> ElementTree.Element:
    """Parse one whole untrusted XML document with DTDs (and so entities) forbidden."""
    _reject_dtd(data)
    parser, _ = _guarded_parser()
    parser.feed(data)
    root: ElementTree.Element = parser.close()
    return root


def _xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    """Read one small index part (workbook, relationships) whole."""
    if archive.getinfo(name).file_size > _MAX_XLSX_INDEX_PART:
        raise _SpreadsheetTooLarge(name)
    return parse_untrusted_xml(archive.read(name))


def _stream(archive: zipfile.ZipFile, name: str, tag: str) -> Iterator[ElementTree.Element]:
    """Yield each completed `tag` element of one part while streaming it. Every finished
    element is detached from its parent, so memory follows one element at a time - never
    the part size, and never a declared sheet dimension. Stopping early stops reading."""
    parser, builder = _guarded_parser()
    stack: list[ElementTree.Element] = []
    open_targets = 0  # `tag` elements on `stack`: O(1) per event, never a stack scan
    with archive.open(name) as handle:
        chunk = handle.read(2048)
        _reject_dtd(chunk)
        while chunk:
            parser.feed(chunk)
            events, builder.events = builder.events, []
            for event, element in events:
                if event == "start":
                    stack.append(element)
                    open_targets += element.tag == tag
                    continue
                stack.pop()
                if element.tag == tag:
                    open_targets -= 1
                    yield element
                elif open_targets:
                    continue  # still part of an unfinished target element
                if stack:
                    stack[-1].remove(element)
            chunk = handle.read(_XML_CHUNK)
        parser.close()


def _column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    value = 0
    for char in letters.group(0) if letters else "A":
        value = value * 26 + ord(char) - 64
    return value - 1


def parse_spreadsheet(content: bytes, limits: ExtractionLimits) -> RawDocument:
    """Bounded XLSX reading. Only cells actually present are read (streamed), and every
    attacker-controlled dimension is capped by `limits`: sheets, highest row and column
    coordinate, cells scanned, cells materialised (blank padding included), shared
    strings, and characters. Anything cut is a warning and the status becomes PARTIAL."""
    fmt, parser = DocumentFormat.SPREADSHEET, XLSX_PARSER
    if not content.startswith(b"PK"):
        return _failed(fmt, parser, ExtractionStatus.UNSUPPORTED, "UNSUPPORTED_SPREADSHEET")
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            if sum(i.file_size for i in archive.infolist()) > _MAX_XLSX_UNCOMPRESSED:
                return _failed(fmt, parser, ExtractionStatus.TOO_LARGE, "SPREADSHEET_TOO_LARGE")
            names = set(archive.namelist())
            document = RawDocument(
                format=fmt, status=ExtractionStatus.SUCCESS, parser=parser, parser_version=parser
            )

            def cut(code: str, page: int | None, detail: str | None = None) -> None:
                document.status = ExtractionStatus.PARTIAL
                document.warnings.append((code, page, detail))

            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_characters = 0
                for item in _stream(archive, "xl/sharedStrings.xml", f"{_MAIN_NS}si"):
                    if len(shared) >= limits.max_spreadsheet_shared_strings:
                        cut("SHARED_STRING_LIMIT_REACHED", None, "entries")
                        break
                    text = "".join(t.text or "" for t in item.iter(f"{_MAIN_NS}t"))
                    shared_characters += len(text)
                    if shared_characters > limits.max_characters:
                        cut("SHARED_STRING_LIMIT_REACHED", None, "characters")
                        break
                    shared.append(text)
            workbook = _xml(archive, "xl/workbook.xml")
            relations = {
                r.get("Id"): r.get("Target", "")
                for r in _xml(archive, "xl/_rels/workbook.xml.rels")
            }
            sheets = list(workbook.iter(f"{_MAIN_NS}sheet"))
            document.page_count = len(sheets)
            scanned = materialised = characters = 0
            exhausted = False  # a workbook-wide budget ran out: read no further sheets
            for number, sheet in enumerate(sheets[: limits.max_spreadsheet_sheets], start=1):
                target = relations.get(sheet.get(f"{_REL_NS}id"), "")
                path = "xl/" + target.lstrip("/").removeprefix("xl/")
                if path not in names:
                    document.warnings.append(("MISSING_SHEET", number, None))
                    continue
                grid: dict[int, dict[int, str]] = {}
                column_cut = False
                # Output characters this sheet will add, charged per stored cell (its text
                # plus one separator) BEFORE storing it: a shared string costs its full
                # length every time a cell references it.
                pending, over_budget = 0, False
                for cell in _stream(archive, path, f"{_MAIN_NS}c"):
                    scanned += 1
                    if scanned > limits.max_cells:
                        cut("SPREADSHEET_CELL_LIMIT_REACHED", number, "cells scanned")
                        exhausted = True
                        break
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
                    if row >= limits.max_spreadsheet_rows:
                        cut("SPREADSHEET_ROW_LIMIT_REACHED", number, None)
                        break
                    column = _column(reference)
                    if column >= limits.max_spreadsheet_columns:
                        if not column_cut:
                            cut("SPREADSHEET_COLUMN_LIMIT_REACHED", number, None)
                            column_cut = True
                        continue
                    pending += len(text) + 1
                    if characters + pending > limits.max_characters:
                        over_budget = True
                        break
                    grid.setdefault(row, {})[column] = text
                if over_budget:  # like an over-budget sheet below: not emitted, stop here
                    cut("CHARACTER_LIMIT_REACHED", number, None)
                    break
                rows_out: list[list[tuple[str, bool]]] = []
                header = min(grid) if grid else None
                for row in sorted(grid):
                    width = max(grid[row]) + 1
                    if materialised + width > limits.max_cells:
                        cut("SPREADSHEET_CELL_LIMIT_REACHED", number, "cells materialised")
                        exhausted = True
                        break
                    materialised += width
                    rows_out.append([(grid[row].get(c, ""), row == header) for c in range(width)])
                lines = ["\t".join(t for t, _ in r) for r in rows_out]
                text = "\n".join(lines) + ("\n" if lines else "")
                if characters + len(text) > limits.max_characters:
                    cut("CHARACTER_LIMIT_REACHED", number, None)
                    break
                characters += len(text)
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
                if exhausted:
                    break
            if len(sheets) > limits.max_spreadsheet_sheets:
                cut("SHEET_LIMIT_REACHED", None, f"{len(sheets)} sheets")
            return document
    except _SpreadsheetTooLarge:
        return _failed(fmt, parser, ExtractionStatus.TOO_LARGE, "SPREADSHEET_TOO_LARGE")
    except (zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError):
        return _failed(fmt, parser, ExtractionStatus.MALFORMED, "MALFORMED_SPREADSHEET")


def raw_document_to_json(document: RawDocument) -> dict[str, Any]:
    """Plain JSON for the parser child's reply - the parent never unpickles child data."""
    return {
        "format": document.format.value if document.format else None,
        "status": document.status.value,
        "parser": document.parser,
        "parser_version": document.parser_version,
        "page_count": document.page_count,
        "warnings": [list(w) for w in document.warnings],
        "pages": [
            {
                "number": page.number,
                "text": page.text,
                "printed_label": page.printed_label,
                "spans": None
                if page.spans is None
                else [[s.block_type.value, s.start, s.end] for s in page.spans],
                "tables": [
                    {
                        "title": t.title,
                        "rows": [[[text, header] for text, header in row] for row in t.rows],
                        "row_spans": [list(span) for span in t.row_spans],
                        "method": t.method,
                    }
                    for t in page.tables
                ],
                "warnings": [list(w) for w in page.warnings],
            }
            for page in document.pages
        ],
    }


def raw_document_from_json(data: dict[str, Any]) -> RawDocument:
    """Exact inverse of `raw_document_to_json` (tuples restored), so isolated parsing
    yields the same RawDocument as in-process parsing."""
    return RawDocument(
        format=DocumentFormat(data["format"]) if data["format"] else None,
        status=ExtractionStatus(data["status"]),
        parser=data["parser"],
        parser_version=data["parser_version"],
        page_count=data["page_count"],
        warnings=[(code, page, detail) for code, page, detail in data["warnings"]],
        pages=[
            RawPage(
                number=page["number"],
                text=page["text"],
                printed_label=page["printed_label"],
                spans=None
                if page["spans"] is None
                else [RawSpan(BlockType(kind), start, end) for kind, start, end in page["spans"]],
                tables=[
                    RawTable(
                        t["title"],
                        [[(text, header) for text, header in row] for row in t["rows"]],
                        [(start, end) for start, end in t["row_spans"]],
                        t["method"],
                    )
                    for t in page["tables"]
                ],
                warnings=[(code, detail) for code, detail in page["warnings"]],
            )
            for page in data["pages"]
        ],
    )


def _parse_in_child(content: bytes, detected: str, limits: dict[str, Any]) -> dict[str, Any]:
    """Runs inside the isolated child; arguments arrive as JSON."""
    return raw_document_to_json(
        parse_document(
            content, DetectedContentType(detected), ExtractionLimits.model_validate(limits)
        )
    )


#: Formats whose parsers face untrusted structure (and pypdf / tree building) and so
#: always run isolated. Plain text is a bounded decode and stays in-process.
_ISOLATED: dict[DetectedContentType, tuple[DocumentFormat, str]] = {
    DetectedContentType.PDF: (DocumentFormat.PDF, PDF_PARSER),
    DetectedContentType.HTML: (DocumentFormat.HTML, HTML_PARSER),
    DetectedContentType.SPREADSHEET: (DocumentFormat.SPREADSHEET, XLSX_PARSER),
}
#: Hard kill this long after `limits.max_seconds`, so the parser's own soft time check
#: can still return a PARTIAL document first.
HARD_DEADLINE_GRACE_SECONDS = 15.0
_RESOURCE_FAILURES = {IsolationFailure.MEMORY_LIMIT, IsolationFailure.OUTPUT_TOO_LARGE}


class LocalDocumentParser:
    """DocumentParser over the local, dependency-light parsers above. PDF, HTML and XLSX
    are parsed in a killable child process (see `isolation`)."""

    def __init__(
        self,
        *,
        memory_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES,
        grace_seconds: float = HARD_DEADLINE_GRACE_SECONDS,
    ) -> None:
        self._memory_bytes = memory_bytes
        self._grace_seconds = grace_seconds

    def parse(
        self, content: bytes, detected: DetectedContentType, limits: ExtractionLimits
    ) -> RawDocument:
        isolated = _ISOLATED.get(detected)
        if isolated is None:
            return parse_document(content, detected, limits)
        result = run_isolated(
            _parse_in_child,
            content,
            detected.value,
            limits.model_dump(mode="json"),
            limits=IsolationLimits(
                seconds=limits.max_seconds + self._grace_seconds,
                memory_bytes=self._memory_bytes,
            ),
        )
        if result.failure is not None:
            fmt, parser = isolated
            status = (
                ExtractionStatus.TOO_LARGE
                if result.failure in _RESOURCE_FAILURES
                else ExtractionStatus.FAILED
            )
            return _failed(fmt, parser, status, result.failure.value)
        return raw_document_from_json(result.value)
