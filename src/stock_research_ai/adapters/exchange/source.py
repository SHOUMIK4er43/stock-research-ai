"""Box 12 bounded acquisition of Kite, NSE and BSE payloads.

Only documented or publicly reachable endpoints are called, with an honest User-Agent and no
cookies, sessions or browser emulation: an anti-bot denial (403) is recorded as BLOCKED and
never worked around. Every request goes through the shared P2 primitives: the URL must be
public HTTPS without credentials (`documents.source.validate_url`) on an allow-listed host,
the host's DNS answers must all be public and are pinned for the connection
(`PinnedTransport`), redirects are refused, the body is size-capped while streaming, each
request is time-boxed, the client has a total request budget, and only network errors (never
401/403/429) are retried, a bounded number of times. Credentials travel only in headers;
errors are fixed codes, never exception text or URLs with secrets.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlencode

import httpx

from stock_research_ai.adapters.documents.pinned import PinnedTransport, is_public_address, pin_key
from stock_research_ai.adapters.documents.source import public_addresses, validate_url
from stock_research_ai.adapters.secret_store import KiteCredentials
from stock_research_ai.application.exchange_intelligence import (
    Budget,
    csv_rows,
    payload_id,
    trusted_source_url,
)
from stock_research_ai.domain.market_intelligence import (
    ALLOWED_SOURCE_HOSTS,
    IST,
    MAX_HISTORY_DAYS,
    MAX_MASTER_ROWS,
    MAX_PAYLOAD_BYTES,
    FetchStatus,
    MarketIntelligenceRequest,
    PayloadKind,
    RawPayload,
)

ALLOWED_HOSTS = ALLOWED_SOURCE_HOSTS
USER_AGENT = "StockResearchAI/0.1 (public exchange evidence)"
KITE_API = "https://api.kite.trade"
NSE_API = "https://www.nseindia.com/api"
NSE_EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
#: Kite serves at most 2000 days of daily candles per request.
KITE_MAX_DAYS_PER_REQUEST = 2000
MAX_QUOTE_INSTRUMENTS = 20
MAX_BSE_PAGES = 10
BSE_PAGE_SIZE = 50
MAX_NETWORK_RETRIES = 2

NSE_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&_-]{0,19}$")
BSE_CODE = re.compile(r"^[0-9]{6}$")
KITE_TOKEN = re.compile(r"^[0-9]{1,12}$")
#: 'NSE:RVNL', 'NSE:NIFTY 50'
KITE_INSTRUMENT = re.compile(r"^(NSE|BSE):[A-Z0-9][A-Z0-9 &_.-]{0,39}$")

Lookup = Callable[[str], Awaitable[list[str]]]


def validate_source_url(url: str) -> str:
    """Public HTTPS on an allow-listed exchange/provider host; raises with fixed text. The
    shared P2 document rule plus the Box 12 host allow-list."""
    trusted_source_url(url)
    validate_url(url)
    return url


def _status_for(http_status: int) -> tuple[FetchStatus, str]:
    if http_status == 401:
        return FetchStatus.AUTH_REQUIRED, "HTTP_401"
    if http_status == 403:
        return FetchStatus.BLOCKED, "HTTP_403"
    if http_status == 429:
        return FetchStatus.RATE_LIMITED, "HTTP_429"
    if http_status == 404:
        return FetchStatus.NOT_FOUND, "HTTP_404"
    if 300 <= http_status < 400:
        return FetchStatus.UNSUPPORTED, "REDIRECT_REFUSED"
    return FetchStatus.HTTP_ERROR, f"HTTP_{http_status}"


class ExchangeFetcher:
    """One bounded HTTP client for all Box 12 sources. Safe to share between tasks."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        lookup: Lookup = public_addresses,
        max_bytes: int = 8_000_000,
        timeout: float = 30.0,
        max_requests: int = 60,
        network_retries: int = 1,
        min_interval: float = 0.5,
        retry_delay: float = 1.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 0 < max_bytes <= MAX_PAYLOAD_BYTES:
            raise ValueError("max_bytes outside the Box 12 bound")
        if not 0 <= network_retries <= MAX_NETWORK_RETRIES:
            raise ValueError("network_retries outside the Box 12 bound")
        if not 0 < timeout <= 120 or not 0 < max_requests <= 500:
            raise ValueError("timeout or request budget outside the Box 12 bound")
        self._transport = transport
        self._lookup = lookup
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._budget = max_requests
        self._retries = network_retries
        self._min_interval = min_interval
        self._retry_delay = retry_delay
        self._clock = clock
        self._budget_lock = asyncio.Lock()
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    @property
    def remaining_requests(self) -> int:
        return self._budget

    def _payload(
        self,
        kind: PayloadKind,
        url: str,
        status: FetchStatus,
        code: str | None,
        *,
        http_status: int | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> RawPayload:
        return RawPayload(
            kind=kind,
            url=url,
            status=status,
            http_status=http_status,
            retrieved_at=self._clock(),
            content=content,
            content_type=content_type[:200] if content_type else None,
            error_code=code,
        )

    async def _spend(self) -> bool:
        async with self._budget_lock:
            if self._budget <= 0:
                return False
            self._budget -= 1
            return True

    async def fetch(
        self, kind: PayloadKind, url: str, *, headers: Mapping[str, str] | None = None
    ) -> RawPayload:
        try:
            validate_source_url(url)
        except ValueError:
            # The rejected URL is never echoed: record the fixed endpoint family only.
            return self._payload(kind, _safe_placeholder(kind), FetchStatus.INVALID_REQUEST,
                                 "INVALID_SOURCE_URL")
        attempt = 0
        while True:
            if not await self._spend():
                return self._payload(kind, url, FetchStatus.UNSUPPORTED, "REQUEST_BUDGET_EXHAUSTED")
            payload = await self._once(kind, url, headers or {})
            retryable = payload.status in (FetchStatus.NETWORK_ERROR, FetchStatus.TIMEOUT)
            if not retryable or attempt >= self._retries:
                return payload
            attempt += 1
            await asyncio.sleep(self._retry_delay)

    async def _once(self, kind: PayloadKind, url: str, headers: Mapping[str, str]) -> RawPayload:
        host = pin_key(url)
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._min_interval - (time.monotonic() - self._last_request.get(host, -1e9))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()
            try:
                async with asyncio.timeout(self._timeout):
                    return await self._request(kind, url, host, headers)
            except TimeoutError:
                return self._payload(kind, url, FetchStatus.TIMEOUT, "TIMEOUT")
            except ValueError:
                return self._payload(kind, url, FetchStatus.NETWORK_ERROR, "NON_PUBLIC_ADDRESS")
            except (httpx.HTTPError, OSError):
                # Exception text may quote request details: never stored.
                return self._payload(kind, url, FetchStatus.NETWORK_ERROR, "NETWORK_ERROR")

    async def _request(
        self, kind: PayloadKind, url: str, host: str, headers: Mapping[str, str]
    ) -> RawPayload:
        pins: dict[str, tuple[str, ...]] = {}
        transport = self._transport
        if transport is None:
            addresses = await self._lookup(host)
            if not addresses or not all(is_public_address(a) for a in addresses):
                raise ValueError("Host does not resolve exclusively to public addresses")
            pins[host] = tuple(addresses)
            transport = PinnedTransport(pins)
        request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*", **headers}
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=self._timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream("GET", url, headers=request_headers) as response,
        ):
            content_type = response.headers.get("content-type")
            if response.status_code != 200:
                status, code = _status_for(response.status_code)
                return self._payload(
                    kind, url, status, code, http_status=response.status_code,
                    content_type=content_type,
                )
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > self._max_bytes:
                return self._payload(
                    kind, url, FetchStatus.TOO_LARGE, "SIZE_LIMIT", http_status=200,
                    content_type=content_type,
                )
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > self._max_bytes:
                    return self._payload(
                        kind, url, FetchStatus.TOO_LARGE, "SIZE_LIMIT", http_status=200,
                        content_type=content_type,
                    )
            return self._payload(
                kind, url, FetchStatus.OK, None, http_status=200, content=bytes(buffer),
                content_type=content_type,
            )


