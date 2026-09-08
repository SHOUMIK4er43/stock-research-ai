"""Central application operations, independent of transport and persistence technology."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from stock_research_ai.application.ports import (
    CompanyRepository,
    DocumentAcquirer,
    DocumentNormalizer,
    DocumentRepository,
    ExtractionRepository,
    MarketNormalizer,
    MarketRepository,
    MarketSource,
    RawArtifactStore,
    SourceAcquirer,
    SourceNormalizer,
)
from stock_research_ai.domain.company import Company, CompanyRegistration
from stock_research_ai.domain.documents import DocumentRequest, DocumentSearchHit, ParsedDocument
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest
from stock_research_ai.domain.market import MarketExtractionResult, MarketRequest
from stock_research_ai.domain.provenance import AcquiredPayload, RawArtifact, SourceRetrieval


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operations: tuple[
        Literal["get_capabilities"], Literal["register_company"], Literal["get_company"]
    ] = ("get_capabilities", "register_company", "get_company")
    analysis_available: Literal[False] = False


@dataclass(frozen=True)
class ExtractionServices:
    source: SourceAcquirer
    artifacts: RawArtifactStore
    normalizer: SourceNormalizer
    repository: ExtractionRepository


@dataclass(frozen=True)
class DocumentServices:
    source: DocumentAcquirer
    artifacts: RawArtifactStore
    normalizer: DocumentNormalizer
    repository: DocumentRepository


@dataclass(frozen=True)
class MarketServices:
    source: MarketSource
    artifacts: RawArtifactStore
    normalizer: MarketNormalizer
    repository: MarketRepository


class ResearchEngine:
    def __init__(
        self,
        repository: CompanyRepository,
        extraction: ExtractionServices | None = None,
        documents: DocumentServices | None = None,
        market: MarketServices | None = None,
    ) -> None:
        self._repository = repository
        self._extraction = extraction
        self._documents = documents
        self._market = market

    async def get_capabilities(self) -> Capabilities:
        return Capabilities()

    async def register_company(self, registration: CompanyRegistration) -> Company:
        company = Company(
            id=uuid4(),
            name=registration.name,
            listings=registration.listings,
            provider_identifiers=registration.provider_identifiers,
        )
        await self._repository.save(company)
        return company

    async def get_company(self, company_id: UUID) -> Company | None:
        return await self._repository.get(company_id)

    async def extract_company(self, request: ExtractionRequest) -> CompanyExtractionResult:
        services = self._extraction
        if services is None:
            raise RuntimeError("Extraction services are not configured")
        payload = await services.source.acquire(request)
        artifact = await services.artifacts.put(payload) if payload.content is not None else None
        await services.repository.record_evidence(payload.retrieval, artifact)
        # Normalize only the verified bytes read back from durable raw storage.
        content = await services.artifacts.read(artifact) if artifact is not None else None
        company_id = uuid4()
        if artifact is not None and content is not None:
            identity = services.normalizer.identify(
                content, artifact, payload.retrieval, company_id
            )
            if identity is not None:
                company_id = await services.repository.bind_company(identity)
        result = services.normalizer.normalize(content, artifact, payload.retrieval, company_id)
        await services.repository.save_extraction(result)
        return result

    async def ingest_document(self, request: DocumentRequest) -> ParsedDocument:
        services = self._documents
        if services is None:
            raise RuntimeError("Document services are not configured")
        if await self._repository.get(request.company_id) is None:
            raise ValueError("Register the company before ingesting documents")
        identity = await services.repository.bind_document(request)
        payload = await services.source.acquire(request)
        artifact = await services.artifacts.put(payload) if payload.content is not None else None
        await services.repository.record_evidence(payload.retrieval, artifact)
        content = await services.artifacts.read(artifact) if artifact else None
        document = services.normalizer.normalize(
            request, identity, payload.retrieval, artifact, content
        )
        await services.repository.save_document(document)
        return document

    async def get_document(self, version_id: UUID) -> ParsedDocument | None:
        if self._documents is None:
            raise RuntimeError("Document services are not configured")
        return await self._documents.repository.get_document(version_id)

    async def search_company_documents(
        self, company_id: UUID, query: str, limit: int = 20
    ) -> tuple[DocumentSearchHit, ...]:
        if self._documents is None:
            raise RuntimeError("Document services are not configured")
        if not query.strip() or not 1 <= limit <= 100:
            raise ValueError("Search requires nonempty text and a limit between 1 and 100")
        return await self._documents.repository.search(company_id, query, limit)

    async def ingest_market_data(self, request: MarketRequest) -> MarketExtractionResult:
        services = self._market
        if services is None:
            raise RuntimeError("Market services are not configured")
        evidence: list[tuple[SourceRetrieval, RawArtifact | None, bytes | None]] = []

        async def preserve(payload: AcquiredPayload) -> tuple[RawArtifact | None, bytes | None]:
            artifact = (
                await services.artifacts.put(payload) if payload.content is not None else None
            )
            await services.repository.record_evidence(payload.retrieval, artifact)
            content = await services.artifacts.read(artifact) if artifact else None
            evidence.append((payload.retrieval, artifact, content))
            return artifact, content

        master = await services.source.acquire_master(request)
        artifact, content = await preserve(master)
        instrument = None
        identity_error = None
        if master.retrieval.status == "success" and artifact is not None and content is not None:
            try:
                instrument = services.normalizer.identify(request, content, artifact)
            except ValueError as exc:
                identity_error = str(exc)
        else:
            identity_error = "Instrument master acquisition failed"
        if instrument is not None:
            for payload in await services.source.acquire_prices(request, instrument):
                await preserve(payload)
        result = services.normalizer.normalize(request, instrument, tuple(evidence), identity_error)
        await services.repository.save_market(result)
        return result

    async def get_market(self, extraction_id: UUID) -> MarketExtractionResult | None:
        if self._market is None:
            raise RuntimeError("Market services are not configured")
        return await self._market.repository.get_market(extraction_id)

    async def market_history(self, instrument_id: UUID) -> tuple[MarketExtractionResult, ...]:
        if self._market is None:
            raise RuntimeError("Market services are not configured")
        return await self._market.repository.market_history(instrument_id)
