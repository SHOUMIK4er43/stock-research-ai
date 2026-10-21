"""DETAILED_RESEARCH_V1 Box 10: deterministic financial forecasting (no valuation).

Three kinds of number, never blurred:
  ACTUAL      a Box 4 canonical value anchoring the forecast (ForecastValue kind=ACTUAL,
              with its whole Box 4 lineage as a Box 5 MetricInput)
  ASSUMPTION  a ForecastAssumption: one driver for one scenario and one forecast year, with
              its source (historical / explicit / management guidance / industry / fallback),
              derivation method, the historical periods or evidence behind it
  FORECAST    a ForecastValue kind=FORECAST: one registered formula applied to named inputs,
              each input a reference to an ACTUAL, an ASSUMPTION or another FORECAST value

A forecast value that cannot be supported is UNAVAILABLE with a reason, never zero. Box 10
produces financial estimates only: no enterprise or equity value, no discounting, no target
multiple, no target price and no recommendation.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_analytics import MetricInput
from stock_research_ai.domain.financial_canonical import (
    Confidence,
    Currency,
    FinancialPeriod,
    Industry,
    ReportingBasis,
    Scale,
)

FORECAST_SCHEMA: Literal["FORECAST_DATASET_V1"] = "FORECAST_DATASET_V1"
FORECAST_ENGINE_VERSION = "forecasting/1"
FORECAST_FORMULA_VERSION = "forecast-formulas/1"
FORECAST_POLICY_VERSION = "forecast-policy/2"
DEFAULT_HORIZON = 3
MAX_HORIZON = 5
DEFAULT_HISTORY_YEARS = 5
MAX_HISTORY_YEARS = 10
MAX_EXPLICIT_ASSUMPTIONS = 2_000
MAX_ISSUES = 2_000
#: Arithmetic guards. Not economic caps: they only reject numbers no real company produces
#: and that would make Decimal arithmetic unbounded.
MAX_PERCENT = Decimal(10) ** 6
MAX_MONEY = Decimal(10) ** 15  # crore
MAX_SHARE_COUNT = Decimal(10) ** 15
MAX_ABS_FORECAST = Decimal(10) ** 18  # crore / per share: above this a series stops
#: Every derived assumption is quantized to 6 decimal places, ROUND_HALF_EVEN (as Box 5);
#: the stored, quantized value is exactly the value the formulas use.
ASSUMPTION_QUANTUM = Decimal("0.000001")
#: crore -> ones: EPS = attributable PAT (crore) x 10^7 / shares (ones)
CRORE = Decimal(10_000_000)

Label = Annotated[str, StringConstraints(max_length=1000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FiscalYear = Annotated[int, Field(ge=1990, le=2100)]


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


class Scenario(StrEnum):
    BASE = "BASE"
    BULL = "BULL"
    BEAR = "BEAR"


class AssumptionUnit(StrEnum):
    PERCENT = "PERCENT"  # 12.5 means 12.5 % (the repository convention), never 0.125
    MONEY = "MONEY"  # Box 4 canonical scale (crore) in the forecast currency
    COUNT = "COUNT"  # absolute count (ones), e.g. shares


class Driver(StrEnum):
    REVENUE_GROWTH = "REVENUE_GROWTH"  # % year on year
    OPERATING_MARGIN = "OPERATING_MARGIN"  # Screener operating profit / revenue %
    EBITDA_MARGIN = "EBITDA_MARGIN"  # reported EBITDA / revenue % (no operating profit)
    OTHER_INCOME_TO_REVENUE = "OTHER_INCOME_TO_REVENUE"
    DA_TO_REVENUE = "DA_TO_REVENUE"
    INTEREST_RATE_ON_DEBT = "INTEREST_RATE_ON_DEBT"  # finance cost / opening gross debt %
    FINANCE_COST_TO_REVENUE = "FINANCE_COST_TO_REVENUE"
    TAX_RATE = "TAX_RATE"  # tax / PBT %
    MINORITY_SHARE_OF_PAT = "MINORITY_SHARE_OF_PAT"
    CAPEX_TO_REVENUE = "CAPEX_TO_REVENUE"
    CAPEX_AMOUNT = "CAPEX_AMOUNT"  # absolute capex; takes precedence over the ratio
    WORKING_CAPITAL_TO_REVENUE = "WORKING_CAPITAL_TO_REVENUE"  # trade working capital %
    DEBT_CHANGE = "DEBT_CHANGE"  # gross debt movement in the year
    SHARE_COUNT = "SHARE_COUNT"  # weighted shares for EPS (explicit only)
    # NON_BANK_FINANCIAL (lender) model: balance-sheet driven, Box 4 canonical facts only
    ASSET_GROWTH = "ASSET_GROWTH"  # total assets % year on year
    ROA = "ROA"  # PAT / average total assets %
    EQUITY_CHANGE_TO_PAT = "EQUITY_CHANGE_TO_PAT"  # (ordinary equity - prior) / PAT %
    #: borrowings / (assets - ordinary equity) %
    BORROWINGS_SHARE_OF_FUNDING = "BORROWINGS_SHARE_OF_FUNDING"
    COST_OF_BORROWINGS = "COST_OF_BORROWINGS"  # finance cost / average borrowings % (a proxy)
    TOTAL_INCOME_TO_AVERAGE_ASSETS = "TOTAL_INCOME_TO_AVERAGE_ASSETS"  # total income / avg assets %


class DriverSpec(BaseModel):
    model_config = _model()
    unit: AssumptionUnit
    minimum: Decimal
    maximum: Decimal
    minimum_exclusive: bool = False


_P, _M, _C = AssumptionUnit.PERCENT, AssumptionUnit.MONEY, AssumptionUnit.COUNT
DRIVER_SPECS: dict[Driver, DriverSpec] = {
    # growth <= -100 % would make revenue zero or negative
    Driver.REVENUE_GROWTH: DriverSpec(
        unit=_P, minimum=Decimal(-100), maximum=MAX_PERCENT, minimum_exclusive=True
    ),
    Driver.OPERATING_MARGIN: DriverSpec(unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT),
    Driver.EBITDA_MARGIN: DriverSpec(unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT),
    Driver.OTHER_INCOME_TO_REVENUE: DriverSpec(unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT),
    Driver.DA_TO_REVENUE: DriverSpec(unit=_P, minimum=Decimal(0), maximum=MAX_PERCENT),
    Driver.INTEREST_RATE_ON_DEBT: DriverSpec(unit=_P, minimum=Decimal(0), maximum=MAX_PERCENT),
    Driver.FINANCE_COST_TO_REVENUE: DriverSpec(unit=_P, minimum=Decimal(0), maximum=MAX_PERCENT),
    Driver.TAX_RATE: DriverSpec(unit=_P, minimum=Decimal(0), maximum=Decimal(100)),
    Driver.MINORITY_SHARE_OF_PAT: DriverSpec(unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT),
    Driver.CAPEX_TO_REVENUE: DriverSpec(unit=_P, minimum=Decimal(0), maximum=MAX_PERCENT),
    Driver.CAPEX_AMOUNT: DriverSpec(unit=_M, minimum=Decimal(0), maximum=MAX_MONEY),
    Driver.WORKING_CAPITAL_TO_REVENUE: DriverSpec(
        unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT
    ),
    Driver.DEBT_CHANGE: DriverSpec(unit=_M, minimum=-MAX_MONEY, maximum=MAX_MONEY),
    Driver.SHARE_COUNT: DriverSpec(
        unit=_C, minimum=Decimal(0), maximum=MAX_SHARE_COUNT, minimum_exclusive=True
    ),
    Driver.ASSET_GROWTH: DriverSpec(
        unit=_P, minimum=Decimal(-100), maximum=MAX_PERCENT, minimum_exclusive=True
    ),
    Driver.ROA: DriverSpec(unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT),
    Driver.EQUITY_CHANGE_TO_PAT: DriverSpec(unit=_P, minimum=-MAX_PERCENT, maximum=MAX_PERCENT),
    Driver.BORROWINGS_SHARE_OF_FUNDING: DriverSpec(
        unit=_P, minimum=Decimal(0), maximum=Decimal(100)
    ),
    Driver.COST_OF_BORROWINGS: DriverSpec(unit=_P, minimum=Decimal(0), maximum=MAX_PERCENT),
    Driver.TOTAL_INCOME_TO_AVERAGE_ASSETS: DriverSpec(
        unit=_P, minimum=Decimal(0), maximum=MAX_PERCENT
    ),
}


def driver_bounds_error(driver: Driver, value: Decimal) -> str | None:
    spec = DRIVER_SPECS[driver]
    low_ok = value > spec.minimum if spec.minimum_exclusive else value >= spec.minimum
    if not low_ok or value > spec.maximum:
        sign = ">" if spec.minimum_exclusive else ">="
        return f"{driver.value} must be {sign} {spec.minimum} and <= {spec.maximum}"
    return None


class AssumptionSource(StrEnum):
    HISTORICAL = "HISTORICAL"  # derived from Box 4 actuals by a registered method
    EXPLICIT = "EXPLICIT"  # supplied by the caller (analyst / user)
    MANAGEMENT_GUIDANCE = "MANAGEMENT_GUIDANCE"  # Box 8 numeric guidance, deterministic policy
    INDUSTRY_INTELLIGENCE = "INDUSTRY_INTELLIGENCE"  # caller-supplied, citing Box 7 / Box 9
    FALLBACK = "FALLBACK"  # a documented policy constant: NOT evidence


class DerivationMethod(StrEnum):
    HISTORICAL_CAGR = "HISTORICAL_CAGR"
    HISTORICAL_SINGLE_YOY = "HISTORICAL_SINGLE_YOY"  # only two contiguous years exist
    HISTORICAL_MEDIAN = "HISTORICAL_MEDIAN"
    HISTORICAL_MAX_YOY = "HISTORICAL_MAX_YOY"
    HISTORICAL_MIN_YOY = "HISTORICAL_MIN_YOY"
    HISTORICAL_MAX = "HISTORICAL_MAX"
    HISTORICAL_MIN = "HISTORICAL_MIN"
    GUIDANCE_MIDPOINT = "GUIDANCE_MIDPOINT"
    GUIDANCE_LOW = "GUIDANCE_LOW"
    GUIDANCE_HIGH = "GUIDANCE_HIGH"
    GUIDANCE_POINT = "GUIDANCE_POINT"
    EXPLICIT_VALUE = "EXPLICIT_VALUE"
    FALLBACK_CONSTANT = "FALLBACK_CONSTANT"


class HistoryCompleteness(StrEnum):
    FULL = "FULL"  # the whole requested window was usable
    SPARSE = "SPARSE"  # fewer usable years than the window
    SINGLE_POINT = "SINGLE_POINT"  # one observation: no dispersion
    NOT_APPLICABLE = "NOT_APPLICABLE"  # explicit / guidance / fallback


class ForecastLine(StrEnum):
    REVENUE = "REVENUE"
    OPERATING_PROFIT = "OPERATING_PROFIT"
    OTHER_INCOME = "OTHER_INCOME"
    EBITDA = "EBITDA"
    DEPRECIATION_AMORTIZATION = "DEPRECIATION_AMORTIZATION"
    EBIT = "EBIT"
    FINANCE_COST = "FINANCE_COST"
    PBT = "PBT"
    TAX = "TAX"
    PAT = "PAT"
    MINORITY_INTEREST = "MINORITY_INTEREST"
    PAT_ATTRIBUTABLE = "PAT_ATTRIBUTABLE"
    EPS = "EPS"
    CAPEX = "CAPEX"
    TRADE_WORKING_CAPITAL = "TRADE_WORKING_CAPITAL"
    WORKING_CAPITAL_CHANGE = "WORKING_CAPITAL_CHANGE"
    GROSS_DEBT = "GROSS_DEBT"
    CFO_ESTIMATE = "CFO_ESTIMATE"
    FCF_ESTIMATE = "FCF_ESTIMATE"
    # NON_BANK_FINANCIAL (lender) model lines
    TOTAL_ASSETS = "TOTAL_ASSETS"
    #: ORDINARY shareholders' book equity (Box 5 COMMON_EQUITY): never hybrid instruments
    COMMON_EQUITY = "COMMON_EQUITY"
    #: total assets - ordinary equity (borrowings + other liabilities + hybrid equity)
    NON_EQUITY_FUNDING = "NON_EQUITY_FUNDING"
    TOTAL_BORROWINGS = "TOTAL_BORROWINGS"
    TOTAL_INCOME = "TOTAL_INCOME"


class ForecastModel(StrEnum):
    """The forecast model Box 10 applies, chosen from Box 4's ONE business classification."""

    NON_FINANCIAL = "NON_FINANCIAL"  # industrial P&L route (Box 4 industry GENERAL)
    BANK = "BANK"  # revenue only (no bank model exists; industrial drivers do not apply)
    NON_BANK_FINANCIAL = "NON_BANK_FINANCIAL"  # balance-sheet driven lender model
    UNKNOWN = "UNKNOWN"  # business class unresolved: revenue only, no sector-specific line


