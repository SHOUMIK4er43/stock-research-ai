"""Deterministic independent-challenger checks over a frozen Part 5B AnalysisSnapshot.

Closed-evidence V1: evidence is re-resolved via the exact same pinned AnalysisRequest
already embedded in the reviewed snapshot - never a fresh discovery, never a "latest"
substitution. Every check either re-derives a value Part 5B itself computed at claim
creation (to catch persisted-data tampering or storage corruption between then and now) or
applies a genuinely new lexical/structural rule Part 5B's own validator never ran.
"""

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from stock_research_ai.application.analysis import (
    Calculation,
    ResolvedEvidence,
    _reference_status,
    build_source_references,
    resolve_evidence,
)
from stock_research_ai.application.evidence_matching import (
    BANK_INDUSTRIAL_TERMS,
    FORBIDDEN_BASE_LIKELIHOOD,
    FORBIDDEN_CERTAINTY,
    FORBIDDEN_RECOMMENDATION,
    _has_conflicting_evidence,
    canonical_values,
    citation_period_scope,
    extract_numeric_tokens,
    find_reference,
    limitation_codes,
    source_epistemic_class,
)
from stock_research_ai.application.hashing import digest
from stock_research_ai.application.ports import (
    CalculationRepository,
    ContextRepository,
    DocumentRepository,
    ExtractionRepository,
    MarketCalculationRepository,
    MarketRepository,
    PatternRepository,
    RawArtifactStore,
    ValuationRepository,
)
from stock_research_ai.domain.analysis import (
    POINT_IN_TIME_ISSUE_CODE,
    AnalysisSnapshot,
    AnalystClaim,
    EvidenceReference,
    ModelMetadata,
)
from stock_research_ai.domain.calculations import CalculationResult
from stock_research_ai.domain.challenge import (
    SEVERITY_BY_TYPE,
    AlternativeInterpretation,
    ChallengeFinding,
    ChallengerInputBundle,
    ChallengeSnapshot,
    ClaimReview,
    IndependenceBasis,
    RelevanceRule,
    SectionReview,
    aggregate_omission_status,
    aggregate_section_outcome,
    compute_claim_outcome,
)
from stock_research_ai.domain.company import Company


class ChallengeIntegrityError(ValueError):
    """Raised when closed-evidence re-resolution or transaction-scoped persistence
    integrity checks fail - distinct from a plain malformed-request ValueError so the CLI
    can map it to its own, more severe, exit code."""


RELEVANCE_REGISTRY_VERSION = "5C.1-v1"

RELEVANCE_REGISTRY: tuple[RelevanceRule, ...] = (
    RelevanceRule(
        rule_id="IT_PROFIT_GROWTH_VS_MARGIN_DETERIORATION",
        section="SECTOR_KPIS",
        sector_profile="IT_SERVICES_PROFILE",
        primary_metric_family=("NET_PROFIT_GROWTH", "SALES_GROWTH"),
        related_metric_family=("OPERATING_MARGIN_DETERIORATION",),
        relationship_type="LINKED_FAMILY_TENSION",
        period_compatibility="ANY_PERIOD",
        scope_compatibility="ANY_SCOPE",
        finding_type="ANALYTICAL_TENSION",
    ),
    RelevanceRule(
        rule_id="BANK_DEPOSIT_GROWTH_VS_DECELERATION",
        section="SECTOR_KPIS",
        sector_profile="BANK_PROFILE",
        primary_metric_family=("DEPOSIT_GROWTH",),
        related_metric_family=("DEPOSIT_GROWTH_DECELERATION",),
        relationship_type="LINKED_FAMILY_TENSION",
        period_compatibility="ANY_PERIOD",
        scope_compatibility="ANY_SCOPE",
        finding_type="ANALYTICAL_TENSION",
    ),
)

