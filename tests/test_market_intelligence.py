"""DETAILED_RESEARCH_V1 Box 12: market and exchange intelligence (offline).

Every payload is built in this file (shapes as observed on NSE / Kite for RVNL, values
synthetic) and every HTTP call goes through httpx.MockTransport. Expected numbers are
computed by hand (exact Decimal / Fraction arithmetic or closed forms), never with Box 12
helpers.
"""

import asyncio
import json
import random
import re
import statistics as pystats
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from stock_research_ai import logging_setup
from stock_research_ai.adapters.exchange import source as exchange_source
from stock_research_ai.adapters.exchange.source import (
    BseClient,
    ExchangeFetcher,
    KiteMarketClient,
    NseClient,
    validate_source_url,
)
from stock_research_ai.adapters.secret_store import KiteCredentials, resolve_kite_credentials
from stock_research_ai.application import market_intelligence as engine
from stock_research_ai.application.corporate_events import (
    classify,
    filter_timeline,
    single_amount,
)
from stock_research_ai.application.exchange_intelligence import (
    isin_valid,
    parse_day,
    parse_moment,
    parse_number,
)
from stock_research_ai.application.market_data import (
    beta,
    compare,
    months_before,
    parse_history,
    period_returns,
    statistics,
)
from stock_research_ai.application.source_coverage import (
    disagreements,
    screener_shareholding_facts,
)
from stock_research_ai.domain.financial import ReportingScope, ValueStatus
from stock_research_ai.domain.market_intelligence import (
    IST,
    Authority,
    AwardMode,
    BenchmarkRole,
    BenchmarkSpec,
    ChainStatus,
    Citation,
    ClientCategory,
    ComparableFact,
    CorporateActionKind,
    CoverageCategory,
    CoverageStatus,
    DisagreementStatus,
    DocumentText,
    EventStatus,
    EventType,
    FetchStatus,
    FinancialBasis,
    Freshness,
    Geography,
    IdentityStatus,
    InstrumentRef,
    MarketIntelligenceDataset,
    MarketIntelligenceRequest,
    MetricStatus,
    OrderBookMetricKind,
    OrderFact,
    OrderStage,
    PayloadKind,
    PeriodRevenue,
    PriceHistory,
    Provider,
    RawPayload,
    ReferenceRevenue,
    ReportedOrderBook,
    ReturnPeriod,
    StatisticKind,
    TaxBasis,
)
from stock_research_ai.domain.screener_dataset import (
    DetailedScreenerDataset,
    DisplayedValue,
    ScreenerSourceRef,
    ScreenerTable,
    ScreenerTableKind,
    TableCell,
    TablePeriod,
    TableRow,
)

AS_OF = datetime(2026, 10, 9, 16, 0, tzinfo=IST)
GENERATED = datetime(2026, 10, 9, 16, 5, tzinfo=IST)
ISIN = "INE415G01027"
SYMBOL = "RVNL"
BSE = "542649"
TOKEN = "1234567"
NIFTY = "256265"
NSE_API = "https://www.nseindia.com/api"
NSE_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
KITE = "https://api.kite.trade"
# Fake credentials, assembled at runtime so no secret-shaped literal sits in the source.
SECRET_KEY = "k" * 4 + "1" * 16
SECRET_TOKEN = "t" * 4 + "2" * 16
KC_KEY = "c" * 4 + "3" * 12
KC_TOKEN = "d" * 4 + "4" * 12


# --- payload factories ------------------------------------------------------------------------


def raw(
    kind: PayloadKind, body: Any, url: str | None = None, *, at: datetime = AS_OF
) -> RawPayload:
    content = body if isinstance(body, bytes) else (
        body.encode() if isinstance(body, str) else json.dumps(body).encode()
    )
    return RawPayload(kind=kind, url=url or f"{NSE_API}/{kind.value.lower()}",
                      status=FetchStatus.OK, http_status=200, retrieved_at=at, content=content)


def failed(kind: PayloadKind, status: FetchStatus, code: str) -> RawPayload:
    return RawPayload(kind=kind, url=f"{NSE_API}/{kind.value.lower()}", status=status,
                      retrieved_at=AS_OF, error_code=code)


def nse_master(*rows: tuple[str, str, str]) -> RawPayload:
    lines = ["SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, "
             "ISIN NUMBER, FACE VALUE"]
    lines += [f"{s},{n},EQ,11-APR-2019,10,1,{i},10" for s, n, i in rows]
    return raw(PayloadKind.NSE_EQUITY_MASTER, "\n".join(lines) + "\n", NSE_MASTER_URL)


def kite_master(*extra: str) -> RawPayload:
    lines = [
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
        "lot_size,instrument_type,segment,exchange",
        f"{TOKEN},4823,{SYMBOL},RAIL VIKAS NIGAM,0,,0,0.05,1,EQ,NSE,NSE",
        f"{NIFTY},1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE",
        "999,1,OTHER,OTHER CO,0,,0,0.05,1,EQ,NSE,NSE",
        *extra,
    ]
    return raw(PayloadKind.KITE_INSTRUMENTS, "\n".join(lines) + "\n", f"{KITE}/instruments/NSE")


def announcement(
    when: str, desc: str, text: str, *, seq: str, url: str | None = None,
    symbol: str = SYMBOL, isin: str = ISIN,
) -> dict[str, Any]:
    return {
        "an_dt": when, "desc": desc, "attchmntText": text, "seq_id": seq, "symbol": symbol,
        "sm_isin": isin, "sm_name": "Rail Vikas Nigam Limited",
        "attchmntFile": url or f"https://nsearchives.nseindia.com/corporate/RVNL_{seq}.pdf",
    }


def nse_announcements(*rows: dict[str, Any]) -> RawPayload:
    return raw(PayloadKind.NSE_ANNOUNCEMENTS, list(rows),
               f"{NSE_API}/corporate-announcements?index=equities&symbol=RVNL")


def bse_announcements(*rows: dict[str, Any]) -> RawPayload:
    return raw(PayloadKind.BSE_ANNOUNCEMENTS, {"Table": list(rows), "Table1": [{"ROWCNT": 1}]},
               "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1")


def bse_row(
    when: str, subject: str, news: str, *, category: str = "Company Update"
) -> dict[str, Any]:
    return {"NEWSID": news, "SCRIP_CD": int(BSE), "NEWSSUB": subject, "HEADLINE": subject,
            "CATEGORYNAME": category, "SUBCATNAME": "", "NEWS_DT": when,
            "ATTACHMENTNAME": f"{news}.pdf"}


def business_days(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def candles(points: list[tuple[date, Decimal]], *, volume: int = 1000) -> list[list[Any]]:
    return [[f"{d.isoformat()}T00:00:00+0530", str(c), str(c), str(c), str(c), volume]
            for d, c in points]


def history_payload(points: list[tuple[date, Decimal]], token: str = TOKEN, *,
                    rows: list[list[Any]] | None = None, volume: int = 1000) -> RawPayload:
    body = {"status": "success", "data": {"candles": rows if rows is not None
                                          else candles(points, volume=volume)}}
    # Raw JSON numbers, as Kite sends them.
    text = json.dumps(body).replace('"', "'")
    text = re.sub(r"'(-?\d+(?:\.\d+)?)'", r"\1", text).replace("'", '"')
    start = points[0][0] if points else date(2020, 1, 1)
    end = points[-1][0] if points else date(2020, 1, 2)
    return raw(PayloadKind.KITE_HISTORICAL, text,
               f"{KITE}/instruments/historical/{token}/day?from={start}&to={end}")


def series(days: list[date], closes: Callable[[int], Decimal]) -> list[tuple[date, Decimal]]:
    return [(d, closes(i)) for i, d in enumerate(days)]


def instrument(kind: str = "EQUITY", symbol: str = SYMBOL) -> InstrumentRef:
    return InstrumentRef.model_validate({"symbol": symbol, "exchange": "NSE", "kind": kind,
                                         "instrument_token": TOKEN})


def history(points: list[tuple[date, Decimal]], kind: str = "EQUITY", *,
            volume: int = 1000, token: str = TOKEN) -> PriceHistory:
    return parse_history([(history_payload(points, token, volume=volume), engine.payload_id(
        history_payload(points, token, volume=volume)))], instrument(kind),
        as_of=AS_OF, stale_days=4)


def quote_payload(**overrides: Any) -> RawPayload:
    entry: dict[str, Any] = {
        "instrument_token": int(TOKEN), "timestamp": "2026-10-09 15:59:00",
        "last_trade_time": "2026-10-09 15:58:58", "last_price": 412.5, "volume": 12345678,
        "average_price": 410.25, "ohlc": {"open": 405.0, "high": 415.0, "low": 401.5,
                                          "close": 404.0},
        "depth": {"buy": [{"price": 412.45, "quantity": 120, "orders": 3}],
                  "sell": [{"price": 412.5, "quantity": 80, "orders": 2}]},
    }
    entry.update(overrides)
    return raw(PayloadKind.KITE_QUOTE, {"status": "success", "data": {"NSE:RVNL": entry}},
               f"{KITE}/quote?i=NSE%3ARVNL")


def request(**kw: Any) -> MarketIntelligenceRequest:
    base: dict[str, Any] = {"nse_symbol": SYMBOL, "bse_code": BSE, "as_of": AS_OF}
    base.update(kw)
    return MarketIntelligenceRequest.model_validate(base)


def build(payloads: list[RawPayload], **kw: Any) -> MarketIntelligenceDataset:
    return engine.build_market_intelligence(request(**kw), payloads, generated_at=GENERATED)


def bse_header(code: str = BSE, isin: str = ISIN) -> RawPayload:
    return raw(PayloadKind.BSE_COMPANY_HEADER,
               {"SecurityCode": code, "SecurityId": SYMBOL, "ISIN": isin,
                "CompanyName": "Rail Vikas Nigam Ltd"},
               "https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w?scripcode=542649")


def base_payloads() -> list[RawPayload]:
    return [nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN)), kite_master(), bse_header()]


# Real RVNL disclosure wording (values synthetic where noted).
L1_TEXT = ("Rail Vikas Nigam Limited emerges as the Lowest Bidder (L1) from East Coast Railway "
           "- Normal course of business of the Company.")
LOA_TEXT = ("Disclosure under Regulation 30 of SEBI (LODR) Regulations, 2015- Letter of "
            "Acceptance from East Coast Railway")
PROJECT = "Execution of Roadbed, Minor Bridges and Other Allied Works between Khurda Road"
L1_DOC = (
    "It is hereby informed that Rail Vikas Nigam Limited emerges as the Lowest Bidder (L1) "
    f"from East Coast Railway for “{PROJECT}.”\nAnnexure-A\n"
    "Whether domestic or international; Domestic\n"
    "Time period by which the 30 Months \norder(s)/contract(s) is to be executed;\n"
    "Broad consideration or size of the Rs. 404,88,18,738/-(Rupees Four hundred four crore "
    "eighty-eight lakh eighteen thousand seven hundred thirty-eight rupees only) (Incl. GST).\n"
    "Whether the promoter/ promoter group have any interest No\n"
)
LOA_DOC = (
    "This is in continuation to our letter dated 03.09.2026 and in pursuant to Regulation 30 "
    "it is hereby informed that Rail Vikas Nigam Limited has received LOA from East Coast "
    f"Railway for “{PROJECT}.”\nS. No. Particulars Details of Projects\n"
    "c. Whether order(s) / contract(s) have been awarded by domestic/ international entity; "
    "Domestic Entity\n"
    "e. Whether domestic or international; Domestic\n"
    "f. Time period by which the \norder(s)/contract(s) is to be executed;\n"
    "g. Broad consideration or size of the \norder(s)/contract(s);\n"
    "Rs. 4048818738 (Rupees Four Hundred And Four Crore Eighty-Eight Lakh Eighteen Thousand "
    "Seven Hundred And Thirty-Eight Rupees Only) including GST\n"
    "h. Whether the promoter/ promoter group / group companies have any interest No\n912 Days"
)
L1_URL = "https://nsearchives.nseindia.com/corporate/RVNL_03092026160600_Covering.pdf"
LOA_URL = "https://nsearchives.nseindia.com/corporate/RVNL_19092026191547_Covering.pdf"
SHA = "a" * 64


def order_payloads() -> list[RawPayload]:
    return [
        *base_payloads(),
        nse_announcements(
            announcement("03-Sep-2026 16:06:07", "Updates", L1_TEXT, seq="1001", url=L1_URL),
            announcement("19-Sep-2026 19:15:59", "Bagging/Receiving of orders/contracts",
                         LOA_TEXT, seq="1002", url=LOA_URL),
            announcement("01-Jan-2025 10:00:00", "General Updates", "Newspaper publication",
                         seq="900"),
        ),
    ]


def docs(**extra: str) -> tuple[DocumentText, ...]:
    texts = {L1_URL: L1_DOC, LOA_URL: LOA_DOC, **extra}
    return tuple(DocumentText(source_url=u, document_sha256=SHA, text=t) for u, t in texts.items())


# --- identity ---------------------------------------------------------------------------------