def _safe_placeholder(kind: PayloadKind) -> str:
    if kind.startswith("KITE"):
        return KITE_API
    if kind.startswith("NSE"):
        return NSE_API
    return BSE_API


# --- Kite -------------------------------------------------------------------------------------


class KiteMarketClient:
    """Read-only Kite Connect market data: instruments, quotes, daily history. No orders."""

    def __init__(self, fetcher: ExchangeFetcher, credentials: KiteCredentials | None) -> None:
        self._fetcher = fetcher
        self._credentials = credentials

    def _headers(self) -> dict[str, str] | None:
        if self._credentials is None:
            return None
        key = self._credentials.api_key.get_secret_value()
        token = self._credentials.access_token.get_secret_value()
        return {"X-Kite-Version": "3", "Authorization": f"token {key}:{token}"}

    async def _get(self, kind: PayloadKind, url: str) -> RawPayload:
        headers = self._headers()
        if headers is None:
            return self._fetcher._payload(
                kind, url, FetchStatus.AUTH_REQUIRED, "KITE_CREDENTIALS_NOT_CONFIGURED"
            )
        return await self._fetcher.fetch(kind, url, headers=headers)

    async def instruments(self, exchange: str = "NSE") -> RawPayload:
        if exchange not in ("NSE", "BSE"):
            return self._fetcher._payload(
                PayloadKind.KITE_INSTRUMENTS, KITE_API, FetchStatus.INVALID_REQUEST,
                "UNSUPPORTED_EXCHANGE",
            )
        return await self._get(PayloadKind.KITE_INSTRUMENTS, f"{KITE_API}/instruments/{exchange}")

    async def quote(self, instruments: Sequence[str]) -> RawPayload:
        if (
            not instruments
            or len(instruments) > MAX_QUOTE_INSTRUMENTS
            or not all(KITE_INSTRUMENT.fullmatch(i) for i in instruments)
        ):
            return self._fetcher._payload(
                PayloadKind.KITE_QUOTE, f"{KITE_API}/quote", FetchStatus.INVALID_REQUEST,
                "MALFORMED_INSTRUMENT",
            )
        query = urlencode([("i", i) for i in instruments], quote_via=quote)
        return await self._get(PayloadKind.KITE_QUOTE, f"{KITE_API}/quote?{query}")

    async def historical(self, token: str, start: date, end: date) -> tuple[RawPayload, ...]:
        base = f"{KITE_API}/instruments/historical"
        if not KITE_TOKEN.fullmatch(token) or end < start or (end - start).days > MAX_HISTORY_DAYS:
            return (
                self._fetcher._payload(
                    PayloadKind.KITE_HISTORICAL, base, FetchStatus.INVALID_REQUEST,
                    "INVALID_HISTORY_REQUEST",
                ),
            )
        payloads = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=KITE_MAX_DAYS_PER_REQUEST - 1), end)
            query = urlencode(
                {
                    "from": f"{chunk_start} 00:00:00",
                    "to": f"{chunk_end} 23:59:59",
                    "continuous": "0",
                    "oi": "0",
                },
                quote_via=quote,
            )
            payloads.append(
                await self._get(PayloadKind.KITE_HISTORICAL, f"{base}/{token}/day?{query}")
            )
            chunk_start = chunk_end + timedelta(days=1)
        return tuple(payloads)


