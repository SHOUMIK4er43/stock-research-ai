"""DETAILED_RESEARCH_V1 Box 13: due-diligence and risk intelligence (no recommendation).

Box 13 reads the trusted outputs of Boxes 3-12 (and explicit, cited analyst inputs for facts no
upstream box extracts: pledges, auditor opinions, contingent liabilities, related-party
transactions, ratings by instrument, ...) and turns them into one auditable forensic view.

Kinds of record, never blurred:
  SIGNAL           a RiskSignal: one registered rule (or positive control) applied to explicit
                   inputs with a documented formula and thresholds. COMPANY_RISK signals carry a
                   GREEN / YELLOW / RED severity only when every input is valid, the formula
                   applies, a threshold is crossed and evidence exists; otherwise the status
                   says why not (UNAVAILABLE, NOT_APPLICABLE, INSUFFICIENT_SAMPLE, ...).
  POSITIVE_CONTROL evidence-backed favourable reading. It never erases a red flag: both stay.
  DATA_LIMITATION  a gap or defect in the data (missing document, blocked source, unresolved
                   disagreement). Never a statement about the company.
  DILIGENCE ITEM   an open question with the source that would answer it. Never answered here.

No AI, no network, no clock: `as_of` is explicit, `generated_at` is injected and kept out of
every ID. A missing value is never zero. Box 13 describes risk; it never rates the security,
recommends a position, sizes a holding or sets a price.
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

from stock_research_ai.domain.financial import ExactDecimal
from stock_research_ai.domain.financial_canonical import (
    Confidence,
    Currency,
    Industry,
)

DUE_DILIGENCE_SCHEMA: Literal["DUE_DILIGENCE_DATASET_V1"] = "DUE_DILIGENCE_DATASET_V1"
DUE_DILIGENCE_ENGINE_VERSION = "due-diligence/2"  # /2: one input conflict / dedup policy
RULE_REGISTRY_VERSION = "risk-rules/2"  # /2: event severity needs an adverse direction
FORMULA_REGISTRY_VERSION = "dd-formulas/1"
RISK_MAP_RULES_VERSION = "risk-map/1"

MAX_EXPLICIT_INPUTS = 2_000  # per input family
MAX_SIGNALS = 5_000
MAX_ENTRIES = 20_000  # catalogue / disagreement / diligence entries per section
MAX_MONEY = Decimal(10) ** 12  # crore
MAX_PERCENT = Decimal(10) ** 6

Label = Annotated[str, StringConstraints(max_length=1000)]
Text = Annotated[str, StringConstraints(max_length=4000)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Money = Annotated[Decimal, Field(ge=-MAX_MONEY, le=MAX_MONEY, allow_inf_nan=False)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, le=MAX_MONEY, allow_inf_nan=False)]
Percent = Annotated[Decimal, Field(ge=-MAX_PERCENT, le=MAX_PERCENT, allow_inf_nan=False)]
Share = Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


# --- vocabulary -----------------------------------------------------------------------------


class Severity(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class SignalStatus(StrEnum):
    EVALUATED = "EVALUATED"  # inputs valid, formula applies: severity assigned
    INFORMATIONAL = "INFORMATIONAL"  # a fact reported without a severity (by rule)
    UNAVAILABLE = "UNAVAILABLE"  # an input is missing / conflicted / not comparable
    NOT_APPLICABLE = "NOT_APPLICABLE"  # the formula has no meaning here (PAT <= 0, ...)
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"  # below the documented minimum N
    UNSUPPORTED = "UNSUPPORTED"  # a threshold is crossed but the evidence is missing
    UNSUPPORTED_SECTOR_RULE = "UNSUPPORTED_SECTOR_RULE"  # industrial rule on a financial


class SignalClass(StrEnum):
    COMPANY_RISK = "COMPANY_RISK"
    POSITIVE_CONTROL = "POSITIVE_CONTROL"


class RiskCategory(StrEnum):
    FINANCIAL = "FINANCIAL"
    CASH_FLOW = "CASH_FLOW"
    LEVERAGE = "LEVERAGE"
    LIQUIDITY = "LIQUIDITY"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    ACCOUNTING = "ACCOUNTING"
    AUDITOR = "AUDITOR"
    GOVERNANCE = "GOVERNANCE"
    MANAGEMENT = "MANAGEMENT"
    OWNERSHIP = "OWNERSHIP"
    REGULATORY = "REGULATORY"
    LEGAL = "LEGAL"
    ORDER_BOOK = "ORDER_BOOK"
    CUSTOMER_CONCENTRATION = "CUSTOMER_CONCENTRATION"
    INDUSTRY = "INDUSTRY"
    VALUATION_INPUT = "VALUATION_INPUT"
    DATA_QUALITY = "DATA_QUALITY"
    OTHER = "OTHER"


class Layer(StrEnum):
    BOX3 = "BOX3"
    BOX4 = "BOX4"
    BOX5 = "BOX5"
    BOX7 = "BOX7"
    BOX8 = "BOX8"
    BOX9 = "BOX9"
    BOX10 = "BOX10"
    BOX11 = "BOX11"
    BOX12 = "BOX12"
    BOX13 = "BOX13"  # a Box 13 registered formula over the inputs listed with it
    REQUEST = "REQUEST"  # an explicit, cited analyst input


class SignalBasis(StrEnum):
    CONSOLIDATED = "CONSOLIDATED"
    STANDALONE = "STANDALONE"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # not a financial-statement fact (events, ratings ...)


class SectorGate(StrEnum):
    """Box 4's business class, as Box 13 applies it (never a separate guess)."""

    NON_FINANCIAL = "NON_FINANCIAL"  # the industrial rule set applies
    FINANCIAL = "FINANCIAL"  # bank / NBFC / insurer (or subtype unresolved): no industrial rules
    UNKNOWN = "UNKNOWN"  # business class unresolved: business-neutral rules only


class BusinessModel(StrEnum):
    GENERAL = "GENERAL"
    ORDER_BOOK_DRIVEN = "ORDER_BOOK_DRIVEN"  # EPC / capital goods / defence


class Level(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class SourceAuthority(StrEnum):
    EXCHANGE_FILING = "EXCHANGE_FILING"  # an official BSE / NSE filing
    COMPANY_DOCUMENT = "COMPANY_DOCUMENT"  # the company's own filed / published document
    RATING_AGENCY = "RATING_AGENCY"
    MARKET_PROVIDER = "MARKET_PROVIDER"
    SCREENER = "SCREENER"
    ANALYST_INPUT = "ANALYST_INPUT"  # never auto-selected over a filed source


#: Lower ranks win a disagreement on a filed fact.
AUTHORITY_RANK: dict[SourceAuthority, int] = {
    SourceAuthority.EXCHANGE_FILING: 1,
    SourceAuthority.COMPANY_DOCUMENT: 2,
    SourceAuthority.RATING_AGENCY: 3,
    SourceAuthority.MARKET_PROVIDER: 3,
    SourceAuthority.SCREENER: 4,
    SourceAuthority.ANALYST_INPUT: 9,
}

#: The words Box 13 never emits (checked by the dataset validator and the tests).
ADVICE_TERMS = (
    "BUY",
    "SELL",
    "HOLD",
    "OVERWEIGHT",
    "UNDERWEIGHT",
    "TARGET PRICE",
    "POSITION SIZE",
    "BUY ZONE",
    "SELL TRIGGER",
)


# --- explicit, cited inputs -------------------------------------------------------------------


class DiligenceSource(BaseModel):
    """Where an explicit input comes from. `evidence_ids` are Box 3 evidence items: when a
    Box 3 bundle is supplied every one must exist in it (a broken reference is refused)."""

    model_config = _model()
    authority: SourceAuthority
    reference: Label = Field(min_length=1)  # e.g. 'Annual report FY2026, note 38, p.214'
    url: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    document_id: UUID | None = None
    page: PositiveInt | None = None
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=50)


