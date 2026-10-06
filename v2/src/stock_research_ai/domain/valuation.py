"""Immutable facts, explicit assumptions, and deterministic valuation results."""

from datetime import date
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.calculations import (
    CalculationIssue,
    CalculationModel,
    CalculationStatus,
)
from stock_research_ai.domain.financial import ExactDecimal, FinancialObservation, ReportingScope
from stock_research_ai.domain.market import MarketObservation
from stock_research_ai.domain.provenance import RawArtifact, SourceRetrieval
from stock_research_ai.domain.sector_kpis import ProfileClassification, SemanticResolution

METHODS = (
    "PRICE_TO_EARNINGS",
    "PRICE_TO_BOOK",
    "FCFF_DCF",
    "IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE",
    "REQUIRED_EPS_CAGR_FOR_RETURN",
    "REQUIRED_BVPS_CAGR_FOR_RETURN",
)


class EvidencePin(CalculationModel):
    kind: Literal["FINANCIAL", "MARKET", "CALCULATION"]
    snapshot_id: UUID
    observation_id: UUID | None = None
    instrument_id: UUID | None = None
    observation_date: date | None = None
    metric_id: str | None = None

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.kind == "CALCULATION":
            if not self.metric_id or self.observation_id is not None:
                raise ValueError("Calculation pin needs metric ID, not an observation ID")
        elif self.observation_id is None:
            raise ValueError("Observation pin requires its exact ID")
        if self.kind == "MARKET" and (self.instrument_id is None or self.observation_date is None):
            raise ValueError("Market price requires instrument and exact observation date")
        return self


class ValuationAssumption(CalculationModel):
    assumption_id: UUID
    classification: Literal["SCENARIO"] = "SCENARIO"
    semantic: str
    value: ExactDecimal
    unit: Literal["DECIMAL_RATE", "CURRENCY", "CURRENCY_PER_SHARE", "SHARES", "YEARS", "MULTIPLE_X"]
    currency: str | None = None
    scale: ExactDecimal = Field(default=Decimal(1), gt=0)
    provenance_basis: Literal["SOURCE_EVIDENCE", "CALLER_DECLARED"]
    declaration: str = Field(min_length=1)
    evidence: EvidencePin | None = None
    effective_context: str | None = None

    @model_validator(mode="after")
    def provenance(self) -> Self:
        if (self.provenance_basis == "SOURCE_EVIDENCE") != (self.evidence is not None):
            raise ValueError("Source-backed assumptions require evidence; declarations do not")
        if self.semantic in {"exit_pe", "exit_pb"} and self.provenance_basis != "CALLER_DECLARED":
            raise ValueError("Exit multiples must be caller declared")
        if (self.unit in {"CURRENCY", "CURRENCY_PER_SHARE"}) != (self.currency is not None):
            raise ValueError("Currency required only on monetary assumptions")
        return self


class SolverPolicy(CalculationModel):
    id: str = "DECIMAL_BISECTION_V1"
    version: int = Field(default=1, ge=1)
    residual_tolerance: ExactDecimal = Field(default=Decimal("1e-30"), gt=0)
    growth_tolerance: ExactDecimal = Field(default=Decimal("1e-30"), gt=0)
    max_iterations: int = Field(default=512, ge=1)


class ValuationPrecision(CalculationModel):
    id: str = "VALUATION_DECIMAL_V1"
    version: int = Field(default=1, ge=1)
    working_digits: int = Field(default=70, ge=70)
    stored_digits: Literal[50] = 50
    display_places: Literal[2] = 2
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"


class ValuationScenario(CalculationModel):
    scenario_id: UUID
    label: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    method: str
    assumptions: tuple[ValuationAssumption, ...]
    solver: SolverPolicy = Field(default_factory=SolverPolicy)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if self.method not in METHODS[2:]:
            raise ValueError("Scenario must select an approved scenario method")
        if len({a.semantic for a in self.assumptions}) != len(self.assumptions):
            raise ValueError("Duplicate assumption semantic")
        return self