# --- NSE --------------------------------------------------------------------------------------

NSE_COMPANY_ENDPOINTS: tuple[tuple[PayloadKind, str], ...] = (
    (PayloadKind.NSE_ANNOUNCEMENTS, "corporate-announcements?index=equities&symbol={s}"),
    (PayloadKind.NSE_CORPORATE_ACTIONS, "corporates-corporateActions?index=equities&symbol={s}"),
    (PayloadKind.NSE_SHAREHOLDING, "corporate-share-holdings-master?index=equities&symbol={s}"),
    (PayloadKind.NSE_ANNUAL_REPORTS, "annual-reports?index=equities&symbol={s}"),
    (PayloadKind.NSE_INSIDER_TRADING, "corporates-pit?index=equities&symbol={s}"),
    (
        PayloadKind.NSE_FINANCIAL_RESULTS,
        "corporates-financial-results?index=equities&symbol={s}&period=Quarterly",
    ),
    (PayloadKind.NSE_BRSR, "corporate-bussiness-sustainabilitiy?index=equities&symbol={s}"),
    (PayloadKind.NSE_CREDIT_RATINGS, "corporate-credit-rating?index=equities&symbol={s}"),
)


SHAREHOLDING_XBRL_FILINGS = 3  # the latest quarters a pledge reading and its trend need


