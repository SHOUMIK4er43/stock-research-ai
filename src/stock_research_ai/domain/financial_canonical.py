"""DETAILED_RESEARCH_V1 Box 4: source observations, reconciliation and canonical statements.

Layers: RAW (Box 1B / Box 3, untouched) -> OBSERVATIONS (each reported number with its
raw text, parsed value, unit, period, basis and exact provenance) -> RECONCILIATION
(explicit outcome per comparable group) -> CANONICAL (one value per metric/period/basis,
always pointing at the observation it came from). Nothing is derived or "fixed":
unmapped rows survive as UnmappedFinancialLine, ambiguity stays ambiguous.
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
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.evidence import SourceAuthority
from stock_research_ai.domain.financial import ExactDecimal

CANONICAL_FINANCIAL_SCHEMA = "CANONICAL_FINANCIAL_DATASET_V1"
TAXONOMY_VERSION = "taxonomy/1"

Label = Annotated[str, StringConstraints(max_length=500)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class Scale(StrEnum):
    ONES = "ONES"
    THOUSANDS = "THOUSANDS"
    LAKHS = "LAKHS"
    MILLIONS = "MILLIONS"
    CRORES = "CRORES"
    BILLIONS = "BILLIONS"
    UNKNOWN = "UNKNOWN"


SCALE_FACTOR: dict[Scale, Decimal] = {
    Scale.ONES: Decimal(1),
    Scale.THOUSANDS: Decimal(1_000),
    Scale.LAKHS: Decimal(100_000),
    Scale.MILLIONS: Decimal(1_000_000),
    Scale.CRORES: Decimal(10_000_000),
    Scale.BILLIONS: Decimal(1_000_000_000),
}
#: Canonical monetary scale: crore, the scale Screener reports in and most Indian
#: filings use. Values stay in their SOURCE currency (no FX conversion in Box 4).
CANONICAL_SCALE = Scale.CRORES


class Currency(StrEnum):
    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ValueKind(StrEnum):
    MONEY = "MONEY"  # normalized to CANONICAL_SCALE in source currency
    PER_SHARE = "PER_SHARE"  # e.g. EPS: currency per share, never rescaled
    PERCENT = "PERCENT"
    COUNT = "COUNT"
    UNKNOWN = "UNKNOWN"


class NumberStatus(StrEnum):
    NUMBER = "NUMBER"
    ZERO_REPORTED = "ZERO_REPORTED"  # an explicit 0 / 0.00
    NIL = "NIL"  # 'Nil' as reported - not silently zero
    DASH = "DASH"  # '-', '–', '—' as reported - not silently zero
    NOT_APPLICABLE = "NOT_APPLICABLE"  # 'NA', 'N.A.'
    BLANK = "BLANK"
    UNPARSEABLE = "UNPARSEABLE"


class ParsedNumber(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    raw: Label
    status: NumberStatus
    value: ExactDecimal | None = None
    is_percent: bool = False
    decimals: NonNegativeInt = 0  # displayed precision, drives rounding tolerance
    footnote_marker: bool = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        has_value = self.status in (NumberStatus.NUMBER, NumberStatus.ZERO_REPORTED)
        if has_value != (self.value is not None):
            raise ValueError("Only numbers carry a value")
        return self


class PeriodType(StrEnum):
    FY = "FY"
    QUARTER = "QUARTER"
    TTM = "TTM"
    HALF_YEAR = "HALF_YEAR"
    NINE_MONTH = "NINE_MONTH"
    POINT_IN_TIME = "POINT_IN_TIME"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class FinancialPeriod(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    period_type: PeriodType
    label: Label  # the source's own label, verbatim
    period_start: date | None = None
    period_end: date | None = None
    duration_months: PositiveInt | None = None
    fiscal_year: int | None = Field(default=None, ge=1990, le=2100)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)

    def key(self) -> str:
        return f"{self.period_type.value}|{self.period_end}|{self.duration_months}"


class ReportingBasis(StrEnum):
    CONSOLIDATED = "CONSOLIDATED"
    STANDALONE = "STANDALONE"
    UNKNOWN = "UNKNOWN"


class StatementType(StrEnum):
    PROFIT_AND_LOSS = "PROFIT_AND_LOSS"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"
    CHANGES_IN_EQUITY = "CHANGES_IN_EQUITY"
    QUARTERLY_RESULTS = "QUARTERLY_RESULTS"
    SEGMENT_RESULTS = "SEGMENT_RESULTS"


class Industry(StrEnum):
    GENERAL = "GENERAL"
    BANK = "BANK"
    NBFC = "NBFC"
    INSURANCE = "INSURANCE"


class CanonicalMetric(StrEnum):
    # Profit & loss
    REVENUE = "REVENUE"
    REVENUE_FROM_OPERATIONS = "REVENUE_FROM_OPERATIONS"
    OTHER_OPERATING_INCOME = "OTHER_OPERATING_INCOME"
    OTHER_INCOME = "OTHER_INCOME"
    TOTAL_INCOME = "TOTAL_INCOME"
    RAW_MATERIAL_COST = "RAW_MATERIAL_COST"
    PURCHASES_OF_STOCK_IN_TRADE = "PURCHASES_OF_STOCK_IN_TRADE"
    CHANGE_IN_INVENTORIES = "CHANGE_IN_INVENTORIES"
    POWER_AND_FUEL = "POWER_AND_FUEL"
    EMPLOYEE_COST = "EMPLOYEE_COST"
    SUBCONTRACTING_COST = "SUBCONTRACTING_COST"
    SOFTWARE_COST = "SOFTWARE_COST"
    SELLING_EXPENSE = "SELLING_EXPENSE"
    ADMINISTRATIVE_EXPENSE = "ADMINISTRATIVE_EXPENSE"
    OTHER_OPERATING_EXPENSE = "OTHER_OPERATING_EXPENSE"
    OTHER_EXPENSES = "OTHER_EXPENSES"  # 'Other expenses' as reported (not assumed operating)
    TOTAL_OPERATING_EXPENSE = "TOTAL_OPERATING_EXPENSE"  # Screener 'Expenses'
    TOTAL_EXPENSES = "TOTAL_EXPENSES"  # filing 'Total expenses' (incl. D&A, finance cost)
    OPERATING_PROFIT = "OPERATING_PROFIT"  # Screener 'Operating Profit' - NOT EBITDA
    EBITDA = "EBITDA"  # only when a source reports it under that name
    DEPRECIATION_AMORTIZATION = "DEPRECIATION_AMORTIZATION"
    EBIT = "EBIT"
    FINANCE_COST = "FINANCE_COST"
    PROFIT_BEFORE_EXCEPTIONAL_ITEMS_AND_TAX = "PROFIT_BEFORE_EXCEPTIONAL_ITEMS_AND_TAX"
    EXCEPTIONAL_ITEMS = "EXCEPTIONAL_ITEMS"
    SHARE_OF_ASSOCIATES_JV = "SHARE_OF_ASSOCIATES_JV"
    PBT = "PBT"
    CURRENT_TAX = "CURRENT_TAX"
    DEFERRED_TAX = "DEFERRED_TAX"
    TOTAL_TAX = "TOTAL_TAX"
    PAT = "PAT"
    MINORITY_INTEREST = "MINORITY_INTEREST"
    PAT_ATTRIBUTABLE_TO_OWNERS = "PAT_ATTRIBUTABLE_TO_OWNERS"
    OTHER_COMPREHENSIVE_INCOME = "OTHER_COMPREHENSIVE_INCOME"
    TOTAL_COMPREHENSIVE_INCOME = "TOTAL_COMPREHENSIVE_INCOME"
    BASIC_EPS = "BASIC_EPS"
    DILUTED_EPS = "DILUTED_EPS"
    EPS = "EPS"  # EPS whose basic/diluted nature the source does not state
    DPS = "DPS"
    # Balance sheet
    EQUITY_SHARE_CAPITAL = "EQUITY_SHARE_CAPITAL"
    OTHER_EQUITY = "OTHER_EQUITY"
    RESERVES = "RESERVES"
    TOTAL_EQUITY = "TOTAL_EQUITY"
    LONG_TERM_BORROWINGS = "LONG_TERM_BORROWINGS"
    SHORT_TERM_BORROWINGS = "SHORT_TERM_BORROWINGS"
    TOTAL_BORROWINGS = "TOTAL_BORROWINGS"
    LEASE_LIABILITIES = "LEASE_LIABILITIES"
    TOTAL_DEBT = "TOTAL_DEBT"
    TRADE_PAYABLES = "TRADE_PAYABLES"
    OTHER_FINANCIAL_LIABILITIES = "OTHER_FINANCIAL_LIABILITIES"
    PROVISIONS = "PROVISIONS"
    DEFERRED_TAX_LIABILITY = "DEFERRED_TAX_LIABILITY"
    OTHER_LIABILITIES = "OTHER_LIABILITIES"
    TOTAL_CURRENT_LIABILITIES = "TOTAL_CURRENT_LIABILITIES"
    TOTAL_NON_CURRENT_LIABILITIES = "TOTAL_NON_CURRENT_LIABILITIES"
    TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
    TOTAL_EQUITY_AND_LIABILITIES = "TOTAL_EQUITY_AND_LIABILITIES"
    PROPERTY_PLANT_EQUIPMENT = "PROPERTY_PLANT_EQUIPMENT"
    FIXED_ASSETS = "FIXED_ASSETS"  # Screener 'Fixed Assets' (its own aggregation)
    RIGHT_OF_USE_ASSETS = "RIGHT_OF_USE_ASSETS"
    CWIP = "CWIP"
    GOODWILL = "GOODWILL"
    INTANGIBLE_ASSETS = "INTANGIBLE_ASSETS"
    INVESTMENT_PROPERTY = "INVESTMENT_PROPERTY"
    NON_CURRENT_INVESTMENTS = "NON_CURRENT_INVESTMENTS"
    CURRENT_INVESTMENTS = "CURRENT_INVESTMENTS"
    TOTAL_INVESTMENTS = "TOTAL_INVESTMENTS"
    INVENTORY = "INVENTORY"
    TRADE_RECEIVABLES = "TRADE_RECEIVABLES"
    CASH_AND_CASH_EQUIVALENTS = "CASH_AND_CASH_EQUIVALENTS"
    BANK_BALANCES = "BANK_BALANCES"
    LOANS = "LOANS"
    OTHER_FINANCIAL_ASSETS = "OTHER_FINANCIAL_ASSETS"
    OTHER_CURRENT_ASSETS = "OTHER_CURRENT_ASSETS"
    OTHER_NON_CURRENT_ASSETS = "OTHER_NON_CURRENT_ASSETS"
    OTHER_ASSETS = "OTHER_ASSETS"  # Screener 'Other Assets'
    TOTAL_CURRENT_ASSETS = "TOTAL_CURRENT_ASSETS"
    TOTAL_NON_CURRENT_ASSETS = "TOTAL_NON_CURRENT_ASSETS"
    TOTAL_ASSETS = "TOTAL_ASSETS"
    # Cash flow
    CFO_BEFORE_WORKING_CAPITAL = "CFO_BEFORE_WORKING_CAPITAL"
    WORKING_CAPITAL_CHANGES = "WORKING_CAPITAL_CHANGES"
    TAX_PAID = "TAX_PAID"
    CFO = "CFO"
    CAPEX = "CAPEX"
    PURCHASE_OF_PPE = "PURCHASE_OF_PPE"
    SALE_OF_PPE = "SALE_OF_PPE"
    ACQUISITIONS = "ACQUISITIONS"
    PURCHASE_OF_INVESTMENTS = "PURCHASE_OF_INVESTMENTS"
    SALE_OF_INVESTMENTS = "SALE_OF_INVESTMENTS"
    INTEREST_RECEIVED = "INTEREST_RECEIVED"
    DIVIDENDS_RECEIVED = "DIVIDENDS_RECEIVED"
    CFI = "CFI"
    DEBT_RAISED = "DEBT_RAISED"
    DEBT_REPAID = "DEBT_REPAID"
    LEASE_PAYMENTS = "LEASE_PAYMENTS"
    DIVIDENDS_PAID = "DIVIDENDS_PAID"
    BUYBACK = "BUYBACK"
    EQUITY_ISSUANCE = "EQUITY_ISSUANCE"
    INTEREST_PAID = "INTEREST_PAID"
    CFF = "CFF"
    NET_CHANGE_IN_CASH = "NET_CHANGE_IN_CASH"
    FX_EFFECT_ON_CASH = "FX_EFFECT_ON_CASH"
    OPENING_CASH = "OPENING_CASH"
    CLOSING_CASH = "CLOSING_CASH"
    # Other (only when a source reports them directly)
    CAPITAL_EMPLOYED = "CAPITAL_EMPLOYED"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    NET_DEBT = "NET_DEBT"
    # Banking (initial, conservative)
    INTEREST_EARNED = "INTEREST_EARNED"
    INTEREST_EXPENDED = "INTEREST_EXPENDED"
    NET_INTEREST_INCOME = "NET_INTEREST_INCOME"
    BANK_FINANCING_PROFIT = "BANK_FINANCING_PROFIT"  # Screener 'Financing Profit'
    BANK_OPERATING_EXPENSES = "BANK_OPERATING_EXPENSES"
    PROVISIONS_AND_CONTINGENCIES = "PROVISIONS_AND_CONTINGENCIES"
    DEPOSITS = "DEPOSITS"  # never folded into borrowings
    ADVANCES = "ADVANCES"


class Confidence(StrEnum):
    HIGH = "HIGH"  # structured table, explicit period/unit/basis
    MEDIUM = "MEDIUM"  # one attribute from a deterministic nearby signal
    LOW = "LOW"  # ambiguous context: preserved, never canonical over HIGH/MEDIUM


class SourceType(StrEnum):
    SCREENER = "SCREENER"
    FILING = "FILING"


class SourceReference(BaseModel):
    """Where an observation came from - a Box 1B cell or a Box 3 table cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_type: SourceType
    source_url: Label
    retrieved_or_extracted_at: AwareDatetime
    document_id: UUID | None = None
    artifact_sha256: Sha256Hex | None = None
    extraction_id: UUID | None = None
    page_number: PositiveInt | None = None
    table_id: UUID | None = None
    row_index: NonNegativeInt | None = None
    column_index: NonNegativeInt | None = None
    locator: Label  # Box 1B cell locator or Box 3 table locator + /r/c
    raw_artifact_id: UUID | None = None  # the Screener page artifact

    @model_validator(mode="after")
    def complete(self) -> Self:
        if self.source_type == SourceType.FILING and None in (
            self.document_id,
            self.artifact_sha256,
            self.page_number,
            self.table_id,
            self.row_index,
            self.column_index,
        ):
            raise ValueError("A filing observation needs document/artifact/page/table/cell")
        if self.source_type == SourceType.SCREENER and self.raw_artifact_id is None:
            raise ValueError("A Screener observation needs the page artifact")
        return self


