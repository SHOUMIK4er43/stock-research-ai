"""Application-owned persistence interface; no storage implementation details."""

from typing import Protocol
from uuid import UUID

from stock_research_ai.domain.analysis import (
    AnalysisSnapshot,
    AnalystExecutionSpec,
    AnstInputBundle,
    ModelMetadata,
)
from stock_research_ai.domain.calculations import CalculationSnapshot
from stock_research_ai.domain.challenge import (
    ChallengerExecutionSpec,
    ChallengerInputBundle,
    ChallengeSnapshot,
)
from stock_research_ai.domain.company import Company, SourceCompanyIdentity
from stock_research_ai.domain.documents import (
    DocumentIdentity,
    DocumentRequest,
    DocumentSearchHit,
    ParsedDocument,
)
from stock_research_ai.domain.extraction import CompanyExtractionResult, ExtractionRequest
from stock_research_ai.domain.macro import MacroRequest, MacroSnapshot
from stock_research_ai.domain.market import MarketExtractionResult, MarketInstrument, MarketRequest
from stock_research_ai.domain.market_calculations import MarketCalculationSnapshot
from stock_research_ai.domain.patterns import PatternSnapshot
from stock_research_ai.domain.provenance import AcquiredPayload, RawArtifact, SourceRetrieval
from stock_research_ai.domain.publications import (
    PublicationHit,
    PublicationRequest,
    PublicationSnapshot,
)
from stock_research_ai.domain.sector_kpis import SectorKPIEvaluation, SemanticResolution
from stock_research_ai.domain.technicals import TechnicalSnapshot
from stock_research_ai.domain.valuation import ValuationSnapshot


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


class DocumentAcquirer(Protocol):
    async def acquire(self, request: DocumentRequest) -> AcquiredPayload: ...


class DocumentNormalizer(Protocol):
    def normalize(
        self,
        request: DocumentRequest,
        identity: DocumentIdentity,
        retrieval: SourceRetrieval,
        artifact: RawArtifact | None,
        content: bytes | None,
    ) -> ParsedDocument: ...


class DocumentRepository(Protocol):
    async def bind_document(self, request: DocumentRequest) -> DocumentIdentity: ...
    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None: ...
    async def save_document(self, document: ParsedDocument) -> None: ...
    async def get_document(self, version_id: UUID) -> ParsedDocument | None: ...
    async def document_history(self, document_id: UUID) -> tuple[ParsedDocument, ...]: ...
    async def search(
        self, company_id: UUID, query: str, limit: int
    ) -> tuple[DocumentSearchHit, ...]: ...


class MarketSource(Protocol):
    async def acquire_master(self, request: MarketRequest) -> AcquiredPayload: ...
    async def acquire_prices(
        self, request: MarketRequest, instrument: MarketInstrument
    ) -> tuple[AcquiredPayload, ...]: ...


class MarketNormalizer(Protocol):
    def identify(
        self, request: MarketRequest, content: bytes, artifact: RawArtifact
    ) -> MarketInstrument: ...
    def normalize(
        self,
        request: MarketRequest,
        instrument: MarketInstrument | None,
        evidence: tuple[tuple[SourceRetrieval, RawArtifact | None, bytes | None], ...],
        identity_error: str | None = None,
    ) -> MarketExtractionResult: ...


class MarketRepository(Protocol):
    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None: ...
    async def save_market(self, result: MarketExtractionResult) -> None: ...
    async def get_market(self, extraction_id: UUID) -> MarketExtractionResult | None: ...
    async def market_history(self, instrument_id: UUID) -> tuple[MarketExtractionResult, ...]: ...


class PublicationAcquirer(Protocol):
    async def acquire(self, request: PublicationRequest) -> AcquiredPayload: ...


class PublicationParser(Protocol):
    def normalize(
        self,
        request: PublicationRequest,
        retrieval: SourceRetrieval,
        artifact: RawArtifact | None,
        content: bytes | None,
    ) -> PublicationSnapshot: ...


class MacroAcquirer(Protocol):
    async def acquire(self, request: MacroRequest) -> tuple[AcquiredPayload, ...]: ...


