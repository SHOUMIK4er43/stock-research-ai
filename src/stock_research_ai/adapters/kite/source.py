"""Documented Kite REST endpoints, bounded acquisition, and offline byte replay."""

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from stock_research_ai.domain.market import MarketInstrument, MarketRequest
from stock_research_ai.domain.provenance import AcquiredPayload, SourceRetrieval
from stock_research_ai.logging_setup import register_secret

MASTER_URL = "https://api.kite.trade/instruments/NSE"


class KiteSource:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        access_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_bytes: int = 20_000_000,
        timeout: float = 40,
    ) -> None:
        self._key = api_key
        self._token = access_token
        register_secret(api_key)
        register_secret(access_token)
        self._transport = transport
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    @classmethod
    def from_environment(cls) -> "KiteSource":
        values = {**dotenv_values(".env"), **os.environ}
        return cls(api_key=values.get("KITE_API_KEY"), access_token=values.get("KITE_ACCESS_TOKEN"))

    async def acquire_master(self, request: MarketRequest) -> AcquiredPayload:
        if request.exchange != "NSE":
            raise ValueError("This adapter currently supports NSE only")
        return await self._acquire(MASTER_URL, request.master_file, False, "market_instruments")

    async def acquire_prices(
        self, request: MarketRequest, instrument: MarketInstrument
    ) -> tuple[AcquiredPayload, ...]:
        start = request.start
        results = []
        while start <= request.end:
            end = min(start + timedelta(days=1999), request.end)
            if request.candles_file:
                end = request.end
            url = str(
                httpx.URL(
                    f"https://api.kite.trade/instruments/historical/{instrument.provider_identifier}/day",
                    params={
                        "from": f"{start} 00:00:00",
                        "to": f"{end} 23:59:59",
                        "continuous": "0",
                        "oi": "0",
                    },
                )
            )
            results.append(await self._acquire(url, request.candles_file, True, "market_candles"))
            start = end + timedelta(days=1)
        return tuple(results)

    async def _acquire(
        self, url: str, replay: Path | None, authenticated: bool, source_type: str
    ) -> AcquiredPayload:
        content: bytes | None = None
        status = "failed"
        error = None
        http_status = None
        content_type = None
        complete = True
        try:
            if replay:
                with replay.open("rb") as handle:
                    content = handle.read(self._max_bytes + 1)
                content_type = (
                    "text/csv" if source_type == "market_instruments" else "application/json"
                )
                status = "success"
            elif authenticated and (not self._key or not self._token):
                status, error = (
                    "blocked",
                    "AUTH_REQUIRED: configure KITE_API_KEY and KITE_ACCESS_TOKEN",
                )
            else:
                headers = {"X-Kite-Version": "3"}
                if authenticated:
                    headers["Authorization"] = f"token {self._key}:{self._token}"
                async with self._lock:
                    await asyncio.sleep(max(0, 0.36 - (time.monotonic() - self._last_request)))
                    self._last_request = time.monotonic()
                    async with (
                        asyncio.timeout(self._timeout),
                        httpx.AsyncClient(
                            transport=self._transport,
                            timeout=self._timeout,
                            follow_redirects=False,
                            trust_env=False,
                        ) as client,
                    ):
                        async with client.stream("GET", url, headers=headers) as response:
                            http_status = response.status_code
                            content_type = response.headers.get("content-type")
                            buffer = bytearray()
                            async for chunk in response.aiter_bytes():
                                buffer.extend(chunk[: self._max_bytes + 1 - len(buffer)])
                                if len(buffer) > self._max_bytes:
                                    break
                            content = bytes(buffer)
                            status = "success" if http_status == 200 else "failed"
                            if http_status in (401, 403, 429):
                                status = "blocked"
                            if http_status != 200:
                                error = f"HTTP_{http_status}: request rejected; no automatic retry"
            if content is not None and len(content) > self._max_bytes:
                content = content[: self._max_bytes]
                complete, status, error = False, "failed", "SIZE_LIMIT: preserved prefix only"
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            # Never persist exception strings that might include credential-bearing requests.
            status, error, complete = "failed", type(exc).__name__, False
        retrieval = SourceRetrieval.model_validate(
            {
                "id": uuid4(),
                "provider": "kite",
                "requested_url": url,
                "final_url": url,
                "retrieved_at": datetime.now(UTC),
                "status": status,
                "http_status": http_status,
                "content_type": content_type,
                "complete": complete,
                "error": error,
                "mode": "replay" if replay else "http",
                "collector_version": "kite-rest/1",
            }
        )
        return AcquiredPayload(
            retrieval=retrieval, content=content, source_type=source_type, file_extension="bin"
        )
