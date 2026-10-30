"""DETAILED_RESEARCH_V1 Box 14: the final research report composer's output (structured only).

Box 14 reads the trusted outputs of Boxes 3-13 and composes ONE canonical
FinalResearchReportDataset. It never renders (Box 15 does), never calls the network, never asks
an AI model and never computes a new economic number: every number in the report is a claim
copied from an upstream fact, calculation, scenario or valuation output, and every claim names
the upstream records and the registered sources it came from.

Kinds of claim, never blurred:
  FACT                  a reported value or disclosure (Box 4 canonical value, a filing, an
                        exchange record).
  CALCULATION           an upstream calculation (Box 5 metric, Box 13 formula / rule, Box 9
                        peer statistic, Box 12 market method), with its formula / record ID.
  MANAGEMENT_STATEMENT  what management said (Box 8). Never a fact until an outcome is linked.
  ANALYST_INFERENCE     a Box 14 reading built only from other claims; never carries a number.
  SCENARIO              a Box 10 forecast / assumption or a Box 11 valuation output.
  DATA_LIMITATION       a gap or defect in the data. Never a statement about the company.

The report carries no rating, ranking, target price, position size or recommendation.
"""

import re
from collections.abc import Iterable
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_canonical import Confidence, Currency, Industry

FINAL_REPORT_SCHEMA: Literal["FINAL_RESEARCH_REPORT_DATASET_V1"] = (
    "FINAL_RESEARCH_REPORT_DATASET_V1"
)
FINAL_REPORT_ENGINE_VERSION = "final-report/1"
SELECTION_RULES_VERSION = "report-selection/2"

MAX_CLAIMS = 25_000
MAX_TABLES = 600
MAX_ROWS = 5_000  # per table
MAX_SOURCES = 20_000
MAX_CHARTS = 600
MAX_ENTRIES = 20_000  # risk / limitation / diligence / disagreement / monitoring entries
MAX_SUMMARY_PRIMARY = 15
MAX_NARRATIVE_CHARS = 4_000_000  # every Box 14-written string, summed