def shareholding_xbrl_urls(payload: RawPayload) -> list[str]:
    """The XBRL URLs of the latest shareholding filings in an NSE feed payload (newest first,
    at most SHAREHOLDING_XBRL_FILINGS); nothing on any parse problem."""
    if payload.status != FetchStatus.OK or payload.content is None:
        return []
    try:
        body = json.loads(payload.content)
    except (ValueError, RecursionError):
        return []
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        return []
    dated: list[tuple[date, str]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("xbrl"), str):
            continue
        try:
            day = datetime.strptime(str(row.get("date")), "%d-%b-%Y").date()
        except ValueError:
            continue
        dated.append((day, row["xbrl"]))
    urls: list[str] = []
    for _day, url in sorted(dated, reverse=True):
        if url not in urls:
            urls.append(url)
    return urls[:SHAREHOLDING_XBRL_FILINGS]


class NseClient:
    def __init__(self, fetcher: ExchangeFetcher) -> None:
        self._fetcher = fetcher

    async def equity_master(self) -> RawPayload:
        return await self._fetcher.fetch(PayloadKind.NSE_EQUITY_MASTER, NSE_EQUITY_MASTER_URL)

    async def company(self, symbol: str) -> tuple[RawPayload, ...]:
        if not NSE_SYMBOL.fullmatch(symbol):
            return tuple(
                self._fetcher._payload(kind, NSE_API, FetchStatus.INVALID_REQUEST,
                                       "MALFORMED_SYMBOL")
                for kind, _ in NSE_COMPANY_ENDPOINTS
            )
        encoded = quote(symbol, safe="")
        payloads = [await self.equity_master()]
        for kind, path in NSE_COMPANY_ENDPOINTS:
            payloads.append(
                await self._fetcher.fetch(kind, f"{NSE_API}/{path.format(s=encoded)}")
            )
            if kind == PayloadKind.NSE_SHAREHOLDING:
                # the latest filings' own XBRL: the feed does not carry the promoter pledge /
                # encumbrance declarations (allowlisted archives host, bounded count)
                for url in shareholding_xbrl_urls(payloads[-1]):
                    payloads.append(
                        await self._fetcher.fetch(PayloadKind.NSE_SHAREHOLDING_XBRL, url))
        return tuple(payloads)


# --- BSE --------------------------------------------------------------------------------------


