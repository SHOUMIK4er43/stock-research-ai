"""DETAILED_RESEARCH_V1 Box 6: deterministic financial chart specifications and artifacts.

A chart is an evidence surface, not an interpretation. Every plotted value is either a
Box 4 canonical value or a Box 5 output (calculated metric, forensic component, check,
trend, health signal); Box 6 only selects, orders, scales for display and labels.
Missing values stay visible gaps. Ordinal status charts (trends, health, screening
checks, Piotroski components) use an explicit display encoding recorded on the series.
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
    Confidence,
    Currency,
    PeriodType,
    ReportingBasis,
)

CHART_BUNDLE_SCHEMA: Literal["FINANCIAL_CHART_BUNDLE_V1"] = "FINANCIAL_CHART_BUNDLE_V1"
CHART_REGISTRY_VERSION = "charts/1"
RENDERER_VERSION = "svg-renderer/1"

Label = Annotated[str, StringConstraints(max_length=1000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ChartType(StrEnum):
    LINE = "LINE"
    BAR = "BAR"
    GROUPED_BAR = "GROUPED_BAR"
    HORIZONTAL_BAR = "HORIZONTAL_BAR"  # categorical rows (components, status summaries)
    SCATTER = "SCATTER"  # the anomaly timeline: period x rule, marker = severity


class ChartCategory(StrEnum):
    GROWTH = "GROWTH"
    MARGINS = "MARGINS"
    QUARTERLY = "QUARTERLY"
    RETURNS = "RETURNS"
    CASH_FLOW = "CASH_FLOW"
    BALANCE_SHEET = "BALANCE_SHEET"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    EARNINGS_QUALITY = "EARNINGS_QUALITY"
    FORENSIC_MODELS = "FORENSIC_MODELS"
    SIGNALS = "SIGNALS"
    BANKING = "BANKING"
    # additive: Box 7 segments/1 charts (no Box 4/5 chart uses these)
    SEGMENTS = "SEGMENTS"
    GEOGRAPHY = "GEOGRAPHY"
    OPERATING_KPIS = "OPERATING_KPIS"


class AxisKind(StrEnum):
    ANNUAL_FLOW = "ANNUAL_FLOW"  # fiscal years (FY periods)
    ANNUAL_BALANCE = "ANNUAL_BALANCE"  # balance sheets as at fiscal year ends
    QUARTERLY = "QUARTERLY"  # quarters only (never YTD / half-year / FY)
    CATEGORY = "CATEGORY"  # named categories (CAGR windows, components, dimensions)
    TIMELINE = "TIMELINE"  # fiscal years x rule codes


class ChartStatus(StrEnum):
    READY = "READY"
    PARTIAL = "PARTIAL"  # emitted, with gaps or a below-preferred point count
    NO_DATA = "NO_DATA"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNSUPPORTED_SECTOR = "UNSUPPORTED_SECTOR"
    RENDER_FAILED = "RENDER_FAILED"


class SourceLayer(StrEnum):
    BOX4 = "BOX4"
    BOX5 = "BOX5"
    BOX7 = "BOX7"  # additive: business intelligence (segments/1 charts)


class PointSourceKind(StrEnum):
    CANONICAL_VALUE = "CANONICAL_VALUE"  # Box 4
    CALCULATED_METRIC = "CALCULATED_METRIC"  # Box 5
    FORENSIC_COMPONENT = "FORENSIC_COMPONENT"  # Box 5 model component
    EARNINGS_QUALITY_CHECK = "EARNINGS_QUALITY_CHECK"  # Box 5
    TREND = "TREND"  # Box 5
    HEALTH_SIGNAL = "HEALTH_SIGNAL"  # Box 5
    ANOMALY = "ANOMALY"  # Box 5
    SEGMENT_VALUE = "SEGMENT_VALUE"  # Box 7 canonical segment value
    GEOGRAPHY_VALUE = "GEOGRAPHY_VALUE"  # Box 7 canonical geography value
    KPI_VALUE = "KPI_VALUE"  # Box 7 canonical operating KPI value
    SEGMENT_ANALYTIC = "SEGMENT_ANALYTIC"  # Box 7 segment / geography / KPI analytic


class SeriesEncoding(StrEnum):
    VALUE = "VALUE"  # the source number itself
    ORDINAL_STATUS = "ORDINAL_STATUS"  # a status shown at a documented rank (display only)


#: the only period type each time axis may hold (TTM / YTD / half-year never qualify)
AXIS_PERIOD_TYPE: dict[AxisKind, PeriodType] = {
    AxisKind.ANNUAL_FLOW: PeriodType.FY,
    AxisKind.ANNUAL_BALANCE: PeriodType.POINT_IN_TIME,
    AxisKind.QUARTERLY: PeriodType.QUARTER,
    AxisKind.TIMELINE: PeriodType.FY,
}


class PointSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    layer: SourceLayer
    kind: PointSourceKind
    ref_id: UUID | None  # canonical value / metric / model result / check / trend / anomaly
    formula_or_rule_id: Label | None = None
    component: Code | None = None  # forensic component or health dimension name
    canonical_value_ids: tuple[UUID, ...] = ()  # Box 4 values behind the point
    metric_ids: tuple[UUID, ...] = ()  # Box 5 metrics behind the point
    observation_ids: tuple[UUID, ...] = ()  # Box 4 selected observations
    source_locators: tuple[Label, ...] = ()  # page/table/row/column or Screener locator
    reconciliation_outcome: Code | None = None  # Box 4 points


class ChartPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    point_id: UUID
    x_key: Label  # Box 4 period key, or a category code
    x_label: Label
    period_type: PeriodType | None
    period_start: date | None = None
    period_end: date | None = None
    y_category: Label | None = None  # SCATTER rows
    value: ExactDecimal | None  # the source value, never rounded or rescaled
    display_value: Label | None  # formatted for the chart's display unit
    status: Code  # CALCULATED or the source's own status
    confidence: Confidence | None
    missing_reason: Label | None = None
    severity: Code | None = None  # anomaly timeline markers
    source: PointSource
    anomaly_ids: tuple[UUID, ...] = ()
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def gap_or_value(self) -> Self:
        if self.value is None and not self.missing_reason:
            raise ValueError("a gap must say why it is missing")
        if self.value is not None and self.source.ref_id is None:
            raise ValueError("a plotted value must reference its Box 4 / Box 5 source")
        return self


class ChartSeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    series_id: UUID
    label: Label
    metric_code: Code
    source_layer: SourceLayer
    encoding: SeriesEncoding
    encoding_note: Label | None = None  # e.g. 'PASS = 1, FAIL = 0'
    unit: Code  # Box 5 AnalyticsUnit value, MONEY, or ORDINAL
    currency: Currency | None
    reporting_basis: ReportingBasis
    frequency: AxisKind
    render_as: Literal["BAR", "LINE", "MARKER"]
    formula_id: Label | None = None
    points: tuple[ChartPoint, ...]
    valid_points: NonNegativeInt
    gaps: NonNegativeInt
    confidence_summary: dict[str, int]
    warnings: tuple[Code, ...] = ()


class ChartAxis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: Label
    display_unit: Label  # e.g. '₹ crore', '%', 'days', 'status rank'
    scale_divisor: ExactDecimal = Decimal(1)  # display value = value / divisor
    zero_baseline: bool = False
    tick_labels: tuple[tuple[ExactDecimal, Label], ...] = ()  # ordinal axes
    categories: tuple[tuple[Label, Label], ...] = ()  # ordered (x_key, label) for x axes


class ChartAnnotation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["ANOMALY", "TREND"]
    ref_id: UUID  # Box 5 anomaly signal_id / trend_id
    rule_id: Label
    code: Code
    severity: Code | None = None
    x_key: Label | None = None
    label: Label  # short, neutral
    metric_ids: tuple[UUID, ...] = ()
    canonical_value_ids: tuple[UUID, ...] = ()


class ChartWarning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: Code
    detail: Label | None = None


class ChartArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sha256: Sha256Hex
    media_type: Literal["image/svg+xml"] = "image/svg+xml"
    extension: Literal[".svg"] = ".svg"
    byte_size: PositiveInt
    width: PositiveInt
    height: PositiveInt
    name: Label  # deterministic, e.g. revenue_annual-consolidated-<sha12>.svg
    content_reference: Label  # sha256/<aa>/<sha>.svg
    svg: str


class ChartSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    chart_id: UUID
    schema_version: Literal["FINANCIAL_CHART_BUNDLE_V1"] = CHART_BUNDLE_SCHEMA
    chart_code: Code
    company_key: Label
    title: Label
    subtitle: Label
    chart_type: ChartType
    category: ChartCategory
    reporting_basis: ReportingBasis
    frequency: AxisKind
    currency: Currency | None
    unit: Code
    width: PositiveInt
    height: PositiveInt
    connect_gaps: Literal[False] = False
    series: tuple[ChartSeries, ...]
    x_axis: ChartAxis
    y_axis: ChartAxis
    annotations: tuple[ChartAnnotation, ...]
    footer: Label
    provenance_summary: dict[str, int]  # counts of point source kinds
    warnings: tuple[ChartWarning, ...]
    status: ChartStatus
    status_reason: Label
    source_dataset_ids: tuple[UUID, ...]  # Box 4 dataset, Box 5 analytics
    registry_version: Label = CHART_REGISTRY_VERSION
    renderer_version: Label = RENDERER_VERSION
    artifact_sha256: Sha256Hex | None = None

    @model_validator(mode="after")
    def safe(self) -> Self:
        if self.status in (ChartStatus.READY, ChartStatus.PARTIAL) and not self.series:
            raise ValueError("an emitted chart has series")
        for s in self.series:
            if s.reporting_basis != self.reporting_basis or s.frequency != self.frequency:
                raise ValueError("a chart never mixes bases or frequencies")
            if s.currency is not None and s.currency != self.currency:
                raise ValueError("a chart never mixes currencies")
            if s.unit != self.unit:
                raise ValueError("a chart never mixes units on one axis")
            expected = AXIS_PERIOD_TYPE.get(self.frequency)
            for p in s.points:
                if expected is not None and p.period_type != expected:
                    raise ValueError(
                        f"a {self.frequency.value} chart only holds {expected.value} periods"
                    )
        return self


class SkippedChart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    chart_code: Code
    reporting_basis: ReportingBasis
    category: ChartCategory
    status: ChartStatus
    reason: Label


class ChartLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_charts: PositiveInt = 300
    max_series_per_chart: PositiveInt = 6
    max_points_per_series: PositiveInt = 40
    max_annotations_per_chart: PositiveInt = 40
    max_svg_bytes: PositiveInt = 400_000
    max_label_chars: PositiveInt = 90


class SourceReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    layer: SourceLayer
    dataset_id: UUID
    sha256: Sha256Hex
    schema_version: Label


class ChartStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    definitions_considered: NonNegativeInt
    charts_ready: NonNegativeInt
    charts_partial: NonNegativeInt
    no_data: NonNegativeInt
    not_applicable: NonNegativeInt
    unsupported_sector: NonNegativeInt
    render_failed: NonNegativeInt
    svg_artifacts: NonNegativeInt
    svg_bytes: NonNegativeInt
    total_points: NonNegativeInt
    total_gaps: NonNegativeInt
    anomaly_annotations: NonNegativeInt
    charts_by_category: dict[str, int]


class FinancialChartBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["FINANCIAL_CHART_BUNDLE_V1"] = CHART_BUNDLE_SCHEMA
    bundle_id: UUID
    company_key: Label
    company_name: Label
    sector: Code
    generated_at: AwareDatetime
    source_box4: SourceReference
    source_box5: SourceReference
    chart_registry_version: Label = CHART_REGISTRY_VERSION
    renderer_version: Label = RENDERER_VERSION
    limits: ChartLimits
    status: Literal["COMPLETE", "PARTIAL"]
    charts: tuple[ChartSpec, ...]
    skipped_charts: tuple[SkippedChart, ...]
    artifacts: tuple[ChartArtifact, ...]  # unique by sha256
    warnings: tuple[ChartWarning, ...]
    statistics: ChartStatistics

    @model_validator(mode="after")
    def consistent(self) -> Self:
        ids = [c.chart_id for c in self.charts]
        if len(ids) != len(set(ids)):
            raise ValueError("chart IDs must be unique")
        shas = [a.sha256 for a in self.artifacts]
        if len(shas) != len(set(shas)):
            raise ValueError("artifacts are stored once per content hash")
        for chart in self.charts:
            if chart.artifact_sha256 is not None and chart.artifact_sha256 not in shas:
                raise ValueError("a chart must reference a stored artifact")
        return self

    def chart(self, code: str, basis: ReportingBasis) -> ChartSpec | None:
        return next(
            (c for c in self.charts if c.chart_code == code and c.reporting_basis == basis), None
        )

    def artifact(self, sha256: str) -> ChartArtifact | None:
        return next((a for a in self.artifacts if a.sha256 == sha256), None)