def test_isin_check_digit_is_verified():
    assert isin_valid(ISIN)
    assert not isin_valid("INE415G01028")
    assert not isin_valid("US0378331005")  # not Indian
    assert not isin_valid("INE415G0102")


def test_identity_resolves_from_exchange_and_kite_evidence():
    data = build(base_payloads())
    identity = data.identity
    assert identity.status == IdentityStatus.RESOLVED
    assert (identity.isin, identity.nse_symbol, identity.bse_code) == (ISIN, SYMBOL, BSE)
    assert identity.kite_instrument_token == TOKEN
    assert identity.name == "Rail Vikas Nigam Limited"
    providers = {(e.field.value, e.provider.value) for e in identity.evidence}
    expected = {("ISIN", "NSE"), ("ISIN", "BSE"), ("KITE_TOKEN", "KITE"), ("BSE_CODE", "BSE")}
    assert expected <= providers
    # 'Ltd' and 'Limited' are the same name; a real spelling difference is informational only.
    assert "NAME_VARIANT" not in {i.code for i in identity.issues}
    variant = build([nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN)), kite_master(),
                     raw(PayloadKind.BSE_COMPANY_HEADER,
                         {"SecurityCode": BSE, "ISIN": ISIN, "CompanyName": "R V N L"},
                         "https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w")])
    assert variant.identity.status == IdentityStatus.RESOLVED
    assert "NAME_VARIANT" in {i.code for i in variant.identity.issues}


def test_identity_is_partial_when_bse_code_is_unconfirmed():
    data = build([nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN)), kite_master()])
    assert data.identity.status == IdentityStatus.PARTIAL
    assert "IDENTITY_FIELD_UNCONFIRMED" in {i.code for i in data.identity.issues}


def test_conflicting_isin_between_exchanges_fails_closed():
    other = "INE002A01018"
    assert isin_valid(other)
    data = build([
        nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN)), kite_master(),
        bse_header(isin=other),
        nse_announcements(announcement("01-Oct-2026 10:00:00", "Dividend", "Dividend", seq="1")),
    ])
    assert data.identity.status == IdentityStatus.AMBIGUOUS
    assert data.identity.isin is None and data.identity.kite_instrument_token is None
    assert data.events == () and data.snapshot is None
    assert "IDENTITY_NOT_RESOLVED" in {i.code for i in data.issues}
    assert all(i.status == CoverageStatus.SOURCE_UNAVAILABLE for i in data.coverage.items)


def test_duplicate_listing_in_master_is_ambiguous():
    data = build([nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN),
                             (SYMBOL, "Rail Vikas Nigam Limited", "INE002A01018"))],
                 bse_code=None)
    assert data.identity.status == IdentityStatus.AMBIGUOUS


def test_ambiguous_kite_instrument_fails_closed():
    dup = f"7654321,4824,{SYMBOL},RAIL VIKAS NIGAM,0,,0,0.05,1,EQ,NSE,NSE"
    data = build([nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN)), kite_master(dup)],
                 bse_code=None)
    assert data.identity.status == IdentityStatus.AMBIGUOUS
    assert "AMBIGUOUS_KITE_INSTRUMENT" in {i.code for i in data.identity.issues}


def test_no_fuzzy_name_matching():
    # A company name alone, or a near-miss symbol, never resolves an identity.
    data = build([nse_master(("RVNLX", "Rail Vikas Nigam Limited", ISIN))], bse_code=None,
                 company_name="Rail Vikas Nigam Limited")
    assert data.identity.status == IdentityStatus.FAILED
    assert data.events == ()


def test_declared_isin_with_bad_check_digit_fails():
    data = build(base_payloads(), isin="INE415G01028")
    assert data.identity.status == IdentityStatus.FAILED
    assert "INVALID_ISIN_CHECK_DIGIT" in {i.code for i in data.identity.issues}


def test_declared_isin_conflicting_with_exchange_is_ambiguous():
    data = build(base_payloads(), isin="INE002A01018")
    assert data.identity.status == IdentityStatus.AMBIGUOUS


def test_rows_of_other_securities_are_dropped():
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 10:00:00", "Dividend", "Dividend Rs 1 per share", seq="1"),
        announcement("01-Oct-2026 11:00:00", "Dividend", "Other company", seq="2",
                     symbol="IRCON", isin="INE962Y01021"),
    )])
    assert len(data.events) == 1
    assert "ROW_IDENTITY_MISMATCH" in {i.code for i in data.issues}


def test_request_requires_a_key_and_validates_symbols():
    with pytest.raises(ValidationError):
        MarketIntelligenceRequest(as_of=AS_OF)
    for bad in ("rvnl", "RV NL", "RVNL;DROP", "A" * 30, "../x"):
        with pytest.raises(ValidationError):
            request(nse_symbol=bad)
    with pytest.raises(ValidationError):
        request(bse_code="54264")
    with pytest.raises(ValidationError):
        request(benchmarks=(BenchmarkSpec(symbol="NIFTY 50"), BenchmarkSpec(symbol="NIFTY 50")))
    with pytest.raises(ValidationError):  # a sector index needs an explicit cited mapping
        BenchmarkSpec(symbol="NIFTY INFRA", role=BenchmarkRole.SECTOR)


# --- quote ------------------------------------------------------------------------------------


def test_quote_snapshot_is_dated_and_sourced():
    data = build([*base_payloads(), quote_payload()])
    snap = data.snapshot
    assert snap is not None
    assert snap.last_price == Decimal("412.5") and snap.previous_close == Decimal("404.0")
    assert (snap.open, snap.high, snap.low) == (Decimal("405.0"), Decimal("415.0"),
                                                Decimal("401.5"))
    assert snap.volume == Decimal(12345678) and snap.bid == Decimal("412.45")
    assert snap.ask == Decimal("412.5") and snap.depth_available
    assert snap.exchange_timestamp == datetime(2026, 10, 9, 15, 59, tzinfo=IST)
    assert snap.market_date == date(2026, 10, 9)
    assert snap.freshness == Freshness.CURRENT and snap.age_seconds == 60
    assert (snap.source, snap.authority) == (Provider.KITE, Authority.MARKET_PROVIDER)
    assert snap.observed_at == AS_OF and snap.instrument.instrument_token == TOKEN


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-10-09 15:55:00", Freshness.CURRENT),
        ("2026-10-08 15:30:00", Freshness.DELAYED),
        ("2026-09-30 15:30:00", Freshness.STALE),
        ("2026-10-10 15:30:00", Freshness.UNKNOWN),  # in the future
        ("not a time", Freshness.UNKNOWN),
    ],
)
def test_quote_freshness(stamp, expected):
    data = build([*base_payloads(), quote_payload(timestamp=stamp, last_trade_time=None)])
    assert data.snapshot is not None and data.snapshot.freshness == expected
    if expected == Freshness.STALE:
        assert "STALE_QUOTE" in {i.code for i in data.snapshot.issues}
        cov = {i.category: i for i in data.coverage.items}[CoverageCategory.MARKET_QUOTE]
        assert cov.status == CoverageStatus.STALE


def test_zero_ohlc_is_unavailable_not_zero():
    data = build([*base_payloads(), quote_payload(ohlc={"open": 0, "high": 0, "low": 0,
                                                        "close": 0}, depth={})])
    snap = data.snapshot
    assert snap is not None
    assert (snap.open, snap.high, snap.low, snap.previous_close) == (None, None, None, None)
    assert "ZERO_TREATED_AS_UNAVAILABLE" in {i.code for i in snap.issues}
    assert not snap.depth_available and snap.bid is None


@pytest.mark.parametrize(
    "body",
    [
        {"status": "error", "message": "Incorrect api_key or access_token.",
         "error_type": "TokenException"},
        {"status": "success", "data": {"NSE:OTHER": {"last_price": 1}}},
        {"status": "success", "data": {"NSE:RVNL": {"last_price": 0}}},
        {"status": "success", "data": {"NSE:RVNL": {"last_price": "abc"}}},
        {"status": "success", "data": {"NSE:RVNL": {"last_price": 10, "instrument_token": 99}}},
    ],
)
def test_bad_quotes_never_become_snapshots(body):
    data = build([*base_payloads(), raw(PayloadKind.KITE_QUOTE, body, f"{KITE}/quote?i=x")])
    assert data.snapshot is None
    assert data.issues


def test_malformed_quote_json():
    data = build([*base_payloads(), raw(PayloadKind.KITE_QUOTE, b"{not json", f"{KITE}/quote")])
    assert data.snapshot is None
    assert "INVALID_SOURCE_DATA" in {i.code for i in data.issues}


# --- history and statistics -------------------------------------------------------------------


def test_candles_parse_with_provenance():
    days = business_days(date(2026, 9, 1), date(2026, 10, 9))
    h = history(series(days, lambda i: Decimal(100 + i)))
    assert len(h.candles) == len(days) and h.latest == date(2026, 10, 9)
    assert h.freshness == Freshness.CURRENT and h.lag_days == 0
    assert h.adjustment_basis == "PROVIDER_DEFINED_UNVERIFIED"
    assert h.candles[0].locator == "$.data.candles[0]"


def test_conflicting_duplicate_candles_are_excluded_identical_kept_once():
    d1, d2 = date(2026, 10, 8), date(2026, 10, 9)
    first = history_payload([(d1, Decimal(10)), (d2, Decimal(11))])
    again = history_payload([(d1, Decimal(10)), (d2, Decimal(12))])
    h = parse_history([(first, engine.payload_id(first)), (again, engine.payload_id(again))],
                      instrument(), as_of=AS_OF, stale_days=4)
    assert [c.trading_date for c in h.candles] == [d1]
    assert "CONFLICTING_DUPLICATE_CANDLE" in {i.code for i in h.issues}


@pytest.mark.parametrize(
    "row",
    [
        ["2026-10-09T00:00:00+0000", 1, 1, 1, 1, 1],  # wrong offset
        ["2026-10-09T09:15:00+0530", 1, 1, 1, 1, 1],  # intraday, not daily
        ["2026-10-09T00:00:00+0530", 0, 1, 1, 1, 1],  # zero price
        ["2026-10-09T00:00:00+0530", -1, 1, 1, 1, 1],
        ["2026-10-09T00:00:00+0530", 1, 0.5, 1, 1, 1],  # high below low
        ["2026-10-09T00:00:00+0530", 1, 1, 1, 1, -5],  # negative volume
        ["2026-10-09T00:00:00+0530", "1e999", 1, 1, 1, 1],
        ["31-02-2026", 1, 1, 1, 1, 1],
        "not a list",
        ["2026-10-09T00:00:00+0530", 1, 1],
    ],
)
def test_malformed_candles_are_rejected(row):
    good = ["2026-10-08T00:00:00+0530", 5, 5, 5, 5, 10]
    payload = raw(PayloadKind.KITE_HISTORICAL,
                  {"status": "success", "data": {"candles": [good, row]}},
                  f"{KITE}/instruments/historical/{TOKEN}/day")
    h = parse_history([(payload, engine.payload_id(payload))], instrument(), as_of=AS_OF,
                      stale_days=4)
    assert [c.trading_date for c in h.candles] == [date(2026, 10, 8)]
    assert "INVALID_CANDLE" in {i.code for i in h.issues}


def test_future_candles_and_stale_history():
    future = history([(date(2026, 10, 9), Decimal(1)), (date(2026, 10, 12), Decimal(2))])
    assert future.latest == date(2026, 10, 9)
    assert "FUTURE_CANDLE" in {i.code for i in future.issues}
    stale = history([(date(2026, 9, 1), Decimal(1)), (date(2026, 9, 2), Decimal(2))])
    assert stale.freshness == Freshness.STALE and stale.lag_days == 37


def test_months_before_clamps_month_ends():
    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_before(date(2024, 3, 31), 1) == date(2024, 2, 29)
    assert months_before(date(2026, 10, 9), 120) == date(2016, 10, 9)


def test_period_returns_and_cagr_by_hand():
    end = date(2026, 10, 9)
    points = [
        (date(2016, 10, 7), Decimal(10)),   # 10Y target 2016-10-09 (Sunday) -> Friday
        (date(2021, 10, 8), Decimal(40)),   # 5Y target 2021-10-09 (Saturday)
        (date(2023, 10, 9), Decimal(50)),   # 3Y
        (date(2025, 10, 9), Decimal(100)),  # 1Y
        (date(2026, 4, 9), Decimal(110)),   # 6M
        (date(2026, 7, 9), Decimal(120)),   # 3M
        (date(2026, 9, 9), Decimal(125)),   # 1M
        (date(2026, 10, 2), Decimal(140)),  # 1W
        (date(2026, 10, 8), Decimal(144)),  # 1D
        (end, Decimal(150)),
    ]
    r = {p.period: p for p in period_returns(history(points))}
    expect = {"1D": (144, 150), "1W": (140, 150), "1M": (125, 150), "3M": (120, 150),
              "6M": (110, 150), "1Y": (100, 150), "3Y": (50, 150), "5Y": (40, 150),
              "10Y": (10, 150)}
    for period, (a, b) in expect.items():
        got = r[ReturnPeriod(period)]
        assert got.status == MetricStatus.AVAILABLE
        exact = (Fraction(b, a) - 1) * 100
        assert got.absolute_return_pct == Decimal(exact.numerator) / Decimal(exact.denominator) \
            or abs(got.absolute_return_pct - Decimal(float(exact))) < Decimal("0.000001")
    three = r[ReturnPeriod.Y3]
    assert three.elapsed_days == 1096
    assert abs(float(three.cagr_pct) - ((3 ** (365 / 1096)) - 1) * 100) < 1e-5
    assert r[ReturnPeriod.Y1].cagr_pct == Decimal("50.000000")
    assert r[ReturnPeriod.M1].cagr_pct is None  # no CAGR below one year
    assert r[ReturnPeriod.Y10].start_date == date(2016, 10, 7)