CAUSAL_CONNECTIVE = re.compile(
    r"\b(because|due to|driven by|led to|caused|resulted in)\b", re.IGNORECASE
)
EVALUATIVE_ADJECTIVE = re.compile(
    r"\b(weak|strong|poor|robust|deteriorat\w*|improv\w*|healthy|concerning)\b", re.IGNORECASE
)
MISCONDUCT_LANGUAGE = re.compile(
    r"\b(fraud\w*|manipulat\w*|misconduct|misstat\w*|cook(?:ed|ing)? the books|embezzl\w*)\b",
    re.IGNORECASE,
)
_ONE_SIDED_QUALIFIERS = ("however", "but ", "although", "on the other hand", "context")


def _metric_family(ref: EvidenceReference, evidence: ResolvedEvidence) -> str | None:
    """The stable metric/pattern identity a reference belongs to, for omission matching."""
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        c: Calculation = evidence.calculations[ref.owning_snapshot_id]
        for calc_result in c.results:
            metric_id = (
                calc_result.metric_id
                if isinstance(calc_result, CalculationResult)
                else calc_result.definition.metric_id
            )
            if metric_id == ref.item_key:
                return metric_id
    if ref.kind == "PATTERN":
        p = evidence.patterns[ref.owning_snapshot_id]
        for finding in p.findings:
            if str(finding.id) == ref.item_key:
                return finding.definition.pattern_id
    return None


def _applicable_profiles(
    ref: EvidenceReference, evidence: ResolvedEvidence
) -> tuple[str, ...] | None:
    """Structural sector applicability, re-derived from the definition object itself -
    independent of whatever SupportAssessment/sector_profile the claim already carries."""
    if ref.kind == "SECTOR_KPI":
        c = evidence.calculations[ref.owning_snapshot_id]
        for calc_result in c.results:
            if not isinstance(calc_result, CalculationResult) and (
                calc_result.definition.metric_id == ref.item_key
            ):
                return calc_result.definition.applicable_profiles
    if ref.kind == "PATTERN":
        p = evidence.patterns[ref.owning_snapshot_id]
        for finding in p.findings:
            if str(finding.id) == ref.item_key:
                return finding.definition.applicable_profiles
    return None


async def build_challenger_bundle(
    reviewed: AnalysisSnapshot,
    company: Company,
    *,
    extractions: ExtractionRepository,
    market: MarketRepository,
    calculations: CalculationRepository,
    patterns: PatternRepository,
    market_calculations: MarketCalculationRepository,
    valuations: ValuationRepository,
    documents: DocumentRepository,
    context: ContextRepository,
    artifacts: RawArtifactStore,
) -> tuple[ChallengerInputBundle, ResolvedEvidence]:
    """Re-resolve evidence via the SAME pinned request the reviewed snapshot itself used,
    then independently re-read every direct and transitive raw artifact byte-for-byte -
    mirroring analyze_company's own safeguard exactly, since JSON-payload hash comparison
    alone never touches raw bytes on disk. A resolution or byte-read failure is an
    integrity failure, not a malformed request - it is re-raised as ChallengeIntegrityError."""
    from stock_research_ai.application.analysis import (
        direct_raw_artifacts,
        transitive_raw_artifacts,
    )

    try:
        evidence = await resolve_evidence(
            reviewed.request,
            company,
            extractions=extractions,
            market=market,
            calculations=calculations,
            patterns=patterns,
            market_calculations=market_calculations,
            valuations=valuations,
            documents=documents,
            context=context,
        )
        raw_artifacts = {
            a.id: a
            for a in (
                *direct_raw_artifacts(evidence),
                *await transitive_raw_artifacts(
                    evidence, extractions=extractions, market=market, calculations=calculations
                ),
            )
        }
        for artifact in raw_artifacts.values():
            await artifacts.read(artifact)
    except ValueError as exc:
        raise ChallengeIntegrityError(str(exc)) from exc
    bundle = ChallengerInputBundle(
        bundle_id=uuid4(),
        reviewed_analysis=reviewed,
        analysis_snapshot_sha256=digest(reviewed),
        assembled_at=datetime.now(UTC),
    )
    return bundle, evidence


