"""DETAILED_RESEARCH_V1 Box 5: deterministic financial analytics and forensic screens.

Three layers, never blurred:
  FACT        a Box 4 canonical value (DerivedMetricInput keeps its full Box 4 lineage)
  CALCULATION a DerivedMetric: formula/version + exact canonical inputs + status
  SIGNAL      a rule-based reading of calculations (trend, anomaly, forensic, health)

A failed calculation has a status and no number; a missing input is never zero.
Forensic outputs are screening signals, never conclusions about a company or its people.
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

from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_canonical import (
    CanonicalMetric,
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    Outcome,
    ReportingBasis,
    SourceType,
    ValueKind,
)

ANALYTICS_SCHEMA: Literal["FINANCIAL_ANALYTICS_DATASET_V1"] = "FINANCIAL_ANALYTICS_DATASET_V1"
ANALYTICS_ENGINE_VERSION = "financial-analytics/1"
#: Every derived non-money value is quantized to 6 decimal places, ROUND_HALF_EVEN.
DERIVED_QUANTUM = Decimal("0.000001")

Label = Annotated[str, StringConstraints(max_length=1000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class CalculationStatus(StrEnum):
    CALCULATED = "CALCULATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INCOMPATIBLE_PERIODS = "INCOMPATIBLE_PERIODS"
    INCOMPATIBLE_BASIS = "INCOMPATIBLE_BASIS"
    INCOMPATIBLE_CURRENCY = "INCOMPATIBLE_CURRENCY"
    ZERO_DENOMINATOR = "ZERO_DENOMINATOR"
    LOW_CONFIDENCE_INPUT = "LOW_CONFIDENCE_INPUT"
    CONFLICTED_INPUT = "CONFLICTED_INPUT"
    UNSUPPORTED_INDUSTRY = "UNSUPPORTED_INDUSTRY"
    INVALID_INPUT = "INVALID_INPUT"


class MetricCategory(StrEnum):
    BASE = "BASE"  # derived building blocks (EBITDA, FCF, capital employed ...)
    GROWTH = "GROWTH"
    MARGIN = "MARGIN"
    RETURN = "RETURN"
    DUPONT = "DUPONT"
    LEVERAGE = "LEVERAGE"
    LIQUIDITY = "LIQUIDITY"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    CASH_FLOW = "CASH_FLOW"
    ACCRUALS = "ACCRUALS"
    BANKING = "BANKING"


class AnalyticsUnit(StrEnum):
    PERCENT = "PERCENT"  # 12.5 means 12.5 %
    PERCENTAGE_POINTS = "PERCENTAGE_POINTS"
    BASIS_POINTS = "BASIS_POINTS"
    TIMES = "TIMES"  # a plain ratio / multiple
    DAYS = "DAYS"
    MONEY = "MONEY"  # Box 4 canonical scale (crore) in the frame currency
    PER_SHARE = "PER_SHARE"


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
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class SignalStatus(StrEnum):
    CLEAR = "CLEAR"
    WATCH = "WATCH"
    ELEVATED = "ELEVATED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


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


class DerivedMetricInput(BaseModel):
    """One Box 4 canonical value used by a calculation, with its whole Box 4 lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Label  # e.g. 'numerator', 'current', 'ebitda.pbt'
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
    supporting_observation_ids: tuple[UUID, ...]
    conflicting_observation_ids: tuple[UUID, ...]


class DerivedMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    metric_id: UUID
    code: Code
    name: Label
    category: MetricCategory
    status: CalculationStatus
    value: ExactDecimal | None
    unit: AnalyticsUnit
    currency: Currency | None  # MONEY / PER_SHARE only
    reporting_basis: ReportingBasis
    period: FinancialPeriod  # the period described (the later endpoint for changes)
    comparison_period: FinancialPeriod | None = None  # earlier endpoint / window start
    formula_id: Label
    formula: Label
    inputs: tuple[DerivedMetricInput, ...]
    derived_from: tuple[UUID, ...] = ()
    missing_inputs: tuple[Label, ...] = ()
    confidence: Confidence | None
    interpretation: Code | None = None  # e.g. NET_CASH, NEGATIVE_BASE
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def lineage(self) -> Self:
        calculated = self.status == CalculationStatus.CALCULATED
        if calculated != (self.value is not None) or calculated != (self.confidence is not None):
            raise ValueError("value and confidence exist exactly when CALCULATED")
        if calculated and not self.inputs:
            raise ValueError("a calculated metric must carry its canonical inputs")
        return self