StatementBasis = Literal["CONSOLIDATED", "STANDALONE"]


class ShareCount(BaseModel):
    """Equity shares outstanding on a date. `bonus_split_adjusted` states whether the count is
    restated for later bonus issues / splits (only adjusted counts compare across them)."""

    model_config = _model()
    as_of: date
    shares: Annotated[Decimal, Field(gt=0, le=Decimal(10) ** 13, allow_inf_nan=False)]
    bonus_split_adjusted: bool
    source: DiligenceSource


class MarketCapInput(BaseModel):
    model_config = _model()
    as_of: date
    value_crore: Annotated[Decimal, Field(gt=0, le=MAX_MONEY, allow_inf_nan=False)]
    currency: Currency = Currency.INR
    source: DiligenceSource


class RetainedEarnings(BaseModel):
    """Retained earnings (surplus in the statement of profit and loss) as reported."""

    model_config = _model()
    as_of: date
    basis: StatementBasis
    value_crore: Money
    currency: Currency = Currency.INR
    source: DiligenceSource


class PledgePoint(BaseModel):
    model_config = _model()
    period_end: date
    pledged_pct_of_promoter_holding: Share
    source: DiligenceSource


class HolderCategory(StrEnum):
    PROMOTER = "PROMOTER"
    GOVERNMENT = "GOVERNMENT"
    FII_FPI = "FII_FPI"
    DII = "DII"
    MUTUAL_FUNDS = "MUTUAL_FUNDS"
    PUBLIC = "PUBLIC"
    LARGE_HOLDER = "LARGE_HOLDER"  # a named holder above the disclosure threshold


class HoldingPoint(BaseModel):
    model_config = _model()
    period_end: date
    category: HolderCategory
    holder: Label | None = None  # LARGE_HOLDER only
    pct: Share
    source: DiligenceSource

    @model_validator(mode="after")
    def named(self) -> Self:
        if (self.category == HolderCategory.LARGE_HOLDER) != (self.holder is not None):
            raise ValueError("Exactly a LARGE_HOLDER point names its holder")
        return self


class ShareholderCount(BaseModel):
    model_config = _model()
    period_end: date
    count: PositiveInt
    source: DiligenceSource


class PreferentialIssue(BaseModel):
    model_config = _model()
    issue_date: date
    kind: Literal["PREFERENTIAL_SHARES", "WARRANTS", "CONVERTIBLES"]
    allottee: Literal["PROMOTER_GROUP", "KMP", "NON_PROMOTER"]
    issue_price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    reference_price: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    #: what `reference_price` is: the regulatory floor or a market price on a stated date
    reference_basis: Literal["ICDR_FLOOR_PRICE", "MARKET_CLOSE_ON_RELEVANT_DATE"]
    shares: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None = None
    source: DiligenceSource


class RelatedPartyKind(StrEnum):
    REVENUE = "REVENUE"  # sales / services to the related party
    PURCHASES = "PURCHASES"
    LOAN_GIVEN = "LOAN_GIVEN"
    ADVANCE_GIVEN = "ADVANCE_GIVEN"
    GUARANTEE_GIVEN = "GUARANTEE_GIVEN"
    BALANCE_RECEIVABLE = "BALANCE_RECEIVABLE"
    BALANCE_PAYABLE = "BALANCE_PAYABLE"
    OTHER = "OTHER"


class RelatedPartyRelation(StrEnum):
    PROMOTER_ENTITY = "PROMOTER_ENTITY"
    KMP_ENTITY = "KMP_ENTITY"
    SUBSIDIARY = "SUBSIDIARY"
    JOINT_VENTURE = "JOINT_VENTURE"
    ASSOCIATE = "ASSOCIATE"
    OTHER = "OTHER"
    UNCLEAR = "UNCLEAR"


class RelatedPartyItem(BaseModel):
    model_config = _model()
    fiscal_year: int = Field(ge=1990, le=2100)
    basis: StatementBasis
    kind: RelatedPartyKind
    counterparty: Label = Field(min_length=1)
    relation: RelatedPartyRelation
    amount_crore: NonNegativeMoney
    currency: Currency = Currency.INR
    source: DiligenceSource


class AuditOpinion(StrEnum):
    UNMODIFIED = "UNMODIFIED"
    QUALIFIED = "QUALIFIED"
    ADVERSE = "ADVERSE"
    DISCLAIMER = "DISCLAIMER"


class AuditorReport(BaseModel):
    """One statutory audit report as filed. CARO and IFC remarks keep their own basis: CARO
    applies to the standalone statements and is never read onto the consolidated basis."""

    model_config = _model()
    fiscal_year: int = Field(ge=1990, le=2100)
    basis: StatementBasis
    auditor: Label = Field(min_length=1)
    opinion: AuditOpinion
    emphasis_of_matter: bool | None = None
    going_concern_uncertainty: bool | None = None
    key_audit_matters: tuple[Label, ...] = Field(default=(), max_length=50)
    caro_adverse_remarks: NonNegativeInt | None = None  # STANDALONE only
    ifc_opinion: AuditOpinion | None = None
    accounting_policy_change: bool | None = None
    restatement: bool | None = None
    source: DiligenceSource

    @model_validator(mode="after")
    def caro_is_standalone(self) -> Self:
        if self.caro_adverse_remarks is not None and self.basis != "STANDALONE":
            raise ValueError("CARO remarks belong to the standalone audit report")
        return self


class AuditorChange(BaseModel):
    model_config = _model()
    change_date: date
    outgoing: Label | None = None
    incoming: Label | None = None
    reason: Literal["ROTATION", "TERM_COMPLETION", "REGULATOR_APPOINTMENT", "RESIGNATION",
                    "REMOVAL", "OTHER", "NOT_STATED"]
    mid_term: bool | None = None
    concerns_unresolved: bool | None = None  # only as the filing or the auditor states it
    source: DiligenceSource


class ManagementRole(StrEnum):
    CEO_MD = "CEO_MD"
    CFO = "CFO"
    CHAIRMAN = "CHAIRMAN"
    COMPANY_SECRETARY = "COMPANY_SECRETARY"
    DIRECTOR = "DIRECTOR"
    OTHER_KMP = "OTHER_KMP"


