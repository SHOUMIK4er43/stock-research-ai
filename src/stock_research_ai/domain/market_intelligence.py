"""DETAILED_RESEARCH_V1 Box 12: market and exchange intelligence (no recommendation).

Every Box 12 value enters through an explicit adapter payload (a RawPayload: Kite, NSE or BSE
bytes, preserved by SHA-256) or an explicit, cited caller input (reported order book, period
revenue, reference revenue, order facts, comparable facts from other sources, the text of an
already-fetched filing document). Box 12 never searches the web, never uses AI and never
browses: classification, dates, values and materiality are deterministic rules over those
inputs.

Kinds of record, never blurred:
  IDENTITY     one company-market identity (ISIN, NSE symbol, BSE code, name, Kite token),
               each field with the payload that evidenced it; ambiguity fails closed
  MARKET       a dated quote snapshot and raw dated daily candles; returns, CAGR, 52-week
               range, drawdown, volatility, liquidity, benchmark comparison and beta are
               calculated here from the candles, with a documented methodology
  EVENT        a canonical exchange filing (type, date, wording, explicit value), merged
               across exchanges without discarding any source
  ORDER        an order/project stage (L1 bidder != letter of award != signed contract !=
               revenue), linked into chains so no order is counted twice
  ACTION       a corporate action with its own announcement / record / ex / payment dates;
               prices are never adjusted here
  FILING       a catalogued document (annual report, results, shareholding, BRSR, ...)
  DISAGREEMENT two sources reporting different values for the same fact, both preserved

A value that cannot be supported is UNAVAILABLE with a reason, never zero. Box 12 describes;
it never ranks, rates or recommends. Money is in crore of the stated currency unless a field
says otherwise, per-share amounts in ones, percentages in percent (12.5 means 12.5 %).
Dates are exchange-local (Asia/Kolkata).
"""

from datetime import date, timedelta, timezone
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

MARKET_INTELLIGENCE_SCHEMA: Literal["MARKET_INTELLIGENCE_DATASET_V1"] = (
    "MARKET_INTELLIGENCE_DATASET_V1"
)
MARKET_INTELLIGENCE_ENGINE_VERSION = "market-intelligence/1"
CLASSIFICATION_VERSION = "event-rules/1"
ORDER_RULES_VERSION = "order-rules/2"
MARKET_METHOD_VERSION = "market-methods/1"
#: Exchange-local time. India has no daylight saving: a fixed +05:30 offset is exact.
IST = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")

#: The only hosts Box 12 fetches from or keeps as source URLs (exact names, HTTPS only).
ALLOWED_SOURCE_HOSTS = frozenset(
    {
        "api.kite.trade",
        "www.nseindia.com",
        "nsearchives.nseindia.com",
        "api.bseindia.com",
        "www.bseindia.com",
    }
)

# --- resource bounds ----------------------------------------------------------------------
MAX_PAYLOAD_BYTES = 16_000_000
MAX_PAYLOADS = 200
MAX_SOURCE_RECORDS = 20_000  # filing/event rows decoded from all payloads together
MAX_MASTER_ROWS = 250_000  # instrument-master lookup rows (Kite / NSE lists), separately
MAX_EVENTS = 5_000
MAX_CANDLES = 4_000  # > 10 years of sessions
MAX_HISTORY_DAYS = 3_700  # 10 years + slack, per request
MAX_DOCUMENT_TEXT = 400_000  # characters of one filing's extracted text
MAX_DOCUMENT_TEXTS = 500
MAX_CALLER_INPUTS = 2_000
MAX_ISSUES = 5_000
#: Arithmetic guards, not economic caps.
MAX_PRICE = Decimal(10) ** 9
MAX_VOLUME = Decimal(10) ** 15
MAX_MONEY = Decimal(10) ** 12  # crore
MAX_PERCENT = Decimal(10) ** 6
#: crore -> ones
CRORE = Decimal(10_000_000)
LAKH = Decimal(100_000)

Label = Annotated[str, StringConstraints(max_length=1000)]
Text = Annotated[str, StringConstraints(max_length=8000)]
Url = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Isin = Annotated[str, StringConstraints(pattern=r"^IN[A-Z0-9]{9}[0-9]$")]
NseSymbol = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9&_-]{0,19}$")]
BseCode = Annotated[str, StringConstraints(pattern=r"^[0-9]{6}$")]
KiteToken = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,12}$")]
Money = Annotated[Decimal, Field(ge=-MAX_MONEY, le=MAX_MONEY, allow_inf_nan=False)]
Percent = Annotated[Decimal, Field(ge=-MAX_PERCENT, le=MAX_PERCENT, allow_inf_nan=False)]
Price = Annotated[Decimal, Field(gt=0, le=MAX_PRICE, allow_inf_nan=False)]
Volume = Annotated[Decimal, Field(ge=0, le=MAX_VOLUME, allow_inf_nan=False)]


def _model() -> ConfigDict:
    return ConfigDict(frozen=True, extra="forbid")


# --- sources and authority -------------------------------------------------------------------


class Provider(StrEnum):
    KITE = "KITE"
    NSE = "NSE"
    BSE = "BSE"
    SCREENER = "SCREENER"
    COMPANY = "COMPANY"  # a company-published document cited by the caller
    CALLER = "CALLER"  # an explicit analyst input


class Authority(StrEnum):
    PRIMARY_REGULATORY = "PRIMARY_REGULATORY"  # BSE/NSE official filing
    PRIMARY_COMPANY = "PRIMARY_COMPANY"  # the company's own filed/published document
    MARKET_PROVIDER = "MARKET_PROVIDER"  # Kite
    THIRD_PARTY_STRUCTURED = "THIRD_PARTY_STRUCTURED"  # Screener
    EXPLICIT_INPUT = "EXPLICIT_INPUT"  # an analyst-supplied value with a citation


#: Lower ranks win a disagreement on a filed fact. EXPLICIT_INPUT is never auto-selected.
AUTHORITY_RANK: dict[Authority, int] = {
    Authority.PRIMARY_REGULATORY: 1,
    Authority.PRIMARY_COMPANY: 2,
    Authority.MARKET_PROVIDER: 3,
    Authority.THIRD_PARTY_STRUCTURED: 4,
    Authority.EXPLICIT_INPUT: 9,
}


class PayloadKind(StrEnum):
    KITE_INSTRUMENTS = "KITE_INSTRUMENTS"  # CSV instrument master
    KITE_QUOTE = "KITE_QUOTE"  # /quote JSON
    KITE_HISTORICAL = "KITE_HISTORICAL"  # /instruments/historical/{token}/day JSON
    NSE_EQUITY_MASTER = "NSE_EQUITY_MASTER"  # archives EQUITY_L.csv
    NSE_ANNOUNCEMENTS = "NSE_ANNOUNCEMENTS"
    NSE_CORPORATE_ACTIONS = "NSE_CORPORATE_ACTIONS"
    NSE_SHAREHOLDING = "NSE_SHAREHOLDING"
    NSE_ANNUAL_REPORTS = "NSE_ANNUAL_REPORTS"
    NSE_INSIDER_TRADING = "NSE_INSIDER_TRADING"
    NSE_FINANCIAL_RESULTS = "NSE_FINANCIAL_RESULTS"
    NSE_BRSR = "NSE_BRSR"
    NSE_CREDIT_RATINGS = "NSE_CREDIT_RATINGS"
    BSE_COMPANY_HEADER = "BSE_COMPANY_HEADER"
    BSE_ANNOUNCEMENTS = "BSE_ANNOUNCEMENTS"
    BSE_CORPORATE_ACTIONS = "BSE_CORPORATE_ACTIONS"


