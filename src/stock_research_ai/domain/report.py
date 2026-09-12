"""Part 6A final-report contracts: pure consumption of frozen Parts 1-5C.

No new evidence kind, no new coverage status, no new arithmetic, no recommendation
vocabulary. Persistence follows Option B (see docs/part6a-proof.md): a
ResearchReportSnapshot pins exact upstream IDs and payload hashes rather than
re-embedding the full nested ChallengeSnapshot/AnalysisSnapshot chain.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.analysis import (
    EvidenceKind,
    EvidenceReference,
    ModelMetadata,
    SectionCoverageStatus,
    ValidationIssue,
)
from stock_research_ai.domain.calculations import CalculationModel
from stock_research_ai.domain.challenge import ClaimReviewOutcome
from stock_research_ai.domain.documents import DocumentType

ReportSectionId = Literal[
    "REPORT_METADATA",
    "EXECUTIVE_RESEARCH_SNAPSHOT",
    "COMPANY_IDENTITY_AND_EVIDENCE_FILE",
    "DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE",
    "BUSINESS_MODEL",
    "INDUSTRY_AND_COMPETITIVE_STRUCTURE",
    "ANNUAL_FINANCIAL_PERFORMANCE",
    "QUARTERLY_PERFORMANCE_AND_INFLECTION",
    "BALANCE_SHEET_ANALYSIS",
    "CASH_FLOW_AND_EARNINGS_QUALITY",
    "SECTOR_SPECIFIC_KPI_ANALYSIS",
    "DEEP_MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
    "CONCALL_INTELLIGENCE",
    "MANAGEMENT_GUIDANCE_AND_COMMITMENTS",
    "MANAGEMENT_GUIDANCE_VS_ACTUAL",
    "MANAGEMENT_NARRATIVE_CHANGE_DETECTION",
    "AGM_INTELLIGENCE",
    "ANNUAL_REPORT_INTELLIGENCE",
    "INVESTOR_PRESENTATION_INTELLIGENCE",
    "MANAGEMENT_CREDIBILITY_CONSISTENCY",
    "OWNERSHIP_AND_SHAREHOLDING",
    "GOVERNANCE_AND_CAPITAL_ALLOCATION",
    "FORENSIC_AND_QUALITY_REVIEW",
    "MARKET_AND_PRICE_ANALYSIS",
    "TECHNICAL_ANALYSIS",
    "VALUATION",
    "MARKET_IMPLIED_EXPECTATIONS",
    "MACRO_POLICY_REGULATION",
    "CATALYSTS",
    "RISK_REGISTER",
    "BULL_BASE_BEAR_SCENARIOS",
    "VARIANT_PERCEPTION",
    "INDEPENDENT_CHALLENGER_REVIEW",
    "RESEARCH_SYNTHESIS",
    "MONITORING_DASHBOARD",
    "DATA_GAPS_AND_LIMITATIONS",
    "EVIDENCE_APPENDIX",
    "CALCULATION_APPENDIX",
    "MANAGEMENT_STATEMENT_APPENDIX",
    "GUIDANCE_VS_ACTUAL_APPENDIX",
    "CHALLENGER_APPENDIX",
    "REPRODUCIBILITY_APPENDIX",
]

ReportCoverageReasonCode = Literal[
    "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED",
    "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED",
    "NO_DOCUMENTS_OF_TYPE_PINNED",
    "NARRATOR_NOT_CONFIGURED",
]

ReportQuoteAttribution = Literal["MANAGEMENT_STATEMENT", "TRANSCRIPT_STATEMENT"]
EpistemicClass = Literal["FACT", "CALCULATION", "INTERPRETATION", "ASSUMPTION", "SCENARIO"]
ResearchCompleteness = Literal["HIGH", "MODERATE", "LIMITED"]
ValuationEvidenceState = Literal["OBSERVED", "SCENARIO_DEPENDENT", "NOT_DETERMINABLE"]


class ReportRequest(CalculationModel):
    """The sole entry point. No AnalysisSnapshot-only fallback exists anywhere."""

    challenge_snapshot_id: UUID


class ReportExecutionSpec(CalculationModel):
    template_id: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    generation_parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ReportSectionCoverage(CalculationModel):
    status: SectionCoverageStatus
    reason: str = Field(min_length=1)
    reason_code: ReportCoverageReasonCode | None = None
    contributing_evidence_kinds: tuple[EvidenceKind, ...] = ()


class ReportMetric(CalculationModel):
    """A single already-canonical value re-displayed verbatim; never recomputed."""

    label: str = Field(min_length=1)
    display_value: str | None = None
    unit: str | None = None
    period: str | None = None
    reporting_scope: str | None = None
    status: str = Field(min_length=1)
    evidence_ref: EvidenceReference | None = None
    issues: tuple[str, ...] = ()


class ReportTable(CalculationModel):
    table_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    section: ReportSectionId
    columns: tuple[str, ...] = Field(min_length=1)
    rows: tuple[tuple[ReportMetric, ...], ...] = ()


class ReportStatement(CalculationModel):
    statement_id: UUID
    section: ReportSectionId
    text: str = Field(min_length=1)
    epistemic_class: EpistemicClass
    origin: Literal["DETERMINISTIC", "NARRATOR"]
    source_claim_ids: tuple[UUID, ...] = ()
    challenge_finding_ids: tuple[UUID, ...] = ()
    alternative_interpretation_ids: tuple[UUID, ...] = ()
    evidence_refs: tuple[EvidenceReference, ...] = ()
    challenge_outcome: ClaimReviewOutcome | None = None
    limitations: tuple[str, ...] = ()
    quote_attribution: ReportQuoteAttribution | None = None

    @model_validator(mode="after")
    def sourced(self) -> Self:
        if not (
            self.source_claim_ids
            or self.evidence_refs
            or self.challenge_finding_ids
            or self.alternative_interpretation_ids
        ):
            raise ValueError("Every report statement must cite something")
        if self.origin == "NARRATOR" and self.epistemic_class == "FACT" and not self.evidence_refs:
            raise ValueError("Narrator FACT text requires a direct evidence citation")
        if self.challenge_outcome == "INVALID_AGAINST_BUNDLE" and not self.challenge_finding_ids:
            raise ValueError("An invalid-outcome statement must expose its finding(s)")
        return self


class ReportSection(CalculationModel):
    section: ReportSectionId
    coverage: ReportSectionCoverage
    tables: tuple[ReportTable, ...] = ()
    statements: tuple[ReportStatement, ...] = ()

    @model_validator(mode="after")
    def scoped(self) -> Self:
        if any(t.section != self.section for t in self.tables):
            raise ValueError("Table section mismatch")
        if any(s.section != self.section for s in self.statements):
            raise ValueError("Statement section mismatch")
        return self


class DocumentCorpusEntry(CalculationModel):
    document_type: DocumentType
    document_id: UUID
    version_id: UUID
    fiscal_label: str | None = None
    publication_date: str | None = None
    coverage_status: Literal["COMPLETE", "PARTIAL", "FAILED"]
    evidence_ref: EvidenceReference


class DocumentCorpus(CalculationModel):
    """corpus_scope is a structural guarantee, not a claim of completeness: only
    documents explicitly pinned into the original Part 5B AnalysisRequest ever
    appear here - never the full retained universe for the company."""

    corpus_scope: Literal["PINNED_REPORT_INPUT_ONLY"] = "PINNED_REPORT_INPUT_ONLY"
    entries: tuple[DocumentCorpusEntry, ...] = ()


class ReportLimitation(CalculationModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    section: ReportSectionId | None = None
    evidence_refs: tuple[EvidenceReference, ...] = ()


class MonitoringEntry(CalculationModel):
    """V1 is a static rendering of already-known variables - never a scheduler,
    watcher, or alert. No trigger threshold is ever invented."""

    metric: str = Field(min_length=1)
    current_value: str | None = None
    prior_value: str | None = None
    direction: Literal["UP", "DOWN", "FLAT", "UNKNOWN"]
    source: EvidenceReference
    period: str | None = None
    coverage: SectionCoverageStatus
    monitoring_rationale: str = Field(min_length=1)


class ExecutiveResearchSnapshot(CalculationModel):
    """No recommendation card, no global stance. Structured fields only."""

    company_overview: tuple[ReportStatement, ...] = ()
    key_financial_trend: tuple[ReportStatement, ...] = ()
    key_sector_trend: tuple[ReportStatement, ...] = ()
    supporting_evidence: tuple[ReportStatement, ...] = ()
    disconfirming_evidence: tuple[EvidenceReference, ...] = ()
    document_highlights: tuple[ReportStatement, ...] = ()
    valuation_evidence: tuple[ReportStatement, ...] = ()
    market_implied_expectations: tuple[ReportStatement, ...] = ()
    catalysts: tuple[ReportStatement, ...] = ()
    risks: tuple[ReportStatement, ...] = ()
    material_challenger_finding_ids: tuple[UUID, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    major_data_gaps: tuple[ReportLimitation, ...] = ()
    research_completeness: ResearchCompleteness
    valuation_evidence_state: ValuationEvidenceState
    narrative: str | None = None


class ReportSynthesis(CalculationModel):
    """No global directional stance. See docs/part6a-proof.md for why it was
    evaluated and removed."""

    research_completeness: ResearchCompleteness
    valuation_evidence_state: ValuationEvidenceState
    supporting_claim_ids: tuple[UUID, ...] = ()
    challenged_claim_ids: tuple[UUID, ...] = ()
    invalid_claim_ids: tuple[UUID, ...] = ()
    unverifiable_claim_ids: tuple[UUID, ...] = ()
    material_disconfirming_evidence: tuple[EvidenceReference, ...] = ()
    material_challenger_finding_ids: tuple[UUID, ...] = ()
    evidence_limitations: tuple[ReportLimitation, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    narrative: str | None = None


class NarrativeBlockCandidate(CalculationModel):
    """The ONLY shape a ReportNarrator may emit. No evidence ID, hash, numeric
    value, unit, period, scope, coverage status, or outcome field exists here."""

    section: ReportSectionId
    text: str = Field(min_length=1)


class RejectedNarrativeBlock(CalculationModel):
    raw_block: dict[str, object]
    issues: tuple[ValidationIssue, ...] = Field(min_length=1)


class ArtifactManifestEntry(CalculationModel):
    artifact: Literal[
        "COMPANY_INSTITUTIONAL_RESEARCH_MD", "COMPANY_INVESTOR_BRIEF_MD", "COMPANY_RESEARCH_JSON"
    ]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ArtifactManifest(CalculationModel):
    entries: tuple[ArtifactManifestEntry, ...] = ()


class ReproducibilityManifest(CalculationModel):
    analysis_snapshot_id: UUID
    analysis_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    challenge_snapshot_id: UUID
    challenge_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    relevance_registry_version: str = Field(min_length=1)
    narrator_metadata: ModelMetadata | None = None


class ResearchReportSnapshot(CalculationModel):
    """Option B persistence: pins exact upstream IDs + hashes; does not re-embed
    the full nested ChallengeSnapshot/AnalysisSnapshot chain. Append-only."""

    schema_version: Literal["6A.1"] = "6A.1"
    id: UUID
    company_id: UUID
    analysis_snapshot_id: UUID
    analysis_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    challenge_snapshot_id: UUID
    challenge_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sections: tuple[ReportSection, ...]
    executive_snapshot: ExecutiveResearchSnapshot
    synthesis: ReportSynthesis
    document_corpus: DocumentCorpus
    monitoring: tuple[MonitoringEntry, ...] = ()
    limitations: tuple[ReportLimitation, ...] = ()
    narrator_metadata: ModelMetadata | None = None
    rejected_narrative_blocks: tuple[RejectedNarrativeBlock, ...] = ()
    artifact_manifest: ArtifactManifest = Field(default_factory=ArtifactManifest)
    reproducibility: ReproducibilityManifest
    validation_issues: tuple[ValidationIssue, ...] = ()
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def lineage(self) -> Self:
        r = self.reproducibility
        if (r.analysis_snapshot_id, r.analysis_snapshot_sha256) != (
            self.analysis_snapshot_id,
            self.analysis_snapshot_sha256,
        ):
            raise ValueError("Reproducibility manifest analysis-snapshot pin mismatch")
        if (r.challenge_snapshot_id, r.challenge_snapshot_sha256) != (
            self.challenge_snapshot_id,
            self.challenge_snapshot_sha256,
        ):
            raise ValueError("Reproducibility manifest challenge-snapshot pin mismatch")
        if r.narrator_metadata != self.narrator_metadata:
            raise ValueError("Reproducibility manifest narrator metadata mismatch")
        if len({s.section for s in self.sections}) != len(self.sections):
            raise ValueError("Duplicate report section")
        return self


__all__ = [
    "ArtifactManifest",
    "ArtifactManifestEntry",
    "DocumentCorpus",
    "DocumentCorpusEntry",
    "EpistemicClass",
    "ExecutiveResearchSnapshot",
    "MonitoringEntry",
    "NarrativeBlockCandidate",
    "RejectedNarrativeBlock",
    "ReportCoverageReasonCode",
    "ReportExecutionSpec",
    "ReportLimitation",
    "ReportMetric",
    "ReportQuoteAttribution",
    "ReportRequest",
    "ReportSection",
    "ReportSectionCoverage",
    "ReportSectionId",
    "ReportStatement",
    "ReportSynthesis",
    "ReportTable",
    "ReproducibilityManifest",
    "ResearchCompleteness",
    "ResearchReportSnapshot",
    "ValuationEvidenceState",
]