class BseClient:
    def __init__(self, fetcher: ExchangeFetcher) -> None:
        self._fetcher = fetcher

    async def company(
        self, code: str, start: date, end: date, *, max_pages: int = 5
    ) -> tuple[RawPayload, ...]:
        if not BSE_CODE.fullmatch(code) or end < start or not 1 <= max_pages <= MAX_BSE_PAGES:
            return (
                self._fetcher._payload(
                    PayloadKind.BSE_COMPANY_HEADER, BSE_API, FetchStatus.INVALID_REQUEST,
                    "MALFORMED_BSE_REQUEST",
                ),
            )
        payloads = [
            await self._fetcher.fetch(
                PayloadKind.BSE_COMPANY_HEADER,
                f"{BSE_API}/ComHeadernew/w?quotetype=EQ&scripcode={code}&seriesid=",
            )
        ]
        for page in range(1, max_pages + 1):
            query = urlencode(
                {
                    "pageno": page,
                    "strCat": "-1",
                    "strPrevDate": start.strftime("%Y%m%d"),
                    "strScrip": code,
                    "strSearch": "P",
                    "strToDate": end.strftime("%Y%m%d"),
                    "strType": "C",
                    "subcategory": "-1",
                }
            )
            payload = await self._fetcher.fetch(
                PayloadKind.BSE_ANNOUNCEMENTS, f"{BSE_API}/AnnSubCategoryGetData/w?{query}"
            )
            payloads.append(payload)
            if not _bse_has_next_page(payload, page):
                break
        payloads.append(
            await self._fetcher.fetch(
                PayloadKind.BSE_CORPORATE_ACTIONS, f"{BSE_API}/CorporateAction/w?scripcode={code}"
            )
        )
        return tuple(payloads)


def _bse_has_next_page(payload: RawPayload, page: int) -> bool:
    """Pagination only: stops on any failure, an empty or short page, or the stated count."""
    if payload.status != FetchStatus.OK or payload.content is None:
        return False
    try:
        body = json.loads(payload.content)
        rows = body.get("Table") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) < BSE_PAGE_SIZE:
            return False
        meta = body.get("Table1")
        if isinstance(meta, list) and meta and isinstance(meta[0], dict):
            total = meta[0].get("ROWCNT")
            if isinstance(total, int) and page * BSE_PAGE_SIZE >= total:
                return False
        return True
    except (ValueError, RecursionError):
        return False


async def acquire(
    request: MarketIntelligenceRequest,
    fetcher: ExchangeFetcher,
    credentials: KiteCredentials | None,
    *,
    history_days: int = 3_650,
    bse_pages: int = 3,
) -> tuple[RawPayload, ...]:
    """Bounded live acquisition for one Box 12 request. Never raises on a source failure:
    every failure is a RawPayload with a status and a fixed error code."""
    kite = KiteMarketClient(fetcher, credentials)
    payloads: list[RawPayload] = []
    master = await kite.instruments("NSE")
    payloads.append(master)
    if request.nse_symbol:
        payloads += await NseClient(fetcher).company(request.nse_symbol)
        payloads.append(await kite.quote([f"NSE:{request.nse_symbol}"]))
    end = request.as_of.astimezone(IST).date()
    start = end - timedelta(days=min(history_days, 3_650))
    if request.bse_code:
        payloads += await BseClient(fetcher).company(
            request.bse_code, end - timedelta(days=365), end, max_pages=bse_pages
        )
    tokens: set[str] = set()
    if master.status == FetchStatus.OK and master.content is not None:
        wanted = {("NSE", request.nse_symbol, "NSE")} | {
            (b.exchange, b.symbol, "INDICES") for b in request.benchmarks
        }
        rows = csv_rows(master, payload_id(master), [], Budget(remaining=MAX_MASTER_ROWS))
        for _, row in rows:
            key = (row.get("EXCHANGE"), row.get("TRADINGSYMBOL"), row.get("SEGMENT"))
            if key in wanted and (
                row.get("SEGMENT") == "INDICES" or row.get("INSTRUMENT_TYPE") == "EQ"
            ):
                tokens.add(row.get("INSTRUMENT_TOKEN", ""))
    for token in sorted(tokens):
        payloads += await kite.historical(token, start, end)
    return tuple(payloads)


__all__ = [
    "ALLOWED_HOSTS",
    "BseClient",
    "ExchangeFetcher",
    "acquire",
    "KiteMarketClient",
    "NseClient",
    "validate_source_url",
]