PAYLOAD_PROVIDER: dict[PayloadKind, Provider] = {
    k: Provider.KITE
    if k.startswith("KITE")
    else Provider.NSE
    if k.startswith("NSE")
    else Provider.BSE
    for k in PayloadKind
}
PAYLOAD_AUTHORITY: dict[PayloadKind, Authority] = {
    k: Authority.MARKET_PROVIDER if p == Provider.KITE else Authority.PRIMARY_REGULATORY
    for k, p in PAYLOAD_PROVIDER.items()
}


class FetchStatus(StrEnum):
    OK = "OK"
    AUTH_REQUIRED = "AUTH_REQUIRED"  # no credential configured / 401
    BLOCKED = "BLOCKED"  # 403 (including anti-bot denials: never bypassed)
    RATE_LIMITED = "RATE_LIMITED"  # 429: never retried automatically
    NOT_FOUND = "NOT_FOUND"  # 404
    UNSUPPORTED = "UNSUPPORTED"  # endpoint/kind not supported, redirect refused, budget spent
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    TOO_LARGE = "TOO_LARGE"
    HTTP_ERROR = "HTTP_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"  # bad URL / symbol / range: nothing was sent


class Issue(BaseModel):
    model_config = _model()
    code: Code
    severity: Literal["ERROR", "WARNING", "INFO"]
    message: Label
    payload_id: UUID | None = None
    locator: Label | None = None


class RawPayload(BaseModel):
    """Exact bytes from one adapter call (or its failure). The only way exchange and market
    data enters Box 12. Contains no credential: URLs never carry tokens."""

    model_config = _model()
    kind: PayloadKind
    url: Url
    status: FetchStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    retrieved_at: AwareDatetime
    content: bytes | None = None
    content_type: Label | None = None
    error_code: Code | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == FetchStatus.OK) != (self.content is not None):
            raise ValueError("Exactly the successful payloads carry content")
        if self.content is not None and len(self.content) > MAX_PAYLOAD_BYTES:
            raise ValueError("Payload exceeds the Box 12 size bound")
        if self.status != FetchStatus.OK and self.error_code is None:
            raise ValueError("A failed payload needs an error code")
        return self


class SourcePayloadRecord(BaseModel):
    """What the dataset keeps of a RawPayload: identity and hash, not the bytes."""

    model_config = _model()
    payload_id: UUID
    kind: PayloadKind
    provider: Provider
    authority: Authority
    url: Url
    status: FetchStatus
    http_status: int | None
    retrieved_at: AwareDatetime
    content_sha256: Sha256Hex | None
    size_bytes: NonNegativeInt
    error_code: Code | None
    records_decoded: NonNegativeInt = 0


class Citation(BaseModel):
    """Where an explicit caller input comes from."""

    model_config = _model()
    provider: Provider
    authority: Authority
    reference: Label = Field(min_length=1)  # e.g. 'Q1 FY27 investor presentation p.7'
    url: Url | None = None


# --- identity ------------------------------------------------------------------------------


class IdentityField(StrEnum):
    ISIN = "ISIN"
    NSE_SYMBOL = "NSE_SYMBOL"
    BSE_CODE = "BSE_CODE"
    NAME = "NAME"
    KITE_TOKEN = "KITE_TOKEN"


class IdentityStatus(StrEnum):
    RESOLVED = "RESOLVED"  # every declared listing confirmed by a source, no conflict
    PARTIAL = "PARTIAL"  # no conflict, but some declared field is unconfirmed
    AMBIGUOUS = "AMBIGUOUS"  # sources conflict or match more than one security
    FAILED = "FAILED"  # nothing confirmed


class IdentityEvidence(BaseModel):
    model_config = _model()
    field: IdentityField
    value: Label
    provider: Provider
    payload_id: UUID | None  # None = declared by the caller
    locator: Label


