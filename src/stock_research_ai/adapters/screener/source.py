"""Bounded anonymous HTTP acquisition; replay never constructs an HTTP client."""

import re
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from stock_research_ai.domain.extraction import ExtractionRequest
from stock_research_ai.domain.provenance import AcquiredPayload, RetrievalStatus, SourceRetrieval

MAX_BYTES = 5_000_000


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        # urlsplit's own ValueError (e.g. a netloc invalid under NFKC normalization) quotes
        # the raw netloc: replace it with the fixed text below (P2-6).
        raise ValueError("Expected a public https://www.screener.in/company/.../ URL") from None
    # A malformed port raises a stdlib ValueError that quotes the raw port text: never let
    # it reach an error message (P2-6). -1 is refused below like any other port.
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or parts.hostname != "www.screener.in"
        or parts.username
        or parts.password
        or port not in {None, 443}
        or parts.query
        or parts.fragment
        or not re.fullmatch(r"/company/[A-Za-z0-9_-]+/(?:consolidated/)?", parts.path)
    ):
        raise ValueError("Expected a public https://www.screener.in/company/.../ URL")
    return url


def analytical_page_urls(source_url: str) -> tuple[str, str]:
    """(consolidated, standalone) pages of the URL's company, in acquisition order: the
    first page that yields the company is the primary (analytical-basis) page. Summary and
    Detailed Research both acquire through this one order, so a company page opened in
    either view gives both products the same reporting basis."""
    url = validate_url(source_url)
    slug = urlsplit(url).path.split("/")[2]
    base = f"https://www.screener.in/company/{slug}/"
    return base + "consolidated/", base


class ScreenerSource:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def acquire(self, request: ExtractionRequest) -> AcquiredPayload:
        if request.provider != "screener":
            raise ValueError("Unsupported provider")
        validate_url(request.url)
        final_url = validate_url(request.final_url or request.url)
        retrieval = SourceRetrieval(
            id=uuid4(),
            provider="screener",
            requested_url=request.url,
            final_url=final_url,
            retrieved_at=datetime.now(UTC),
            status=RetrievalStatus.SUCCESS,
            mode="replay" if request.replay_path else "http",
            content_type="text/html",
        )
        if request.replay_path is not None:
            with request.replay_path.open("rb") as handle:
                content = handle.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise ValueError("Replay exceeds the 5 MB limit")
            return AcquiredPayload(retrieval=retrieval, content=content)
        content = b""
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=30,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "StockResearchAI/0.1 (public evidence research)"},
            ) as client:
                for _ in range(4):
                    async with client.stream("GET", final_url) as response:
                        retrieval = retrieval.model_copy(
                            update={
                                "final_url": str(response.url),
                                "http_status": response.status_code,
                                "content_type": response.headers.get("content-type"),
                                "encoding": response.encoding or "utf-8",
                            }
                        )
                        async for chunk in response.aiter_bytes():
                            content += chunk
                            if len(content) > MAX_BYTES:
                                raise ValueError("HTTP body exceeds the 5 MB limit")
                        if response.is_redirect:
                            target = response.headers.get("location")
                            if not target:
                                raise ValueError("Redirect has no Location")
                            final_url = validate_url(urljoin(str(response.url), target))
                            content = b""
                            continue
                        if response.status_code != 200:
                            status = (
                                RetrievalStatus.BLOCKED
                                if response.status_code in {401, 403, 429}
                                else RetrievalStatus.FAILED
                            )
                            retrieval = retrieval.model_copy(
                                update={"status": status, "error": f"HTTP {response.status_code}"}
                            )
                        elif "text/html" not in response.headers.get("content-type", "").lower():
                            retrieval = retrieval.model_copy(
                                update={
                                    "status": RetrievalStatus.FAILED,
                                    "error": "Expected text/html",
                                }
                            )
                        return AcquiredPayload(retrieval=retrieval, content=content)
                raise ValueError("Too many redirects")
        except (httpx.HTTPError, ValueError) as exc:
            return AcquiredPayload(
                retrieval=retrieval.model_copy(
                    update={"status": RetrievalStatus.FAILED, "error": str(exc)}
                ),
                content=content or None,
            )
