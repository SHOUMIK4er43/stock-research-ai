"""Immutable technical series and internally resolvable derived-point lineage."""

from datetime import date
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.calculations import (
    CalculationIssue,
    CalculationModel,
    CalculationStatus,
)
from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.market_calculations import (
    MarketCalculationInput,
    MarketSourceReference,
)

PUBLIC_INDICATORS = (
    "EMA20",
    "RSI14",
    "MACD12_26",
    "MACD_SIGNAL9",
    "MACD_HISTOGRAM",
    "TRUE_RANGE",
    "ATR14",
    "BOLLINGER_MIDDLE20",
    "BOLLINGER_UPPER20_2",
    "BOLLINGER_LOWER20_2",
    "PLUS_DI14",
    "MINUS_DI14",
    "ADX14",
)


class PrecisionPolicy(CalculationModel):
    id: str = "TECHNICAL_DECIMAL_RECURSIVE_V1"
    version: int = Field(default=1, ge=1)
    working_digits: int = Field(default=70, ge=70)
    stored_digits: Literal[50] = 50
    display_places: Literal[2] = 2
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"


class IndicatorDefinition(CalculationModel):
    indicator_id: str
    version: int = Field(default=1, ge=1)
    formula_id: str
    formula_version: int = 1
    parameters: tuple[tuple[str, str], ...] = ()
    first_observation: int = Field(ge=1)
    required_fields: tuple[str, ...] = ("open", "high", "low", "close")
    seed_policy: str
    smoothing_policy: str
    public: bool = True


class TechnicalRequest(CalculationModel):
    extraction_id: UUID
    instrument_id: UUID
    start_date: date
    end_date: date
    start_observation_id: UUID | None = None
    end_observation_id: UUID | None = None
    indicators: tuple[str, ...] = Field(min_length=1)
    definition_version: Literal[1] = 1
    seed_scope_policy: Literal["REQUEST_RANGE_ONLY_V1"] = "REQUEST_RANGE_ONLY_V1"

    @model_validator(mode="after")
    def inventory(self) -> Self:
        if len(set(self.indicators)) != len(self.indicators) or any(
            i not in PUBLIC_INDICATORS for i in self.indicators
        ):
            raise ValueError("Only unique approved V1 indicator configurations are supported")
        if self.end_date < self.start_date:
            raise ValueError("Invalid requested range")
        return self


class IndicatorPoint(CalculationModel):
    id: UUID
    indicator_id: str
    definition_version: int
    date: date
    status: CalculationStatus
    value: ExactDecimal | None = None
    display_value: ExactDecimal | None = None
    numerical_method: Literal["DECIMAL_RECURSIVE", "DECIMAL_FINITE_WINDOW", "DECIMAL_SQRT"]
    observation_ids: tuple[UUID, ...] = ()
    seed_observation_ids: tuple[UUID, ...] = ()
    previous_point_id: UUID | None = None
    dependent_point_ids: tuple[UUID, ...] = ()
    issue: str | None = None

    @model_validator(mode="after")
    def value_state(self) -> Self:
        if (self.status == "AVAILABLE") != (
            self.value is not None and self.display_value is not None
        ):
            raise ValueError("Point status/value mismatch")
        if self.status != "AVAILABLE" and (
            self.value is not None or self.display_value is not None
        ):
            raise ValueError("Failed point contains value")
        return self


class IndicatorSeries(CalculationModel):
    definition: IndicatorDefinition
    status: CalculationStatus
    unit: Literal[
        "PRICE", "INDEX_POINTS", "DIMENSIONLESS_0_100", "SQUARED_PRICE", "SQUARED_INDEX_POINTS"
    ]
    currency: str | None = None
    numerical_method: str
    points: tuple[IndicatorPoint, ...]
    requested: bool
    issues: tuple[CalculationIssue, ...] = ()


class TechnicalSnapshot(CalculationModel):
    schema_version: Literal["4E.1"] = "4E.1"
    id: UUID
    request: TechnicalRequest
    calculated_at: AwareDatetime
    source: MarketSourceReference
    selected_start: date | None = None
    selected_end: date | None = None
    inputs: tuple[MarketCalculationInput, ...] = ()
    precision: PrecisionPolicy
    series: tuple[IndicatorSeries, ...]
    adjustment_basis: Literal["UNKNOWN", "UNADJUSTED", "PROVIDER_ADJUSTED"] = "UNKNOWN"
    corporate_action_coverage: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    corporate_action_reason: Literal["NOT_PROVIDED_BY_SOURCE"] = "NOT_PROVIDED_BY_SOURCE"
    issues: tuple[CalculationIssue, ...] = ()

    @model_validator(mode="after")
    def graph(self) -> Self:
        if self.source.extraction_id != self.request.extraction_id:
            raise ValueError("Source extraction mismatch")
        if {s.definition.indicator_id for s in self.series if s.requested} != set(
            self.request.indicators
        ):
            raise ValueError("Requested series inventory mismatch")
        if len({s.definition.indicator_id for s in self.series}) != len(self.series):
            raise ValueError("Duplicate series")
        observations = {i.observation.id: i for i in self.inputs}
        if len(observations) != len(self.inputs):
            raise ValueError("Duplicate input IDs")
        if any(
            i.extraction_id != self.request.extraction_id
            or i.observation.instrument_id != self.request.instrument_id
            for i in self.inputs
        ):
            raise ValueError("Input identity mismatch")
        dates = [i.observation.trading_date for i in self.inputs]
        if dates != sorted(set(dates)):
            raise ValueError("Input dates not unique and ordered")
        if dates and (
            self.selected_start != dates[0]
            or self.selected_end != dates[-1]
            or dates[0] != self.request.start_date
            or dates[-1] != self.request.end_date
        ):
            raise ValueError("Selected range mismatch")
        seen: dict[UUID, IndicatorPoint] = {}
        for s in self.series:
            if dates and [p.date for p in s.points] != dates:
                raise ValueError("Point dates must match selected observations")
            if s.status == "AVAILABLE" and (
                not s.points
                or s.points[-1].date != self.request.end_date
                or s.points[-1].status != "AVAILABLE"
            ):
                raise ValueError("Series status must match requested terminal point")
            for p in s.points:
                if p.id in seen:
                    raise ValueError("Duplicate point ID")
                if (
                    p.indicator_id != s.definition.indicator_id
                    or p.definition_version != s.definition.version
                ):
                    raise ValueError("Point definition mismatch")
                if not self.request.start_date <= p.date <= self.request.end_date:
                    raise ValueError("Point outside request")
                for o in (*p.observation_ids, *p.seed_observation_ids):
                    if o not in observations or observations[o].observation.trading_date > p.date:
                        raise ValueError("Unresolved/future observation")
                for ref in (
                    *p.dependent_point_ids,
                    *((p.previous_point_id,) if p.previous_point_id else ()),
                ):
                    if (
                        ref not in seen
                        or seen[ref].date > p.date
                        or seen[ref].status != "AVAILABLE"
                    ):
                        raise ValueError("Unresolved/future point")
                if p.previous_point_id and (
                    seen[p.previous_point_id].indicator_id != p.indicator_id
                    or seen[p.previous_point_id].date >= p.date
                ):
                    raise ValueError("Invalid recursive predecessor")
                if p.status == "AVAILABLE" and not (
                    p.observation_ids
                    or p.seed_observation_ids
                    or p.dependent_point_ids
                    or p.previous_point_id
                ):
                    raise ValueError("Point has no evidence lineage")
                seen[p.id] = p
        return self