class CompanyIdentity(BaseModel):
    """`isin`, `nse_symbol` and `bse_code` are VERIFIED identifiers: each was stated by a
    provider payload (never only by the caller) without conflict. A BSE code counts as
    verified only when a BSE header states it together with an ISIN, so a header that merely
    echoes the requested code proves nothing. What the caller declared is kept separately in
    `declared_*` and never admits exchange rows on its own."""

    model_config = _model()
    identity_id: UUID
    status: IdentityStatus
    isin: Isin | None = None
    nse_symbol: NseSymbol | None = None
    bse_code: BseCode | None = None
    declared_isin: Isin | None = None
    declared_nse_symbol: NseSymbol | None = None
    declared_bse_code: BseCode | None = None
    name: Label | None = None
    kite_instrument_token: KiteToken | None = None
    kite_exchange: Literal["NSE", "BSE"] | None = None
    evidence: tuple[IdentityEvidence, ...] = ()
    issues: tuple[Issue, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status in (IdentityStatus.RESOLVED, IdentityStatus.PARTIAL)


# --- market data ---------------------------------------------------------------------------


class Freshness(StrEnum):
    CURRENT = "CURRENT"  # within the request's current-quote window
    DELAYED = "DELAYED"  # older than current but within the stale bound: show its time
    STALE = "STALE"  # older than the stale bound: never present as current
    UNKNOWN = "UNKNOWN"  # no usable timestamp


class InstrumentRef(BaseModel):
    model_config = _model()
    symbol: Label = Field(min_length=1, max_length=40)
    exchange: Literal["NSE", "BSE"]
    kind: Literal["EQUITY", "INDEX"]
    instrument_token: KiteToken | None = None
    name: Label | None = None


class MarketSnapshot(BaseModel):
    """One dated quote. `previous_close` is the provider's prior-session close."""

    model_config = _model()
    snapshot_id: UUID
    instrument: InstrumentRef
    last_price: Price
    previous_close: Price | None = None
    open: Price | None = None
    high: Price | None = None
    low: Price | None = None
    volume: Volume | None = None
    average_price: Price | None = None
    bid: Price | None = None
    ask: Price | None = None
    depth_available: bool = False
    last_trade_time: AwareDatetime | None = None
    exchange_timestamp: AwareDatetime | None = None
    observed_at: AwareDatetime  # when the payload was retrieved
    market_date: date | None  # exchange-local date of exchange_timestamp
    freshness: Freshness
    age_seconds: NonNegativeInt | None = None
    currency: Literal["INR"] = "INR"
    source: Provider
    authority: Authority
    payload_id: UUID
    locator: Label
    issues: tuple[Issue, ...] = ()

    @model_validator(mode="after")
    def ranges(self) -> Self:
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("High below low")
        if (self.freshness == Freshness.UNKNOWN) != (self.age_seconds is None):
            raise ValueError("Freshness is UNKNOWN exactly when no age is known")
        return self


class Candle(BaseModel):
    model_config = _model()
    trading_date: date
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Volume | None = None
    payload_id: UUID
    locator: Label

    @model_validator(mode="after")
    def ohlc(self) -> Self:
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("Inconsistent OHLC")
        return self


class PriceHistory(BaseModel):
    model_config = _model()
    history_id: UUID
    instrument: InstrumentRef
    interval: Literal["DAY"] = "DAY"
    #: Kite documents its own adjustment practice; Box 12 never re-adjusts and never claims it
    #: verified the provider's adjustments against the corporate-action ledger.
    adjustment_basis: Literal["PROVIDER_DEFINED_UNVERIFIED"] = "PROVIDER_DEFINED_UNVERIFIED"
    return_basis: Literal["PRICE_ONLY_EXCLUDING_DIVIDENDS"] = "PRICE_ONLY_EXCLUDING_DIVIDENDS"
    candles: tuple[Candle, ...] = Field(default=(), max_length=MAX_CANDLES)
    earliest: date | None = None
    latest: date | None = None
    freshness: Freshness
    lag_days: NonNegativeInt | None = None
    payload_ids: tuple[UUID, ...] = ()
    issues: tuple[Issue, ...] = ()

    @model_validator(mode="after")
    def ordered(self) -> Self:
        dates = [c.trading_date for c in self.candles]
        if dates != sorted(set(dates)):
            raise ValueError("Candle dates must be unique and ascending")
        if (self.earliest, self.latest) != ((dates[0], dates[-1]) if dates else (None, None)):
            raise ValueError("History bounds disagree with candles")
        return self


class MetricStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class ReturnPeriod(StrEnum):
    D1 = "1D"
    W1 = "1W"
    M1 = "1M"
    M3 = "3M"
    M6 = "6M"
    Y1 = "1Y"
    Y3 = "3Y"
    Y5 = "5Y"
    Y10 = "10Y"


class PeriodReturn(BaseModel):
    model_config = _model()
    period: ReturnPeriod
    status: MetricStatus
    target_start: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    start_close: Price | None = None
    end_close: Price | None = None
    elapsed_days: NonNegativeInt | None = None
    absolute_return_pct: Percent | None = None
    cagr_pct: Percent | None = None  # only for periods of at least one year
    method: Label
    reason: Code | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        available = self.status == MetricStatus.AVAILABLE
        if available != (self.absolute_return_pct is not None) or available == (
            self.reason is not None
        ):
            raise ValueError("An available return has a value and no reason")
        return self


class StatisticKind(StrEnum):
    HIGH_52W = "HIGH_52W"
    LOW_52W = "LOW_52W"
    DISTANCE_FROM_HIGH_52W = "DISTANCE_FROM_HIGH_52W"
    DISTANCE_FROM_LOW_52W = "DISTANCE_FROM_LOW_52W"
    MAX_DRAWDOWN_1Y = "MAX_DRAWDOWN_1Y"
    MAX_DRAWDOWN_FULL_HISTORY = "MAX_DRAWDOWN_FULL_HISTORY"
    ANNUALIZED_VOLATILITY_1Y = "ANNUALIZED_VOLATILITY_1Y"
    AVERAGE_VOLUME_20D = "AVERAGE_VOLUME_20D"
    AVERAGE_VOLUME_252D = "AVERAGE_VOLUME_252D"
    AVERAGE_TRADED_VALUE_20D = "AVERAGE_TRADED_VALUE_20D"
    AVERAGE_TRADED_VALUE_252D = "AVERAGE_TRADED_VALUE_252D"


class MarketStatistic(BaseModel):
    model_config = _model()
    kind: StatisticKind
    status: MetricStatus
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    unit: Literal["PRICE", "PERCENT", "SHARES", "INR_CRORE"]
    window_start: date | None = None
    window_end: date | None = None
    observations: NonNegativeInt = 0
    reference_date: date | None = None  # date of the high / low / drawdown peak
    second_date: date | None = None  # drawdown trough
    method: Label
    reason: Code | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        available = self.status == MetricStatus.AVAILABLE
        if available != (self.value is not None) or available == (self.reason is not None):
            raise ValueError("An available statistic has a value and no reason")
        return self


class BenchmarkRole(StrEnum):
    BROAD_MARKET = "BROAD_MARKET"
    SECTOR = "SECTOR"


class BenchmarkComparison(BaseModel):
    model_config = _model()
    benchmark: InstrumentRef
    role: BenchmarkRole
    period: ReturnPeriod
    status: MetricStatus
    start_date: date | None = None
    end_date: date | None = None
    company_return_pct: Percent | None = None
    benchmark_return_pct: Percent | None = None
    excess_return_pp: Percent | None = None  # company - benchmark, percentage points
    relative_performance_pct: Percent | None = None  # (1+c)/(1+b) - 1
    reason: Code | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == MetricStatus.AVAILABLE) != (self.excess_return_pp is not None):
            raise ValueError("An available comparison has an excess return")
        return self


class BetaMethodology(BaseModel):
    """The only beta Box 12 computes. Any other beta must come in as an explicit input."""

    model_config = _model()
    frequency: Literal["DAILY"] = "DAILY"
    return_definition: Literal["SIMPLE_CLOSE_TO_CLOSE"] = "SIMPLE_CLOSE_TO_CLOSE"
    lookback_days: Literal[365] = 365
    minimum_observations: Literal[200] = 200
    alignment: Literal["COMMON_SESSIONS_CONSECUTIVE_IN_BOTH"] = (
        "COMMON_SESSIONS_CONSECUTIVE_IN_BOTH"
    )
    estimator: Literal["SAMPLE_COVARIANCE_OVER_SAMPLE_VARIANCE"] = (
        "SAMPLE_COVARIANCE_OVER_SAMPLE_VARIANCE"
    )


class BetaEstimate(BaseModel):
    model_config = _model()
    benchmark: InstrumentRef
    methodology: BetaMethodology = BetaMethodology()
    status: MetricStatus
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    observations: NonNegativeInt = 0
    start_date: date | None = None
    end_date: date | None = None
    reason: Code | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == MetricStatus.AVAILABLE) != (self.value is not None):
            raise ValueError("An available beta has a value")
        return self


class BenchmarkSpec(BaseModel):
    """A benchmark the caller asks for. A sector index is used only when explicitly mapped."""

    model_config = _model()
    symbol: Label = Field(min_length=1, max_length=40)  # Kite tradingsymbol, e.g. 'NIFTY 50'
    exchange: Literal["NSE", "BSE"] = "NSE"
    role: BenchmarkRole = BenchmarkRole.BROAD_MARKET
    mapping_citation: Citation | None = None

    @model_validator(mode="after")
    def mapped(self) -> Self:
        if self.role == BenchmarkRole.SECTOR and self.mapping_citation is None:
            raise ValueError("A sector index needs an explicit, cited mapping")
        return self