def test_returns_unavailable_without_history_or_across_gaps():
    points = [(date(2025, 9, 1), Decimal(10)), (date(2026, 10, 9), Decimal(20))]
    r = {p.period: p for p in period_returns(history(points))}
    assert r[ReturnPeriod.Y1].reason == "HISTORY_GAP_AT_START"
    assert r[ReturnPeriod.Y3].reason == "INSUFFICIENT_HISTORY"
    assert r[ReturnPeriod.Y3].absolute_return_pct is None  # never zero
    assert r[ReturnPeriod.D1].status == MetricStatus.AVAILABLE


def test_52_week_range_drawdown_and_distances():
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    closes = {d: Decimal(100) for d in days}
    closes[date(2026, 2, 2)] = Decimal(120)  # peak
    closes[date(2026, 3, 2)] = Decimal(90)  # trough after the peak
    closes[date(2025, 9, 1)] = Decimal(500)  # outside the 52-week window
    points = [(d, closes[d]) for d in days]
    stats = {s.kind: s for s in statistics(history(points))}
    assert stats[StatisticKind.HIGH_52W].value == 120
    assert stats[StatisticKind.HIGH_52W].reference_date == date(2026, 2, 2)
    assert stats[StatisticKind.LOW_52W].value == 90
    assert stats[StatisticKind.DISTANCE_FROM_HIGH_52W].value == Decimal("-16.666667")
    assert stats[StatisticKind.DISTANCE_FROM_LOW_52W].value == Decimal("11.111111")
    dd = stats[StatisticKind.MAX_DRAWDOWN_1Y]
    assert dd.value == Decimal("25.000000")
    assert (dd.reference_date, dd.second_date) == (date(2026, 2, 2), date(2026, 3, 2))
    full = stats[StatisticKind.MAX_DRAWDOWN_FULL_HISTORY]
    assert full.value == Decimal("82.000000") and full.reference_date == date(2025, 9, 1)


def test_statistics_unavailable_on_short_history():
    days = business_days(date(2026, 6, 1), date(2026, 10, 9))
    stats = {s.kind: s for s in statistics(history(series(days, lambda i: Decimal(100 + i))))}
    for kind in (StatisticKind.HIGH_52W, StatisticKind.MAX_DRAWDOWN_1Y,
                 StatisticKind.ANNUALIZED_VOLATILITY_1Y, StatisticKind.AVERAGE_VOLUME_252D):
        assert stats[kind].status == MetricStatus.UNAVAILABLE and stats[kind].value is None
    assert stats[StatisticKind.AVERAGE_VOLUME_20D].status == MetricStatus.AVAILABLE


def test_volatility_matches_independent_sample_stdev():
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    rng = random.Random(7)
    closes = [Decimal(100)]
    for _ in days[1:]:
        closes.append((closes[-1] * Decimal(str(1 + rng.uniform(-0.03, 0.03)))).quantize(
            Decimal("0.01")))
    points = list(zip(days, closes, strict=True))
    stats = {s.kind: s for s in statistics(history(points))}
    window = [c for d, c in points if d >= date(2025, 10, 9)]
    rets = [float(b / a - 1) for a, b in zip(window, window[1:], strict=False)]
    expected = pystats.stdev(rets) * 252 ** 0.5 * 100
    vol = stats[StatisticKind.ANNUALIZED_VOLATILITY_1Y]
    assert vol.observations == len(rets) >= 200
    assert abs(float(vol.value) - expected) < 1e-6


def test_constant_prices_have_zero_volatility_and_drawdown():
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    stats = {s.kind: s for s in statistics(history(series(days, lambda i: Decimal(50))))}
    assert stats[StatisticKind.ANNUALIZED_VOLATILITY_1Y].value == 0
    assert stats[StatisticKind.MAX_DRAWDOWN_1Y].value == 0


def test_liquidity_averages():
    days = business_days(date(2026, 8, 1), date(2026, 10, 9))
    stats = {s.kind: s for s in statistics(history(series(days, lambda i: Decimal(200)),
                                                   volume=500_000))}
    assert stats[StatisticKind.AVERAGE_VOLUME_20D].value == Decimal(500_000)
    # 200 x 500,000 = 10 crore
    assert stats[StatisticKind.AVERAGE_TRADED_VALUE_20D].value == Decimal("10.000000")
    assert "APPROXIMATION" in stats[StatisticKind.AVERAGE_TRADED_VALUE_20D].method


def test_index_volume_is_not_applicable():
    days = business_days(date(2026, 8, 1), date(2026, 10, 9))
    h = history(series(days, lambda i: Decimal(25000)), kind="INDEX", token=NIFTY)
    stats = {s.kind: s for s in statistics(h)}
    assert stats[StatisticKind.AVERAGE_VOLUME_20D].reason == "VOLUME_NOT_APPLICABLE"
    assert all(c.volume is None for c in h.candles)


def test_benchmark_comparison_by_hand():
    days = [date(2025, 10, 9), date(2026, 10, 8), date(2026, 10, 9)]
    company = history([(days[0], Decimal(100)), (days[1], Decimal(140)), (days[2], Decimal(150))])
    bench = history([(days[0], Decimal(20000)), (days[1], Decimal(22000)),
                     (days[2], Decimal(24000))], kind="INDEX", token=NIFTY)
    rows = {c.period: c for c in compare(company, bench, BenchmarkRole.BROAD_MARKET)}
    one = rows[ReturnPeriod.Y1]
    assert (one.company_return_pct, one.benchmark_return_pct) == (Decimal("50.000000"),
                                                                  Decimal("20.000000"))
    assert one.excess_return_pp == Decimal("30.000000")
    assert one.relative_performance_pct == Decimal("25.000000")  # 1.5 / 1.2 - 1
    assert rows[ReturnPeriod.Y3].status == MetricStatus.UNAVAILABLE


def test_beta_uses_documented_methodology_and_matches_independent_estimate():
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    rng = random.Random(3)
    rets = [Decimal(str(round(rng.uniform(-0.02, 0.02), 4))) for _ in days[1:]]
    b, c = [Decimal(1000)], [Decimal(100)]
    q = Decimal("0.01")  # exchange prices have two decimals
    for r in rets:
        b.append((b[-1] * (1 + r)).quantize(q))
        c.append((c[-1] * (1 + 2 * r)).quantize(q))
    company = history(list(zip(days, c, strict=True)))
    bench = history(list(zip(days, b, strict=True)), kind="INDEX", token=NIFTY)
    estimate = beta(company, bench)
    assert estimate.status == MetricStatus.AVAILABLE
    pairs = [(c[i - 1], c[i], b[i - 1], b[i]) for i in range(1, len(days))
             if days[i - 1] >= date(2025, 10, 9)]
    ys = [float(c1 / c0 - 1) for c0, c1, _, _ in pairs]
    xs = [float(b1 / b0 - 1) for _, _, b0, b1 in pairs]
    expected = pystats.covariance(xs, ys) / pystats.variance(xs)
    assert abs(float(estimate.value) - expected) < 1e-6
    assert abs(expected - 2) < 0.01
    m = estimate.methodology
    assert (m.frequency, m.lookback_days, m.minimum_observations) == ("DAILY", 365, 200)
    assert m.estimator == "SAMPLE_COVARIANCE_OVER_SAMPLE_VARIANCE"
    assert estimate.observations >= 200


def test_beta_unavailable_with_too_few_observations():
    days = business_days(date(2026, 6, 1), date(2026, 10, 9))
    company = history(series(days, lambda i: Decimal(100 + i)))
    bench = history(series(days, lambda i: Decimal(1000 + 3 * i)), kind="INDEX", token=NIFTY)
    estimate = beta(company, bench)
    assert estimate.status == MetricStatus.UNAVAILABLE and estimate.value is None
    assert estimate.reason == "INSUFFICIENT_OBSERVATIONS"


def test_beta_skips_sessions_missing_in_either_series():
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    company = history(series(days, lambda i: Decimal(100 + i % 7)))
    bench = history(series([d for i, d in enumerate(days) if i % 3], lambda i: Decimal(
        1000 + i % 5)), kind="INDEX", token=NIFTY)
    estimate = beta(company, bench)
    # every third session is absent from the benchmark: those returns are not paired
    assert estimate.observations < len([d for d in days if d >= date(2025, 10, 9)]) * 2 // 3


def test_market_data_in_full_build():
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    payloads = [*base_payloads(), quote_payload(),
                history_payload(series(days, lambda i: Decimal(100) + Decimal(i) / 10)),
                history_payload(series(days, lambda i: Decimal(20000) + i), NIFTY)]
    data = build(payloads)
    assert data.company_history is not None and data.company_history.latest == date(2026, 10, 9)
    assert len(data.benchmark_histories) == 1
    assert {c.benchmark.symbol for c in data.benchmark_comparisons} == {"NIFTY 50"}
    assert data.betas[0].benchmark.instrument_token == NIFTY
    assert data.sector_index == "NOT_MAPPED"
    cov = {i.category: i for i in data.coverage.items}
    assert cov[CoverageCategory.MARKET_HISTORY].status == CoverageStatus.AVAILABLE


def test_sector_index_only_when_explicitly_mapped():
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    infra = "261129"
    payloads = [nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN)), bse_header(),
                kite_master(f"{infra},1002,NIFTY INFRA,NIFTY INFRA,0,,0,0,0,EQ,INDICES,NSE"),
                history_payload(series(days, lambda i: Decimal(100) + i)),
                history_payload(series(days, lambda i: Decimal(9000) + i), infra)]
    cite = Citation(provider=Provider.CALLER, authority=Authority.EXPLICIT_INPUT,
                    reference="analyst sector mapping")
    data = build(payloads, benchmarks=(BenchmarkSpec(symbol="NIFTY INFRA",
                                                     role=BenchmarkRole.SECTOR,
                                                     mapping_citation=cite),))
    assert data.sector_index == "MAPPED"
    assert {c.role for c in data.benchmark_comparisons} == {BenchmarkRole.SECTOR}


def test_unattributed_history_is_ignored():
    data = build([*base_payloads(),
                  history_payload([(date(2026, 10, 9), Decimal(5))], token="555")])
    assert data.company_history is None
    assert {"UNATTRIBUTED_HISTORY_PAYLOAD", "NO_COMPANY_HISTORY"} <= {i.code for i in data.issues}


# --- events -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "text", "expected"),
    [
        ("Updates", L1_TEXT, EventType.L1_BIDDER),
        ("Bagging/Receiving of orders/contracts", LOA_TEXT, EventType.LOA),
        ("Bagging/Receiving of orders/contracts",
         "Cancellation of Letter of Award received from East Coast Railway",
         EventType.ORDER_CANCELLATION),
        ("Updates", "Revision in the value of the contract awarded by NMDC",
         EventType.ORDER_REVISION),
        ("Awarding orders/contract", "Receipt of work order from Central Organisation",
         EventType.ORDER_AWARD),
        ("Clarification", "The Exchange has sought clarification with respect to announcement "
         "regarding receipt of order", EventType.OTHER_MATERIAL_EVENT),
        ("Trading Window", "Trading Window closure pursuant to SEBI (Prohibition of Insider "
         "Trading)", EventType.OTHER_MATERIAL_EVENT),
        ("Copy of Newspaper Publication", "Transfer of Unpaid Dividend to IEPF Authority",
         EventType.OTHER_MATERIAL_EVENT),
        ("Financial Result Updates", "Audited financial results", EventType.RESULTS),
        ("Change in Auditors", "Change in Auditors of the company", EventType.AUDITOR_CHANGE),
        ("Change in Management", "Appointment of Chief Financial Officer", EventType.KMP_CHANGE),
        ("Change in Director(s)", "Appointment of Director", EventType.BOARD_CHANGE),
        ("Credit Rating", "Assignment of Credit Rating", EventType.CREDIT_RATING),
        ("Action(s) taken or orders passed", "Imposition of fine by NSE for the quarter",
         EventType.EXCHANGE_PENALTY),
        ("Action(s) taken or orders passed", "Demand Notice received for GST demand order",
         EventType.REGULATORY),
        ("Dividend", "Interim dividend of Rs 1 per share", EventType.DIVIDEND),
        ("Bonus", "Bonus issue 1:1", EventType.BONUS),
        ("Updates", "Sub-division of equity shares", EventType.SPLIT),
        ("Updates", "Rights issue of equity shares", EventType.RIGHTS),
        ("Buy back", "Buyback of equity shares", EventType.BUYBACK),
        ("Analysts/Institutional Investor Meet/Con. Call Updates", "Transcript of con call",
         EventType.CONCALL),
        ("Investor Presentation", "Investor presentation Q1", EventType.INVESTOR_PRESENTATION),
        ("Shareholders meeting", "Annual General Meeting notice", EventType.AGM_EGM),
        ("Postal Ballot", "Postal ballot notice", EventType.POSTAL_BALLOT),
        ("Disclosure under SEBI Takeover Regulations", "Disclosure", EventType.SHAREHOLDING),
        ("Updates", "Signed MoU with Visakhapatnam Port Authority", EventType.MOU),
        ("Updates", "Formation of Joint Venture Company", EventType.JV),
        ("Updates", "Acquisition of 51% stake", EventType.ACQUISITION),
        ("Updates", "Issue of non-convertible debentures", EventType.FUND_RAISE),
        ("Updates", "Arbitration award in dispute", EventType.LITIGATION),
        ("Updates", "Bulk deal disclosure", EventType.BULK_DEAL),
        ("Copy of Newspaper Publication", "Newspaper publication", EventType.OTHER_MATERIAL_EVENT),
    ],
)
def test_classification_rules(category, text, expected):
    kind, rule = classify(category, text, None)
    assert kind == expected and rule