class AnalyticalPeriod(BaseModel):
    """A traceable period selection (latest FY, prior-year quarter, 5Y window ...)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    role: PeriodRole
    reporting_basis: ReportingBasis
    status: Literal["SELECTED", "UNAVAILABLE"]
    period: FinancialPeriod | None
    start_period: FinancialPeriod | None = None  # windows only
    reason: Label


class SeriesPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    period: FinancialPeriod
    metric_id: UUID
    status: CalculationStatus
    value: ExactDecimal | None


class AnalyticalSeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: Code
    reporting_basis: ReportingBasis
    frequency: Literal["ANNUAL", "QUARTERLY"]
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
    code: Code
    severity: Literal["WATCH", "ELEVATED"]
    reporting_basis: ReportingBasis
    period: FinancialPeriod
    observed: ExactDecimal
    threshold: ExactDecimal
    unit: AnalyticsUnit
    rule_id: Label
    explanation: Label
    metric_ids: tuple[UUID, ...]
    canonical_value_ids: tuple[UUID, ...]


class ForensicSignal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    signal_id: UUID
    code: Code
    status: SignalStatus
    reporting_basis: ReportingBasis
    period: FinancialPeriod | None
    observed: ExactDecimal | None
    watch_threshold: ExactDecimal
    elevated_threshold: ExactDecimal
    unit: AnalyticsUnit
    rule_id: Label
    explanation: Label
    metric_ids: tuple[UUID, ...]
    canonical_value_ids: tuple[UUID, ...]


class ForensicComponent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Code
    status: ComponentStatus
    value: ExactDecimal | None = None
    formula: Label
    metric_ids: tuple[UUID, ...] = ()
    canonical_value_ids: tuple[UUID, ...] = ()
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
    components_passed: NonNegativeInt | None = None  # Piotroski only
    coverage: ExactDecimal  # available / total
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


class AnalyticsWarning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: Code
    detail: Label | None = None
    ids: tuple[UUID, ...] = ()


class AnalyticsLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_canonical_values: PositiveInt = 200_000
    max_periods_per_series: PositiveInt = 40
    max_derived_metrics: PositiveInt = 50_000
    max_forensic_signals: PositiveInt = 5_000
    max_anomaly_signals: PositiveInt = 5_000
    max_warnings: PositiveInt = 10_000


class AnalyticsFrame(BaseModel):
    """One basis analysed in one currency; other currencies are never mixed in."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    reporting_basis: ReportingBasis
    currency: Currency
    annual_periods: tuple[date, ...]
    quarter_periods: tuple[date, ...]
    has_ttm: bool
    excluded_other_currency_values: NonNegativeInt


class AnalyticsStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    canonical_values_consumed: NonNegativeInt
    derived_metrics: NonNegativeInt
    metrics_by_status: dict[str, int]
    metrics_by_category: dict[str, int]
    trends: NonNegativeInt
    trends_by_class: dict[str, int]
    anomalies: NonNegativeInt
    forensic_signals_by_status: dict[str, int]
    models_by_status: dict[str, int]
    health_by_state: dict[str, int]


class FinancialAnalyticsDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["FINANCIAL_ANALYTICS_DATASET_V1"] = ANALYTICS_SCHEMA
    engine_version: Label = ANALYTICS_ENGINE_VERSION
    analytics_id: UUID
    source_dataset_id: UUID
    company_key: Label
    company_name: Label
    industry: Industry
    generated_at: AwareDatetime
    limits: AnalyticsLimits
    status: Literal["COMPLETE", "PARTIAL"]
    frames: tuple[AnalyticsFrame, ...]
    periods: tuple[AnalyticalPeriod, ...]
    metrics: tuple[DerivedMetric, ...]
    series: tuple[AnalyticalSeries, ...]
    trends: tuple[TrendAssessment, ...]
    anomalies: tuple[AnomalySignal, ...]
    forensic_signals: tuple[ForensicSignal, ...]
    forensic_models: tuple[ForensicModelResult, ...]
    health: tuple[FinancialHealthSignal, ...]
    warnings: tuple[AnalyticsWarning, ...]
    statistics: AnalyticsStatistics

    @model_validator(mode="after")
    def integrity(self) -> Self:
        ids = [m.metric_id for m in self.metrics]
        if len(ids) != len(set(ids)):
            raise ValueError("metric IDs must be unique")
        known = set(ids)
        for metric in self.metrics:
            if not set(metric.derived_from) <= known:
                raise ValueError("derived_from must reference metrics in this dataset")
        for frame in self.frames:
            for metric in self.metrics:
                if metric.reporting_basis == frame.reporting_basis and metric.currency not in (
                    None,
                    frame.currency,
                ):
                    raise ValueError("a frame never mixes currencies")
        return self

    def metric(
        self, code: str, basis: ReportingBasis, period_end: date | None = None
    ) -> list[DerivedMetric]:
        return [
            m
            for m in self.metrics
            if m.code == code
            and m.reporting_basis == basis
            and (period_end is None or m.period.period_end == period_end)
        ]

    def model_result(
        self, model: ForensicModel, basis: ReportingBasis
    ) -> ForensicModelResult | None:
        return next(
            (r for r in self.forensic_models if r.model == model and r.reporting_basis == basis),
            None,
        )