Label = Annotated[str, StringConstraints(max_length=1000)]
Text = Annotated[str, StringConstraints(max_length=4000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

#: The words Box 14 never writes. Matched as whole words / phrases (so 'BUYBACK' or
#: 'SHAREHOLDING' are not advice) in Box 14-generated text only, never in quoted source text.
ADVICE_TERMS = (
    "BUY",
    "SELL",
    "HOLD",
    "OVERWEIGHT",
    "UNDERWEIGHT",
    "TARGET PRICE",
    "POSITION SIZE",
    "BUY ZONE",
    "SELL TRIGGER",
)
_ADVICE = re.compile(
    r"(?<![A-Z0-9_])(" + "|".join(t.replace(" ", r"[\s_-]+") for t in ADVICE_TERMS) +
    r")(?![A-Z0-9_])", re.IGNORECASE)


def advice_term(text: str) -> str | None:
    """The first advice term in Box 14-written text, or None."""
    found = _ADVICE.search(text)
    return found.group(1).upper() if found else None


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


# --- vocabulary -----------------------------------------------------------------------------


class ClaimType(StrEnum):
    FACT = "FACT"
    CALCULATION = "CALCULATION"
    MANAGEMENT_STATEMENT = "MANAGEMENT_STATEMENT"
    ANALYST_INFERENCE = "ANALYST_INFERENCE"
    SCENARIO = "SCENARIO"
    DATA_LIMITATION = "DATA_LIMITATION"


class ClaimBasis(StrEnum):
    CONSOLIDATED = "CONSOLIDATED"  # the primary analytical basis
    STANDALONE = "STANDALONE"  # the forensic / control basis
    NOT_APPLICABLE = "NOT_APPLICABLE"  # not a financial-statement value (events, ratings ...)


class Scenario(StrEnum):
    BASE = "BASE"
    BULL = "BULL"
    BEAR = "BEAR"


class Polarity(StrEnum):
    POSITIVE = "POSITIVE"  # an upstream positive control / favourable evaluated reading
    NEGATIVE = "NEGATIVE"  # an upstream YELLOW / RED company risk
    NEUTRAL = "NEUTRAL"


class Materiality(StrEnum):
    HIGH = "HIGH"  # executive summary or a RED company risk
    MEDIUM = "MEDIUM"  # a core report section
    LOW = "LOW"  # appendix only


class Layer(StrEnum):
    BOX3 = "BOX3"
    BOX4 = "BOX4"
    BOX5 = "BOX5"
    BOX6 = "BOX6"
    BOX7 = "BOX7"
    BOX8 = "BOX8"
    BOX9 = "BOX9"
    BOX10 = "BOX10"
    BOX11 = "BOX11"
    BOX12 = "BOX12"
    BOX13 = "BOX13"


class SectionId(StrEnum):
    DUE_DILIGENCE_STATUS = "DUE_DILIGENCE_STATUS"
    EXECUTIVE_SUMMARY = "EXECUTIVE_SUMMARY"
    COMPANY_OVERVIEW = "COMPANY_OVERVIEW"
    BUSINESS_MODEL = "BUSINESS_MODEL"
    INDUSTRY_COMPETITION = "INDUSTRY_COMPETITION"
    HISTORICAL_FINANCIALS = "HISTORICAL_FINANCIALS"
    CASH_FLOW_BALANCE_SHEET = "CASH_FLOW_BALANCE_SHEET"
    ACCOUNTING_FORENSICS = "ACCOUNTING_FORENSICS"
    MANAGEMENT_GOVERNANCE = "MANAGEMENT_GOVERNANCE"
    CONCALL_GUIDANCE = "CONCALL_GUIDANCE"
    CORPORATE_EVENTS = "CORPORATE_EVENTS"
    ORDER_BOOK = "ORDER_BOOK"
    SHAREHOLDING = "SHAREHOLDING"
    CREDIT_LIQUIDITY = "CREDIT_LIQUIDITY"
    FORECASTS = "FORECASTS"
    SCENARIO_ANALYSIS = "SCENARIO_ANALYSIS"
    VALUATION = "VALUATION"
    RISKS = "RISKS"
    WHAT_CHANGES_THE_VIEW = "WHAT_CHANGES_THE_VIEW"
    MONITORING = "MONITORING"
    DATA_QUALITY_RECONCILIATION = "DATA_QUALITY_RECONCILIATION"
    SOURCE_REGISTER = "SOURCE_REGISTER"
    APPENDICES = "APPENDICES"


#: The canonical reading order (section 0 .. 22).
SECTION_ORDER: tuple[SectionId, ...] = tuple(SectionId)

SECTION_TITLES: dict[SectionId, str] = {
    SectionId.DUE_DILIGENCE_STATUS: "Due-Diligence Status",
    SectionId.EXECUTIVE_SUMMARY: "Executive Summary",
    SectionId.COMPANY_OVERVIEW: "Company Overview",
    SectionId.BUSINESS_MODEL: "Business & Operating Model",
    SectionId.INDUSTRY_COMPETITION: "Industry & Competitive Position",
    SectionId.HISTORICAL_FINANCIALS: "Historical Financial Performance",
    SectionId.CASH_FLOW_BALANCE_SHEET: "Cash Flow & Balance Sheet",
    SectionId.ACCOUNTING_FORENSICS: "Accounting / Earnings Quality / Forensics",
    SectionId.MANAGEMENT_GOVERNANCE: "Management & Governance",
    SectionId.CONCALL_GUIDANCE: "Concall & Guidance Analysis",
    SectionId.CORPORATE_EVENTS: "Corporate / Exchange Events",
    SectionId.ORDER_BOOK: "Order Book / Project Pipeline",
    SectionId.SHAREHOLDING: "Shareholding & Ownership",
    SectionId.CREDIT_LIQUIDITY: "Credit / Liquidity",
    SectionId.FORECASTS: "Forecasts",
    SectionId.SCENARIO_ANALYSIS: "Scenario Analysis",
    SectionId.VALUATION: "Valuation",
    SectionId.RISKS: "Risks & Due-Diligence Findings",
    SectionId.WHAT_CHANGES_THE_VIEW: "What Changes the View",
    SectionId.MONITORING: "Monitoring Checklist",
    SectionId.DATA_QUALITY_RECONCILIATION: "Data Quality & Reconciliation",
    SectionId.SOURCE_REGISTER: "Source Register",
    SectionId.APPENDICES: "Appendices",
}


class SectionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class InputFamily(StrEnum):
    BOX3_EVIDENCE = "BOX3_EVIDENCE"
    BOX4_FINANCIALS = "BOX4_FINANCIALS"
    BOX5_ANALYTICS = "BOX5_ANALYTICS"
    BOX6_CHARTS = "BOX6_CHARTS"
    BOX7_BUSINESS = "BOX7_BUSINESS"
    BOX8_MANAGEMENT = "BOX8_MANAGEMENT"
    BOX9_INDUSTRY = "BOX9_INDUSTRY"
    BOX10_FORECAST = "BOX10_FORECAST"
    BOX11_VALUATION = "BOX11_VALUATION"
    BOX12_MARKET = "BOX12_MARKET"
    BOX13_DILIGENCE = "BOX13_DILIGENCE"
    QUARTERLY_FINANCIALS = "QUARTERLY_FINANCIALS"
    SEGMENT_DATA = "SEGMENT_DATA"
    CONCALLS = "CONCALLS"
    GUIDANCE = "GUIDANCE"
    ORDER_LEDGER = "ORDER_LEDGER"
    SHAREHOLDING_DATA = "SHAREHOLDING_DATA"
    RATINGS = "RATINGS"
    MARKET_DATA = "MARKET_DATA"


class FamilyStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# --- provenance and claims -----------------------------------------------------------------------


class UpstreamRecord(BaseModel):
    """One upstream record a claim rests on, by its own ID in its own dataset."""

    model_config = _model()
    layer: Layer
    kind: Code  # CANONICAL_VALUE / METRIC / SIGNAL / EVENT / ORDER_CHAIN / GUIDANCE / ...
    record_id: UUID


class ReportClaim(BaseModel):
    model_config = _model()
    claim_id: UUID  # uuid5 over the claim key: never depends on generated_at
    claim_type: ClaimType
    topic: Code  # stable subject code (REVENUE, HIGH_ACCRUALS, ORDER_BOOK_REPORTED, ...)
    statement: Text = Field(min_length=1)  # Box 14 template text (advice-checked)
    #: verbatim upstream text (a quote, a filing title, a business description). Labelled as
    #: source text, never rewritten and never advice-checked.
    source_text: Text | None = None
    metric: Code | None = None
    value: ExactDecimal | None = None
    unit: Label | None = None
    currency: Currency | None = None
    period: Label | None = None
    period_end: date | None = None
    basis: ClaimBasis
    scenario: Scenario | None = None
    source_ids: tuple[UUID, ...] = Field(default=(), max_length=500)
    upstream: tuple[UpstreamRecord, ...] = Field(default=(), max_length=500)
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=500)
    calculation_ids: tuple[UUID, ...] = Field(default=(), max_length=500)
    formula_ids: tuple[Label, ...] = Field(default=(), max_length=50)
    risk_ids: tuple[UUID, ...] = Field(default=(), max_length=500)
    derived_from: tuple[UUID, ...] = Field(default=(), max_length=500)  # other claims
    confidence: Confidence | None = None
    polarity: Polarity = Polarity.NEUTRAL
    materiality: Materiality = Materiality.LOW
    limitations: tuple[Code, ...] = Field(default=(), max_length=50)
    section_ids: tuple[SectionId, ...] = Field(default=(), max_length=len(SECTION_ORDER))

    @model_validator(mode="after")
    def disciplined(self) -> Self:
        t = self.claim_type
        if t == ClaimType.ANALYST_INFERENCE:
            if self.value is not None:
                raise ValueError("An analyst inference never carries a number of its own")
            if not self.derived_from:
                raise ValueError("An analyst inference names the claims it reads")
        elif not self.upstream:
            raise ValueError("A claim must name its upstream records")
        if self.value is not None and not self.source_ids:
            raise ValueError("A numeric claim needs provenance (registered sources)")
        if (self.claim_type == ClaimType.SCENARIO) != (self.scenario is not None):
            raise ValueError("Exactly a SCENARIO claim names its scenario")
        if t == ClaimType.CALCULATION and not (self.calculation_ids and self.formula_ids):
            raise ValueError("A calculation claim references its upstream calculation and formula")
        if t == ClaimType.MANAGEMENT_STATEMENT and not any(
                u.layer == Layer.BOX8 for u in self.upstream):
            raise ValueError("A management statement comes from Box 8")
        if t == ClaimType.SCENARIO and not any(
                u.layer in (Layer.BOX10, Layer.BOX11) for u in self.upstream):
            raise ValueError("A scenario claim comes from Box 10 / Box 11")
        if t == ClaimType.DATA_LIMITATION and not self.limitations:
            raise ValueError("A data limitation names its limitation code")
        if t == ClaimType.DATA_LIMITATION and self.polarity != Polarity.NEUTRAL:
            raise ValueError("A data limitation is never a statement about the company")
        term = advice_term(self.statement)
        if term:
            raise ValueError(f"Box 14 never writes advice language ({term})")
        return self


# --- source register -------------------------------------------------------------------------


class SourceType(StrEnum):
    FILING = "FILING"  # a filed document (annual report, results, transcript, notice)
    EXCHANGE_RECORD = "EXCHANGE_RECORD"  # an exchange feed / announcement / archive record
    PROVIDER_RECORD = "PROVIDER_RECORD"  # a market-data or aggregator page (Kite, Screener)
    RATING_AGENCY = "RATING_AGENCY"
    COMPANY_DOCUMENT = "COMPANY_DOCUMENT"  # the company's own site / presentation
    ANALYST_INPUT = "ANALYST_INPUT"  # an explicit cited input; never a company fact alone
    UPSTREAM_DATASET = "UPSTREAM_DATASET"  # a Box 3-13 dataset (lineage, not a document)


class ReportSource(BaseModel):
    """One deduplicated source: a document / record is listed once however often it is cited.
    `authority` is the upstream box's own authority label (Box 14 adds no hierarchy)."""

    model_config = _model()
    source_id: UUID
    key: Label  # the dedup key (normalised URL, document ID or reference)
    source_type: SourceType
    authority: tuple[Label, ...]  # e.g. 'BOX4:FILING/PRIMARY_REGULATORY', 'BOX12:MARKET_PROVIDER'
    title: Label
    document_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    artifact_sha256: tuple[Sha256Hex, ...] = Field(default=(), max_length=50)
    url: Label | None = None
    document_date: date | None = None
    retrieval_date: date | None = None
    layers: tuple[Layer, ...]
    section_ids: tuple[SectionId, ...] = ()
    bases: tuple[ClaimBasis, ...] = ()
    status: Code = "AVAILABLE"  # AVAILABLE / BLOCKED / AUTH_REQUIRED / FAILED / ...
    limitations: tuple[Code, ...] = ()
    claim_count: NonNegativeInt = 0


# --- tables and charts -----------------------------------------------------------------------


class ColumnKind(StrEnum):
    HISTORICAL = "HISTORICAL"
    FORECAST = "FORECAST"
    VALUE = "VALUE"
    TEXT = "TEXT"


class TableColumn(BaseModel):
    model_config = _model()
    key: Code
    label: Label
    kind: ColumnKind
    period: Label | None = None
    period_end: date | None = None
    scenario: Scenario | None = None
    basis: ClaimBasis | None = None  # a column that holds one reporting basis only


class CellStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    TEXT = "TEXT"


class TableCell(BaseModel):
    model_config = _model()
    column: Code
    status: CellStatus
    value: ExactDecimal | None = None
    text: Label | None = None
    #: UPSTREAM text is copied verbatim (a title, a name); REPORT text is Box 14's own words
    text_origin: Literal["UPSTREAM", "REPORT"] | None = None
    claim_id: UUID | None = None
    note: Code | None = None  # why a cell is unavailable / not applicable

    @model_validator(mode="after")
    def filled(self) -> Self:
        if (self.status == CellStatus.AVAILABLE) != (self.value is not None):
            raise ValueError("Exactly an AVAILABLE cell has a value")
        if self.value is not None and self.claim_id is None:
            raise ValueError("A numeric cell references the claim that carries its value")
        if (self.status == CellStatus.TEXT) != (self.text is not None):
            raise ValueError("Exactly a TEXT cell has text")
        if (self.text is None) != (self.text_origin is None):
            raise ValueError("Text names its origin")
        if self.status in (CellStatus.UNAVAILABLE, CellStatus.NOT_APPLICABLE) and not self.note:
            raise ValueError("An empty cell says why")
        return self


class RowKind(StrEnum):
    HISTORICAL = "HISTORICAL"
    FORECAST = "FORECAST"
    ASSUMPTION = "ASSUMPTION"
    VALUATION = "VALUATION"
    FACT = "FACT"
    CALCULATION = "CALCULATION"
    MANAGEMENT_STATEMENT = "MANAGEMENT_STATEMENT"
    DATA_LIMITATION = "DATA_LIMITATION"


class TableRow(BaseModel):
    model_config = _model()
    key: Label
    label: Label
    kind: RowKind
    unit: Label | None = None
    cells: tuple[TableCell, ...]
    claim_id: UUID | None = None  # the row's own claim (a ledger row, an event)


class ReportTable(BaseModel):
    model_config = _model()
    table_id: UUID
    section_id: SectionId
    code: Code
    title: Label
    basis: ClaimBasis
    period: Label | None = None
    scenario: Scenario | None = None
    columns: tuple[TableColumn, ...] = Field(min_length=1, max_length=64)
    rows: tuple[TableRow, ...] = Field(max_length=MAX_ROWS)
    footnotes: tuple[Label, ...] = ()
    source_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_SOURCES)
    claim_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_CLAIMS)
    appendix: bool = False
    truncated: bool = False

    @model_validator(mode="after")
    def shaped(self) -> Self:
        keys = [c.key for c in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("Column keys are unique")
        known = set(keys)
        for row in self.rows:
            if any(c.column not in known for c in row.cells):
                raise ValueError("A cell names an unknown column")
        if self.basis != ClaimBasis.NOT_APPLICABLE and any(
                c.basis not in (None, self.basis) for c in self.columns):
            raise ValueError("A single-basis table has no column of another basis")
        return self


class ChartReference(BaseModel):
    """A pointer to a Box 6 chart definition. Box 14 renders nothing."""

    model_config = _model()
    chart_id: UUID
    upstream_chart_id: UUID
    chart_code: Code
    title: Label
    purpose: Label
    section_id: SectionId
    basis: ClaimBasis
    upstream_status: Code
    source_dataset_ids: tuple[UUID, ...]
    metric_codes: tuple[Code, ...] = ()


# --- sections --------------------------------------------------------------------------------


class FamilyCheck(BaseModel):
    model_config = _model()
    family: InputFamily
    status: FamilyStatus
    detail: Label | None = None


class SectionCompleteness(BaseModel):
    """Deterministic: which required input families were available (no percentage)."""

    model_config = _model()
    required: tuple[InputFamily, ...]
    available: tuple[InputFamily, ...]
    missing: tuple[InputFamily, ...]
    not_applicable: tuple[InputFamily, ...]
    #: the section's material-evidence contract: every gap that keeps it from COMPLETE even
    #: though its input families exist (a rule not evaluated, a ledger block partial ...)
    material_gaps: tuple[Code, ...] = ()


class ReportSection(BaseModel):
    model_config = _model()
    section_id: SectionId
    order: NonNegativeInt
    title: Label
    status: SectionStatus
    status_reason: Label
    completeness: SectionCompleteness
    claim_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_CLAIMS)  # core reader layer
    limitation_claim_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_CLAIMS)
    table_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_TABLES)
    chart_ids: tuple[UUID, ...] = Field(default=(), max_length=MAX_CHARTS)
    notes: tuple[Code, ...] = ()


