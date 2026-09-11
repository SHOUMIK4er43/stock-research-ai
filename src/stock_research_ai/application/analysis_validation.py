"""Deterministic acceptance/rejection of AnalystModel output. Nothing here does arithmetic;
every check either resolves a citation back into the bundle or performs a lexical scan.
"""

import re
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from stock_research_ai.application.analysis import Calculation, ResolvedEvidence
from stock_research_ai.application.analysis import _reference_status as reference_status
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
    SourceEpistemicClass,
    SupportAssessment,
    TimeScope,
    ValidationIssue,
)
from stock_research_ai.domain.calculations import CalculationResult, CalculationSnapshot
from stock_research_ai.domain.financial import ReportingScope
from stock_research_ai.domain.metrics import MetricMappingStatus

FORBIDDEN_RECOMMENDATION = re.compile(
    r"\b(buy|sell|\bhold\b|overweight|underweight|outperform|underperform|accumulate|reduce"
    r"|initiate coverage|target price)\b",
    re.IGNORECASE,
)
FORBIDDEN_CERTAINTY = re.compile(
    r"\b(guaranteed|certain to|will definitely|always beats|never fails|without doubt)\b",
    re.IGNORECASE,
)
FORBIDDEN_HISTORICAL_KNOWLEDGE = re.compile(
    r"\b(was known|was already known|was publicly known|confirmed as of|historically known"
    r"|was known to the market)\b",
    re.IGNORECASE,
)
FORBIDDEN_BASE_LIKELIHOOD = re.compile(
    r"\b(most likely|most probable|highest probability|expected case)\b", re.IGNORECASE
)
BANK_INDUSTRIAL_TERMS = re.compile(
    r"\b(net debt|ev/ebitda|free cash flow yield|fcff)\b", re.IGNORECASE
)

_NUM = r"-?\d+(?:,\d{3})*(?:\.\d+)?"
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PERCENT", re.compile(rf"({_NUM})\s?%")),
    ("BASIS_POINTS", re.compile(rf"({_NUM})\s?(?:bps|basis points)", re.IGNORECASE)),
    ("MULTIPLE_X", re.compile(rf"({_NUM})x\b", re.IGNORECASE)),
    ("CURRENCY", re.compile(rf"(?:₹|Rs\.?|INR)\s?({_NUM})", re.IGNORECASE)),
)
_PLAIN_NUMBER = re.compile(_NUM)

_UNIT_FAMILY_ALIASES = {
    "RATIO_X": "MULTIPLE_X",
    "CURRENCY_PER_SHARE": "CURRENCY",
}


class NumericToken:
    __slots__ = ("value", "family")

    def __init__(self, value: Decimal, family: str) -> None:
        self.value = value
        self.family = family


def extract_numeric_tokens(statement: str) -> tuple[NumericToken, ...]:
    tokens: list[NumericToken] = []
    covered: list[tuple[int, int]] = []
    for family, pattern in _TOKEN_PATTERNS:
        for match in pattern.finditer(statement):
            covered.append(match.span())
            raw = match.group(1).replace(",", "")
            tokens.append(NumericToken(Decimal(raw), family))

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < c[1] and c[0] < span[1] for c in covered)

    for match in _PLAIN_NUMBER.finditer(statement):
        if overlaps(match.span()):
            continue
        raw = match.group(0).replace(",", "")
        tokens.append(NumericToken(Decimal(raw), "NUMBER"))
    return tuple(tokens)