class ManagementChange(BaseModel):
    model_config = _model()
    change_date: date
    role: ManagementRole
    person: Label | None = None
    kind: Literal["APPOINTMENT", "RESIGNATION", "RETIREMENT", "REMOVAL", "TERM_END", "DEATH"]
    planned_succession: bool | None = None  # only when the filing discloses it
    source: DiligenceSource


class ContingentCategory(StrEnum):
    TAX = "TAX"
    LEGAL_CLAIM = "LEGAL_CLAIM"
    GUARANTEE = "GUARANTEE"  # corporate guarantees given
    BANK_GUARANTEE = "BANK_GUARANTEE"
    PERFORMANCE_GUARANTEE = "PERFORMANCE_GUARANTEE"
    CAPITAL_COMMITMENT = "CAPITAL_COMMITMENT"  # a commitment, never a contingent liability
    OTHER = "OTHER"
    TOTAL = "TOTAL"  # the note's own stated total


class ContingentItem(BaseModel):
    model_config = _model()
    as_of: date
    basis: StatementBasis
    category: ContingentCategory
    description: Label | None = None
    amount_crore: NonNegativeMoney
    currency: Currency = Currency.INR
    source: DiligenceSource


class CwipBucket(StrEnum):
    LT_1Y = "LT_1Y"
    Y1_2 = "Y1_2"
    Y2_3 = "Y2_3"
    GT_3Y = "GT_3Y"


class CwipAgeing(BaseModel):
    model_config = _model()
    as_of: date
    basis: StatementBasis
    bucket: CwipBucket
    status: Literal["IN_PROGRESS", "SUSPENDED"]
    amount_crore: NonNegativeMoney
    currency: Currency = Currency.INR
    source: DiligenceSource


class RatingOutlook(StrEnum):
    STABLE = "STABLE"
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    DEVELOPING = "DEVELOPING"
    WATCH_NEGATIVE = "WATCH_NEGATIVE"
    WATCH_POSITIVE = "WATCH_POSITIVE"
    WATCH_DEVELOPING = "WATCH_DEVELOPING"
    NOT_STATED = "NOT_STATED"


class RatingActionKind(StrEnum):
    ASSIGNED = "ASSIGNED"
    REAFFIRMED = "REAFFIRMED"
    UPGRADED = "UPGRADED"
    DOWNGRADED = "DOWNGRADED"
    WITHDRAWN = "WITHDRAWN"
    OUTLOOK_REVISED = "OUTLOOK_REVISED"
    WATCH = "WATCH"
    NOT_STATED = "NOT_STATED"


class RatingInput(BaseModel):
    """A rating action by instrument, as the agency published it (grade text verbatim)."""

    model_config = _model()
    action_date: date
    agency: Label = Field(min_length=1)
    instrument: Label = Field(min_length=1)
    rating: Label = Field(min_length=1)
    outlook: RatingOutlook = RatingOutlook.NOT_STATED
    action: RatingActionKind = RatingActionKind.NOT_STATED
    sensitivities: Text | None = None
    source: DiligenceSource


class AgencyAdjustedDebt(BaseModel):
    """Debt as a rating agency adjusts it; kept apart from reported debt (definitions differ)."""

    model_config = _model()
    as_of: date
    agency: Label = Field(min_length=1)
    value_crore: NonNegativeMoney
    definition: Label = Field(min_length=1)
    currency: Currency = Currency.INR
    source: DiligenceSource


class DebtMaturity(BaseModel):
    model_config = _model()
    as_of: date
    basis: StatementBasis
    bucket: Literal["WITHIN_1Y", "Y1_3", "Y3_5", "BEYOND_5Y"]
    amount_crore: NonNegativeMoney
    currency: Currency = Currency.INR
    source: DiligenceSource


class ExecutivePay(BaseModel):
    """Total remuneration of the stated KMP scope for one fiscal year."""

    model_config = _model()
    fiscal_year: int = Field(ge=1990, le=2100)
    scope: Label = Field(min_length=1)  # e.g. 'MD + CFO + CS (Form MGT-7 / AR)'
    amount_crore: NonNegativeMoney
    currency: Currency = Currency.INR
    source: DiligenceSource


class BoardFact(BaseModel):
    model_config = _model()
    as_of: date
    directors: PositiveInt
    independent_directors: NonNegativeInt
    executive_chair: bool | None = None
    average_attendance_pct: Share | None = None
    source: DiligenceSource

    @model_validator(mode="after")
    def counted(self) -> Self:
        if self.independent_directors > self.directors:
            raise ValueError("More independent directors than directors")
        return self


class PenaltyInput(BaseModel):
    model_config = _model()
    penalty_date: date
    authority: Label = Field(min_length=1)  # e.g. 'NSE', 'BSE', 'SEBI'
    amount_inr: Annotated[Decimal, Field(ge=0, le=MAX_MONEY * 10_000_000)] | None = None
    reason: Label = Field(min_length=1)
    source: DiligenceSource


class ComplaintCount(BaseModel):
    model_config = _model()
    fiscal_year: int = Field(ge=1990, le=2100)
    received: NonNegativeInt
    pending_at_year_end: NonNegativeInt
    source: DiligenceSource


class WorkingCapitalItem(StrEnum):
    CONTRACT_ASSETS = "CONTRACT_ASSETS"
    UNBILLED_REVENUE = "UNBILLED_REVENUE"
    CUSTOMER_ADVANCES = "CUSTOMER_ADVANCES"
    RETENTION_MONEY = "RETENTION_MONEY"
    RECEIVABLES_OVER_6_MONTHS = "RECEIVABLES_OVER_6_MONTHS"  # receivables ageing


class WorkingCapitalDisclosure(BaseModel):
    model_config = _model()
    as_of: date
    basis: StatementBasis
    item: WorkingCapitalItem
    amount_crore: NonNegativeMoney
    currency: Currency = Currency.INR
    source: DiligenceSource


class OrderBookReport(BaseModel):
    """An order book the company itself reports (e.g. Ind AS 115 remaining performance
    obligations), with its basis, tax basis and definition as stated."""

    model_config = _model()
    as_of: date
    value_crore: Annotated[Decimal, Field(ge=0, le=MAX_MONEY, allow_inf_nan=False)]
    basis: StatementBasis
    tax_basis: Literal["INCLUSIVE_OF_TAX", "EXCLUSIVE_OF_TAX", "UNKNOWN"] = "UNKNOWN"
    definition: Label = Field(min_length=1)
    currency: Currency = Currency.INR
    source: DiligenceSource


class StatutoryTaxRate(BaseModel):
    """The rate the company's own context makes relevant (regime, entity, year) - never
    assumed by Box 13."""

    model_config = _model()
    rate_pct: Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]
    applies_from_fiscal_year: int = Field(ge=1990, le=2100)
    source: DiligenceSource


