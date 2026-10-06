"""Publications preserve source assertions; they do not infer events or impacts."""

from datetime import date
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.provenance import (
    ObservationProvenance,
    RawArtifact,
    SourceLink,
    SourceRetrieval,
)
from stock_research_ai.domain.subjects import EvidenceSubject


class PublicationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    url: str
    source_url: str
    source_key: str = Field(min_length=1)
    format: Literal["ISSUER_PDF", "RBI_HTML"]
    subjects: tuple[EvidenceSubject, ...] = ()
    replay_file: Path | None = None
    evidence_kind: Literal["PROVIDER", "SYNTHETIC"] = "PROVIDER"


class PublicationIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    publisher: str
    source_key: str
    source_url: str


class PublicationPassage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str = Field(min_length=1)
    provenance: ObservationProvenance


class PublicationCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    overall: Literal["COMPLETE", "PARTIAL", "FAILED"]
    source_reachable: bool
    metadata_present: bool
    body: Literal["AVAILABLE", "PARTIAL", "MISSING"]
    attachments: Literal["NOT_PRESENT", "LINKED_NOT_ACQUIRED"]
    publication_date_known: bool
    subject_linkage: Literal["ESTABLISHED", "ABSENT", "AMBIGUOUS"]
    source_locator_available: bool
    issues: tuple[str, ...] = ()


class PublicationSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["3C.1"] = "3C.1"
    id: UUID
    identity: PublicationIdentity
    request: PublicationRequest
    retrieved_at: AwareDatetime
    title: str | None
    publication_date: date | None
    published_at: AwareDatetime | None = None
    provider_updated_at: AwareDatetime | None = None
    reference_period: str | None = None
    passages: tuple[PublicationPassage, ...] = ()
    metadata_evidence: tuple[PublicationPassage, ...] = ()
    subjects: tuple[EvidenceSubject, ...] = ()
    attachments: tuple[SourceLink, ...] = ()
    retrieval: SourceRetrieval
    artifact: RawArtifact | None
    coverage: PublicationCoverage
    normalizer_version: str

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if self.retrieved_at != self.retrieval.retrieved_at:
            raise ValueError("Retrieval time mismatch")
        if self.artifact and self.artifact.retrieval_id != self.retrieval.id:
            raise ValueError("Artifact retrieval mismatch")
        proofs = [p.provenance for p in (*self.passages, *self.metadata_evidence)]
        proofs += [s.provenance for s in self.subjects if s.provenance]
        if any(not self.artifact or p.raw_artifact_id != self.artifact.id for p in proofs):
            raise ValueError("Publication provenance mismatch")
        if self.coverage.publication_date_known != (self.publication_date is not None):
            raise ValueError("Date coverage mismatch")
        if self.coverage.overall == "COMPLETE" and (
            not self.title
            or not self.publication_date
            or not self.passages
            or not self.subjects
            or self.coverage.issues
            or not self.artifact
            or not self.artifact.complete
            or self.retrieval.status != "success"
        ):
            raise ValueError("Incomplete publication cannot claim success")
        return self


class PublicationHit(BaseModel):
    snapshot_id: UUID
    publication_id: UUID
    title: str | None
    publisher: str
    source_url: str
    text: str
    provenance: ObservationProvenance
    retrieval_id: UUID