class MacroParser(Protocol):
    def normalize(
        self,
        request: MacroRequest,
        evidence: tuple[tuple[SourceRetrieval, RawArtifact | None, bytes | None], ...],
    ) -> MacroSnapshot: ...


class ContextRepository(Protocol):
    async def record_evidence(
        self, retrieval: SourceRetrieval, artifact: RawArtifact | None
    ) -> None: ...
    async def save_publication(self, snapshot: PublicationSnapshot) -> None: ...
    async def save_macro(self, snapshot: MacroSnapshot) -> None: ...
    async def publication_history(self, identity_id: UUID) -> tuple[PublicationSnapshot, ...]: ...
    async def macro_history(self, series_id: UUID) -> tuple[MacroSnapshot, ...]: ...
    async def search_publications(self, query: str, limit: int) -> tuple[PublicationHit, ...]: ...
    async def get_publication(self, snapshot_id: UUID) -> PublicationSnapshot | None: ...
    async def get_macro(self, snapshot_id: UUID) -> MacroSnapshot | None: ...


class CalculationRepository(Protocol):
    async def get_calculation(
        self, snapshot_id: UUID
    ) -> CalculationSnapshot | SectorKPIEvaluation | None: ...

    async def save_calculation(
        self, snapshot: CalculationSnapshot | SectorKPIEvaluation
    ) -> None: ...
    async def calculation_history(
        self, extraction_id: UUID
    ) -> tuple[CalculationSnapshot | SectorKPIEvaluation, ...]: ...


class MarketCalculationRepository(Protocol):
    async def save_technical(self, snapshot: TechnicalSnapshot) -> None: ...
    async def technical_history(self, extraction_id: UUID) -> tuple[TechnicalSnapshot, ...]: ...

    async def save_market_calculation(self, snapshot: MarketCalculationSnapshot) -> None: ...
    async def market_calculation_history(
        self, extraction_id: UUID
    ) -> tuple[MarketCalculationSnapshot, ...]: ...


class SemanticResolver(Protocol):
    def resolve(
        self, source: CompanyExtractionResult, semantics: tuple[str, ...]
    ) -> tuple[SemanticResolution, ...]: ...


class PatternRepository(Protocol):
    async def save_pattern(self, snapshot: PatternSnapshot) -> None: ...
    async def pattern_history(self, extraction_id: UUID) -> tuple[PatternSnapshot, ...]: ...


class ValuationRepository(Protocol):
    async def save_valuation(self, snapshot: ValuationSnapshot) -> None: ...
    async def valuation_history(self, company_id: UUID) -> tuple[ValuationSnapshot, ...]: ...


class AnalystModel(Protocol):
    """Provider-neutral: turns a pinned bundle into a raw structured response.

    Never receives or returns a provider SDK object; secrets are read by the concrete
    adapter directly from its own environment, never through this protocol or its inputs.
    """

    async def generate(
        self, bundle: AnstInputBundle, execution: AnalystExecutionSpec
    ) -> ModelMetadata: ...


class AnalysisRepository(Protocol):
    async def save_analysis(self, snapshot: AnalysisSnapshot) -> None: ...
    async def analysis_history(self, company_id: UUID) -> tuple[AnalysisSnapshot, ...]: ...
    async def get_analysis(self, analysis_snapshot_id: UUID) -> AnalysisSnapshot | None: ...


class ChallengerModel(Protocol):
    """Provider-neutral: turns a pinned ChallengerInputBundle into a raw structured
    response. Distinct role/Protocol from AnalystModel, reusing the same provider-neutral
    value types (PromptTemplate/RenderedModelRequest/RawAnalystResponse/ModelMetadata)."""

    async def review(
        self, bundle: ChallengerInputBundle, execution: ChallengerExecutionSpec
    ) -> ModelMetadata: ...


class ChallengeRepository(Protocol):
    async def save_challenge(self, snapshot: ChallengeSnapshot) -> None: ...
    async def challenge_history(
        self, analysis_snapshot_id: UUID
    ) -> tuple[ChallengeSnapshot, ...]: ...
