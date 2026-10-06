from urllib.parse import urlparse

from stock_research_ai.adapters.documents.source import DocumentSource
from stock_research_ai.domain.documents import DocumentRequest
from stock_research_ai.domain.provenance import AcquiredPayload
from stock_research_ai.domain.publications import PublicationRequest


class PublicationSource:
    def __init__(self, source: DocumentSource | None = None) -> None:
        self._source = source or DocumentSource()

    async def acquire(self, request: PublicationRequest) -> AcquiredPayload:
        original = urlparse(request.source_url).hostname
        acquired = urlparse(request.url).hostname
        if request.format == "RBI_HTML":
            if original != "www.rbi.org.in" or acquired != original:
                raise ValueError("RBI adapter requires official RBI URLs")
            provider = "rbi"
        else:
            if original not in {"www.yesbank.in", "www.yes.bank.in", "www.bseindia.com"}:
                raise ValueError("Issuer source-of-record URL required")
            if acquired not in {original, "bsmedia.business-standard.com"}:
                raise ValueError("Unsupported issuer acquisition host")
            provider = "yesbank" if acquired == original else "business_standard_filing_mirror"
        payload = await self._source.acquire(
            DocumentRequest(
                url=request.url,
                provider=provider,
                replay_path=request.replay_file,
            )
        )
        return payload.model_copy(update={"source_type": "publication"})