def test_classification_never_reads_adjectives_as_types():
    kind, rule = classify("Updates", "A major, significant and strategic milestone", None)
    assert (kind, rule) == (EventType.OTHER_MATERIAL_EVENT, "UNMATCHED")


def test_event_ledger_fields_and_chronology():
    data = build([*base_payloads(), nse_announcements(
        announcement("05-Oct-2026 10:00:00", "Dividend", "Interim dividend of Rs 1.50 per share",
                     seq="3"),
        announcement("01-Oct-2026 18:54:20", "Bagging/Receiving of orders/contracts",
                     "Letter of Award from SJVN Thermal Private Limited worth Rs. 903.01 crore "
                     "(Incl. GST). A major and significant order.", seq="2"),
        announcement("01-Oct-2026 09:00:00", "Financial Result Updates",
                     "Standalone and consolidated financial results", seq="1"),
    )])
    events = data.events
    assert [e.event_date for e in events] == sorted(e.event_date for e in events)
    assert [e.document_reference for e in events] == ["1", "2", "3"]
    order = events[1]
    assert order.event_type == EventType.LOA
    assert order.financial_value_crore == Decimal("903.01")
    assert order.value_currency == "INR" and order.value_tax_basis == TaxBasis.INCLUSIVE_OF_TAX
    assert order.counterparty == "SJVN Thermal Private Limited"
    assert "major and significant" in (order.management_wording or "")
    assert order.authority == Authority.PRIMARY_REGULATORY
    assert order.event_time == datetime(2026, 10, 1, 18, 54, 20, tzinfo=IST)
    assert order.sources[0].record_id == "2" and order.exchanges == ("NSE",)
    assert events[0].basis == FinancialBasis.BOTH
    assert order.basis == FinancialBasis.NOT_APPLICABLE
    assert order.materiality is not None and order.materiality.reason == "NO_REFERENCE_REVENUE"


@pytest.mark.parametrize(
    ("text", "basis"),
    [
        ("Consolidated financial results", FinancialBasis.CONSOLIDATED),
        ("Standalone financial results", FinancialBasis.STANDALONE),
        ("Financial results (standalone & consolidated)", FinancialBasis.BOTH),
        ("Financial results", FinancialBasis.UNKNOWN),
    ],
)
def test_basis_metadata_is_preserved_never_mixed(text, basis):
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 09:00:00", "Financial Result Updates", text, seq="1"))])
    assert data.events[0].basis == basis


def test_materiality_is_deterministic_value_over_cited_revenue():
    ref = ReferenceRevenue(fiscal_year=2026, value_crore=Decimal("20000"),
                           basis=FinancialBasis.CONSOLIDATED,
                           citation=Citation(provider=Provider.CALLER,
                                             authority=Authority.EXPLICIT_INPUT,
                                             reference="Box 4 FY26 consolidated revenue"))
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 18:54:20", "Bagging/Receiving of orders/contracts",
                     "Letter of Award from NMDC Limited worth Rs. 500 crore", seq="2"),
        announcement("02-Oct-2026 18:54:20", "Bagging/Receiving of orders/contracts",
                     "Letter of Award from NMDC Limited - a major, strategic order", seq="3"),
    )], reference_revenue=ref)
    valued, wordy = data.events
    assert valued.materiality is not None
    assert valued.materiality.ratio_pct == Decimal("2.500000")
    assert valued.materiality.denominator_basis == FinancialBasis.CONSOLIDATED
    assert "NUMERATOR_TAX_BASIS_UNKNOWN" in valued.materiality.issues
    assert wordy.materiality is None and wordy.financial_value_crore is None


@pytest.mark.parametrize(
    ("text", "crore", "ambiguous"),
    [
        ("Rs. 404,88,18,738/-", Decimal("404.8818738"), False),
        ("Rs. 4048818738 including GST", Decimal("404.8818738"), False),
        ("worth ₹ 1,234.5 Cr", Decimal("1234.5"), False),
        ("INR 25 lakh", Decimal("0.25"), False),
        ("USD 10 million", Decimal("10000000") / Decimal("10000000") * 1, False),
        ("Rs. 100 crore and Rs. 200 crore", None, True),
        ("Rs. 100 crore (Rs. 100 crore)", Decimal("100"), False),
        ("value not disclosed", None, False),
        ("Rs. 0 crore", None, False),
        ("Rs. 9" + "9" * 30 + " crore", None, False),
    ],
)
def test_stated_amounts(text, crore, ambiguous):
    found, is_ambiguous = single_amount(text)
    assert is_ambiguous == ambiguous
    assert (found.crore if found else None) == crore


def test_same_record_twice_is_one_event_and_conflicts_are_flagged():
    row = announcement("01-Oct-2026 10:00:00", "Dividend", "Dividend Rs 1 per share", seq="7")
    changed = {**row, "attchmntText": "Dividend Rs 2 per share"}
    first = nse_announcements(row)
    second = raw(PayloadKind.NSE_ANNOUNCEMENTS, [row, changed], f"{NSE_API}/page2")
    data = build([*base_payloads(), first, second])
    assert len(data.events) == 1
    assert "CONFLICTING_DUPLICATE_RECORD" in {i.code for i in data.issues}


def test_cross_exchange_filing_is_one_event_with_both_sources():
    subject = "Letter of Award from NMDC Limited worth Rs. 500 crore"
    data = build([*base_payloads(),
                  nse_announcements(announcement("01-Oct-2026 18:54:20",
                                                 "Bagging/Receiving of orders/contracts",
                                                 subject, seq="11")),
                  bse_announcements(bse_row("2026-10-01T18:55:02.13",
                                            "Letter of Award from NMDC Limited worth Rs. 500 "
                                            "crore", "abc-1"))])
    assert len(data.events) == 1
    event = data.events[0]
    assert event.exchanges == ("BSE", "NSE")
    assert {s.provider for s in event.sources} == {Provider.NSE, Provider.BSE}
    assert len(data.order_chains) == 1  # counted once


def test_explicit_revision_supersedes_and_keeps_the_original():
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 10:00:00", "Shareholders meeting", "Notice of AGM", seq="1"),
        announcement("03-Oct-2026 10:00:00", "Shareholders meeting",
                     "Corrigendum to notice of AGM in continuation of our letter dated "
                     "01.10.2026", seq="2"),
    )])
    original, revision = data.events
    assert original.status == EventStatus.SUPERSEDED
    assert revision.supersedes_event_id == original.event_id
    assert revision.event_id in original.related_event_ids


def test_revision_without_reference_supersedes_nothing():
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 10:00:00", "Shareholders meeting", "Notice of AGM", seq="1"),
        announcement("03-Oct-2026 10:00:00", "Shareholders meeting", "Revised notice of AGM",
                     seq="2"),
    )])
    assert all(e.status == EventStatus.ACTIVE for e in data.events)


def test_untrusted_attachment_urls_are_dropped():
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 10:00:00", "Dividend", "Dividend", seq="1",
                     url="https://127.0.0.1/x.pdf"),
        announcement("01-Oct-2026 11:00:00", "Dividend", "Dividend 2", seq="2",
                     url="javascript:alert(1)"),
        announcement("01-Oct-2026 12:00:00", "Dividend", "Dividend 3", seq="3",
                     url="https://evil.example.com/x.pdf"),
    )])
    assert all(e.source_url is None for e in data.events)
    assert sum(i.code == "UNTRUSTED_SOURCE_URL" for i in data.issues) == 3


def test_malformed_dates_are_dropped_not_guessed():
    data = build([*base_payloads(), nse_announcements(
        announcement("31-Feb-2026 10:00:00", "Dividend", "x", seq="1"),
        announcement("01-Oct-1890 10:00:00", "Dividend", "y", seq="2"),
        announcement("yesterday", "Dividend", "z", seq="3"),
    )])
    assert data.events == ()
    assert "MALFORMED_DATE" in {i.code for i in data.issues}
    assert parse_day("20-JUL-2026") == date(2026, 7, 20)
    assert parse_moment("2026-10-07T12:01:48.68") == datetime(2026, 10, 7, 12, 1, 48, tzinfo=IST)
    assert parse_day("9" * 500) is None
    assert parse_number("1,23,456.50") == Decimal("123456.50")
    assert parse_number("1e9") is None and parse_number(True) is None


def test_long_text_is_bounded():
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 10:00:00", "Updates" * 400, "x" * 50_000, seq="1"))])
    event = data.events[0]
    assert len(event.title) <= 1000 and len(event.summary or "") <= 8000


# --- corporate actions -----------------------------------------------------------------------


def nse_actions(*rows: dict[str, Any]) -> RawPayload:
    return raw(PayloadKind.NSE_CORPORATE_ACTIONS, list(rows), f"{NSE_API}/ca")


def action(subject: str, ex: str, rec: str | None = None) -> dict[str, Any]:
    return {"symbol": SYMBOL, "isin": ISIN, "subject": subject, "exDate": ex,
            "recDate": rec or ex, "bcStartDate": "-", "bcEndDate": "-", "faceVal": "10",
            "caBroadcastDate": None}


def test_corporate_actions_keep_every_date_and_never_adjust():
    data = build([*base_payloads(), nse_actions(
        action("Dividend - Re 0.71 Per Share", "18-Aug-2026"),
        action("Bonus 1:2", "01-Jun-2026"),
        action("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share",
               "02-Feb-2026"),
        action("Rights 1:5 @ Premium Rs 50/-", "03-Mar-2026"),
        action("Final Dividend - Rs 1.50 Per Share + Special Dividend - Rs 1 Per Share",
               "04-Apr-2026"),
        action("Buy Back of shares", "05-May-2026"),
    )])
    kinds = {a.kind: a for a in data.corporate_actions}
    dividend = next(a for a in data.corporate_actions if a.ex_date == date(2026, 8, 18))
    assert dividend.amount_per_share == Decimal("0.71") and dividend.currency == "INR"
    assert dividend.record_date == date(2026, 8, 18) and dividend.price_adjustment == "NOT_APPLIED"
    assert kinds[CorporateActionKind.BONUS].ratio == "1:2"
    split = kinds[CorporateActionKind.SPLIT]
    assert (split.face_value_from, split.face_value_to) == (Decimal(10), Decimal(2))
    assert kinds[CorporateActionKind.RIGHTS].ratio == "1:5"
    assert CorporateActionKind.BUYBACK in kinds
    double = next(a for a in data.corporate_actions if a.ex_date == date(2026, 4, 4))
    assert double.amount_per_share is None
    assert "MULTIPLE_AMOUNTS" in {i.code for i in double.issues}
    assert [a.ex_date for a in data.corporate_actions] == sorted(
        a.ex_date for a in data.corporate_actions)


def test_nse_and_bse_actions_merge_and_disagree_explicitly():
    bse = raw(PayloadKind.BSE_CORPORATE_ACTIONS,
              {"Table": [{"scrip_code": BSE, "Purpose": "Dividend - Rs. - 0.72/- per share",
                          "Ex_date": "18 Aug 2026", "RD_Date": "18 Aug 2026"},
                         {"scrip_code": BSE, "Purpose": "Bonus 1:2", "Ex_date": "01 Jun 2026"}]},
              "https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w?scripcode=542649")
    data = build([*base_payloads(), bse, nse_actions(
        action("Dividend - Re 0.71 Per Share", "18-Aug-2026"), action("Bonus 1:2", "01-Jun-2026"))])
    bonus = next(a for a in data.corporate_actions if a.kind == CorporateActionKind.BONUS)
    assert {s.provider for s in bonus.sources} == {Provider.NSE, Provider.BSE}
    dividend = next(a for a in data.corporate_actions if a.kind == CorporateActionKind.DIVIDEND)
    assert dividend.amount_per_share is None  # never silently picked
    disagreement = next(d for d in data.disagreements if d.subject == "DIVIDEND_PER_SHARE")
    assert disagreement.status == DisagreementStatus.OPEN  # same authority
    assert {disagreement.value_a, disagreement.value_b} == {Decimal("0.71"), Decimal("0.72")}


