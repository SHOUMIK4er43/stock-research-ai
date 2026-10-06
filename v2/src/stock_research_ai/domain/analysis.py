"""Interpretation-only analyst contracts over pinned Parts 1-5A evidence.

No new source facts are minted here. Every AnalystClaim the model can author is
epistemic_class=INTERPRETATION, structurally; FACT/CALCULATION/SCENARIO describe only what
was cited, never the claim itself. Coverage, support and temporal fields are computed by the
validator/engine, never supplied by the model.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.calculations import CalculationModel
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.sector_kpis import ProfileClassification

EvidenceKind = Literal[
    "COMPANY",
    "FINANCIAL",
    "SHAREHOLDING",
    "MARKET",
    "FINANCIAL_CALCULATION",
    "SECTOR_KPI",
    "PATTERN",
    "MARKET_CALCULATION",
    "TECHNICAL",
    "VALUATION",
    "DOCUMENT_CHUNK",
    "PUBLICATION",
    "MACRO",
]

SourceEpistemicClass = Literal["FACT", "CALCULATION", "SCENARIO"]

AnalysisSection = Literal[
    "COMPANY_OVERVIEW",
    "BUSINESS_MODEL",
    "HISTORICAL_FUNDAMENTALS",
    "QUARTERLY_INFLECTIONS",
    "BALANCE_SHEET",
    "CASH_FLOW_QUALITY",
    "SECTOR_KPIS",
    "FORENSIC_REVIEW",
    "MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
    "OWNERSHIP_AND_SHAREHOLDING",
    "INDUSTRY_AND_MACRO",
    "MARKET_PERFORMANCE",
    "TECHNICAL_STATE",
    "VALUATION",
    "EXPECTATIONS",
    "CATALYSTS",
    "RISKS",
    "VARIANT_PERCEPTION",
    "BULL_BASE_BEAR_INTERPRETATION",
]

SectionCoverageStatus = Literal[
    "AVAILABLE",
    "PARTIAL",
    "INSUFFICIENT_INPUT",
    "NOT_APPLICABLE",
    "UNAVAILABLE",
    "UNVERIFIED_METRIC_SEMANTICS",
    "INCOMPATIBLE_SCOPE",
    "INCOMPATIBLE_PERIOD",
]

AS_OF_POLICY: Literal["ANALYSIS_CURRENT_PINNED_BUNDLE_V1"] = "ANALYSIS_CURRENT_PINNED_BUNDLE_V1"
POINT_IN_TIME_ISSUE_CODE = "POINT_IN_TIME_AVAILABILITY_UNVERIFIED"


class EvidenceReference(CalculationModel):
    kind: EvidenceKind
    owning_snapshot_id: UUID
    item_key: str | None = None
    snapshot_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")


class SupportAssessment(CalculationModel):
    basis: Literal[
        "SINGLE_SOURCE", "CORROBORATED_MULTI_SOURCE", "CONFLICTING_EVIDENCE", "INSUFFICIENT_SUPPORT"
    ]
    distinct_source_kinds: int = Field(ge=0)
    cites_unverified_semantics: bool
    cites_limitation_issue: bool


class SectionCoverage(CalculationModel):
    section: AnalysisSection
    status: SectionCoverageStatus
    reason: str = Field(min_length=1)
    contributing_evidence_kinds: tuple[EvidenceKind, ...] = ()
    upstream_statuses: tuple[str, ...] = ()

    @model_validator(mode="after")
    def sorted_unique(self) -> Self:
        if tuple(sorted(set(self.upstream_statuses))) != self.upstream_statuses:
            raise ValueError("upstream_statuses must be deduplicated and sorted")
        if len(self.upstream_statuses) > 1 and (
            self.status != "UNAVAILABLE" or self.reason != "HETEROGENEOUS_NON_AVAILABLE_INPUTS"
        ):
            raise ValueError(
                "Multiple distinct non-available upstream statuses must resolve to "
                "UNAVAILABLE with reason HETEROGENEOUS_NON_AVAILABLE_INPUTS"
            )
        return self


class TimeScope(CalculationModel):
    observation_period: str | None = None
    reporting_scope: ReportingScope | None = None
    evaluated_at: AwareDatetime
    as_of_policy: Literal["ANALYSIS_CURRENT_PINNED_BUNDLE_V1"] = AS_OF_POLICY
    point_in_time_unverified: bool


class CitationCandidate(CalculationModel):
    """What AnalystModel may claim about a citation; never trusted, always re-resolved."""

    kind: EvidenceKind
    owning_snapshot_id: UUID
    item_key: str | None = None


class ManagementQuoteCandidate(CalculationModel):
    chunk: CitationCandidate
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def chunk_kind(self) -> Self:
        if self.chunk.kind != "DOCUMENT_CHUNK":
            raise ValueError("Management quote must cite a document chunk")
        return self


class AnalystClaimCandidate(CalculationModel):
    """The ONLY shape AnalystModel may emit. No epistemic/coverage/support/temporal fields."""

    section: AnalysisSection
    statement: str = Field(min_length=1)
    materiality: Literal["PRIMARY", "SUPPORTING", "CONTEXTUAL"]
    citations: tuple[CitationCandidate, ...] = Field(min_length=1)
    management_quote: ManagementQuoteCandidate | None = None
    dependency_assumption_ids: tuple[UUID, ...] = ()


class ManagementStatement(CalculationModel):
    statement_id: UUID
    chunk: EvidenceReference
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def chunk_kind(self) -> Self:
        if self.chunk.kind != "DOCUMENT_CHUNK":
            raise ValueError("Management statement must cite a document chunk")
        return self


class AnalystClaim(CalculationModel):
    claim_id: UUID
    section: AnalysisSection
    epistemic_class: Literal["INTERPRETATION"] = "INTERPRETATION"
    statement: str = Field(min_length=1)
    materiality: Literal["PRIMARY", "SUPPORTING", "CONTEXTUAL"]
    citations: tuple[EvidenceReference, ...] = Field(min_length=1)
    source_epistemic_classes: tuple[SourceEpistemicClass, ...]
    support: SupportAssessment
    time_scope: TimeScope
    sector_profile: str | None = None
    limitations: tuple[str, ...] = ()
    management_statement: ManagementStatement | None = None
    dependency_assumption_ids: tuple[UUID, ...] = ()

    @model_validator(mode="after")
    def shape(self) -> Self:
        if len(self.source_epistemic_classes) != len(self.citations):
            raise ValueError("One source epistemic class per citation is required")
        if tuple(sorted(set(self.limitations))) != self.limitations:
            raise ValueError("Limitations must be deduplicated and sorted")
        return self


class ValidationIssue(CalculationModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class RejectedClaim(CalculationModel):
    raw_candidate: dict[str, object]
    issues: tuple[ValidationIssue, ...] = Field(min_length=1)


class AnalysisRequest(CalculationModel):
    """Evidence and analysis intent only. No provider configuration of any kind."""

    company_id: UUID
    classification: ProfileClassification
    profile_id: str
    financial_extraction_id: UUID | None = None
    calculation_snapshot_ids: tuple[UUID, ...] = ()
    sector_kpi_snapshot_ids: tuple[UUID, ...] = ()
    pattern_snapshot_ids: tuple[UUID, ...] = ()
    market_extraction_ids: tuple[UUID, ...] = ()
    market_calculation_snapshot_ids: tuple[UUID, ...] = ()
    technical_snapshot_ids: tuple[UUID, ...] = ()
    valuation_snapshot_ids: tuple[UUID, ...] = ()
    document_chunk_refs: tuple[tuple[UUID, UUID], ...] = ()  # (document_version_id, chunk_id)
    publication_snapshot_ids: tuple[UUID, ...] = ()
    # (macro_snapshot_id, reference_period)
    macro_snapshot_lookups: tuple[tuple[UUID, str], ...] = ()
    sections: tuple[AnalysisSection, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("Duplicate requested section")
        return self


class AnalystExecutionSpec(CalculationModel):
    """Provider-neutral: what to run. No API keys, no vendor SDK objects, ever."""

    template_id: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    generation_parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class PromptTemplate(CalculationModel):
    template_id: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    content: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RenderedModelRequest(CalculationModel):
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    template: PromptTemplate
    generation_parameters: dict[str, str | int | float | bool]
    generation_parameters_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rendered_text: str = Field(min_length=1)
    rendered_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RawAnalystResponse(CalculationModel):
    structured_payload: dict[str, object]
    structured_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ModelMetadata(CalculationModel):
    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str | None = None
    request: RenderedModelRequest
    raw_response: RawAnalystResponse
    generated_at: AwareDatetime


class AnstInputBundle(CalculationModel):
    """Assembled only by ResearchEngine. Sorted deterministically before hashing."""

    schema_version: Literal["5B.1"] = "5B.1"
    bundle_id: UUID
    request: AnalysisRequest
    assembled_at: AwareDatetime
    sources: tuple[EvidenceReference, ...]
    section_coverage: tuple[SectionCoverage, ...]

    @model_validator(mode="after")
    def canonical_order(self) -> Self:
        def key(r: EvidenceReference) -> tuple[str, str, str]:
            return (r.kind, str(r.owning_snapshot_id), r.item_key or "")

        if tuple(sorted(self.sources, key=key)) != self.sources:
            raise ValueError("Bundle sources must be in canonical sort order before hashing")
        if tuple(sorted(self.section_coverage, key=lambda c: c.section)) != self.section_coverage:
            raise ValueError("Section coverage must be sorted by section")
        if len({(s.kind, s.owning_snapshot_id, s.item_key) for s in self.sources}) != len(
            self.sources
        ):
            raise ValueError("Duplicate evidence reference in bundle")
        return self


class AnalysisSnapshot(CalculationModel):
    schema_version: Literal["5B.1"] = "5B.1"
    id: UUID
    company_id: UUID
    request: AnalysisRequest
    execution: AnalystExecutionSpec
    input_bundle: AnstInputBundle
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sources: tuple[EvidenceReference, ...]
    model_metadata: ModelMetadata
    section_coverage: tuple[SectionCoverage, ...]
    claims: tuple[AnalystClaim, ...] = ()
    management_statements: tuple[ManagementStatement, ...] = ()
    rejected: tuple[RejectedClaim, ...] = ()
    validation_issues: tuple[ValidationIssue, ...] = ()
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if self.input_bundle.request != self.request:
            raise ValueError("Snapshot request must match the bundle's own pinned request")
        if self.input_bundle.bundle_id is None:
            raise ValueError("Bundle identity missing")
        if self.sources != self.input_bundle.sources:
            raise ValueError("Snapshot sources must match the exact persisted bundle sources")
        if self.section_coverage != self.input_bundle.section_coverage:
            raise ValueError("Snapshot coverage must match the exact persisted bundle coverage")
        if self.company_id != self.request.company_id:
            raise ValueError("Snapshot company mismatch")
        refs = {(r.kind, r.owning_snapshot_id, r.item_key) for r in self.sources}
        for claim in self.claims:
            for citation in claim.citations:
                key = (citation.kind, citation.owning_snapshot_id, citation.item_key)
                if key not in refs:
                    raise ValueError("Claim citation outside pinned bundle sources")
            if claim.management_statement:
                key = (
                    claim.management_statement.chunk.kind,
                    claim.management_statement.chunk.owning_snapshot_id,
                    claim.management_statement.chunk.item_key,
                )
                if key not in refs:
                    raise ValueError("Management statement citation outside pinned bundle sources")
        if len({c.claim_id for c in self.claims}) != len(self.claims):
            raise ValueError("Duplicate claim identity")
        return self


__all__ = [
    "AS_OF_POLICY",
    "POINT_IN_TIME_ISSUE_CODE",
    "AnalysisRequest",
    "AnalysisSection",
    "AnalysisSnapshot",
    "AnalystClaim",
    "AnalystClaimCandidate",
    "AnalystExecutionSpec",
    "AnstInputBundle",
    "CitationCandidate",
    "EvidenceKind",
    "EvidenceReference",
    "ManagementQuoteCandidate",
    "ManagementStatement",
    "ModelMetadata",
    "PromptTemplate",
    "RawAnalystResponse",
    "RejectedClaim",
    "RenderedModelRequest",
    "SectionCoverage",
    "SectionCoverageStatus",
    "SourceEpistemicClass",
    "SupportAssessment",
    "TimeScope",
    "ValidationIssue",
]