def _omission_review(
    claim: AnalystClaim,
    resolved_fresh: list[EvidenceReference],
    fresh_sources: tuple[EvidenceReference, ...],
    evidence: ResolvedEvidence,
    registry: tuple[RelevanceRule, ...],
) -> tuple[str, tuple[ChallengeFinding, ...], tuple[EvidenceReference, ...]]:
    applicable = [
        rule
        for rule in registry
        if rule.section == claim.section
        and (rule.sector_profile is None or rule.sector_profile == claim.sector_profile)
    ]
    if not applicable:
        return "OMISSION_REVIEW_NOT_COVERED_BY_V1_RULES", (), ()
    cited_keys = {(r.kind, r.owning_snapshot_id, r.item_key) for r in resolved_fresh}
    cited_families = {
        family for family in (_metric_family(r, evidence) for r in resolved_fresh) if family
    }
    findings: list[ChallengeFinding] = []
    extra: list[EvidenceReference] = []
    material = False
    for rule in applicable:
        if not cited_families & set(rule.primary_metric_family):
            continue
        related = [
            ref
            for ref in fresh_sources
            if _metric_family(ref, evidence) in rule.related_metric_family
            and _reference_status(ref, evidence) == "AVAILABLE"
            and (ref.kind, ref.owning_snapshot_id, ref.item_key) not in cited_keys
        ]
        if related:
            material = True
            findings.append(
                ChallengeFinding(
                    finding_id=uuid4(),
                    finding_type=rule.finding_type,
                    severity=SEVERITY_BY_TYPE[rule.finding_type],
                    statement=(
                        f"Relevance rule {rule.rule_id}: related evidence is pinned in the "
                        "bundle but not cited by this claim."
                    ),
                    cited_evidence=tuple(related),
                    source="DETERMINISTIC",
                )
            )
            extra.extend(related)
    if material:
        return "MATERIAL_OMISSION_FOUND", tuple(findings), tuple(extra)
    return "NO_RELEVANT_OMISSION_FOUND", (), ()