# --- orders -----------------------------------------------------------------------------------


def test_l1_then_loa_links_by_letter_reference_and_counts_once():
    data = build(order_payloads(), document_texts=docs())
    assert len(data.orders) == 2
    l1, loa = data.orders
    assert l1.stage == OrderStage.L1_BIDDER and loa.stage == OrderStage.LOA
    assert l1.gross_value_crore == loa.gross_value_crore == Decimal("404.8818738")
    assert loa.value_tax_basis == TaxBasis.INCLUSIVE_OF_TAX
    assert loa.referenced_letter_dates == (date(2026, 9, 3),)
    assert l1.execution_period == "30 months" and l1.execution_months == 30
    assert loa.execution_period is None  # '912 Days' is not under its label: unknown
    assert loa.geography == Geography.DOMESTIC and loa.client == "East Coast Railway"
    assert loa.project is not None and loa.project.startswith("Execution of Roadbed")
    assert "VALUE_FROM_ANNEXURE_LABEL" in loa.extraction_rules
    (chain,) = [c for c in data.order_chains]
    assert chain.stages == (OrderStage.L1_BIDDER, OrderStage.LOA)
    assert chain.link_rules == ("LETTER_REFERENCE",)
    assert chain.status == ChainStatus.CONFIRMED and chain.counted_in_confirmed
    assert chain.confirmed_gross_value_crore == Decimal("404.8818738")
    assert chain.pending_l1_value_crore is None  # the L1 is not counted again
    assert chain.award_mode == AwardMode.COMPETITIVE_BIDDING
    events = {e.event_id: e for e in data.events}
    assert events[l1.event_id].related_event_ids == (loa.event_id,)


def test_l1_alone_is_never_confirmed():
    payloads = [*base_payloads(), nse_announcements(
        announcement("03-Sep-2026 16:06:07", "Updates", L1_TEXT, seq="1001", url=L1_URL),
        announcement("01-Jan-2025 10:00:00", "General Updates", "Newspaper publication",
                     seq="900"))]
    data = build(payloads, document_texts=docs())
    (chain,) = data.order_chains
    assert chain.status == ChainStatus.PENDING_L1 and not chain.counted_in_confirmed
    assert chain.confirmed_gross_value_crore is None
    assert chain.pending_l1_value_crore == Decimal("404.8818738")
    metrics = {m.kind: m for m in data.order_book.metrics}  # type: ignore[union-attr]
    pending = metrics[OrderBookMetricKind.DISCLOSED_PENDING_L1_VALUE]
    assert pending.value == Decimal("404.881874")
    assert "L1_IS_NOT_CONFIRMED_ORDER_BOOK" in pending.issues
    assert metrics[OrderBookMetricKind.DISCLOSED_CONFIRMED_INFLOW].value == 0
    short = build([*base_payloads(), nse_announcements(
        announcement("03-Sep-2026 16:06:07", "Updates", L1_TEXT, seq="1001", url=L1_URL))],
        document_texts=docs())
    metrics = {m.kind: m for m in short.order_book.metrics}  # type: ignore[union-attr]
    # one recent filing does not prove the ledger covers the trailing year: never zero
    for kind in (OrderBookMetricKind.DISCLOSED_CONFIRMED_INFLOW,
                 OrderBookMetricKind.DISCLOSED_PENDING_L1_VALUE):
        assert metrics[kind].value is None
        assert metrics[kind].reason == "LEDGER_DOES_NOT_COVER_PERIOD"


def test_without_documents_no_link_is_guessed():
    data = build(order_payloads())
    assert {c.status for c in data.order_chains} == {ChainStatus.PENDING_L1,
                                                     ChainStatus.CONFIRMED}
    assert all(c.link_rules == () for c in data.order_chains)
    loa_chain = next(c for c in data.order_chains if c.status == ChainStatus.CONFIRMED)
    assert loa_chain.confirmed_gross_value_crore is None  # not disclosed in the subject
    assert "ORDER_VALUE_NOT_DISCLOSED" in {i.code for i in loa_chain.issues}


def test_duplicate_loa_announcement_is_not_a_second_order():
    again = announcement("20-Sep-2026 10:00:00", "Bagging/Receiving of orders/contracts",
                         LOA_TEXT + " (re-filed)", seq="1003", url=LOA_URL + "?again")
    payloads = order_payloads()
    payloads[-1] = nse_announcements(
        announcement("03-Sep-2026 16:06:07", "Updates", L1_TEXT, seq="1001", url=L1_URL),
        announcement("19-Sep-2026 19:15:59", "Bagging/Receiving of orders/contracts", LOA_TEXT,
                     seq="1002", url=LOA_URL),
        again,
        announcement("01-Jan-2025 10:00:00", "General Updates", "Newspaper publication",
                     seq="900"),
    )
    texts = docs(**{LOA_URL + "?again": LOA_DOC})
    data = build(payloads, document_texts=texts)
    (chain,) = data.order_chains
    assert len(chain.order_ids) == 3
    assert any(r.startswith("DUPLICATE_STAGE") for r in chain.link_rules)
    assert chain.confirmed_gross_value_crore == Decimal("404.8818738")
    inflow = {m.kind: m for m in data.order_book.metrics}[  # type: ignore[union-attr]
        OrderBookMetricKind.DISCLOSED_CONFIRMED_INFLOW]
    assert inflow.value == Decimal("404.881874") and inflow.chains == 1


def _loa(seq: str, when: str, text: str, url: str) -> dict[str, Any]:
    return announcement(when, "Bagging/Receiving of orders/contracts", text, seq=seq, url=url)


def test_contract_signature_and_cancellation_and_revision():
    a_url = "https://nsearchives.nseindia.com/corporate/A.pdf"
    c_url = "https://nsearchives.nseindia.com/corporate/C.pdf"
    x_url = "https://nsearchives.nseindia.com/corporate/X.pdf"
    r_url = "https://nsearchives.nseindia.com/corporate/R.pdf"
    project = "for “Setting up of Buffer Stockpiles at Vizag”"
    data = build([*base_payloads(), nse_announcements(
        _loa("1", "01-Jun-2026 10:00:00", "Letter of Award of Contract from NMDC Limited", a_url),
        _loa("2", "01-Jul-2026 10:00:00", "Signing of contract agreement with NMDC Limited",
             c_url),
        _loa("3", "01-Aug-2026 10:00:00", "Revision in the value of the contract from NMDC "
             "Limited", r_url),
        _loa("4", "01-Sep-2026 10:00:00", "Cancellation of Letter of Award received from "
             "Southern Railway", x_url),
    )], document_texts=tuple(DocumentText(source_url=u, document_sha256=SHA, text=t) for u, t in (
        (a_url, f"received Letter of Award of Contract from NMDC Limited {project}. "
                "Broad consideration or size of the order Rs. 100 crore"),
        (c_url, f"signed contract agreement with NMDC Limited {project}."),
        (r_url, f"revision of the contract from NMDC Limited {project}. Broad consideration "
                "or size of the order Rs. 120 crore"),
        (x_url, "in continuation to our letter dated 01.03.2026 the LOA from Southern Railway "
                "has been cancelled"),
    )))
    stages = {o.stage for o in data.orders}
    assert stages == {OrderStage.LOA, OrderStage.SIGNED_CONTRACT, OrderStage.REVISION,
                      OrderStage.CANCELLATION}
    nmdc = next(c for c in data.order_chains if c.client == "NMDC Limited")
    assert nmdc.stages == (OrderStage.LOA, OrderStage.SIGNED_CONTRACT, OrderStage.REVISION)
    assert nmdc.revised and nmdc.revision_delta_crore == Decimal(20)
    assert nmdc.confirmed_gross_value_crore == Decimal(120)
    orphan = next(c for c in data.order_chains if c.client == "Southern Railway")
    assert orphan.status == ChainStatus.UNLINKED_CANCELLATION
    assert orphan.confirmed_gross_value_crore is None and orphan.cancelled_value_crore is None


def test_cancellation_removes_a_linked_order():
    a_url = "https://nsearchives.nseindia.com/corporate/A.pdf"
    x_url = "https://nsearchives.nseindia.com/corporate/X.pdf"
    data = build([*base_payloads(), nse_announcements(
        _loa("1", "27-Nov-2025 10:00:00", "Letter of Award from East Coast Railway worth Rs. "
             "50 crore", a_url),
        _loa("2", "01-Oct-2026 18:54:20", "Cancellation of Letter of Award received from East "
             "Coast Railway", x_url),
    )], document_texts=(DocumentText(source_url=x_url, document_sha256=SHA,
                                     text="This is in continuation to our letter dated "
                                     "27.11.2025 ... LOA from East Coast Railway has been "
                                     "cancelled (without any financial repercussions)"),))
    (chain,) = data.order_chains
    assert chain.status == ChainStatus.CANCELLED and not chain.counted_in_confirmed
    assert chain.cancelled_value_crore == Decimal(50) and chain.confirmed_gross_value_crore is None
    rate = {m.kind: m for m in data.order_book.metrics}[  # type: ignore[union-attr]
        OrderBookMetricKind.CANCELLATION_RATE]
    assert rate.value == Decimal(100)


def test_jv_value_is_attributable_only_with_a_stated_share():
    a_url = "https://nsearchives.nseindia.com/corporate/JV1.pdf"
    b_url = "https://nsearchives.nseindia.com/corporate/JV2.pdf"
    data = build([*base_payloads(), nse_announcements(
        _loa("1", "01-Sep-2026 10:00:00", "Letter of Acceptance from Central Railway worth Rs. "
             "1000 crore received by RVNL-XYZ JV", a_url),
        _loa("2", "02-Sep-2026 10:00:00", "Letter of Acceptance from Western Railway worth Rs. "
             "400 crore received by RVNL-ABC JV", b_url),
    )], order_facts=(OrderFact(source_url=a_url, jv_share_pct=Decimal(60), jv_partner="XYZ",
                               client_category=ClientCategory.GOVERNMENT,
                               citation=Citation(provider=Provider.COMPANY,
                                                 authority=Authority.PRIMARY_COMPANY,
                                                 reference="LOA annexure")),))
    stated, unstated = data.orders
    assert stated.joint_venture and stated.attributable_value_crore == Decimal("600.000000")
    assert stated.gross_value_crore == Decimal(1000)
    assert unstated.joint_venture and unstated.attributable_value_crore is None
    assert "JV_SHARE_NOT_STATED" in {i.code for i in unstated.issues}
    jv = {m.kind: m for m in data.order_book.metrics}[  # type: ignore[union-attr]
        OrderBookMetricKind.JV_ATTRIBUTABLE_CONFIRMED_VALUE]
    assert jv.status == MetricStatus.UNAVAILABLE
    assert jv.reason == "ATTRIBUTABLE_VALUE_UNKNOWN_FOR_SOME_ORDERS"


def test_ambiguous_order_link_is_flagged_not_guessed():
    urls = [f"https://nsearchives.nseindia.com/corporate/{n}.pdf" for n in "ABC"]
    data = build([*base_payloads(), nse_announcements(
        announcement("01-Sep-2026 10:00:00", "Updates", "RVNL emerges as the Lowest Bidder "
                     "(L1) from East Coast Railway", seq="1", url=urls[0]),
        announcement("01-Sep-2026 11:00:00", "Updates", "RVNL emerges as the Lowest Bidder "
                     "(L1) from East Coast Railway", seq="2", url=urls[1]),
        _loa("3", "10-Sep-2026 10:00:00", "Letter of Acceptance from East Coast Railway",
             urls[2]),
    )], document_texts=(DocumentText(source_url=urls[2], document_sha256=SHA,
                                     text="in continuation to our letter dated 01.09.2026 "
                                     "received LOA from East Coast Railway for “Other work”"),))
    loa_chain = next(c for c in data.order_chains if c.status == ChainStatus.CONFIRMED)
    assert loa_chain.stages == (OrderStage.LOA,)
    assert "AMBIGUOUS_ORDER_LINK" in {i.code for i in loa_chain.issues}
    assert sum(c.status == ChainStatus.PENDING_L1 for c in data.order_chains) == 2


