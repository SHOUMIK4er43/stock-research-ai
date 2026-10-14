"""DETAILED_RESEARCH_V1 Box 15: the renderer-neutral presentation model and display policy.

Box 15 renders the Box 14 FinalResearchReportDataset; it never researches. The flow is

    FinalResearchReportDataset -> RenderDocument (this module's model, built once by
    application.report_presentation) -> Markdown / HTML / PDF

so no renderer re-reads the dataset, re-derives a number or re-orders a section. JSON is the
Box 14 dataset itself, serialised. Rendered Markdown / HTML is never parsed back into data.

Display rounding (the ONE policy every renderer uses; underlying values never change):
  * every number is shown with 2 decimals, rounded half-up, with ',' thousands grouping
    (e.g. 13.086771 -> 13.09, -4064 -> -4,064.00); COUNT values that are whole numbers are
    shown without decimals (107);
  * a non-zero value that would round to 0.00 is shown as '<0.01' / '>-0.01', never as a
    zero;
  * the minus sign is always kept and always ASCII '-';
  * values compared against a threshold (risk register, view-change / monitoring thresholds,
    source-disagreement observations) are shown EXACTLY as Box 14 carries them, so rounding
    can never move a value across a threshold;
  * unit suffixes: PERCENT -> '%', PERCENTAGE_POINTS -> ' pp', TIMES -> 'x'; INR crore units
    -> '₹ ... Cr'; INR per-share units -> '₹ .../share'; any other unit is printed verbatim
    after the number (Box 15 never infers a scale Box 14 did not state).
The exact value always remains in report.json.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, NonNegativeInt, StringConstraints

RENDERER_VERSION = "report-renderer/1"
ORCHESTRATOR_VERSION = "report-orchestrator/1"
MANIFEST_SCHEMA = "FINAL_REPORT_MANIFEST_V1"
STAGE_SCHEMA = "DETAILED_RESEARCH_STAGE_V1"

DISCLAIMER = (
    "Research and information only. This report is not investment advice and makes no "
    "recommendation or rating of any kind and sets no price objective. Every figure is "
    "copied from the cited upstream records; forecasts and valuations are scenarios, not "
    "predictions."
)

# --- resource bounds ------------------------------------------------------------------------

MAX_MARKDOWN_BYTES = 48 * 1024 * 1024
MAX_HTML_BYTES = 64 * 1024 * 1024
MAX_PDF_BYTES = 128 * 1024 * 1024
MAX_PDF_PAGES = 3_000
MAX_PDF_SECONDS = 600.0
#: table cells a text renderer (Markdown / HTML) will lay out; above it the format fails
#: explicitly (RENDER_LIMIT_EXCEEDED) instead of silently dropping rows.
MAX_TEXT_CELLS = 600_000
#: table cells the PDF engine will lay out.
MAX_PDF_CELLS = 150_000
#: rows per laid-out PDF table chunk (each chunk repeats its header row)
PDF_ROWS_PER_CHUNK = 150
#: source references printed next to one claim; the remainder is counted, never hidden
MAX_REFS_SHOWN = 6


class RenderFormat(StrEnum):
    JSON = "JSON"
    MARKDOWN = "MARKDOWN"
    HTML = "HTML"
    PDF = "PDF"


ALL_FORMATS: tuple[RenderFormat, ...] = tuple(RenderFormat)

FILE_NAMES: dict[RenderFormat, str] = {
    RenderFormat.JSON: "report.json",
    RenderFormat.MARKDOWN: "report.md",
    RenderFormat.HTML: "report.html",
    RenderFormat.PDF: "report.pdf",
}

MEDIA_TYPES: dict[RenderFormat, str] = {
    RenderFormat.JSON: "application/json",
    RenderFormat.MARKDOWN: "text/markdown; charset=utf-8",
    RenderFormat.HTML: "text/html; charset=utf-8",
    RenderFormat.PDF: "application/pdf",
}

EXTENSIONS: dict[RenderFormat, str] = {
    RenderFormat.JSON: "json",
    RenderFormat.MARKDOWN: "md",
    RenderFormat.HTML: "html",
    RenderFormat.PDF: "pdf",
}


class FailureCode(StrEnum):
    INPUT_ERROR = "INPUT_ERROR"
    UPSTREAM_RESEARCH_FAILED = "UPSTREAM_RESEARCH_FAILED"
    REPORT_COMPOSITION_FAILED = "REPORT_COMPOSITION_FAILED"
    RENDER_FAILED = "RENDER_FAILED"
    PARTIAL_RENDER = "PARTIAL_RENDER"
    OUTPUT_IO_FAILED = "OUTPUT_IO_FAILED"
    #: the declared NSE symbol / BSE code / ISIN is not the acquired Screener company's
    COMPANY_IDENTITY_MISMATCH = "COMPANY_IDENTITY_MISMATCH"
    #: no Screener company page (consolidated or standalone) could be acquired
    SOURCE_ACQUISITION_FAILED = "SOURCE_ACQUISITION_FAILED"
    #: a product generation job did not finish within its time bound
    GENERATION_TIMEOUT = "GENERATION_TIMEOUT"


class RenderError(Exception):
    """A renderer could not produce its format. `code` is stable (RENDER_LIMIT_EXCEEDED,
    RENDER_ENGINE_ERROR, ...); `detail` never carries report text."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --- display policy -------------------------------------------------------------------------