def review_claim(
    claim: AnalystClaim,
    fresh_sources: tuple[EvidenceReference, ...],
    evidence: ResolvedEvidence,
    company_id: UUID,
    registry: tuple[RelevanceRule, ...] = RELEVANCE_REGISTRY,
) -> ClaimReview:
    findings: list[ChallengeFinding] = []
    reviewed_citations: list[EvidenceReference] = []
    resolved_fresh: list[EvidenceReference] = []

    def add(finding_type: str, statement: str, cited: tuple[EvidenceReference, ...] = ()) -> None:
        findings.append(
            ChallengeFinding(
                finding_id=uuid4(),
                finding_type=finding_type,  # type: ignore[arg-type]
                severity=SEVERITY_BY_TYPE[finding_type],  # type: ignore[index]
                statement=statement,
                cited_evidence=cited,
                source="DETERMINISTIC",
            )
        )

    for citation, declared_class in zip(
        claim.citations, claim.source_epistemic_classes, strict=True
    ):
        fresh_ref = find_reference(
            citation.kind, citation.owning_snapshot_id, citation.item_key, fresh_sources
        )
        if fresh_ref is None:
            add(
                "CITATION_NOT_FOUND",
                f"Cited evidence {citation.kind}:{citation.item_key} no longer resolves "
                "against the pinned request.",
                (citation,),
            )
            continue
        reviewed_citations.append(fresh_ref)
        if fresh_ref.snapshot_payload_sha256 != citation.snapshot_payload_sha256:
            add(
                "CITATION_HASH_MISMATCH",
                f"Cited evidence {citation.kind}:{citation.item_key} hash no longer matches "
                "the persisted claim's citation.",
                (fresh_ref,),
            )
            continue
        resolved_fresh.append(fresh_ref)
        fresh_class = source_epistemic_class(fresh_ref, evidence)
        if fresh_class == "SCENARIO" and declared_class == "FACT":
            add(
                "ASSUMPTION_PROMOTED_TO_FACT",
                "Citation freshly re-resolves as SCENARIO evidence but was recorded as FACT.",
                (fresh_ref,),
            )

    if evidence.financial is not None and evidence.financial.company is not None:
        financial_refs = tuple(r for r in resolved_fresh if r.kind in ("FINANCIAL", "SHAREHOLDING"))
        if financial_refs and evidence.financial.company.company_id != company_id:
            add(
                "WRONG_COMPANY_EVIDENCE",
                "Cited financial evidence belongs to a different company than the reviewed "
                "analysis.",
                financial_refs,
            )

    for token in extract_numeric_tokens(claim.statement):
        same_value_any_family = False
        same_value_same_family = False
        for ref in resolved_fresh:
            for value, family in canonical_values(ref, evidence):
                if token.value == value:
                    same_value_any_family = True
                    if token.family == family:
                        same_value_same_family = True
        if resolved_fresh and not same_value_same_family:
            if same_value_any_family:
                add(
                    "UNIT_MISMATCH",
                    f"Numeric value {token.value} is only supported by cited evidence under "
                    "a different unit family.",
                    tuple(resolved_fresh),
                )
            else:
                add(
                    "NUMERIC_MISMATCH",
                    f"Numeric value {token.value} ({token.family}) is not supported by any "
                    "freshly re-resolved cited evidence.",
                    tuple(resolved_fresh),
                )

    periods = {citation_period_scope(r, evidence)[0] for r in resolved_fresh} - {None}
    scopes = {citation_period_scope(r, evidence)[1] for r in resolved_fresh} - {None}
    if len(periods) > 1:
        add(
            "INCOMPATIBLE_PERIOD",
            "Freshly re-resolved cited evidence spans more than one observation period.",
            tuple(resolved_fresh),
        )
    if len(scopes) > 1:
        add(
            "INCOMPATIBLE_SCOPE",
            "Freshly re-resolved cited evidence spans more than one reporting scope.",
            tuple(resolved_fresh),
        )

    if _has_conflicting_evidence(list(resolved_fresh), evidence):
        add(
            "DETERMINISTIC_CONFLICT",
            "Freshly re-resolved cited evidence for the same metric/period/scope disagrees "
            "in value.",
            tuple(resolved_fresh),
        )

    if CAUSAL_CONNECTIVE.search(claim.statement) and claim.management_statement is None:
        add(
            "UNSUPPORTED_CAUSALITY",
            "Statement asserts a causal relationship without a verified management quote "
            "reporting management's own explanation.",
            tuple(resolved_fresh),
        )

    if FORBIDDEN_RECOMMENDATION.search(claim.statement):
        add(
            "FORBIDDEN_RECOMMENDATION_LANGUAGE",
            "Statement contains recommendation or target-price language.",
        )
    if FORBIDDEN_CERTAINTY.search(claim.statement):
        add("UNSUPPORTED_CERTAINTY", "Statement asserts unwarranted certainty.")
    if FORBIDDEN_BASE_LIKELIHOOD.search(claim.statement):
        add(
            "ASSUMPTION_PROMOTED_TO_FACT",
            "Statement attaches likelihood language to a scenario label, treating an "
            "assumption as a factual probability.",
        )

    if claim.sector_profile is not None:
        for ref in resolved_fresh:
            profiles = _applicable_profiles(ref, evidence)
            if profiles is not None and claim.sector_profile not in profiles:
                add(
                    "SECTOR_POLICY_VIOLATION",
                    f"Cited evidence is not structurally applicable to sector profile "
                    f"{claim.sector_profile}.",
                    (ref,),
                )
        if claim.sector_profile == "BANK_PROFILE" and BANK_INDUSTRIAL_TERMS.search(
            claim.statement
        ):
            add(
                "SECTOR_LANGUAGE_CONCERN",
                "Statement uses industrial-company terminology under a bank sector profile.",
            )

    if EVALUATIVE_ADJECTIVE.search(claim.statement):
        unavailable = tuple(
            r for r in resolved_fresh if _reference_status(r, evidence) != "AVAILABLE"
        )
        if unavailable:
            add(
                "EVALUATIVE_CLAIM_ON_UNAVAILABLE_EVIDENCE",
                "Statement makes an evaluative judgment about evidence that does not "
                "freshly resolve as AVAILABLE.",
                unavailable,
            )

    if MISCONDUCT_LANGUAGE.search(claim.statement):
        pattern_refs = tuple(r for r in resolved_fresh if r.kind == "PATTERN")
        if pattern_refs:
            add(
                "MISCONDUCT_LANGUAGE_UNSUPPORTED",
                "Statement uses misconduct/fraud language beyond a quantitative pattern's "
                "own interpretation boundary.",
                pattern_refs,
            )

    if claim.management_statement is not None:
        chunk = claim.management_statement.chunk
        fresh_ref = find_reference(
            "DOCUMENT_CHUNK", chunk.owning_snapshot_id, chunk.item_key, fresh_sources
        )
        doc = evidence.documents.get(chunk.owning_snapshot_id)
        chunk_text = None
        if doc is not None:
            for parsed_chunk in doc.chunks:
                if str(parsed_chunk.id) == chunk.item_key:
                    chunk_text = parsed_chunk.text
        if fresh_ref is None or chunk_text is None or (
            claim.management_statement.text not in chunk_text
        ):
            add(
                "MANAGEMENT_QUOTE_ALTERED",
                "Management quote is no longer an exact substring of the freshly re-fetched "
                "document chunk.",
                (chunk,),
            )
        elif not any(q in claim.statement.lower() for q in _ONE_SIDED_QUALIFIERS):
            add(
                "ONE_SIDED_INTERPRETATION",
                "A management quote is presented without any offsetting qualifier.",
                (chunk,),
            )

    if claim.section == "VARIANT_PERCEPTION":
        for ref in resolved_fresh:
            if ref.kind != "VALUATION":
                continue
            v = evidence.valuations[ref.owning_snapshot_id]
            for selection, val_result in zip(v.request.selections, v.results, strict=True):
                if str(val_result.id) == ref.item_key and selection.scenario is not None:
                    scenario_ids = {str(a.assumption_id) for a in selection.scenario.assumptions}
                    declared = {str(a) for a in claim.dependency_assumption_ids}
                    if not declared or not declared.issubset(scenario_ids):
                        add(
                            "SCENARIO_ASSUMPTION_INVALID",
                            "Declared dependency assumptions no longer resolve exactly to "
                            "the cited scenario's own assumptions.",
                            (ref,),
                        )

    point_in_time_unverified = any(
        POINT_IN_TIME_ISSUE_CODE in limitation_codes(r, evidence) for r in resolved_fresh
    )
    if point_in_time_unverified and POINT_IN_TIME_ISSUE_CODE not in claim.limitations:
        add(
            "POINT_IN_TIME_LIMITATION_OMITTED",
            "Cited evidence carries an unverified point-in-time-availability limitation "
            "that the persisted claim does not declare.",
            tuple(resolved_fresh),
        )

    omission_status, omission_findings, extra_evidence = _omission_review(
        claim, resolved_fresh, fresh_sources, evidence, registry
    )
    findings.extend(omission_findings)

    return ClaimReview(
        review_id=uuid4(),
        analyst_claim_id=claim.claim_id,
        outcome=compute_claim_outcome(tuple(findings)),
        omission_review_status=omission_status,  # type: ignore[arg-type]
        findings=tuple(findings),
        reviewed_citations=tuple(reviewed_citations),
        additional_relevant_evidence=extra_evidence,
        alternative_interpretations=(),
        unresolved_questions=(),
    )