# --- events --------------------------------------------------------------------------------


class EventType(StrEnum):
    RESULTS = "RESULTS"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    CONCALL = "CONCALL"
    INVESTOR_PRESENTATION = "INVESTOR_PRESENTATION"
    ORDER_AWARD = "ORDER_AWARD"
    L1_BIDDER = "L1_BIDDER"
    LOA = "LOA"
    ORDER_CANCELLATION = "ORDER_CANCELLATION"
    ORDER_REVISION = "ORDER_REVISION"
    MOU = "MOU"
    JV = "JV"
    ACQUISITION = "ACQUISITION"
    DIVESTMENT = "DIVESTMENT"
    FUND_RAISE = "FUND_RAISE"
    DIVIDEND = "DIVIDEND"
    BUYBACK = "BUYBACK"
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    RIGHTS = "RIGHTS"
    BOARD_CHANGE = "BOARD_CHANGE"
    KMP_CHANGE = "KMP_CHANGE"
    AUDITOR_CHANGE = "AUDITOR_CHANGE"
    CREDIT_RATING = "CREDIT_RATING"
    LITIGATION = "LITIGATION"
    REGULATORY = "REGULATORY"
    EXCHANGE_PENALTY = "EXCHANGE_PENALTY"
    AGM_EGM = "AGM_EGM"
    POSTAL_BALLOT = "POSTAL_BALLOT"
    SHAREHOLDING = "SHAREHOLDING"
    INSIDER_TRADE = "INSIDER_TRADE"
    BULK_DEAL = "BULK_DEAL"
    BLOCK_DEAL = "BLOCK_DEAL"
    OTHER_MATERIAL_EVENT = "OTHER_MATERIAL_EVENT"


ORDER_EVENT_TYPES = frozenset(
    {
        EventType.ORDER_AWARD,
        EventType.L1_BIDDER,
        EventType.LOA,
        EventType.ORDER_CANCELLATION,
        EventType.ORDER_REVISION,
    }
)


class FinancialBasis(StrEnum):
    CONSOLIDATED = "CONSOLIDATED"
    STANDALONE = "STANDALONE"
    BOTH = "BOTH"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class TaxBasis(StrEnum):
    INCLUSIVE_OF_TAX = "INCLUSIVE_OF_TAX"
    EXCLUSIVE_OF_TAX = "EXCLUSIVE_OF_TAX"
    UNKNOWN = "UNKNOWN"


class EventSourceRef(BaseModel):
    """One exchange record behind a canonical event. Merging never drops a source."""

    model_config = _model()
    provider: Provider
    authority: Authority
    payload_id: UUID
    record_id: Label | None  # the exchange's own sequence / news id
    locator: Label
    raw_category: Label | None = None
    source_url: Url | None = None
    published_at: AwareDatetime | None = None


class Materiality(BaseModel):
    """Deterministic only: a stated value divided by an explicit, cited annual revenue."""

    model_config = _model()
    status: MetricStatus
    ratio_pct: Percent | None = None
    numerator_crore: Money | None = None
    denominator_crore: Money | None = None
    denominator_fiscal_year: int | None = None
    denominator_basis: FinancialBasis | None = None
    method: Literal["STATED_VALUE_OVER_REFERENCE_ANNUAL_REVENUE"] = (
        "STATED_VALUE_OVER_REFERENCE_ANNUAL_REVENUE"
    )
    issues: tuple[Code, ...] = ()
    reason: Code | None = None


class EventStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"  # a later filing explicitly revises it


class CorporateEvent(BaseModel):
    model_config = _model()
    event_id: UUID
    identity_id: UUID
    event_date: date  # exchange-local dissemination date
    event_time: AwareDatetime | None = None
    exchanges: tuple[Literal["NSE", "BSE"], ...] = Field(min_length=1)
    event_type: EventType
    classification_rule: Code
    title: Label
    summary: Text | None = None
    #: The filer's own wording, verbatim; never used to decide materiality.
    management_wording: Text | None = None
    financial_value_crore: Money | None = None
    value_currency: Literal["INR", "USD", "EUR"] | None = None
    value_tax_basis: TaxBasis | None = None
    value_text: Label | None = None  # verbatim value as written
    counterparty: Label | None = None
    source_url: Url | None = None
    document_reference: Label | None = None
    authority: Authority
    basis: FinancialBasis
    status: EventStatus = EventStatus.ACTIVE
    supersedes_event_id: UUID | None = None
    related_event_ids: tuple[UUID, ...] = ()
    sources: tuple[EventSourceRef, ...] = Field(min_length=1)
    materiality: Materiality | None = None
    issues: tuple[Issue, ...] = ()

    @model_validator(mode="after")
    def valued(self) -> Self:
        if (self.financial_value_crore is None) != (self.value_currency is None):
            raise ValueError("A stated value needs its currency")
        if self.supersedes_event_id == self.event_id or self.event_id in self.related_event_ids:
            raise ValueError("An event cannot relate to itself")
        return self


class CorporateActionKind(StrEnum):
    DIVIDEND = "DIVIDEND"
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    RIGHTS = "RIGHTS"
    BUYBACK = "BUYBACK"
    MERGER = "MERGER"
    DEMERGER = "DEMERGER"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


class CorporateAction(BaseModel):
    model_config = _model()
    action_id: UUID
    kind: CorporateActionKind
    subject: Label  # verbatim
    announcement_date: date | None = None
    record_date: date | None = None
    ex_date: date | None = None
    book_closure_start: date | None = None
    book_closure_end: date | None = None
    payment_date: date | None = None
    effective_date: date | None = None
    amount_per_share: Decimal | None = Field(default=None, ge=0, le=MAX_PRICE)
    currency: Literal["INR"] | None = None
    ratio: Label | None = None  # verbatim 'a:b' for bonus / rights
    face_value_from: Decimal | None = Field(default=None, gt=0, le=MAX_PRICE)
    face_value_to: Decimal | None = Field(default=None, gt=0, le=MAX_PRICE)
    #: Box 12 never adjusts prices or share counts for an action.
    price_adjustment: Literal["NOT_APPLIED"] = "NOT_APPLIED"
    sources: tuple[EventSourceRef, ...] = Field(min_length=1)
    issues: tuple[Issue, ...] = ()


# --- orders --------------------------------------------------------------------------------


class OrderStage(StrEnum):
    L1_BIDDER = "L1_BIDDER"  # lowest bidder: NOT an order
    LOA = "LOA"  # letter of award / acceptance / work order: an awarded order
    SIGNED_CONTRACT = "SIGNED_CONTRACT"  # contract agreement signed
    CANCELLATION = "CANCELLATION"
    REVISION = "REVISION"