class DueDiligenceStatus(BaseModel):
    """Section 0: what the reader must know first. Counts carry their explicit denominator;
    there is deliberately no completeness score or percentage."""

    model_config = _model()
    input_families: tuple[FamilyCheck, ...]
    families_available: NonNegativeInt
    families_checked: NonNegativeInt  # the explicit denominator
    sections_by_status: tuple[tuple[SectionStatus, NonNegativeInt], ...]
    sections_total: NonNegativeInt
    open_source_conflicts: NonNegativeInt
    conflict_claim_ids: tuple[UUID, ...]
    blocked_sources: tuple[UUID, ...]  # source register IDs with a blocked / failed status
    unavailable_information_claim_ids: tuple[UUID, ...]
    open_diligence_item_ids: tuple[UUID, ...]  # Box 13 HIGH / MEDIUM items
    valuation_inputs_complete: bool
    valuation_status: Label
    forecast_inputs_complete: bool
    forecast_status: Label
    scoring: Literal["NONE"] = "NONE"


class SummaryKind(StrEnum):
    WHAT_THE_COMPANY_DOES = "WHAT_THE_COMPANY_DOES"
    OPERATING_DRIVERS = "OPERATING_DRIVERS"
    FINANCIAL_TRAJECTORY = "FINANCIAL_TRAJECTORY"
    STRENGTHS = "STRENGTHS"
    BALANCE_SHEET_LIQUIDITY = "BALANCE_SHEET_LIQUIDITY"
    MANAGEMENT_GUIDANCE = "MANAGEMENT_GUIDANCE"
    ORDER_BOOK = "ORDER_BOOK"
    FORECAST_RANGE = "FORECAST_RANGE"
    VALUATION_RANGE = "VALUATION_RANGE"
    MAJOR_RISKS = "MAJOR_RISKS"
    MONITORING = "MONITORING"
    DATA_LIMITATIONS = "DATA_LIMITATIONS"


#: kinds that count toward the primary-claim limit (risks, monitoring and limitations do not)
PRIMARY_KINDS = frozenset({
    SummaryKind.WHAT_THE_COMPANY_DOES, SummaryKind.OPERATING_DRIVERS,
    SummaryKind.FINANCIAL_TRAJECTORY, SummaryKind.STRENGTHS, SummaryKind.BALANCE_SHEET_LIQUIDITY,
    SummaryKind.MANAGEMENT_GUIDANCE, SummaryKind.ORDER_BOOK, SummaryKind.FORECAST_RANGE,
    SummaryKind.VALUATION_RANGE,
})


class SummaryPoint(BaseModel):
    model_config = _model()
    kind: SummaryKind
    claim_ids: tuple[UUID, ...]  # existing claims only; the summary writes no claim of its own
    #: NOT_APPLICABLE: upstream says the topic does not apply to this company (never a gap)
    status: Literal["AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"]
    reason: Label | None = None
    monitoring_item_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def referenced(self) -> Self:
        if self.status == "AVAILABLE" and not (self.claim_ids or self.monitoring_item_ids):
            raise ValueError("An available summary point references existing claims")
        if self.status != "AVAILABLE" and self.reason is None:
            raise ValueError("An unavailable summary point says why")
        if self.status == "NOT_APPLICABLE" and (self.claim_ids or self.monitoring_item_ids):
            raise ValueError("A not-applicable summary point cites no claim")
        return self


