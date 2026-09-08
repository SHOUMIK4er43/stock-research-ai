"""Financial record contracts only: no calculations or persistence in Part 1."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.metrics import MetricMappingStatus
from stock_research_ai.domain.provenance import CalculationProvenance, ObservationProvenance


def _reject_inexact_number(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError(
            "Use a Decimal, decimal string, or integer; floats and booleans are invalid"
        )
    return value


ExactDecimal = Annotated[
    Decimal, BeforeValidator(_reject_inexact_number), Field(allow_inf_nan=False)
]


class ReportingScope(StrEnum):
    STANDALONE = "standalone"
    CONSOLIDATED = "consolidated"
    UNKNOWN = "unknown"


class PeriodKind(StrEnum):
    INSTANT = "instant"
    DURATION = "duration"


class PeriodFrequency(StrEnum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    TTM = "ttm"
    POINT_IN_TIME = "point_in_time"
    IRREGULAR = "irregular"


class DateResolution(StrEnum):
    EXACT = "exact"
    DERIVED = "derived"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"


class FinancialPeriod(BaseModel):
    """Fiscal labels supplement actual dates; instant periods have no start date."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    kind: PeriodKind
    fiscal_label: str = Field(min_length=1)
    end_date: date | None = None
    frequency: PeriodFrequency = PeriodFrequency.IRREGULAR
    date_resolution: DateResolution = DateResolution.EXACT
    derivation_rule: str | None = None
    derivation_inputs: tuple[str, ...] = ()
    original_date_key: str | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    start_date: date | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        if self.kind == PeriodKind.INSTANT and self.start_date is not None:
            raise ValueError("An instant period must not have a start date")
        if self.date_resolution == DateResolution.DERIVED and (
            not self.derivation_rule or not self.derivation_inputs
        ):
            raise ValueError("Derived dates require a rule and inputs")
        if self.date_resolution == DateResolution.EXACT and self.end_date is None:
            raise ValueError("Exact periods require an end date")
        if self.kind == PeriodKind.DURATION and self.date_resolution == DateResolution.EXACT:
            if self.start_date is None:
                raise ValueError("A duration period requires a start date")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("A period's start date must not follow its end date")
        return self


class UnitKind(StrEnum):
    UNKNOWN = "unknown"
    MONEY = "money"
    NUMBER = "number"
    DAYS = "days"
    SHARES = "shares"
    PERCENT = "percent"
    RATIO = "ratio"


class FinancialUnit(BaseModel):
    """Scale is a multiplier; percent values are percentage points (25 means 25%)."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    kind: UnitKind
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    scale: ExactDecimal = Field(default=Decimal("1"), gt=0)

    @model_validator(mode="after")
    def validate_currency(self) -> Self:
        if self.kind == UnitKind.MONEY and self.currency is None:
            raise ValueError("Monetary units require a currency")
        if self.kind != UnitKind.MONEY and self.currency is not None:
            raise ValueError("Currency is only valid for monetary units")
        return self


class ValueStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    BLANK = "blank"
    NOT_REPORTED = "not_reported"
    NOT_APPLICABLE = "not_applicable"
    PARSE_ERROR = "parse_error"
    UNAVAILABLE = "unavailable"


class ParsedValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    original: str
    value: ExactDecimal | None
    status: ValueStatus
    unit: FinancialUnit
    reason: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == ValueStatus.AVAILABLE) != (self.value is not None):
            raise ValueError("Only available values may be non-null")
        return self


class FinancialObservation(BaseModel):
    """A normalized observation retains its original representation and source link."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    layer: Literal["NORMALIZED"] = "NORMALIZED"
    id: UUID
    company_id: UUID
    metric: str = Field(min_length=1)
    metric_id: str | None = None
    mapping_status: MetricMappingStatus = MetricMappingStatus.UNMAPPED
    original_label: str | None = None
    series_key: str | None = None
    publication_at: AwareDatetime | None = None
    source_revision: str | None = None
    value: ExactDecimal | None
    value_status: ValueStatus
    unit: FinancialUnit
    period: FinancialPeriod
    reporting_scope: ReportingScope
    provenance: ObservationProvenance
    original_value: Annotated[str, StringConstraints(strip_whitespace=False)] | None = None
    original_unit: FinancialUnit | None = None

    @model_validator(mode="after")
    def validate_value_status(self) -> Self:
        if self.value_status == ValueStatus.AVAILABLE and self.value is None:
            raise ValueError("An available observation requires a value")
        if self.value_status == ValueStatus.AVAILABLE and (
            self.original_value is None or self.original_unit is None
        ):
            raise ValueError("An available observation requires its original value and unit")
        if self.value_status != ValueStatus.AVAILABLE and self.value is not None:
            raise ValueError("A missing observation must have a null value")
        return self


class DerivedMetric(BaseModel):
    """A future Python-calculated result referencing exact immutable input records."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    layer: Literal["DERIVED"] = "DERIVED"
    id: UUID
    company_id: UUID
    metric: str = Field(min_length=1)
    value: ExactDecimal
    unit: FinancialUnit
    period: FinancialPeriod
    reporting_scope: ReportingScope
    provenance: CalculationProvenance