class ValuationSelection(CalculationModel):
    method: str
    version: Literal[1] = 1
    price: EvidencePin | None = None
    denominator: EvidencePin | None = None
    scenario: ValuationScenario | None = None

    @model_validator(mode="after")
    def inventory(self) -> Self:
        if self.method not in METHODS:
            raise ValueError("Unknown valuation method")
        if self.method in METHODS[:2] and self.scenario is not None:
            raise ValueError("Observed multiples cannot use scenario assumptions")
        if self.scenario and self.scenario.method != self.method:
            raise ValueError("Scenario method mismatch")
        return self


class ValuationRequest(CalculationModel):
    company_id: UUID
    financial_extraction_id: UUID | None = None
    reporting_scope: ReportingScope
    profile_id: str
    profile_version: int = 1
    classification: ProfileClassification
    as_of_policy: Literal["CURRENT_PINNED_SNAPSHOT_V1"] = "CURRENT_PINNED_SNAPSHOT_V1"
    selections: tuple[ValuationSelection, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self) -> Self:
        scenarios = [s.scenario for s in self.selections if s.scenario]
        if len({s.scenario_id for s in scenarios}) != len(scenarios):
            raise ValueError("Duplicate scenario identity")
        ids = [a.assumption_id for s in scenarios for a in s.assumptions]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate assumption identity")
        return self


class ValuationDefinition(CalculationModel):
    method: str
    version: int = 1
    formula_id: str
    formula_version: int = 1
    expression: str
    profiles: tuple[str, ...]
    classification: Literal["CALCULATION", "SCENARIO"]
    category: Literal[
        "OBSERVED_MULTIPLE_CALCULATION", "SCENARIO_CALCULATION", "SCENARIO_EXPECTATION"
    ]


class ValuationSource(CalculationModel):
    kind: Literal["FINANCIAL", "MARKET", "CALCULATION"]
    snapshot_id: UUID
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    artifacts: tuple[RawArtifact, ...] = ()
    retrievals: tuple[SourceRetrieval, ...] = ()


class ValuationInput(CalculationModel):
    classification: Literal["FACT", "CALCULATION"]
    pin: EvidencePin
    semantic: str
    value: ExactDecimal | None
    unit: str
    currency: str | None
    scale: ExactDecimal = Decimal(1)
    financial_observation: FinancialObservation | None = None
    market_observation: MarketObservation | None = None
    mapping: SemanticResolution | None = None
    semantic_verified: bool

    @model_validator(mode="after")
    def evidence_value(self) -> Self:
        o = self.financial_observation
        m = self.market_observation
        if self.pin.kind == "FINANCIAL":
            if o is None or m is not None or self.classification != "FACT":
                raise ValueError("Financial FACT requires its original observation")
            if (self.value, self.currency, self.scale) != (o.value, o.unit.currency, o.unit.scale):
                raise ValueError("Financial input differs from pinned observation")
            if self.mapping and (
                self.mapping.observation_id != o.id or self.mapping.provenance != o.provenance
            ):
                raise ValueError("Semantic mapping lineage mismatch")
            if self.semantic_verified and (
                self.mapping is None
                or self.mapping.status != "VERIFIED"
                or self.mapping.canonical_semantic != self.semantic
            ):
                raise ValueError("Verified input requires an exact semantic mapping")
        elif self.pin.kind == "MARKET":
            if m is None or o is not None or self.classification != "FACT":
                raise ValueError("Market FACT requires its original observation")
            if (
                self.value != m.close
                or m.trading_date != self.pin.observation_date
                or m.instrument_id != self.pin.instrument_id
            ):
                raise ValueError("Market input differs from pinned observation")
        elif self.classification != "CALCULATION" or o is not None or m is not None:
            raise ValueError("Calculation input classification mismatch")
        return self


