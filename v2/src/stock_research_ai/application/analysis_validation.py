"""Deterministic acceptance/rejection of AnalystModel output. Nothing here does arithmetic;
every check either resolves a citation back into the bundle or performs a lexical scan.

Pure evidence-matching primitives now live in application/evidence_matching.py, shared with
Part 5C's independent challenger; they are imported back here unchanged.
"""

from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from stock_research_ai.application.analysis import ResolvedEvidence
from stock_research_ai.application.analysis import _reference_status as reference_status
from stock_research_ai.application.evidence_matching import (
    BANK_INDUSTRIAL_TERMS,
    FORBIDDEN_BASE_LIKELIHOOD,
    FORBIDDEN_CERTAINTY,
    FORBIDDEN_HISTORICAL_KNOWLEDGE,
    FORBIDDEN_RECOMMENDATION,
    _has_conflicting_evidence,
    canonical_values,
    citation_period_scope,
    extract_numeric_tokens,
    find_reference,
    is_unverified_semantic,
    limitation_codes,
    source_epistemic_class,
)
from stock_research_ai.domain.analysis import (
    AS_OF_POLICY,
    POINT_IN_TIME_ISSUE_CODE,
    AnalystClaim,
    AnalystClaimCandidate,
    AnstInputBundle,
    EvidenceReference,
    ManagementStatement,
    RawAnalystResponse,
    RejectedClaim,
    SupportAssessment,
    TimeScope,
    ValidationIssue,
)


