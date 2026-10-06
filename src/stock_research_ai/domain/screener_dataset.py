"""DETAILED_RESEARCH_V1 Box 1B: the complete structured dataset a Screener page exposes.

Separate from the frozen CompanyExtractionResult (Summary V1 / FULL_RESEARCH_V1) and from
the EvidenceCatalogue. Raw + normalized: every value keeps its displayed text next to the
parsed number, and a unit is stated only when the page states it. Screener-reported
figures (growth/CAGR tables, pros/cons) are captured as reported, never recomputed.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.financial import (
    ExactDecimal,
    FinancialUnit,
    ReportingScope,
    ValueStatus,
)

SCREENER_DATASET_SCHEMA = "SCREENER_RESEARCH_DATASET_V1"

Label = Annotated[str, StringConstraints(max_length=300)]
Text = Annotated[str, StringConstraints(max_length=5000)]
Url = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ScreenerTableKind(StrEnum):
    QUARTERLY_RESULTS = "QUARTERLY_RESULTS"
    PROFIT_LOSS = "PROFIT_LOSS"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"
    RATIOS = "RATIOS"
    SHAREHOLDING_QUARTERLY = "SHAREHOLDING_QUARTERLY"
    SHAREHOLDING_YEARLY = "SHAREHOLDING_YEARLY"


class DisplayedValue(BaseModel):
    """Exactly what the page showed, plus a number only when it parses unambiguously."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    displayed: Text
    value: ExactDecimal | None
    status: ValueStatus
    unit: FinancialUnit | None  # None = the page does not state a unit

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == ValueStatus.AVAILABLE) != (self.value is not None):
            raise ValueError("Only available values may carry a number")
        return self


class TableCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    period: Label
    value: DisplayedValue
    link: Url | None = None  # e.g. the per-quarter 'Raw PDF' source link
    locator: Label


class TableRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: Label  # cleaned: trailing expand marker removed
    displayed_label: Label  # verbatim
    expandable: bool  # Screener offers a sub-schedule ('+'), not captured from the page
    unit: FinancialUnit | None  # stated by the label/section, else None
    cells: tuple[TableCell, ...]


class TablePeriod(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: Label
    end_date: date | None  # only from the page's explicit data-date-key


class ScreenerTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: ScreenerTableKind
    reporting_basis: ReportingScope
    caption: Label | None  # verbatim, e.g. 'Standalone Figures in Rs. Crores / View ...'
    periods: tuple[TablePeriod, ...]
    rows: tuple[TableRow, ...]
    locator: Label

    @model_validator(mode="after")
    def rectangular(self) -> Self:
        labels = [p.label for p in self.periods]
        if any([c.period for c in row.cells] != labels for row in self.rows):
            raise ValueError("Every row must have exactly one cell per period, in order")
        return self


class GrowthEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    horizon: Label  # e.g. '10 Years', 'TTM', 'Last Year' - as displayed, colon removed
    displayed_horizon: Label
    value: DisplayedValue


class GrowthTable(BaseModel):
    """A Screener-reported growth/return table, captured as reported."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    title: Label
    entries: tuple[GrowthEntry, ...]
    locator: Label


class SnapshotComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: Label | None
    value: DisplayedValue


class SnapshotMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: Label
    value: DisplayedValue
    components: tuple[SnapshotComponent, ...] = ()  # e.g. 'High / Low' -> two values
    locator: Label


class PriceQuote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    price: DisplayedValue
    change: DisplayedValue | None
    as_of_text: Label | None  # verbatim; the page shows no year, so none is inferred


class ClassificationLevel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    level: Label  # e.g. 'Broad Sector', 'Industry'
    name: Label
    url: Url


class NamedLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Label
    url: Url


class ScreenerCompanyProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Label
    page_url: Url
    source_company_key: Label
    warehouse_id: Annotated[str, StringConstraints(pattern=r"^\d{1,12}$")] | None
    reporting_basis: ReportingScope
    bse_code: Label | None
    nse_symbol: Label | None
    website_url: Url | None
    about: Text | None
    key_points: Text | None
    price: PriceQuote | None
    classification: tuple[ClassificationLevel, ...] = ()
    index_memberships: tuple[NamedLink, ...] = ()


class MachineGeneratedNotes(BaseModel):
    """Screener's own checklist text - third-party commentary, not evidence of fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    origin: Literal["SCREENER_MACHINE_GENERATED"] = "SCREENER_MACHINE_GENERATED"
    pros: tuple[Text, ...] = ()
    cons: tuple[Text, ...] = ()


class PeerCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    column: Label
    value: DisplayedValue


class PeerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rank: Label
    name: Label
    company_url: Url | None
    screener_company_id: Annotated[str, StringConstraints(pattern=r"^\d{1,12}$")] | None
    is_subject_company: bool
    cells: tuple[PeerCell, ...]


class PeerComparison(BaseModel):
    """The separately fetched peer fragment; its own source, time and content hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_url: Url
    retrieved_at: AwareDatetime
    content_sha256: Sha256Hex
    size_bytes: NonNegativeInt
    columns: tuple[Label, ...]
    peers: tuple[PeerRow, ...]
    summary_rows: tuple[PeerRow, ...] = ()  # e.g. 'Median: 73 Co.'


class PeerStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["NOT_REQUESTED", "RETRIEVED", "FAILED", "BLOCKED", "UNAVAILABLE"]
    code: Code | None = None


class DatasetIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: Code
    section: Label
    locator: Label | None = None
    displayed: Label | None = None  # the offending source text, bounded


class ScreenerSourceRef(BaseModel):
    """Provenance back to the exact preserved Screener page artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    page_url: Url
    retrieved_at: AwareDatetime
    retrieval_id: UUID
    raw_artifact_id: UUID
    content_sha256: Sha256Hex
    size_bytes: NonNegativeInt


class DetailedScreenerDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["SCREENER_RESEARCH_DATASET_V1"] = "SCREENER_RESEARCH_DATASET_V1"
    company_key: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]{1,64}$")
    ]
    source: ScreenerSourceRef
    profile: ScreenerCompanyProfile
    snapshot: tuple[SnapshotMetric, ...] = ()
    tables: tuple[ScreenerTable, ...] = ()
    growth: tuple[GrowthTable, ...] = ()
    notes: MachineGeneratedNotes | None = None
    peer_status: PeerStatus
    peers: PeerComparison | None = None
    issues: tuple[DatasetIssue, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        kinds = [t.kind for t in self.tables]
        if len(kinds) != len(set(kinds)):
            raise ValueError("Each table kind appears at most once")
        if (self.peer_status.status == "RETRIEVED") != (self.peers is not None):
            raise ValueError("Peers are present exactly when retrieved")
        return self

    def table(self, kind: ScreenerTableKind) -> ScreenerTable | None:
        return next((t for t in self.tables if t.kind == kind), None)
