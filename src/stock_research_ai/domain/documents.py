"""Discovered references only; document contents are not acquired."""

import hashlib
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.provenance import (
    ObservationProvenance,
    RawArtifact,
    RetrievalStatus,
    SourceLink,
    SourceRetrieval,
)


class DocumentType(StrEnum):
    ANNUAL_REPORT = "annual_report"
    INVESTOR_PRESENTATION = "investor_presentation"
    EARNINGS_PRESENTATION = "earnings_presentation"
    CONCALL_TRANSCRIPT = "concall_transcript"
    ANNOUNCEMENT = "announcement"
    CREDIT_RATING = "credit_rating"
    OTHER = "other"


class DocumentReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    title: str
    document_type: DocumentType
    link: SourceLink
    provenance: ObservationProvenance
    source_date_text: str | None = None
    publication_at: AwareDatetime | None = None


class DocumentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    company_id: UUID
    url: str
    document_type: DocumentType = DocumentType.OTHER
    title: str | None = None
    provider: str = Field(default="public_document", min_length=1)
    source_document_key: str | None = None
    publication_date: date | None = None
    fiscal_label: str | None = None
    reference: DocumentReference | None = None
    replay_path: Path | None = None
    final_url: str | None = None

    @model_validator(mode="after")
    def consistent_reference(self) -> Self:
        if self.final_url and not self.replay_path:
            raise ValueError("final_url is only valid for replay")
        if self.reference and self.url != self.reference.link.resolved_url:
            raise ValueError("Request URL must match the supplied document reference")
        return self


class DocumentIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    company_id: UUID
    provider: str
    source_document_key: str | None = None
    original_url: str


class DocumentExtractionIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    message: str
    page_number: int | None = Field(default=None, ge=1)