def test_order_analytics_fail_closed_on_tax_basis_and_allow_by_policy():
    cite = Citation(provider=Provider.COMPANY, authority=Authority.PRIMARY_COMPANY,
                    reference="FY26 annual report")
    revenue = PeriodRevenue(period_start=date(2025, 4, 1), period_end=date(2026, 3, 31),
                            value_crore=Decimal(20000), basis=FinancialBasis.CONSOLIDATED,
                            citation=cite)
    opening = ReportedOrderBook(as_of=date(2025, 3, 31), value_crore=Decimal(90000),
                                tax_basis=TaxBasis.EXCLUSIVE_OF_TAX,
                                basis=FinancialBasis.CONSOLIDATED, citation=cite)
    closing = ReportedOrderBook(as_of=date(2026, 3, 31), value_crore=Decimal(95000),
                                tax_basis=TaxBasis.EXCLUSIVE_OF_TAX,
                                basis=FinancialBasis.CONSOLIDATED, citation=cite)
    url = "https://nsearchives.nseindia.com/corporate/O1.pdf"
    payloads = [*base_payloads(), nse_announcements(
        announcement("01-Mar-2025 10:00:00", "Updates", "Newspaper", seq="0"),
        _loa("1", "01-Jun-2025 10:00:00", "Letter of Award from NMDC Limited worth Rs. 1000 "
             "crore (excluding GST)", url),
    )]
    data = build(payloads, period_revenues=(revenue,), reported_order_books=(opening, closing))
    m = {x.kind: x for x in data.order_book.metrics}  # type: ignore[union-attr]
    assert m[OrderBookMetricKind.ORDER_BOOK_TO_REVENUE].value == Decimal("4.750000")
    assert m[OrderBookMetricKind.EXECUTION_RATE].value == Decimal("22.222222")
    assert m[OrderBookMetricKind.BOOK_TO_BILL].value == Decimal("0.050000")
    assert m[OrderBookMetricKind.BOOK_TO_BILL].completeness == "DISCLOSED_ORDERS_ONLY"
    rec = data.order_book.reconciliation  # type: ignore[union-attr]
    assert rec is not None and rec.status == MetricStatus.AVAILABLE
    # 90000 + 1000 - 20000 - 0 + 0
    assert rec.implied_closing_crore == Decimal(71000)
    assert rec.difference_crore == Decimal(24000)
    assert "INFLOW_IS_A_LOWER_BOUND_UNDISCLOSED_ORDERS_EXCLUDED" in rec.issues

    inclusive = [*base_payloads(), nse_announcements(
        announcement("01-Mar-2025 10:00:00", "Updates", "Newspaper", seq="0"),
        _loa("1", "01-Jun-2025 10:00:00", "Letter of Award from NMDC Limited worth Rs. 1000 "
             "crore (Incl. GST)", url))]
    strict = build(inclusive, period_revenues=(revenue,), reported_order_books=(opening,))
    m = {x.kind: x for x in strict.order_book.metrics}  # type: ignore[union-attr]
    assert m[OrderBookMetricKind.BOOK_TO_BILL].reason == "TAX_BASIS_NOT_COMPARABLE"
    assert m[OrderBookMetricKind.BOOK_TO_BILL].value is None
    assert strict.order_book.reconciliation.reason == "TAX_BASIS_NOT_COMPARABLE"  # type: ignore[union-attr]
    allowed = build(inclusive, period_revenues=(revenue,), reported_order_books=(opening,),
                    allow_tax_inclusive_order_values=True)
    m = {x.kind: x for x in allowed.order_book.metrics}  # type: ignore[union-attr]
    assert m[OrderBookMetricKind.BOOK_TO_BILL].value == Decimal("0.050000")
    assert "TAX_INCLUSIVE_OR_UNKNOWN_VALUES_PERMITTED_BY_POLICY" in m[
        OrderBookMetricKind.BOOK_TO_BILL].issues


def test_order_book_basis_mismatch_is_not_comparable():
    cite = Citation(provider=Provider.COMPANY, authority=Authority.PRIMARY_COMPANY, reference="x")
    revenue = PeriodRevenue(period_start=date(2025, 4, 1), period_end=date(2026, 3, 31),
                            value_crore=Decimal(20000), basis=FinancialBasis.STANDALONE,
                            citation=cite)
    book = ReportedOrderBook(as_of=date(2026, 3, 31), value_crore=Decimal(90000),
                             tax_basis=TaxBasis.EXCLUSIVE_OF_TAX,
                             basis=FinancialBasis.CONSOLIDATED, citation=cite)
    data = build(base_payloads(), period_revenues=(revenue,), reported_order_books=(book,))
    m = {x.kind: x for x in data.order_book.metrics}  # type: ignore[union-attr]
    assert m[OrderBookMetricKind.ORDER_BOOK_TO_REVENUE].reason == "BASIS_MISMATCH"
    with pytest.raises(ValidationError):  # revenue is one basis, never mixed
        PeriodRevenue(period_start=date(2025, 4, 1), period_end=date(2026, 3, 31),
                      value_crore=Decimal(1), basis=FinancialBasis.BOTH, citation=cite)


def test_mix_metrics_require_complete_evidence():
    data = build(order_payloads(), document_texts=docs())
    m = {x.kind: x for x in data.order_book.metrics}  # type: ignore[union-attr]
    assert m[OrderBookMetricKind.DOMESTIC_SHARE].value == Decimal(100)
    assert m[OrderBookMetricKind.TOP_CLIENT_SHARE].value == Decimal(100)
    assert m[OrderBookMetricKind.COMPETITIVE_BIDDING_SHARE].value == Decimal(100)
    gov = m[OrderBookMetricKind.GOVERNMENT_SHARE]
    assert gov.status == MetricStatus.UNAVAILABLE and gov.value is None
    assert gov.reason == "CLIENT_CATEGORY_NOT_DISCLOSED_FOR_SOME_ORDERS"
    growth = m[OrderBookMetricKind.ORDER_INFLOW_GROWTH]
    assert growth.reason == "INPUT_NOT_SUPPLIED"


# --- shareholding, filings, disagreements ------------------------------------------------------


def shareholding_payload(*rows: tuple[str, str, str, str]) -> RawPayload:
    return raw(PayloadKind.NSE_SHAREHOLDING, [
        {"symbol": SYMBOL, "isin": ISIN, "date": period, "pr_and_prgrp": promoter,
         "public_val": public, "employeeTrusts": "0", "revisedData": revised,
         "submissionDate": "20-JUL-2026", "recordId": f"{period}{revised}",
         "broadcastDate": "20-JUL-2026 16:56:30",
         "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP.xml"}
        for period, promoter, public, revised in rows
    ], f"{NSE_API}/corporate-share-holdings-master")


def screener_dataset(promoter: str) -> DetailedScreenerDataset:
    table = ScreenerTable(
        kind=ScreenerTableKind.SHAREHOLDING_QUARTERLY, reporting_basis=ReportingScope.UNKNOWN,
        caption=None, periods=(TablePeriod(label="Jun 2026", end_date=date(2026, 6, 30)),),
        rows=(TableRow(label="Promoters", displayed_label="Promoters +", expandable=True,
                       unit=None,
                       cells=(TableCell(period="Jun 2026", locator="#shareholding r1c1",
                                        value=DisplayedValue(displayed=f"{promoter}%",
                                                             value=Decimal(promoter),
                                                             status=ValueStatus.AVAILABLE,
                                                             unit=None)),)),),
        locator="#quarterly-shp",
    )
    return DetailedScreenerDataset.model_construct(
        company_key="screener:RVNL",
        source=ScreenerSourceRef.model_construct(page_url="https://www.screener.in/company/RVNL/"),
        tables=(table,),
    )


def test_shareholding_filings_keep_revisions_and_reconcile_with_screener():
    sh = shareholding_payload(("30-JUN-2026", "72.84", "27.16", "N"),
                              ("30-JUN-2026", "72.80", "27.20", "Y"),
                              ("31-MAR-2026", "72.84", "27.16", "N"))
    facts = screener_shareholding_facts(screener_dataset("72.90"))
    assert facts[0].citation.authority == Authority.THIRD_PARTY_STRUCTURED
    data = build([*base_payloads(), sh], comparable_facts=tuple(facts))
    june = [s for s in data.shareholding if s.period_end == date(2026, 6, 30)]
    assert len(june) == 2 and june[1].supersedes_filing_id == june[0].filing_id
    assert june[1].promoter_pct == Decimal("72.80")
    assert "FII_FPI" in june[0].unparsed_categories  # not invented from the summary feed
    (d,) = [x for x in data.disagreements if x.subject == "PROMOTER_HOLDING_PCT"]
    assert d.source_a.provider == Provider.NSE and d.value_a == Decimal("72.80")
    assert d.source_b.provider == Provider.SCREENER and d.value_b == Decimal("72.90")
    assert d.difference == Decimal("-0.10")
    assert d.status == DisagreementStatus.RESOLVED_BY_AUTHORITY_POLICY
    assert d.canonical_selection == Provider.NSE and "both values preserved" in \
        d.selection_rationale


def test_agreeing_sources_create_no_disagreement_and_explicit_inputs_are_never_selected():
    def fact(value: str, authority: Authority, provider: Provider) -> ComparableFact:
        return ComparableFact(metric="PROMOTER_HOLDING_PCT", period=date(2026, 6, 30),
                              value=Decimal(value), unit="PERCENT",
                              citation=Citation(provider=provider, authority=authority,
                                                reference=provider.value))
    assert disagreements([fact("72.84", Authority.PRIMARY_REGULATORY, Provider.NSE),
                          fact("72.84", Authority.THIRD_PARTY_STRUCTURED,
                               Provider.SCREENER)]) == []
    (d,) = disagreements([fact("72.84", Authority.PRIMARY_REGULATORY, Provider.NSE),
                          fact("70", Authority.EXPLICIT_INPUT, Provider.CALLER)])
    assert d.status == DisagreementStatus.OPEN and d.canonical_selection is None


def test_filing_documents_and_insider_trades():
    results = raw(PayloadKind.NSE_FINANCIAL_RESULTS, [
        {"symbol": SYMBOL, "isin": ISIN, "consolidated": "Consolidated", "audited": "Un-Audited",
         "period": "Quarterly", "relatingTo": "First Quarter", "fromDate": "01-Apr-2026",
         "toDate": "30-Jun-2026", "broadCastDate": "11-Aug-2026 17:00:00", "seqNumber": "1",
         "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/R1.xml"},
        {"symbol": SYMBOL, "isin": ISIN, "consolidated": "Non-Consolidated",
         "audited": "Un-Audited", "period": "Quarterly", "relatingTo": "First Quarter",
         "fromDate": "01-Apr-2026", "toDate": "30-Jun-2026",
         "broadCastDate": "11-Aug-2026 17:00:00", "seqNumber": "2",
         "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/R2.xml"},
    ], f"{NSE_API}/results")
    reports = raw(PayloadKind.NSE_ANNUAL_REPORTS, {"data": [
        {"fromYr": "2025", "toYr": "2026", "submission_type": "Revised",
         "disseminationDateTime": "21-AUG-2026 18:47:23",
         "fileName": "https://nsearchives.nseindia.com/annual_reports/AR.pdf"}]}, f"{NSE_API}/ar")
    ratings = raw(PayloadKind.NSE_CREDIT_RATINGS, [
        {"AppID": "1", "Symbol": "NOTLISTED", "ISIN": "INE171A08032", "CompanyName": "Other",
         "NameOfCRAgency": "CARE", "CreditRating": "AA+", "RatingAction": "Reaffirm",
         "DateofCR": "07-10-2026", "BroadcastDateTime": "10-OCT-2026 22:20:00"},
        {"AppID": "2", "Symbol": "NOTLISTED", "ISIN": "INE415G08011", "CompanyName": "RVNL",
         "NameOfCRAgency": "CARE", "CreditRating": "AAA", "RatingAction": "Assigned",
         "DateofCR": "01-10-2026", "BroadcastDateTime": "02-OCT-2026 10:00:00"},
    ], f"{NSE_API}/cr")
    insider = raw(PayloadKind.NSE_INSIDER_TRADING, {"data": [
        {"symbol": SYMBOL, "acqName": "A Person", "personCategory": "Employees",
         "tdpTransactionType": "Acquisition", "acqMode": "Market Purchase",
         "secType": "Equity Shares", "secAcq": "140", "secVal": "57750",
         "befAcqSharesNo": "500", "afterAcqSharesNo": "640", "acqfromDt": "02-Sep-2025",
         "acqtoDt": "02-Sep-2025", "intimDt": "02-Sep-2025", "date": "03-Sep-2025 17:27",
         "pid": "1187077"}]}, f"{NSE_API}/pit")
    data = build([*base_payloads(), results, reports, ratings, insider])
    bases = {f.basis for f in data.filings if f.category.value == "FINANCIAL_RESULT"}
    assert bases == {FinancialBasis.CONSOLIDATED, FinancialBasis.STANDALONE}
    report = next(f for f in data.filings if f.category.value == "ANNUAL_REPORT")
    assert report.revised and report.published_on == date(2026, 8, 21)
    rating = [f for f in data.filings if f.category.value == "CREDIT_RATING"]
    assert len(rating) == 1 and "AAA" in rating[0].title  # market-wide feed filtered by issuer
    (trade,) = data.insider_trades
    assert trade.quantity == 140 and trade.value_inr == 57750
    assert trade.disclosed_at == datetime(2025, 9, 3, 17, 27, tzinfo=IST)


# --- coverage and timeline ---------------------------------------------------------------------