class OperatingRoute(StrEnum):
    OPERATING_PROFIT = "OPERATING_PROFIT"  # margin on Screener operating profit + other income
    REPORTED_EBITDA = "REPORTED_EBITDA"  # margin on EBITDA as reported
    UNAVAILABLE = "UNAVAILABLE"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"  # a line, a scenario or the whole forecast could not be produced


class ForecastStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


# --- request ------------------------------------------------------------------------------------


class AssumptionInput(BaseModel):
    """One caller-supplied assumption. INDUSTRY_INTELLIGENCE must cite its evidence."""

    model_config = _model()
    driver: Driver
    scenario: Scenario
    fiscal_year: FiscalYear
    value: ExactDecimal
    unit: AssumptionUnit
    source: Literal[AssumptionSource.EXPLICIT, AssumptionSource.INDUSTRY_INTELLIGENCE] = (
        AssumptionSource.EXPLICIT
    )
    currency: Currency | None = None  # MONEY only: must equal the forecast currency
    scale: Scale | None = None  # MONEY: CRORES; COUNT: ONES - stated, never guessed
    reference: Label | None = None
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        spec = DRIVER_SPECS[self.driver]
        if self.unit != spec.unit:
            raise ValueError(f"{self.driver.value} is expressed in {spec.unit.value}")
        problem = driver_bounds_error(self.driver, self.value)
        if problem:
            raise ValueError(problem)
        if self.unit == AssumptionUnit.MONEY:
            if self.currency in (None, Currency.UNKNOWN, Currency.OTHER):
                raise ValueError("a MONEY assumption states its currency")
            if self.scale != Scale.CRORES:
                raise ValueError("a MONEY assumption is stated in CRORES (Box 4 canonical scale)")
        elif self.unit == AssumptionUnit.COUNT:
            if self.scale != Scale.ONES or self.currency is not None:
                raise ValueError("a COUNT assumption is an absolute count (scale ONES)")
        elif self.currency is not None or self.scale is not None:
            raise ValueError("a PERCENT assumption carries no currency or scale")
        if self.source == AssumptionSource.INDUSTRY_INTELLIGENCE and not (
            self.evidence_ids or self.reference
        ):
            raise ValueError("an industry-intelligence assumption cites its Box 7 / Box 9 evidence")
        return self


