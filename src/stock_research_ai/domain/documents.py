"""Discovered references only; document contents are not acquired."""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from stock_research_ai.domain.provenance import ObservationProvenance, SourceLink


class DocumentType(StrEnum):
    ANNUAL_REPORT = "annual_report"
    INVESTOR_PRESENTATION = "investor_presentation"
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
