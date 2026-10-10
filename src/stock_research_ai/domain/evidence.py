"""DETAILED_RESEARCH_V1 Box 1: catalogue of discovered public research documents.

Document-level provenance only: what exists, what kind it is, when and where it came
from, and how to retrieve it later. Nothing is inferred - an unknown date, period or
basis stays None. Authority is metadata for later reconciliation, never a filter.
"""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from stock_research_ai.domain.financial import ReportingScope

EVIDENCE_CATALOGUE_SCHEMA = "EVIDENCE_CATALOGUE_V1"

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Url = Annotated[str, StringConstraints(min_length=1, max_length=2048)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class EvidenceDocumentType(StrEnum):
    SCREENER_PAGE = "SCREENER_PAGE"
    SCREENER_EXPORT = "SCREENER_EXPORT"
    ANNUAL_REPORT = "ANNUAL_REPORT"
    QUARTERLY_RESULT = "QUARTERLY_RESULT"
    INVESTOR_PRESENTATION = "INVESTOR_PRESENTATION"
    CONCALL_TRANSCRIPT = "CONCALL_TRANSCRIPT"
    CONCALL_AUDIO = "CONCALL_AUDIO"
    AGM_NOTICE = "AGM_NOTICE"
    AGM_TRANSCRIPT = "AGM_TRANSCRIPT"
    AGM_MINUTES = "AGM_MINUTES"
    SHAREHOLDER_LETTER = "SHAREHOLDER_LETTER"
    CREDIT_RATING_REPORT = "CREDIT_RATING_REPORT"
    EXCHANGE_ANNOUNCEMENT = "EXCHANGE_ANNOUNCEMENT"
    REGULATION_30_FILING = "REGULATION_30_FILING"
    SHAREHOLDING_PATTERN = "SHAREHOLDING_PATTERN"
    INSIDER_TRADING_DISCLOSURE = "INSIDER_TRADING_DISCLOSURE"
    CORPORATE_GOVERNANCE_REPORT = "CORPORATE_GOVERNANCE_REPORT"
    BRSR = "BRSR"
    OTHER_COMPANY_DOCUMENT = "OTHER_COMPANY_DOCUMENT"


#: Types describing only the channel a document arrived through, not its content. When
#: one document is seen as both, the content type wins (see application merge rules).
CHANNEL_DOCUMENT_TYPES = frozenset(
    {
        EvidenceDocumentType.EXCHANGE_ANNOUNCEMENT,
        EvidenceDocumentType.REGULATION_30_FILING,
        EvidenceDocumentType.OTHER_COMPANY_DOCUMENT,
    }
)


class SourceAuthority(StrEnum):
    """Who stands behind the copy. Highest authority first."""

    PRIMARY_REGULATORY = "PRIMARY_REGULATORY"  # stock exchange / regulator filing
    PRIMARY_COMPANY = "PRIMARY_COMPANY"  # the company's own verified website
    SECONDARY_AGGREGATOR = "SECONDARY_AGGREGATOR"  # e.g. Screener
    THIRD_PARTY = "THIRD_PARTY"  # e.g. a credit rating agency
    UNVERIFIED = "UNVERIFIED"  # host not recognised; never assumed to be the company


AUTHORITY_RANK = {authority: rank for rank, authority in enumerate(SourceAuthority)}


class ClassificationConfidence(StrEnum):
    HIGH = "HIGH"  # explicit source section and label
    MEDIUM = "MEDIUM"  # explicit keyword in the document title
    LOW = "LOW"  # fallback


class DocumentRetrievalStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"  # retrievable later
    RETRIEVED = "RETRIEVED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INELIGIBLE = "INELIGIBLE"  # e.g. not HTTPS; will not be fetched as-is


class EvidenceCompany(BaseModel):
    """The verified company a catalogue belongs to. `company_key` is the stable,
    storage-independent key used in deterministic document identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    company_key: Annotated[
        str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*:[A-Za-z0-9_.:-]{1,64}$")
    ]
    company_id: UUID | None = None
    name: ShortText
    source_company_key: ShortText
    reporting_scope: ReportingScope
    website_url: Url | None = None


class DocumentDiscovery(BaseModel):
    """Where and how one sighting of a document was found."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_name: ShortText
    discovered_via_url: Url
    locator: Text
    source_section: Text | None = None
    source_label: Text | None = None
    classified_as: EvidenceDocumentType
    discovered_at: AwareDatetime
    raw_artifact_id: UUID | None = None


class RetrievalState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: DocumentRetrievalStatus = DocumentRetrievalStatus.NOT_ATTEMPTED
    status_code: Code | None = None
    retrieved_at: AwareDatetime | None = None
    content_hash: Sha256Hex | None = None
    file_size_bytes: NonNegativeInt | None = None
    mime_type: ShortText | None = None
    local_artifact_reference: Text | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        retrieved = self.status == DocumentRetrievalStatus.RETRIEVED
        if retrieved and (self.content_hash is None or self.retrieved_at is None):
            raise ValueError("A retrieved document requires its content hash and time")
        if not retrieved and (
            self.content_hash is not None
            or self.file_size_bytes is not None
            or self.local_artifact_reference is not None
        ):
            raise ValueError("Only a retrieved document may carry content metadata")
        return self


def _http_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        # urlsplit's own message quotes the raw netloc: never surface it (P2-6)
        raise ValueError("Document URLs must be absolute http(s) URLs") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Document URLs must be absolute http(s) URLs")
    if parts.username or parts.password:
        raise ValueError("Document URLs must not embed credentials")
    return value


class DocumentCandidate(BaseModel):
    """One source's sighting of a document, before catalogue identity and merging."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    document_type: EvidenceDocumentType
    confidence: ClassificationConfidence
    title: Text
    url: Url
    is_direct_document: bool
    published_date: date | None = None
    published_date_text: Text | None = None
    period_label: ShortText | None = None
    fiscal_year: int | None = Field(default=None, ge=1990, le=2100)
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    reporting_basis: ReportingScope | None = None
    language: ShortText | None = None
    retrieval: RetrievalState = Field(default_factory=RetrievalState)
    discovery: DocumentDiscovery
    metadata: dict[ShortText, Text] = Field(default_factory=dict, max_length=32)

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        return _http_url(value)

    @model_validator(mode="after")
    def quarter_needs_year(self) -> Self:
        if self.fiscal_quarter is not None and self.fiscal_year is None:
            raise ValueError("A fiscal quarter requires an explicit fiscal year")
        return self


class ResearchDocument(BaseModel):
    """One catalogued document with deterministic identity and full discovery lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    document_id: UUID
    identity_key: Text
    company_key: str
    company_id: UUID | None
    document_type: EvidenceDocumentType
    confidence: ClassificationConfidence
    title: Text
    source_url: Url
    download_url: Url | None
    alternate_urls: tuple[Url, ...] = ()
    source_domain: ShortText
    source_authority: SourceAuthority
    published_date: date | None
    published_date_text: Text | None
    period_label: ShortText | None
    fiscal_year: int | None = Field(ge=1990, le=2100)
    fiscal_quarter: int | None = Field(ge=1, le=4)
    reporting_basis: ReportingScope | None
    language: ShortText | None
    file_extension: Annotated[str, StringConstraints(pattern=r"^\.[a-z0-9]{1,8}$")] | None
    retrieval: RetrievalState
    discovered_from: tuple[DocumentDiscovery, ...] = Field(min_length=1)
    discovered_at: AwareDatetime
    duplicate_of: UUID | None = None
    metadata: dict[ShortText, Text] = Field(default_factory=dict, max_length=32)

    @field_validator("source_url", "download_url")
    @classmethod
    def http_url(cls, value: str | None) -> str | None:
        return None if value is None else _http_url(value)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.fiscal_quarter is not None and self.fiscal_year is None:
            raise ValueError("A fiscal quarter requires an explicit fiscal year")
        if self.duplicate_of == self.document_id:
            raise ValueError("A document cannot duplicate itself")
        if self.discovered_at != min(d.discovered_at for d in self.discovered_from):
            raise ValueError("discovered_at must be the earliest discovery")
        return self


class CatalogueIssue(BaseModel):
    """A machine-readable note about discovery; codes and locators only."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_name: ShortText
    code: Code
    locator: Text | None = None
    url: Url | None = None


class SourceDiscovery(BaseModel):
    """What one EvidenceSource returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_name: ShortText
    candidates: tuple[DocumentCandidate, ...] = ()
    issues: tuple[CatalogueIssue, ...] = ()


class SourceOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_name: ShortText
    status: Literal["SUCCESS", "FAILED"]
    failure_code: Code | None = None
    candidates_found: NonNegativeInt


class EvidenceCatalogue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["EVIDENCE_CATALOGUE_V1"] = "EVIDENCE_CATALOGUE_V1"
    company: EvidenceCompany
    catalogued_at: AwareDatetime
    documents: tuple[ResearchDocument, ...] = Field(default=(), max_length=10_000)
    sources: tuple[SourceOutcome, ...] = ()
    issues: tuple[CatalogueIssue, ...] = ()

    @model_validator(mode="after")
    def unique_documents(self) -> Self:
        ids = [d.document_id for d in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("Document IDs must be unique within a catalogue")
        if any(d.company_key != self.company.company_key for d in self.documents):
            raise ValueError("Every document must belong to the catalogue company")
        known = set(ids)
        if any(d.duplicate_of is not None and d.duplicate_of not in known for d in self.documents):
            raise ValueError("duplicate_of must reference a catalogued document")
        return self


class EvidenceSourceError(Exception):
    """A source failed as a whole; the catalogue records its code and continues."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