class ForecastRequest(BaseModel):
    model_config = _model()
    horizon: int = Field(default=DEFAULT_HORIZON, ge=1, le=MAX_HORIZON)
    scenarios: tuple[Scenario, ...] = (Scenario.BASE, Scenario.BULL, Scenario.BEAR)
    reporting_basis: Literal[ReportingBasis.CONSOLIDATED, ReportingBasis.STANDALONE] | None = None
    history_years: int = Field(default=DEFAULT_HISTORY_YEARS, ge=2, le=MAX_HISTORY_YEARS)
    explicit_assumptions: tuple[AssumptionInput, ...] = Field(
        default=(), max_length=MAX_EXPLICIT_ASSUMPTIONS
    )

    @model_validator(mode="after")
    def unique(self) -> Self:
        if not self.scenarios or len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("scenarios must be a non-empty set")
        seen: set[tuple[Driver, Scenario, int]] = set()
        for a in self.explicit_assumptions:
            key = (a.driver, a.scenario, a.fiscal_year)
            if key in seen:
                raise ValueError(
                    f"duplicate assumption {a.driver.value}/{a.scenario.value}/FY{a.fiscal_year}"
                )
            if a.scenario not in self.scenarios:
                raise ValueError(f"assumption for unrequested scenario {a.scenario.value}")
            seen.add(key)
        return self