class LeverageOverride(BaseModel):
    """A sector module's net-debt / EBITDA thresholds (e.g. for cyclicals), cited."""

    model_config = _model()
    yellow_above: Annotated[Decimal, Field(gt=0, le=100, allow_inf_nan=False)]
    red_above: Annotated[Decimal, Field(gt=0, le=100, allow_inf_nan=False)]
    reason: Label = Field(min_length=1)
    source: DiligenceSource

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.red_above <= self.yellow_above:
            raise ValueError("RED threshold must be above the YELLOW threshold")
        return self


class ReconciliationExplanation(BaseModel):
    """Why a standalone and a consolidated figure differ, as a source states it."""

    model_config = _model()
    metric: Code
    fiscal_year: int = Field(ge=1990, le=2100)
    kind: Literal["GROUP_STRUCTURE", "OTHER"]
    explanation: Label = Field(min_length=1)
    source: DiligenceSource


class ReconciliationReview(BaseModel):
    """An attestation that the sources able to explain standalone / consolidated differences
    for a year (subsidiary / JV notes, consolidation schedule) were actually checked. Without
    it a material difference is never called 'unexplained'."""

    model_config = _model()
    fiscal_year: int = Field(ge=1990, le=2100)
    sources_checked: tuple[Label, ...] = Field(min_length=1, max_length=20)
    source: DiligenceSource


class EventDirection(StrEnum):
    ADVERSE = "ADVERSE"  # e.g. a claim filed against the company, a liability imposed on it
    FAVOURABLE = "FAVOURABLE"  # e.g. an arbitral award in the company's favour
    NEUTRAL = "NEUTRAL"


class ExchangeEventAssessment(BaseModel):
    """Whether a Box 12 exchange event is adverse to the company, as a cited source states it
    (the order, award or notice itself). Box 13 never reads direction from a filing's wording:
    'court', 'tribunal' or 'arbitration' name a topic, not an outcome."""

    model_config = _model()
    event_id: UUID  # a Box 12 event
    direction: EventDirection
    source: DiligenceSource


def _bounded() -> object:
    return Field(default=(), max_length=MAX_EXPLICIT_INPUTS)


class DueDiligenceRequest(BaseModel):
    model_config = _model()
    as_of: date
    company_key: Label = Field(min_length=1)  # must equal the Box 4 company key
    nse_symbol: Label | None = None  # must match the Box 12 identity when Box 12 is given
    isin: Label | None = None
    business_model: BusinessModel = BusinessModel.GENERAL
    business_model_source: DiligenceSource | None = None
    share_counts: tuple[ShareCount, ...] = _bounded()  # type: ignore[assignment]
    market_cap: MarketCapInput | None = None
    retained_earnings: tuple[RetainedEarnings, ...] = _bounded()  # type: ignore[assignment]
    #: Box 5's documented X2 proxy (reserves / other equity) instead of reported retained
    #: earnings: off by default, because reserves are not retained earnings.
    altman_allow_reserves_proxy: bool = False
    pledges: tuple[PledgePoint, ...] = _bounded()  # type: ignore[assignment]
    holdings: tuple[HoldingPoint, ...] = _bounded()  # type: ignore[assignment]
    shareholder_counts: tuple[ShareholderCount, ...] = _bounded()  # type: ignore[assignment]
    preferential_issues: tuple[PreferentialIssue, ...] = _bounded()  # type: ignore[assignment]
    related_party: tuple[RelatedPartyItem, ...] = _bounded()  # type: ignore[assignment]
    auditor_reports: tuple[AuditorReport, ...] = _bounded()  # type: ignore[assignment]
    auditor_changes: tuple[AuditorChange, ...] = _bounded()  # type: ignore[assignment]
    management_changes: tuple[ManagementChange, ...] = _bounded()  # type: ignore[assignment]
    contingent_liabilities: tuple[ContingentItem, ...] = _bounded()  # type: ignore[assignment]
    cwip_ageing: tuple[CwipAgeing, ...] = _bounded()  # type: ignore[assignment]
    ratings: tuple[RatingInput, ...] = _bounded()  # type: ignore[assignment]
    agency_adjusted_debt: tuple[AgencyAdjustedDebt, ...] = _bounded()  # type: ignore[assignment]
    debt_maturities: tuple[DebtMaturity, ...] = _bounded()  # type: ignore[assignment]
    executive_pay: tuple[ExecutivePay, ...] = _bounded()  # type: ignore[assignment]
    board: tuple[BoardFact, ...] = _bounded()  # type: ignore[assignment]
    penalties: tuple[PenaltyInput, ...] = _bounded()  # type: ignore[assignment]
    complaints: tuple[ComplaintCount, ...] = _bounded()  # type: ignore[assignment]
    working_capital: tuple[WorkingCapitalDisclosure, ...] = _bounded()  # type: ignore[assignment]
    order_book_reports: tuple[OrderBookReport, ...] = _bounded()  # type: ignore[assignment]
    statutory_tax_rate: StatutoryTaxRate | None = None
    leverage_override: LeverageOverride | None = None
    reconciliation_explanations: tuple[ReconciliationExplanation, ...] = (
        _bounded()  # type: ignore[assignment]
    )
    reconciliation_reviews: tuple[ReconciliationReview, ...] = _bounded()  # type: ignore[assignment]
    event_assessments: tuple[ExchangeEventAssessment, ...] = _bounded()  # type: ignore[assignment]

    @model_validator(mode="after")
    def declared(self) -> Self:
        if self.business_model == BusinessModel.ORDER_BOOK_DRIVEN and (
            self.business_model_source is None
        ):
            raise ValueError("An order-book-driven classification needs a cited source")
        return self


# --- signals ----------------------------------------------------------------------------------


class Threshold(BaseModel):
    model_config = _model()
    severity: Severity
    comparison: Literal["GT", "GE", "LT", "LE", "EQ"]
    value: ExactDecimal
    note: Label | None = None


class SignalInput(BaseModel):
    """One calculation input, with the upstream record it came from."""

    model_config = _model()
    role: Label
    value: ExactDecimal | None
    unit: Label
    period: Label | None = None
    period_end: date | None = None
    basis: SignalBasis
    layer: Layer
    record_id: UUID | None = None
    detail: Label | None = None


