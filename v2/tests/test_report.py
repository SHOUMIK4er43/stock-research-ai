"""Pure domain/validator proofs for Part 6A - no engine, no database, no network."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_research_ai.application.report import (
    CORE_SECTIONS,
    MAJOR_COMPLEMENTARY_SECTIONS,
    _claim_report_statements,
    compute_research_completeness,
    quote_attribution,
)
from stock_research_ai.application.report_validation import validate_narrative_block
from stock_research_ai.domain.analysis import (
    AnalystClaim,
    EvidenceReference,
    SupportAssessment,
    TimeScope,
)
from stock_research_ai.domain.challenge import ChallengeFinding, ClaimReview
from stock_research_ai.domain.documents import DocumentType
from stock_research_ai.domain.report import (
    ReportMetric,
    ReportRequest,
    ReportSection,
    ReportSectionCoverage,
    ReportStatement,
    ReportTable,
)

FAKE_HASH = "a" * 64


def evidence_reference(kind="VALUATION", item_key="k1"):
    return EvidenceReference(
        kind=kind, owning_snapshot_id=uuid4(), item_key=item_key,
        snapshot_payload_sha256=FAKE_HASH,
    )


def _claim(citations=(), sector_profile=None):
    ref = citations[0] if citations else evidence_reference()
    return AnalystClaim(
        claim_id=uuid4(), section="VALUATION", statement="Observed valuation evidence is cited.",
        materiality="PRIMARY", citations=(ref,), source_epistemic_classes=("CALCULATION",),
        support=SupportAssessment(
            basis="SINGLE_SOURCE", distinct_source_kinds=1, cites_unverified_semantics=False,
            cites_limitation_issue=False,
        ),
        time_scope=TimeScope(
            evaluated_at=datetime.now(UTC), as_of_policy="ANALYSIS_CURRENT_PINNED_BUNDLE_V1",
            point_in_time_unverified=False,
        ),
        sector_profile=sector_profile,
    )


def _review(claim_id, outcome, findings=()):
    return ClaimReview(
        review_id=uuid4(), analyst_claim_id=claim_id, outcome=outcome,
        omission_review_status="NO_RELEVANT_OMISSION_FOUND", findings=findings,
    )


def _finding(finding_type, severity, source="DETERMINISTIC"):
    return ChallengeFinding(
        finding_id=uuid4(), finding_type=finding_type, severity=severity,
        statement="synthetic finding", source=source,
    )


class TestReportRequest:
    def test_requires_challenge_snapshot_id(self):
        req = ReportRequest(challenge_snapshot_id=uuid4())
        assert req.challenge_snapshot_id is not None

    def test_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            ReportRequest(challenge_snapshot_id=uuid4(), analysis_snapshot_id=uuid4())


class TestCoverageVocabulary:
    def test_only_frozen_statuses_accepted(self):
        with pytest.raises(ValidationError):
            ReportSectionCoverage(status="NOT_YET_SUPPORTED_BY_UPSTREAM", reason="x")

    def test_frozen_status_with_reason_code_constructs(self):
        coverage = ReportSectionCoverage(
            status="UNAVAILABLE", reason="No AGM classification exists upstream.",
            reason_code="UPSTREAM_CAPABILITY_NOT_IMPLEMENTED",
        )
        assert coverage.status == "UNAVAILABLE"
        assert coverage.reason_code == "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED"

    def test_unknown_reason_code_rejected(self):
        with pytest.raises(ValidationError):
            ReportSectionCoverage(status="UNAVAILABLE", reason="x", reason_code="MADE_UP_CODE")


class TestReportStatementInvariants:
    def test_must_cite_something(self):
        with pytest.raises(ValidationError):
            ReportStatement(
                statement_id=uuid4(), section="BUSINESS_MODEL", text="x",
                epistemic_class="INTERPRETATION", origin="DETERMINISTIC",
            )

    def test_invalid_outcome_requires_finding_ids(self):
        with pytest.raises(ValidationError):
            ReportStatement(
                statement_id=uuid4(), section="VALUATION", text="x",
                epistemic_class="INTERPRETATION", origin="DETERMINISTIC",
                source_claim_ids=(uuid4(),), challenge_outcome="INVALID_AGAINST_BUNDLE",
            )

    def test_narrator_fact_requires_evidence_ref(self):
        with pytest.raises(ValidationError):
            ReportStatement(
                statement_id=uuid4(), section="VALUATION", text="x", epistemic_class="FACT",
                origin="NARRATOR", source_claim_ids=(uuid4(),),
            )

    def test_well_formed_statement_constructs(self):
        st = ReportStatement(
            statement_id=uuid4(), section="VALUATION", text="x", epistemic_class="INTERPRETATION",
            origin="DETERMINISTIC", source_claim_ids=(uuid4(),),
        )
        assert st.text == "x"


class TestClaimReportStatements:
    def test_supported_claim_produces_one_statement(self):
        claim = _claim()
        review = _review(claim.claim_id, "SUPPORTED_BY_BUNDLE")
        statements = _claim_report_statements(claim, review, "VALUATION")
        assert len(statements) == 1
        assert statements[0].challenge_outcome == "SUPPORTED_BY_BUNDLE"
        assert statements[0].text == claim.statement

    def test_invalid_claim_produces_no_statements_in_topical_section(self):
        claim = _claim()
        finding = _finding("NUMERIC_MISMATCH", "BLOCKING")
        review = _review(claim.claim_id, "INVALID_AGAINST_BUNDLE", findings=(finding,))
        statements = _claim_report_statements(claim, review, "VALUATION")
        assert statements == ()

    def test_challenged_claim_pairs_finding_statements(self):
        claim = _claim()
        finding = _finding("DETERMINISTIC_CONFLICT", "NON_BLOCKING")
        review = _review(claim.claim_id, "CHALLENGED", findings=(finding,))
        statements = _claim_report_statements(claim, review, "VALUATION")
        assert len(statements) == 2
        assert statements[0].challenge_outcome == "CHALLENGED"
        assert statements[1].challenge_finding_ids == (finding.finding_id,)

    def test_unverifiable_claim_gets_limitation_tag(self):
        claim = _claim()
        finding = _finding("INSUFFICIENT_EVIDENCE_TO_VERIFY", "INFORMATIONAL")
        review = _review(claim.claim_id, "UNVERIFIABLE_FROM_BUNDLE", findings=(finding,))
        statements = _claim_report_statements(claim, review, "VALUATION")
        assert "NOT_INDEPENDENTLY_VERIFIABLE_FROM_BUNDLE" in statements[0].limitations


class TestQuoteAttribution:
    def test_concall_transcript_is_neutral(self):
        assert quote_attribution(DocumentType.CONCALL_TRANSCRIPT) == "TRANSCRIPT_STATEMENT"

    def test_annual_report_is_management_statement(self):
        assert quote_attribution(DocumentType.ANNUAL_REPORT) == "MANAGEMENT_STATEMENT"

    def test_investor_presentation_is_management_statement(self):
        assert quote_attribution(DocumentType.INVESTOR_PRESENTATION) == "MANAGEMENT_STATEMENT"

    def test_credit_rating_is_neutral(self):
        assert quote_attribution(DocumentType.CREDIT_RATING) == "TRANSCRIPT_STATEMENT"

    def test_other_is_neutral(self):
        assert quote_attribution(DocumentType.OTHER) == "TRANSCRIPT_STATEMENT"


def _section_with_table_value(value: str, status: str = "AVAILABLE") -> ReportSection:
    metric = ReportMetric(
        label="Sales Growth", display_value=value, status=status,
        evidence_ref=evidence_reference(kind="SECTOR_KPI"),
    )
    table = ReportTable(
        table_id="t1", title="T", section="VALUATION", columns=("Metric",), rows=((metric,),),
    )
    return ReportSection(
        section="VALUATION",
        coverage=ReportSectionCoverage(status="AVAILABLE", reason="ok"),
        tables=(table,),
    )


class TestNarratorValidation:
    def test_unknown_section_rejected(self):
        statement, rejection = validate_narrative_block(
            {"section": "VALUATION", "text": "Sales grew 12%."}, ()
        )
        assert statement is None
        assert rejection.issues[0].code == "UNKNOWN_SECTION"

    def test_malformed_block_rejected(self):
        section = _section_with_table_value("12")
        statement, rejection = validate_narrative_block({"section": "VALUATION"}, (section,))
        assert statement is None
        assert rejection.issues[0].code == "MALFORMED_RESPONSE"

    def test_forbidden_recommendation_language_rejected(self):
        section = _section_with_table_value("12")
        statement, rejection = validate_narrative_block(
            {"section": "VALUATION", "text": "We recommend investors buy the stock."}, (section,)
        )
        assert statement is None
        assert rejection.issues[0].code == "FORBIDDEN_RECOMMENDATION_LANGUAGE"

    def test_unsupported_numeric_token_rejected(self):
        section = _section_with_table_value("12")
        statement, rejection = validate_narrative_block(
            {"section": "VALUATION", "text": "Growth accelerated to 47%."}, (section,)
        )
        assert statement is None
        assert rejection.issues[0].code == "UNSUPPORTED_NUMERIC_TOKEN"

    def test_supported_numeric_token_accepted(self):
        section = _section_with_table_value("12")
        statement, rejection = validate_narrative_block(
            {"section": "VALUATION", "text": "Growth was 12% for the period."}, (section,)
        )
        assert rejection is None
        assert statement is not None
        assert statement.origin == "NARRATOR"
        assert statement.epistemic_class == "INTERPRETATION"
        assert len(statement.evidence_refs) == 1

    def test_no_citable_content_rejected(self):
        section = ReportSection(
            section="VALUATION",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="ok"),
        )
        statement, rejection = validate_narrative_block(
            {"section": "VALUATION", "text": "Nothing numeric here at all."}, (section,)
        )
        assert statement is None
        assert rejection.issues[0].code == "NO_CITABLE_CONTENT_IN_SECTION"

    def test_invalid_section_blocks_narrator_elsewhere(self):
        finding = _finding("NUMERIC_MISMATCH", "BLOCKING")
        bad_statement = ReportStatement(
            statement_id=uuid4(), section="VALUATION", text="An invalid claim.",
            epistemic_class="INTERPRETATION", origin="DETERMINISTIC",
            challenge_finding_ids=(finding.finding_id,), challenge_outcome="INVALID_AGAINST_BUNDLE",
        )
        section = ReportSection(
            section="VALUATION",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="ok"),
            statements=(bad_statement,),
        )
        statement, rejection = validate_narrative_block(
            {"section": "VALUATION", "text": "Everything looks solid here."}, (section,)
        )
        assert statement is None
        assert rejection.issues[0].code == "NARRATOR_CANNOT_NARRATE_INVALID_SECTION"

    def test_forbidden_corpus_completeness_claim_rejected(self):
        section = ReportSection(
            section="DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE",
            coverage=ReportSectionCoverage(status="AVAILABLE", reason="ok"),
            statements=(
                ReportStatement(
                    statement_id=uuid4(), section="DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE",
                    text="4 annual reports present.", epistemic_class="FACT",
                    origin="DETERMINISTIC",
                    evidence_refs=(evidence_reference(kind="DOCUMENT_CHUNK"),),
                ),
            ),
        )
        statement, rejection = validate_narrative_block(
            {"section": "DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE",
             "text": "All annual reports are available."},
            (section,),
        )
        assert statement is None
        assert rejection.issues[0].code == "FORBIDDEN_CORPUS_COMPLETENESS_CLAIM"


def _all_family_sections(status="AVAILABLE", overrides=None):
    """One ReportSection per CORE/MAJOR_COMPLEMENTARY family member, all at `status`
    unless a specific section_id is overridden with an (status, reason_code) pair."""
    overrides = overrides or {}
    sections = []
    for section_id in (*CORE_SECTIONS, *MAJOR_COMPLEMENTARY_SECTIONS):
        s_status, reason_code = overrides.get(section_id, (status, None))
        sections.append(
            ReportSection(
                section=section_id,
                coverage=ReportSectionCoverage(
                    status=s_status, reason="x", reason_code=reason_code
                ),
            )
        )
    return tuple(sections)


class TestResearchCompleteness:
    """Mechanical family-based rule: no percentage, no weighted score, no model input."""

    def test_broad_coverage_is_high(self):
        assert compute_research_completeness(_all_family_sections("AVAILABLE")) == "HIGH"

    def test_not_applicable_does_not_reduce_completeness(self):
        core_section = next(iter(CORE_SECTIONS))
        overrides = {core_section: ("NOT_APPLICABLE", None)}
        sections = _all_family_sections("AVAILABLE", overrides)
        assert compute_research_completeness(sections) == "HIGH"

    def test_one_core_gap_is_moderate(self):
        core_section = next(iter(CORE_SECTIONS))
        overrides = {core_section: ("UNAVAILABLE", None)}
        sections = _all_family_sections("AVAILABLE", overrides)
        assert compute_research_completeness(sections) == "MODERATE"

    def test_two_core_gaps_is_limited(self):
        core_iter = iter(CORE_SECTIONS)
        first, second = next(core_iter), next(core_iter)
        overrides = {first: ("UNAVAILABLE", None), second: ("INSUFFICIENT_INPUT", None)}
        sections = _all_family_sections("AVAILABLE", overrides)
        assert compute_research_completeness(sections) == "LIMITED"

    def test_core_available_but_complementary_missing_is_moderate(self):
        complementary_section = next(iter(MAJOR_COMPLEMENTARY_SECTIONS))
        overrides = {complementary_section: ("UNAVAILABLE", None)}
        sections = _all_family_sections("AVAILABLE", overrides)
        assert compute_research_completeness(sections) == "MODERATE"

    def test_insufficient_input_is_not_treated_as_successful_coverage(self):
        core_iter = iter(CORE_SECTIONS)
        first, second = next(core_iter), next(core_iter)
        overrides = {first: ("INSUFFICIENT_INPUT", None), second: ("INSUFFICIENT_INPUT", None)}
        sections = _all_family_sections("AVAILABLE", overrides)
        assert compute_research_completeness(sections) == "LIMITED"

    def test_unavailable_is_not_treated_as_successful_coverage(self):
        core_iter = iter(CORE_SECTIONS)
        first, second = next(core_iter), next(core_iter)
        overrides = {first: ("UNAVAILABLE", None), second: ("UNAVAILABLE", None)}
        sections = _all_family_sections("AVAILABLE", overrides)
        assert compute_research_completeness(sections) == "LIMITED"

    def test_capability_gap_reason_code_excluded_from_scoring(self):
        """A permanent V1 capability boundary (no upstream data model at all) must not
        make HIGH permanently unreachable - it is not a genuine evidence-coverage gap
        for this company."""
        core_iter = iter(CORE_SECTIONS)
        first, second = next(core_iter), next(core_iter)
        overrides = {
            first: ("UNAVAILABLE", "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED"),
            second: ("INSUFFICIENT_INPUT", "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED"),
        }
        sections = _all_family_sections("AVAILABLE", overrides)
        assert compute_research_completeness(sections) == "HIGH"

    def test_narrator_candidate_schema_has_no_completeness_field(self):
        from stock_research_ai.domain.report import NarrativeBlockCandidate

        assert "research_completeness" not in NarrativeBlockCandidate.model_fields

    def test_narrator_cannot_smuggle_a_completeness_value(self):
        from stock_research_ai.domain.report import NarrativeBlockCandidate

        with pytest.raises(ValidationError):
            NarrativeBlockCandidate.model_validate(
                {"section": "VALUATION", "text": "x", "research_completeness": "HIGH"}
            )