# --- result -------------------------------------------------------------------------------------


class GuidanceReference(BaseModel):
    """The Box 8 guidance an assumption follows (numbers exactly as Box 8 parsed them)."""

    model_config = _model()
    management_dataset_id: UUID | None
    guidance_id: UUID
    event_id: UUID
    statement_ids: tuple[UUID, ...]
    subject_code: Code
    target_type: Code
    low: ExactDecimal | None
    high: ExactDecimal | None
    value: ExactDecimal | None
    raw_target_text: Label
    policy: Label  # the deterministic mapping applied


class ForecastAssumption(BaseModel):
    model_config = _model()
    assumption_id: UUID
    driver: Driver
    scenario: Scenario
    fiscal_year: FiscalYear
    value: ExactDecimal
    unit: AssumptionUnit
    currency: Currency | None = None
    source: AssumptionSource
    method: DerivationMethod
    historical_periods: tuple[Label, ...] = ()  # Box 4 period keys used, oldest first
    canonical_value_ids: tuple[UUID, ...] = ()  # the Box 4 values behind a derivation
    #: (period key, observed value): a ratio in %, or a revenue endpoint for a CAGR
    observations: tuple[tuple[Label, ExactDecimal], ...] = ()
    guidance: GuidanceReference | None = None
    reference: Label | None = None  # explicit / industry: the caller's citation
    evidence_ids: tuple[UUID, ...] = ()
    completeness: HistoryCompleteness
    confidence: Confidence
    overrides: tuple[Label, ...] = ()  # the derived defaults this assumption replaced
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def traceable(self) -> Self:
        if self.source == AssumptionSource.HISTORICAL and not (
            self.historical_periods and self.canonical_value_ids and self.observations
        ):
            raise ValueError("a historical assumption names its periods and Box 4 values")
        if self.source == AssumptionSource.MANAGEMENT_GUIDANCE and self.guidance is None:
            raise ValueError("a guidance assumption references its Box 8 guidance")
        if self.source == AssumptionSource.FALLBACK and "FALLBACK_ASSUMPTION" not in self.warnings:
            raise ValueError("a fallback assumption is always flagged")
        if (self.source == AssumptionSource.FALLBACK) != (
            self.method == DerivationMethod.FALLBACK_CONSTANT
        ):
            raise ValueError("only fallbacks use the fallback method")
        # whatever its source, no assumption outside its driver's bounds reaches a formula
        problem = driver_bounds_error(self.driver, self.value)
        if problem:
            raise ValueError(problem)
        return self


