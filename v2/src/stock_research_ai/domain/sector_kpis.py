"""Versioned sector eligibility metadata around shared financial calculations."""

from datetime import date
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.calculations import (
    AlignmentPolicy,
    CalculationIssue,
    CalculationModel,
    CalculationStatus,
    FormulaDefinition,
    SectorDeclaration,
)
from stock_research_ai.domain.financial import ExactDecimal, FinancialObservation, ReportingScope
from stock_research_ai.domain.provenance import ObservationProvenance


class ProfileClassification(SectorDeclaration):
    version: int = Field(default=1, ge=1)
    effective_start: date | None = None
    effective_end: date | None = None

    @model_validator(mode="after")
    def dates(self) -> Self:
        if (
            self.effective_start
            and self.effective_end
            and self.effective_start > self.effective_end
        ):
            raise ValueError("Invalid classification effective interval")
        return self


class KPIDefinition(CalculationModel):
    metric_id: str
    version: int = Field(default=1, ge=1)
    category: Literal["SECTOR_KPI", "GENERIC_SOURCE_METRIC"] = "SECTOR_KPI"
    applicable_profiles: tuple[str, ...]
    required_semantics: tuple[str, ...]
    formula: FormulaDefinition | None
    source_metric: Literal["revenue", "sales", "net_profit", "eps", "deposits"] = "net_profit"
    unit: Literal["PERCENT", "BASIS_POINTS"] = "PERCENT"
    period_policy: str
    limitations: tuple[str, ...] = ()


class SectorProfile(CalculationModel):
    profile_id: str
    version: int = Field(ge=1)
    classification: Literal["BANK", "NON_FINANCIAL"]
    description: str
    supported_kpis: tuple[str, ...]


class SemanticMappingDefinition(CalculationModel):
    mapping_id: str
    version: int = Field(default=1, ge=1)
    provider: str
    section: str
    source_metric_id: str
    source_identity: str
    source_definition: str
    extractor_mapping_version: str = "1"
    canonical_semantic: str
    meaning: str


class SemanticResolution(CalculationModel):
    observation_id: UUID
    provider: str
    section: str
    source_metric_id: str | None
    source_identity: str
    canonical_semantic: str
    status: Literal["VERIFIED", "UNVERIFIED"]
    definition: SemanticMappingDefinition | None
    provenance: ObservationProvenance


class SectorKPIRequest(CalculationModel):
    company_id: UUID
    extraction_id: UUID
    reporting_scope: ReportingScope
    classification: ProfileClassification
    profile_id: str
    profile_version: int = Field(default=1, ge=1)
    alignment: AlignmentPolicy
    current_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    comparison: Literal["ANNUAL_YOY", "QUARTERLY_YOY", "QUARTERLY_SEQUENTIAL"] = "ANNUAL_YOY"
    kpis: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.kpis)) != len(self.kpis):
            raise ValueError("Duplicate requested KPIs")
        return self


class SectorKPIResult(CalculationModel):
    classification: Literal["CALCULATION"] = "CALCULATION"
    snapshot_id: UUID
    extraction_id: UUID
    definition: KPIDefinition
    profile: SectorProfile
    sector: ProfileClassification
    alignment: AlignmentPolicy
    period: str
    comparison: str
    reporting_scope: ReportingScope
    calculated_at: AwareDatetime
    status: CalculationStatus
    value: ExactDecimal | None = None
    display_value: ExactDecimal | None = None
    exact_numerator: int | None = None
    exact_denominator: int | None = None
    inputs: tuple[FinancialObservation, ...] = ()
    mappings: tuple[SemanticResolution, ...] = ()
    issues: tuple[CalculationIssue, ...] = ()

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.status == "AVAILABLE":
            if any(
                v is None
                for v in (
                    self.value,
                    self.display_value,
                    self.exact_numerator,
                    self.exact_denominator,
                )
            ):
                raise ValueError("Successful KPI requires exact arithmetic and display")
            if not self.definition.formula or not self.inputs or not self.exact_denominator:
                raise ValueError("Successful KPI requires formula and inputs")
            for observation in self.inputs:
                links = [m for m in self.mappings if m.observation_id == observation.id]
                if len(links) != 1 or links[0].status != "VERIFIED" or links[0].definition is None:
                    raise ValueError(
                        "Every KPI input requires exactly one verified semantic mapping"
                    )
                if links[0].provenance != observation.provenance:
                    raise ValueError("Mapping provenance mismatch")
            if any(i.severity == "ERROR" for i in self.issues):
                raise ValueError("Successful KPI cannot hide errors")
        elif any(
            v is not None
            for v in (self.value, self.display_value, self.exact_numerator, self.exact_denominator)
        ):
            raise ValueError("Failed KPI cannot contain calculated values")
        return self


class SectorKPIEvaluation(CalculationModel):
    payload_type: Literal["sector_kpi_evaluation"] = "sector_kpi_evaluation"
    schema_version: Literal["4C.1"] = "4C.1"
    id: UUID
    request: SectorKPIRequest
    profile: SectorProfile
    source_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    calculated_at: AwareDatetime
    results: tuple[SectorKPIResult, ...]

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if (self.profile.profile_id, self.profile.version) != (
            self.request.profile_id,
            self.request.profile_version,
        ):
            raise ValueError("Profile selection mismatch")
        if tuple(r.definition.metric_id for r in self.results) != self.request.kpis:
            raise ValueError("KPI inventory mismatch")
        for r in self.results:
            if (
                r.snapshot_id != self.id
                or r.extraction_id != self.request.extraction_id
                or r.profile != self.profile
                or r.sector != self.request.classification
                or r.alignment != self.request.alignment
                or r.period != self.request.current_period
                or r.comparison != self.request.comparison
                or r.calculated_at != self.calculated_at
                or r.reporting_scope != self.request.reporting_scope
                or any(o.company_id != self.request.company_id for o in r.inputs)
            ):
                raise ValueError("KPI lineage mismatch")
        return self
