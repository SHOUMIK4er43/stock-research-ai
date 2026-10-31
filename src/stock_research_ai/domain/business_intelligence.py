"""DETAILED_RESEARCH_V1 Box 7: business, segment and operating KPI intelligence.

The structured operating layer: WHAT businesses produced the Box 4 numbers. Every fact
is a reported number or a source excerpt with exact provenance (Box 3 page/block/line,
or a Box 4 observation, or the Box 1B page artifact). Nothing is inferred from general
knowledge, nothing is merged fuzzily, and no history is connected across a segment
structure change unless the source restated it. Unknown labels survive as unmapped.
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
    PositiveInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.evidence import EvidenceDocumentType, SourceAuthority
from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_canonical import (
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    ParsedNumber,
    ReportingBasis,
    Scale,
    ValueKind,
)
from stock_research_ai.domain.financial_charts import (
    ChartArtifact,
    ChartLimits,
    ChartSpec,
    ChartStatistics,
    ChartWarning,
    SkippedChart,
)

BUSINESS_SCHEMA: Literal["BUSINESS_INTELLIGENCE_DATASET_V1"] = "BUSINESS_INTELLIGENCE_DATASET_V1"
ENGINE_VERSION = "business-intelligence/1"
REGISTRY_VERSION = "business-registry/1"
FORMULA_VERSION = "business-formulas/1"
SEGMENT_CHART_REGISTRY_VERSION = "segments/1"

Label = Annotated[str, StringConstraints(max_length=1000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Key = Annotated[str, StringConstraints(min_length=1, max_length=300)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Excerpt = Annotated[str, StringConstraints(max_length=4000)]


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


# --- vocabularies ----------------------------------------------------------------------------


class SectorFamily(StrEnum):
    GENERAL = "GENERAL"
    FINANCIAL = "FINANCIAL"
    IT_SERVICES = "IT_SERVICES"
    POWER_UTILITIES = "POWER_UTILITIES"
    MANUFACTURING = "MANUFACTURING"
    REAL_ESTATE = "REAL_ESTATE"
    RETAIL = "RETAIL"
    HOTELS = "HOTELS"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"  # Box 4's business class unresolved: no sector-specific treatment


class BusinessEvidenceKind(StrEnum):
    BUSINESS_DESCRIPTION = "BUSINESS_DESCRIPTION"
    PRODUCT_SERVICE = "PRODUCT_SERVICE"
    END_MARKET = "END_MARKET"
    CUSTOMER_CHANNEL = "CUSTOMER_CHANNEL"
    CUSTOMER_CONCENTRATION = "CUSTOMER_CONCENTRATION"
    SEGMENT_DESCRIPTION = "SEGMENT_DESCRIPTION"
    SEGMENT_CHANGE = "SEGMENT_CHANGE"
    GEOGRAPHY_DESCRIPTION = "GEOGRAPHY_DESCRIPTION"
    CAPACITY = "CAPACITY"
    UTILIZATION = "UTILIZATION"
    PRODUCTION = "PRODUCTION"
    VOLUME = "VOLUME"
    REALIZATION = "REALIZATION"
    ORDER_BOOK = "ORDER_BOOK"
    BACKLOG = "BACKLOG"
    CAPEX = "CAPEX"
    PROJECT = "PROJECT"
    STORE_COUNT = "STORE_COUNT"
    BRANCH_COUNT = "BRANCH_COUNT"
    CUSTOMER_COUNT = "CUSTOMER_COUNT"
    USER_COUNT = "USER_COUNT"
    SUBSCRIBER_COUNT = "SUBSCRIBER_COUNT"
    EMPLOYEE_COUNT = "EMPLOYEE_COUNT"
    AUM = "AUM"
    LOAN_BOOK = "LOAN_BOOK"
    DISBURSEMENT = "DISBURSEMENT"
    ASSET_QUALITY = "ASSET_QUALITY"
    OCCUPANCY = "OCCUPANCY"
    ROOM_KEYS = "ROOM_KEYS"
    AREA = "AREA"
    ENERGY_CAPACITY = "ENERGY_CAPACITY"
    GENERATION = "GENERATION"
    OTHER = "OTHER"


class SourceKind(StrEnum):
    BOX3_BLOCK = "BOX3_BLOCK"  # one line of a Box 3 text block (page/chars/line)
    BOX3_EVIDENCE = "BOX3_EVIDENCE"  # a Box 3 EvidenceItem excerpt
    BOX4_OBSERVATION = "BOX4_OBSERVATION"  # a Box 4 source observation (-> Box 3 cell)
    BOX1B_PROFILE = "BOX1B_PROFILE"  # the preserved Screener page artifact


class SegmentMetric(StrEnum):
    REVENUE = "REVENUE"
    EXTERNAL_REVENUE = "EXTERNAL_REVENUE"
    INTER_SEGMENT_REVENUE = "INTER_SEGMENT_REVENUE"
    SEGMENT_RESULT = "SEGMENT_RESULT"
    EBIT = "EBIT"  # only when the source labels the result EBIT
    EBITDA = "EBITDA"  # only when actually reported
    ASSETS = "ASSETS"
    LIABILITIES = "LIABILITIES"
    CAPITAL_EMPLOYED = "CAPITAL_EMPLOYED"
    CAPEX = "CAPEX"
    DEPRECIATION = "DEPRECIATION"
    ORDER_BOOK = "ORDER_BOOK"
    REVENUE_SHARE = "REVENUE_SHARE"  # a reported % of revenue, never recomputed
    OTHER = "OTHER"


class SegmentRole(StrEnum):
    REPORTABLE = "REPORTABLE"
    SUBSEGMENT = "SUBSEGMENT"
    TOTAL = "TOTAL"  # the table's own total row/column
    ELIMINATION = "ELIMINATION"  # inter-segment eliminations
    UNALLOCATED = "UNALLOCATED"  # unallocable / corporate items
    RECONCILING_ITEM = "RECONCILING_ITEM"  # other bridge lines to the company total
    COMPANY_TOTAL = "COMPANY_TOTAL"  # e.g. 'Revenue from operations' after eliminations


class SegmentLevel(StrEnum):
    GROUP = "GROUP"
    PRIMARY = "PRIMARY"
    SUBSEGMENT = "SUBSEGMENT"


class NameMatch(StrEnum):
    EXACT = "EXACT"  # identical after documented normalization (case, '&'/'and', markers)
    EXPLICIT_ALIAS = "EXPLICIT_ALIAS"  # configured alias with stated evidence
    RENAMED = "RENAMED"  # the source says so
    POSSIBLE_RENAME = "POSSIBLE_RENAME"  # one in, one out, no statement: never merged
    DISTINCT = "DISTINCT"
    UNKNOWN = "UNKNOWN"


class Comparability(StrEnum):
    COMPARABLE = "COMPARABLE"
    RESTATED_COMPARABLE = "RESTATED_COMPARABLE"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    UNKNOWN = "UNKNOWN"


class LineageKind(StrEnum):
    RENAMED = "RENAMED"
    SPLIT = "SPLIT"
    MERGED = "MERGED"
    REORGANIZED = "REORGANIZED"


class Restated(StrEnum):
    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


class GeographyDimension(StrEnum):
    COUNTRY = "COUNTRY"
    REGION = "REGION"
    DOMESTIC = "DOMESTIC"
    EXPORT = "EXPORT"
    MARKET_GROUP = "MARKET_GROUP"
    TOTAL = "TOTAL"
    UNKNOWN = "UNKNOWN"


class GeographyMetric(StrEnum):
    REVENUE = "REVENUE"
    REVENUE_SHARE = "REVENUE_SHARE"  # a reported % of revenue
    ASSETS = "ASSETS"
    EMPLOYEES = "EMPLOYEES"
    ORDER_BOOK = "ORDER_BOOK"
    OTHER = "OTHER"


class Attribution(StrEnum):
    CUSTOMER_LOCATION = "CUSTOMER_LOCATION"  # domicile / location of the customer
    ENTITY_LOCATION = "ENTITY_LOCATION"  # location of the selling entity
    ASSET_LOCATION = "ASSET_LOCATION"  # location of the assets
    UNKNOWN = "UNKNOWN"


class KPIUnit(StrEnum):
    MONEY = "MONEY"
    PERCENT = "PERCENT"
    BASIS_POINTS = "BASIS_POINTS"
    COUNT = "COUNT"
    PEOPLE = "PEOPLE"
    CUSTOMERS = "CUSTOMERS"
    USERS = "USERS"
    STORES = "STORES"
    BRANCHES = "BRANCHES"
    MW = "MW"
    GW = "GW"
    MU = "MU"
    MWH = "MWH"
    GWH = "GWH"
    TONNES = "TONNES"
    KT = "KT"
    MT = "MT"  # 'MT' as printed (metric or million tonnes): never converted
    MTPA = "MTPA"  # million tonnes per annum (a capacity rate, never summed with MT)
    LITRES = "LITRES"
    KL = "KL"
    SQ_FT = "SQ_FT"
    MSF = "MSF"  # million square feet
    SQ_M = "SQ_M"
    ACRES = "ACRES"
    ROOMS = "ROOMS"
    VEHICLES = "VEHICLES"
    UNITS = "UNITS"
    DAYS = "DAYS"
    X = "X"
    MONEY_PER_UNIT = "MONEY_PER_UNIT"  # realization, ADR, RevPAR
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class KPIPeriodKind(StrEnum):
    FLOW_PERIOD = "FLOW_PERIOD"
    POINT_IN_TIME = "POINT_IN_TIME"
    QUARTER = "QUARTER"
    FY = "FY"
    MONTH = "MONTH"
    DATE = "DATE"
    PROJECT_LIFE = "PROJECT_LIFE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class CapacityStatus(StrEnum):
    INSTALLED = "INSTALLED"
    OPERATIONAL = "OPERATIONAL"
    AVAILABLE = "AVAILABLE"
    UNDER_CONSTRUCTION = "UNDER_CONSTRUCTION"
    PLANNED = "PLANNED"
    ANNOUNCED = "ANNOUNCED"
    UNKNOWN = "UNKNOWN"


class ProjectStatus(StrEnum):
    ANNOUNCED = "ANNOUNCED"
    PLANNED = "PLANNED"
    UNDER_CONSTRUCTION = "UNDER_CONSTRUCTION"
    COMMISSIONED = "COMMISSIONED"
    COMPLETED = "COMPLETED"
    ON_HOLD = "ON_HOLD"
    UNKNOWN = "UNKNOWN"


class ReconOutcome(StrEnum):
    EXACT_MATCH = "EXACT_MATCH"
    MATCH_WITHIN_TOLERANCE = "MATCH_WITHIN_TOLERANCE"
    SOURCE_PRECEDENCE = "SOURCE_PRECEDENCE"
    RESTATEMENT = "RESTATEMENT"
    CONFLICT = "CONFLICT"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    AMBIGUOUS = "AMBIGUOUS"


class TotalOutcome(StrEnum):
    MATCH = "MATCH"
    MATCH_WITHIN_TOLERANCE = "MATCH_WITHIN_TOLERANCE"
    ELIMINATIONS_EXPLAIN_DIFFERENCE = "ELIMINATIONS_EXPLAIN_DIFFERENCE"
    UNALLOCATED_EXPLAINS_DIFFERENCE = "UNALLOCATED_EXPLAINS_DIFFERENCE"
    CONFLICT = "CONFLICT"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class AnalyticStatus(StrEnum):
    CALCULATED = "CALCULATED"
    INPUT_MISSING = "INPUT_MISSING"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    NOT_COMPARABLE = "NOT_COMPARABLE"  # segment structure / definition discontinuity
    BASIS_MISMATCH = "BASIS_MISMATCH"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    PERIOD_MISMATCH = "PERIOD_MISMATCH"
    DIVISION_BY_ZERO = "DIVISION_BY_ZERO"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # e.g. a negative growth base
    INPUT_CONFLICT = "INPUT_CONFLICT"


class Domain(StrEnum):
    SEGMENT = "SEGMENT"
    GEOGRAPHY = "GEOGRAPHY"
    KPI = "KPI"
    TABLE = "TABLE"  # a table Box 7 could not interpret deterministically


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --- provenance ------------------------------------------------------------------------------


class SourceLocation(BaseModel):
    """Walks back to company -> document -> artifact SHA -> page -> block/table -> URL."""

    model_config = _model()
    source_kind: SourceKind
    document_id: UUID | None = None
    extraction_id: UUID | None = None
    document_type: EvidenceDocumentType | None = None
    source_url: Label
    artifact_sha256: Sha256Hex
    authority: SourceAuthority
    page_start: PositiveInt | None = None
    page_end: PositiveInt | None = None
    block_ids: tuple[UUID, ...] = ()
    table_id: UUID | None = None  # the Box 3 table holding the block, when there is one
    line_index: NonNegativeInt | None = None  # line inside the first block
    row: NonNegativeInt | None = None  # row inside the interpreted table frame
    column: NonNegativeInt | None = None  # value column inside the interpreted frame
    box3_evidence_id: UUID | None = None
    box4_observation_id: UUID | None = None
    locator: Label

    @model_validator(mode="after")
    def walkable(self) -> Self:
        if self.source_kind in (SourceKind.BOX3_BLOCK, SourceKind.BOX3_EVIDENCE):
            if self.document_id is None or self.extraction_id is None:
                raise ValueError("Box 3 provenance names its document and extraction")
            if self.page_start is None or (not self.block_ids and self.table_id is None):
                raise ValueError("Box 3 provenance names its page and blocks/table")
        if self.source_kind == SourceKind.BOX3_EVIDENCE and self.box3_evidence_id is None:
            raise ValueError("Box 3 evidence provenance names the evidence item")
        if self.source_kind == SourceKind.BOX4_OBSERVATION and self.box4_observation_id is None:
            raise ValueError("Box 4 provenance names the Box 4 observation")
        if self.page_start and self.page_end and self.page_end < self.page_start:
            raise ValueError("a source cannot end before it starts")
        return self


class BusinessEvidence(BaseModel):
    model_config = _model()
    evidence_id: UUID
    company_key: Label
    evidence_kind: BusinessEvidenceKind
    raw_text: Excerpt
    normalized_text: Excerpt
    section: Code | None
    source: SourceLocation
    confidence: Confidence
    tags: tuple[Code, ...] = ()
    metadata: dict[str, str] = {}


# --- segments --------------------------------------------------------------------------------


class SegmentObservation(BaseModel):
    model_config = _model()
    observation_id: UUID
    company_key: Label
    segment_raw_name: Label
    segment_key: Key  # documented normalization of the raw name (never fuzzy)
    segment_canonical_name: Label | None = None  # set when safely mapped
    segment_parent_key: Key | None = None
    segment_level: SegmentLevel
    segment_role: SegmentRole
    metric: SegmentMetric
    raw_metric_label: Label
    raw_value: Label
    parsed: ParsedNumber
    value_kind: ValueKind
    normalized_value: ExactDecimal | None  # MONEY: Box 4 canonical scale (crore)
    currency: Currency
    scale: Scale
    raw_unit: Label | None
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    basis_method: Code  # EXPLICIT_CAPTION / SECTION_SCOPE / DOCUMENT_TITLE / UNKNOWN
    audited: bool | None = None
    restated: bool = False
    frame_id: UUID  # the interpreted table the value belongs to
    source: SourceLocation
    confidence: Confidence
    warnings: tuple[Code, ...] = ()
    metadata: dict[str, str] = {}


class CanonicalSegmentValue(BaseModel):
    model_config = _model()
    canonical_value_id: UUID
    segment_key: Key
    segment_name: Label
    segment_role: SegmentRole
    metric: SegmentMetric
    value: ExactDecimal
    value_kind: ValueKind
    currency: Currency
    scale: Scale
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    selected_observation_id: UUID
    supporting_observation_ids: tuple[UUID, ...]
    conflicting_observation_ids: tuple[UUID, ...]
    reconciliation_record_id: UUID
    outcome: ReconOutcome
    confidence: Confidence


class SegmentDefinition(BaseModel):
    model_config = _model()
    segment_key: Key
    canonical_name: Label
    raw_names: tuple[Label, ...]
    role: SegmentRole
    level: SegmentLevel
    parent_key: Key | None = None
    child_keys: tuple[Key, ...] = ()
    name_match: NameMatch  # how its raw names were unified
    alias_evidence: tuple[Label, ...] = ()
    reporting_bases: tuple[ReportingBasis, ...]
    first_period_end: date | None
    last_period_end: date | None
    observation_count: NonNegativeInt
    description_evidence_ids: tuple[UUID, ...] = ()


class SegmentNameRelation(BaseModel):
    model_config = _model()
    relation_id: UUID
    from_key: Key
    to_key: Key
    outcome: NameMatch
    reason: Label
    evidence_ids: tuple[UUID, ...] = ()


class SegmentLineageEvent(BaseModel):
    model_config = _model()
    event_id: UUID
    kind: LineageKind
    effective_period_end: date | None
    old_segments: tuple[Label, ...]
    new_segments: tuple[Label, ...]
    restated_comparatives: Restated
    raw_text: Excerpt
    evidence_id: UUID
    source: SourceLocation


class SegmentStructure(BaseModel):
    model_config = _model()
    structure_id: UUID
    reporting_basis: ReportingBasis
    frequency: Code  # FY / QUARTER
    period_end: date
    segment_keys: tuple[Key, ...]  # reportable segments with revenue for this period
    document_ids: tuple[UUID, ...]
    presented_in_later_document: bool  # comparatives re-presented by a later filing


class ComparabilityAssessment(BaseModel):
    model_config = _model()
    assessment_id: UUID
    reporting_basis: ReportingBasis
    frequency: Code
    from_period_end: date
    to_period_end: date
    status: Comparability
    added_segments: tuple[Key, ...] = ()
    removed_segments: tuple[Key, ...] = ()
    renamed: tuple[tuple[Key, Key], ...] = ()
    lineage_event_ids: tuple[UUID, ...] = ()
    reason: Label


# --- geography -------------------------------------------------------------------------------


class GeographyObservation(BaseModel):
    model_config = _model()
    observation_id: UUID
    company_key: Label
    geography_raw_name: Label
    geography_key: Key
    dimension: GeographyDimension
    attribution: Attribution
    attribution_text: Label | None = None
    metric: GeographyMetric
    raw_metric_label: Label
    raw_value: Label
    parsed: ParsedNumber
    value_kind: ValueKind
    normalized_value: ExactDecimal | None
    currency: Currency
    scale: Scale
    raw_unit: Label | None
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    basis_method: Code
    is_total: bool = False
    frame_id: UUID
    source: SourceLocation
    confidence: Confidence
    warnings: tuple[Code, ...] = ()
    metadata: dict[str, str] = {}


class CanonicalGeographyValue(BaseModel):
    model_config = _model()
    canonical_value_id: UUID
    geography_key: Key
    geography_name: Label
    dimension: GeographyDimension
    attribution: Attribution
    metric: GeographyMetric
    value: ExactDecimal
    value_kind: ValueKind
    currency: Currency
    scale: Scale
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    is_total: bool
    selected_observation_id: UUID
    supporting_observation_ids: tuple[UUID, ...]
    conflicting_observation_ids: tuple[UUID, ...]
    reconciliation_record_id: UUID
    outcome: ReconOutcome
    confidence: Confidence


class GeographyDefinition(BaseModel):
    model_config = _model()
    geography_key: Key
    name: Label
    raw_names: tuple[Label, ...]
    dimension: GeographyDimension
    attributions: tuple[Attribution, ...]


# --- operating KPIs --------------------------------------------------------------------------


class OperatingKPIObservation(BaseModel):
    model_config = _model()
    kpi_observation_id: UUID
    company_key: Label
    canonical_kpi: Code
    kpi_family: SectorFamily  # the registry family the definition belongs to
    raw_label: Label
    qualifier: Code | None = None  # e.g. capacity status, EXCLUDING_TRAINEES
    raw_value: Label
    parsed: ParsedNumber
    unit: KPIUnit
    raw_unit: Label | None
    currency: Currency | None = None  # money / money-per-unit KPIs only
    scale: Scale
    normalized_value: ExactDecimal | None  # MONEY: crore; otherwise as reported
    period: FinancialPeriod | None
    period_kind: KPIPeriodKind
    as_of: date | None = None
    reporting_basis: ReportingBasis
    segment_key: Key | None = None
    segment_raw_name: Label | None = None
    geography_key: Key | None = None
    geography_raw_name: Label | None = None
    definition_text: Label | None = None
    frame_id: UUID
    source: SourceLocation
    confidence: Confidence
    warnings: tuple[Code, ...] = ()
    metadata: dict[str, str] = {}


class CanonicalKPIValue(BaseModel):
    model_config = _model()
    canonical_value_id: UUID
    canonical_kpi: Code
    qualifier: Code | None
    segment_key: Key | None
    geography_key: Key | None
    value: ExactDecimal
    unit: KPIUnit
    currency: Currency | None
    scale: Scale
    period: FinancialPeriod
    period_kind: KPIPeriodKind
    reporting_basis: ReportingBasis
    selected_observation_id: UUID
    supporting_observation_ids: tuple[UUID, ...]
    conflicting_observation_ids: tuple[UUID, ...]
    reconciliation_record_id: UUID
    outcome: ReconOutcome
    confidence: Confidence
    box4_canonical_value_id: UUID | None = None  # Box 4 already holds this concept
    box4_comparison: Code | None = None  # EXACT_MATCH / MATCH_WITHIN_TOLERANCE / CONFLICT


class CapacityFact(BaseModel):
    model_config = _model()
    fact_id: UUID
    capacity_status: CapacityStatus
    value: ExactDecimal | None
    unit: KPIUnit
    raw_unit: Label | None
    as_of: date | None
    period: FinancialPeriod | None
    segment_key: Key | None
    site: Label | None
    geography_key: Key | None
    raw_label: Label
    kpi_observation_id: UUID
    source: SourceLocation
    confidence: Confidence


class OrderBookFact(BaseModel):
    model_config = _model()
    fact_id: UUID
    kind: Literal["ORDER_BOOK", "ORDER_INFLOW", "BACKLOG"]
    value: ExactDecimal | None
    currency: Currency | None
    scale: Scale
    normalized_value: ExactDecimal | None
    as_of: date | None
    period: FinancialPeriod | None
    scope: Label  # COMPANY or the segment / geography it is stated for
    definition: Label | None
    executable_period: Label | None
    raw_label: Label
    kpi_observation_id: UUID
    source: SourceLocation
    confidence: Confidence


class ProjectFact(BaseModel):
    model_config = _model()
    project_id: UUID
    name: Label
    segment: Label | None = None
    location: Label | None = None
    project_type: Label | None = None
    capacity_addition_raw: Label | None = None
    capacity_addition: ExactDecimal | None = None
    capacity_unit: KPIUnit | None = None
    announced_raw: Label | None = None
    expected_completion_raw: Label | None = None
    actual_completion_raw: Label | None = None
    capex_raw: Label | None = None
    capex_value: ExactDecimal | None = None
    capex_currency: Currency | None = None
    capex_scale: Scale | None = None
    capex_normalized: ExactDecimal | None = None
    status: ProjectStatus
    raw_status: Label | None = None
    fields: tuple[tuple[Label, Label], ...]  # every 'Key: value' line, verbatim
    source: SourceLocation
    confidence: Confidence


# --- reconciliation & analytics --------------------------------------------------------------


class ReconciliationRecord(BaseModel):
    model_config = _model()
    record_id: UUID
    domain: Domain
    key: Label  # the comparability key, human-readable
    outcome: ReconOutcome
    observation_ids: tuple[UUID, ...]
    selected_observation_id: UUID | None
    difference: ExactDecimal | None = None
    tolerance: ExactDecimal | None = None
    reason: Label


class TotalAdjustment(BaseModel):
    model_config = _model()
    label: Label
    role: SegmentRole
    signed_value: ExactDecimal
    observation_id: UUID


class TotalReconciliation(BaseModel):
    model_config = _model()
    check_id: UUID
    domain: Domain
    metric: Code
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    currency: Currency
    frame_id: UUID
    component_sum: ExactDecimal | None
    component_ids: tuple[UUID, ...]
    reported_total: ExactDecimal | None
    reported_total_source: Code  # TABLE_TOTAL / BOX4_CANONICAL / NONE
    reported_total_ref: UUID | None
    adjustments: tuple[TotalAdjustment, ...] = ()
    difference: ExactDecimal | None
    tolerance: ExactDecimal | None
    outcome: TotalOutcome
    explanation: Label


class SegmentAnalytic(BaseModel):
    model_config = _model()
    analytic_id: UUID
    code: Code
    formula_id: Label
    subject_domain: Domain
    subject_key: Key
    subject_name: Label
    qualifier: Code | None = None
    period: FinancialPeriod
    comparison_period: FinancialPeriod | None = None
    value: ExactDecimal | None
    unit: Literal["PERCENT"]
    status: AnalyticStatus
    reason: Label
    reporting_basis: ReportingBasis
    currency: Currency | None
    comparability: Comparability | None = None
    input_canonical_ids: tuple[UUID, ...]
    input_observation_ids: tuple[UUID, ...]
    metadata: dict[str, str] = {}

    @model_validator(mode="after")
    def value_iff_calculated(self) -> Self:
        if (self.status == AnalyticStatus.CALCULATED) != (self.value is not None):
            raise ValueError("only a calculated analytic carries a value")
        return self


class FormulaDefinition(BaseModel):
    model_config = _model()
    formula_id: Label
    code: Code
    expression: Label
    rules: Label


class UnmappedOperatingLine(BaseModel):
    model_config = _model()
    line_id: UUID
    company_key: Label
    domain: Domain
    raw_label: Label
    raw_values: tuple[Label, ...]
    reason: Code
    context: Label | None
    sector_family: SectorFamily
    document_type: EvidenceDocumentType | None
    source: SourceLocation


class BusinessWarning(BaseModel):
    model_config = _model()
    code: Code
    severity: Severity
    detail: Label | None = None


class BusinessLimits(BaseModel):
    model_config = _model()
    max_documents: PositiveInt = 200
    max_tables: PositiveInt = 5_000
    max_business_evidence: PositiveInt = 2_000
    max_segment_observations: PositiveInt = 20_000
    max_geography_observations: PositiveInt = 10_000
    max_kpi_observations: PositiveInt = 20_000
    max_project_facts: PositiveInt = 1_000
    max_analytics: PositiveInt = 20_000
    max_unmapped: PositiveInt = 10_000
    max_lines_per_frame: PositiveInt = 400


class UpstreamReference(BaseModel):
    model_config = _model()
    layer: Literal["BOX1B", "BOX3", "BOX4"]
    dataset_id: UUID | None
    sha256: Sha256Hex
    schema_version: Label


class ClassificationRef(BaseModel):
    model_config = _model()
    level: Label
    name: Label
    source_url: Label


class BusinessStructure(BaseModel):
    model_config = _model()
    company_key: Label
    company_name: Label
    sector_family: SectorFamily
    sector_reason: Label
    industry: Industry
    classification: tuple[ClassificationRef, ...]
    reporting_bases: tuple[ReportingBasis, ...]
    segments: tuple[SegmentDefinition, ...]
    geographies: tuple[GeographyDefinition, ...]
    description_evidence_ids: tuple[UUID, ...]
    product_service_evidence_ids: tuple[UUID, ...]
    end_market_evidence_ids: tuple[UUID, ...]
    customer_channel_evidence_ids: tuple[UUID, ...]
    segment_description_evidence_ids: tuple[UUID, ...]


class BusinessStatistics(BaseModel):
    model_config = _model()
    documents_consumed: NonNegativeInt
    frames_interpreted: NonNegativeInt
    business_evidence_items: NonNegativeInt
    segment_observations: NonNegativeInt
    canonical_segment_values: NonNegativeInt
    geography_observations: NonNegativeInt
    canonical_geography_values: NonNegativeInt
    kpi_observations: NonNegativeInt
    canonical_kpi_values: NonNegativeInt
    capacity_facts: NonNegativeInt
    order_book_facts: NonNegativeInt
    project_facts: NonNegativeInt
    mapped_kpis: NonNegativeInt  # distinct canonical KPI codes observed
    unmapped_kpis: NonNegativeInt  # unmapped KPI lines
    unmapped_lines: NonNegativeInt
    segment_conflicts: NonNegativeInt
    geography_conflicts: NonNegativeInt
    kpi_conflicts: NonNegativeInt
    segment_structure_changes: NonNegativeInt
    analytics_calculated: NonNegativeInt
    analytics_not_calculable: NonNegativeInt
    box4_segment_observations_considered: NonNegativeInt
    box4_segment_observations_accepted: NonNegativeInt
    warnings_by_code: dict[str, int]


class BusinessIntelligenceDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["BUSINESS_INTELLIGENCE_DATASET_V1"] = BUSINESS_SCHEMA
    engine_version: Label = ENGINE_VERSION
    registry_version: Label = REGISTRY_VERSION
    formula_version: Label = FORMULA_VERSION
    dataset_id: UUID
    company_key: Label
    company_name: Label
    sector: SectorFamily
    generated_at: AwareDatetime
    source_box1b: UpstreamReference | None
    source_box3: UpstreamReference | None
    source_box4: UpstreamReference
    limits: BusinessLimits
    status: Literal["COMPLETE", "PARTIAL"]
    business_structure: BusinessStructure
    business_evidence: tuple[BusinessEvidence, ...]
    segment_observations: tuple[SegmentObservation, ...]
    canonical_segment_values: tuple[CanonicalSegmentValue, ...]
    segment_name_relations: tuple[SegmentNameRelation, ...]
    segment_structures: tuple[SegmentStructure, ...]
    segment_comparability: tuple[ComparabilityAssessment, ...]
    segment_lineage: tuple[SegmentLineageEvent, ...]
    geography_observations: tuple[GeographyObservation, ...]
    canonical_geography_values: tuple[CanonicalGeographyValue, ...]
    operating_kpi_observations: tuple[OperatingKPIObservation, ...]
    canonical_kpi_values: tuple[CanonicalKPIValue, ...]
    capacity_facts: tuple[CapacityFact, ...]
    order_book_facts: tuple[OrderBookFact, ...]
    project_facts: tuple[ProjectFact, ...]
    formulas: tuple[FormulaDefinition, ...]
    segment_analytics: tuple[SegmentAnalytic, ...]
    reconciliation_records: tuple[ReconciliationRecord, ...]
    total_reconciliations: tuple[TotalReconciliation, ...]
    unmapped_lines: tuple[UnmappedOperatingLine, ...]
    warnings: tuple[BusinessWarning, ...]
    statistics: BusinessStatistics

    @model_validator(mode="after")
    def no_orphans(self) -> Self:
        def unique(ids: list[UUID], what: str) -> set[UUID]:
            if len(ids) != len(set(ids)):
                raise ValueError(f"{what} IDs must be unique")
            return set(ids)

        segments = unique([o.observation_id for o in self.segment_observations], "segment")
        geos = unique([o.observation_id for o in self.geography_observations], "geography")
        kpis = unique([o.kpi_observation_id for o in self.operating_kpi_observations], "KPI")
        evidence = unique([e.evidence_id for e in self.business_evidence], "evidence")
        records = unique([r.record_id for r in self.reconciliation_records], "record")
        canonical: set[UUID] = set()
        for values, observed in (
            (self.canonical_segment_values, segments),
            (self.canonical_geography_values, geos),
            (self.canonical_kpi_values, kpis),
        ):
            for v in values:
                linked = {
                    v.selected_observation_id,
                    *v.supporting_observation_ids,
                    *v.conflicting_observation_ids,
                }
                if not linked <= observed:
                    raise ValueError(f"canonical value {v.canonical_value_id} is orphaned")
                if v.reconciliation_record_id not in records:
                    raise ValueError(f"canonical value {v.canonical_value_id} has no record")
                canonical.add(v.canonical_value_id)
        observed_all = segments | geos | kpis
        for a in self.segment_analytics:
            if not set(a.input_canonical_ids) <= canonical:
                raise ValueError(f"analytic {a.analytic_id} references unknown inputs")
            if not set(a.input_observation_ids) <= observed_all:
                raise ValueError(f"analytic {a.analytic_id} references unknown observations")
        facts = [(f.fact_id, f.kpi_observation_id) for f in self.capacity_facts] + [
            (f.fact_id, f.kpi_observation_id) for f in self.order_book_facts
        ]
        for fact_id, kpi_id in facts:
            if kpi_id not in kpis:
                raise ValueError(f"fact {fact_id} has no KPI observation")
        for r in self.reconciliation_records:
            if not set(r.observation_ids) <= observed_all:
                raise ValueError(f"record {r.record_id} references unknown observations")
        for check in self.total_reconciliations:
            ids = {*check.component_ids, *(a.observation_id for a in check.adjustments)}
            if not ids <= observed_all:
                raise ValueError(f"total check {check.check_id} references unknown inputs")
        s = self.business_structure
        referenced = {
            *s.description_evidence_ids,
            *s.product_service_evidence_ids,
            *s.end_market_evidence_ids,
            *s.customer_channel_evidence_ids,
            *s.segment_description_evidence_ids,
            *(i for d in s.segments for i in d.description_evidence_ids),
            *(e.evidence_id for e in self.segment_lineage),
            *(i for r in self.segment_name_relations for i in r.evidence_ids),
        }
        if not referenced <= evidence:
            raise ValueError("the business structure references unknown evidence")
        return self


# --- optional Box 6-compatible segment charts -----------------------------------------------


class BusinessChartBundle(BaseModel):
    """Additive `segments/1` charts rendered by the Box 6 renderer from Box 7 values."""

    model_config = _model()
    schema_version: Literal["BUSINESS_CHART_BUNDLE_V1"] = "BUSINESS_CHART_BUNDLE_V1"
    bundle_id: UUID
    company_key: Label
    company_name: Label
    generated_at: AwareDatetime
    source_box7_dataset_id: UUID
    source_box7_sha256: Sha256Hex
    chart_registry_version: Label = SEGMENT_CHART_REGISTRY_VERSION
    renderer_version: Label
    limits: ChartLimits
    status: Literal["COMPLETE", "PARTIAL"]
    charts: tuple[ChartSpec, ...]
    skipped_charts: tuple[SkippedChart, ...]
    artifacts: tuple[ChartArtifact, ...]
    warnings: tuple[ChartWarning, ...]
    statistics: ChartStatistics

    @model_validator(mode="after")
    def consistent(self) -> Self:
        shas = {a.sha256 for a in self.artifacts}
        if len(shas) != len(self.artifacts):
            raise ValueError("artifacts are stored once per content hash")
        for chart in self.charts:
            if chart.artifact_sha256 is not None and chart.artifact_sha256 not in shas:
                raise ValueError("a chart references a missing artifact")
        return self