STAGE_ORDER = {OrderStage.L1_BIDDER: 0, OrderStage.LOA: 1, OrderStage.SIGNED_CONTRACT: 2}


class CancellationScope(StrEnum):
    FULL = "FULL"  # the filing explicitly cancels the whole order / LOA / contract
    PARTIAL = "PARTIAL"  # the filing explicitly cancels a part (amount or percent if stated)
    UNSPECIFIED = "UNSPECIFIED"  # neither is explicit: never read as a full cancellation


class Geography(StrEnum):
    DOMESTIC = "DOMESTIC"
    INTERNATIONAL = "INTERNATIONAL"
    UNKNOWN = "UNKNOWN"


class AwardMode(StrEnum):
    COMPETITIVE_BIDDING = "COMPETITIVE_BIDDING"
    NOMINATION = "NOMINATION"
    UNKNOWN = "UNKNOWN"


class ClientCategory(StrEnum):
    GOVERNMENT = "GOVERNMENT"  # ministry / department / railway zone
    PSU = "PSU"  # government-owned company
    PRIVATE = "PRIVATE"
    UNKNOWN = "UNKNOWN"


class OrderRecord(BaseModel):
    """One order-stage disclosure, from one canonical event. Values are as stated."""

    model_config = _model()
    order_id: UUID
    event_id: UUID
    stage: OrderStage
    announcement_date: date
    client: Label | None = None
    client_key: Label | None = None  # normalized for linking only
    project: Text | None = None
    gross_value_crore: Money | None = None
    value_currency: Literal["INR", "USD", "EUR"] | None = None
    value_tax_basis: TaxBasis = TaxBasis.UNKNOWN
    value_text: Label | None = None
    joint_venture: bool | None = None  # None = not stated
    jv_share_pct: Percent | None = None
    jv_partner: Label | None = None
    attributable_value_crore: Money | None = None
    geography: Geography = Geography.UNKNOWN
    segment: Label | None = None
    execution_period: Label | None = None  # verbatim, e.g. '36 Months'
    execution_months: NonNegativeInt | None = None
    expected_completion: date | None = None
    award_mode: AwardMode = AwardMode.UNKNOWN
    client_category: ClientCategory = ClientCategory.UNKNOWN
    referenced_letter_dates: tuple[date, ...] = ()
    #: Explicit identifiers as 'KIND:ID' (TENDER, CONTRACT, LOA, WORK_ORDER, PACKAGE).
    reference_ids: tuple[Label, ...] = ()
    cancellation_scope: CancellationScope | None = None  # cancellation records only
    cancellation_pct: Percent | None = None  # an explicitly stated cancelled share
    source_url: Url | None = None
    document_sha256: Sha256Hex | None = None
    extraction_rules: tuple[Code, ...] = ()
    issues: tuple[Issue, ...] = ()

    @model_validator(mode="after")
    def valued(self) -> Self:
        if (self.gross_value_crore is None) != (self.value_currency is None):
            raise ValueError("A stated order value needs its currency")
        if (self.cancellation_scope is not None) != (self.stage == OrderStage.CANCELLATION):
            raise ValueError("Only a cancellation has a cancellation scope")
        if self.cancellation_pct is not None and not Decimal(0) < self.cancellation_pct < 100:
            raise ValueError("A cancelled share must be in (0, 100)")
        if self.attributable_value_crore is not None and self.gross_value_crore is None:
            raise ValueError("Attributable value requires a gross value")
        if self.jv_share_pct is not None and not Decimal(0) < self.jv_share_pct <= 100:
            raise ValueError("JV share must be in (0, 100]")
        return self


class ChainStatus(StrEnum):
    PENDING_L1 = "PENDING_L1"  # lowest bidder only: never confirmed order book
    CONFIRMED = "CONFIRMED"  # awarded (LOA) or contracted (or re-awarded), not cancelled
    PARTIALLY_CANCELLED = "PARTIALLY_CANCELLED"  # an explicit part cancelled, rest active
    CANCELLED = "CANCELLED"  # explicitly cancelled as a whole
    #: A cancellation whose scope or amount cannot be determined: the remaining value is
    #: unknown, never assumed to be zero (full) or unchanged.
    CANCELLATION_UNRESOLVED = "CANCELLATION_UNRESOLVED"
    UNLINKED_CANCELLATION = "UNLINKED_CANCELLATION"  # cancels an order outside the ledger
    UNLINKED_REVISION = "UNLINKED_REVISION"


#: Chains whose remaining value counts in the confirmed order value.
COUNTED_CHAIN_STATUSES = frozenset({ChainStatus.CONFIRMED, ChainStatus.PARTIALLY_CANCELLED})


class TransitionKind(StrEnum):
    L1 = "L1"
    AWARD = "AWARD"  # first award of the chain (LOA / contract)
    REAWARD = "REAWARD"  # a new award after a full cancellation
    STAGE = "STAGE"  # a later confirmed stage of the same award (e.g. signed contract)
    PARTIAL_CANCELLATION = "PARTIAL_CANCELLATION"
    FULL_CANCELLATION = "FULL_CANCELLATION"
    UNRESOLVED_CANCELLATION = "UNRESOLVED_CANCELLATION"
    REVISION = "REVISION"
    DUPLICATE = "DUPLICATE"  # the same stage disclosed again: never a second order
    IGNORED = "IGNORED"  # recorded, no economic effect (reason in `rule`)


class OrderTransition(BaseModel):
    """One step of a chain's chronological state machine, with its provenance. `amount` is
    the awarded value (AWARD / REAWARD), the cancelled value (cancellations) or the
    revision delta (REVISION); None when not stated or not comparable."""

    model_config = _model()
    order_id: UUID
    effective_date: date
    stage: OrderStage
    kind: TransitionKind
    status_after: ChainStatus | None
    value_before_crore: Money | None = None
    value_after_crore: Money | None = None
    amount_crore: Money | None = None
    currency: Literal["INR", "USD", "EUR"] | None = None
    tax_basis: TaxBasis = TaxBasis.UNKNOWN
    rule: Code


class L1Resolution(StrEnum):
    LINKED_TO_AWARD = "LINKED_TO_AWARD"  # the chain reached LOA / contract
    #: Coverage is complete and no later award could belong to this bid.
    TRUE_PENDING = "TRUE_PENDING"
    #: A later award of a compatible client exists that the evidence can neither link to
    #: this bid nor rule out (missing attachment text, project, tender number ...).
    UNRESOLVED_LINKAGE = "UNRESOLVED_LINKAGE"
    #: The announcement feed cannot show whether a later award exists (failed or truncated
    #: source, or the bid predates ledger coverage).
    SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"


class L1Linkage(BaseModel):
    """Why an L1 bid is (or is not) known to be still pending. 'No LOA was found' and 'we
    could not tell whether an LOA exists' are never the same answer."""

    model_config = _model()
    resolution: L1Resolution
    follow_up_search_performed: bool
    event_coverage_complete: bool
    document_text_available: bool
    strong_link_key: bool  # project text or an explicit tender / contract number
    unresolved_award_candidates: NonNegativeInt = 0
    candidate_order_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    reasons: tuple[Code, ...] = ()