class ExecutiveSummary(BaseModel):
    model_config = _model()
    built_after: tuple[SectionId, ...]  # the completed sections it was derived from
    points: tuple[SummaryPoint, ...]
    primary_claim_ids: tuple[UUID, ...] = Field(max_length=MAX_SUMMARY_PRIMARY)
    risk_claim_ids: tuple[UUID, ...]
    limitation_claim_ids: tuple[UUID, ...]
    yellow_risks_not_listed: NonNegativeInt = 0  # every RED is listed; extra YELLOW counted
    recommendation: Literal["NONE"] = "NONE"


class ScenarioCase(BaseModel):
    """A Box 10 / Box 11 scenario as a case. A scenario, never a recommendation; no
    probability is ever assigned (Box 10 supplies none)."""

    model_config = _model()
    scenario: Scenario
    case: Literal["BULL_CASE", "BASE_CASE", "BEAR_CASE"]
    status: SectionStatus
    thesis: Label
    driver_claim_ids: tuple[UUID, ...]  # Box 10 assumptions
    forecast_claim_ids: tuple[UUID, ...]
    valuation_claim_ids: tuple[UUID, ...]
    risk_ids: tuple[UUID, ...]  # Box 13 RED / YELLOW risks that bear on every case
    conditions_required: tuple[Label, ...]
    invalidation_conditions: tuple[Label, ...]
    probability: Literal["NOT_PROVIDED"] = "NOT_PROVIDED"


class InvestmentCase(BaseModel):
    model_config = _model()
    cases: tuple[ScenarioCase, ...] = Field(max_length=3)
    expected_value: Literal["NONE"] = "NONE"  # no probabilities upstream -> no weighting
    recommendation: Literal["NONE"] = "NONE"


class GuidanceTrackerEntry(BaseModel):
    model_config = _model()
    guidance_id: UUID
    statement_claim_id: UUID  # MANAGEMENT_STATEMENT
    date_made: date | None
    deadline: Label | None
    deadline_end: date | None
    metric: Code
    target: Label  # upstream raw target text
    outcome_id: UUID | None
    result: Code  # Box 8 outcome result (NOT_YET_DUE stays open, never 'missed')
    actual_claim_id: UUID | None  # FACT / CALCULATION, linked separately
    lifecycle: Code
    evidence_ids: tuple[UUID, ...]
    #: one row per real target: its lifecycle across calls, oldest first ('ISSUED
    #: 16-Jan-2026 -> REITERATED 15-Oct-2026'); the earlier statements stay cited claims
    history: Label | None = None
    earlier_statement_claim_ids: tuple[UUID, ...] = ()


class OrderBucketKind(StrEnum):
    CONFIRMED_ORDER_BOOK = "CONFIRMED_ORDER_BOOK"
    PENDING_L1 = "PENDING_L1"
    UNRESOLVED_L1 = "UNRESOLVED_L1"
    SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"


class OrderBucket(BaseModel):
    model_config = _model()
    bucket: OrderBucketKind
    label: Label
    chain_ids: tuple[UUID, ...] = Field(max_length=MAX_ENTRIES)
    count_claim_id: UUID | None
    value_claim_ids: tuple[UUID, ...]
    note: Label


class OrderBookView(BaseModel):
    """L1 != LOA != CONTRACT != REVENUE: the buckets are never added together."""

    model_config = _model()
    applicable: bool
    applicability: Label
    buckets: tuple[OrderBucket, ...]
    reported_order_book_claim_ids: tuple[UUID, ...]
    metric_claim_ids: tuple[UUID, ...]
    l1_policy: Literal["L1_NEVER_CONFIRMED_REPORTED_SEPARATELY"] = (
        "L1_NEVER_CONFIRMED_REPORTED_SEPARATELY")
    combined_total: Literal["NONE"] = "NONE"

    @model_validator(mode="after")
    def separated(self) -> Self:
        kinds = [b.bucket for b in self.buckets]
        if len(kinds) != len(set(kinds)):
            raise ValueError("One bucket per order stage")
        sets = [set(b.chain_ids) for b in self.buckets]
        for i, a in enumerate(sets):
            for b in sets[i + 1:]:
                if a & b:
                    raise ValueError("An order chain sits in exactly one bucket")
        return self


class ValuationMethodView(BaseModel):
    model_config = _model()
    scenario: Scenario
    method: Code
    status: Code  # Box 11 status: COMPLETE / PARTIAL / UNAVAILABLE
    reason: Code | None
    missing_inputs: tuple[Label, ...]
    assumption_claim_ids: tuple[UUID, ...]
    result_claim_ids: tuple[UUID, ...]
    limitation_claim_ids: tuple[UUID, ...]


class ValuationView(BaseModel):
    model_config = _model()
    methods: tuple[ValuationMethodView, ...]
    market_context_claim_ids: tuple[UUID, ...]
    blended_value: Literal["NONE"] = "NONE"  # methods are never force-blended
    target_price: Literal["NONE"] = "NONE"


class RiskKind(StrEnum):
    COMPANY_RISK = "COMPANY_RISK"
    POSITIVE_CONTROL = "POSITIVE_CONTROL"


class RiskEntry(BaseModel):
    """A Box 13 signal, as Box 13 graded it (Box 14 never re-grades)."""

    model_config = _model()
    risk_id: UUID
    code: Code
    kind: RiskKind
    category: Code
    status: Code
    severity: Literal["GREEN", "YELLOW", "RED"] | None
    reason: Code | None
    claim_id: UUID
    input_claim_ids: tuple[UUID, ...] = ()
    mitigating_risk_ids: tuple[UUID, ...] = ()
    likelihood: Code | None = None
    impact: Code | None = None


class LimitationEntry(BaseModel):
    model_config = _model()
    entry_id: UUID
    kind: Code
    layer: Label
    claim_id: UUID
    affects: tuple[Code, ...] = ()
    #: the canonical limitation ID every section cites (DL-001 ...); full detail is printed
    #: once, in the register (None only on reports composed before the register)
    ref: Annotated[str, StringConstraints(pattern=r"^DL-\d{3,5}$")] | None = None
    occurrences: int = Field(default=1, ge=1)  # identical upstream items merged into this one
    #: further Box 13 data-quality items this entry stands for (identical ones merged, or a
    #: rule-input item the rule's own entry already states) - none is ever silently dropped
    merged_item_ids: tuple[UUID, ...] = ()


class DiligenceEntry(BaseModel):
    model_config = _model()
    item_id: UUID
    importance: Code
    claim_id: UUID
    needed_source: Label
    related_risk_ids: tuple[UUID, ...]
    status: Literal["OPEN"] = "OPEN"


RuleOutcome = Literal["RED", "YELLOW", "GREEN", "INFORMATIONAL", "NOT_EVALUATED",
                      "NOT_APPLICABLE"]


class RuleCoverage(BaseModel):
    """One company-risk rule as the reader needs it: what was tested and what came out -
    a grade, an evaluation without a grade, NOT EVALUATED (with the missing evidence) or
    not applicable. NOT EVALUATED is never 'no risk'."""

    model_config = _model()
    risk_id: UUID
    code: Code
    title: Label  # 'Promoter pledge'
    category: Code
    outcome: RuleOutcome
    reason: Code | None = None  # the upstream reason code (PLEDGE_DATA_NOT_SUPPLIED ...)
    reason_text: Label | None = None  # 'promoter pledge data not available'
    #: an evaluated rule's measure and value, 'EBIT / finance cost = 20.48x' - what a GREEN
    #: result is about (the title names the risk tested, not a finding)
    measured: Label | None = None