class FinancialWarning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: Code
    detail: Label | None = None
    observation_ids: tuple[UUID, ...] = ()


class SourceFinancialObservation(BaseModel):
    """One reported number, exactly as reported, plus its interpretation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    observation_id: UUID
    company_key: str
    metric: CanonicalMetric | None  # None: unmapped (still a full observation)
    statement_type: StatementType
    raw_label: Label
    label_context: Label | None = None  # enclosing caption, e.g. 'Current liabilities'
    raw_value: Label
    parsed: ParsedNumber
    value_kind: ValueKind
    currency: Currency
    scale: Scale
    raw_unit_caption: Label | None = None
    normalized_value: ExactDecimal | None  # MONEY: in CANONICAL_SCALE; else = parsed
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    authority: SourceAuthority
    source: SourceReference
    extraction_method: Label
    confidence: Confidence
    audited: bool | None = None
    restated: bool | None = None
    segment: Label | None = None
    warnings: tuple[Code, ...] = ()


class UnmappedFinancialLine(BaseModel):
    """A financial row the taxonomy does not (yet) recognise - never dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    line_id: UUID
    statement_type: StatementType
    reporting_basis: ReportingBasis
    raw_label: Label
    label_context: Label | None
    source_type: SourceType
    observation_ids: tuple[UUID, ...]