class OrderChain(BaseModel):
    """One project across its stages. Exactly one chain counts a project's value."""

    model_config = _model()
    chain_id: UUID
    client: Label | None
    project: Text | None
    order_ids: tuple[UUID, ...] = Field(min_length=1)
    stages: tuple[OrderStage, ...]
    link_rules: tuple[Code, ...] = ()
    status: ChainStatus
    revised: bool = False
    reawarded: bool = False
    first_announced: date
    confirmed_on: date | None = None  # first LOA / contract
    last_updated: date
    value_currency: Literal["INR", "USD", "EUR"] | None = None
    value_tax_basis: TaxBasis = TaxBasis.UNKNOWN
    pending_l1_value_crore: Money | None = None
    confirmed_gross_value_crore: Money | None = None
    confirmed_attributable_value_crore: Money | None = None
    cancelled_value_crore: Money | None = None
    revision_delta_crore: Money | None = None
    geography: Geography = Geography.UNKNOWN
    award_mode: AwardMode = AwardMode.UNKNOWN
    client_category: ClientCategory = ClientCategory.UNKNOWN
    counted_in_confirmed: bool
    counting_note: Code
    transitions: tuple[OrderTransition, ...] = ()
    l1_linkage: L1Linkage | None = None
    issues: tuple[Issue, ...] = ()

    @model_validator(mode="after")
    def counted(self) -> Self:
        if self.counted_in_confirmed != (self.status in COUNTED_CHAIN_STATUSES):
            raise ValueError("Only confirmed chains count in the confirmed order value")
        if not {t.order_id for t in self.transitions} <= set(self.order_ids):
            raise ValueError("A transition belongs to an order of its chain")
        if self.status == ChainStatus.PENDING_L1 and (
            self.confirmed_gross_value_crore is not None
            or self.confirmed_attributable_value_crore is not None
        ):
            raise ValueError("An L1 chain has no confirmed value")
        return self


class ReportedOrderBook(BaseModel):
    """An order book the company itself reported (explicit, cited)."""

    model_config = _model()
    as_of: date
    value_crore: Money
    currency: Literal["INR", "USD", "EUR"] = "INR"
    tax_basis: TaxBasis
    basis: FinancialBasis
    citation: Citation


class PeriodRevenue(BaseModel):
    """Revenue for one fiscal period (explicit, cited; e.g. from Box 4)."""

    model_config = _model()
    period_start: date
    period_end: date
    value_crore: Money
    currency: Literal["INR", "USD", "EUR"] = "INR"
    tax_basis: TaxBasis = TaxBasis.EXCLUSIVE_OF_TAX
    basis: FinancialBasis
    citation: Citation

    @model_validator(mode="after")
    def period(self) -> Self:
        if self.period_end <= self.period_start:
            raise ValueError("Empty revenue period")
        if self.basis not in (FinancialBasis.CONSOLIDATED, FinancialBasis.STANDALONE):
            raise ValueError("Revenue must be consolidated or standalone, never mixed")
        return self


class ReportedInflow(BaseModel):
    """Order inflow the company itself reported for a period (explicit, cited)."""

    model_config = _model()
    period_start: date
    period_end: date
    value_crore: Money
    currency: Literal["INR", "USD", "EUR"] = "INR"
    tax_basis: TaxBasis
    citation: Citation


class OrderFact(BaseModel):
    """A cited, explicit fact about one disclosed order (matched by its filing URL)."""

    model_config = _model()
    source_url: Url
    jv_share_pct: Percent | None = None
    jv_partner: Label | None = None
    client_category: ClientCategory | None = None
    segment: Label | None = None
    award_mode: AwardMode | None = None
    expected_completion: date | None = None
    citation: Citation


class OrderBookMetricKind(StrEnum):
    DISCLOSED_CONFIRMED_INFLOW = "DISCLOSED_CONFIRMED_INFLOW"
    DISCLOSED_PENDING_L1_VALUE = "DISCLOSED_PENDING_L1_VALUE"  # never in the order book
    ORDER_BOOK_TO_REVENUE = "ORDER_BOOK_TO_REVENUE"
    BOOK_TO_BILL = "BOOK_TO_BILL"
    ORDER_INFLOW_GROWTH = "ORDER_INFLOW_GROWTH"
    EXECUTION_RATE = "EXECUTION_RATE"
    CANCELLATION_RATE = "CANCELLATION_RATE"
    DOMESTIC_SHARE = "DOMESTIC_SHARE"
    INTERNATIONAL_SHARE = "INTERNATIONAL_SHARE"
    TOP_CLIENT_SHARE = "TOP_CLIENT_SHARE"
    TOP5_CLIENT_SHARE = "TOP5_CLIENT_SHARE"
    GOVERNMENT_SHARE = "GOVERNMENT_SHARE"
    COMPETITIVE_BIDDING_SHARE = "COMPETITIVE_BIDDING_SHARE"
    NOMINATION_SHARE = "NOMINATION_SHARE"
    JV_ATTRIBUTABLE_CONFIRMED_VALUE = "JV_ATTRIBUTABLE_CONFIRMED_VALUE"
    #: The split of the pending-L1 pipeline by linkage evidence: never one overstated total.
    PENDING_L1_TRUE_PENDING_VALUE = "PENDING_L1_TRUE_PENDING_VALUE"
    PENDING_L1_UNRESOLVED_VALUE = "PENDING_L1_UNRESOLVED_VALUE"


