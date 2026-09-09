"""Immutable calculation evidence. Raw lineage belongs to actual input observations."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.financial import ExactDecimal, FinancialObservation, ReportingScope
from stock_research_ai.domain.provenance import ObservationProvenance


class CalculationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


CalculationStatus = Literal[
    "AVAILABLE",
    "INSUFFICIENT_INPUT",
    "NOT_APPLICABLE",
    "DIVISION_BY_ZERO",
    "INVALID_INPUT",
    "INVALID_PERIOD",
    "INCOMPATIBLE_SERIES",
    "UNVERIFIED_METRIC_SEMANTICS",
    "INCOMPATIBLE_PERIOD",
    "INCOMPATIBLE_SCOPE",
]


class CalculationIssue(CalculationModel):
    code: str
    severity: Literal["LIMITATION", "ERROR"]
    observation_ids: tuple[UUID, ...] = ()
    message: str


class SectorDeclaration(CalculationModel):
    classification: Literal["BANK", "OTHER_FINANCIAL", "NON_FINANCIAL", "UNKNOWN"]
    basis: Literal["CALLER_DECLARED", "VERIFIED_MAPPING"]
    declaration: str = Field(min_length=1)
    provenance: ObservationProvenance | None = None

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if self.basis == "VERIFIED_MAPPING" and not self.provenance:
            raise ValueError("Verified sector mapping requires evidence")
        return self


class AlignmentPolicy(CalculationModel):
    id: Literal["DECLARED_FISCAL_PERIOD_KEYS"] = "DECLARED_FISCAL_PERIOD_KEYS"
    version: Literal["1"] = "1"
    fiscal_year_end_month: int = Field(ge=1, le=12)
    basis: Literal["CALLER_DECLARED"] = "CALLER_DECLARED"
    declaration: str = Field(min_length=1)


class CalculationSpec(CalculationModel):
    metric_id: str = Field(min_length=1)
    formula_id: Literal[
        "SOURCE_METRIC_GROWTH",
        "OPERATING_MARGIN",
        "AVERAGE_ASSETS_RETURN",
        "OPERATING_MARGIN_CHANGE_BPS",
    ]
    source_metric: Literal["revenue", "sales", "net_profit", "eps", "deposits"] = "net_profit"
    current_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    comparison: Literal["ANNUAL_YOY", "QUARTERLY_YOY", "QUARTERLY_SEQUENTIAL"] = "ANNUAL_YOY"


class CalculationRequest(CalculationModel):
    company_id: UUID
    extraction_id: UUID
    reporting_scope: ReportingScope
    sector: SectorDeclaration
    alignment: AlignmentPolicy
    calculations: tuple[CalculationSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({s.metric_id for s in self.calculations}) != len(self.calculations):
            raise ValueError("Duplicate requested metric IDs")
        return self


class FormulaDefinition(CalculationModel):
    id: str
    version: Literal["1"] = "1"
    expression: str
    precision: Literal[50] = 50
    working_precision: int = Field(default=50, ge=50)
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"
    display_places: Literal[2] = 2


class CalculationResult(CalculationModel):
    classification: Literal["CALCULATION"] = "CALCULATION"
    snapshot_id: UUID
    extraction_id: UUID
    metric_id: str
    label: str
    formula: FormulaDefinition
    alignment: AlignmentPolicy
    sector: SectorDeclaration
    period: str
    comparison: str
    reporting_scope: ReportingScope
    calculated_at: AwareDatetime
    status: CalculationStatus
    value: ExactDecimal | None = None
    display_value: ExactDecimal | None = None
    exact_numerator: int | None = None
    exact_denominator: int | None = None
    unit: Literal["PERCENT", "BASIS_POINTS"] = "PERCENT"
    inputs: tuple[FinancialObservation, ...] = ()
    issues: tuple[str, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        values = (self.value, self.display_value, self.exact_numerator, self.exact_denominator)
        if self.status == "AVAILABLE":
            if any(v is None for v in values) or not self.inputs or not self.exact_denominator:
                raise ValueError("Successful calculation requires values and actual inputs")
        elif any(v is not None for v in values):
            raise ValueError("Failed calculations cannot contain values")
        return self


class CalculationSnapshot(CalculationModel):
    schema_version: Literal["4.1"] = "4.1"
    id: UUID
    request: CalculationRequest
    source_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    calculated_at: AwareDatetime
    results: tuple[CalculationResult, ...]

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if tuple(r.metric_id for r in self.results) != tuple(
            s.metric_id for s in self.request.calculations
        ):
            raise ValueError("Result inventory mismatch")
        for r, spec in zip(self.results, self.request.calculations, strict=True):
            if (
                r.snapshot_id != self.id
                or r.extraction_id != self.request.extraction_id
                or r.alignment != self.request.alignment
                or r.sector != self.request.sector
                or r.calculated_at != self.calculated_at
                or r.reporting_scope != self.request.reporting_scope
                or r.period != spec.current_period
                or r.comparison != spec.comparison
                or r.formula.id != spec.formula_id
                or any(o.company_id != self.request.company_id for o in r.inputs)
            ):
                raise ValueError("Calculation lineage mismatch")
        return self