class ValuationValue(CalculationModel):
    name: str
    value: ExactDecimal
    display_value: ExactDecimal
    unit: str
    currency: str | None = None
    numerical_method: Literal[
        "DECIMAL_INTEGER_POWER", "DECIMAL_DIVISION", "DECIMAL_ROOT", "DECIMAL_BISECTION"
    ]
    numerical_version: int = 1


class SolverTrace(CalculationModel):
    policy: SolverPolicy
    lower: ExactDecimal
    upper: ExactDecimal
    lower_residual: ExactDecimal
    upper_residual: ExactDecimal
    iterations: int
    final_lower: ExactDecimal
    final_upper: ExactDecimal
    final_residual: ExactDecimal
    termination: str


class ValuationResult(CalculationModel):
    id: UUID
    snapshot_id: UUID
    selection_index: int
    definition: ValuationDefinition
    status: CalculationStatus
    inputs: tuple[ValuationInput, ...] = ()
    assumption_ids: tuple[UUID, ...] = ()
    scenario_id: UUID | None = None
    values: tuple[ValuationValue, ...] = ()
    solver: SolverTrace | None = None
    issues: tuple[CalculationIssue, ...] = ()

    @model_validator(mode="after")
    def state(self) -> Self:
        if (self.status == "AVAILABLE") != bool(self.values):
            raise ValueError("Available result requires values; failure must not contain outputs")
        if self.status == "AVAILABLE" and any(i.severity == "ERROR" for i in self.issues):
            raise ValueError("Available result contains blocking issue")
        return self


class ValuationSnapshot(CalculationModel):
    payload_type: Literal["valuation_snapshot"] = "valuation_snapshot"
    schema_version: Literal["5A.1"] = "5A.1"
    id: UUID
    request: ValuationRequest
    evaluated_at: AwareDatetime
    precision: ValuationPrecision
    sources: tuple[ValuationSource, ...]
    results: tuple[ValuationResult, ...]
    source_reported_multiples: tuple[FinancialObservation, ...] = ()
    source_reported_classification: Literal["FACT"] = "FACT"
    source_reported_category: Literal["SOURCE_REPORTED_VALUATION_EVIDENCE"] = (
        "SOURCE_REPORTED_VALUATION_EVIDENCE"
    )

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if len(self.results) != len(self.request.selections):
            raise ValueError("Result inventory mismatch")
        refs = {(s.kind, s.snapshot_id) for s in self.sources}
        if len(refs) != len(self.sources) or len({r.id for r in self.results}) != len(self.results):
            raise ValueError("Duplicate source or result identity")
        if (
            self.request.financial_extraction_id is not None
            and ("FINANCIAL", self.request.financial_extraction_id) not in refs
        ):
            raise ValueError("Pinned primary financial source missing")
        for selection in self.request.selections:
            required = [p for p in (selection.price, selection.denominator) if p is not None]
            if selection.scenario:
                required.extend(a.evidence for a in selection.scenario.assumptions if a.evidence)
            if any((p.kind, p.snapshot_id) not in refs for p in required):
                raise ValueError("Pinned source missing")
        for i, (result, selection) in enumerate(
            zip(self.results, self.request.selections, strict=True)
        ):
            if (
                result.snapshot_id != self.id
                or result.selection_index != i
                or result.definition.method != selection.method
            ):
                raise ValueError("Result identity mismatch")
            scenario = selection.scenario
            if result.scenario_id != (scenario.scenario_id if scenario else None):
                raise ValueError("Scenario reference mismatch")
            if (
                result.assumption_ids != tuple(a.assumption_id for a in scenario.assumptions)
                if scenario
                else result.assumption_ids != ()
            ):
                raise ValueError("Assumption reference mismatch")
            for item in result.inputs:
                if (item.pin.kind, item.pin.snapshot_id) not in refs:
                    raise ValueError("Input snapshot missing")
                o = item.financial_observation or item.market_observation
                if o is not None and o.id != item.pin.observation_id:
                    raise ValueError("Input observation mismatch")
                if (
                    item.financial_observation
                    and item.financial_observation.company_id != self.request.company_id
                ):
                    raise ValueError("Input company mismatch")
        return self
