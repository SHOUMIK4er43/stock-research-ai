"""DETAILED_RESEARCH_V1 Box 11: deterministic valuation over a Box 10 forecast (no recommendation).

Four kinds of number, never blurred:
  BOX 10 / BOX 4  a forecast value, a forecast assumption or an actual anchor of the Box 10
                  dataset, or a Box 4 canonical balance (cash), referenced by its ID
  ASSUMPTION      a ValuationAssumption: one explicit valuation input (a rate, a weight, a
                  terminal growth, a share count, a bridge override), per scenario where it
                  is scenario-specific, with its unit and its citation
  MULTIPLE        a MultipleInput: one explicit or peer-cited trading multiple for one metric,
                  one scenario and one forecast year
  VALUATION       a ValuationValue: one registered formula applied to named inputs, each input
                  a reference to any of the above or to another valuation value

A valuation number that cannot be supported is UNAVAILABLE with a reason, never zero. Box 11
computes values only: it never ranks, rates or recommends (no buy / hold / sell, no sizing).
Units follow the repository convention: money in crore of the forecast currency, per-share
values in ones of that currency, rates in percent (12.5 means 12.5 %, never 0.125), share
counts in ones.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_analytics import MetricInput
from stock_research_ai.domain.financial_canonical import (
    Currency,
    FinancialPeriod,
    Industry,
    ReportingBasis,
    Scale,
)
from stock_research_ai.domain.forecasting import (
    MAX_HORIZON,
    MAX_MONEY,
    MAX_PERCENT,
    MAX_SHARE_COUNT,
    Code,
    FiscalYear,
    ForecastStatus,
    Label,
    Scenario,
    Sha256Hex,
)

VALUATION_SCHEMA: Literal["VALUATION_DATASET_V1"] = "VALUATION_DATASET_V1"
VALUATION_ENGINE_VERSION = "valuation/1"
VALUATION_FORMULA_VERSION = "valuation-formulas/1"
VALUATION_POLICY_VERSION = "valuation-policy/1"
MAX_ASSUMPTIONS = 500
MAX_MULTIPLES = 500
MAX_ISSUES = 2_000
#: Arithmetic guards, not economic caps: they reject only numbers no real company produces.
MAX_MULTIPLE = Decimal(10) ** 6
MAX_BETA = Decimal(1000)
MAX_PRICE = Decimal(10) ** 12  # per share, ones
MAX_ABS_VALUE = Decimal(10) ** 18  # crore / per share: above this a value is OUT_OF_RANGE
#: crore -> ones: per share = equity value (crore) x 10^7 / shares (ones)
CRORE = Decimal(10_000_000)


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


class ValuationMethod(StrEnum):
    DCF_FCFF_GORDON = "DCF_FCFF_GORDON"  # FCFF DCF, Gordon-growth terminal value
    DCF_FCFF_EXIT_MULTIPLE = "DCF_FCFF_EXIT_MULTIPLE"  # FCFF DCF, EV/EBITDA exit terminal value
    PE = "PE"  # equity value = attributable PAT x P/E
    EV_EBITDA = "EV_EBITDA"  # enterprise value = operating EBITDA x EV/EBITDA
    EV_EBIT = "EV_EBIT"  # enterprise value = operating EBIT x EV/EBIT
    PB = "PB"  # equity value = ordinary book equity x P/B (NON_BANK_FINANCIAL lender model)


DCF_METHODS = (ValuationMethod.DCF_FCFF_GORDON, ValuationMethod.DCF_FCFF_EXIT_MULTIPLE)
MULTIPLE_METHODS = (ValuationMethod.PE, ValuationMethod.EV_EBITDA, ValuationMethod.EV_EBIT)
#: the industrial default (unchanged); P/B is the lender model's, never a default here
DEFAULT_METHODS = (*DCF_METHODS, *MULTIPLE_METHODS)
#: NON_BANK_FINANCIAL: equity methods only (ordinary book equity, owners' PAT)
LENDER_METHODS = (ValuationMethod.PB, ValuationMethod.PE)


class MultipleBasis(StrEnum):
    EQUITY = "EQUITY"  # the multiple prices equity: no enterprise-to-equity bridge
    ENTERPRISE = "ENTERPRISE"  # the multiple prices the enterprise: bridged to equity


MULTIPLE_BASIS: dict[ValuationMethod, MultipleBasis] = {
    ValuationMethod.PE: MultipleBasis.EQUITY,
    ValuationMethod.EV_EBITDA: MultipleBasis.ENTERPRISE,
    ValuationMethod.EV_EBIT: MultipleBasis.ENTERPRISE,
    ValuationMethod.PB: MultipleBasis.EQUITY,
}


class MultipleSource(StrEnum):
    EXPLICIT = "EXPLICIT"  # supplied by the caller (analyst / user)
    PEER_REFERENCE = "PEER_REFERENCE"  # caller-supplied, citing structured comparable data


class AssumptionUnit(StrEnum):
    PERCENT = "PERCENT"  # 12.5 means 12.5 %
    RATIO = "RATIO"  # a pure number (beta)
    MULTIPLE = "MULTIPLE"  # x times
    MONEY = "MONEY"  # crore of the forecast currency
    PER_SHARE = "PER_SHARE"  # ones of the forecast currency per share
    COUNT = "COUNT"  # ones (shares)


class AssumptionKind(StrEnum):
    # scenario-specific (one per scenario)
    RISK_FREE_RATE = "RISK_FREE_RATE"
    BETA = "BETA"
    EQUITY_RISK_PREMIUM = "EQUITY_RISK_PREMIUM"
    COST_OF_EQUITY = "COST_OF_EQUITY"  # explicit cost of equity: replaces CAPM, never mixed
    PRE_TAX_COST_OF_DEBT = "PRE_TAX_COST_OF_DEBT"
    WACC_TAX_RATE = "WACC_TAX_RATE"  # marginal tax rate for the debt shield
    EQUITY_WEIGHT = "EQUITY_WEIGHT"  # target E / (D + E) %
    DEBT_WEIGHT = "DEBT_WEIGHT"  # target D / (D + E) %
    TERMINAL_GROWTH = "TERMINAL_GROWTH"
    TERMINAL_EV_EBITDA = "TERMINAL_EV_EBITDA"  # exit multiple (DCF_FCFF_EXIT_MULTIPLE only)
    # company-level facts at the valuation date (scenario-free)
    SHARE_COUNT = "SHARE_COUNT"  # shares outstanding at the valuation date, ones
    GROSS_DEBT = "GROSS_DEBT"  # replaces the Box 10 gross-debt anchor
    CASH = "CASH"  # replaces the Box 4 cash and cash equivalents
    MINORITY_INTEREST = "MINORITY_INTEREST"  # non-controlling interest (balance sheet)
    OTHER_EQUITY_ADJUSTMENT = "OTHER_EQUITY_ADJUSTMENT"  # signed, added to equity value
    MARKET_PRICE = "MARKET_PRICE"  # a stated price, for comparison only


class AssumptionSpec(BaseModel):
    model_config = _model()
    unit: AssumptionUnit
    minimum: Decimal
    maximum: Decimal
    minimum_exclusive: bool = False
    scenario_specific: bool
    citation_required: bool = False


_P, _R, _X = AssumptionUnit.PERCENT, AssumptionUnit.RATIO, AssumptionUnit.MULTIPLE
_MO, _PS, _C = AssumptionUnit.MONEY, AssumptionUnit.PER_SHARE, AssumptionUnit.COUNT
_RATE = dict(unit=_P, minimum=Decimal(-100), maximum=MAX_PERCENT, minimum_exclusive=True)
_SHARE = dict(unit=_P, minimum=Decimal(0), maximum=Decimal(100))
K = AssumptionKind
ASSUMPTION_SPECS: dict[AssumptionKind, AssumptionSpec] = {
    K.RISK_FREE_RATE: AssumptionSpec(**_RATE, scenario_specific=True),  # type: ignore[arg-type]
    K.BETA: AssumptionSpec(unit=_R, minimum=-MAX_BETA, maximum=MAX_BETA, scenario_specific=True),
    K.EQUITY_RISK_PREMIUM: AssumptionSpec(
        unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT, scenario_specific=True
    ),
    K.COST_OF_EQUITY: AssumptionSpec(**_RATE, scenario_specific=True),  # type: ignore[arg-type]
    K.PRE_TAX_COST_OF_DEBT: AssumptionSpec(**_RATE, scenario_specific=True),  # type: ignore[arg-type]
    K.WACC_TAX_RATE: AssumptionSpec(**_SHARE, scenario_specific=True),  # type: ignore[arg-type]
    K.EQUITY_WEIGHT: AssumptionSpec(**_SHARE, scenario_specific=True),  # type: ignore[arg-type]
    K.DEBT_WEIGHT: AssumptionSpec(**_SHARE, scenario_specific=True),  # type: ignore[arg-type]
    # g <= -100 % would make the terminal cash flow zero or change its sign
    K.TERMINAL_GROWTH: AssumptionSpec(**_RATE, scenario_specific=True),  # type: ignore[arg-type]
    K.TERMINAL_EV_EBITDA: AssumptionSpec(
        unit=_X,
        minimum=Decimal(0),
        maximum=MAX_MULTIPLE,
        minimum_exclusive=True,
        scenario_specific=True,
    ),
    K.SHARE_COUNT: AssumptionSpec(
        unit=_C,
        minimum=Decimal(0),
        maximum=MAX_SHARE_COUNT,
        minimum_exclusive=True,
        scenario_specific=False,
    ),
    K.GROSS_DEBT: AssumptionSpec(
        unit=_MO,
        minimum=Decimal(0),
        maximum=MAX_MONEY,
        scenario_specific=False,
        citation_required=True,
    ),
    K.CASH: AssumptionSpec(
        unit=_MO,
        minimum=Decimal(0),
        maximum=MAX_MONEY,
        scenario_specific=False,
        citation_required=True,
    ),
    K.MINORITY_INTEREST: AssumptionSpec(
        unit=_MO,
        minimum=-MAX_MONEY,
        maximum=MAX_MONEY,
        scenario_specific=False,
        citation_required=True,
    ),
    K.OTHER_EQUITY_ADJUSTMENT: AssumptionSpec(
        unit=_MO,
        minimum=-MAX_MONEY,
        maximum=MAX_MONEY,
        scenario_specific=False,
        citation_required=True,
    ),
    K.MARKET_PRICE: AssumptionSpec(
        unit=_PS,
        minimum=Decimal(0),
        maximum=MAX_PRICE,
        minimum_exclusive=True,
        scenario_specific=False,
        citation_required=True,
    ),
}
CAPM_KINDS = (K.RISK_FREE_RATE, K.BETA, K.EQUITY_RISK_PREMIUM)


def assumption_bounds_error(kind: AssumptionKind, value: Decimal) -> str | None:
    spec = ASSUMPTION_SPECS[kind]
    low_ok = value > spec.minimum if spec.minimum_exclusive else value >= spec.minimum
    if not low_ok or value > spec.maximum:
        sign = ">" if spec.minimum_exclusive else ">="
        return f"{kind.value} must be {sign} {spec.minimum} and <= {spec.maximum}"
    return None


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"  # a value, a method or the whole valuation could not be produced


class ValuationStatus(StrEnum):
    COMPLETE = "COMPLETE"  # fair value per share available
    PARTIAL = "PARTIAL"  # enterprise or equity value available, per-share value not
    UNAVAILABLE = "UNAVAILABLE"


# --- request ------------------------------------------------------------------------------------


class AssumptionInput(BaseModel):
    """One explicit valuation input. Bridge overrides and prices must cite their source."""

    model_config = _model()
    kind: AssumptionKind
    scenario: Scenario | None = None  # required exactly for scenario-specific kinds
    value: ExactDecimal
    unit: AssumptionUnit
    currency: Currency | None = None  # MONEY / PER_SHARE: must equal the forecast currency
    scale: Scale | None = None  # MONEY: CRORES; PER_SHARE and COUNT: ONES
    reference: Label | None = None
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        spec = ASSUMPTION_SPECS[self.kind]
        if self.unit != spec.unit:
            raise ValueError(f"{self.kind.value} is expressed in {spec.unit.value}")
        if spec.scenario_specific != (self.scenario is not None):
            raise ValueError(
                f"{self.kind.value} is "
                + ("stated per scenario" if spec.scenario_specific else "scenario-free")
            )
        problem = assumption_bounds_error(self.kind, self.value)
        if problem:
            raise ValueError(problem)
        if self.unit in (AssumptionUnit.MONEY, AssumptionUnit.PER_SHARE):
            if self.currency in (None, Currency.UNKNOWN, Currency.OTHER):
                raise ValueError(f"a {self.unit.value} assumption states its currency")
            wanted = Scale.CRORES if self.unit == AssumptionUnit.MONEY else Scale.ONES
            if self.scale != wanted:
                raise ValueError(f"a {self.unit.value} assumption is stated in {wanted.value}")
        elif self.unit == AssumptionUnit.COUNT:
            if self.scale != Scale.ONES or self.currency is not None:
                raise ValueError("a COUNT assumption is an absolute count (scale ONES)")
        elif self.currency is not None or self.scale is not None:
            raise ValueError(f"a {self.unit.value} assumption carries no currency or scale")
        if spec.citation_required and not (self.reference or self.evidence_ids):
            raise ValueError(f"{self.kind.value} cites its source (reference or evidence)")
        return self


class MultipleInput(BaseModel):
    """One trading multiple for one method, scenario and forecast year. Never inferred from
    qualitative text: explicit, or citing structured comparable-company data."""

    model_config = _model()
    method: Literal[
        ValuationMethod.PE, ValuationMethod.EV_EBITDA, ValuationMethod.EV_EBIT, ValuationMethod.PB
    ]
    scenario: Scenario
    fiscal_year: FiscalYear
    multiple: ExactDecimal = Field(gt=0, le=MAX_MULTIPLE)
    source: MultipleSource = MultipleSource.EXPLICIT
    reference: Label | None = None
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def cited(self) -> Self:
        if self.source == MultipleSource.PEER_REFERENCE and not (
            self.reference or self.evidence_ids
        ):
            raise ValueError("a peer-reference multiple cites its comparable-company data")
        return self


class ValuationRequest(BaseModel):
    model_config = _model()
    scenarios: tuple[Scenario, ...] = (Scenario.BASE, Scenario.BULL, Scenario.BEAR)
    #: not stated: the forecast model's default (industrial: DEFAULT_METHODS; NON_BANK_FINANCIAL:
    #: LENDER_METHODS); stated: exactly these, each applied or refused with a reason
    methods: tuple[ValuationMethod, ...] = DEFAULT_METHODS
    #: forecast years discounted explicitly; None = every Box 10 forecast year
    dcf_horizon: int | None = Field(default=None, ge=1, le=MAX_HORIZON)
    #: the basis the caller expects; must equal the forecast's basis when stated
    reporting_basis: Literal[ReportingBasis.CONSOLIDATED, ReportingBasis.STANDALONE] | None = None
    assumptions: tuple[AssumptionInput, ...] = Field(default=(), max_length=MAX_ASSUMPTIONS)
    multiples: tuple[MultipleInput, ...] = Field(default=(), max_length=MAX_MULTIPLES)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if not self.scenarios or len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("scenarios must be a non-empty set")
        if not self.methods or len(set(self.methods)) != len(self.methods):
            raise ValueError("methods must be a non-empty set")
        seen: set[tuple[AssumptionKind, Scenario | None]] = set()
        for a in self.assumptions:
            key = (a.kind, a.scenario)
            if key in seen:
                where = f"/{a.scenario.value}" if a.scenario else ""
                raise ValueError(f"duplicate assumption {a.kind.value}{where}")
            if a.scenario is not None and a.scenario not in self.scenarios:
                raise ValueError(f"assumption for unrequested scenario {a.scenario.value}")
            seen.add(key)
        for scenario in self.scenarios:
            if (K.COST_OF_EQUITY, scenario) in seen and any(
                (k, scenario) in seen for k in CAPM_KINDS
            ):
                raise ValueError(
                    f"{scenario.value}: an explicit COST_OF_EQUITY and CAPM inputs conflict"
                )
        multiples: set[tuple[ValuationMethod, Scenario, int]] = set()
        for m in self.multiples:
            key_m = (m.method, m.scenario, m.fiscal_year)
            if key_m in multiples:
                raise ValueError(
                    f"duplicate multiple {m.method.value}/{m.scenario.value}/FY{m.fiscal_year}"
                )
            if m.scenario not in self.scenarios:
                raise ValueError(f"multiple for unrequested scenario {m.scenario.value}")
            if m.method not in self.methods and (
                "methods" in self.model_fields_set or m.method not in LENDER_METHODS
            ):
                raise ValueError(f"multiple for unrequested method {m.method.value}")
            multiples.add(key_m)
        if any(a.kind == K.TERMINAL_EV_EBITDA for a in self.assumptions) and (
            ValuationMethod.DCF_FCFF_EXIT_MULTIPLE not in self.methods
        ):
            raise ValueError("TERMINAL_EV_EBITDA given but DCF_FCFF_EXIT_MULTIPLE not requested")
        return self


# --- result -------------------------------------------------------------------------------------


class ValuationAssumption(BaseModel):
    """An explicit input as used: never derived, never defaulted."""

    model_config = _model()
    assumption_id: UUID
    kind: AssumptionKind
    scenario: Scenario | None
    value: ExactDecimal
    unit: AssumptionUnit
    currency: Currency | None = None
    scale: Scale | None = None
    source: Literal["EXPLICIT"] = "EXPLICIT"
    reference: Label | None = None
    evidence_ids: tuple[UUID, ...] = ()
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def bounded(self) -> Self:
        problem = assumption_bounds_error(self.kind, self.value)
        if problem:
            raise ValueError(problem)
        return self


class InputKind(StrEnum):
    BOX10_FORECAST = "BOX10_FORECAST"  # a ForecastValue (kind FORECAST) of the same scenario
    BOX10_ACTUAL = "BOX10_ACTUAL"  # a ForecastValue actual anchor (scenario-free)
    BOX10_ASSUMPTION = "BOX10_ASSUMPTION"  # a ForecastAssumption of the same scenario
    BOX4_VALUE = "BOX4_VALUE"  # a Box 4 canonical value (lineage in box4_inputs)
    ASSUMPTION = "ASSUMPTION"  # a ValuationAssumption
    MULTIPLE = "MULTIPLE"  # a MultipleInput (as recorded in multiples)
    VALUATION = "VALUATION"  # another ValuationValue
    PERIOD_INDEX = "PERIOD_INDEX"  # a discount period t (years after the valuation date)


class FormulaInput(BaseModel):
    model_config = _model()
    role: Code  # e.g. CASH_FLOW, DISCOUNT_FACTOR
    kind: InputKind
    ref_id: UUID | None  # None only for PERIOD_INDEX
    value: ExactDecimal

    @model_validator(mode="after")
    def referenced(self) -> Self:
        if (self.ref_id is None) != (self.kind == InputKind.PERIOD_INDEX):
            raise ValueError("every input except a period index references its source")
        return self


class ValueUnit(StrEnum):
    MONEY = "MONEY"  # crore
    PER_SHARE = "PER_SHARE"  # ones per share
    PERCENT = "PERCENT"
    FACTOR = "FACTOR"  # a discount factor
    COUNT = "COUNT"  # shares, ones


class ValuationValue(BaseModel):
    model_config = _model()
    value_id: UUID
    name: Code  # e.g. FCFF, DISCOUNT_FACTOR, ENTERPRISE_VALUE
    scenario: Scenario | None  # None: a scenario-free fact at the valuation date
    method: ValuationMethod | None  # None: shared by the scenario's methods (WACC, FCFF)
    fiscal_year: int | None = None  # forecast year of a per-year value
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    value: ExactDecimal | None
    unit: ValueUnit
    currency: Currency | None  # MONEY / PER_SHARE only
    formula_id: Label | None = None
    inputs: tuple[FormulaInput, ...] = ()
    box4_inputs: tuple[MetricInput, ...] = ()  # lineage of BOX4_VALUE inputs
    reason: Code | None = None  # why UNAVAILABLE
    missing_inputs: tuple[Label, ...] = ()
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def derivable(self) -> Self:
        available = self.status == "AVAILABLE"
        if available != (self.value is not None) or available == (self.reason is not None):
            raise ValueError("a value exists exactly when AVAILABLE; otherwise a reason")
        if available and not self.formula_id:
            raise ValueError("a valuation number has a registered formula")
        if (self.currency is not None) != (self.unit in (ValueUnit.MONEY, ValueUnit.PER_SHARE)):
            raise ValueError("money and per-share values (only) state their currency")
        return self


class ValuationFormula(BaseModel):
    model_config = _model()
    formula_id: Label
    expression: Label
    inputs: tuple[Code, ...]
    variadic: Code | None = None  # a role that repeats (e.g. one PV per forecast year)
    unit: ValueUnit
    notes: Label = ""


class DcfPeriod(BaseModel):
    """One discounted forecast year (end-of-period convention)."""

    model_config = _model()
    fiscal_year: int
    period_index: int  # t: years after the valuation date
    cash_flow: ExactDecimal | None
    discount_factor: ExactDecimal | None
    present_value: ExactDecimal | None
    cash_flow_id: UUID
    discount_factor_id: UUID
    present_value_id: UUID


class MultipleDetail(BaseModel):
    model_config = _model()
    metric: Code  # PAT_ATTRIBUTABLE, OPERATING_EBITDA, OPERATING_EBIT
    fiscal_year: int
    multiple: ExactDecimal
    basis: MultipleBasis
    source: MultipleSource
    reference: Label | None
    evidence_ids: tuple[UUID, ...]
    metric_value: ExactDecimal | None
    metric_value_id: UUID | None


class MethodResult(BaseModel):
    model_config = _model()
    scenario: Scenario
    method: ValuationMethod
    status: ValuationStatus
    reason: Code | None = None  # why not COMPLETE
    missing_inputs: tuple[Label, ...] = ()
    currency: Currency
    enterprise_value: ExactDecimal | None = None
    equity_value: ExactDecimal | None = None
    fair_value_per_share: ExactDecimal | None = None
    fair_value_vs_price_percent: ExactDecimal | None = None  # vs a stated MARKET_PRICE only
    #: DCF only
    wacc: ExactDecimal | None = None
    terminal_growth: ExactDecimal | None = None
    schedule: tuple[DcfPeriod, ...] = ()
    terminal_value: ExactDecimal | None = None
    present_value_of_terminal_value: ExactDecimal | None = None
    sum_present_value_forecast: ExactDecimal | None = None
    #: multiples only
    multiple: MultipleDetail | None = None
    value_ids: tuple[UUID, ...]  # every valuation value this result is built from

    @model_validator(mode="after")
    def consistent(self) -> Self:
        complete = self.status == ValuationStatus.COMPLETE
        if complete != (self.fair_value_per_share is not None) or complete == (
            self.reason is not None
        ):
            raise ValueError("COMPLETE exactly when a fair value per share exists; else a reason")
        if self.status == ValuationStatus.PARTIAL and (
            self.enterprise_value is None and self.equity_value is None
        ):
            raise ValueError("a PARTIAL result has an enterprise or an equity value")
        return self


class ValuationIssue(BaseModel):
    model_config = _model()
    code: Code
    severity: Severity
    scenario: Scenario | None = None
    method: ValuationMethod | None = None
    fiscal_year: int | None = None
    subject: Label | None = None
    detail: Label | None = None


class ForecastSource(BaseModel):
    model_config = _model()
    schema_version: Label
    engine_version: Label
    forecast_id: UUID
    canonical_sha256: Sha256Hex  # of canonical_forecast_json (generated_at excluded)
    status: ForecastStatus
    box4_dataset_id: UUID
    box4_canonical_sha256: Sha256Hex
    box4_supplied: bool  # whether Box 4 was given to read the cash balance


class ValuationManifest(BaseModel):
    model_config = _model()
    engine_version: Label
    formula_version: Label
    policy_version: Label
    formulas: tuple[ValuationFormula, ...]
    policies: tuple[tuple[Code, Label], ...]


class ValuationDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["VALUATION_DATASET_V1"] = VALUATION_SCHEMA
    engine_version: Label = VALUATION_ENGINE_VERSION
    valuation_id: UUID
    company_key: Label
    company_name: Label
    industry: Industry
    generated_at: AwareDatetime  # injected; never part of any ID or value
    source_forecast: ForecastSource
    request: ValuationRequest
    #: COMPLETE: every requested result COMPLETE; UNAVAILABLE: none even PARTIAL
    status: ValuationStatus
    reporting_basis: ReportingBasis
    currency: Currency
    valuation_date: FinancialPeriod | None  # the latest actual balance-sheet date
    forecast_years: tuple[FinancialPeriod, ...]  # the years discounted / priced
    scenarios: tuple[Scenario, ...]
    assumptions: tuple[ValuationAssumption, ...]
    multiples: tuple[MultipleInput, ...]
    multiple_ids: tuple[UUID, ...]  # one per multiple, in the same order
    values: tuple[ValuationValue, ...]
    results: tuple[MethodResult, ...]
    issues: tuple[ValuationIssue, ...]
    manifest: ValuationManifest

    @model_validator(mode="after")
    def closed(self) -> Self:
        if len(self.multiple_ids) != len(self.multiples):
            raise ValueError("one ID per multiple")
        value_ids = [v.value_id for v in self.values]
        assumption_ids = [a.assumption_id for a in self.assumptions]
        everything = value_ids + assumption_ids + list(self.multiple_ids)
        if len(everything) != len(set(everything)):
            raise ValueError("valuation IDs must be unique")
        values = {v.value_id: v for v in self.values}
        assumptions = {a.assumption_id: a for a in self.assumptions}
        multiples = dict(zip(self.multiple_ids, self.multiples, strict=True))
        formulas = {f.formula_id: f for f in self.manifest.formulas}
        for v in self.values:
            if v.scenario is not None and v.scenario not in self.scenarios:
                raise ValueError("value for a scenario that was not valued")
            if v.currency is not None and v.currency != self.currency:
                raise ValueError("one valuation currency: never mixed")
            if v.status == "UNAVAILABLE":
                continue
            formula = formulas.get(v.formula_id or "")
            if formula is None:
                raise ValueError(f"formula {v.formula_id} is not in the manifest")
            roles = [i.role for i in v.inputs]
            fixed = [r for r in roles if r != formula.variadic]
            if sorted(fixed) != sorted(formula.inputs) or (
                formula.variadic is not None and formula.variadic not in roles
            ):
                raise ValueError(f"{v.name} does not match {formula.formula_id} inputs")
            if formula.unit != v.unit:
                raise ValueError(f"{v.name} unit differs from its formula")
            box4 = {m.canonical_value_id: m.value for m in v.box4_inputs}
            for i in v.inputs:
                if i.kind == InputKind.VALUATION:
                    source = values.get(i.ref_id)  # type: ignore[arg-type]
                    if source is None or source.value != i.value:
                        raise ValueError(f"{v.name} names an unknown valuation input")
                    if source.scenario not in (None, v.scenario):
                        raise ValueError("a valuation uses its own scenario's values")
                elif i.kind == InputKind.ASSUMPTION:
                    a = assumptions.get(i.ref_id)  # type: ignore[arg-type]
                    if a is None or a.value != i.value or a.scenario not in (None, v.scenario):
                        raise ValueError("a valuation uses its own scenario's assumption")
                elif i.kind == InputKind.MULTIPLE:
                    m = multiples.get(i.ref_id)  # type: ignore[arg-type]
                    if m is None or m.multiple != i.value or m.scenario != v.scenario:
                        raise ValueError("a valuation uses its own scenario's multiple")
                elif i.kind == InputKind.BOX4_VALUE and box4.get(i.ref_id) != i.value:  # type: ignore[arg-type]
                    raise ValueError("a Box 4 input carries its lineage")
        for r in self.results:
            if r.scenario not in self.scenarios or r.currency != self.currency:
                raise ValueError("result outside the valued scenarios or currency")
            for ref in r.value_ids:
                if ref not in values or values[ref].scenario not in (None, r.scenario):
                    raise ValueError("a result is built from its own scenario's values")
        return self