_CENT = Decimal("0.01")


def _group(text: str) -> str:
    whole, _, frac = text.partition(".")
    grouped = f"{int(whole):,}"
    return f"{grouped}.{frac}" if frac else grouped


def display_number(value: Decimal, *, whole: bool = False) -> str:
    """2-decimal, half-up, grouped; never shows a non-zero value as zero; keeps the sign."""
    if value.is_nan() or value.is_infinite():
        return str(value)
    if whole and value == value.to_integral_value():
        text = _group(f"{abs(value).to_integral_value():f}")
        return f"-{text}" if value < 0 else text
    rounded = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    if rounded == 0 and value != 0:
        return "<0.01" if value > 0 else ">-0.01"
    text = _group(f"{abs(rounded):f}")
    return f"-{text}" if rounded < 0 else text


def display_exact(value: Decimal) -> str:
    """The exact Box 14 value, grouped for reading, every carried digit kept."""
    if value.is_nan() or value.is_infinite():
        return str(value)
    text = _group(f"{abs(value):f}")
    return f"-{text}" if value < 0 else text


def _unit_key(unit: str | None) -> str:
    return (unit or "").upper().replace("_", " ").strip()


def unit_kind(unit: str | None) -> str:
    key = _unit_key(unit)
    if key in ("PERCENT", "%", "PERCENTAGE", "PCT"):
        return "PERCENT"
    if key in ("PERCENTAGE POINTS", "PP"):
        return "PP"
    if key in ("TIMES", "X"):
        return "TIMES"
    if key == "COUNT":
        return "COUNT"
    if "CRORE" in key:
        return "INR_CRORE" if "INR" in key or key.startswith("CRORE") else "CRORE"
    if "PER SHARE" in key:
        return "INR_PER_SHARE" if "INR" in key else "PER_SHARE"
    return "OTHER"


def display_value(value: Decimal, unit: str | None, *, exact: bool = False) -> str:
    """A value with its unit, per the module's display policy."""
    kind = unit_kind(unit)
    number = display_exact(value) if exact else display_number(value, whole=kind == "COUNT")
    if kind == "PERCENT":
        return f"{number}%"
    if kind == "PP":
        return f"{number} pp"
    if kind == "TIMES":
        return f"{number}x"
    if kind == "COUNT":
        return number
    if kind == "INR_CRORE":
        return f"-₹{number[1:]} Cr" if number.startswith("-") else f"₹{number} Cr"
    if kind == "INR_PER_SHARE":
        return f"-₹{number[1:]}/share" if number.startswith("-") else f"₹{number}/share"
    if kind == "CRORE":
        return f"{number} crore"
    return f"{number} {unit}" if unit else number