class RiskCoverage(BaseModel):
    """THE risk-coverage count of a report (company-risk rules only; positive controls are
    reported apart). Every surface - cover, executive summary, Section 17, the side panel -
    shows these numbers and this headline, never its own count."""

    model_config = _model()
    applicable: NonNegativeInt  # evaluated + not evaluated
    evaluated: NonNegativeInt  # red + yellow + green + informational
    red: NonNegativeInt
    yellow: NonNegativeInt
    green: NonNegativeInt
    informational: NonNegativeInt = 0
    not_evaluated: NonNegativeInt
    not_applicable: NonNegativeInt
    rules: tuple[RuleCoverage, ...] = Field(max_length=MAX_ENTRIES)
    headline: Label

    @model_validator(mode="after")
    def counted(self) -> Self:
        by: dict[str, int] = {}
        for r in self.rules:
            by[r.outcome] = by.get(r.outcome, 0) + 1
        if (self.red, self.yellow, self.green, self.informational, self.not_evaluated,
                self.not_applicable) != tuple(by.get(k, 0) for k in (
                    "RED", "YELLOW", "GREEN", "INFORMATIONAL", "NOT_EVALUATED",
                    "NOT_APPLICABLE")):
            raise ValueError("risk coverage counts are the counts of its rules")
        if self.evaluated != self.red + self.yellow + self.green + self.informational or (
                self.applicable != self.evaluated + self.not_evaluated):
            raise ValueError("evaluated / applicable follow the rule outcomes")
        if any(r.outcome == "NOT_EVALUATED" and not r.reason for r in self.rules):
            raise ValueError("a rule that was not evaluated names the missing evidence")
        return self


class DisclosedRiskEntry(BaseModel):
    """A risk / headwind management itself disclosed (application.management_risks): a cited
    MANAGEMENT STATEMENT, never graded and never a registered rule's finding."""

    model_config = _model()
    category: Code  # CURRENCY, CLIENT_CONCENTRATION ...
    label: Label
    claim_id: UUID
    event_date: date | None = None


class RiskPresentation(BaseModel):
    """Company risk and data limitation never share a list."""

    model_config = _model()
    risk_register: tuple[RiskEntry, ...] = Field(max_length=MAX_ENTRIES)  # every Box 13 signal
    red: tuple[UUID, ...]
    yellow: tuple[UUID, ...]
    positive_controls: tuple[UUID, ...]
    data_limitations: tuple[LimitationEntry, ...] = Field(max_length=MAX_ENTRIES)
    open_diligence: tuple[DiligenceEntry, ...] = Field(max_length=MAX_ENTRIES)
    #: the canonical coverage (None only on reports composed before it existed)
    coverage: RiskCoverage | None = None
    #: management-disclosed risks / headwinds (verbatim, cited, ungraded)
    management_disclosed: tuple[DisclosedRiskEntry, ...] = Field(default=(),
                                                                 max_length=MAX_ENTRIES)


class ViewDirection(StrEnum):
    STRENGTHENS = "STRENGTHENS"
    WEAKENS = "WEAKENS"
    RESOLVES_UNCERTAINTY = "RESOLVES_UNCERTAINTY"


class ThresholdRef(BaseModel):
    """An upstream-defined threshold (Box 13 rule registry), copied, never invented."""

    model_config = _model()
    severity: Code
    comparison: Code
    value: ExactDecimal
    rule_id: Label


class ViewChangeEntry(BaseModel):
    model_config = _model()
    entry_id: UUID
    direction: ViewDirection
    condition: Label
    subject: Code  # metric or event code
    thresholds: tuple[ThresholdRef, ...]
    related_claim_ids: tuple[UUID, ...]
    related_risk_ids: tuple[UUID, ...]
    source_required: Label
    status: Literal["OPEN", "MONITORING"]


class MonitoringItem(BaseModel):
    model_config = _model()
    item_id: UUID
    topic: Code
    subject: Code
    reason: Label
    latest_claim_ids: tuple[UUID, ...]
    thresholds: tuple[ThresholdRef, ...]
    source_to_check: Label
    trigger: Literal["NEXT_RESULTS", "NEXT_ANNUAL_REPORT", "NEXT_SHAREHOLDING_FILING",
                     "NEXT_RATING_ACTION", "ON_EXCHANGE_FILING", "ON_DATA_RESOLUTION"]
    related_risk_ids: tuple[UUID, ...] = ()


class ReconciliationEntry(BaseModel):
    """A Box 13 standalone / consolidated comparison. INPUT_CONFLICT stays a data limitation."""

    model_config = _model()
    comparison_id: UUID
    metric: Code
    period: Label
    status: Code  # Box 13 ReconciliationStatus
    standalone_claim_id: UUID | None
    consolidated_claim_id: UUID | None
    difference_claim_id: UUID | None
    claim_id: UUID
    classification: Literal["COMPARISON", "DATA_LIMITATION"]


class DisagreementValueView(BaseModel):
    model_config = _model()
    source: Label
    authority: Label
    value: ExactDecimal | None
    record_id: UUID | None


class DisagreementView(BaseModel):
    """Every losing observation stays visible."""

    model_config = _model()
    entry_id: UUID
    origin: Code
    topic: Label
    period: Label
    basis: Label
    values: tuple[DisagreementValueView, ...] = Field(min_length=2)
    status: Literal["OPEN", "RESOLVED"]
    canonical_selection: Label | None
    reason: Label
    claim_id: UUID


class AppendixId(StrEnum):
    FULL_FINANCIALS = "FULL_FINANCIALS"
    STANDALONE_FINANCIALS = "STANDALONE_FINANCIALS"
    QUARTERLY_TABLES = "QUARTERLY_TABLES"
    EVENT_LEDGER = "EVENT_LEDGER"
    ORDER_LEDGER = "ORDER_LEDGER"
    SHAREHOLDING_HISTORY = "SHAREHOLDING_HISTORY"
    MANAGEMENT_STATEMENTS = "MANAGEMENT_STATEMENTS"
    GOVERNANCE_CATALOGUE = "GOVERNANCE_CATALOGUE"
    FORECAST_ASSUMPTIONS = "FORECAST_ASSUMPTIONS"
    VALUATION_ASSUMPTIONS = "VALUATION_ASSUMPTIONS"
    RISK_REGISTER = "RISK_REGISTER"
    OPEN_DILIGENCE = "OPEN_DILIGENCE"
    SOURCE_DISAGREEMENTS = "SOURCE_DISAGREEMENTS"
    SOURCE_REGISTER = "SOURCE_REGISTER"


class Appendix(BaseModel):
    model_config = _model()
    appendix_id: AppendixId
    title: Label
    status: SectionStatus
    table_ids: tuple[UUID, ...]
    claim_ids: tuple[UUID, ...] = Field(max_length=MAX_CLAIMS)
    entries: NonNegativeInt
    upstream_entries: NonNegativeInt  # what the upstream holds (== entries unless truncated)
    truncated: bool = False
    limitation_claim_id: UUID | None = None

    @model_validator(mode="after")
    def surfaced(self) -> Self:
        if self.truncated and self.limitation_claim_id is None:
            raise ValueError("A truncated appendix surfaces its limitation")
        return self


# --- request and metadata --------------------------------------------------------------------


class FinalReportRequest(BaseModel):
    """What to compose. `as_of` is explicit: no fact dated after it may enter the report."""

    model_config = _model()
    as_of: date
    company_key: Label = Field(min_length=1)  # must equal every upstream dataset's key
    nse_symbol: Label | None = None  # must match the Box 13 / Box 12 identity when given
    isin: Label | None = None



class InputReference(BaseModel):
    model_config = _model()
    layer: Layer
    dataset_id: UUID | None
    sha256: Sha256Hex
    schema_version: Label
    status: Label
    assessed_by_box13: bool  # Box 13 recorded this exact dataset (hash) as its own input