def validate_candidate(
    raw_candidate: dict[str, object],
    bundle: AnstInputBundle,
    evidence: ResolvedEvidence,
    sector_profile: str | None,
) -> tuple[AnalystClaim | None, RejectedClaim | None]:
    issues: list[ValidationIssue] = []
    try:
        candidate = AnalystClaimCandidate.model_validate(raw_candidate)
    except ValidationError as exc:
        return None, RejectedClaim(
            raw_candidate=raw_candidate,
            issues=(ValidationIssue(code="MALFORMED_RESPONSE", message=str(exc)),),
        )

    if candidate.section not in bundle.request.sections:
        issues.append(ValidationIssue(code="SECTION_NOT_REQUESTED", message=candidate.section))

    resolved: list[EvidenceReference] = []
    for citation in candidate.citations:
        ref = find_reference(citation.kind, citation.owning_snapshot_id, citation.item_key,
                              bundle.sources)
        if ref is None:
            issues.append(
                ValidationIssue(
                    code="CITATION_NOT_IN_BUNDLE",
                    message=f"{citation.kind}:{citation.owning_snapshot_id}:{citation.item_key}",
                )
            )
        else:
            resolved.append(ref)

    if candidate.management_quote is not None:
        mq_ref = find_reference(
            "DOCUMENT_CHUNK",
            candidate.management_quote.chunk.owning_snapshot_id,
            candidate.management_quote.chunk.item_key,
            bundle.sources,
        )
        if mq_ref is None:
            issues.append(
                ValidationIssue(
                    code="MANAGEMENT_QUOTE_CITATION_NOT_IN_BUNDLE",
                    message="Management quote chunk citation is not in the pinned bundle",
                )
            )
        else:
            version_id = mq_ref.owning_snapshot_id
            doc = evidence.documents.get(version_id)
            chunk_text = None
            if doc is not None:
                for chunk in doc.chunks:
                    if str(chunk.id) == mq_ref.item_key:
                        chunk_text = chunk.text
            if chunk_text is None or candidate.management_quote.text not in chunk_text:
                issues.append(
                    ValidationIssue(
                        code="MANAGEMENT_QUOTE_NOT_EXACT_SUBSTRING",
                        message="Quoted text is not an exact substring of the cited chunk",
                    )
                )

    if issues:
        return None, RejectedClaim(raw_candidate=raw_candidate, issues=tuple(issues))

    for token in extract_numeric_tokens(candidate.statement):
        supported = any(
            token.value == value and token.family == family
            for ref in resolved
            for value, family in canonical_values(ref, evidence)
        )
        if not supported:
            issues.append(
                ValidationIssue(
                    code="UNSUPPORTED_NUMERIC_CLAIM",
                    message=f"{token.value} ({token.family}) not found in cited evidence",
                )
            )

    if FORBIDDEN_RECOMMENDATION.search(candidate.statement):
        issues.append(ValidationIssue(
            code="FORBIDDEN_RECOMMENDATION_LANGUAGE",
            message="Statement contains forbidden recommendation language",
        ))
    if FORBIDDEN_CERTAINTY.search(candidate.statement):
        issues.append(ValidationIssue(
            code="FORBIDDEN_CERTAINTY_LANGUAGE",
            message="Statement contains forbidden certainty language",
        ))
    if sector_profile == "BANK_PROFILE" and BANK_INDUSTRIAL_TERMS.search(candidate.statement):
        issues.append(ValidationIssue(
            code="SECTOR_LANGUAGE_VIOLATION",
            message="Statement uses industrial-company language not applicable to this profile",
        ))

    point_in_time_unverified = any(
        POINT_IN_TIME_ISSUE_CODE in limitation_codes(ref, evidence) for ref in resolved
    )
    if point_in_time_unverified and FORBIDDEN_HISTORICAL_KNOWLEDGE.search(candidate.statement):
        issues.append(ValidationIssue(
            code="UNVERIFIED_HISTORICAL_KNOWLEDGE_CLAIM",
            message="Statement implies historical knowledge that is not independently verified",
        ))

    if candidate.section == "BULL_BASE_BEAR_INTERPRETATION":
        for ref in resolved:
            if ref.kind != "VALUATION":
                continue
            v = evidence.valuations[ref.owning_snapshot_id]
            for selection, val_result in zip(v.request.selections, v.results, strict=True):
                if str(val_result.id) == ref.item_key and selection.scenario is not None:
                    if selection.scenario.label.strip().upper() == "BASE" and (
                        FORBIDDEN_BASE_LIKELIHOOD.search(candidate.statement)
                    ):
                        issues.append(
                            ValidationIssue(
                            code="BASE_LABEL_LIKELIHOOD_LANGUAGE",
                            message="BASE is a scenario label only; it must not imply likelihood",
                        )
                        )

    if candidate.section == "VARIANT_PERCEPTION":
        scenario_assumption_ids: set[str] = set()
        for ref in resolved:
            if ref.kind != "VALUATION":
                continue
            v = evidence.valuations[ref.owning_snapshot_id]
            for val_result, selection in zip(v.results, v.request.selections, strict=True):
                if str(val_result.id) == ref.item_key and selection.scenario is not None:
                    scenario_assumption_ids |= {
                        str(a.assumption_id) for a in selection.scenario.assumptions
                    }
        declared = {str(a) for a in candidate.dependency_assumption_ids}
        if not declared or not declared.issubset(scenario_assumption_ids):
            issues.append(
                ValidationIssue(
                    code="VARIANT_PERCEPTION_ASSUMPTION_LINEAGE_INVALID",
                    message="dependency_assumption_ids must resolve exactly to the cited scenario",
                )
            )

    if issues:
        return None, RejectedClaim(raw_candidate=raw_candidate, issues=tuple(issues))

    periods = {citation_period_scope(r, evidence)[0] for r in resolved} - {None}
    scopes = {citation_period_scope(r, evidence)[1] for r in resolved} - {None}
    time_scope = TimeScope(
        observation_period=next(iter(periods)) if len(periods) == 1 else None,
        reporting_scope=next(iter(scopes)) if len(scopes) == 1 else None,
        evaluated_at=bundle.assembled_at,
        as_of_policy=AS_OF_POLICY,
        point_in_time_unverified=point_in_time_unverified,
    )
    limitations = tuple(
        sorted({code for ref in resolved for code in limitation_codes(ref, evidence)})
    )
    source_classes = tuple(source_epistemic_class(r, evidence) for r in resolved)
    distinct_kinds = len({r.kind for r in resolved})
    unverified = any(is_unverified_semantic(r, evidence) for r in resolved)
    all_non_available = bool(resolved) and all(
        reference_status(r, evidence) not in ("AVAILABLE",) for r in resolved
    )
    conflicting = _has_conflicting_evidence(resolved, evidence)
    if conflicting:
        basis: Literal[
            "SINGLE_SOURCE", "CORROBORATED_MULTI_SOURCE", "CONFLICTING_EVIDENCE",
            "INSUFFICIENT_SUPPORT",
        ] = "CONFLICTING_EVIDENCE"
    elif all_non_available:
        basis = "INSUFFICIENT_SUPPORT"
    elif distinct_kinds > 1:
        basis = "CORROBORATED_MULTI_SOURCE"
    else:
        basis = "SINGLE_SOURCE"
    support = SupportAssessment(
        basis=basis,
        distinct_source_kinds=distinct_kinds,
        cites_unverified_semantics=unverified,
        cites_limitation_issue=bool(limitations),
    )
    management_statement = None
    if candidate.management_quote is not None:
        mq_ref = find_reference(
            "DOCUMENT_CHUNK",
            candidate.management_quote.chunk.owning_snapshot_id,
            candidate.management_quote.chunk.item_key,
            bundle.sources,
        )
        assert mq_ref is not None
        management_statement = ManagementStatement(
            statement_id=uuid4(), chunk=mq_ref, text=candidate.management_quote.text
        )

    claim = AnalystClaim(
        claim_id=uuid4(),
        section=candidate.section,
        statement=candidate.statement,
        materiality=candidate.materiality,
        citations=tuple(resolved),
        source_epistemic_classes=source_classes,
        support=support,
        time_scope=time_scope,
        sector_profile=sector_profile,
        limitations=limitations,
        management_statement=management_statement,
        dependency_assumption_ids=candidate.dependency_assumption_ids,
    )
    return claim, None