def test_coverage_states_without_a_score():
    payloads = [*base_payloads(), nse_announcements(
        announcement("01-Oct-2026 10:00:00", "Shareholders meeting", "AGM notice", seq="1"),
        announcement("01-Jan-2023 10:00:00", "Investor Presentation", "Investor presentation",
                     seq="2")),
        failed(PayloadKind.NSE_SHAREHOLDING, FetchStatus.BLOCKED, "HTTP_403"),
        raw(PayloadKind.NSE_FINANCIAL_RESULTS, [], f"{NSE_API}/fr"),
        raw(PayloadKind.NSE_INSIDER_TRADING, {"data": []}, f"{NSE_API}/pit")]
    data = build(payloads)
    cov = {i.category: i for i in data.coverage.items}
    assert data.coverage.scoring == "NONE"
    assert cov[CoverageCategory.AGM_EGM_MATERIAL].status == CoverageStatus.AVAILABLE
    assert cov[CoverageCategory.INVESTOR_PRESENTATIONS].status == CoverageStatus.STALE
    assert cov[CoverageCategory.SHAREHOLDING_FILINGS].status == CoverageStatus.SOURCE_UNAVAILABLE
    assert cov[CoverageCategory.SHAREHOLDING_FILINGS].source_failures == ("HTTP_403",)
    assert cov[CoverageCategory.SHAREHOLDING_FILINGS].count == 0
    assert cov[CoverageCategory.QUARTERLY_RESULTS].status == CoverageStatus.MISSING
    assert cov[CoverageCategory.INSIDER_FILINGS].status == CoverageStatus.NOT_FOUND
    assert cov[CoverageCategory.MARKET_QUOTE].source_failures == ("NOT_ACQUIRED",)
    assert len(cov) == len(CoverageCategory)


def test_timeline_windows_and_filtering():
    data = build([*base_payloads(), nse_announcements(
        announcement("09-Oct-2026 10:00:00", "Dividend", "Dividend Rs 1 per share", seq="1"),
        announcement("01-Sep-2026 10:00:00", "Credit Rating", "Credit rating", seq="2"),
        announcement("01-Mar-2026 10:00:00", "Credit Rating", "Credit rating", seq="3"),
        announcement("01-Mar-2025 10:00:00", "Credit Rating", "Credit rating", seq="4"),
    ), nse_actions(action("Dividend - Re 1 Per Share", "05-Oct-2026"))])
    windows = {w.days: w for w in data.timeline.windows}
    assert (windows[30].total, windows[90].total, windows[365].total) == (2, 3, 4)
    assert dict(windows[30].counts) == {"DIVIDEND": 2}
    entries = data.timeline.entries
    assert [e.entry_date for e in entries] == sorted(e.entry_date for e in entries)
    ratings = filter_timeline(entries, date(2026, 1, 1), date(2026, 10, 9), ["CREDIT_RATING"])
    assert [e.entry_date for e in ratings] == [date(2026, 3, 1), date(2026, 9, 1)]


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "issue"),
    [
        (FetchStatus.BLOCKED, "HTTP_403", "SOURCE_BLOCKED"),
        (FetchStatus.RATE_LIMITED, "HTTP_429", "SOURCE_RATE_LIMITED"),
        (FetchStatus.NOT_FOUND, "HTTP_404", "SOURCE_UNAVAILABLE"),
        (FetchStatus.TIMEOUT, "TIMEOUT", "SOURCE_TIMEOUT"),
        (FetchStatus.NETWORK_ERROR, "NETWORK_ERROR", "SOURCE_NETWORK_ERROR"),
        (FetchStatus.TOO_LARGE, "SIZE_LIMIT", "SOURCE_TOO_LARGE"),
    ],
)
def test_source_failures_are_explicit_never_zero(status, code, issue):
    data = build([*base_payloads(), failed(PayloadKind.NSE_ANNOUNCEMENTS, status, code)])
    assert issue in {i.code for i in data.issues}
    assert data.events == ()
    cov = {i.category: i for i in data.coverage.items}[CoverageCategory.MATERIAL_ANNOUNCEMENTS]
    assert cov.status == CoverageStatus.SOURCE_UNAVAILABLE and code in cov.source_failures
    record = next(p for p in data.payloads if p.kind == PayloadKind.NSE_ANNOUNCEMENTS)
    assert record.status == status and record.content_sha256 is None


@pytest.mark.parametrize(
    "content",
    [b"{", b"\xff\xfe", b"[" * 100_000 + b"]" * 100_000, b'{"data": 5}', b"<html>Access Denied",
     b"[1, 2, 3]"],
)
def test_malformed_payloads_are_invalid_source_data(content):
    data = build([*base_payloads(), raw(PayloadKind.NSE_ANNOUNCEMENTS, content)])
    assert data.events == ()
    assert {"INVALID_SOURCE_DATA", "INVALID_SOURCE_ROWS"} & {i.code for i in data.issues}


def test_raw_payload_contract():
    with pytest.raises(ValidationError):
        RawPayload(kind=PayloadKind.NSE_ANNOUNCEMENTS, url="x", status=FetchStatus.OK,
                   retrieved_at=AS_OF)
    with pytest.raises(ValidationError):
        RawPayload(kind=PayloadKind.NSE_ANNOUNCEMENTS, url="x", status=FetchStatus.BLOCKED,
                   retrieved_at=AS_OF)
    with pytest.raises(ValidationError):
        RawPayload(kind=PayloadKind.NSE_ANNOUNCEMENTS, url="x", status=FetchStatus.OK,
                   retrieved_at=AS_OF, content=b"x" * 16_000_001)
    with pytest.raises(ValidationError):  # naive time
        RawPayload(kind=PayloadKind.NSE_ANNOUNCEMENTS, url="x", status=FetchStatus.OK,
                   retrieved_at=datetime(2026, 1, 1), content=b"")


def test_payload_and_row_budgets():
    with pytest.raises(engine.MarketIntelligenceInputError):
        build([raw(PayloadKind.NSE_ANNOUNCEMENTS, [], f"{NSE_API}/p{i}") for i in range(201)])
    rows = [announcement("01-Oct-2026 10:00:00", "Updates", f"x{i}", seq=str(i))
            for i in range(20_005)]
    data = build([*base_payloads(), nse_announcements(*rows),
                  shareholding_payload(("30-JUN-2026", "72.84", "27.16", "N"))])
    assert "SOURCE_RECORD_LIMIT" in {i.code for i in data.issues}
    assert len(data.events) <= 5_000
    cov = {i.category: i for i in data.coverage.items}
    # rows cut by the budget are never reported as 'missing'
    assert cov[CoverageCategory.SHAREHOLDING_FILINGS].status == CoverageStatus.SOURCE_UNAVAILABLE
    assert "SOURCE_RECORD_LIMIT" in cov[CoverageCategory.SHAREHOLDING_FILINGS].source_failures


def test_large_instrument_masters_do_not_starve_filing_feeds():
    filler = [f"{100000 + i},{i},SYM{i},CO {i},0,,0,0.05,1,EQ,NSE,NSE" for i in range(30_000)]
    master_rows = [(f"S{i}", f"Company {i}", "INE002A01018") for i in range(5_000)]
    data = build([nse_master((SYMBOL, "Rail Vikas Nigam Limited", ISIN), *master_rows),
                  kite_master(*filler), bse_header(),
                  nse_announcements(announcement("01-Oct-2026 10:00:00", "Dividend",
                                                 "Dividend Rs 1 per share", seq="1"))])
    assert data.identity.status == IdentityStatus.RESOLVED
    assert len(data.events) == 1
    assert "SOURCE_RECORD_LIMIT" not in {i.code for i in data.issues}


# --- determinism, immutability, async, performance ----------------------------------------------


def rich_payloads() -> list[RawPayload]:
    days = business_days(date(2025, 9, 1), date(2026, 10, 9))
    return [
        *order_payloads(), quote_payload(),
        history_payload(series(days, lambda i: Decimal(100) + Decimal(i % 17))),
        history_payload(series(days, lambda i: Decimal(20000) + i % 13), NIFTY),
        nse_actions(action("Dividend - Re 0.71 Per Share", "18-Aug-2026")),
        shareholding_payload(("30-JUN-2026", "72.84", "27.16", "N")),
    ]


def test_output_is_deterministic_and_order_independent():
    payloads = rich_payloads()
    first = build(payloads, document_texts=docs())
    shuffled = payloads[::-1]
    later = [p.model_copy(update={"retrieved_at": p.retrieved_at + timedelta(hours=1)})
             for p in shuffled]
    second = engine.build_market_intelligence(request(document_texts=docs()), later,
                                              generated_at=GENERATED + timedelta(days=1))
    one = engine.canonical_market_intelligence_json(first)
    assert one == engine.canonical_market_intelligence_json(second)
    assert first.dataset_id == second.dataset_id
    duplicated = build(payloads + payloads, document_texts=docs())
    assert engine.canonical_market_intelligence_json(duplicated) == one
    assert "uuid4" not in Path(engine.__file__).read_text()


def test_dataset_is_immutable():
    data = build(rich_payloads(), document_texts=docs())
    with pytest.raises(ValidationError):
        data.events[0].title = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        data.identity.isin = "INE002A01018"  # type: ignore[misc]
    assert isinstance(data.events, tuple) and isinstance(data.order_chains, tuple)


def test_dataset_rejects_broken_lineage():
    data = build(rich_payloads(), document_texts=docs())
    dumped = data.model_dump()
    dumped["orders"] = dumped["orders"][:1]
    with pytest.raises(ValidationError):
        MarketIntelligenceDataset.model_validate(dumped)
    dumped = data.model_dump()
    dumped["events"] = list(reversed(dumped["events"]))
    with pytest.raises(ValidationError):
        MarketIntelligenceDataset.model_validate(dumped)


def test_async_builds_are_independent_and_identical():
    payloads = rich_payloads()

    async def run() -> list[str]:
        results = await asyncio.gather(*[
            engine.build_market_intelligence_async(request(document_texts=docs()), payloads,
                                                   generated_at=GENERATED)
            for _ in range(6)
        ])
        return [engine.canonical_market_intelligence_json(r) for r in results]

    outputs = asyncio.run(run())
    assert len(set(outputs)) == 1


def test_performance_at_the_bounds():
    days = business_days(date(2016, 9, 1), date(2026, 10, 9))
    rows = [announcement(f"{(date(2016, 10, 1) + timedelta(days=i % 3600)).strftime('%d-%b-%Y')}"
                         " 10:00:00", "Bagging/Receiving of orders/contracts" if i % 5 == 0
                         else "Updates", f"Letter of Acceptance from Zone {i % 40} Railway worth "
                         f"Rs. {i + 1} crore" if i % 5 == 0 else f"Update number {i}", seq=str(i))
            for i in range(4_900)]
    payloads = [*base_payloads(), nse_announcements(*rows),
                history_payload(series(days, lambda i: Decimal(100) + Decimal(i % 50))),
                history_payload(series(days, lambda i: Decimal(20000) + i % 70), NIFTY)]
    start = time.perf_counter()
    data = build(payloads)
    elapsed = time.perf_counter() - start
    assert len(data.events) == 4_900 and len(data.company_history.candles) > 2_500  # type: ignore[union-attr]
    assert elapsed < 30, elapsed


def test_no_recommendation_language():
    data = build(rich_payloads(), document_texts=docs())
    assert data.recommendation == "NONE"
    text = engine.canonical_market_intelligence_json(data)
    assert not re.search(r"\b(BUY|SELL|HOLD|ACCUMULATE|OUTPERFORM|UNDERPERFORM|TARGET_PRICE)\b",
                         text)
    for module in ("market_intelligence", "market_data", "corporate_events", "order_ledger",
                   "source_coverage", "exchange_intelligence"):
        source = (Path(engine.__file__).parent / f"{module}.py").read_text()
        assert not re.search(r"\b(buy|sell|hold) (rating|recommendation)\b", source, re.I)
        assert "anthropic" not in source and "aicredits" not in source  # no LLM


# --- adapters ---------------------------------------------------------------------------------


def fetcher(handler: Callable[[httpx.Request], httpx.Response], **kw: Any) -> ExchangeFetcher:
    return ExchangeFetcher(transport=httpx.MockTransport(handler), min_interval=0,
                           retry_delay=0, clock=lambda: AS_OF, **kw)


def test_fetcher_success_and_headers():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"[]", headers={"content-type": "application/json"})

    payload = asyncio.run(fetcher(handler).fetch(PayloadKind.NSE_ANNOUNCEMENTS,
                                                 f"{NSE_API}/corporate-announcements"))
    assert payload.status == FetchStatus.OK and payload.content == b"[]"
    assert seen[0].headers["user-agent"].startswith("StockResearchAI/")
    assert "cookie" not in seen[0].headers


@pytest.mark.parametrize(
    ("code", "status"),
    [(403, FetchStatus.BLOCKED), (429, FetchStatus.RATE_LIMITED), (401, FetchStatus.AUTH_REQUIRED),
     (404, FetchStatus.NOT_FOUND), (500, FetchStatus.HTTP_ERROR),
     (302, FetchStatus.UNSUPPORTED)],
)
def test_fetcher_http_failures_are_not_retried(code, status):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(code, content=b"Access Denied",
                              headers={"location": "https://127.0.0.1/"})

    payload = asyncio.run(fetcher(handler, network_retries=2).fetch(
        PayloadKind.BSE_ANNOUNCEMENTS, "https://api.bseindia.com/BseIndiaAPI/api/x"))
    assert payload.status == status and payload.content is None
    assert len(calls) == 1  # 403/429 are never worked around or retried


