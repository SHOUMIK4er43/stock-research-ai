"""Bounded public HTTPS acquisition, with offline file replay."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from stock_research_ai.adapters.documents.pinned import PinnedTransport, is_public_address, pin_key
from stock_research_ai.domain.documents import DocumentRequest
from stock_research_ai.domain.provenance import AcquiredPayload, RetrievalStatus, SourceRetrieval


def validate_url(url: str) -> str:
    parts = urlsplit(url)
    # A malformed port raises a stdlib ValueError that quotes the raw port text: never let
    # it reach an error message (P2-6). -1 is refused below like any other port.
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or port not in {None, 443}
        or "\\" in url
        or any(ord(c) < 32 for c in url)
    ):
        raise ValueError("Documents require a public HTTPS URL without credentials")
    host = parts.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local document URLs are not supported")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not is_public_address(str(address)):
            raise ValueError("Document URL must not address a private network")
    return url


async def public_addresses(host: str) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(record[4][0]) for record in records]


class DocumentSource:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        lookup: Callable[[str], Awaitable[list[str]]] = public_addresses,
        max_bytes: int = 25_000_000,
        timeout: float = 45,
    ) -> None:
        self.transport, self.lookup = transport, lookup
        self.max_bytes, self.timeout = max_bytes, timeout

    async def acquire(self, request: DocumentRequest) -> AcquiredPayload:
        retrieval = SourceRetrieval(
            id=uuid4(),
            provider=request.provider,
            requested_url=request.url,
            final_url=request.final_url or request.url,
            retrieved_at=datetime.now(UTC),
            status=RetrievalStatus.SUCCESS,
            mode="replay" if request.replay_path else "http",
            content_type="application/pdf" if request.replay_path else None,
        )
        content: bytes | None = None
        try:
            validate_url(request.url)
            final = validate_url(request.final_url or request.url)
            if request.replay_path:
                with request.replay_path.open("rb") as handle:
                    content = handle.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    retrieval = retrieval.model_copy(update={"complete": False})
                    raise ValueError(
                        "Document exceeds maximum file size; stored bytes are incomplete"
                    )
            else:
                # Without an injected transport, each hop connects only to the addresses
                # validated for it below (resolved-IP pinning, no second DNS lookup).
                pins: dict[str, tuple[str, ...]] = {}
                transport = self.transport or PinnedTransport(pins)
                async with asyncio.timeout(self.timeout):
                    async with httpx.AsyncClient(
                        transport=transport,
                        timeout=self.timeout,
                        trust_env=False,
                        follow_redirects=False,
                        headers={"User-Agent": "StockResearchAI/0.1 (public document evidence)"},
                    ) as client:
                        for _ in range(5):
                            host = pin_key(final)
                            addresses = await self.lookup(host)
                            if not addresses or not all(is_public_address(a) for a in addresses):
                                raise ValueError(
                                    "Document host does not resolve exclusively to public addresses"
                                )
                            pins.clear()
                            pins[host] = tuple(addresses)
                            content = b""
                            async with client.stream("GET", final) as response:
                                retrieval = retrieval.model_copy(
                                    update={
                                        "final_url": str(response.url),
                                        "http_status": response.status_code,
                                        "content_type": response.headers.get("content-type"),
                                        "complete": False,
                                    }
                                )
                                async for chunk in response.aiter_bytes():
                                    content += chunk[: max(0, self.max_bytes + 1 - len(content))]
                                    if len(content) > self.max_bytes:
                                        raise ValueError(
                                            "Document exceeds maximum file size; "
                                            "stored bytes are incomplete"
                                        )
                                retrieval = retrieval.model_copy(update={"complete": True})
                                if response.is_redirect:
                                    location = response.headers.get("location")
                                    if not location:
                                        raise ValueError("Redirect has no Location")
                                    final = validate_url(urljoin(str(response.url), location))
                                    continue
                                if response.status_code != 200:
                                    retrieval = retrieval.model_copy(
                                        update={
                                            "status": RetrievalStatus.BLOCKED
                                            if response.status_code in {401, 403, 406, 429}
                                            else RetrievalStatus.FAILED,
                                            "error": f"HTTP {response.status_code}",
                                        }
                                    )
                                break
                        else:
                            raise ValueError("Too many redirects")
        except (ValueError, OSError, httpx.HTTPError, TimeoutError) as exc:
            retrieval = retrieval.model_copy(
                update={"status": RetrievalStatus.FAILED, "error": str(exc)}
            )
        # MIME and PDF validity are assessed only after the exact acquired bytes are preserved.
        extension: Literal["pdf", "bin"] = (
            "pdf" if content and content.startswith(b"%PDF-") else "bin"
        )
        return AcquiredPayload(
            retrieval=retrieval,
            content=content,
            source_type="company_document",
            file_extension=extension,
        )