class FormulaInput(BaseModel):
    model_config = _model()
    role: Code  # e.g. PRIOR_REVENUE, GROWTH
    kind: Literal["ACTUAL", "FORECAST", "ASSUMPTION"]
    ref_id: UUID  # a ForecastValue.value_id or a ForecastAssumption.assumption_id
    value: ExactDecimal


class ForecastValue(BaseModel):
    model_config = _model()
    value_id: UUID
    line: ForecastLine
    scenario: Scenario | None  # None: an ACTUAL anchor shared by every scenario
    kind: Literal["ACTUAL", "FORECAST"]
    period: FinancialPeriod
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    value: ExactDecimal | None
    unit: Literal["MONEY", "PER_SHARE"]
    currency: Currency
    scale: Scale  # CRORES for MONEY, ONES per share
    formula_id: Label | None = None  # FORECAST only
    formula_text: Label | None = None
    inputs: tuple[FormulaInput, ...] = ()
    actual: MetricInput | None = None  # ACTUAL: the Box 4 canonical value and lineage
    actual_components: tuple[MetricInput, ...] = ()  # ACTUAL built from several values
    reason: Code | None = None  # why UNAVAILABLE
    missing_inputs: tuple[Label, ...] = ()
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def derivable(self) -> Self:
        available = self.status == "AVAILABLE"
        if available != (self.value is not None) or available == (self.reason is not None):
            raise ValueError("a value exists exactly when AVAILABLE; otherwise a reason")
        if self.kind == "ACTUAL":
            if self.scenario is not None or (
                available and self.actual is None and not self.actual_components
            ):
                raise ValueError("an actual anchor is scenario-free and carries its Box 4 value")
        elif available and not (self.formula_id and self.inputs):
            raise ValueError("a forecast number has a formula and its inputs")
        elif self.scenario is None:
            raise ValueError("a forecast value belongs to one scenario")
        return self


class ForecastIssue(BaseModel):
    model_config = _model()
    code: Code
    severity: Severity
    scenario: Scenario | None = None
    fiscal_year: int | None = None
    subject: Label | None = None  # a line or driver
    detail: Label | None = None


class ForecastFormula(BaseModel):
    model_config = _model()
    formula_id: Label
    line: ForecastLine
    expression: Label
    inputs: tuple[Code, ...]
    notes: Label = ""


class ScenarioCompleteness(BaseModel):
    model_config = _model()
    scenario: Scenario
    status: ForecastStatus
    lines_available: tuple[ForecastLine, ...]
    lines_unavailable: tuple[ForecastLine, ...]
    sources: dict[str, int]  # assumptions by source
    fallback_assumptions: NonNegativeInt