def compute_independence_basis(
    reviewed_metadata: ModelMetadata, challenger_metadata: ModelMetadata | None
) -> IndependenceBasis:
    if challenger_metadata is None:
        return "DETERMINISTIC_ONLY"
    if challenger_metadata.provider != reviewed_metadata.provider:
        return "SEPARATE_MODEL"
    if challenger_metadata.model_id != reviewed_metadata.model_id:
        return "SAME_PROVIDER_DIFFERENT_MODEL"
    if challenger_metadata.request.template.template_id != (
        reviewed_metadata.request.template.template_id
    ):
        return "SAME_MODEL_SEPARATE_PROMPT"
    return "SAME_MODEL_SAME_PROMPT"


def _build_section_reviews(
    reviewed: AnalysisSnapshot, claim_reviews: tuple[ClaimReview, ...]
) -> tuple[SectionReview, ...]:
    reviews_by_claim = {r.analyst_claim_id: r for r in claim_reviews}
    coverage_by_section = {c.section: c.status for c in reviewed.section_coverage}
    result: list[SectionReview] = []
    for section in sorted(reviewed.request.sections):
        claims_in_section = [c for c in reviewed.claims if c.section == section]
        reviews = [reviews_by_claim[c.claim_id] for c in claims_in_section]
        blocking = sum(1 for r in reviews for f in r.findings if f.severity == "BLOCKING")
        non_blocking = sum(1 for r in reviews for f in r.findings if f.severity == "NON_BLOCKING")
        informational = sum(
            1 for r in reviews for f in r.findings if f.severity == "INFORMATIONAL"
        )
        uncited = tuple(e for r in reviews for e in r.additional_relevant_evidence)
        result.append(
            SectionReview(
                section=section,
                coverage_status=coverage_by_section.get(section, "UNAVAILABLE"),
                claim_count=len(claims_in_section),
                reviewed_claim_count=len(reviews),
                blocking_findings=blocking,
                non_blocking_findings=non_blocking,
                informational_findings=informational,
                uncited_relevant_evidence=uncited,
                omission_review_coverage=aggregate_omission_status(
                    tuple(r.omission_review_status for r in reviews)
                ),
                section_review_outcome=aggregate_section_outcome(tuple(r.outcome for r in reviews)),
            )
        )
    return tuple(result)


