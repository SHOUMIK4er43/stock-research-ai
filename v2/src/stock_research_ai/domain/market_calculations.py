"""Market-specific metadata using the shared derived-evidence primitives."""

from datetime import date
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.calculations import (
    CalculationIssue,
    CalculationModel,
    CalculationStatus,
    FormulaDefinition,
)
from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.market import MarketInstrument, MarketObservation

MarketMetric = Literal[
    "PRICE_RETURN",
    "PRICE_CAGR",
    "MAX_DRAWDOWN",
    "REALIZED_VOLATILITY",
    "SMA",
    "BENCHMARK_RELATIVE_RETURN",
]


class MarketCalculationSpec(CalculationModel):
    metric_id: MarketMetric
    window: int = Field(default=20, ge=1)


class MarketCalculationRequest(CalculationModel):
    extraction_id: UUID
    instrument_id: UUID
    benchmark_extraction_id: UUID | None = None
    benchmark_instrument_id: UUID | None = None
    start_date: date
    end_date: date
    start_observation_id: UUID | None = None
    end_observation_id: UUID | None = None
    alignment_policy: Literal["EXACT_DATES_ONLY_V1"] = "EXACT_DATES_ONLY_V1"
    benchmark_alignment_policy: Literal["EXACT_SHARED_ENDPOINTS_V1"] = "EXACT_SHARED_ENDPOINTS_V1"
    price_field: Literal["close"] = "close"
    calculations: tuple[MarketCalculationSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({(s.metric_id, s.window) for s in self.calculations}) != len(self.calculations):
            raise ValueError("Duplicate requested calculation")
        if bool(self.benchmark_extraction_id) != bool(self.benchmark_instrument_id):
            raise ValueError("Benchmark requires both extraction and instrument identity")
        return self


class MarketSourceReference(CalculationModel):
    extraction_id: UUID
    instrument: MarketInstrument | None
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MarketCalculationInput(CalculationModel):
    extraction_id: UUID
    observation: MarketObservation


class DrawdownEpisode(CalculationModel):
    peak_observation_id: UUID
    peak_date: date
    peak_value: ExactDecimal
    trough_observation_id: UUID
    trough_date: date
    trough_value: ExactDecimal
    recovery_observation_id: UUID | None = None
    recovery_date: date | None = None
    recovery_value: ExactDecimal | None = None


class MarketCalculationResult(CalculationModel):
    classification: Literal["CALCULATION"] = "CALCULATION"
    snapshot_id: UUID
    metric_id: MarketMetric
    formula: FormulaDefinition
    status: CalculationStatus
    calculated_at: AwareDatetime
    requested_start: date
    requested_end: date
    actual_start: date | None = None
    actual_end: date | None = None
    alignment_policy: str
    price_field: Literal["close"] = "close"
    sources: tuple[MarketSourceReference, ...]
    inputs: tuple[MarketCalculationInput, ...] = ()
    adjustment_basis: Literal["UNKNOWN", "UNADJUSTED", "PROVIDER_ADJUSTED"] = "UNKNOWN"
    corporate_action_coverage: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    corporate_action_reason: Literal["NOT_PROVIDED_BY_SOURCE"] = "NOT_PROVIDED_BY_SOURCE"
    issues: tuple[CalculationIssue, ...] = ()
    value: ExactDecimal | None = None
    display_value: ExactDecimal | None = None
    exact_numerator: int | None = None
    exact_denominator: int | None = None
    numerical_method: Literal["EXACT_RATIONAL", "DECIMAL_TRANSCENDENTAL"] = "EXACT_RATIONAL"
    unit: Literal["PERCENT", "PERCENTAGE_POINTS", "PRICE", "INDEX_POINTS"] = "PERCENT"
    currency: str | None = None
    annualization_policy: Literal["ACTUAL_DAYS_365_FIXED_V1", "ARITHMETIC_SAMPLE_252_V1"] | None = (
        None
    )
    elapsed_days: int | None = None
    elapsed_years: ExactDecimal | None = None
    observation_count: int = Field(default=0, ge=0)
    return_count: int | None = None
    annualization_factor: Literal[252] | None = None
    return_definition: Literal["ARITHMETIC_SIMPLE_RETURN"] | None = None
    variance_definition: Literal["SAMPLE_VARIANCE"] | None = None
    window: int | None = None
    episode: DrawdownEpisode | None = None
    company_price_return: ExactDecimal | None = None
    benchmark_price_return: ExactDecimal | None = None

    @model_validator(mode="after")
    def valid_result(self) -> Self:
        if self.status == "AVAILABLE":
            if self.value is None or self.display_value is None or not self.inputs:
                raise ValueError("Successful calculation requires values and inputs")
            if self.actual_start is None or self.actual_end is None:
                raise ValueError("Successful calculation requires actual dates")
            if any(i.severity == "ERROR" for i in self.issues):
                raise ValueError("Available calculation cannot hide errors")
            if self.numerical_method == "EXACT_RATIONAL" and (
                self.exact_numerator is None or not self.exact_denominator
            ):
                raise ValueError("Rational result needs exact fraction")
        elif any(
            v is not None
            for v in (
                self.value,
                self.display_value,
                self.exact_numerator,
                self.exact_denominator,
                self.episode,
                self.company_price_return,
                self.benchmark_price_return,
            )
        ):
            raise ValueError("Failed result must not contain calculated values")
        return self


class MarketCalculationSnapshot(CalculationModel):
    schema_version: Literal["4B.1"] = "4B.1"
    id: UUID
    request: MarketCalculationRequest
    calculated_at: AwareDatetime
    sources: tuple[MarketSourceReference, ...]
    results: tuple[MarketCalculationResult, ...]

    @model_validator(mode="after")
    def lineage(self) -> Self:
        refs = {s.extraction_id: s for s in self.sources}
        expected = {self.request.extraction_id}
        if self.request.benchmark_extraction_id:
            expected.add(self.request.benchmark_extraction_id)
        if set(refs) != expected or len(refs) != len(self.sources):
            raise ValueError("Source inventory mismatch")
        if len(self.results) != len(self.request.calculations):
            raise ValueError("Calculation inventory mismatch")
        for result, spec in zip(self.results, self.request.calculations, strict=True):
            if (
                result.snapshot_id != self.id
                or result.calculated_at != self.calculated_at
                or result.metric_id != spec.metric_id
                or result.formula.id != spec.metric_id
                or result.requested_start != self.request.start_date
                or result.requested_end != self.request.end_date
                or result.sources != self.sources
            ):
                raise ValueError("Calculation lineage mismatch")
            if result.alignment_policy != (
                self.request.benchmark_alignment_policy
                if spec.metric_id == "BENCHMARK_RELATIVE_RETURN"
                else self.request.alignment_policy
            ):
                raise ValueError("Alignment policy mismatch")
            for inp in result.inputs:
                ref = refs.get(inp.extraction_id)
                if (
                    not ref
                    or not ref.instrument
                    or inp.observation.instrument_id != ref.instrument.id
                ):
                    raise ValueError("Input identity mismatch")
        return self