class HistoricalBase(BaseModel):
    model_config = _model()
    reporting_basis: ReportingBasis
    basis_selection: Label
    currency: Currency
    scale: Scale
    fiscal_year_end_month: int | None
    latest_actual: FinancialPeriod | None
    annual_periods: tuple[Label, ...]  # contiguous 12-month fiscal years used, oldest first
    excluded_periods: tuple[tuple[Label, Code], ...]  # (period key, reason)
    ignored_period_types: dict[str, int]  # quarter / TTM / half-year values never used
    operating_route: OperatingRoute
    other_basis_available: bool  # informational: the other basis is never blended in
    #: None only in datasets written before the model was recorded
    forecast_model: ForecastModel | None = None


class Box4Source(BaseModel):
    model_config = _model()
    schema_version: Label
    dataset_id: UUID
    canonical_sha256: Sha256Hex
    status: Label


class ForecastManifest(BaseModel):
    model_config = _model()
    engine_version: Label
    formula_version: Label
    policy_version: Label
    formulas: tuple[ForecastFormula, ...]
    policies: tuple[tuple[Code, Label], ...]


class ForecastDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["FORECAST_DATASET_V1"] = FORECAST_SCHEMA
    engine_version: Label = FORECAST_ENGINE_VERSION
    forecast_id: UUID
    company_key: Label
    company_name: Label
    industry: Industry
    generated_at: AwareDatetime  # injected; never part of forecast_id or any value ID
    source_box4: Box4Source
    management_dataset_id: UUID | None
    request: ForecastRequest
    status: ForecastStatus
    base: HistoricalBase
    forecast_years: tuple[FinancialPeriod, ...]
    scenarios: tuple[Scenario, ...]
    actuals: tuple[ForecastValue, ...]
    assumptions: tuple[ForecastAssumption, ...]
    values: tuple[ForecastValue, ...]
    completeness: tuple[ScenarioCompleteness, ...]
    issues: tuple[ForecastIssue, ...]
    manifest: ForecastManifest

    @model_validator(mode="after")
    def closed(self) -> Self:
        actual_ids = [v.value_id for v in self.actuals]
        value_ids = [v.value_id for v in self.values]
        assumption_ids = [a.assumption_id for a in self.assumptions]
        everything = actual_ids + value_ids + assumption_ids
        if len(everything) != len(set(everything)):
            raise ValueError("forecast IDs must be unique")
        known = {
            "ACTUAL": set(actual_ids),
            "FORECAST": set(value_ids),
            "ASSUMPTION": set(assumption_ids),
        }
        assumptions = {a.assumption_id: a for a in self.assumptions}
        values = {v.value_id: v for v in (*self.actuals, *self.values)}
        formulas = {f.formula_id for f in self.manifest.formulas}
        for v in self.values:
            if v.kind != "FORECAST":
                raise ValueError("values holds forecasts only")
            if v.formula_id is not None and v.formula_id not in formulas:
                raise ValueError(f"formula {v.formula_id} is not in the manifest")
            for i in v.inputs:
                if i.ref_id not in known[i.kind]:
                    raise ValueError(f"{v.line.value} names an unknown {i.kind} input")
                if i.kind == "ASSUMPTION":
                    a = assumptions[i.ref_id]
                    if a.scenario != v.scenario or a.value != i.value:
                        raise ValueError("a forecast uses its own scenario's assumption")
                else:
                    source = values[i.ref_id]
                    if source.value != i.value or source.scenario not in (None, v.scenario):
                        raise ValueError("a forecast input is another value of its scenario")
        if any(v.kind != "ACTUAL" for v in self.actuals):
            raise ValueError("actuals holds actual anchors only")
        if any(a.scenario not in self.scenarios for a in self.assumptions):
            raise ValueError("assumption for a scenario that was not forecast")
        years = [p.fiscal_year for p in self.forecast_years]
        if any(y is None for y in years) or any(
            b is None or a is None or b != a + 1 for a, b in zip(years, years[1:], strict=False)
        ):
            raise ValueError("forecast fiscal years are consecutive and increasing")
        return self

    def series(self, line: ForecastLine, scenario: Scenario) -> tuple[ForecastValue, ...]:
        return tuple(v for v in self.values if v.line == line and v.scenario == scenario)

    def value(self, line: ForecastLine, scenario: Scenario, fiscal_year: int) -> ForecastValue:
        return next(
            v
            for v in self.values
            if v.line == line and v.scenario == scenario and v.period.fiscal_year == fiscal_year
        )

    def assumption(
        self, driver: Driver, scenario: Scenario, fiscal_year: int
    ) -> ForecastAssumption | None:
        return next(
            (
                a
                for a in self.assumptions
                if a.driver == driver and a.scenario == scenario and a.fiscal_year == fiscal_year
            ),
            None,
        )