class RiskSignal(BaseModel):
    model_config = _model()
    risk_id: UUID
    rule_id: Label  # CODE/version
    code: Code
    signal_class: SignalClass
    category: RiskCategory
    title: Label
    status: SignalStatus
    severity: Severity | None
    metric: Code
    value: ExactDecimal | None
    unit: Label
    threshold: tuple[Threshold, ...]
    period: Label | None
    period_end: date | None
    basis: SignalBasis
    formula_id: Label
    calculation: Label  # the formula with this signal's numbers
    inputs: tuple[SignalInput, ...]
    evidence_ids: tuple[UUID, ...]  # Box 3 evidence / Box 4 observations / Box 8 / Box 12 records
    record_ids: tuple[UUID, ...]  # canonical values, Box 5 metrics, chains, outcomes, inputs
    source_ids: tuple[Label, ...]  # URLs / document references the evidence lives in
    explanation: Label
    mitigant: Label | None = None
    mitigating_signal_ids: tuple[UUID, ...] = ()
    early_warning: Label | None = None
    confidence: Confidence | None
    issues: tuple[Code, ...] = ()
    reason: Code | None = None  # why the status is not EVALUATED

    @model_validator(mode="after")
    def disciplined(self) -> Self:
        evaluated = self.status == SignalStatus.EVALUATED
        if evaluated != (self.severity is not None):
            raise ValueError("A severity exists exactly when the rule was evaluated")
        if evaluated == (self.reason is not None):
            raise ValueError("A non-evaluated signal says why; an evaluated one does not")
        if self.severity in (Severity.YELLOW, Severity.RED) or (
            self.signal_class == SignalClass.POSITIVE_CONTROL
        ):
            if not self.inputs or not (self.evidence_ids or self.record_ids):
                raise ValueError("A flag or positive control needs inputs and evidence")
            if not self.source_ids or self.confidence is None:
                raise ValueError("A flag or positive control needs its sources and confidence")
        if self.signal_class == SignalClass.POSITIVE_CONTROL and self.severity != Severity.GREEN:
            raise ValueError("A positive control is an evaluated GREEN reading")
        if self.mitigating_signal_ids and self.signal_class != SignalClass.COMPANY_RISK:
            raise ValueError("Only a company risk lists mitigating controls")
        return self


# --- forensic metrics and models ----------------------------------------------------------------


class MetricKind(StrEnum):
    REPORTED = "REPORTED"  # a Box 4 canonical value as reported
    DERIVED = "DERIVED"  # a registered ratio / difference of reported values
    ADJUSTED = "ADJUSTED"  # a reported value with a documented adjustment


class ForensicMetric(BaseModel):
    model_config = _model()
    metric_id: UUID
    code: Code
    label: Label
    kind: MetricKind
    layer: Layer  # BOX4 / BOX5 (consumed) or BOX13 (a Box 13 formula)
    status: Literal["AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"]
    value: ExactDecimal | None
    unit: Label
    period: Label | None
    period_end: date | None
    basis: SignalBasis
    formula_id: Label
    record_ids: tuple[UUID, ...]
    reason: Label | None = None
    issues: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def valued(self) -> Self:
        if (self.status == "AVAILABLE") != (self.value is not None):
            raise ValueError("Exactly an AVAILABLE metric has a value")
        if self.status == "AVAILABLE" and not self.record_ids:
            raise ValueError("An available metric names its upstream records")
        if self.status != "AVAILABLE" and self.reason is None:
            raise ValueError("An unavailable metric says why")
        return self


class ModelComponent(BaseModel):
    model_config = _model()
    name: Code
    status: Literal["AVAILABLE", "PASS", "FAIL", "UNAVAILABLE"]
    value: ExactDecimal | None = None
    source: Layer
    detail: Label | None = None


class ForensicModelView(BaseModel):
    model_config = _model()
    model: Literal["PIOTROSKI_F_SCORE", "ALTMAN_Z", "BENEISH_M"]
    variant: Code
    status: Literal["CALCULATED", "UNAVAILABLE", "NOT_APPLICABLE"]
    score: ExactDecimal | None
    zone: Code | None
    formula: Label
    components: tuple[ModelComponent, ...]
    missing_components: tuple[Code, ...]
    record_ids: tuple[UUID, ...]  # the Box 5 result / Box 13 inputs
    applicability: Label
    warnings: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def complete(self) -> Self:
        if (self.status == "CALCULATED") != (self.score is not None):
            raise ValueError("A model has a score exactly when CALCULATED")
        if self.status == "CALCULATED" and self.missing_components:
            raise ValueError("A partial model is never a score")
        return self


class FinancialForensics(BaseModel):
    model_config = _model()
    metrics: tuple[ForensicMetric, ...]
    models: tuple[ForensicModelView, ...]
    signal_ids: tuple[UUID, ...]


class Box5CheckRef(BaseModel):
    """A Box 5 earnings-quality check, referenced (never re-graded)."""

    model_config = _model()
    check_id: UUID
    code: Code
    status: Label
    box5_severity: Label | None
    observed: ExactDecimal | None
    period: Label | None


class EarningsQuality(BaseModel):
    model_config = _model()
    reported: tuple[ForensicMetric, ...]
    derived: tuple[ForensicMetric, ...]
    adjusted: tuple[ForensicMetric, ...]
    box5_checks: tuple[Box5CheckRef, ...]
    signal_ids: tuple[UUID, ...]


class WorkingCapitalSeries(BaseModel):
    model_config = _model()
    code: Code
    frequency: Literal["ANNUAL", "QUARTERLY"]
    points: tuple[ForensicMetric, ...]  # oldest first
    reason: Label | None = None  # why a series is empty


class WorkingCapitalForensics(BaseModel):
    model_config = _model()
    series: tuple[WorkingCapitalSeries, ...]
    disclosures: tuple[ForensicMetric, ...]  # contract assets / unbilled / advances ratios
    signal_ids: tuple[UUID, ...]


class RatingObservation(BaseModel):
    model_config = _model()
    observation_id: UUID
    action_date: date
    agency: Label
    instrument: Label | None  # None: the exchange feed did not state it
    rating_text: Label
    scale: Literal["INDIA_LONG_TERM", "INDIA_SHORT_TERM", "UNSUPPORTED"]
    notch: int | None  # INDIA_LONG_TERM only: AAA = 1 ... D = 20
    investment_grade: bool | None
    outlook: RatingOutlook
    action: RatingActionKind
    layer: Layer  # REQUEST (by instrument) or BOX12 (exchange feed title)
    record_id: UUID
    source_id: Label


class CreditProfile(BaseModel):
    model_config = _model()
    metrics: tuple[ForensicMetric, ...]
    ratings: tuple[RatingObservation, ...]
    agency_adjusted_debt: tuple[ForensicMetric, ...]  # never merged with reported debt
    maturities: tuple[ForensicMetric, ...]
    liquidity_evidence_ids: tuple[UUID, ...]  # Box 3 rating / liquidity disclosures
    signal_ids: tuple[UUID, ...]


class OwnershipPoint(BaseModel):
    model_config = _model()
    period_end: date
    pct: ExactDecimal
    layer: Layer
    record_id: UUID
    source_id: Label


class OwnershipSeries(BaseModel):
    model_config = _model()
    category: HolderCategory | Literal["PLEDGED_OF_PROMOTER", "SHAREHOLDER_COUNT"]
    holder: Label | None = None
    points: tuple[OwnershipPoint, ...]  # oldest first, one per period (latest revision)
    change_qoq_pp: ExactDecimal | None
    change_4q_pp: ExactDecimal | None
    change_12q_pp: ExactDecimal | None


class Ownership(BaseModel):
    model_config = _model()
    series: tuple[OwnershipSeries, ...]
    unparsed_categories: tuple[Code, ...]  # Box 12 fields the exchange summary does not carry
    signal_ids: tuple[UUID, ...]


class GovernanceEntry(BaseModel):
    model_config = _model()
    entry_id: UUID
    kind: Code  # BOARD / KMP_CHANGE / AUDITOR_CHANGE / EXCHANGE_PENALTY / ...
    entry_date: date | None
    title: Label
    layer: Layer
    record_id: UUID
    source_id: Label | None


