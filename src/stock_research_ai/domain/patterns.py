"""Immutable quantitative pattern evidence; outcomes do not establish intent."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.calculations import (
    AlignmentPolicy,
    CalculationIssue,
    CalculationModel,
    FormulaDefinition,
)
from stock_research_ai.domain.financial import ExactDecimal, FinancialObservation, ReportingScope
from stock_research_ai.domain.sector_kpis import (
    ProfileClassification,
    SectorProfile,
    SemanticResolution,
)

PatternStatus = Literal[
    "AVAILABLE",
    "INSUFFICIENT_INPUT",
    "NOT_APPLICABLE",
    "UNVERIFIED_METRIC_SEMANTICS",
    "INCOMPATIBLE_PERIOD",
    "INCOMPATIBLE_SCOPE",
    "INVALID_INPUT",
]
Outcome = Literal["TRIGGERED", "NOT_TRIGGERED", "OBSERVATION_ONLY"]


class ThresholdPolicy(CalculationModel):
    id: str
    version: int = Field(ge=1)
    basis: Literal["SYSTEM_DEFINED", "DIRECTIONAL_CONDITION"]
    threshold: ExactDecimal
    unit: str
    operator: Literal["LESS_THAN_OR_EQUAL", "LESS_THAN"]
    description: str


class PatternDefinition(CalculationModel):
    pattern_id: str
    version: int = Field(default=1, ge=1)
    applicable_profiles: tuple[str, ...]
    required_semantics: tuple[str, ...]
    required_calculations: tuple[str, ...] = ()
    annual_periods: int = Field(ge=1)
    period_policy: str = "CONSECUTIVE_ANNUAL_DECLARED_FISCAL_ENDPOINTS_V1"
    scope_policy: str = "SAME_COMPANY_SCOPE_CURRENCY_V1"
    rule_id: str
    rule_version: int = 1
    conditions: tuple[str, ...]
    threshold: ThresholdPolicy | None = None
    interpretation_boundary: str = (
        "Quantitative observation or review flag only; no intent conclusion."
    )


class PatternSelection(CalculationModel):
    pattern_id: str
    version: int = Field(default=1, ge=1)


class PinnedCalculation(CalculationModel):
    role: str
    snapshot_id: UUID
    metric_id: str


class PatternRequest(CalculationModel):
    company_id: UUID
    extraction_id: UUID
    classification: ProfileClassification
    profile_id: str
    profile_version: int = 1
    reporting_scope: ReportingScope
    alignment: AlignmentPolicy
    current_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    patterns: tuple[PatternSelection, ...] = Field(min_length=1)
    calculation_mode: Literal["AUTO_CREATE", "PINNED"] = "AUTO_CREATE"
    pinned_calculations: tuple[PinnedCalculation, ...] = ()

    @model_validator(mode="after")
    def request_consistency(self) -> Self:
        if len({p.pattern_id for p in self.patterns}) != len(self.patterns):
            raise ValueError("Duplicate patterns")
        if len({p.role for p in self.pinned_calculations}) != len(self.pinned_calculations):
            raise ValueError("Duplicate calculation roles")
        if self.calculation_mode == "AUTO_CREATE" and self.pinned_calculations:
            raise ValueError("Cannot mix pinned and automatically created calculations")
        return self


class ExactValue(CalculationModel):
    name: str
    numerator: int
    denominator: int = Field(gt=0)
    value: ExactDecimal
    display: ExactDecimal
    unit: str
    formula: FormulaDefinition | None = None


class CalculationReference(CalculationModel):
    role: str
    snapshot_id: UUID
    metric_id: str
    extraction_id: UUID
    formula: FormulaDefinition
    profile_id: str | None = None
    profile_version: int | None = None
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PatternFinding(CalculationModel):
    id: UUID
    snapshot_id: UUID
    definition: PatternDefinition
    evaluation_status: PatternStatus
    rule_outcome: Outcome | None = None
    observed: tuple[ExactValue, ...] = ()
    inputs: tuple[FinancialObservation, ...] = ()
    mappings: tuple[SemanticResolution, ...] = ()
    calculations: tuple[CalculationReference, ...] = ()
    issues: tuple[CalculationIssue, ...] = ()
    statement: str

    @model_validator(mode="after")
    def consistency(self) -> Self:
        if (self.evaluation_status == "AVAILABLE") != (self.rule_outcome is not None):
            raise ValueError("Only available findings require an outcome")
        if self.evaluation_status == "AVAILABLE":
            if not self.inputs or any(i.severity == "ERROR" for i in self.issues):
                raise ValueError("Available finding requires valid evidence")
            if len({o.id for o in self.inputs}) != len(self.inputs):
                raise ValueError("Duplicate finding observation IDs")
            for o in self.inputs:
                matches = [m for m in self.mappings if m.observation_id == o.id]
                if (
                    len(matches) != 1
                    or matches[0].status != "VERIFIED"
                    or not matches[0].definition
                ):
                    raise ValueError("Each input needs one verified mapping")
                if matches[0].provenance != o.provenance:
                    raise ValueError("Mapping provenance mismatch")
        return self


class PatternExecution(CalculationModel):
    calculation_mode: Literal["AUTO_CREATE", "PINNED"]
    created_snapshot_ids: tuple[UUID, ...] = ()
    referenced_snapshot_ids: tuple[UUID, ...] = ()


class PatternSnapshot(CalculationModel):
    schema_version: Literal["4D.1"] = "4D.1"
    id: UUID
    request: PatternRequest
    profile: SectorProfile | None
    evaluated_at: AwareDatetime
    source_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution: PatternExecution
    findings: tuple[PatternFinding, ...]

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if [(f.definition.pattern_id, f.definition.version) for f in self.findings] != [
            (p.pattern_id, p.version) for p in self.request.patterns
        ]:
            raise ValueError("Pattern inventory mismatch")
        if any(f.snapshot_id != self.id for f in self.findings):
            raise ValueError("Pattern snapshot lineage mismatch")
        if any(o.company_id != self.request.company_id for f in self.findings for o in f.inputs):
            raise ValueError("Pattern company lineage mismatch")
        if self.profile and (self.profile.profile_id, self.profile.version) != (
            self.request.profile_id,
            self.request.profile_version,
        ):
            raise ValueError("Pattern profile lineage mismatch")
        if any(
            r.extraction_id != self.request.extraction_id
            for f in self.findings
            for r in f.calculations
        ):
            raise ValueError("Calculation extraction lineage mismatch")
        if len({f.id for f in self.findings}) != len(self.findings):
            raise ValueError("Duplicate finding IDs")
        refs = {r.snapshot_id for f in self.findings for r in f.calculations}
        if refs != set(self.execution.referenced_snapshot_ids):
            raise ValueError("Execution references mismatch")
        if self.execution.calculation_mode != self.request.calculation_mode:
            raise ValueError("Execution mode mismatch")
        if self.execution.calculation_mode == "PINNED" and self.execution.created_snapshot_ids:
            raise ValueError("Pinned execution cannot create calculations")
        if not set(self.execution.created_snapshot_ids) <= refs:
            raise ValueError("Created calculation missing from finding lineage")
        return self