class OrderBookMetric(BaseModel):
    model_config = _model()
    kind: OrderBookMetricKind
    status: MetricStatus
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    unit: Literal["INR_CRORE", "PERCENT", "RATIO", "CRORE"]
    period_start: date | None = None
    period_end: date | None = None
    chains: NonNegativeInt = 0
    method: Label
    completeness: Literal["DISCLOSED_ORDERS_ONLY", "REPORTED", "MIXED", "NOT_APPLICABLE"]
    reason: Code | None = None
    issues: tuple[Code, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> Self:
        available = self.status == MetricStatus.AVAILABLE
        if available != (self.value is not None) or available == (self.reason is not None):
            raise ValueError("An available metric has a value and no reason")
        return self


class OrderBookReconciliation(BaseModel):
    """opening + inflow - executed revenue - cancellations +/- revisions = implied closing."""

    model_config = _model()
    status: MetricStatus
    period_start: date
    period_end: date
    opening_crore: Money | None = None
    inflow_crore: Money | None = None
    inflow_source: Literal["REPORTED", "LEDGER_DISCLOSED_ORDERS_ONLY"] | None = None
    executed_revenue_crore: Money | None = None
    cancellations_crore: Money | None = None
    revisions_crore: Money | None = None
    implied_closing_crore: Money | None = None
    reported_closing_crore: Money | None = None
    difference_crore: Money | None = None
    reason: Code | None = None
    issues: tuple[Code, ...] = ()


class OrderBookAnalytics(BaseModel):
    model_config = _model()
    metrics: tuple[OrderBookMetric, ...]
    reconciliation: OrderBookReconciliation | None = None
    l1_policy: Literal["L1_NEVER_CONFIRMED_REPORTED_SEPARATELY"] = (
        "L1_NEVER_CONFIRMED_REPORTED_SEPARATELY"
    )
    tax_inclusive_values_allowed: bool = False


# --- ownership, filings, disagreements, coverage, timeline ---------------------------------


class ShareholdingFiling(BaseModel):
    """One exchange shareholding-pattern filing. Only the fields the feed itself states are
    parsed; the rest of the pattern lives in the cited XBRL and is listed as unparsed."""

    model_config = _model()
    filing_id: UUID
    period_end: date
    submitted_on: date | None = None
    promoter_pct: Percent | None = None
    public_pct: Percent | None = None
    employee_trusts_pct: Percent | None = None
    revised: bool = False
    supersedes_filing_id: UUID | None = None
    unparsed_categories: tuple[Code, ...] = (
        "FII_FPI",
        "DII",
        "MUTUAL_FUNDS",
        "GOVERNMENT",
        "SHAREHOLDER_COUNT",
        "PROMOTER_PLEDGE",
        "LARGE_HOLDERS",
    )
    xbrl_url: Url | None = None
    sources: tuple[EventSourceRef, ...] = Field(min_length=1)


class InsiderTrade(BaseModel):
    model_config = _model()
    record_id: UUID
    person: Label
    person_category: Label | None = None
    transaction_type: Label | None = None  # verbatim (Buy / Sell / Pledge ...)
    mode: Label | None = None
    security_type: Label | None = None
    quantity: Volume | None = None
    value_inr: Decimal | None = Field(default=None, ge=0, le=MAX_MONEY * CRORE)
    shares_before: Volume | None = None
    shares_after: Volume | None = None
    from_date: date | None = None
    to_date: date | None = None
    intimation_date: date | None = None
    disclosed_at: AwareDatetime | None = None
    sources: tuple[EventSourceRef, ...] = Field(min_length=1)


class FilingCategory(StrEnum):
    ANNUAL_REPORT = "ANNUAL_REPORT"
    FINANCIAL_RESULT = "FINANCIAL_RESULT"
    BRSR = "BRSR"
    CREDIT_RATING = "CREDIT_RATING"


class FilingDocument(BaseModel):
    model_config = _model()
    document_id: UUID
    category: FilingCategory
    title: Label
    period_label: Label | None = None
    period_start: date | None = None
    period_end: date | None = None
    basis: FinancialBasis
    audited: bool | None = None
    revised: bool = False
    published_on: date | None = None
    url: Url | None = None
    sources: tuple[EventSourceRef, ...] = Field(min_length=1)


class ComparableFact(BaseModel):
    """A value one source states for a fact another source may also state."""

    model_config = _model()
    metric: Code  # e.g. PROMOTER_HOLDING_PCT, DIVIDEND_PER_SHARE
    period: date  # period end / ex-date the value refers to
    basis: FinancialBasis = FinancialBasis.NOT_APPLICABLE
    value: Decimal = Field(allow_inf_nan=False)
    unit: Literal["PERCENT", "INR_PER_SHARE", "INR_CRORE", "COUNT"]
    citation: Citation


class DisagreementStatus(StrEnum):
    OPEN = "OPEN"  # same authority: no automatic selection
    RESOLVED_BY_AUTHORITY_POLICY = "RESOLVED_BY_AUTHORITY_POLICY"


class SourceDisagreement(BaseModel):
    model_config = _model()
    disagreement_id: UUID
    subject: Code
    period: date
    basis: FinancialBasis
    unit: Label
    source_a: Citation
    value_a: Decimal = Field(allow_inf_nan=False)
    source_b: Citation
    value_b: Decimal = Field(allow_inf_nan=False)
    difference: Decimal = Field(allow_inf_nan=False)  # value_a - value_b
    reason: Label | None = None  # only when a source states it
    canonical_selection: Provider | None = None
    selection_rationale: Label
    status: DisagreementStatus

    @model_validator(mode="after")
    def selected(self) -> Self:
        if (self.status == DisagreementStatus.OPEN) != (self.canonical_selection is None):
            raise ValueError("Only a policy-resolved disagreement selects a source")
        return self


class CoverageCategory(StrEnum):
    ANNUAL_REPORTS = "ANNUAL_REPORTS"
    QUARTERLY_RESULTS = "QUARTERLY_RESULTS"
    INVESTOR_PRESENTATIONS = "INVESTOR_PRESENTATIONS"
    CONCALL_TRANSCRIPTS = "CONCALL_TRANSCRIPTS"
    CONCALL_RECORDINGS = "CONCALL_RECORDINGS"
    CREDIT_RATINGS = "CREDIT_RATINGS"
    AGM_EGM_MATERIAL = "AGM_EGM_MATERIAL"
    BRSR = "BRSR"
    SHAREHOLDING_FILINGS = "SHAREHOLDING_FILINGS"
    INSIDER_FILINGS = "INSIDER_FILINGS"
    MATERIAL_ANNOUNCEMENTS = "MATERIAL_ANNOUNCEMENTS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    MARKET_QUOTE = "MARKET_QUOTE"
    MARKET_HISTORY = "MARKET_HISTORY"


class CoverageStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"  # available, but the latest item is older than the category's bound
    MISSING = "MISSING"  # expected, sources answered, nothing found
    NOT_FOUND = "NOT_FOUND"  # not expected for every company, nothing found
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"  # no source for it answered


class CoverageItem(BaseModel):
    model_config = _model()
    category: CoverageCategory
    expected: bool
    expectation_basis: Code
    status: CoverageStatus
    count: NonNegativeInt
    latest_date: date | None = None
    age_days: int | None = None
    stale_after_days: NonNegativeInt | None = None
    providers: tuple[Provider, ...] = ()
    source_failures: tuple[Code, ...] = ()


class SourceCoverageDataset(BaseModel):
    model_config = _model()
    as_of: date
    items: tuple[CoverageItem, ...]
    #: Deliberately no composite score: the per-category states are the output.
    scoring: Literal["NONE"] = "NONE"


class TimelineEntry(BaseModel):
    model_config = _model()
    entry_id: UUID
    entry_date: date
    entry_time: AwareDatetime | None = None
    kind: Literal["EVENT", "CORPORATE_ACTION", "FILING"]
    category: Code
    title: Label
    reference_id: UUID
    source_url: Url | None = None


class TimelineWindow(BaseModel):
    model_config = _model()
    days: Literal[30, 90, 365]
    start_date: date
    end_date: date
    total: NonNegativeInt
    counts: tuple[tuple[Code, NonNegativeInt], ...]  # sorted by category


class FilingTimeline(BaseModel):
    model_config = _model()
    as_of: date
    entries: tuple[TimelineEntry, ...]  # ascending (date, time, kind, category, id)
    windows: tuple[TimelineWindow, ...]


# --- request and dataset -------------------------------------------------------------------


class ReferenceRevenue(BaseModel):
    """The latest trusted annual revenue used as the materiality denominator."""

    model_config = _model()
    fiscal_year: int = Field(ge=1990, le=2100)
    value_crore: Money = Field(gt=0)
    currency: Literal["INR", "USD", "EUR"] = "INR"
    basis: FinancialBasis
    citation: Citation

    @model_validator(mode="after")
    def single_basis(self) -> Self:
        if self.basis not in (FinancialBasis.CONSOLIDATED, FinancialBasis.STANDALONE):
            raise ValueError("Reference revenue must be consolidated or standalone")
        return self


class DocumentText(BaseModel):
    """Text already extracted (by the existing isolated document parser) from one filing's
    attachment. Order details are read from it only by fixed, anchored rules."""

    model_config = _model()
    source_url: Url
    document_sha256: Sha256Hex
    text: str = Field(max_length=MAX_DOCUMENT_TEXT)


class MarketIntelligenceRequest(BaseModel):
    model_config = _model()
    nse_symbol: NseSymbol | None = None
    bse_code: BseCode | None = None
    isin: Isin | None = None
    company_name: Label | None = None
    as_of: AwareDatetime
    benchmarks: tuple[BenchmarkSpec, ...] = (BenchmarkSpec(symbol="NIFTY 50"),)
    quote_current_seconds: int = Field(default=900, ge=1, le=86_400)
    quote_stale_days: int = Field(default=4, ge=1, le=30)
    history_stale_days: int = Field(default=4, ge=1, le=30)
    reference_revenue: ReferenceRevenue | None = None
    period_revenues: tuple[PeriodRevenue, ...] = Field(default=(), max_length=50)
    reported_order_books: tuple[ReportedOrderBook, ...] = Field(default=(), max_length=50)
    reported_inflows: tuple[ReportedInflow, ...] = Field(default=(), max_length=50)
    order_facts: tuple[OrderFact, ...] = Field(default=(), max_length=MAX_CALLER_INPUTS)
    comparable_facts: tuple[ComparableFact, ...] = Field(default=(), max_length=MAX_CALLER_INPUTS)
    document_texts: tuple[DocumentText, ...] = Field(default=(), max_length=MAX_DOCUMENT_TEXTS)
    #: Policy switch: allow GST-inclusive order values in ratios against ex-tax revenue.
    allow_tax_inclusive_order_values: bool = False

    @model_validator(mode="after")
    def identified(self) -> Self:
        if not (self.nse_symbol or self.bse_code or self.isin):
            raise ValueError("Declare at least one of NSE symbol, BSE code or ISIN")
        roles = [b.role for b in self.benchmarks]
        if roles.count(BenchmarkRole.SECTOR) > 1:
            raise ValueError("At most one sector index")
        if len({(b.exchange, b.symbol) for b in self.benchmarks}) != len(self.benchmarks):
            raise ValueError("Duplicate benchmark")
        return self


class MarketIntelligenceDataset(BaseModel):
    model_config = _model()
    schema_version: Literal["MARKET_INTELLIGENCE_DATASET_V1"] = MARKET_INTELLIGENCE_SCHEMA
    engine_version: Literal["market-intelligence/1"] = "market-intelligence/1"
    classification_version: Literal["event-rules/1"] = "event-rules/1"
    order_rules_version: Literal["order-rules/2"] = "order-rules/2"
    market_method_version: Literal["market-methods/1"] = "market-methods/1"
    dataset_id: UUID
    generated_at: AwareDatetime
    as_of: AwareDatetime
    identity: CompanyIdentity
    payloads: tuple[SourcePayloadRecord, ...] = Field(max_length=MAX_PAYLOADS)
    snapshot: MarketSnapshot | None = None
    company_history: PriceHistory | None = None
    benchmark_histories: tuple[PriceHistory, ...] = ()
    period_returns: tuple[PeriodReturn, ...] = ()
    statistics: tuple[MarketStatistic, ...] = ()
    benchmark_comparisons: tuple[BenchmarkComparison, ...] = ()
    betas: tuple[BetaEstimate, ...] = ()
    sector_index: Literal["MAPPED", "NOT_MAPPED"] = "NOT_MAPPED"
    events: tuple[CorporateEvent, ...] = Field(default=(), max_length=MAX_EVENTS)
    corporate_actions: tuple[CorporateAction, ...] = ()
    orders: tuple[OrderRecord, ...] = ()
    order_chains: tuple[OrderChain, ...] = ()
    order_book: OrderBookAnalytics | None = None
    shareholding: tuple[ShareholdingFiling, ...] = ()
    insider_trades: tuple[InsiderTrade, ...] = ()
    filings: tuple[FilingDocument, ...] = ()
    disagreements: tuple[SourceDisagreement, ...] = ()
    coverage: SourceCoverageDataset
    timeline: FilingTimeline
    issues: tuple[Issue, ...] = Field(default=(), max_length=MAX_ISSUES)
    #: Box 12 describes; it carries no rating, ranking, target or recommendation.
    recommendation: Literal["NONE"] = "NONE"

    @model_validator(mode="after")
    def lineage(self) -> Self:
        payload_ids = {p.payload_id for p in self.payloads}
        if len(payload_ids) != len(self.payloads):
            raise ValueError("Duplicate payload IDs")
        event_ids = [e.event_id for e in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("Duplicate event IDs")
        keys = [
            (e.event_date, e.event_time is None,
             e.event_time.isoformat() if e.event_time else "", str(e.event_id))
            for e in self.events
        ]
        if keys != sorted(keys):
            raise ValueError("Events must be chronological")
        known = set(event_ids)
        for event in self.events:
            if event.identity_id != self.identity.identity_id:
                raise ValueError("Event belongs to another identity")
            linked = {*event.related_event_ids}
            if event.supersedes_event_id is not None:
                linked.add(event.supersedes_event_id)
            if not linked <= known:
                raise ValueError("Event links to an unknown event")
            if any(s.payload_id not in payload_ids for s in event.sources):
                raise ValueError("Event source payload missing")
        order_ids = {o.order_id for o in self.orders}
        if len(order_ids) != len(self.orders):
            raise ValueError("Duplicate order IDs")
        if any(o.event_id not in known for o in self.orders):
            raise ValueError("Order without its event")
        seen: set[UUID] = set()
        for chain in self.order_chains:
            if not set(chain.order_ids) <= order_ids:
                raise ValueError("Chain references an unknown order")
            if seen & set(chain.order_ids):
                raise ValueError("An order belongs to exactly one chain")
            seen |= set(chain.order_ids)
        if seen != order_ids:
            raise ValueError("Every order belongs to a chain")
        for history in (self.company_history, *self.benchmark_histories):
            if history is not None and not set(history.payload_ids) <= payload_ids:
                raise ValueError("History payload missing")
        if self.snapshot is not None and self.snapshot.payload_id not in payload_ids:
            raise ValueError("Snapshot payload missing")
        return self
