"""Immutable evidence and lineage contracts; Part 1 does not ingest artifacts."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, AwareDatetime, BaseModel, ConfigDict, Field


class SourceLocation(BaseModel):
    """A precise locator, such as a table/row/column, JSON path, or page."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    locator: str = Field(min_length=1)
    section: str | None = Field(default=None, min_length=1)


class RawArtifact(BaseModel):
    """Metadata identifying an exact preserved source payload and retrieval."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    layer: Literal["RAW"] = "RAW"
    id: UUID
    source_url: AnyHttpUrl
    source_type: str = Field(min_length=1)
    retrieved_at: AwareDatetime
    content_reference: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    collector_version: str = Field(min_length=1)
    retrieval_id: UUID | None = None
    size_bytes: int = Field(default=0, ge=0)
    content_type: str | None = None
    encoding: str = "utf-8"
    complete: bool = True
    representation: Literal["decoded_http_body", "replay_bytes"] = "decoded_http_body"


class ObservationProvenance(BaseModel):
    """Link a normalized value to its raw artifact and extraction location."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    raw_artifact_id: UUID
    source_location: SourceLocation
    normalizer_version: str = Field(min_length=1)


class CalculationProvenance(BaseModel):
    """Exact input record IDs and descriptive formula metadata, never executable code."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    input_ids: tuple[UUID, ...] = Field(min_length=1)
    formula: str = Field(min_length=1)
    calculation_version: str = Field(min_length=1)
    calculated_at: AwareDatetime


class RetrievalStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"


class SourceRetrieval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    provider: str = Field(min_length=1)
    requested_url: str
    final_url: str
    retrieved_at: AwareDatetime
    status: RetrievalStatus
    http_status: int | None = None
    content_type: str | None = None
    encoding: str = "utf-8"
    complete: bool = True
    error: str | None = None
    mode: Literal["http", "replay"] = "http"
    collector_version: str = "1"


class AcquiredPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    retrieval: SourceRetrieval
    content: bytes | None
    source_type: str = "company_html"
    file_extension: Literal["html", "pdf", "bin"] = "html"


class SourceLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    original_href: str
    resolved_url: str
    source_location: SourceLocation