def review_snapshot(
    bundle: ChallengerInputBundle,
    evidence: ResolvedEvidence,
    *,
    registry: tuple[RelevanceRule, ...] = RELEVANCE_REGISTRY,
    challenger_metadata: ModelMetadata | None = None,
    validated_model_findings: dict[UUID, tuple[ChallengeFinding, ...]] | None = None,
    validated_alternatives: dict[UUID, tuple[AlternativeInterpretation, ...]] | None = None,
    rejected_candidates: tuple[object, ...] = (),
) -> ChallengeSnapshot:
    reviewed = bundle.reviewed_analysis
    fresh_sources = build_source_references(reviewed.request, evidence)
    findings_by_claim = validated_model_findings or {}
    alts_by_claim = validated_alternatives or {}
    claim_reviews: list[ClaimReview] = []
    for claim in reviewed.claims:
        base = review_claim(claim, fresh_sources, evidence, reviewed.company_id, registry)
        extra_findings = findings_by_claim.get(claim.claim_id, ())
        extra_alts = alts_by_claim.get(claim.claim_id, ())
        if extra_findings or extra_alts:
            findings = base.findings + extra_findings
            base = ClaimReview(
                review_id=base.review_id,
                analyst_claim_id=base.analyst_claim_id,
                outcome=compute_claim_outcome(findings),
                omission_review_status=base.omission_review_status,
                findings=findings,
                reviewed_citations=base.reviewed_citations,
                additional_relevant_evidence=base.additional_relevant_evidence,
                alternative_interpretations=base.alternative_interpretations + extra_alts,
                unresolved_questions=base.unresolved_questions,
            )
        claim_reviews.append(base)
    frozen_reviews = tuple(claim_reviews)
    return ChallengeSnapshot(
        id=uuid4(),
        analysis_snapshot_id=reviewed.id,
        analysis_snapshot_sha256=bundle.analysis_snapshot_sha256,
        challenger_bundle=bundle,
        challenger_bundle_sha256=digest(bundle),
        relevance_registry_version=RELEVANCE_REGISTRY_VERSION,
        claim_reviews=frozen_reviews,
        section_reviews=_build_section_reviews(reviewed, frozen_reviews),
        independence_basis=compute_independence_basis(reviewed.model_metadata, challenger_metadata),
        challenger_model_metadata=challenger_metadata,
        rejected_candidates=tuple(rejected_candidates),  # type: ignore[arg-type]
        evaluated_at=datetime.now(UTC),
    )


__all__ = [
    "RELEVANCE_REGISTRY",
    "RELEVANCE_REGISTRY_VERSION",
    "ChallengeIntegrityError",
    "build_challenger_bundle",
    "compute_independence_basis",
    "review_claim",
    "review_snapshot",
]