def canonical_values(
    ref: EvidenceReference, evidence: ResolvedEvidence
) -> tuple[tuple[Decimal, str], ...]:
    """(value, unit_family) pairs a citation legitimately supports, from already-canonical
    display values only - never recomputed."""
    values: list[tuple[Decimal, str]] = []
    if ref.kind in ("FINANCIAL", "SHAREHOLDING"):
        f = evidence.financial
        assert f is not None
        for section in (
            f.top_metrics, f.profit_loss, f.quarterly, f.balance_sheet, f.cashflow, f.ratios,
        ):
            for o in section:
                if str(o.id) == ref.item_key and o.value is not None:
                    family = {"money": "CURRENCY", "percent": "PERCENT"}.get(o.unit.kind, "NUMBER")
                    values.append((o.value * o.unit.scale, family))
        for holding in f.shareholding.holdings:
            if (
                str(holding.observation.id) == ref.item_key
                and holding.observation.value is not None
            ):
                o = holding.observation
                family = {"money": "CURRENCY", "percent": "PERCENT"}.get(o.unit.kind, "NUMBER")
                values.append((o.value * o.unit.scale, family))  # type: ignore[operator]
        for count in f.shareholding.shareholder_counts:
            if str(count.id) == ref.item_key and count.value is not None:
                values.append((count.value * count.unit.scale, "NUMBER"))
    elif ref.kind == "MARKET":
        m = evidence.markets[ref.owning_snapshot_id]
        for market_observation in m.observations:
            if str(market_observation.id) == ref.item_key:
                values.append((market_observation.close, "CURRENCY"))
    elif ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        c: Calculation = evidence.calculations[ref.owning_snapshot_id]
        for calc_result in c.results:
            metric_id = (
                calc_result.metric_id
                if isinstance(calc_result, CalculationResult)
                else calc_result.definition.metric_id
            )
            if metric_id == ref.item_key and calc_result.display_value is not None:
                unit = calc_result.unit if isinstance(calc_result, CalculationResult) else (
                    calc_result.definition.unit
                )
                values.append((calc_result.display_value, _UNIT_FAMILY_ALIASES.get(unit, unit)))
    elif ref.kind == "MARKET_CALCULATION":
        mc = evidence.market_calculations[ref.owning_snapshot_id]
        for mc_result in mc.results:
            if str(mc_result.metric_id) == ref.item_key and mc_result.display_value is not None:
                mc_family = _UNIT_FAMILY_ALIASES.get(mc_result.unit, mc_result.unit)
                values.append((mc_result.display_value, mc_family))
    elif ref.kind == "TECHNICAL":
        t = evidence.technicals[ref.owning_snapshot_id]
        for series in t.series:
            for point in series.points:
                if str(point.id) == ref.item_key and point.display_value is not None:
                    values.append(
                        (point.display_value, _UNIT_FAMILY_ALIASES.get(series.unit, series.unit))
                    )
    elif ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for val_result in v.results:
            if str(val_result.id) == ref.item_key:
                for value in val_result.values:
                    values.append(
                        (value.display_value, _UNIT_FAMILY_ALIASES.get(value.unit, value.unit))
                    )
    elif ref.kind == "MACRO":
        macro_snapshot = evidence.macros[ref.owning_snapshot_id]
        for observation in macro_snapshot.observations:
            if observation.reference_period == ref.item_key:
                values.append((observation.value, "NUMBER"))
    return tuple(values)


def is_unverified_semantic(ref: EvidenceReference, evidence: ResolvedEvidence) -> bool:
    if ref.kind not in ("FINANCIAL", "SHAREHOLDING"):
        return False
    f = evidence.financial
    assert f is not None
    for section in (f.top_metrics, f.profit_loss, f.quarterly, f.balance_sheet, f.cashflow,
                    f.ratios):
        for o in section:
            if str(o.id) == ref.item_key:
                return o.mapping_status != MetricMappingStatus.MAPPED
    for holding in f.shareholding.holdings:
        if str(holding.observation.id) == ref.item_key:
            return holding.observation.mapping_status != MetricMappingStatus.MAPPED
    for count in f.shareholding.shareholder_counts:
        if str(count.id) == ref.item_key:
            return count.mapping_status != MetricMappingStatus.MAPPED
    return False


def source_epistemic_class(
    ref: EvidenceReference, evidence: ResolvedEvidence
) -> SourceEpistemicClass:
    if ref.kind in ("COMPANY", "FINANCIAL", "SHAREHOLDING", "MARKET", "DOCUMENT_CHUNK",
                    "PUBLICATION", "MACRO"):
        return "FACT"
    if ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for val_result in v.results:
            if str(val_result.id) == ref.item_key:
                return "SCENARIO" if val_result.definition.classification == "SCENARIO" else (
                    "CALCULATION"
                )
    return "CALCULATION"