def cell_number(value: Decimal, unit: str | None) -> str:
    """A table cell: the number with the compact suffixes only (the row label names the
    unit in full)."""
    kind = unit_kind(unit)
    number = display_number(value, whole=kind == "COUNT")
    return {"PERCENT": f"{number}%", "PP": f"{number} pp", "TIMES": f"{number}x"}.get(
        kind, number)


STATUS_LABELS = {
    "COMPLETE": "Complete",
    "PARTIAL": "Partial",
    "UNAVAILABLE": "Data unavailable",
    "NOT_APPLICABLE": "Not applicable",
}

CLAIM_TYPE_LABELS = {
    "FACT": "Fact",
    "CALCULATION": "Calculation",
    "MANAGEMENT_STATEMENT": "Management statement",
    "ANALYST_INFERENCE": "Analyst inference",
    "SCENARIO": "Scenario",
    "DATA_LIMITATION": "Data limitation",
}

UNAVAILABLE_TEXT = "Data unavailable"
NOT_APPLICABLE_TEXT = "Not applicable"
NO_CELL_TEXT = "Not provided upstream"


# --- presentation model ---------------------------------------------------------------------

Tone = Literal["RED", "YELLOW", "POSITIVE", "LIMITATION", "DILIGENCE", "NEUTRAL", "NOTICE",
               "SCENARIO"]
CellKind = Literal["NUMBER", "TEXT", "UPSTREAM_TEXT", "UNAVAILABLE", "NOT_APPLICABLE",
                   "NO_CELL"]


@dataclass(frozen=True, slots=True)
class ClaimView:
    claim_id: UUID
    claim_type: str
    type_label: str  # 'Fact', 'Management statement', 'Scenario · BASE' ...
    statement: str  # Box 14 text, verbatim
    source_text: str | None  # upstream text, verbatim (a quote, a filing title)
    value: str | None  # display value (policy above)
    exact_value: str | None  # Box 14 value, every digit
    basis: str  # CONSOLIDATED / STANDALONE / NOT_APPLICABLE
    period: str | None
    scenario: str | None
    severity: str | None = None  # RED / YELLOW / GREEN, copied from Box 13 via Box 14
    refs: tuple[str, ...] = ()  # source register refs (S1, S2, ...)
    more_refs: int = 0
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Paragraph:
    text: str
    tone: Tone = "NEUTRAL"
    label: str | None = None  # a short bold lead ('Reason', 'Status' ...)


@dataclass(frozen=True, slots=True)
class ClaimList:
    title: str | None
    tone: Tone
    claims: tuple[ClaimView, ...]
    empty_text: str | None = None
    extra: tuple[tuple[str, str], ...] = ()  # (label, text) lines shown after the claims


@dataclass(frozen=True, slots=True)
class KeyValues:
    title: str | None
    tone: Tone
    rows: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Column:
    label: str
    tags: tuple[str, ...] = ()  # FORECAST / HISTORICAL / BASE / STANDALONE ...
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class Cell:
    text: str
    kind: CellKind
    note: str | None = None
    forecast: bool = False
    link: str | None = None  # an http(s) URL from the source register only


@dataclass(frozen=True, slots=True)
class Row:
    label: str
    tags: tuple[str, ...]
    cells: tuple[Cell, ...]


@dataclass(frozen=True, slots=True)
class Table:
    table_id: str
    title: str
    tags: tuple[str, ...]  # 'Basis: CONSOLIDATED', 'Scenario: BASE', 'Period: ...'
    columns: tuple[Column, ...]  # the first column is the row label
    rows: tuple[Row, ...]
    footnotes: tuple[str, ...] = ()
    notices: tuple[str, ...] = ()  # e.g. 'Appendix truncated: ...'
    refs: tuple[str, ...] = ()
    more_refs: int = 0


