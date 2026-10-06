"""DETAILED_RESEARCH_V1 Box 2: retrieving catalogued evidence as immutable artifacts.

Contracts only. A retrieval records exactly what happened to one catalogue document -
including every redirect hop and a typed outcome - and points at content-addressed bytes
only when those bytes were verified to be the kind of document expected. Nothing here
reads document contents.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from stock_research_ai.domain.evidence import EvidenceDocumentType

EVIDENCE_RETRIEVAL_MANIFEST_SCHEMA = "EVIDENCE_RETRIEVAL_MANIFEST_V1"

Url = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class RetrievalOutcome(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"  # with a code saying why (not selected, budget, audio, ...)
    RETRIEVED = "RETRIEVED"
    BLOCKED = "BLOCKED"  # 401/403/406/429/451 or an unsafe redirect destination
    NOT_FOUND = "NOT_FOUND"  # 404/410
    UNSUPPORTED = "UNSUPPORTED"  # URL the safety policy will not fetch
    TOO_LARGE = "TOO_LARGE"
    INVALID_CONTENT = "INVALID_CONTENT"  # e.g. an HTML login page where a PDF was expected
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"


class DetectedContentType(StrEnum):
    PDF = "PDF"
    HTML = "HTML"
    TEXT = "TEXT"
    SPREADSHEET = "SPREADSHEET"
    AUDIO_REFERENCE = "AUDIO_REFERENCE"  # audio is referenced, not downloaded
    UNKNOWN = "UNKNOWN"


class RedirectHop(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    url: Url
    http_status: int = Field(ge=100, le=599)


class FetchResult(BaseModel):
    """What a fetcher observed for one URL - transport facts only, no interpretation."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    outcome: Literal["OK", "BLOCKED", "NOT_FOUND", "UNSUPPORTED", "TOO_LARGE", "TIMEOUT", "FAILED"]
    code: Code | None = None
    final_url: Url | None = None
    redirects: tuple[RedirectHop, ...] = ()
    http_status: int | None = Field(default=None, ge=100, le=599)
    declared_content_type: ShortText | None = None
    content: bytes | None = None

    @model_validator(mode="after")
    def content_only_when_ok(self) -> Self:
        if (self.outcome == "OK") != (self.content is not None):
            raise ValueError("Content is present exactly when the fetch succeeded")
        return self


class StoredArtifact(BaseModel):
    """Reference to immutable, content-addressed bytes (the path is derived from the hash,
    so the same bytes always have the same reference)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    reference: Annotated[
        str, StringConstraints(pattern=r"^sha256/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z]{2,4}$")
    ]
    sha256: Sha256Hex
    size_bytes: NonNegativeInt


class RetrievedEvidenceArtifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    document_id: UUID
    document_type: EvidenceDocumentType
    retrieval_id: UUID
    selection_rank: PositiveInt | None  # None = not selected by the policy
    requested_url: Url
    final_url: Url | None = None
    redirects: tuple[RedirectHop, ...] = ()
    retrieved_at: AwareDatetime | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    declared_content_type: ShortText | None = None
    detected_content_type: DetectedContentType | None = None
    file_extension: Annotated[str, StringConstraints(pattern=r"^\.[a-z]{2,4}$")] | None = None
    size_bytes: NonNegativeInt | None = None
    sha256: Sha256Hex | None = None  # of the bytes received, even when they were rejected
    outcome: RetrievalOutcome
    code: Code | None = None
    artifact: StoredArtifact | None = None
    duplicate_of: UUID | None = None  # an earlier document with identical bytes

    @model_validator(mode="after")
    def consistent(self) -> Self:
        retrieved = self.outcome == RetrievalOutcome.RETRIEVED
        if retrieved and (
            self.artifact is None or self.sha256 is None or self.detected_content_type is None
        ):
            raise ValueError("A retrieved document needs verified, stored bytes")
        if not retrieved and self.artifact is not None:
            raise ValueError("Only verified documents are stored")
        if self.artifact is not None and self.artifact.sha256 != self.sha256:
            raise ValueError("Stored artifact hash must match the retrieved bytes")
        if self.duplicate_of == self.document_id:
            raise ValueError("A document cannot duplicate itself")
        return self


#: Order of retrieval only - never a judgement about a document's investment value.
DEFAULT_TIERS: dict[EvidenceDocumentType, int] = {
    EvidenceDocumentType.ANNUAL_REPORT: 1,
    EvidenceDocumentType.QUARTERLY_RESULT: 1,
    EvidenceDocumentType.INVESTOR_PRESENTATION: 1,
    EvidenceDocumentType.CONCALL_TRANSCRIPT: 1,
    EvidenceDocumentType.CREDIT_RATING_REPORT: 1,
    EvidenceDocumentType.AGM_NOTICE: 2,
    EvidenceDocumentType.AGM_TRANSCRIPT: 2,
    EvidenceDocumentType.AGM_MINUTES: 2,
    EvidenceDocumentType.SHAREHOLDER_LETTER: 2,
    EvidenceDocumentType.CORPORATE_GOVERNANCE_REPORT: 2,
    EvidenceDocumentType.BRSR: 2,
    EvidenceDocumentType.SHAREHOLDING_PATTERN: 2,
    EvidenceDocumentType.INSIDER_TRADING_DISCLOSURE: 2,
    EvidenceDocumentType.REGULATION_30_FILING: 2,
    EvidenceDocumentType.EXCHANGE_ANNOUNCEMENT: 3,
    EvidenceDocumentType.OTHER_COMPANY_DOCUMENT: 3,
    EvidenceDocumentType.CONCALL_AUDIO: 3,
}


class RetrievalPolicy(BaseModel):
    """Budgets and order only. Defaults are conservative; no retries unless configured.
    Concall audio is always reference-only in Box 2 (no download)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tiers: dict[EvidenceDocumentType, int] = Field(default_factory=lambda: dict(DEFAULT_TIERS))
    max_tier: int = Field(default=3, ge=1, le=9)
    per_type_limit: dict[EvidenceDocumentType, NonNegativeInt] = Field(default_factory=dict)
    max_documents: PositiveInt = 200
    max_file_bytes: PositiveInt = 50_000_000
    max_total_bytes: PositiveInt = 1_000_000_000
    concurrency: int = Field(default=2, ge=1, le=8)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_redirects: int = Field(default=5, ge=0, le=10)
    max_attempts: int = Field(default=1, ge=1, le=3)  # retries only for TIMEOUT/FAILED


class TypeOutcomeCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    document_type: EvidenceDocumentType
    outcome: RetrievalOutcome
    count: PositiveInt


class EvidenceRetrievalManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["EVIDENCE_RETRIEVAL_MANIFEST_V1"] = "EVIDENCE_RETRIEVAL_MANIFEST_V1"
    run_id: UUID
    company_key: str
    catalogued_at: AwareDatetime
    started_at: AwareDatetime
    completed_at: AwareDatetime
    policy: RetrievalPolicy
    documents_discovered: NonNegativeInt
    documents_selected: NonNegativeInt
    documents_retrieved: NonNegativeInt
    documents_failed: NonNegativeInt
    documents_blocked: NonNegativeInt
    total_bytes_retrieved: NonNegativeInt
    unique_content_hashes: NonNegativeInt
    duplicate_content_documents: NonNegativeInt
    outcomes_by_type: tuple[TypeOutcomeCount, ...]
    artifacts: tuple[RetrievedEvidenceArtifact, ...]

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("A run cannot complete before it starts")
        if len({a.document_id for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("One retrieval record per document")
        return self
