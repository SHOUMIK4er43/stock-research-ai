"""Fetch the Screener peer-comparison fragment the company page itself loads.

The page calls GET /api/company/{warehouseId}/peers/ (Screener's own client URL table,
company.customisation.js -> Utils.getUrl("peers")) and injects the returned HTML table.
This is an undocumented internal endpoint, so the fetch is optional and isolated:
one attempt, fixed host, numeric ID only, no redirects, bounded time and size, and
every failure becomes a status code rather than an exception.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx

PEERS_URL = "https://www.screener.in/api/company/{warehouse_id}/peers/"
MAX_PEER_BYTES = 1_000_000

PeerFetchStatus = Literal["RETRIEVED", "FAILED", "BLOCKED"]


@dataclass(frozen=True)
class PeerFetch:
    status: PeerFetchStatus
    code: str | None
    url: str
    retrieved_at: datetime
    content: bytes | None


def peers_url(warehouse_id: str) -> str:
    if not re.fullmatch(r"\d{1,12}", warehouse_id):
        raise ValueError("Screener warehouse IDs are numeric")
    return PEERS_URL.format(warehouse_id=warehouse_id)


class ScreenerPeerClient:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
        max_bytes: int = MAX_PEER_BYTES,
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes

    async def fetch(self, warehouse_id: str) -> PeerFetch:
        url = peers_url(warehouse_id)
        retrieved_at = datetime.now(UTC)

        def outcome(status: PeerFetchStatus, code: str, content: bytes | None = None) -> PeerFetch:
            return PeerFetch(status, code, url, retrieved_at, content)

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "StockResearchAI/0.1 (public evidence research)"},
            ) as client:
                async with client.stream("GET", url) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self._max_bytes:
                            return outcome("FAILED", "RESPONSE_TOO_LARGE")
                    if response.status_code in (401, 403, 429):
                        return outcome("BLOCKED", f"HTTP_{response.status_code}")
                    if response.is_redirect:
                        return outcome("FAILED", "REDIRECT_REFUSED")
                    if response.status_code != 200:
                        return outcome("FAILED", "HTTP_ERROR")
                    if "text/html" not in response.headers.get("content-type", "").lower():
                        return outcome("FAILED", "UNEXPECTED_CONTENT_TYPE")
                    return PeerFetch("RETRIEVED", None, url, retrieved_at, bytes(content))
        except httpx.TimeoutException:
            return outcome("FAILED", "TIMEOUT")
        except httpx.HTTPError:
            return outcome("FAILED", "CONNECTION_FAILED")
