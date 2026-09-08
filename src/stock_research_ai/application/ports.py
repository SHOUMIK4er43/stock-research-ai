"""Application-owned persistence interface; no storage implementation details."""

from typing import Protocol
from uuid import UUID

from stock_research_ai.domain.company import Company, SourceCompanyIdentity
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest
from stock_research_ai.domain.provenance import AcquiredPayload, RawArtifact, SourceRetrieval


class CompanyRepository(Protocol):
    async def save(self, company: Company) -> None:
        """Persist a company."""
        ...

    async def get(self, company_id: UUID) -> Company | None:
        """Return a company, or None when the ID is unknown."""
        ...


class SourceAcquirer(Protocol):
    async def acquire(self, request: ExtractionRequest) -> AcquiredPayload: ...


class RawArtifactStore(Protocol):
    async def put(self, payload: AcquiredPayload) -> RawArtifact: ...
    async def read(self, artifact: RawArtifact) -> bytes: ...


class SourceNormalizer(Protocol):
    def identify(
        self, content: bytes, artifact: RawArtifact, retrieval: SourceRetrieval, company_id: UUID
    ) -> SourceCompanyIdentity | None: ...
    def normalize(
        self,
        content: bytes | None,
        artifact: RawArtifact | None,
        retrieval: SourceRetrieval,
        company_id: UUID,
    ) -> CompanyExtractionResult: ...


class ExtractionRepository(Protocol):
    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None: ...
    async def bind_company(self, identity: SourceCompanyIdentity) -> UUID: ...
    async def save_extraction(self, result: CompanyExtractionResult) -> None: ...
    async def get_extraction(self, extraction_id: UUID) -> CompanyExtractionResult | None: ...
