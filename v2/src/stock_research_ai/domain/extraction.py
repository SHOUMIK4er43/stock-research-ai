"""Validated, versioned evidence snapshots and explicit section coverage."""

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.company import SourceCompanyIdentity
from stock_research_ai.domain.documents import DocumentReference
from stock_research_ai.domain.financial import FinancialObservation
from stock_research_ai.domain.metrics import MetricDefinition
from stock_research_ai.domain.peers import PeerSnapshot
from stock_research_ai.domain.provenance import RawArtifact, SourceLocation, SourceRetrieval
from stock_research_ai.domain.shareholding import ShareholdingSnapshot


class Section(StrEnum):
    COMPANY_IDENTITY = "COMPANY_IDENTITY"
    TOP_METRICS = "TOP_METRICS"
    QUARTERLY = "QUARTERLY"
    PROFIT_LOSS = "PROFIT_LOSS"
    BALANCE_SHEET = "BALANCE_SHEET"
    CASH_FLOW = "CASH_FLOW"
    RATIOS = "RATIOS"
    SHAREHOLDING = "SHAREHOLDING"
    PEERS = "PEERS"
    DOCUMENTS = "DOCUMENTS"


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str
    url: str
    replay_path: Path | None = None
    final_url: str | None = None

    @model_validator(mode="after")
    def replay_metadata_only(self) -> Self:
        if self.final_url is not None and self.replay_path is None:
            raise ValueError("final_url is only valid for replay; live acquisition discovers it")
        return self


class ExtractionMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    provider: str
    created_at: AwareDatetime
    normalizer_version: str = "1"
    mapping_version: str = "1"


class CoverageIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    severity: Literal["warning", "error"]
    section: Section
    message: str
    original_label: str | None = None
    original_value: str | None = None
    raw_artifact_id: UUID | None = None
    retrieval_id: UUID | None = None
    source_location: SourceLocation | None = None


class SectionCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    section: Section
    presence: Literal["FOUND", "MISSING", "UNAVAILABLE"]
    parse_status: Literal["SUCCESS", "PARTIAL", "FAILED", "NOT_ATTEMPTED"]
    tables: int = Field(default=0, ge=0)
    rows: int = Field(default=0, ge=0)
    cells: int = Field(default=0, ge=0)
    records: int = Field(default=0, ge=0)
    issues: tuple[CoverageIssue, ...] = ()

    @model_validator(mode="after")
    def nonempty_success(self) -> Self:
        if self.parse_status == "SUCCESS" and (self.presence != "FOUND" or self.records == 0):
            raise ValueError("Successful coverage requires found, nonempty records")
        return self


class CoverageReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    overall: Literal["COMPLETE", "PARTIAL", "FAILED"]
    sections: tuple[SectionCoverage, ...]

    @model_validator(mode="after")
    def complete_inventory(self) -> Self:
        if len(self.sections) != len(Section) or {s.section for s in self.sections} != set(Section):
            raise ValueError("Coverage must contain every expected section exactly once")
        if self.overall == "COMPLETE" and any(s.parse_status != "SUCCESS" for s in self.sections):
            raise ValueError("Complete extraction requires all sections to succeed")
        return self


class CompanyExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["2.0"] = "2.0"
    metadata: ExtractionMetadata
    company: SourceCompanyIdentity | None
    metric_definitions: tuple[MetricDefinition, ...] = ()
    top_metrics: tuple[FinancialObservation, ...] = ()
    quarterly: tuple[FinancialObservation, ...] = ()
    profit_loss: tuple[FinancialObservation, ...] = ()
    balance_sheet: tuple[FinancialObservation, ...] = ()
    cashflow: tuple[FinancialObservation, ...] = ()
    ratios: tuple[FinancialObservation, ...] = ()
    shareholding: ShareholdingSnapshot = Field(default_factory=ShareholdingSnapshot)
    peers: PeerSnapshot = Field(default_factory=PeerSnapshot)
    documents: tuple[DocumentReference, ...] = ()
    retrievals: tuple[SourceRetrieval, ...]
    raw_artifacts: tuple[RawArtifact, ...]
    coverage: CoverageReport

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        artifacts = {a.id for a in self.raw_artifacts}
        retrievals = {r.id for r in self.retrievals}
        if any(a.retrieval_id not in retrievals for a in self.raw_artifacts):
            raise ValueError("Raw artifacts must reference snapshot retrievals")
        observations = (
            *self.top_metrics,
            *self.quarterly,
            *self.profit_loss,
            *self.balance_sheet,
            *self.cashflow,
            *self.ratios,
            *(h.observation for h in self.shareholding.holdings),
            *self.shareholding.shareholder_counts,
        )
        if self.company is None:
            if (
                self.coverage.overall != "FAILED"
                or observations
                or self.documents
                or self.peers.entries
            ):
                raise ValueError("Usable normalized records require company identity")
        elif any(r.company_id != self.company.company_id for r in observations):
            raise ValueError("Observations must belong to the snapshot company")
        provenance = [r.provenance for r in observations]
        provenance.extend(d.provenance for d in self.documents)
        provenance.extend(m.provenance for p in self.peers.entries for m in p.metrics)
        if self.company is not None:
            provenance.append(self.company.provenance)
            provenance.extend(
                listing.provenance
                for listing in self.company.listings
                if listing.provenance is not None
            )
            if any(
                listing.company_id != self.company.company_id for listing in self.company.listings
            ):
                raise ValueError("Listings must belong to the snapshot company")
        if any(p.raw_artifact_id not in artifacts for p in provenance):
            raise ValueError("Every normalized record must reference snapshot evidence")
        return self