class ReportLimits(BaseModel):
    model_config = _model()
    max_claims: NonNegativeInt = MAX_CLAIMS
    max_tables: NonNegativeInt = MAX_TABLES
    max_rows: NonNegativeInt = MAX_ROWS
    max_sources: NonNegativeInt = MAX_SOURCES
    max_charts: NonNegativeInt = MAX_CHARTS
    max_entries: NonNegativeInt = MAX_ENTRIES
    max_summary_primary: NonNegativeInt = MAX_SUMMARY_PRIMARY
    max_narrative_chars: NonNegativeInt = MAX_NARRATIVE_CHARS
    max_core_events: NonNegativeInt = 40
    max_appendix_rows: NonNegativeInt = 2_000
    max_core_statements_per_call: NonNegativeInt = 6


class ReportMetadata(BaseModel):
    model_config = _model()
    company_key: Label
    company_name: Label
    nse_symbol: Label | None
    isin: Label | None
    bse_code: Label | None
    identity_status: Label
    industry: Industry  # Box 4's subtype of the ONE business classification
    sector_gate: Code
    business_model: Code  # Box 13's order-book dimension (GENERAL = not order-book driven)
    analytical_basis: ClaimBasis
    control_basis: ClaimBasis | None
    currency: Currency
    unit_note: Label
    as_of: date
    latest_fiscal_year_end: date | None
    generated_at: AwareDatetime  # injected; never part of an ID
    inputs: tuple[InputReference, ...]
    limits: ReportLimits
    #: Box 4's coarse business class (FINANCIAL / NON_FINANCIAL / UNKNOWN) and its reason;
    #: None only in reports written before the class was recorded
    business_class: Code | None = None
    classification_reason: Label | None = None
    #: why this analytical basis (Box 13), e.g. a consolidated basis that is not current
    basis_selection: Label | None = None
    engine_version: Label = FINAL_REPORT_ENGINE_VERSION
    selection_rules_version: Label = SELECTION_RULES_VERSION


class ReportIssue(BaseModel):
    model_config = _model()
    code: Code
    severity: Literal["ERROR", "WARNING", "INFO"]
    detail: Label | None = None


# --- dataset ---------------------------------------------------------------------------------

_FACTUAL = (ClaimType.FACT, ClaimType.CALCULATION)


# --- research completeness (analytical truth, apart from execution / rendering) ---------------


#: the section whose status a material requirement gates (COMPLETE never sits over it)
REQUIREMENT_SECTIONS: dict[str, SectionId] = {
    "PEER_ANALYSIS": SectionId.INDUSTRY_COMPETITION,
    "FORECAST": SectionId.FORECASTS,
    "VALUATION": SectionId.VALUATION,
    "RISK_ANALYSIS": SectionId.RISKS,
    "GOVERNANCE": SectionId.MANAGEMENT_GOVERNANCE,
}


class CompletenessBlock(BaseModel):
    """One research requirement of the company's business model, with its evidence status.

    MATERIAL blocks decide the overall status; OPTIONAL blocks are shown, never counted;
    NOT_APPLICABLE never counts against the research."""

    model_config = _model()
    block: Code
    title: Label
    material: bool
    status: SectionStatus
    reasons: tuple[Code, ...] = ()  # structured codes, one per incomplete requirement kind
    detail: Label | None = None
    section_ids: tuple[SectionId, ...] = ()

    @model_validator(mode="after")
    def reasoned(self) -> Self:
        if self.status in (SectionStatus.PARTIAL, SectionStatus.UNAVAILABLE) and not self.reasons:
            raise ValueError("an incomplete requirement states its reason codes")
        if self.status == SectionStatus.COMPLETE and self.reasons:
            raise ValueError("a COMPLETE requirement carries no incompleteness reason")
        return self


Overall = Literal[SectionStatus.COMPLETE, SectionStatus.PARTIAL, SectionStatus.UNAVAILABLE]


def overall_completeness(blocks: Iterable[CompletenessBlock]) -> Overall:
    """COMPLETE only when every MATERIAL applicable block is COMPLETE; UNAVAILABLE when none
    holds a meaningful result; PARTIAL otherwise. OPTIONAL and NOT_APPLICABLE never count."""
    material = [b.status for b in blocks if b.material and b.status != SectionStatus.NOT_APPLICABLE]
    if not material or all(st == SectionStatus.UNAVAILABLE for st in material):
        return SectionStatus.UNAVAILABLE
    if all(st == SectionStatus.COMPLETE for st in material):
        return SectionStatus.COMPLETE
    return SectionStatus.PARTIAL