@dataclass(frozen=True, slots=True)
class Chart:
    chart_id: str
    title: str
    code: str
    purpose: str
    basis: str
    status: str
    metrics: tuple[str, ...]
    svg: str | None  # Box 6's own rendered SVG (HTML only); None -> reference fallback


Block = Paragraph | ClaimList | KeyValues | Table | Chart


@dataclass(frozen=True, slots=True)
class Section:
    order: int
    section_id: str
    title: str
    status: str
    status_label: str
    status_reason: str
    missing: tuple[str, ...]
    blocks: tuple[Block, ...]
    prominent: bool = False


@dataclass(frozen=True, slots=True)
class RenderDocument:
    report_id: str
    title: str
    company_name: str
    identifiers: tuple[tuple[str, str], ...]
    as_of: str
    generated_at: str
    box14_sha256: str
    header_facts: tuple[tuple[str, str], ...]
    sections: tuple[Section, ...]
    disclaimer: str = DISCLAIMER
    warnings: tuple[str, ...] = field(default=())
    cells: int = 0  # every table cell in the document (resource bound input)


# --- manifest -------------------------------------------------------------------------------

_Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Short = Annotated[str, StringConstraints(max_length=400)]


def _frozen() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


class ManifestFile(BaseModel):
    model_config = _frozen()
    format: RenderFormat
    filename: _Short
    media_type: _Short
    bytes: NonNegativeInt
    sha256: _Sha


class ManifestFailure(BaseModel):
    model_config = _frozen()
    format: RenderFormat
    code: _Short
    detail: _Short


ScreenerPage = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9_-]{1,40}$")]
_SCREENER_COMPANY_PATH = re.compile(r"^/company/([A-Za-z0-9_-]{1,40})/(?:consolidated/)?$")


def screener_page_key(url: str | None) -> str | None:
    """The upper-case company path segment of a Screener company page URL, else None.
    Screener serves /company/infy/ as /company/INFY/, so the segment is folded to upper case;
    it is an opaque page key, never an exchange symbol."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.hostname != "www.screener.in"
            or parts.username or parts.password or port not in {None, 443}
            or parts.query or parts.fragment):
        return None
    match = _SCREENER_COMPANY_PATH.fullmatch(parts.path)
    return match.group(1).upper() if match else None


class ReportManifest(BaseModel):
    """Deterministic: the same Box 14 dataset, renderer version and format set give the same
    manifest. It records no clock, host, path outside the report root or secret."""

    model_config = _frozen()
    schema_version: Literal["FINAL_REPORT_MANIFEST_V1"] = "FINAL_REPORT_MANIFEST_V1"
    artifact_id: UUID  # uuid5(Box 14 SHA-256 | renderer version): the cache key
    location: _Short  # '<company>/<as_of>/<artifact_id>' relative to the report root
    company_key: _Short
    company_name: _Short
    nse_symbol: _Short | None
    isin: _Short | None
    as_of: date
    report_id: UUID
    box14_sha256: _Sha
    box14_schema: _Short
    box14_generated_at: _Short
    engines: tuple[tuple[_Short, _Short], ...]
    status: Literal["COMPLETE", "PARTIAL_RENDER", "RENDER_FAILED"]
    requested: tuple[RenderFormat, ...]
    files: tuple[ManifestFile, ...]
    failures: tuple[ManifestFailure, ...]
    warnings: tuple[_Short, ...]
    limitations: tuple[_Short, ...]
    #: the acquired primary Screener page's company path segment (upper case), taken from the
    #: acquired dataset - never from a caller - so the extension can find "this page's" report
    screener_page: ScreenerPage | None = None
    recommendation: Literal["NONE"] = "NONE"
