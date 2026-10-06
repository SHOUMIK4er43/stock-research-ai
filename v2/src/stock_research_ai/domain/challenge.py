"""Independent-challenger contracts over a frozen Part 5B AnalysisSnapshot.

Closed-evidence V1: every citation any finding/review can reference must already exist in
the reviewed snapshot's own persisted AnstInputBundle. Nothing here discovers new evidence.
Severity and claim outcome are structural invariants, not application-layer convention -
see ChallengeFinding.severity_matches_type and ClaimReview.outcome_matches_findings.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from stock_research_ai.domain.analysis import (
    AnalysisSection,
    AnalysisSnapshot,
    CitationCandidate,
    EvidenceReference,
    ModelMetadata,
    SectionCoverageStatus,
    ValidationIssue,
)
from stock_research_ai.domain.calculations import CalculationModel

FindingType = Literal[
    # BLOCKING - mechanically established integrity/validity failures
    "CITATION_NOT_FOUND",
    "WRONG_COMPANY_EVIDENCE",
    "CITATION_HASH_MISMATCH",
    "UPSTREAM_LINEAGE_INVALID",
    "NUMERIC_MISMATCH",
    "UNIT_MISMATCH",
    "INCOMPATIBLE_SCOPE",
    "INCOMPATIBLE_PERIOD",
    "ASSUMPTION_PROMOTED_TO_FACT",
    "SCENARIO_ASSUMPTION_INVALID",
    "FORBIDDEN_RECOMMENDATION_LANGUAGE",
    "MANAGEMENT_QUOTE_ALTERED",
    "MISCONDUCT_LANGUAGE_UNSUPPORTED",
    "TEMPORAL_OVERCLAIM",
    "SECTOR_POLICY_VIOLATION",
    "EVALUATIVE_CLAIM_ON_UNAVAILABLE_EVIDENCE",
    # NON_BLOCKING - substantive but not integrity-breaking
    "DETERMINISTIC_CONFLICT",
    "ANALYTICAL_TENSION",
    "MODEL_SUGGESTED_ANALYTICAL_TENSION",
    "MISSING_MATERIAL_EVIDENCE",
    "MODEL_SUGGESTED_MATERIAL_EVIDENCE",
    "POINT_IN_TIME_LIMITATION_OMITTED",
    "UNSUPPORTED_CAUSALITY",
    "UNSUPPORTED_CERTAINTY",
    "ONE_SIDED_INTERPRETATION",
    "SECTOR_LANGUAGE_CONCERN",
    "MODEL_SUGGESTED_LANGUAGE_CONCERN",
    # INFORMATIONAL
    "INSUFFICIENT_EVIDENCE_TO_VERIFY",
]

Severity = Literal["BLOCKING", "NON_BLOCKING", "INFORMATIONAL"]

SEVERITY_BY_TYPE: dict[FindingType, Severity] = {
    "CITATION_NOT_FOUND": "BLOCKING",
    "WRONG_COMPANY_EVIDENCE": "BLOCKING",
    "CITATION_HASH_MISMATCH": "BLOCKING",
    "UPSTREAM_LINEAGE_INVALID": "BLOCKING",
    "NUMERIC_MISMATCH": "BLOCKING",
    "UNIT_MISMATCH": "BLOCKING",
    "INCOMPATIBLE_SCOPE": "BLOCKING",
    "INCOMPATIBLE_PERIOD": "BLOCKING",
    "ASSUMPTION_PROMOTED_TO_FACT": "BLOCKING",
    "SCENARIO_ASSUMPTION_INVALID": "BLOCKING",
    "FORBIDDEN_RECOMMENDATION_LANGUAGE": "BLOCKING",
    "MANAGEMENT_QUOTE_ALTERED": "BLOCKING",
    "MISCONDUCT_LANGUAGE_UNSUPPORTED": "BLOCKING",
    "TEMPORAL_OVERCLAIM": "BLOCKING",
    "SECTOR_POLICY_VIOLATION": "BLOCKING",
    "EVALUATIVE_CLAIM_ON_UNAVAILABLE_EVIDENCE": "BLOCKING",
    "DETERMINISTIC_CONFLICT": "NON_BLOCKING",
    "ANALYTICAL_TENSION": "NON_BLOCKING",
    "MODEL_SUGGESTED_ANALYTICAL_TENSION": "NON_BLOCKING",
    "MISSING_MATERIAL_EVIDENCE": "NON_BLOCKING",
    "MODEL_SUGGESTED_MATERIAL_EVIDENCE": "NON_BLOCKING",
    "POINT_IN_TIME_LIMITATION_OMITTED": "NON_BLOCKING",
    "UNSUPPORTED_CAUSALITY": "NON_BLOCKING",
    "UNSUPPORTED_CERTAINTY": "NON_BLOCKING",
    "ONE_SIDED_INTERPRETATION": "NON_BLOCKING",
    "SECTOR_LANGUAGE_CONCERN": "NON_BLOCKING",
    "MODEL_SUGGESTED_LANGUAGE_CONCERN": "NON_BLOCKING",
    "INSUFFICIENT_EVIDENCE_TO_VERIFY": "INFORMATIONAL",
}

CHALLENGE_TIER: frozenset[FindingType] = frozenset(
    {
        "DETERMINISTIC_CONFLICT",
        "MISSING_MATERIAL_EVIDENCE",
        "MODEL_SUGGESTED_MATERIAL_EVIDENCE",
        "ANALYTICAL_TENSION",
        "MODEL_SUGGESTED_ANALYTICAL_TENSION",
    }
)
LIMITATION_TIER: frozenset[FindingType] = frozenset(
    {
        "POINT_IN_TIME_LIMITATION_OMITTED",
        "UNSUPPORTED_CAUSALITY",
        "UNSUPPORTED_CERTAINTY",
        "ONE_SIDED_INTERPRETATION",
        "SECTOR_LANGUAGE_CONCERN",
        "MODEL_SUGGESTED_LANGUAGE_CONCERN",
    }
)

ClaimReviewOutcome = Literal[
    "SUPPORTED_BY_BUNDLE",
    "SUPPORTED_WITH_LIMITATIONS",
    "CHALLENGED",
    "INVALID_AGAINST_BUNDLE",
    "UNVERIFIABLE_FROM_BUNDLE",
]

OmissionReviewStatus = Literal[
    "NO_RELEVANT_OMISSION_FOUND",
    "MATERIAL_OMISSION_FOUND",
    "OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES",
]

IndependenceBasis = Literal[
    "DETERMINISTIC_ONLY",
    "SEPARATE_MODEL",
    "SAME_PROVIDER_DIFFERENT_MODEL",
    "SAME_MODEL_SEPARATE_PROMPT",
    "SAME_MODEL_SAME_PROMPT",
    "UNKNOWN",
]

ModelSuggestionKind = Literal[
    "MATERIAL_EVIDENCE", "ANALYTICAL_TENSION", "ALTERNATIVE_INTERPRETATION", "LANGUAGE_CONCERN"
]

FindingSource = Literal["DETERMINISTIC", "MODEL_ASSISTED"]


def compute_claim_outcome(findings: "tuple[ChallengeFinding, ...]") -> ClaimReviewOutcome:
    """The ONE deterministic mapping from findings to a claim outcome. A model-sourced
    finding can move a claim from SUPPORTED_BY_BUNDLE to CHALLENGED, but never to
    INVALID_AGAINST_BUNDLE - only a BLOCKING (structurally DETERMINISTIC-only) finding can."""
    if any(f.severity == "BLOCKING" for f in findings):
        return "INVALID_AGAINST_BUNDLE"
    non_informational = [f for f in findings if f.finding_type != "INSUFFICIENT_EVIDENCE_TO_VERIFY"]
    if findings and not non_informational:
        return "UNVERIFIABLE_FROM_BUNDLE"
    if any(f.finding_type in CHALLENGE_TIER for f in findings):
        return "CHALLENGED"
    if any(f.finding_type in LIMITATION_TIER for f in findings):
        return "SUPPORTED_WITH_LIMITATIONS"
    return "SUPPORTED_BY_BUNDLE"


class ChallengeFinding(CalculationModel):
    finding_id: UUID
    finding_type: FindingType
    severity: Severity
    statement: str = Field(min_length=1)
    cited_evidence: tuple[EvidenceReference, ...] = ()
    source: FindingSource

    @model_validator(mode="after")
    def severity_matches_type(self) -> Self:
        if self.severity != SEVERITY_BY_TYPE[self.finding_type]:
            raise ValueError("Finding severity must match its fixed per-type value")
        if self.severity == "BLOCKING" and self.source != "DETERMINISTIC":
            raise ValueError("Only a DETERMINISTIC finding may carry BLOCKING severity")
        return self


class AlternativeInterpretation(CalculationModel):
    interpretation_id: UUID
    original_claim_id: UUID
    statement: str = Field(min_length=1)
    epistemic_class: Literal["INTERPRETATION"] = "INTERPRETATION"
    citations: tuple[EvidenceReference, ...] = Field(min_length=1)
    source: FindingSource


class ClaimReview(CalculationModel):
    review_id: UUID
    analyst_claim_id: UUID
    outcome: ClaimReviewOutcome
    omission_review_status: OmissionReviewStatus
    findings: tuple[ChallengeFinding, ...] = ()
    reviewed_citations: tuple[EvidenceReference, ...] = ()
    additional_relevant_evidence: tuple[EvidenceReference, ...] = ()
    alternative_interpretations: tuple[AlternativeInterpretation, ...] = ()
    unresolved_questions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def outcome_matches_findings(self) -> Self:
        if self.outcome != compute_claim_outcome(self.findings):
            raise ValueError(
                "ClaimReview outcome must match the deterministic aggregation of its findings"
            )
        return self


SectionReviewOutcome = Literal[
    "ALL_SUPPORTED", "HAS_LIMITATIONS", "HAS_CHALLENGES", "HAS_INVALID",
    "HAS_UNVERIFIABLE", "NOT_REVIEWED",
]

_SECTION_OUTCOME_PRIORITY: tuple[tuple[ClaimReviewOutcome, SectionReviewOutcome], ...] = (
    ("INVALID_AGAINST_BUNDLE", "HAS_INVALID"),
    ("CHALLENGED", "HAS_CHALLENGES"),
    ("UNVERIFIABLE_FROM_BUNDLE", "HAS_UNVERIFIABLE"),
    ("SUPPORTED_WITH_LIMITATIONS", "HAS_LIMITATIONS"),
)


def aggregate_section_outcome(outcomes: tuple[ClaimReviewOutcome, ...]) -> SectionReviewOutcome:
    if not outcomes:
        return "NOT_REVIEWED"
    present = set(outcomes)
    for claim_outcome, section_outcome in _SECTION_OUTCOME_PRIORITY:
        if claim_outcome in present:
            return section_outcome
    return "ALL_SUPPORTED"


def aggregate_omission_status(statuses: tuple[OmissionReviewStatus, ...]) -> OmissionReviewStatus:
    if "MATERIAL_OMISSION_FOUND" in statuses:
        return "MATERIAL_OMISSION_FOUND"
    if "NO_RELEVANT_OMISSION_FOUND" in statuses:
        return "NO_RELEVANT_OMISSION_FOUND"
    return "OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES"


class SectionReview(CalculationModel):
    section: AnalysisSection
    coverage_status: SectionCoverageStatus
    claim_count: int = Field(ge=0)
    reviewed_claim_count: int = Field(ge=0)
    blocking_findings: int = Field(ge=0)
    non_blocking_findings: int = Field(ge=0)
    informational_findings: int = Field(ge=0)
    uncited_relevant_evidence: tuple[EvidenceReference, ...] = ()
    omission_review_coverage: OmissionReviewStatus
    section_review_outcome: SectionReviewOutcome

    @model_validator(mode="after")
    def counts_consistent(self) -> Self:
        if self.reviewed_claim_count > self.claim_count:
            raise ValueError("Cannot review more claims than exist in the section")
        return self


RelevanceRelationshipType = Literal[
    "SAME_FAMILY_OMISSION", "LINKED_FAMILY_OMISSION", "LINKED_FAMILY_TENSION"
]
PeriodCompatibility = Literal["SAME_PERIOD_REQUIRED", "ANY_PERIOD"]
ScopeCompatibility = Literal["SAME_SCOPE_REQUIRED", "ANY_SCOPE"]


class RelevanceRule(CalculationModel):
    rule_id: str = Field(min_length=1)
    section: AnalysisSection
    sector_profile: str | None = None
    primary_metric_family: tuple[str, ...] = Field(min_length=1)
    related_metric_family: tuple[str, ...] = Field(min_length=1)
    relationship_type: RelevanceRelationshipType
    period_compatibility: PeriodCompatibility
    scope_compatibility: ScopeCompatibility
    finding_type: Literal["MISSING_MATERIAL_EVIDENCE", "ANALYTICAL_TENSION"]

    @model_validator(mode="after")
    def relationship_matches_finding(self) -> Self:
        if self.relationship_type == "LINKED_FAMILY_TENSION" and (
            self.finding_type != "ANALYTICAL_TENSION"
        ):
            raise ValueError("A tension relationship must produce ANALYTICAL_TENSION")
        if self.relationship_type != "LINKED_FAMILY_TENSION" and (
            self.finding_type != "MISSING_MATERIAL_EVIDENCE"
        ):
            raise ValueError("An omission relationship must produce MISSING_MATERIAL_EVIDENCE")
        return self


class ChallengerFindingCandidate(CalculationModel):
    """The ONLY shape a ChallengerModel may emit. No finding_type, no severity, no
    citation validity, no claim outcome - those are all engine/validator computed."""

    analyst_claim_id: UUID
    suggestion_kind: ModelSuggestionKind
    statement: str = Field(min_length=1)
    cited_evidence: tuple[CitationCandidate, ...] = Field(min_length=1)


class RejectedChallengerCandidate(CalculationModel):
    raw_candidate: dict[str, object]
    issues: tuple[ValidationIssue, ...] = Field(min_length=1)


class ChallengerExecutionSpec(CalculationModel):
    template_id: str = Field(min_length=1)
    template_version: int = Field(ge=1)
    generation_parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ChallengeRequest(CalculationModel):
    analysis_snapshot_id: UUID


class ChallengerInputBundle(CalculationModel):
    """Embeds the reviewed AnalysisSnapshot verbatim - the closed-evidence boundary. No
    other evidence source exists anywhere in Part 5C's design."""

    schema_version: Literal["5C.1"] = "5C.1"
    bundle_id: UUID
    reviewed_analysis: AnalysisSnapshot
    analysis_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    assembled_at: AwareDatetime


