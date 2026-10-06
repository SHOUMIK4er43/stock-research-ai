"""Deterministic acceptance/rejection of ChallengerModel output.

A ChallengerFindingCandidate carries only an untrusted `suggestion_kind`; this module is
the ONLY place that maps an accepted suggestion into a persisted, model-originated
ChallengeFinding.finding_type (or an AlternativeInterpretation). The model can never set
finding_type, severity, claim outcome, citation validity, or integrity status directly.
"""

from uuid import UUID, uuid4

from pydantic import ValidationError

from stock_research_ai.application.evidence_matching import (
    FORBIDDEN_CERTAINTY,
    FORBIDDEN_RECOMMENDATION,
    find_reference,
)
from stock_research_ai.domain.analysis import EvidenceReference, RawAnalystResponse, ValidationIssue
from stock_research_ai.domain.challenge import (
    SEVERITY_BY_TYPE,
    AlternativeInterpretation,
    ChallengeFinding,
    ChallengerFindingCandidate,
    ChallengerInputBundle,
    RejectedChallengerCandidate,
)

_SUGGESTION_FINDING_TYPE: dict[str, str] = {
    "MATERIAL_EVIDENCE": "MODEL_SUGGESTED_MATERIAL_EVIDENCE",
    "ANALYTICAL_TENSION": "MODEL_SUGGESTED_ANALYTICAL_TENSION",
    "LANGUAGE_CONCERN": "MODEL_SUGGESTED_LANGUAGE_CONCERN",
}


_ValidatedCandidate = tuple[
    ChallengeFinding | None, AlternativeInterpretation | None, RejectedChallengerCandidate | None
]


def validate_challenger_candidate(
    raw_candidate: dict[str, object],
    bundle: ChallengerInputBundle,
    claim_ids: set[UUID],
) -> _ValidatedCandidate:
    issues: list[ValidationIssue] = []
    try:
        candidate = ChallengerFindingCandidate.model_validate(raw_candidate)
    except ValidationError as exc:
        return None, None, RejectedChallengerCandidate(
            raw_candidate=raw_candidate,
            issues=(ValidationIssue(code="MALFORMED_RESPONSE", message=str(exc)),),
        )

    if candidate.analyst_claim_id not in claim_ids:
        issues.append(
            ValidationIssue(
                code="UNKNOWN_CLAIM",
                message=f"analyst_claim_id {candidate.analyst_claim_id} is not in the reviewed "
                "snapshot",
            )
        )

    resolved: list[EvidenceReference] = []
    sources = bundle.reviewed_analysis.input_bundle.sources
    for citation in candidate.cited_evidence:
        ref = find_reference(citation.kind, citation.owning_snapshot_id, citation.item_key, sources)
        if ref is None:
            issues.append(
                ValidationIssue(
                    code="CITATION_NOT_IN_BUNDLE",
                    message=f"{citation.kind}:{citation.owning_snapshot_id}:{citation.item_key}",
                )
            )
        else:
            resolved.append(ref)

    if FORBIDDEN_RECOMMENDATION.search(candidate.statement):
        issues.append(
            ValidationIssue(
                code="FORBIDDEN_RECOMMENDATION_LANGUAGE",
                message="Suggestion statement contains forbidden recommendation language",
            )
        )
    if FORBIDDEN_CERTAINTY.search(candidate.statement):
        issues.append(
            ValidationIssue(
                code="FORBIDDEN_CERTAINTY_LANGUAGE",
                message="Suggestion statement contains forbidden certainty language",
            )
        )

    if issues:
        return None, None, RejectedChallengerCandidate(
            raw_candidate=raw_candidate, issues=tuple(issues)
        )

    if candidate.suggestion_kind == "ALTERNATIVE_INTERPRETATION":
        return (
            None,
            AlternativeInterpretation(
                interpretation_id=uuid4(),
                original_claim_id=candidate.analyst_claim_id,
                statement=candidate.statement,
                citations=tuple(resolved),
                source="MODEL_ASSISTED",
            ),
            None,
        )

    finding_type = _SUGGESTION_FINDING_TYPE[candidate.suggestion_kind]
    finding = ChallengeFinding(
        finding_id=uuid4(),
        finding_type=finding_type,  # type: ignore[arg-type]
        severity=SEVERITY_BY_TYPE[finding_type],  # type: ignore[index]
        statement=candidate.statement,
        cited_evidence=tuple(resolved),
        source="MODEL_ASSISTED",
    )
    return finding, None, None


def validate_challenger_response(
    raw_response: RawAnalystResponse,
    bundle: ChallengerInputBundle,
    claim_ids: set[UUID],
) -> tuple[
    dict[UUID, tuple[ChallengeFinding, ...]],
    dict[UUID, tuple[AlternativeInterpretation, ...]],
    tuple[RejectedChallengerCandidate, ...],
]:
    raw_candidates = raw_response.structured_payload.get("candidates", [])
    findings_by_claim: dict[UUID, list[ChallengeFinding]] = {}
    alts_by_claim: dict[UUID, list[AlternativeInterpretation]] = {}
    rejected: list[RejectedChallengerCandidate] = []
    if not isinstance(raw_candidates, list):
        return {}, {}, (
            RejectedChallengerCandidate(
                raw_candidate={"candidates": raw_candidates},
                issues=(
                    ValidationIssue(code="MALFORMED_RESPONSE", message="candidates must be a list"),
                ),
            ),
        )
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            rejected.append(
                RejectedChallengerCandidate(
                    raw_candidate={"value": raw_candidate},
                    issues=(ValidationIssue(code="MALFORMED_RESPONSE", message="not an object"),),
                )
            )
            continue
        finding, alt, rejection = validate_challenger_candidate(raw_candidate, bundle, claim_ids)
        if finding is not None:
            findings_by_claim.setdefault(UUID(str(raw_candidate["analyst_claim_id"])), []).append(
                finding
            )
        elif alt is not None:
            alts_by_claim.setdefault(alt.original_claim_id, []).append(alt)
        elif rejection is not None:
            rejected.append(rejection)
    return (
        {k: tuple(v) for k, v in findings_by_claim.items()},
        {k: tuple(v) for k, v in alts_by_claim.items()},
        tuple(rejected),
    )


__all__ = ["validate_challenger_candidate", "validate_challenger_response"]