def limitation_codes(ref: EvidenceReference, evidence: ResolvedEvidence) -> tuple[str, ...]:
    """Issue codes propagated from a citation's own upstream result, generically per kind."""
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        c = evidence.calculations[ref.owning_snapshot_id]
        for calc_result in c.results:
            metric_id = (
                calc_result.metric_id
                if isinstance(calc_result, CalculationResult)
                else calc_result.definition.metric_id
            )
            if metric_id == ref.item_key:
                if isinstance(calc_result, CalculationResult):
                    return tuple(sorted(calc_result.issues))
                return tuple(sorted(i.code for i in calc_result.issues))
    if ref.kind == "PATTERN":
        p = evidence.patterns[ref.owning_snapshot_id]
        for finding in p.findings:
            if str(finding.id) == ref.item_key:
                return tuple(sorted(i.code for i in finding.issues))
    if ref.kind == "MARKET_CALCULATION":
        mc = evidence.market_calculations[ref.owning_snapshot_id]
        for mc_result in mc.results:
            if str(mc_result.metric_id) == ref.item_key:
                return tuple(sorted(i.code for i in mc_result.issues))
    if ref.kind == "TECHNICAL":
        t = evidence.technicals[ref.owning_snapshot_id]
        for series in t.series:
            for point in series.points:
                if str(point.id) == ref.item_key and point.issue:
                    return (point.issue,)
    if ref.kind == "VALUATION":
        v = evidence.valuations[ref.owning_snapshot_id]
        for val_result in v.results:
            if str(val_result.id) == ref.item_key:
                return tuple(sorted(i.code for i in val_result.issues))
    return ()


def citation_period_scope(
    ref: EvidenceReference, evidence: ResolvedEvidence
) -> tuple[str | None, ReportingScope | None]:
    if ref.kind == "FINANCIAL_CALCULATION":
        c = evidence.calculations[ref.owning_snapshot_id]
        assert isinstance(c, CalculationSnapshot)
        for spec in c.request.calculations:
            if spec.metric_id == ref.item_key:
                return spec.current_period, c.request.reporting_scope
        return None, c.request.reporting_scope
    if ref.kind == "SECTOR_KPI":
        kpi = evidence.calculations[ref.owning_snapshot_id]
        assert not isinstance(kpi, CalculationSnapshot)
        return kpi.request.current_period, kpi.request.reporting_scope
    if ref.kind == "FINANCIAL":
        f = evidence.financial
        assert f is not None
        for section in (f.top_metrics, f.profit_loss, f.quarterly, f.balance_sheet, f.cashflow,
                        f.ratios):
            for o in section:
                if str(o.id) == ref.item_key:
                    return o.period.fiscal_label, o.reporting_scope
    return None, None


def _semantic_key(ref: EvidenceReference) -> str | None:
    """A comparability key for 'same semantic metric' - only kinds with a stable metric_id
    style item_key are ever compared; everything else can never be called conflicting."""
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI", "MARKET_CALCULATION"):
        return ref.item_key
    return None


def _has_conflicting_evidence(
    refs: list[EvidenceReference], evidence: ResolvedEvidence
) -> bool:
    """Strict comparability per architecture: same semantic metric, same period, same
    reporting scope, compatible unit - only then may disagreement be called a conflict."""
    grouped: dict[tuple[str, str | None, ReportingScope | None], set[tuple[Decimal, str]]] = {}
    for ref in refs:
        key_metric = _semantic_key(ref)
        if key_metric is None:
            continue
        period, scope = citation_period_scope(ref, evidence)
        group_key = (key_metric, period, scope)
        for value, family in canonical_values(ref, evidence):
            grouped.setdefault(group_key, set()).add((value, family))
    return any(
        len({family for _, family in values}) == 1 and len({v for v, _ in values}) > 1
        for values in grouped.values()
    )


def find_reference(
    kind: str,
    owning_snapshot_id: object,
    item_key: str | None,
    sources: tuple[EvidenceReference, ...],
) -> EvidenceReference | None:
    for ref in sources:
        if ref.kind == kind and ref.owning_snapshot_id == owning_snapshot_id and (
            ref.item_key == item_key
        ):
            return ref
    return None


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