def validate_response(
    raw_response: RawAnalystResponse,
    bundle: AnstInputBundle,
    evidence: ResolvedEvidence,
    sector_profile: str | None,
) -> tuple[tuple[AnalystClaim, ...], tuple[RejectedClaim, ...]]:
    raw_claims = raw_response.structured_payload.get("claims", [])
    if not isinstance(raw_claims, list):
        return (), (
            RejectedClaim(
                raw_candidate={"claims": raw_claims},
                issues=(
                    ValidationIssue(code="MALFORMED_RESPONSE", message="claims must be a list"),
                ),
            ),
        )
    accepted: list[AnalystClaim] = []
    rejected: list[RejectedClaim] = []
    seen: set[tuple[str, str, tuple[tuple[str, str, str | None], ...]]] = set()
    for raw_candidate in raw_claims:
        if not isinstance(raw_candidate, dict):
            rejected.append(
                RejectedClaim(
                    raw_candidate={"value": raw_candidate},
                    issues=(ValidationIssue(code="MALFORMED_RESPONSE", message="not an object"),),
                )
            )
            continue
        claim, rejection = validate_candidate(raw_candidate, bundle, evidence, sector_profile)
        if claim is not None:
            key = (
                claim.section,
                claim.statement.strip().casefold(),
                tuple(
                    sorted((c.kind, str(c.owning_snapshot_id), c.item_key) for c in claim.citations)
                ),
            )
            if key in seen:
                rejected.append(
                    RejectedClaim(
                        raw_candidate=raw_candidate,
                        issues=(
                            ValidationIssue(
                                code="DUPLICATE_CLAIM",
                                message="Identical section/statement/citations already accepted",
                            ),
                        ),
                    )
                )
                continue
            seen.add(key)
            accepted.append(claim)
        elif rejection is not None:
            rejected.append(rejection)
    return tuple(accepted), tuple(rejected)


__all__ = [
    "canonical_values",
    "citation_period_scope",
    "extract_numeric_tokens",
    "find_reference",
    "limitation_codes",
    "source_epistemic_class",
    "validate_candidate",
    "validate_response",
]