def test_fetcher_network_errors_retry_a_bounded_number_of_times():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        raise httpx.ConnectError("boom with secret-ish detail")

    payload = asyncio.run(fetcher(handler, network_retries=2).fetch(
        PayloadKind.NSE_ANNOUNCEMENTS, f"{NSE_API}/x"))
    assert payload.status == FetchStatus.NETWORK_ERROR and len(calls) == 3
    assert payload.error_code == "NETWORK_ERROR"  # never the exception text


def test_fetcher_timeout():
    async def slow(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    f = ExchangeFetcher(transport=httpx.MockTransport(slow), timeout=0.05, min_interval=0,
                        network_retries=0)
    payload = asyncio.run(f.fetch(PayloadKind.NSE_ANNOUNCEMENTS, f"{NSE_API}/x"))
    assert payload.status == FetchStatus.TIMEOUT


def test_fetcher_size_limits():
    declared = fetcher(lambda r: httpx.Response(200, content=b"x" * 10,
                                                headers={"content-length": "999999999"}),
                       max_bytes=1000)
    assert asyncio.run(declared.fetch(PayloadKind.NSE_ANNOUNCEMENTS, f"{NSE_API}/x")).status == \
        FetchStatus.TOO_LARGE

    def stream(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"y" * 5000))

    payload = asyncio.run(fetcher(stream, max_bytes=1000).fetch(
        PayloadKind.NSE_ANNOUNCEMENTS, f"{NSE_API}/x"))
    assert payload.status == FetchStatus.TOO_LARGE and payload.content is None


@pytest.mark.parametrize(
    "url",
    [
        "http://www.nseindia.com/api/x",
        "https://127.0.0.1/api",
        "https://10.0.0.1/api",
        "https://[::1]/api",
        "https://localhost/api",
        "https://evil.example.com/api",
        "https://user:pass@www.nseindia.com/api",
        "https://www.nseindia.com:8443/api",
        "https://www.nseindia.com.evil.com/api",
        "file:///etc/passwd",
        "https://www.nseindia.com/api\r\nX: y",
        "https://www.nseindia.com/" + "a" * 3000,
    ],
)
def test_fetcher_refuses_bad_urls_without_network_or_echo(url):
    calls = []
    f = fetcher(lambda r: calls.append(r) or httpx.Response(200))
    payload = asyncio.run(f.fetch(PayloadKind.NSE_ANNOUNCEMENTS, url))
    assert payload.status == FetchStatus.INVALID_REQUEST and not calls
    assert payload.url in {NSE_API} and url not in payload.model_dump_json()
    with pytest.raises(ValueError):
        validate_source_url(url)


def test_fetcher_pins_only_public_addresses():
    async def lookup(host: str) -> list[str]:
        return ["127.0.0.1"]  # DNS rebinding to loopback

    f = ExchangeFetcher(lookup=lookup, min_interval=0, network_retries=0)
    payload = asyncio.run(f.fetch(PayloadKind.NSE_ANNOUNCEMENTS, f"{NSE_API}/x"))
    assert payload.status == FetchStatus.NETWORK_ERROR
    assert payload.error_code == "NON_PUBLIC_ADDRESS"


def test_fetcher_request_budget_and_bounds():
    calls = []
    f = fetcher(lambda r: calls.append(r) or httpx.Response(200, content=b"[]"), max_requests=2)

    async def run() -> list[RawPayload]:
        return [await f.fetch(PayloadKind.NSE_ANNOUNCEMENTS, f"{NSE_API}/{i}") for i in range(4)]

    results = asyncio.run(run())
    assert [r.status for r in results].count(FetchStatus.OK) == 2
    assert results[-1].error_code == "REQUEST_BUDGET_EXHAUSTED" and len(calls) == 2
    for bad in ({"max_bytes": 10**9}, {"network_retries": 5}, {"timeout": 0},
                {"max_requests": 10_000}):
        with pytest.raises(ValueError):
            ExchangeFetcher(**bad)  # type: ignore[arg-type]


def test_concurrent_fetches_are_safe():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=req.url.path.encode())

    f = fetcher(handler, max_requests=50)

    async def run() -> list[RawPayload]:
        return list(await asyncio.gather(*[
            f.fetch(PayloadKind.NSE_ANNOUNCEMENTS, f"{NSE_API}/{i}") for i in range(30)]))

    results = asyncio.run(run())
    assert sorted(r.content for r in results) == sorted(  # type: ignore[type-var]
        f"/api/{i}".encode() for i in range(30))
    assert f.remaining_requests == 20


CREDS = KiteCredentials(SecretStr(SECRET_KEY), SecretStr(SECRET_TOKEN), "environment")


def test_kite_without_credentials_makes_no_request():
    calls = []
    client = KiteMarketClient(fetcher(lambda r: calls.append(r) or httpx.Response(200)), None)
    payload = asyncio.run(client.quote(["NSE:RVNL"]))
    assert payload.status == FetchStatus.AUTH_REQUIRED
    assert payload.error_code == "KITE_CREDENTIALS_NOT_CONFIGURED" and not calls


def test_kite_credentials_travel_only_in_the_header():
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(403, content=json.dumps({
            "status": "error", "message": f"bad token {SECRET_TOKEN}",
            "error_type": "TokenException"}).encode())

    client = KiteMarketClient(fetcher(handler), CREDS)
    payload = asyncio.run(client.quote(["NSE:RVNL", "NSE:NIFTY 50"]))
    assert seen[0].headers["authorization"] == f"token {SECRET_KEY}:{SECRET_TOKEN}"
    assert seen[0].headers["x-kite-version"] == "3"
    assert SECRET_TOKEN not in str(seen[0].url) and SECRET_KEY not in str(seen[0].url)
    assert payload.status == FetchStatus.BLOCKED and payload.content is None
    dumped = payload.model_dump_json()
    assert SECRET_TOKEN not in dumped and SECRET_KEY not in dumped
    assert SECRET_TOKEN not in repr(CREDS) and SECRET_KEY not in str(CREDS)
    assert "NSE%3ANIFTY%2050" in str(seen[0].url)


def test_kite_history_is_chunked_and_bounded():
    urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        urls.append(str(req.url))
        return httpx.Response(200, content=b'{"status":"success","data":{"candles":[]}}')

    client = KiteMarketClient(fetcher(handler), CREDS)
    payloads = asyncio.run(client.historical(TOKEN, date(2016, 10, 9), date(2026, 10, 9)))
    assert len(payloads) == 2 and len(urls) == 2
    assert all(f"/instruments/historical/{TOKEN}/day" in u for u in urls)
    too_long = asyncio.run(client.historical(TOKEN, date(2010, 1, 1), date(2026, 10, 9)))
    assert too_long[0].status == FetchStatus.INVALID_REQUEST
    assert asyncio.run(client.historical("12a", date(2026, 1, 1), date(2026, 2, 1)))[0].status \
        == FetchStatus.INVALID_REQUEST
    assert asyncio.run(client.quote(["NSE:RVNL;DROP"])).status == FetchStatus.INVALID_REQUEST
    assert asyncio.run(client.quote(["NSE:X"] * 21)).status == FetchStatus.INVALID_REQUEST
    assert len(urls) == 2


def test_nse_client_validates_symbols_and_covers_endpoints():
    urls: list[str] = []
    f = fetcher(lambda r: urls.append(str(r.url)) or httpx.Response(200, content=b"[]"))
    bad = asyncio.run(NseClient(f).company("RVNL&x=1/../"))
    assert all(p.status == FetchStatus.INVALID_REQUEST for p in bad) and not urls
    payloads = asyncio.run(NseClient(f).company("M&M"))
    assert {p.kind for p in payloads} >= {PayloadKind.NSE_EQUITY_MASTER,
                                          PayloadKind.NSE_ANNOUNCEMENTS,
                                          PayloadKind.NSE_CORPORATE_ACTIONS}
    assert any("symbol=M%26M" in u for u in urls)


def test_bse_pagination_is_bounded():
    pages: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        pages.append(str(req.url))
        if "AnnSubCategoryGetData" in str(req.url):
            body = {"Table": [{"NEWSID": str(i)} for i in range(50)],
                    "Table1": [{"ROWCNT": 10_000}]}
            return httpx.Response(200, content=json.dumps(body).encode())
        return httpx.Response(403)

    payloads = asyncio.run(BseClient(fetcher(handler)).company(
        BSE, date(2026, 1, 1), date(2026, 10, 1), max_pages=3))
    assert sum(p.kind == PayloadKind.BSE_ANNOUNCEMENTS for p in payloads) == 3
    assert payloads[0].status == FetchStatus.BLOCKED
    assert asyncio.run(BseClient(fetcher(handler)).company(
        "54264", date(2026, 1, 1), date(2026, 10, 1)))[0].status == FetchStatus.INVALID_REQUEST
    assert asyncio.run(BseClient(fetcher(handler)).company(
        BSE, date(2026, 1, 1), date(2026, 10, 1), max_pages=99))[0].status \
        == FetchStatus.INVALID_REQUEST


def test_acquire_end_to_end_offline():
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/instruments/NSE"):
            return httpx.Response(200, content=kite_master().content)
        if "/quote" in url:
            return httpx.Response(200, content=quote_payload().content)
        if "/historical/" in url:
            days = business_days(date(2026, 9, 1), date(2026, 10, 9))
            return httpx.Response(200, content=history_payload(series(
                days, lambda i: Decimal(100 + i))).content)
        if "EQUITY_L" in url:
            return httpx.Response(200, content=nse_master(
                (SYMBOL, "Rail Vikas Nigam Limited", ISIN)).content)
        if "corporate-announcements" in url:
            return httpx.Response(200, content=order_payloads()[-1].content)
        if "bseindia" in url:
            return httpx.Response(403, content=b"Access Denied")
        return httpx.Response(200, content=b"[]")

    req = request()
    payloads = asyncio.run(exchange_source.acquire(req, fetcher(handler, max_requests=60), CREDS,
                                          history_days=60))
    data = engine.build_market_intelligence(req, payloads, generated_at=GENERATED)
    assert data.identity.status == IdentityStatus.PARTIAL  # BSE blocked: code unconfirmed
    assert data.snapshot is not None and data.company_history is not None
    assert "SOURCE_BLOCKED" in {i.code for i in data.issues}
    assert len(data.events) == 3
    dumped = engine.canonical_market_intelligence_json(data)
    assert SECRET_TOKEN not in dumped and SECRET_KEY not in dumped


# --- secrets ----------------------------------------------------------------------------------


class FakeKeychain:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        raise AssertionError("never written")

    def delete(self, name: str) -> None:
        raise AssertionError("never deleted")


def test_kite_credentials_precedence_and_redaction(tmp_path):
    env = {"KITE_API_KEY": SECRET_KEY, "KITE_ACCESS_TOKEN": SECRET_TOKEN,
           "KITE_API_SECRET": "never-read-secret-123"}
    creds = resolve_kite_credentials(environ=env, dotenv_path=None, keychain=FakeKeychain({}))
    assert creds is not None and creds.source == "environment"
    assert creds.access_token.get_secret_value() == SECRET_TOKEN
    assert SECRET_TOKEN in logging_setup._registered()
    assert "never-read-secret-123" not in logging_setup._registered()
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"KITE_API_KEY={SECRET_KEY}\nKITE_ACCESS_TOKEN={SECRET_TOKEN}\n")
    from_file = resolve_kite_credentials(environ={}, dotenv_path=dotenv,
                                         keychain=FakeKeychain({}))
    assert from_file is not None and from_file.api_key.get_secret_value() == SECRET_KEY
    keychain = resolve_kite_credentials(
        environ={"KITE_API_KEY": "only-a-key-123456"}, dotenv_path=None,
        keychain=FakeKeychain({"kite-api-key": KC_KEY, "kite-access-token": KC_TOKEN}))
    assert keychain is not None and keychain.source == "keychain"
    assert keychain.api_key.get_secret_value() == KC_KEY  # pairs are never mixed
    assert resolve_kite_credentials(environ={}, dotenv_path=None,
                                    keychain=FakeKeychain({})) is None


def test_no_secret_literals_in_box12_sources():
    root = Path(engine.__file__).parents[1]
    for path in [root / "adapters/exchange/source.py",
                 *[(root / "application" / f"{m}.py") for m in (
                     "market_intelligence", "market_data", "exchange_intelligence",
                     "corporate_events", "order_ledger", "source_coverage")],
                 root / "domain/market_intelligence.py"]:
        text = path.read_text()
        assert not re.search(r"(api_key|access_token|secret)\s*=\s*['\"][A-Za-z0-9]{8,}", text)
        assert not re.search(r"[\"']Cookie[\"']\s*:|cookies=", text)


def test_module_constants_are_bounded():
    assert exchange_source.KITE_MAX_DAYS_PER_REQUEST == 2000
    assert exchange_source.MAX_NETWORK_RETRIES <= 2 and exchange_source.MAX_BSE_PAGES <= 10
    assert exchange_source.ALLOWED_HOSTS == frozenset({
        "api.kite.trade", "www.nseindia.com", "nsearchives.nseindia.com", "api.bseindia.com",
        "www.bseindia.com"})