class DocumentPage(BaseModel):
    """Physical PDF pages are one-indexed; printed page labels are not inferred."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    version_id: UUID
    artifact_id: UUID
    number: int = Field(ge=1)
    text: str
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_version: str
    status: Literal["EXTRACTED", "LOW_TEXT", "EMPTY", "OCR_REQUIRED", "FAILED"]
    issues: tuple[DocumentExtractionIssue, ...] = ()

    @model_validator(mode="after")
    def verify_text(self) -> Self:
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("Page text hash mismatch")
        if self.status == "EXTRACTED" and not self.text.strip():
            raise ValueError("An extracted page must contain text")
        return self


class DocumentSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    page_id: UUID
    label: str
    heading: str | None = None
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    rule: str = "explicit_heading_v1"


class DocumentChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    version_id: UUID
    artifact_id: UUID
    page_id: UUID
    page_number: int = Field(ge=1)
    section_id: UUID
    ordinal: int = Field(ge=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    text: str
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    character_count: int = Field(ge=0)
    word_count: int = Field(ge=0)


class DocumentCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    overall: Literal["COMPLETE", "PARTIAL", "FAILED"]
    acquisition_status: RetrievalStatus
    parser_status: Literal["SUCCESS", "PARTIAL", "FAILED", "NOT_ATTEMPTED"]
    page_count: int = Field(ge=0)
    source_page_count: int | None = Field(default=None, ge=0)
    pages_extracted: int = Field(ge=0)
    pages_empty: int = Field(ge=0)
    pages_failed: int = Field(ge=0)
    pages_low_text: int = Field(ge=0)
    suspected_scanned_pages: tuple[int, ...] = ()
    characters_extracted: int = Field(ge=0)
    metadata_available: bool
    section_detection: Literal["COMPLETE", "PARTIAL", "UNKNOWN"]
    issues: tuple[DocumentExtractionIssue, ...] = ()


class ParsedDocument(BaseModel):
    """Derived evidence snapshot. The original artifact is always retained separately."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["3A.1"] = "3A.1"
    identity: DocumentIdentity
    version_id: UUID
    created_at: AwareDatetime
    title: str
    document_type: DocumentType
    classification_basis: Literal[
        "caller_declared", "source_reference", "explicit_heading", "unknown"
    ]
    publication_date: date | None = None
    fiscal_label: str | None = None
    source_reference: DocumentReference | None = None
    retrieval: SourceRetrieval
    artifact: RawArtifact | None
    parser_version: str
    metadata: tuple[tuple[str, str], ...] = ()
    pages: tuple[DocumentPage, ...] = ()
    sections: tuple[DocumentSection, ...] = ()
    chunks: tuple[DocumentChunk, ...] = ()
    coverage: DocumentCoverage

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.artifact and self.artifact.retrieval_id != self.retrieval.id:
            raise ValueError("Artifact must belong to the retrieval")
        if self.pages and self.artifact is None:
            raise ValueError("Pages require original evidence")
        page_map = {page.id: page for page in self.pages}
        section_map = {section.id: section for section in self.sections}
        if len(page_map) != len(self.pages) or [p.number for p in self.pages] != list(
            range(1, len(self.pages) + 1)
        ):
            raise ValueError("Page IDs and one-indexed numbering must be unambiguous")
        for current_page in self.pages:
            if (
                not self.artifact
                or current_page.artifact_id != self.artifact.id
                or current_page.version_id != self.version_id
            ):
                raise ValueError("Page provenance mismatch")
        for current_section in self.sections:
            page = page_map.get(current_section.page_id)
            if (
                page is None
                or not 0 <= current_section.start_char <= current_section.end_char <= len(page.text)
            ):
                raise ValueError("Section offsets must reference page text")
        for chunk in self.chunks:
            page = page_map.get(chunk.page_id)
            section = section_map.get(chunk.section_id)
            if page is None or section is None or section.page_id != page.id:
                raise ValueError("Chunk must reference its page and section")
            if (
                chunk.version_id != self.version_id
                or chunk.artifact_id != page.artifact_id
                or chunk.page_number != page.number
            ):
                raise ValueError("Chunk provenance mismatch")
            if not section.start_char <= chunk.start_char < chunk.end_char <= section.end_char:
                raise ValueError("Chunk offsets must stay within its section")
            if chunk.text != page.text[chunk.start_char : chunk.end_char]:
                raise ValueError("Chunk text must equal its exact page slice")
            if (
                chunk.character_count != len(chunk.text)
                or chunk.word_count != len(chunk.text.split())
                or chunk.text_sha256 != hashlib.sha256(chunk.text.encode()).hexdigest()
            ):
                raise ValueError("Chunk text metadata mismatch")
        if len({chunk.id for chunk in self.chunks}) != len(self.chunks):
            raise ValueError("Chunk IDs must be unique")
        if [chunk.ordinal for chunk in self.chunks] != list(range(1, len(self.chunks) + 1)):
            raise ValueError("Chunk ordering must be contiguous")
        for covered_page in self.pages:
            position = 0
            for chunk in (c for c in self.chunks if c.page_id == covered_page.id):
                if chunk.start_char != position:
                    raise ValueError("Chunks must cover page text without gaps or overlaps")
                position = chunk.end_char
            if position != len(covered_page.text):
                raise ValueError("Chunks must preserve all extracted page text")
        counts = (
            sum(p.status == "EXTRACTED" for p in self.pages),
            sum(p.status == "EMPTY" for p in self.pages),
            sum(p.status == "FAILED" for p in self.pages),
            sum(p.status == "LOW_TEXT" for p in self.pages),
        )
        if counts != (
            self.coverage.pages_extracted,
            self.coverage.pages_empty,
            self.coverage.pages_failed,
            self.coverage.pages_low_text,
        ):
            raise ValueError("Coverage page status counts mismatch")
        if self.coverage.suspected_scanned_pages != tuple(
            p.number for p in self.pages if p.status == "OCR_REQUIRED"
        ):
            raise ValueError("Coverage scanned-page inventory mismatch")
        if self.coverage.page_count != len(self.pages):
            raise ValueError("Coverage page count mismatch")
        if self.coverage.characters_extracted != sum(len(p.text) for p in self.pages):
            raise ValueError("Coverage character count mismatch")
        if self.coverage.overall == "COMPLETE" and (
            not self.pages
            or any(p.status != "EXTRACTED" for p in self.pages)
            or self.retrieval.status != RetrievalStatus.SUCCESS
        ):
            raise ValueError("Complete coverage requires acquired, nonempty extracted pages")
        return self


class DocumentSearchHit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    document_id: UUID
    version_id: UUID
    title: str
    source_url: str
    retrieval_id: UUID
    artifact_id: UUID
    artifact_sha256: str
    page_id: UUID
    page_number: int
    chunk_id: UUID
    section: str
    text: str
