"""DETAILED_RESEARCH_V1 Box 5: deterministic financial analytics and forensic screens.

Three layers, never blurred:
  FACT        a Box 4 canonical value (MetricInput keeps its whole Box 4 lineage)
  CALCULATION a CalculatedMetric: registered formula + exact canonical inputs + status
  SIGNAL      a rule-based reading of calculations (trend, anomaly, earnings-quality check,
              forensic model, health), each naming its versioned rule

A failed calculation has a status and no number; a missing input is never zero. Forensic
outputs are screening signals that may require review, never conclusions.
"""

from datetime import date
from decimal import Decimal
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

from stock_research_ai.domain.evidence import SourceAuthority
from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_canonical import (
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    Outcome,
    PeriodType,
    ReportingBasis,
    SourceType,
    StatementType,
    ValueKind,
)

ANALYTICS_SCHEMA: Literal["FINANCIAL_ANALYTICS_DATASET_V2"] = "FINANCIAL_ANALYTICS_DATASET_V2"
ANALYTICS_ENGINE_VERSION = "financial-analytics/2"
#: Every derived non-money value is quantized to 6 decimal places, ROUND_HALF_EVEN.
DERIVED_QUANTUM = Decimal("0.000001")

Label = Annotated[str, StringConstraints(max_length=1000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class CalculationStatus(StrEnum):
    CALCULATED = "CALCULATED"
    INPUT_MISSING = "INPUT_MISSING"  # the period exists but this input was not reported
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"  # the period itself is not in the data
    INPUT_CONFLICT = "INPUT_CONFLICT"  # Box 4 CONFLICT / AMBIGUOUS: no canonical value
    LOW_CONFIDENCE_INPUT = "LOW_CONFIDENCE_INPUT"  # Box 4 NOT_COMPARABLE / LOW confidence
    NOT_APPLICABLE = "NOT_APPLICABLE"  # economically meaningless (negative base, net cash)
    DIVISION_BY_ZERO = "DIVISION_BY_ZERO"
    BASIS_MISMATCH = "BASIS_MISMATCH"
    PERIOD_MISMATCH = "PERIOD_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    UNSUPPORTED_SECTOR = "UNSUPPORTED_SECTOR"
    INVALID_INPUT = "INVALID_INPUT"


class MetricCategory(StrEnum):
    BUILDING_BLOCK = "BUILDING_BLOCK"
    GROWTH = "GROWTH"
    PROFITABILITY = "PROFITABILITY"
    RETURNS = "RETURNS"
    DUPONT = "DUPONT"
    LEVERAGE = "LEVERAGE"
    LIQUIDITY = "LIQUIDITY"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    CASH_FLOW_QUALITY = "CASH_FLOW_QUALITY"
    CAPITAL_ALLOCATION = "CAPITAL_ALLOCATION"
    ACCRUALS = "ACCRUALS"
    BANKING = "BANKING"


class AnalyticsUnit(StrEnum):
    PERCENT = "PERCENT"  # 12.5 means 12.5 %
    PERCENTAGE_POINTS = "PERCENTAGE_POINTS"
    BASIS_POINTS = "BASIS_POINTS"
    TIMES = "TIMES"
    DAYS = "DAYS"
    MONEY = "MONEY"  # Box 4 canonical scale (crore) in the frame currency
    PER_SHARE = "PER_SHARE"
    COUNT = "COUNT"


class Sector(StrEnum):
    GENERAL = "GENERAL"  # industrial / services companies
    FINANCIAL = "FINANCIAL"  # banks, NBFCs, insurers, or financial with an unresolved subtype
    UNKNOWN = "UNKNOWN"  # business class unresolved: business-neutral formulas only


def sector_applies(sector: Sector, sectors: tuple[Sector, ...]) -> bool:
    """UNKNOWN never acts as GENERAL or FINANCIAL: only a formula / rule registered for
    both (business-neutral) applies to it."""
    if sector == Sector.UNKNOWN:
        return Sector.GENERAL in sectors and Sector.FINANCIAL in sectors
    return sector in sectors


class PeriodRole(StrEnum):
    LATEST_FY = "LATEST_FY"
    PREVIOUS_FY = "PREVIOUS_FY"
    LATEST_QUARTER = "LATEST_QUARTER"
    PRIOR_YEAR_QUARTER = "PRIOR_YEAR_QUARTER"
    SEQUENTIAL_QUARTER = "SEQUENTIAL_QUARTER"
    LATEST_TTM = "LATEST_TTM"
    PREVIOUS_TTM = "PREVIOUS_TTM"
    WINDOW_3Y = "WINDOW_3Y"
    WINDOW_5Y = "WINDOW_5Y"
    WINDOW_10Y = "WINDOW_10Y"


class TrendClass(StrEnum):
    STRONGLY_IMPROVING = "STRONGLY_IMPROVING"
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    DETERIORATING = "DETERIORATING"
    STRONGLY_DETERIORATING = "STRONGLY_DETERIORATING"
    VOLATILE = "VOLATILE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


class Severity(StrEnum):
    INFO = "INFO"  # evaluated, below every threshold
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ComponentStatus(StrEnum):
    AVAILABLE = "AVAILABLE"  # a numeric component (Altman X-ratio, Beneish index)
    PASS = "PASS"  # a pass/fail component (Piotroski signal)
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


class ForensicModel(StrEnum):
    PIOTROSKI_F_SCORE = "PIOTROSKI_F_SCORE"
    ALTMAN_Z = "ALTMAN_Z"
    BENEISH_M = "BENEISH_M"


class HealthDimension(StrEnum):
    GROWTH = "GROWTH"
    MARGINS = "MARGINS"
    RETURNS = "RETURNS"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_CONVERSION = "CASH_CONVERSION"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    EARNINGS_QUALITY = "EARNINGS_QUALITY"
    FORENSIC_SCREEN = "FORENSIC_SCREEN"


class HealthState(StrEnum):
    STRONG = "STRONG"
    HEALTHY = "HEALTHY"
    NEUTRAL = "NEUTRAL"
    WATCH = "WATCH"
    WEAK = "WEAK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# --- registries (recorded in the calculation manifest) -------------------------------------


class FormulaVariant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Code
    expression: Label


class FormulaDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    formula_id: Label  # CODE/version; a variant adds /VARIANT
    code: Code
    version: PositiveInt
    name: Label
    category: MetricCategory
    unit: AnalyticsUnit
    expression: Label
    required_inputs: tuple[Label, ...]
    optional_inputs: tuple[Label, ...] = ()
    periods: tuple[PeriodType, ...]
    sectors: tuple[Sector, ...]
    variants: tuple[FormulaVariant, ...] = ()
    notes: Label = ""


class RuleThreshold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    severity: Severity
    value: ExactDecimal


class RuleDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule_id: Label  # FAMILY/CODE/version
    family: Literal["EARNINGS_QUALITY", "ANOMALY", "TREND", "HEALTH"]
    code: Code
    version: PositiveInt
    description: Label
    metrics: tuple[Code, ...]
    comparison: Literal["AT_LEAST", "AT_MOST", "ABSOLUTE_AT_LEAST", "BANDS", "CLASSIFY"]
    thresholds: tuple[RuleThreshold, ...]
    unit: AnalyticsUnit | None
    condition: Label | None = None
    template: Label
    sectors: tuple[Sector, ...]


class CalculationManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    engine_version: Label
    formula_registry_version: Label
    rule_registry_version: Label
    formulas: tuple[FormulaDefinition, ...]
    rules: tuple[RuleDefinition, ...]


# --- calculations ---------------------------------------------------------------------------


class MetricInput(BaseModel):
    """One Box 4 canonical value used by a calculation, with its whole Box 4 lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Label  # e.g. 'numerator', 'equity_open.reserves'
    canonical_value_id: UUID
    metric: CanonicalMetric
    value: ExactDecimal
    value_kind: ValueKind
    currency: Currency
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    outcome: Outcome
    confidence: Confidence
    reconciliation_record_id: UUID
    selected_observation_id: UUID
    selected_source: SourceType
    selected_authority: SourceAuthority
    source_locator: Label
    supporting_observation_ids: tuple[UUID, ...]
    conflicting_observation_ids: tuple[UUID, ...]


class CalculatedMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    metric_id: UUID
    code: Code
    label: Label
    category: MetricCategory
    status: CalculationStatus
    value: ExactDecimal | None
    unit: AnalyticsUnit
    currency: Currency | None  # MONEY / PER_SHARE only
    reporting_basis: ReportingBasis
    period: FinancialPeriod  # the period described (later endpoint for changes)
    comparison_period: FinancialPeriod | None = None  # earlier endpoint / window start
    formula_id: Label
    formula_text: Label
    input_value_ids: tuple[UUID, ...]  # canonical value IDs (sorted, unique)
    input_metric_ids: tuple[UUID, ...] = ()  # calculated metrics this one builds on
    input_periods: tuple[Label, ...] = ()  # Box 4 period keys of every input (sorted)
    provenance: tuple[MetricInput, ...]
    missing_inputs: tuple[Label, ...] = ()
    confidence: Confidence | None
    interpretation: Code | None = None  # e.g. NET_CASH, NEGATIVE_BASE
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def lineage(self) -> Self:
        calculated = self.status == CalculationStatus.CALCULATED
        if calculated != (self.value is not None) or calculated != (self.confidence is not None):
            raise ValueError("value and confidence exist exactly when CALCULATED")
        if calculated and not self.provenance:
            raise ValueError("a calculated metric must carry its canonical inputs")
        ids = tuple(sorted({i.canonical_value_id for i in self.provenance}, key=str))
        if self.input_value_ids != ids:
            raise ValueError("input_value_ids must list exactly the provenance values")
        return self


class AnalyticalPeriod(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: PeriodRole
    reporting_basis: ReportingBasis
    status: Literal["SELECTED", "UNAVAILABLE"]
    period: FinancialPeriod | None
    start_period: FinancialPeriod | None = None
    reason: Label


class SeriesPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    period: FinancialPeriod
    status: CalculationStatus
    value: ExactDecimal | None
    metric_id: UUID | None = None  # a calculated series point
    canonical_value_id: UUID | None = None  # a reported (Box 4) series point

    @model_validator(mode="after")
    def one_reference(self) -> Self:
        if (self.metric_id is None) == (self.canonical_value_id is None):
            raise ValueError("a point references exactly one metric or canonical value")
        return self


class AnalyticalSeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    series_id: UUID
    code: Code
    reporting_basis: ReportingBasis
    frequency: Literal["ANNUAL", "QUARTERLY"]
    source: Literal["CANONICAL", "CALCULATED"]
    unit: AnalyticsUnit
    points: tuple[SeriesPoint, ...]  # oldest first


class TrendAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    trend_id: UUID
    series_code: Code
    reporting_basis: ReportingBasis
    classification: TrendClass
    rule_id: Label
    higher_is_better: bool
    points_used: NonNegativeInt
    direction_consistency: ExactDecimal | None  # share of steps moving the overall way
    first_period: FinancialPeriod | None
    last_period: FinancialPeriod | None
    first_value: ExactDecimal | None
    last_value: ExactDecimal | None
    change: ExactDecimal | None
    explanation: Label
    metric_ids: tuple[UUID, ...]


class AnomalySignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    signal_id: UUID
    rule_id: Label
    code: Code
    status: Literal["TRIGGERED"] = "TRIGGERED"
    severity: Severity
    reporting_basis: ReportingBasis
    periods: tuple[FinancialPeriod, ...]
    observed: ExactDecimal
    threshold: ExactDecimal
    unit: AnalyticsUnit
    explanation: Label
    metric_ids: tuple[UUID, ...]
    canonical_value_ids: tuple[UUID, ...]


class EarningsQualityCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    check_id: UUID
    rule_id: Label
    code: Code
    status: CalculationStatus  # CALCULATED when evaluated, else why not
    severity: Severity | None  # INFO..HIGH when evaluated
    reporting_basis: ReportingBasis
    period: FinancialPeriod | None
    observed: ExactDecimal | None
    unit: AnalyticsUnit
    observation: Label
    metric_ids: tuple[UUID, ...]
    canonical_value_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def evaluated(self) -> Self:
        if (self.status == CalculationStatus.CALCULATED) != (self.severity is not None):
            raise ValueError("a check has a severity exactly when it was evaluated")
        return self


class ForensicComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Code
    status: ComponentStatus
    value: ExactDecimal | None = None
    formula: Label
    metric_ids: tuple[UUID, ...] = ()
    canonical_value_ids: tuple[UUID, ...] = ()
    input_values: tuple[tuple[Label, ExactDecimal], ...] = ()
    missing_inputs: tuple[Label, ...] = ()

    @model_validator(mode="after")
    def available(self) -> Self:
        if (self.status == ComponentStatus.AVAILABLE) != (self.value is not None):
            raise ValueError("exactly the AVAILABLE (numeric) components carry a value")
        return self


class ForensicModelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    result_id: UUID
    model: ForensicModel
    variant: Code
    formula: Label
    status: CalculationStatus
    reporting_basis: ReportingBasis
    period: FinancialPeriod | None
    comparison_period: FinancialPeriod | None
    components: tuple[ForensicComponent, ...]
    components_total: PositiveInt
    components_available: NonNegativeInt
    components_passed: NonNegativeInt | None = None  # Piotroski
    score_numerator: NonNegativeInt | None = None  # Piotroski: passed among AVAILABLE
    score_denominator: NonNegativeInt | None = None  # Piotroski: components AVAILABLE
    availability_label: Label  # e.g. '5 / 7 available' - never presented as x / 9
    coverage: ExactDecimal
    score: ExactDecimal | None
    zone: Code | None = None  # screening band, never a verdict
    applicability: Label
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def scored(self) -> Self:
        if (self.status == CalculationStatus.CALCULATED) != (self.score is not None):
            raise ValueError("a model has a score exactly when CALCULATED")
        return self


class FinancialHealthSignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    dimension: HealthDimension
    state: HealthState
    reporting_basis: ReportingBasis
    rule_id: Label
    reasons: tuple[Label, ...]
    metric_ids: tuple[UUID, ...]


class UnmappedLineReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    line_id: UUID
    statement_type: StatementType
    reporting_basis: ReportingBasis
    source_type: SourceType
    raw_label: Label
    label_context: Label | None
    periods: tuple[Label, ...]
    observation_count: NonNegativeInt
    review_hints: tuple[Code, ...]  # calculations the label may bear on (keyword rule)
    hinted_calculations_unavailable: tuple[Code, ...]  # of those, ones not CALCULATED


class UnmappedSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule_id: Label
    total_lines: NonNegativeInt
    by_statement: dict[str, int]
    lines_with_unavailable_hints: NonNegativeInt
    references: tuple[UnmappedLineReference, ...]
    truncated: bool


class AnalyticsWarning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: Code
    detail: Label | None = None
    ids: tuple[UUID, ...] = ()


class AnalyticsLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_canonical_values: PositiveInt = 200_000
    max_periods: PositiveInt = 40  # per basis and frequency (most recent kept)
    max_calculated_metrics: PositiveInt = 60_000
    max_anomalies: PositiveInt = 5_000
    max_series: PositiveInt = 500
    max_checks: PositiveInt = 5_000
    max_provenance_refs: PositiveInt = 64  # canonical inputs per metric
    max_warning_chars: PositiveInt = 500
    max_warnings: PositiveInt = 10_000
    max_unmapped_references: PositiveInt = 2_000


class AnalyticsFrame(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reporting_basis: ReportingBasis
    currency: Currency
    annual_periods: tuple[date, ...]
    quarter_periods: tuple[date, ...]
    has_ttm: bool
    excluded_other_currency_values: NonNegativeInt


class Box4Reference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Label
    taxonomy_version: Label
    dataset_id: UUID
    canonical_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    status: Label
    canonical_values: NonNegativeInt
    unmapped_lines: NonNegativeInt


class AnalyticsStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    canonical_inputs_considered: NonNegativeInt
    canonical_inputs_used: NonNegativeInt
    calculated_metrics: NonNegativeInt
    unavailable_calculations: NonNegativeInt
    metrics_by_category: dict[str, int]
    metrics_by_status: dict[str, int]
    confidence_counts: dict[str, int]
    anomalies_by_severity: dict[str, int]
    checks_by_severity: dict[str, int]
    trends_by_class: dict[str, int]
    forensic_models: dict[str, int]  # COMPLETE / INCOMPLETE / NOT_APPLICABLE
    unmapped_lines_referenced: NonNegativeInt
    annual_periods_analysed: NonNegativeInt
    quarter_periods_analysed: NonNegativeInt
    series: NonNegativeInt


CATEGORY_FIELDS: dict[MetricCategory, str] = {
    MetricCategory.BUILDING_BLOCK: "building_blocks",
    MetricCategory.GROWTH: "growth",
    MetricCategory.PROFITABILITY: "profitability",
    MetricCategory.RETURNS: "returns",
    MetricCategory.DUPONT: "dupont",
    MetricCategory.LEVERAGE: "leverage",
    MetricCategory.LIQUIDITY: "liquidity",
    MetricCategory.WORKING_CAPITAL: "working_capital",
    MetricCategory.CASH_FLOW_QUALITY: "cash_flow_quality",
    MetricCategory.CAPITAL_ALLOCATION: "capital_allocation",
    MetricCategory.ACCRUALS: "accruals",
    MetricCategory.BANKING: "banking",
}


class FinancialAnalyticsDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["FINANCIAL_ANALYTICS_DATASET_V2"] = ANALYTICS_SCHEMA
    engine_version: Label = ANALYTICS_ENGINE_VERSION
    analytics_id: UUID
    company_key: Label
    company_name: Label
    industry: Industry
    sector: Sector
    reporting_bases: tuple[ReportingBasis, ...]
    generated_at: AwareDatetime
    source_box4: Box4Reference
    limits: AnalyticsLimits
    status: Literal["COMPLETE", "PARTIAL"]
    frames: tuple[AnalyticsFrame, ...]
    periods: tuple[AnalyticalPeriod, ...]
    building_blocks: tuple[CalculatedMetric, ...]
    growth: tuple[CalculatedMetric, ...]
    profitability: tuple[CalculatedMetric, ...]
    returns: tuple[CalculatedMetric, ...]
    dupont: tuple[CalculatedMetric, ...]
    leverage: tuple[CalculatedMetric, ...]
    liquidity: tuple[CalculatedMetric, ...]
    working_capital: tuple[CalculatedMetric, ...]
    cash_flow_quality: tuple[CalculatedMetric, ...]
    capital_allocation: tuple[CalculatedMetric, ...]
    accruals: tuple[CalculatedMetric, ...]
    banking: tuple[CalculatedMetric, ...]
    earnings_quality: tuple[EarningsQualityCheck, ...]
    forensic_scores: tuple[ForensicModelResult, ...]
    anomalies: tuple[AnomalySignal, ...]
    trends: tuple[TrendAssessment, ...]
    series: tuple[AnalyticalSeries, ...]
    health: tuple[FinancialHealthSignal, ...]
    unmapped: UnmappedSummary
    warnings: tuple[AnalyticsWarning, ...]
    statistics: AnalyticsStatistics
    manifest: CalculationManifest

    def all_metrics(self) -> tuple[CalculatedMetric, ...]:
        return tuple(m for field in CATEGORY_FIELDS.values() for m in getattr(self, field))

    @model_validator(mode="after")
    def integrity(self) -> Self:
        for category, field in CATEGORY_FIELDS.items():
            if any(m.category != category for m in getattr(self, field)):
                raise ValueError(f"{field} holds only {category.value} metrics")
        metrics = self.all_metrics()
        ids = [m.metric_id for m in metrics]
        if len(ids) != len(set(ids)):
            raise ValueError("metric IDs must be unique")
        known = set(ids)
        if any(not set(m.input_metric_ids) <= known for m in metrics):
            raise ValueError("input_metric_ids must reference metrics in this dataset")
        currencies = {f.reporting_basis: f.currency for f in self.frames}
        for m in metrics:
            if m.currency is not None and currencies.get(m.reporting_basis) != m.currency:
                raise ValueError("a frame never mixes currencies")
            if any(i.reporting_basis != m.reporting_basis for i in m.provenance):
                raise ValueError("a metric never mixes reporting bases")
        formulas = {f.formula_id for f in self.manifest.formulas}
        for m in metrics:
            base = "/".join(m.formula_id.split("/")[:2])
            if base not in formulas:
                raise ValueError(f"formula {m.formula_id} is not in the manifest")
        return self

    def metric(
        self, code: str, basis: ReportingBasis, period_end: date | None = None
    ) -> list[CalculatedMetric]:
        return [
            m
            for m in self.all_metrics()
            if m.code == code
            and m.reporting_basis == basis
            and (period_end is None or m.period.period_end == period_end)
        ]
