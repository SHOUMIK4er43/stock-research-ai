"""Pure domain/validator proofs for Part 5C - no engine, no database, no network."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_research_ai.application.challenge_validation import (
    validate_challenger_candidate,
    validate_challenger_response,
)
from stock_research_ai.domain.analysis import (
    AnalysisRequest,
    AnalysisSnapshot,
    AnalystClaim,
    AnalystExecutionSpec,
    AnstInputBundle,
    ModelMetadata,
    PromptTemplate,
    RawAnalystResponse,
    RenderedModelRequest,
    SectionCoverage,
    SupportAssessment,
    TimeScope,
)
from stock_research_ai.domain.challenge import (
    CHALLENGE_TIER,
    LIMITATION_TIER,
    SEVERITY_BY_TYPE,
    AlternativeInterpretation,
    ChallengeFinding,
    ChallengeRequest,
    ChallengerInputBundle,
    ChallengeSnapshot,
    ClaimReview,
    RelevanceRule,
    SectionReview,
    aggregate_omission_status,
    aggregate_section_outcome,
    compute_claim_outcome,
)

FAKE_HASH = "a" * 64


def evidence_reference(kind="VALUATION", item_key="k1"):
    from stock_research_ai.domain.analysis import EvidenceReference

    return EvidenceReference(
        kind=kind, owning_snapshot_id=uuid4(), item_key=item_key,
        snapshot_payload_sha256=FAKE_HASH,
    )


def finding(finding_type, source="DETERMINISTIC", cited=()):
    return ChallengeFinding(
        finding_id=uuid4(),
        finding_type=finding_type,
        severity=SEVERITY_BY_TYPE[finding_type],
        statement="synthetic finding",
        cited_evidence=cited,
        source=source,
    )


class TestOutcomeAggregation:
    def test_no_findings_is_supported(self):
        assert compute_claim_outcome(()) == "SUPPORTED_BY_BUNDLE"

    def test_blocking_finding_is_invalid_regardless_of_other_findings(self):
        findings = (finding("CITATION_NOT_FOUND"), finding("SECTOR_LANGUAGE_CONCERN"))
        assert compute_claim_outcome(findings) == "INVALID_AGAINST_BUNDLE"

    def test_only_informational_is_unverifiable(self):
        findings = (finding("INSUFFICIENT_EVIDENCE_TO_VERIFY"),)
        assert compute_claim_outcome(findings) == "UNVERIFIABLE_FROM_BUNDLE"

    def test_challenge_tier_wins_over_limitation_tier(self):
        findings = (finding("DETERMINISTIC_CONFLICT"), finding("UNSUPPORTED_CERTAINTY"))
        assert compute_claim_outcome(findings) == "CHALLENGED"

    def test_limitation_tier_alone_is_supported_with_limitations(self):
        findings = (finding("POINT_IN_TIME_LIMITATION_OMITTED"),)
        assert compute_claim_outcome(findings) == "SUPPORTED_WITH_LIMITATIONS"

    def test_every_finding_type_is_in_exactly_one_tier_or_neither_ambiguously(self):
        for finding_type, severity in SEVERITY_BY_TYPE.items():
            in_challenge = finding_type in CHALLENGE_TIER
            in_limitation = finding_type in LIMITATION_TIER
            assert not (in_challenge and in_limitation)
            if severity == "BLOCKING":
                assert not in_challenge and not in_limitation


class TestChallengeFindingInvariants:
    def test_severity_must_match_fixed_table(self):
        with pytest.raises(ValidationError):
            ChallengeFinding(
                finding_id=uuid4(), finding_type="CITATION_NOT_FOUND", severity="NON_BLOCKING",
                statement="x", source="DETERMINISTIC",
            )

    def test_model_assisted_finding_cannot_be_blocking(self):
        with pytest.raises(ValidationError):
            ChallengeFinding(
                finding_id=uuid4(), finding_type="CITATION_NOT_FOUND", severity="BLOCKING",
                statement="x", source="MODEL_ASSISTED",
            )

    def test_model_suggested_finding_types_are_never_blocking(self):
        for finding_type in (
            "MODEL_SUGGESTED_MATERIAL_EVIDENCE",
            "MODEL_SUGGESTED_ANALYTICAL_TENSION",
            "MODEL_SUGGESTED_LANGUAGE_CONCERN",
        ):
            assert SEVERITY_BY_TYPE[finding_type] == "NON_BLOCKING"
            finding(finding_type, source="MODEL_ASSISTED")  # must not raise


class TestClaimReviewInvariants:
    def test_outcome_must_match_deterministic_aggregation(self):
        with pytest.raises(ValidationError):
            ClaimReview(
                review_id=uuid4(), analyst_claim_id=uuid4(), outcome="CHALLENGED",
                omission_review_status="NO_RELEVANT_OMISSION_FOUND", findings=(),
            )

    def test_consistent_outcome_constructs_cleanly(self):
        review = ClaimReview(
            review_id=uuid4(), analyst_claim_id=uuid4(), outcome="SUPPORTED_BY_BUNDLE",
            omission_review_status="NO_RELEVANT_OMISSION_FOUND", findings=(),
        )
        assert review.outcome == "SUPPORTED_BY_BUNDLE"


class TestSectionAggregation:
    def test_invalid_dominates(self):
        outcomes = ("SUPPORTED_BY_BUNDLE", "INVALID_AGAINST_BUNDLE", "CHALLENGED")
        assert aggregate_section_outcome(outcomes) == "HAS_INVALID"

    def test_all_supported_when_no_issues(self):
        assert aggregate_section_outcome(("SUPPORTED_BY_BUNDLE",) * 3) == "ALL_SUPPORTED"

    def test_empty_is_not_reviewed(self):
        assert aggregate_section_outcome(()) == "NOT_REVIEWED"

    def test_omission_material_dominates(self):
        statuses = ("NO_RELEVANT_OMISSION_FOUND", "MATERIAL_OMISSION_FOUND")
        assert aggregate_omission_status(statuses) == "MATERIAL_OMISSION_FOUND"

    def test_omission_not_covered_only_when_nothing_else_present(self):
        statuses = ("OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES",) * 2
        assert aggregate_omission_status(statuses) == "OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES"


class TestRelevanceRuleInvariants:
    def test_tension_relationship_requires_tension_finding_type(self):
        with pytest.raises(ValidationError):
            RelevanceRule(
                rule_id="r1", section="SECTOR_KPIS", sector_profile=None,
                primary_metric_family=("A",), related_metric_family=("B",),
                relationship_type="LINKED_FAMILY_TENSION",
                period_compatibility="ANY_PERIOD", scope_compatibility="ANY_SCOPE",
                finding_type="MISSING_MATERIAL_EVIDENCE",
            )

    def test_omission_relationship_requires_omission_finding_type(self):
        with pytest.raises(ValidationError):
            RelevanceRule(
                rule_id="r1", section="SECTOR_KPIS", sector_profile=None,
                primary_metric_family=("A",), related_metric_family=("B",),
                relationship_type="SAME_FAMILY_OMISSION",
                period_compatibility="ANY_PERIOD", scope_compatibility="ANY_SCOPE",
                finding_type="ANALYTICAL_TENSION",
            )


def _minimal_execution():
    return AnalystExecutionSpec(template_id="v1", template_version=1)


def _minimal_model_metadata(bundle_sha="b" * 64):
    template = PromptTemplate(
        template_id="v1", template_version=1, content="c", content_sha256="c" * 64
    )
    request = RenderedModelRequest(
        bundle_sha256=bundle_sha, template=template, generation_parameters={},
        generation_parameters_sha256="d" * 64, rendered_text="r", rendered_sha256="e" * 64,
    )
    return ModelMetadata(
        provider="stub", model_id="m1", model_version="1", request=request,
        raw_response=RawAnalystResponse(structured_payload={}, structured_payload_sha256="f" * 64),
        generated_at=datetime.now(UTC),
    )


def _minimal_reviewed_snapshot():
    ref = evidence_reference()
    request = AnalysisRequest(
        company_id=uuid4(), classification=dict(
            classification="NON_FINANCIAL", basis="CALLER_DECLARED", declaration="x"
        ),
        profile_id="IT_SERVICES_PROFILE", sections=("VALUATION",),
    )
    coverage = (
        SectionCoverage(section="VALUATION", status="AVAILABLE", reason="ok",
                         contributing_evidence_kinds=("VALUATION",)),
    )
    bundle = AnstInputBundle(
        bundle_id=uuid4(), request=request, assembled_at=datetime.now(UTC), sources=(ref,),
        section_coverage=coverage,
    )
    claim = AnalystClaim(
        claim_id=uuid4(), section="VALUATION", statement="Observed valuation evidence is cited.",
        materiality="PRIMARY", citations=(ref,), source_epistemic_classes=("CALCULATION",),
        support=SupportAssessment(
            basis="SINGLE_SOURCE", distinct_source_kinds=1, cites_unverified_semantics=False,
            cites_limitation_issue=False,
        ),
        time_scope=TimeScope(
            evaluated_at=bundle.assembled_at, as_of_policy="ANALYSIS_CURRENT_PINNED_BUNDLE_V1",
            point_in_time_unverified=False,
        ),
        sector_profile="IT_SERVICES_PROFILE",
    )
    from stock_research_ai.application.hashing import digest

    return AnalysisSnapshot(
        id=uuid4(), company_id=request.company_id, request=request, execution=_minimal_execution(),
        input_bundle=bundle, bundle_sha256=digest(bundle), sources=(ref,),
        model_metadata=_minimal_model_metadata(bundle_sha=digest(bundle)),
        section_coverage=coverage, claims=(claim,), evaluated_at=bundle.assembled_at,
    ), claim, ref


class TestChallengeSnapshotInvariants:
    def test_every_claim_must_receive_exactly_one_review(self):
        from stock_research_ai.application.hashing import digest

        reviewed, claim, ref = _minimal_reviewed_snapshot()
        challenger_bundle = ChallengerInputBundle(
            bundle_id=uuid4(), reviewed_analysis=reviewed,
            analysis_snapshot_sha256=digest(reviewed), assembled_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            ChallengeSnapshot(
                id=uuid4(), analysis_snapshot_id=reviewed.id,
                analysis_snapshot_sha256=digest(reviewed), challenger_bundle=challenger_bundle,
                challenger_bundle_sha256=digest(challenger_bundle),
                relevance_registry_version="5C.1-v1", claim_reviews=(),
                section_reviews=(), independence_basis="DETERMINISTIC_ONLY",
                evaluated_at=datetime.now(UTC),
            )

    def test_citation_outside_bundle_is_refused(self):
        from stock_research_ai.application.hashing import digest

        reviewed, claim, ref = _minimal_reviewed_snapshot()
        challenger_bundle = ChallengerInputBundle(
            bundle_id=uuid4(), reviewed_analysis=reviewed,
            analysis_snapshot_sha256=digest(reviewed), assembled_at=datetime.now(UTC),
        )
        rogue_ref = evidence_reference(item_key="not-in-bundle")
        review = ClaimReview(
            review_id=uuid4(), analyst_claim_id=claim.claim_id, outcome="SUPPORTED_BY_BUNDLE",
            omission_review_status="NO_RELEVANT_OMISSION_FOUND",
            additional_relevant_evidence=(rogue_ref,),
        )
        with pytest.raises(ValidationError):
            ChallengeSnapshot(
                id=uuid4(), analysis_snapshot_id=reviewed.id,
                analysis_snapshot_sha256=digest(reviewed), challenger_bundle=challenger_bundle,
                challenger_bundle_sha256=digest(challenger_bundle),
                relevance_registry_version="5C.1-v1", claim_reviews=(review,),
                section_reviews=(), independence_basis="DETERMINISTIC_ONLY",
                evaluated_at=datetime.now(UTC),
            )

    def test_well_formed_snapshot_constructs(self):
        from stock_research_ai.application.hashing import digest

        reviewed, claim, ref = _minimal_reviewed_snapshot()
        challenger_bundle = ChallengerInputBundle(
            bundle_id=uuid4(), reviewed_analysis=reviewed,
            analysis_snapshot_sha256=digest(reviewed), assembled_at=datetime.now(UTC),
        )
        review = ClaimReview(
            review_id=uuid4(), analyst_claim_id=claim.claim_id, outcome="SUPPORTED_BY_BUNDLE",
            omission_review_status="OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES",
        )
        section_review = SectionReview(
            section="VALUATION", coverage_status="AVAILABLE", claim_count=1,
            reviewed_claim_count=1, blocking_findings=0, non_blocking_findings=0,
            informational_findings=0,
            omission_review_coverage="OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES",
            section_review_outcome="ALL_SUPPORTED",
        )
        snapshot = ChallengeSnapshot(
            id=uuid4(), analysis_snapshot_id=reviewed.id, analysis_snapshot_sha256=digest(reviewed),
            challenger_bundle=challenger_bundle, challenger_bundle_sha256=digest(challenger_bundle),
            relevance_registry_version="5C.1-v1", claim_reviews=(review,),
            section_reviews=(section_review,), independence_basis="DETERMINISTIC_ONLY",
            evaluated_at=datetime.now(UTC),
        )
        assert snapshot.claim_reviews[0].outcome == "SUPPORTED_BY_BUNDLE"

    def test_challenge_request_shape(self):
        assert ChallengeRequest(analysis_snapshot_id=uuid4()).analysis_snapshot_id is not None


class TestChallengerCandidateValidation:
    def _bundle(self):
        from stock_research_ai.application.hashing import digest

        reviewed, claim, ref = _minimal_reviewed_snapshot()
        bundle = ChallengerInputBundle(
            bundle_id=uuid4(), reviewed_analysis=reviewed,
            analysis_snapshot_sha256=digest(reviewed), assembled_at=datetime.now(UTC),
        )
        return bundle, claim, ref

    def test_alternative_interpretation_maps_to_alt_not_finding(self):
        bundle, claim, ref = self._bundle()
        raw = dict(
            analyst_claim_id=str(claim.claim_id), suggestion_kind="ALTERNATIVE_INTERPRETATION",
            statement="An alternative reading of the same evidence.",
            cited_evidence=[dict(kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id),
                                  item_key=ref.item_key)],
        )
        finding, alt, rejection = validate_challenger_candidate(raw, bundle, {claim.claim_id})
        assert finding is None and rejection is None
        assert isinstance(alt, AlternativeInterpretation)
        assert alt.source == "MODEL_ASSISTED"

    @pytest.mark.parametrize(
        "suggestion_kind,expected_type",
        [
            ("MATERIAL_EVIDENCE", "MODEL_SUGGESTED_MATERIAL_EVIDENCE"),
            ("ANALYTICAL_TENSION", "MODEL_SUGGESTED_ANALYTICAL_TENSION"),
            ("LANGUAGE_CONCERN", "MODEL_SUGGESTED_LANGUAGE_CONCERN"),
        ],
    )
    def test_suggestion_kind_maps_to_fixed_finding_type(self, suggestion_kind, expected_type):
        bundle, claim, ref = self._bundle()
        raw = dict(
            analyst_claim_id=str(claim.claim_id), suggestion_kind=suggestion_kind,
            statement="A model suggestion about this claim.",
            cited_evidence=[dict(kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id),
                                  item_key=ref.item_key)],
        )
        finding, alt, rejection = validate_challenger_candidate(raw, bundle, {claim.claim_id})
        assert rejection is None and alt is None
        assert finding.finding_type == expected_type
        assert finding.source == "MODEL_ASSISTED"
        assert finding.severity == "NON_BLOCKING"

    def test_model_can_never_smuggle_a_finding_type_field(self):
        """ChallengerFindingCandidate has no finding_type field at all - a model trying to
        set one directly is silently dropped by extra='forbid', causing a MALFORMED_RESPONSE
        rejection rather than ever reaching a persisted ChallengeFinding."""
        bundle, claim, ref = self._bundle()
        raw = dict(
            analyst_claim_id=str(claim.claim_id), finding_type="NUMERIC_MISMATCH",
            suggestion_kind="MATERIAL_EVIDENCE", statement="x",
            cited_evidence=[dict(kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id),
                                  item_key=ref.item_key)],
        )
        finding, alt, rejection = validate_challenger_candidate(raw, bundle, {claim.claim_id})
        assert finding is None and alt is None
        assert rejection is not None
        assert rejection.issues[0].code == "MALFORMED_RESPONSE"

    def test_unknown_claim_id_is_rejected(self):
        bundle, claim, ref = self._bundle()
        raw = dict(
            analyst_claim_id=str(uuid4()), suggestion_kind="MATERIAL_EVIDENCE", statement="x",
            cited_evidence=[dict(kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id),
                                  item_key=ref.item_key)],
        )
        finding, alt, rejection = validate_challenger_candidate(raw, bundle, {claim.claim_id})
        assert finding is None and alt is None
        assert rejection.issues[0].code == "UNKNOWN_CLAIM"

    def test_citation_not_in_bundle_is_rejected(self):
        bundle, claim, ref = self._bundle()
        raw = dict(
            analyst_claim_id=str(claim.claim_id), suggestion_kind="MATERIAL_EVIDENCE",
            statement="x",
            cited_evidence=[dict(kind="VALUATION", owning_snapshot_id=str(uuid4()), item_key="z")],
        )
        finding, alt, rejection = validate_challenger_candidate(raw, bundle, {claim.claim_id})
        assert finding is None and alt is None
        assert rejection.issues[0].code == "CITATION_NOT_IN_BUNDLE"

    def test_forbidden_recommendation_language_is_rejected(self):
        bundle, claim, ref = self._bundle()
        raw = dict(
            analyst_claim_id=str(claim.claim_id), suggestion_kind="MATERIAL_EVIDENCE",
            statement="We recommend investors buy the stock.",
            cited_evidence=[dict(kind=ref.kind, owning_snapshot_id=str(ref.owning_snapshot_id),
                                  item_key=ref.item_key)],
        )
        finding, alt, rejection = validate_challenger_candidate(raw, bundle, {claim.claim_id})
        assert finding is None and alt is None
        assert rejection.issues[0].code == "FORBIDDEN_RECOMMENDATION_LANGUAGE"

    def test_response_level_routes_by_claim_id(self):
        bundle, claim, ref = self._bundle()
        raw_response = RawAnalystResponse(
            structured_payload={
                "candidates": [
                    dict(
                        analyst_claim_id=str(claim.claim_id),
                        suggestion_kind="ANALYTICAL_TENSION",
                        statement="A tension worth reviewing.",
                        cited_evidence=[
                            dict(
                                kind=ref.kind,
                                owning_snapshot_id=str(ref.owning_snapshot_id),
                                item_key=ref.item_key,
                            )
                        ],
                    )
                ]
            },
            structured_payload_sha256="a" * 64,
        )
        findings, alts, rejected = validate_challenger_response(
            raw_response, bundle, {claim.claim_id}
        )
        assert rejected == ()
        assert claim.claim_id in findings
        assert findings[claim.claim_id][0].finding_type == "MODEL_SUGGESTED_ANALYTICAL_TENSION"
        assert alts == {}
