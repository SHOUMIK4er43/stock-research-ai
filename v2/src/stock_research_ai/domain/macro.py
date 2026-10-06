"""Release-vintage-safe macro observations with explicit series semantics."""

from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.provenance import ObservationProvenance, RawArtifact, SourceRetrieval


class MacroRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    periods: tuple[str, ...] = Field(min_length=1)
    base_year: str
    metadata_file: Path | None = None
    data_file: Path | None = None
    evidence_kind: Literal["PROVIDER", "SYNTHETIC"] = "PROVIDER"

    @model_validator(mode="after")
    def valid(self) -> Self:
        from datetime import datetime

        for period in self.periods:
            if datetime.strptime(period, "%Y-%m").strftime("%Y-%m") != period:
                raise ValueError("Use YYYY-MM reference periods")
        if sorted(set(self.periods)) != list(self.periods):
            raise ValueError("Periods must be unique and ordered")
        if bool(self.metadata_file) != bool(self.data_file):
            raise ValueError("Replay requires metadata and data")
        return self


class MacroSeriesIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    id: UUID
    publisher: str
    dataset: str
    source_series_key: str
    geography: str
    population: str
    frequency: Literal["MONTHLY"]
    unit: Literal["INDEX_POINTS", "UNKNOWN"]
    scale: Decimal
    base_year: str
    seasonal_adjustment: Literal["UNKNOWN", "ADJUSTED", "UNADJUSTED"]
    provenance: ObservationProvenance
    mapping_provenance: ObservationProvenance


class MacroObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    series_id: UUID
    reference_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    value: Decimal
    source_status: str | None = None
    revision_status: Literal["UNKNOWN", "PROVISIONAL", "FINAL", "REVISED"] = "UNKNOWN"
    published_at: AwareDatetime | None = None
    provider_vintage_at: AwareDatetime | None = None
    retrieved_at: AwareDatetime
    provenance: ObservationProvenance


class MacroCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    overall: Literal["COMPLETE", "PARTIAL", "FAILED"]
    requested_periods: tuple[str, ...]
    returned_periods: tuple[str, ...]
    missing_periods: tuple[str, ...]
    duplicate_periods: tuple[str, ...] = ()
    malformed_records: int = Field(ge=0)
    unknown_metadata: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


class MacroSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["3C.1"] = "3C.1"
    id: UUID
    request: MacroRequest
    series: MacroSeriesIdentity | None
    observations: tuple[MacroObservation, ...]
    retrievals: tuple[SourceRetrieval, ...]
    artifacts: tuple[RawArtifact, ...]
    coverage: MacroCoverage
    normalizer_version: str

    @model_validator(mode="after")
    def valid(self) -> Self:
        artifacts = {a.id: a for a in self.artifacts}
        retrievals = {r.id: r for r in self.retrievals}
        if any(a.retrieval_id not in retrievals for a in artifacts.values()):
            raise ValueError("Missing retrieval")
        if self.series and any(
            p.raw_artifact_id not in artifacts
            for p in (self.series.provenance, self.series.mapping_provenance)
        ):
            raise ValueError("Missing series evidence")
        periods = tuple(o.reference_period for o in self.observations)
        if self.coverage.requested_periods != self.request.periods or not set(periods) <= set(
            self.request.periods
        ):
            raise ValueError("Requested-period coverage mismatch")
        if periods != tuple(sorted(set(periods))) or periods != self.coverage.returned_periods:
            raise ValueError("Period uniqueness/order/coverage mismatch")
        if self.coverage.missing_periods != tuple(sorted(set(self.request.periods) - set(periods))):
            raise ValueError("Missing-period coverage mismatch")
        for o in self.observations:
            if not self.series or o.series_id != self.series.id:
                raise ValueError("Wrong series")
            a = artifacts.get(o.provenance.raw_artifact_id)
            if (
                not a
                or a.retrieval_id is None
                or o.retrieved_at != retrievals[a.retrieval_id].retrieved_at
            ):
                raise ValueError("Observation evidence mismatch")
        if self.coverage.overall == "COMPLETE" and (
            not self.observations
            or self.coverage.missing_periods
            or self.coverage.issues
            or self.coverage.unknown_metadata
        ):
            raise ValueError("Incomplete macro coverage")
        return self