class ResearchCompleteness(BaseModel):
    """THE research completeness conclusion: derived from the material-requirement ledger of
    the company's Box 4 business model - never from the pipeline having run."""

    model_config = _model()
    business_model: Code  # NON_FINANCIAL / BANK / NON_BANK_FINANCIAL / UNKNOWN
    subtype: Code | None = None  # non-bank financial subtype when Box 4 resolved it
    overall: Overall
    blocks: tuple[CompletenessBlock, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def derived(self) -> Self:
        if len({b.block for b in self.blocks}) != len(self.blocks):
            raise ValueError("one ledger entry per requirement")
        if self.overall != overall_completeness(self.blocks):
            raise ValueError("overall completeness must follow the material-requirement ledger")
        return self

    def by_status(self, status: SectionStatus, *, material: bool | None = None) -> list[str]:
        return [b.block for b in self.blocks if b.status == status
                and (material is None or b.material == material)]


class FinalResearchReportDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["FINAL_RESEARCH_REPORT_DATASET_V1"] = FINAL_REPORT_SCHEMA
    report_id: UUID
    metadata: ReportMetadata
    sections: tuple[ReportSection, ...]
    due_diligence_status: DueDiligenceStatus
    executive_summary: ExecutiveSummary
    investment_case: InvestmentCase
    guidance_tracker: tuple[GuidanceTrackerEntry, ...] = Field(max_length=MAX_ENTRIES)
    order_book: OrderBookView
    valuation: ValuationView
    risks: RiskPresentation
    what_changes_the_view: tuple[ViewChangeEntry, ...] = Field(max_length=MAX_ENTRIES)
    monitoring: tuple[MonitoringItem, ...] = Field(max_length=MAX_ENTRIES)
    reconciliation: tuple[ReconciliationEntry, ...] = Field(max_length=MAX_ENTRIES)
    source_disagreements: tuple[DisagreementView, ...] = Field(max_length=MAX_ENTRIES)
    appendices: tuple[Appendix, ...]
    claims: tuple[ReportClaim, ...] = Field(max_length=MAX_CLAIMS)
    tables: tuple[ReportTable, ...] = Field(max_length=MAX_TABLES)
    charts: tuple[ChartReference, ...] = Field(max_length=MAX_CHARTS)
    sources: tuple[ReportSource, ...] = Field(max_length=MAX_SOURCES)
    issues: tuple[ReportIssue, ...] = ()
    #: the analytical completeness conclusion (None only on reports composed before it existed)
    research_completeness: ResearchCompleteness | None = None
    #: Box 14 composes research; it carries no rating, target or recommendation.
    recommendation: Literal["NONE"] = "NONE"

    def claim(self, claim_id: UUID) -> ReportClaim:
        return next(c for c in self.claims if c.claim_id == claim_id)

    def section(self, section_id: SectionId) -> ReportSection:
        return next(s for s in self.sections if s.section_id == section_id)

    def table(self, code: str) -> ReportTable:
        return next(t for t in self.tables if t.code == code)

    @model_validator(mode="after")
    def integrity(self) -> Self:  # noqa: C901 - one place for every cross-reference rule
        def fail(message: str) -> None:
            raise ValueError(message)

        # --- section order and identity
        if tuple(s.section_id for s in self.sections) != SECTION_ORDER:
            fail("Every canonical section appears once, in canonical order")
        if [s.order for s in self.sections] != list(range(len(SECTION_ORDER))):
            fail("Section order numbers follow the canonical order")
        # --- completeness: a requirement is never COMPLETE over an incomplete section
        if self.research_completeness is not None:
            by_section = {s.section_id: s.status for s in self.sections}
            for block in self.research_completeness.blocks:
                if block.status == SectionStatus.COMPLETE and any(
                        by_section[i] in (SectionStatus.PARTIAL, SectionStatus.UNAVAILABLE)
                        for i in block.section_ids):
                    fail(f"Requirement {block.block} is COMPLETE over an incomplete section")
            if (self.research_completeness.overall != SectionStatus.COMPLETE
                    and by_section[SectionId.EXECUTIVE_SUMMARY] == SectionStatus.COMPLETE):
                fail("The executive summary is never more complete than the research")
            # ... and a section is never COMPLETE over its own incomplete requirement
            for block in self.research_completeness.blocks:
                gated = REQUIREMENT_SECTIONS.get(block.block)
                if gated is not None and block.status in (
                        SectionStatus.PARTIAL, SectionStatus.UNAVAILABLE) and (
                        by_section[gated] == SectionStatus.COMPLETE):
                    fail(f"Section {gated.value} is COMPLETE over requirement {block.block} "
                         f"{block.status.value}")
        for s in self.sections:
            if s.status == SectionStatus.COMPLETE and (
                    s.completeness.missing or s.completeness.material_gaps):
                fail(f"Section {s.section_id.value} is COMPLETE with missing inputs or "
                     "material-evidence gaps")
        claims = {c.claim_id: c for c in self.claims}
        if len(claims) != len(self.claims):
            fail("Claim IDs are unique")
        tables = {t.table_id: t for t in self.tables}
        if len(tables) != len(self.tables):
            fail("Table IDs are unique")
        charts = {c.chart_id for c in self.charts}
        if len(charts) != len(self.charts):
            fail("Chart IDs are unique")
        sources = {s.source_id: s for s in self.sources}
        if len(sources) != len(self.sources) or len({s.key for s in self.sources}) != len(
                self.sources):
            fail("The source register is deduplicated")
        risk_ids = {r.risk_id for r in self.risks.risk_register}
        if len(risk_ids) != len(self.risks.risk_register):
            fail("Risk register IDs are unique")

        def known(ids: tuple[UUID, ...], what: str) -> None:
            if not set(ids) <= claims.keys():
                fail(f"{what} references an unknown claim")

        # --- claims: provenance, risks, period, basis
        as_of = self.metadata.as_of
        for c in self.claims:
            if not set(c.source_ids) <= sources.keys():
                fail(f"A claim references an unregistered source ({c.topic})")
            if not set(c.risk_ids) <= risk_ids:
                fail(f"A claim references an unknown risk ({c.topic})")
            known(c.derived_from, "An inference")
            kinds = {sources[s].source_type for s in c.source_ids}
            if c.claim_type in _FACTUAL and c.value is not None and not (
                    kinds - {SourceType.UPSTREAM_DATASET}):
                fail(f"A numeric fact / calculation reaches a document or record ({c.topic})")
            if c.claim_type == ClaimType.FACT and kinds and kinds <= {
                    SourceType.ANALYST_INPUT, SourceType.UPSTREAM_DATASET}:
                fail(f"A company fact is never sourced from analyst input alone ({c.topic})")
            if c.claim_type == ClaimType.FACT and any(
                    claims[d].claim_type == ClaimType.ANALYST_INFERENCE for d in c.derived_from):
                fail(f"A company fact is never derived from an analyst inference ({c.topic})")
            if c.claim_type != ClaimType.SCENARIO and c.period_end is not None and (
                    c.period_end > as_of):
                fail(f"A non-scenario claim is dated after the report as_of ({c.topic})")
        # --- sections and tables
        for s in self.sections:
            known(s.claim_ids, "A section")
            known(s.limitation_claim_ids, "A section")
            if any(claims[i].claim_type != ClaimType.DATA_LIMITATION
                   for i in s.limitation_claim_ids):
                fail("A section's limitation list holds data limitations only")
            if not set(s.table_ids) <= tables.keys() or not set(s.chart_ids) <= charts:
                fail("A section references an unknown table or chart")
        for t in self.tables:
            known(t.claim_ids, "A table")
            if not set(t.source_ids) <= sources.keys():
                fail("A table references an unregistered source")
            columns = {col.key: col for col in t.columns}
            for row in t.rows:
                if row.claim_id is not None:
                    known((row.claim_id,), "A table row")
                for cell in row.cells:
                    if cell.text_origin == "REPORT" and cell.text and advice_term(cell.text):
                        fail("Box 14 never writes advice language")
                    if cell.claim_id is None:
                        continue
                    known((cell.claim_id,), "A table cell")
                    claim = claims[cell.claim_id]
                    if cell.value is not None and claim.value != cell.value:
                        fail(f"A table value equals the value of the claim it cites ({t.code})")
                    column = columns[cell.column]
                    stated = next((x.text for x in row.cells if x.column == "BASIS"), None)
                    expected = column.basis or (ClaimBasis(stated) if stated in (
                        "CONSOLIDATED", "STANDALONE") else None) or t.basis
                    if claim.basis not in (expected, ClaimBasis.NOT_APPLICABLE):
                        fail(f"A table never mixes reporting bases silently ({t.code})")
                    scenario = claim.claim_type == ClaimType.SCENARIO
                    if column.kind == ColumnKind.FORECAST and not scenario:
                        fail(f"A forecast column holds scenario claims only ({t.code})")
                    if column.kind == ColumnKind.HISTORICAL and scenario:
                        fail(f"A forecast is never shown as history ({t.code})")
                    if row.kind == RowKind.HISTORICAL and scenario:
                        fail(f"A forecast is never labelled historical ({t.code})")
                    if row.kind in (RowKind.FORECAST, RowKind.ASSUMPTION) and (
                            column.kind != ColumnKind.HISTORICAL) and not scenario:
                        fail(f"A forecast row holds scenarios outside history ({t.code})")
                    if row.kind == RowKind.MANAGEMENT_STATEMENT and (
                            claim.claim_type != ClaimType.MANAGEMENT_STATEMENT):
                        fail(f"A management-statement row holds management statements ({t.code})")
            for text in (t.title, *t.footnotes, *(c.label for c in t.columns)):
                if advice_term(text):
                    fail("Box 14 never writes advice language")
        # --- executive summary: only existing claims from completed sections
        body = {i for s in self.sections if s.section_id != SectionId.EXECUTIVE_SUMMARY
                for i in (*s.claim_ids, *s.limitation_claim_ids)}
        summary = self.executive_summary
        listed = {i for p in summary.points for i in p.claim_ids}
        if not listed <= body:
            fail("The executive summary only cites claims of completed report sections")
        if not set(summary.primary_claim_ids) <= listed or not set(
                summary.risk_claim_ids) <= listed or not set(summary.limitation_claim_ids) <= (
                listed):
            fail("Summary claim lists are drawn from its points")
        reds = set(self.risks.red)
        red_claims = {r.claim_id for r in self.risks.risk_register if r.risk_id in reds}
        if not red_claims <= set(summary.risk_claim_ids):
            fail("Every RED company risk reaches the executive summary")
        controls = set(self.risks.positive_controls)
        if controls and not any(claims[i].polarity == Polarity.POSITIVE for i in listed):
            fail("Positive evidence is never erased from the summary")
        if (reds or self.risks.yellow) and not any(
                claims[i].polarity == Polarity.NEGATIVE for i in listed):
            fail("Risks are never erased from the summary")
        # --- a section holding applicable structured items is never wholly not applicable
        items = {SectionId.MONITORING: {m.item_id for m in self.monitoring},
                 SectionId.WHAT_CHANGES_THE_VIEW: {e.entry_id for e in self.what_changes_the_view}}
        if not {i for p in summary.points for i in p.monitoring_item_ids} <= items[
                SectionId.MONITORING]:
            fail("The executive summary only cites existing monitoring items")
        for s in self.sections:
            if s.status == SectionStatus.NOT_APPLICABLE and items.get(s.section_id):
                fail(f"A section with applicable items is not wholly not applicable "
                     f"({s.section_id.value})")
        # --- risks: company risk and data limitation never share a list
        by_risk = {r.risk_id: r for r in self.risks.risk_register}
        for r in self.risks.risk_register:
            known((r.claim_id, *r.input_claim_ids), "A risk entry")
            claim = claims[r.claim_id]
            if r.status == "EVALUATED" and claim.claim_type == ClaimType.DATA_LIMITATION:
                fail("An evaluated risk is not a data limitation")
            if r.status != "EVALUATED" and r.severity is not None:
                fail("Only an evaluated signal carries a severity")
        for ids, severity in ((self.risks.red, "RED"), (self.risks.yellow, "YELLOW")):
            for i in ids:
                entry = by_risk.get(i)
                if entry is None or entry.kind != RiskKind.COMPANY_RISK or (
                        entry.severity != severity or entry.status != "EVALUATED"):
                    fail(f"The {severity} list holds evaluated {severity} company risks only")
                    continue
                if claims[entry.claim_id].claim_type == ClaimType.DATA_LIMITATION:
                    fail("A data limitation never appears as a company risk")
        for i in self.risks.positive_controls:
            if i not in by_risk or by_risk[i].kind != RiskKind.POSITIVE_CONTROL:
                fail("The positive-control list holds positive controls only")
        cov = self.risks.coverage
        if cov is not None:
            company = {r.risk_id for r in self.risks.risk_register
                       if r.kind == RiskKind.COMPANY_RISK}
            if {r.risk_id for r in cov.rules} != company or len(cov.rules) != len(company):
                fail("Risk coverage lists every company-risk rule exactly once")
            for rule in cov.rules:
                entry = by_risk[rule.risk_id]
                graded = rule.outcome in ("RED", "YELLOW", "GREEN")
                if graded != (entry.status == "EVALUATED" and entry.severity is not None) or (
                        graded and rule.outcome != entry.severity):
                    fail("A rule's coverage outcome is its Box 13 grade")
            if (cov.red, cov.yellow) != (len(self.risks.red), len(self.risks.yellow)):
                fail("Risk coverage counts the RED / YELLOW lists")
        for disclosed in self.risks.management_disclosed:
            known((disclosed.claim_id,), "A management-disclosed risk")
            if claims[disclosed.claim_id].claim_type != ClaimType.MANAGEMENT_STATEMENT:
                fail("A management-disclosed risk is a management statement, never a grade")
        for d in self.risks.data_limitations:
            known((d.claim_id,), "A data limitation")
            if claims[d.claim_id].claim_type != ClaimType.DATA_LIMITATION:
                fail("A data-limitation entry is a DATA_LIMITATION claim")
        for o in self.risks.open_diligence:
            known((o.claim_id,), "A diligence item")
            if not set(o.related_risk_ids) <= risk_ids:
                fail("A diligence item references an unknown risk")
        # --- order book: L1 never in confirmed
        for b in self.order_book.buckets:
            known((*b.value_claim_ids, *((b.count_claim_id,) if b.count_claim_id else ())),
                  "An order bucket")
            for i in b.value_claim_ids:
                topic = claims[i].topic
                l1 = "L1" in topic.split("_")
                if (b.bucket == OrderBucketKind.CONFIRMED_ORDER_BOOK) == l1:
                    fail("L1 values never enter the confirmed order book (and vice versa)")
        known(self.order_book.reported_order_book_claim_ids, "The order book")
        known(self.order_book.metric_claim_ids, "The order book")
        # --- management statements, scenarios and valuation stay what they are
        for g in self.guidance_tracker:
            known((g.statement_claim_id, *g.earlier_statement_claim_ids),
                  "The guidance tracker")
            if claims[g.statement_claim_id].claim_type != ClaimType.MANAGEMENT_STATEMENT:
                fail("Guidance is a management statement until realised")
            if g.actual_claim_id is not None:
                known((g.actual_claim_id,), "The guidance tracker")
                if claims[g.actual_claim_id].claim_type not in _FACTUAL:
                    fail("A guidance outcome links a reported fact or calculation")
        for case in self.investment_case.cases:
            known((*case.driver_claim_ids, *case.forecast_claim_ids, *case.valuation_claim_ids),
                  "A scenario case")
            for i in (*case.driver_claim_ids, *case.forecast_claim_ids,
                      *case.valuation_claim_ids):
                if claims[i].claim_type != ClaimType.SCENARIO or claims[i].scenario != (
                        case.scenario):
                    fail("A scenario case cites its own scenario's claims only")
            if not set(case.risk_ids) <= risk_ids:
                fail("A scenario case references an unknown risk")
        for m in self.valuation.methods:
            known((*m.assumption_claim_ids, *m.result_claim_ids, *m.limitation_claim_ids),
                  "A valuation method")
            for i in (*m.assumption_claim_ids, *m.result_claim_ids):
                if claims[i].claim_type != ClaimType.SCENARIO or claims[i].scenario != (
                        m.scenario):
                    fail("A valuation method cites its own scenario's outputs only")
        known(self.valuation.market_context_claim_ids, "Market context")
        # --- the remaining structures
        for v in self.what_changes_the_view:
            known(v.related_claim_ids, "A view-change entry")
            if not set(v.related_risk_ids) <= risk_ids:
                fail("A view-change entry references an unknown risk")
        for item in self.monitoring:
            known(item.latest_claim_ids, "A monitoring item")
            if not set(item.related_risk_ids) <= risk_ids:
                fail("A monitoring item references an unknown risk")
        for rec in self.reconciliation:
            known(tuple(i for i in (rec.claim_id, rec.standalone_claim_id,
                                    rec.consolidated_claim_id, rec.difference_claim_id)
                        if i is not None), "A reconciliation")
            if rec.standalone_claim_id and claims[rec.standalone_claim_id].basis != (
                    ClaimBasis.STANDALONE):
                fail("A standalone reconciliation value carries the standalone basis")
            if rec.consolidated_claim_id and claims[rec.consolidated_claim_id].basis != (
                    ClaimBasis.CONSOLIDATED):
                fail("A consolidated reconciliation value carries the consolidated basis")
            if rec.classification == "DATA_LIMITATION" and claims[rec.claim_id].claim_type != (
                    ClaimType.DATA_LIMITATION):
                fail("An unavailable / conflicted reconciliation stays a data limitation")
        for dis in self.source_disagreements:
            known((dis.claim_id,), "A disagreement")
        for a in self.appendices:
            known(a.claim_ids, "An appendix")
            if not set(a.table_ids) <= tables.keys():
                fail("An appendix references an unknown table")
        dd = self.due_diligence_status
        known((*dd.conflict_claim_ids, *dd.unavailable_information_claim_ids), "Section 0")
        if not set(dd.blocked_sources) <= sources.keys():
            fail("Section 0 references an unregistered source")
        if dd.sections_total != len(SECTION_ORDER) or sum(
                n for _, n in dd.sections_by_status) != len(SECTION_ORDER):
            fail("Section 0 counts every section once")
        # --- advice language in every Box 14-written string, and the narrative budget
        written = [s.title for s in self.sections] + [s.status_reason for s in self.sections]
        written += [c.statement for c in self.claims]
        written += [x.thesis for x in self.investment_case.cases]
        written += [y for x in self.investment_case.cases
                    for y in (*x.conditions_required, *x.invalidation_conditions)]
        written += [v.condition for v in self.what_changes_the_view]
        written += [m.reason for m in self.monitoring]
        written += [p.reason or "" for p in summary.points]
        for text in written:
            term = advice_term(text)
            if term:
                fail(f"Box 14 never writes advice language ({term})")
        if sum(len(t) for t in written) > self.metadata.limits.max_narrative_chars:
            fail("The report exceeds its narrative budget")
        return self