class GovernanceEvidenceStatus(StrEnum):
    """What the REVIEWED sources show for one governance topic. NO_ADVERSE_EVIDENCE_IN_
    REVIEWED_SOURCE is never 'good governance': it covers only what was reviewed."""

    EVIDENCE_FOUND = "EVIDENCE_FOUND"  # a flag, or a located factual disclosure
    NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE = "NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE"
    SOURCE_NOT_AVAILABLE = "SOURCE_NOT_AVAILABLE"  # no source for any rule of the topic
    NOT_EVALUABLE = "NOT_EVALUABLE"  # some evidence, but the topic's rules cannot all run
    NOT_APPLICABLE = "NOT_APPLICABLE"


class GovernanceRiskStatus(StrEnum):
    FLAGGED = "FLAGGED"  # a registered rule graded YELLOW / RED on cited evidence
    EVALUATED_NO_FLAG = "EVALUATED_NO_FLAG"  # every applicable rule ran; none flagged
    NOT_EVALUABLE = "NOT_EVALUABLE"  # missing evidence: never a green finding
    NOT_APPLICABLE = "NOT_APPLICABLE"


class GovernanceTopic(BaseModel):
    """One row of the governance evidence ledger (no score, no opinion): the topic, what the
    reviewed sources show, the registered rules' outcome and the evidence it rests on."""

    model_config = _model()
    topic: Code
    title: Label
    material: bool  # counted by the research-completeness ledger
    evidence_status: GovernanceEvidenceStatus
    risk_status: GovernanceRiskStatus
    finding: Label
    reason: Code | None = None  # why the topic is not evaluated
    signal_ids: tuple[UUID, ...] = ()
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=50)  # Box 3 / Box 12 / ...
    record_ids: tuple[UUID, ...] = Field(default=(), max_length=50)  # Box 4 / request inputs
    source_ids: tuple[Label, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def sourced(self) -> Self:
        E, R = GovernanceEvidenceStatus, GovernanceRiskStatus
        found = self.evidence_status in (E.EVIDENCE_FOUND, E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE)
        if found and not (self.evidence_ids or self.record_ids):
            raise ValueError("a governance finding cites its evidence (no provenance, no "
                             "conclusion)")
        if self.risk_status == R.FLAGGED and self.evidence_status != E.EVIDENCE_FOUND:
            raise ValueError("a governance flag rests on found evidence")
        if (self.evidence_status == E.NO_ADVERSE_EVIDENCE_IN_REVIEWED_SOURCE) != (
                self.risk_status == R.EVALUATED_NO_FLAG):
            raise ValueError("'no adverse evidence' means every applicable rule ran unflagged")
        if (self.evidence_status == E.NOT_APPLICABLE) != (self.risk_status == R.NOT_APPLICABLE):
            raise ValueError("a topic is not applicable as a whole")
        if self.risk_status == R.NOT_EVALUABLE and self.reason is None:
            raise ValueError("an unevaluated governance topic says why")
        if self.evidence_status in (E.SOURCE_NOT_AVAILABLE, E.NOT_EVALUABLE) and (
                self.risk_status != R.NOT_EVALUABLE):
            raise ValueError("missing evidence is never an evaluated governance reading")
        return self


class Governance(BaseModel):
    model_config = _model()
    catalogue: tuple[GovernanceEntry, ...] = Field(max_length=MAX_ENTRIES)
    scoring: Literal["NONE"] = "NONE"  # deliberately no composite governance score
    signal_ids: tuple[UUID, ...]
    #: the governance evidence ledger: one row per topic, source-backed or honestly missing
    topics: tuple[GovernanceTopic, ...] = Field(default=(), max_length=32)


class ManagementReliability(BaseModel):
    """Box 8 guidance outcomes, counted. Only COMPLETED quantitative targets are scored; open
    (not-yet-due) guidance is never held against management."""

    model_config = _model()
    window_start: date
    window_end: date
    minimum_sample: PositiveInt
    sample_status: Literal["SUFFICIENT", "INSUFFICIENT_SAMPLE", "UNAVAILABLE"]
    issued: NonNegativeInt
    completed: NonNegativeInt
    met: NonNegativeInt  # MET + BEAT
    missed: NonNegativeInt
    partial: NonNegativeInt
    slipped: NonNegativeInt  # later lowered / widened by management
    withdrawn: NonNegativeInt
    open: NonNegativeInt  # NOT_YET_DUE
    not_scored: NonNegativeInt  # NOT_COMPARABLE / INSUFFICIENT_DATA / AMBIGUOUS
    accuracy_pct: ExactDecimal | None
    outcome_ids: tuple[UUID, ...]
    qa_answer_status: tuple[tuple[Code, NonNegativeInt], ...]
    signal_ids: tuple[UUID, ...]


class ExchangeEventRisk(BaseModel):
    model_config = _model()
    event_id: UUID
    event_date: date
    event_type: Code
    category: RiskCategory
    title: Label
    value_crore: ExactDecimal | None
    materiality_pct: ExactDecimal | None  # Box 12 materiality (stated value / revenue)
    status: Literal["EVALUATED", "INFORMATIONAL"]
    severity: Severity | None
    rule: Label
    source_ids: tuple[Label, ...]
    #: ADVERSE_BY_EVENT_TYPE (a cancellation, an exchange fine) or the cited direction; a
    #: litigation / regulatory event without a cited ADVERSE direction is never graded
    direction: Literal["ADVERSE_BY_EVENT_TYPE", "ADVERSE", "FAVOURABLE", "NEUTRAL",
                       "CONFLICTING", "NOT_ESTABLISHED", "NOT_ASSESSED"] = "NOT_ASSESSED"

    @model_validator(mode="after")
    def graded(self) -> Self:
        if (self.status == "EVALUATED") != (self.severity is not None):
            raise ValueError("Only a numerically material event carries a severity")
        if self.severity in (Severity.YELLOW, Severity.RED) and self.direction not in (
                "ADVERSE_BY_EVENT_TYPE", "ADVERSE"):
            raise ValueError("Only an event adverse to the company is a risk flag")
        return self


class ExchangeEvents(BaseModel):
    model_config = _model()
    window_start: date
    window_end: date
    entries: tuple[ExchangeEventRisk, ...] = Field(max_length=MAX_ENTRIES)
    counts: tuple[tuple[Code, NonNegativeInt], ...]
    signal_ids: tuple[UUID, ...]


class OrderMetricView(BaseModel):
    model_config = _model()
    kind: Code
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    value: ExactDecimal | None
    unit: Label
    completeness: Label
    reason: Code | None


class PendingL1Summary(BaseModel):
    """Pending L1 bids are a pipeline, never confirmed backlog."""

    model_config = _model()
    chains: NonNegativeInt
    true_pending: NonNegativeInt
    unresolved_linkage: NonNegativeInt
    source_incomplete: NonNegativeInt
    counted_in_confirmed: Literal[0] = 0
    true_pending_value_crore: ExactDecimal | None
    unresolved_value_crore: ExactDecimal | None
    chain_ids: tuple[UUID, ...] = Field(max_length=MAX_ENTRIES)


class OrderBookQuality(BaseModel):
    model_config = _model()
    applicable: bool
    applicability_basis: tuple[Code, ...]
    reported_order_book_crore: ExactDecimal | None
    reported_order_book_as_of: date | None
    reported_order_book_tax_basis: Label | None
    reported_order_book_basis: Label | None = None
    reported_order_book_definition: Label | None = None
    reported_order_book_layer: Layer | None = None  # BOX7 (extracted) or REQUEST (cited)
    metrics: tuple[OrderMetricView, ...]
    pending_l1: PendingL1Summary | None
    confirmed_chains: NonNegativeInt
    cancelled_chains: NonNegativeInt
    unlinked_cancellations: NonNegativeInt
    revised_chains: NonNegativeInt
    signal_ids: tuple[UUID, ...]


class ContingentLiabilities(BaseModel):
    model_config = _model()
    as_of: date | None
    basis: SignalBasis
    by_category: tuple[tuple[ContingentCategory, ExactDecimal], ...]
    total_crore: ExactDecimal | None
    total_method: Literal["STATED_TOTAL", "SUM_OF_CATEGORIES", "UNAVAILABLE"]
    capital_commitments_crore: ExactDecimal | None  # reported, never added to the total
    record_ids: tuple[UUID, ...]
    issues: tuple[Code, ...]
    signal_ids: tuple[UUID, ...]


class RelatedParty(BaseModel):
    model_config = _model()
    fiscal_year: int | None
    basis: SignalBasis
    totals: tuple[tuple[RelatedPartyKind, ExactDecimal], ...]
    record_ids: tuple[UUID, ...]
    signal_ids: tuple[UUID, ...]


class AuditorTextMention(BaseModel):
    """A fixed-phrase match in Box 3 auditor-report evidence: a lead, never a finding."""

    model_config = _model()
    evidence_id: UUID
    document_id: UUID
    phrase: Code
    page: PositiveInt
    source_id: Label


class AuditorSignals(BaseModel):
    model_config = _model()
    reports: tuple[UUID, ...]  # explicit auditor-report input IDs
    changes: tuple[UUID, ...]
    text_mentions: tuple[AuditorTextMention, ...] = Field(max_length=MAX_ENTRIES)
    signal_ids: tuple[UUID, ...]


class ReconciliationStatus(StrEnum):
    EXPECTED_GROUP_DIFFERENCE = "EXPECTED_GROUP_DIFFERENCE"
    MATERIAL_EXPLAINED = "MATERIAL_EXPLAINED"
    MATERIAL_UNEXPLAINED = "MATERIAL_UNEXPLAINED"  # only after a recorded source review
    MATERIAL_NOT_REVIEWED = "MATERIAL_NOT_REVIEWED"  # material, sources not yet checked
    IMMATERIAL = "IMMATERIAL"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    UNAVAILABLE = "UNAVAILABLE"


class BasisComparison(BaseModel):
    model_config = _model()
    comparison_id: UUID
    metric: Code
    period: Label
    period_end: date
    standalone_value: ExactDecimal | None
    consolidated_value: ExactDecimal | None
    absolute_difference: ExactDecimal | None
    percentage_difference: ExactDecimal | None  # of the consolidated value, when valid
    explanation: Label | None
    explanation_record_id: UUID | None
    status: ReconciliationStatus
    record_ids: tuple[UUID, ...]
    reason: Label


class DisagreementGroup(StrEnum):
    FINANCIAL = "FINANCIAL"
    MARKET = "MARKET"
    ORDER_BOOK = "ORDER_BOOK"
    SHAREHOLDING = "SHAREHOLDING"
    RATING = "RATING"
    EVENT = "EVENT"
    GOVERNANCE = "GOVERNANCE"


class DisagreeingValue(BaseModel):
    model_config = _model()
    source: Label
    authority: Label
    value: ExactDecimal | None
    record_id: UUID | None


class DisagreementEntry(BaseModel):
    model_config = _model()
    entry_id: UUID
    group: DisagreementGroup
    origin: Layer
    subject: Label
    period: Label
    basis: Label
    values: tuple[DisagreeingValue, ...] = Field(min_length=2)
    status: Literal["OPEN", "RESOLVED"]
    canonical_source: Label | None
    reason: Label

    @model_validator(mode="after")
    def selected(self) -> Self:
        if (self.status == "OPEN") != (self.canonical_source is None):
            raise ValueError("Only a resolved disagreement names its canonical source")
        return self


class SourceDisagreements(BaseModel):
    model_config = _model()
    entries: tuple[DisagreementEntry, ...] = Field(max_length=MAX_ENTRIES)
    open_count: NonNegativeInt
    resolved_count: NonNegativeInt
    by_group: tuple[tuple[DisagreementGroup, NonNegativeInt], ...]
    truncated: bool = False


class DataQualityItem(BaseModel):
    model_config = _model()
    item_id: UUID
    kind: Code  # MISSING_REQUIRED_DOCUMENT / STALE_DATA / BLOCKED_SOURCE / ...
    layer: Layer
    detail: Label
    record_ids: tuple[UUID, ...] = ()
    affects: tuple[Code, ...] = ()  # rule codes whose inputs this limits


class DataQualitySummary(BaseModel):
    """DATA_LIMITATION only: never a statement about the company."""

    model_config = _model()
    risk_kind: Literal["DATA_LIMITATION"] = "DATA_LIMITATION"
    items: tuple[DataQualityItem, ...] = Field(max_length=MAX_ENTRIES)
    counts: tuple[tuple[Code, NonNegativeInt], ...]
    rules_unavailable: NonNegativeInt
    rules_evaluated: NonNegativeInt


class OpenDiligenceItem(BaseModel):
    model_config = _model()
    item_id: UUID
    question: Label
    reason: Label
    importance: Level
    needed_source: Label
    related_risk_ids: tuple[UUID, ...] = ()
    status: Literal["OPEN"] = "OPEN"


class RiskMapEntry(BaseModel):
    model_config = _model()
    risk_id: UUID
    code: Code
    category: RiskCategory
    severity: Severity
    likelihood: Level
    likelihood_rule: Label
    impact: Level
    impact_rule: Label


class PeerContext(BaseModel):
    """Box 9 peer statistics beside a company metric: context only, no severity."""

    model_config = _model()
    metric_code: Code
    period_end: date
    company_value: ExactDecimal | None
    peer_count: NonNegativeInt
    peer_median: ExactDecimal | None
    peer_minimum: ExactDecimal | None
    peer_maximum: ExactDecimal | None
    benchmark_id: UUID


class SectorSpecificRisks(BaseModel):
    model_config = _model()
    sector_gate: SectorGate
    business_model: BusinessModel
    gated_rules: tuple[Code, ...]  # rules not applied for this sector
    peer_context: tuple[PeerContext, ...]
    signal_ids: tuple[UUID, ...]


# --- registry and manifest -------------------------------------------------------------------


class RuleDefinition(BaseModel):
    model_config = _model()
    formula_id: Label  # CODE/version
    code: Code
    version: PositiveInt
    name: Label
    signal_class: SignalClass
    category: RiskCategory
    inputs: tuple[Label, ...]
    formula_text: Label
    applicability: Label
    sectors: tuple[SectorGate, ...]
    thresholds: tuple[Threshold, ...]
    unit: Label
    impact: Level
    minimum_sample: PositiveInt | None = None
    notes: Label = ""


class FormulaDefinition(BaseModel):
    """A Box 13 metric formula (not a rule): forensic metrics, adjusted values, model parts."""

    model_config = _model()
    formula_id: Label
    code: Code
    name: Label
    inputs: tuple[Label, ...]
    formula_text: Label
    applicability: Label
    unit: Label
    version: PositiveInt


class DueDiligenceManifest(BaseModel):
    model_config = _model()
    engine_version: Label
    rule_registry_version: Label
    formula_registry_version: Label
    risk_map_rules_version: Label
    rules: tuple[RuleDefinition, ...]
    formulas: tuple[FormulaDefinition, ...]
    policies: tuple[tuple[Code, Label], ...]


class UpstreamRef(BaseModel):
    model_config = _model()
    layer: Layer
    dataset_id: UUID | None
    sha256: Sha256Hex
    schema_version: Label
    status: Label


class DueDiligenceIssue(BaseModel):
    model_config = _model()
    code: Code
    severity: Literal["ERROR", "WARNING", "INFO"]
    detail: Label | None = None


# --- dataset --------------------------------------------------------------------------------


class DueDiligenceDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["DUE_DILIGENCE_DATASET_V1"] = DUE_DILIGENCE_SCHEMA
    engine_version: Label = DUE_DILIGENCE_ENGINE_VERSION
    dataset_id: UUID
    company_key: Label
    company_name: Label
    industry: Industry
    sector_gate: SectorGate
    business_model: BusinessModel
    analytical_basis: SignalBasis  # CONSOLIDATED unless the company reports none
    basis_selection: Label
    control_basis: SignalBasis | None  # the forensic / control layer (standalone)
    currency: Currency
    as_of: date
    latest_fiscal_year_end: date | None
    generated_at: AwareDatetime  # injected; never part of an ID
    sources: tuple[UpstreamRef, ...]
    request: DueDiligenceRequest
    financial_forensics: FinancialForensics
    earnings_quality: EarningsQuality
    accounting_risks: tuple[UUID, ...]
    balance_sheet_risks: tuple[UUID, ...]
    liquidity_credit_risks: CreditProfile
    working_capital_risks: WorkingCapitalForensics
    governance_risks: Governance
    management_reliability: ManagementReliability
    ownership_risks: Ownership
    exchange_event_risks: ExchangeEvents
    order_book_quality: OrderBookQuality
    contingent_liabilities: ContingentLiabilities
    related_party_risks: RelatedParty
    auditor_signals: AuditorSignals
    standalone_consolidated_reconciliation: tuple[BasisComparison, ...]
    source_disagreements: SourceDisagreements
    red_flags: tuple[UUID, ...]  # COMPANY_RISK signals graded YELLOW or RED, RED first
    positive_controls: tuple[UUID, ...]
    risk_map: tuple[RiskMapEntry, ...]
    open_diligence_items: tuple[OpenDiligenceItem, ...] = Field(max_length=MAX_ENTRIES)
    data_quality_summary: DataQualitySummary
    sector_specific_risks: SectorSpecificRisks
    signals: tuple[RiskSignal, ...] = Field(max_length=MAX_SIGNALS)
    issues: tuple[DueDiligenceIssue, ...]
    manifest: DueDiligenceManifest
    #: Box 13 describes risk; it carries no rating, ranking, target or recommendation.
    recommendation: Literal["NONE"] = "NONE"

    def signal(self, risk_id: UUID) -> RiskSignal:
        return next(s for s in self.signals if s.risk_id == risk_id)

    def by_code(self, code: str) -> list[RiskSignal]:
        return [s for s in self.signals if s.code == code]

    def section_signal_ids(self) -> tuple[UUID, ...]:
        return (
            *self.financial_forensics.signal_ids,
            *self.earnings_quality.signal_ids,
            *self.accounting_risks,
            *self.balance_sheet_risks,
            *self.liquidity_credit_risks.signal_ids,
            *self.working_capital_risks.signal_ids,
            *self.governance_risks.signal_ids,
            *self.management_reliability.signal_ids,
            *self.ownership_risks.signal_ids,
            *self.exchange_event_risks.signal_ids,
            *self.order_book_quality.signal_ids,
            *self.contingent_liabilities.signal_ids,
            *self.related_party_risks.signal_ids,
            *self.auditor_signals.signal_ids,
            *self.sector_specific_risks.signal_ids,
        )

    @model_validator(mode="after")
    def integrity(self) -> Self:
        ids = [s.risk_id for s in self.signals]
        if len(ids) != len(set(ids)):
            raise ValueError("Signal IDs must be unique")
        known = set(ids)
        if not set(self.section_signal_ids()) <= known:
            raise ValueError("A section references an unknown signal")
        if set(ids) - set(self.section_signal_ids()):
            raise ValueError("Every signal belongs to a section")
        by_id = {s.risk_id: s for s in self.signals}
        flags = {
            s.risk_id
            for s in self.signals
            if s.signal_class == SignalClass.COMPANY_RISK
            and s.severity in (Severity.YELLOW, Severity.RED)
        }
        if set(self.red_flags) != flags or len(self.red_flags) != len(flags):
            raise ValueError("red_flags lists exactly the YELLOW / RED company risks")
        ranks = [0 if by_id[i].severity == Severity.RED else 1 for i in self.red_flags]
        if ranks != sorted(ranks):
            raise ValueError("red_flags lists RED before YELLOW")
        controls = {
            s.risk_id for s in self.signals if s.signal_class == SignalClass.POSITIVE_CONTROL
        }
        if set(self.positive_controls) != controls:
            raise ValueError("positive_controls lists exactly the positive controls")
        if {e.risk_id for e in self.risk_map} != flags:
            raise ValueError("The risk map covers exactly the red flags")
        for s in self.signals:
            if not set(s.mitigating_signal_ids) <= controls:
                raise ValueError("A mitigant must be a positive control")
        for item in self.open_diligence_items:
            if not set(item.related_risk_ids) <= known:
                raise ValueError("A diligence item references an unknown signal")
        # the readings Box 13 authors (upstream / caller text stays in inputs and calculation)
        text = " ".join(
            (s.title + " " + s.explanation + " " + (s.mitigant or "")).upper()
            for s in self.signals
            if s.status in (SignalStatus.EVALUATED, SignalStatus.INFORMATIONAL)
        )
        for term in ADVICE_TERMS:
            if f" {term} " in f" {text} ":
                raise ValueError(f"Box 13 never uses advice language ({term})")
        return self
