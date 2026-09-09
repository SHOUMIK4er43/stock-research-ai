"""Central application operations, independent of transport and persistence technology."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from stock_research_ai.application.calculations import calculate
from stock_research_ai.application.market_calculations import calculate_market
from stock_research_ai.application.ports import (
    CalculationRepository,
    CompanyRepository,
    ContextRepository,
    DocumentAcquirer,
    DocumentNormalizer,
    DocumentRepository,
    ExtractionRepository,
    MacroAcquirer,
    MacroParser,
    MarketCalculationRepository,
    MarketNormalizer,
    MarketRepository,
    MarketSource,
    PublicationAcquirer,
    PublicationParser,
    RawArtifactStore,
    SemanticResolver,
    SourceAcquirer,
    SourceNormalizer,
)
from stock_research_ai.application.sector_kpis import evaluate_kpis
from stock_research_ai.domain.calculations import CalculationRequest, CalculationSnapshot
from stock_research_ai.domain.company import Company, CompanyRegistration
from stock_research_ai.domain.documents import DocumentRequest, DocumentSearchHit, ParsedDocument
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest
from stock_research_ai.domain.macro import MacroObservation, MacroRequest, MacroSnapshot
from stock_research_ai.domain.market import MarketExtractionResult, MarketRequest
from stock_research_ai.domain.market_calculations import (
    MarketCalculationRequest,
    MarketCalculationSnapshot,
)
from stock_research_ai.domain.provenance import AcquiredPayload, RawArtifact, SourceRetrieval
from stock_research_ai.domain.publications import (
    PublicationHit,
    PublicationRequest,
    PublicationSnapshot,
)
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation, SectorKPIRequest


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


@dataclass(frozen=True)
class MarketCalculationServices:
    market: MarketRepository
    repository: MarketCalculationRepository
    artifacts: RawArtifactStore


@dataclass(frozen=True)
class SectorKPIServices:
    extractions: ExtractionRepository
    repository: CalculationRepository
    resolver: SemanticResolver
    artifacts: RawArtifactStore


@dataclass(frozen=True)
class CalculationServices:
    extractions: ExtractionRepository
    repository: CalculationRepository
    artifacts: RawArtifactStore


@dataclass(frozen=True)
class ContextServices:
    publications: PublicationAcquirer
    publication_parser: PublicationParser
    macro: MacroAcquirer
    macro_parser: MacroParser
    artifacts: RawArtifactStore
    repository: ContextRepository


class ResearchEngine:
    def __init__(
        self,
        repository: CompanyRepository,
        extraction: ExtractionServices | None = None,
        documents: DocumentServices | None = None,
        market: MarketServices | None = None,
        context: ContextServices | None = None,
        calculations: CalculationServices | None = None,
        sector_kpis: SectorKPIServices | None = None,
        market_calculations: MarketCalculationServices | None = None,
    ) -> None:
        self._repository = repository
        self._extraction = extraction
        self._documents = documents
        self._market = market
        self._context = context
        self._calculations = calculations
        self._sector_kpis = sector_kpis
        self._market_calculations = market_calculations

    async def calculate_market(
        self, request: MarketCalculationRequest
    ) -> MarketCalculationSnapshot:
        services = self._market_calculations
        if services is None:
            raise RuntimeError("Market calculation services not configured")
        source = await services.market.get_market(request.extraction_id)
        benchmark = (
            await services.market.get_market(request.benchmark_extraction_id)
            if request.benchmark_extraction_id
            else None
        )
        if source is None or (request.benchmark_extraction_id and benchmark is None):
            raise ValueError("Unknown market extraction")
        for snapshot in (source, benchmark):
            if snapshot:
                for artifact in snapshot.raw_artifacts:
                    await services.artifacts.read(artifact)
        result = calculate_market(source, request, benchmark)
        await services.repository.save_market_calculation(result)
        return result

    async def calculate_sector_kpis(self, request: SectorKPIRequest) -> SectorKPIEvaluation:
        services = self._sector_kpis
        if services is None:
            raise RuntimeError("Sector KPI services not configured")
        source = await services.extractions.get_extraction(request.extraction_id)
        if source is None:
            raise ValueError("Unknown extraction snapshot")
        for artifact in source.raw_artifacts:
            await services.artifacts.read(artifact)
        result = evaluate_kpis(source, request, services.resolver)
        await services.repository.save_calculation(result)
        return result

    async def calculate_financials(self, request: CalculationRequest) -> CalculationSnapshot:
        services = self._calculations
        if services is None:
            raise RuntimeError("Calculation services not configured")
        source = await services.extractions.get_extraction(request.extraction_id)
        if source is None:
            raise ValueError("Unknown extraction snapshot")
        # Verify retained original evidence, without acquiring or normalizing new evidence.
        for artifact in source.raw_artifacts:
            await services.artifacts.read(artifact)
        result = calculate(source, request)
        await services.repository.save_calculation(result)
        return result

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
        if request.company_id is None or await self._repository.get(request.company_id) is None:
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

    async def ingest_publication(self, request: PublicationRequest) -> PublicationSnapshot:
        services = self._context
        if services is None:
            raise RuntimeError("Context services not configured")
        payload = await services.publications.acquire(request)
        artifact = await services.artifacts.put(payload) if payload.content is not None else None
        await services.repository.record_evidence(payload.retrieval, artifact)
        content = await services.artifacts.read(artifact) if artifact else None
        result = services.publication_parser.normalize(
            request, payload.retrieval, artifact, content
        )
        await services.repository.save_publication(result)
        return result

    async def ingest_macro(self, request: MacroRequest) -> MacroSnapshot:
        services = self._context
        if services is None:
            raise RuntimeError("Context services not configured")
        evidence = []
        for payload in await services.macro.acquire(request):
            artifact = (
                await services.artifacts.put(payload) if payload.content is not None else None
            )
            await services.repository.record_evidence(payload.retrieval, artifact)
            content = await services.artifacts.read(artifact) if artifact else None
            evidence.append((payload.retrieval, artifact, content))
        result = services.macro_parser.normalize(request, tuple(evidence))
        await services.repository.save_macro(result)
        return result

    async def publication_history(self, publication_id: UUID) -> tuple[PublicationSnapshot, ...]:
        if self._context is None:
            raise RuntimeError("Context services not configured")
        return await self._context.repository.publication_history(publication_id)

    async def macro_history(self, series_id: UUID) -> tuple[MacroSnapshot, ...]:
        if self._context is None:
            raise RuntimeError("Context services not configured")
        return await self._context.repository.macro_history(series_id)

    async def search_publications(self, query: str, limit: int = 20) -> tuple[PublicationHit, ...]:
        if self._context is None:
            raise RuntimeError("Context services not configured")
        if not query.strip() or not 1 <= limit <= 100:
            raise ValueError("Invalid search")
        return await self._context.repository.search_publications(query, limit)

    async def lookup_macro(self, series_id: UUID, period: str) -> tuple[MacroObservation, ...]:
        return tuple(
            o
            for s in await self.macro_history(series_id)
            for o in s.observations
            if o.reference_period == period
        )