class Outcome(StrEnum):
    MATCH = "MATCH"
    MATCH_WITHIN_TOLERANCE = "MATCH_WITHIN_TOLERANCE"
    SOURCE_PRECEDENCE = "SOURCE_PRECEDENCE"
    RESTATEMENT = "RESTATEMENT"
    CONFLICT = "CONFLICT"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    AMBIGUOUS = "AMBIGUOUS"


class TolerancePolicy(BaseModel):
    """Two values agree when they differ by no more than half a unit of the coarser
    displayed precision (rounding), plus an optional relative band (default 0)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rounding_half_units: Decimal = Decimal("0.5")
    relative: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.01"))
    #: accounting checks: summed rounding half-units of the components, plus this band
    validation_relative: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("0.05"))


class ReconciliationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    record_id: UUID
    key: Label
    metric: CanonicalMetric
    statement_type: StatementType
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    currency: Currency
    value_kind: ValueKind
    outcome: Outcome
    selected_observation_id: UUID | None
    supporting_observation_ids: tuple[UUID, ...]
    conflicting_observation_ids: tuple[UUID, ...]
    excluded_observation_ids: tuple[UUID, ...] = ()
    max_difference: ExactDecimal | None = None
    reason: Label


class CanonicalFinancialValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    canonical_value_id: UUID
    metric: CanonicalMetric
    value: ExactDecimal
    value_kind: ValueKind
    currency: Currency
    scale: Scale  # CANONICAL_SCALE for MONEY, ONES otherwise
    period: FinancialPeriod
    reporting_basis: ReportingBasis
    statement_type: StatementType
    selected_observation_id: UUID
    supporting_observation_ids: tuple[UUID, ...]
    conflicting_observation_ids: tuple[UUID, ...]
    reconciliation_record_id: UUID
    outcome: Outcome
    selection_reason: Label
    confidence: Confidence


class CanonicalLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    metric: CanonicalMetric
    values: tuple[CanonicalFinancialValue, ...]  # one per period, oldest first


class CanonicalStatement(BaseModel):
    """One statement for one basis and one frequency, organised metric x period."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    statement_type: StatementType
    reporting_basis: ReportingBasis
    frequency: Literal["ANNUAL", "QUARTERLY", "POINT_IN_TIME"]
    periods: tuple[FinancialPeriod, ...]
    lines: tuple[CanonicalLine, ...]

    @model_validator(mode="after")
    def one_basis(self) -> Self:
        for line in self.lines:
            if any(
                v.reporting_basis != self.reporting_basis or v.metric != line.metric
                for v in line.values
            ):
                raise ValueError("A statement never mixes bases or metrics within a line")
        return self


class ValidationCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: Literal["BALANCE_SHEET_EQUATION", "CASH_RECONCILIATION", "PBT_TAX_PAT"]
    reporting_basis: ReportingBasis
    period: FinancialPeriod
    status: Literal["PASS", "FAIL"]
    expected: ExactDecimal
    actual: ExactDecimal
    difference: ExactDecimal
    canonical_value_ids: tuple[UUID, ...]


class FinancialLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_tables: PositiveInt = 5_000
    max_observations: PositiveInt = 500_000
    max_unmapped_lines: PositiveInt = 100_000
    max_reconciliation_groups: PositiveInt = 200_000


class FinancialStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    total_observations: NonNegativeInt
    observations_by_source: dict[str, NonNegativeInt]
    observations_by_statement: dict[str, NonNegativeInt]
    mapped_observations: NonNegativeInt
    unmapped_observations: NonNegativeInt
    unmapped_lines: NonNegativeInt
    canonical_values: NonNegativeInt
    outcomes: dict[str, NonNegativeInt]
    validation_pass: NonNegativeInt
    validation_fail: NonNegativeInt


class CanonicalFinancialDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["CANONICAL_FINANCIAL_DATASET_V1"] = "CANONICAL_FINANCIAL_DATASET_V1"
    taxonomy_version: Label = TAXONOMY_VERSION
    dataset_id: UUID
    company_key: str
    company_name: Label
    industry: Industry
    fiscal_year_end_month: int | None = Field(default=None, ge=1, le=12)
    generated_at: AwareDatetime
    screener_source_sha256s: tuple[Sha256Hex, ...]
    evidence_bundle_id: UUID | None
    tolerance: TolerancePolicy
    limits: FinancialLimits
    status: Literal["COMPLETE", "PARTIAL"]
    observations: tuple[SourceFinancialObservation, ...]
    unmapped_lines: tuple[UnmappedFinancialLine, ...]
    reconciliation: tuple[ReconciliationRecord, ...]
    canonical_values: tuple[CanonicalFinancialValue, ...]
    statements: tuple[CanonicalStatement, ...]
    validations: tuple[ValidationCheck, ...]
    warnings: tuple[FinancialWarning, ...]
    statistics: FinancialStatistics

    @model_validator(mode="after")
    def traceable(self) -> Self:
        observed = {o.observation_id for o in self.observations}
        if len(observed) != len(self.observations):
            raise ValueError("Observation IDs must be unique")
        for value in self.canonical_values:
            if (
                value.selected_observation_id not in observed
                or not set(value.supporting_observation_ids) <= observed
            ):
                raise ValueError("Every canonical value must reach a source observation")
        return self

    def statement(
        self,
        statement_type: StatementType,
        basis: ReportingBasis,
        frequency: Literal["ANNUAL", "QUARTERLY", "POINT_IN_TIME"],
    ) -> CanonicalStatement | None:
        return next(
            (
                s
                for s in self.statements
                if s.statement_type == statement_type
                and s.reporting_basis == basis
                and s.frequency == frequency
            ),
            None,
        )

    @property
    def consolidated(self) -> tuple[CanonicalStatement, ...]:
        return tuple(s for s in self.statements if s.reporting_basis == ReportingBasis.CONSOLIDATED)

    @property
    def standalone(self) -> tuple[CanonicalStatement, ...]:
        return tuple(s for s in self.statements if s.reporting_basis == ReportingBasis.STANDALONE)
