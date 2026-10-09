"""DETAILED_RESEARCH_V1 Box 2: safe HTTP retrieval of public evidence documents.

Reuses the existing document safety primitives (validate_url, public_addresses):
HTTPS only, no embedded credentials, no localhost/private/link-local/reserved
destinations - checked by DNS on EVERY hop, with every redirect target re-validated,
https->http downgrades refused, redirect count and loops bounded. Bodies are streamed
under a byte cap with a whole-request deadline. No authentication is ever sent and no
cookies are carried between hops. Failures are typed outcomes, never exceptions.
"""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from stock_research_ai.adapters.documents.pinned import PinnedTransport, is_public_address, pin_key
from stock_research_ai.adapters.documents.source import public_addresses, validate_url
from stock_research_ai.adapters.raw_artifacts import FileRawArtifactStore
from stock_research_ai.domain.evidence_retrieval import FetchResult, RedirectHop, StoredArtifact
from stock_research_ai.domain.provenance import AcquiredPayload, RetrievalStatus, SourceRetrieval

USER_AGENT = "StockResearchAI/0.1 (public evidence research)"
_BLOCKED_STATUSES = frozenset({401, 403, 406, 429, 451})


def unsupported_code(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "http":
        return "NOT_HTTPS"
    if parts.scheme != "https":
        return "UNSUPPORTED_SCHEME"
    if parts.username or parts.password:
        return "CREDENTIALS_IN_URL"
    return "PRIVATE_DESTINATION"


def _public(addresses: list[str]) -> bool:
    return bool(addresses) and all(is_public_address(a) for a in addresses)


class SafeEvidenceFetcher:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        lookup: Callable[[str], Awaitable[list[str]]] = public_addresses,
    ) -> None:
        self._transport = transport
        self._lookup = lookup

    async def fetch(
        self, url: str, *, max_bytes: int, timeout_seconds: float, max_redirects: int
    ) -> FetchResult:
        hops: list[RedirectHop] = []
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._fetch(url, max_bytes, timeout_seconds, max_redirects, hops)
        except TimeoutError:
            return FetchResult(outcome="TIMEOUT", code="DEADLINE_EXCEEDED", redirects=tuple(hops))

    async def _fetch(
        self,
        url: str,
        max_bytes: int,
        timeout_seconds: float,
        max_redirects: int,
        hops: list[RedirectHop],
    ) -> FetchResult:
        def result(outcome: str, code: str, **fields: object) -> FetchResult:
            return FetchResult.model_validate(
                {"outcome": outcome, "code": code, "redirects": tuple(hops), **fields}
            )

        try:
            current = validate_url(url)
        except ValueError:
            return result("UNSUPPORTED", unsupported_code(url))
        seen = {current}
        # Without an injected transport, each hop connects only to the addresses
        # validated for it below (resolved-IP pinning, no second DNS lookup).
        pins: dict[str, tuple[str, ...]] = {}
        async with httpx.AsyncClient(
            transport=self._transport or PinnedTransport(pins),
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            while True:
                try:
                    host = pin_key(current)
                except ValueError:
                    return result("BLOCKED" if hops else "UNSUPPORTED", "PRIVATE_DESTINATION")
                try:
                    addresses = await self._lookup(host)
                except OSError:
                    return result("FAILED", "DNS_FAILED", final_url=current)
                if not _public(addresses):
                    outcome = "BLOCKED" if hops else "UNSUPPORTED"
                    return result(outcome, "PRIVATE_DESTINATION", final_url=current)
                pins.clear()
                pins[host] = tuple(addresses)
                client.cookies.clear()  # nothing is carried from one hop to the next
                try:
                    async with client.stream("GET", current) as response:
                        status = response.status_code
                        if response.is_redirect:
                            hops.append(RedirectHop(url=current, http_status=status))
                            location = response.headers.get("location")
                            if not location:
                                return result("FAILED", "REDIRECT_WITHOUT_LOCATION")
                            if len(hops) > max_redirects:
                                return result("FAILED", "TOO_MANY_REDIRECTS")
                            target = urljoin(current, location)
                            try:
                                target = validate_url(target)
                            except ValueError:
                                return result("BLOCKED", "UNSAFE_REDIRECT")
                            if target in seen:
                                return result("FAILED", "REDIRECT_LOOP")
                            seen.add(target)
                            current = target
                            continue
                        fields: dict[str, object] = {
                            "final_url": current,
                            "http_status": status,
                            "declared_content_type": (response.headers.get("content-type") or "")[
                                :200
                            ]
                            or None,
                        }
                        if status in (404, 410):
                            return result("NOT_FOUND", f"HTTP_{status}", **fields)
                        if status in _BLOCKED_STATUSES:
                            return result("BLOCKED", f"HTTP_{status}", **fields)
                        if status != 200:
                            return result("FAILED", f"HTTP_{status}", **fields)
                        declared_length = response.headers.get("content-length", "")
                        if declared_length.isdigit() and int(declared_length) > max_bytes:
                            return result("TOO_LARGE", "CONTENT_LENGTH_OVER_LIMIT", **fields)
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > max_bytes:
                                return result("TOO_LARGE", "BODY_OVER_LIMIT", **fields)
                        return FetchResult.model_validate(
                            {
                                "outcome": "OK",
                                "redirects": tuple(hops),
                                "content": bytes(content),
                                **fields,
                            }
                        )
                except httpx.TimeoutException:
                    return result("TIMEOUT", "READ_TIMEOUT", final_url=current)
                except httpx.HTTPError:
                    return result("FAILED", "CONNECTION_FAILED", final_url=current)


class RawArtifactEvidenceWriter:
    """Stores verified bytes in the existing content-addressed, never-overwriting store."""

    def __init__(self, store: FileRawArtifactStore) -> None:
        self._store = store

    async def store(
        self,
        content: bytes,
        *,
        extension: str,
        final_url: str,
        retrieved_at: datetime,
        declared_content_type: str | None,
    ) -> StoredArtifact:
        suffix: Literal["pdf", "html", "bin"] = (
            "pdf" if extension == ".pdf" else "html" if extension == ".html" else "bin"
        )
        artifact = await self._store.put(
            AcquiredPayload(
                retrieval=SourceRetrieval(
                    id=uuid4(),
                    provider="evidence_retrieval",
                    requested_url=final_url,
                    final_url=final_url,
                    retrieved_at=retrieved_at,
                    status=RetrievalStatus.SUCCESS,
                    content_type=declared_content_type,
                ),
                content=content,
                source_type="evidence_document",
                file_extension=suffix,
            )
        )
        return StoredArtifact(
            reference=artifact.content_reference,
            sha256=artifact.content_sha256,
            size_bytes=artifact.size_bytes,
        )


class RawArtifactEvidenceReader:
    """Reads content-addressed bytes back, refusing paths outside the store and any
    bytes whose size or SHA-256 differ from the retrieval record."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    async def read(self, reference: str, sha256: str, size_bytes: int) -> bytes:
        path = (self._root / reference).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError("ARTIFACT_PATH_ESCAPE")
        content = path.read_bytes()
        if len(content) != size_bytes or hashlib.sha256(content).hexdigest() != sha256:
            raise ValueError("ARTIFACT_INTEGRITY_FAILED")
        return content