class ChallengeSnapshot(CalculationModel):
    schema_version: Literal["5C.1"] = "5C.1"
    id: UUID
    analysis_snapshot_id: UUID
    analysis_snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    challenger_bundle: ChallengerInputBundle
    challenger_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    relevance_registry_version: str = Field(min_length=1)
    claim_reviews: tuple[ClaimReview, ...]
    section_reviews: tuple[SectionReview, ...]
    independence_basis: IndependenceBasis
    challenger_model_metadata: ModelMetadata | None = None
    rejected_candidates: tuple[RejectedChallengerCandidate, ...] = ()
    validation_issues: tuple[ValidationIssue, ...] = ()
    evaluated_at: AwareDatetime

    @model_validator(mode="after")
    def lineage(self) -> Self:
        reviewed = self.challenger_bundle.reviewed_analysis
        if self.analysis_snapshot_id != reviewed.id:
            raise ValueError("Snapshot must pin the exact reviewed analysis snapshot")
        if self.analysis_snapshot_sha256 != self.challenger_bundle.analysis_snapshot_sha256:
            raise ValueError("Analysis snapshot hash mismatch between snapshot and bundle")
        claim_ids = {c.claim_id for c in reviewed.claims}
        reviewed_ids = tuple(r.analyst_claim_id for r in self.claim_reviews)
        if len(set(reviewed_ids)) != len(reviewed_ids):
            raise ValueError("Duplicate claim review")
        if set(reviewed_ids) != claim_ids:
            raise ValueError("Every accepted analyst claim must receive exactly one review")
        source_keys = {
            (s.kind, s.owning_snapshot_id, s.item_key) for s in reviewed.input_bundle.sources
        }

        def refs_within_bundle(refs: tuple[EvidenceReference, ...]) -> bool:
            return all((r.kind, r.owning_snapshot_id, r.item_key) in source_keys for r in refs)

        for review in self.claim_reviews:
            if not refs_within_bundle(review.reviewed_citations):
                raise ValueError("Reviewed citation outside the frozen evidence bundle")
            if not refs_within_bundle(review.additional_relevant_evidence):
                raise ValueError("Additional relevant evidence outside the frozen evidence bundle")
            for finding in review.findings:
                if not refs_within_bundle(finding.cited_evidence):
                    raise ValueError("Finding citation outside the frozen evidence bundle")
            for alt in review.alternative_interpretations:
                if not refs_within_bundle(alt.citations):
                    raise ValueError("Alternative interpretation citation outside the bundle")
        return self


__all__ = [
    "CHALLENGE_TIER",
    "LIMITATION_TIER",
    "SEVERITY_BY_TYPE",
    "AlternativeInterpretation",
    "ChallengeFinding",
    "ChallengeRequest",
    "ChallengeSnapshot",
    "ChallengerExecutionSpec",
    "ChallengerFindingCandidate",
    "ChallengerInputBundle",
    "ClaimReview",
    "ClaimReviewOutcome",
    "FindingSource",
    "FindingType",
    "IndependenceBasis",
    "ModelSuggestionKind",
    "OmissionReviewStatus",
    "PeriodCompatibility",
    "RejectedChallengerCandidate",
    "RelevanceRelationshipType",
    "RelevanceRule",
    "ScopeCompatibility",
    "SectionReview",
    "SectionReviewOutcome",
    "Severity",
    "aggregate_omission_status",
    "aggregate_section_outcome",
    "compute_claim_outcome",
]
