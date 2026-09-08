"""Central application operations, independent of transport and persistence technology."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from stock_research_ai.application.ports import (
    CompanyRepository,
    ExtractionRepository,
    RawArtifactStore,
    SourceAcquirer,
    SourceNormalizer,
)
from stock_research_ai.domain.company import Company, CompanyRegistration
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest


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


class ResearchEngine:
    def __init__(
        self, repository: CompanyRepository, extraction: ExtractionServices | None = None
    ) -> None:
        self._repository = repository
        self._extraction = extraction

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
