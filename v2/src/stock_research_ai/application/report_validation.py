"""Deterministic validation of ReportNarrator output.

A NarrativeBlockCandidate carries only `section` and `text` - no evidence ID, hash,
numeric value, unit, period, scope, coverage status, or outcome field exists on it.
This module is the ONLY place narrator prose is checked, against the closed
universe of values the deterministic assembler already produced for that section
(its own tables and statements) - never against raw evidence a second time.
"""

import re
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from pydantic import ValidationError

from stock_research_ai.application.evidence_matching import (
    FORBIDDEN_CERTAINTY,
    FORBIDDEN_RECOMMENDATION,
    extract_numeric_tokens,
)
from stock_research_ai.domain.analysis import RawAnalystResponse, ValidationIssue
from stock_research_ai.domain.report import (
    NarrativeBlockCandidate,
    RejectedNarrativeBlock,
    ReportSection,
    ReportStatement,
)

FORBIDDEN_COMPLETENESS_CLAIM = re.compile(
    r"\b(all annual reports|every filing|complete document history|"
    r"entire document (universe|history)|full document history|all documents are available)\b",
    re.IGNORECASE,
)


def _numeric_universe_for_section(section: ReportSection) -> set[Decimal]:
    universe: set[Decimal] = set()
    for table in section.tables:
        for row in table.rows:
            for cell in row:
                if cell.display_value is None:
                    continue
                try:
                    universe.add(Decimal(cell.display_value))
                except InvalidOperation:
                    continue
    for statement in section.statements:
        universe.update(token.value for token in extract_numeric_tokens(statement.text))
    return universe


def validate_narrative_block(
    raw_block: dict[str, object], sections: tuple[ReportSection, ...]
) -> tuple[ReportStatement | None, RejectedNarrativeBlock | None]:
    issues: list[ValidationIssue] = []
    try:
        candidate = NarrativeBlockCandidate.model_validate(raw_block)
    except ValidationError as exc:
        return None, RejectedNarrativeBlock(
            raw_block=raw_block,
            issues=(ValidationIssue(code="MALFORMED_RESPONSE", message=str(exc)),),
        )

    section = next((s for s in sections if s.section == candidate.section), None)
    if section is None:
        return None, RejectedNarrativeBlock(
            raw_block=raw_block,
            issues=(ValidationIssue(code="UNKNOWN_SECTION", message=candidate.section),),
        )

    if FORBIDDEN_RECOMMENDATION.search(candidate.text):
        issues.append(
            ValidationIssue(
                code="FORBIDDEN_RECOMMENDATION_LANGUAGE",
                message="Narrative text contains recommendation/target-price/BUY-SELL-HOLD "
                "language",
            )
        )
    if FORBIDDEN_CERTAINTY.search(candidate.text):
        issues.append(
            ValidationIssue(
                code="FORBIDDEN_CERTAINTY_LANGUAGE",
                message="Narrative text asserts unwarranted certainty",
            )
        )
    if candidate.section == "DOCUMENT_CORPUS_AND_EVIDENCE_COVERAGE" and (
        FORBIDDEN_COMPLETENESS_CLAIM.search(candidate.text)
    ):
        issues.append(
            ValidationIssue(
                code="FORBIDDEN_CORPUS_COMPLETENESS_CLAIM",
                message="Narrative text implies full document-universe completeness beyond the "
                "pinned report input",
            )
        )
    if any(st.challenge_outcome == "INVALID_AGAINST_BUNDLE" for st in section.statements) and (
        candidate.section not in ("INDEPENDENT_CHALLENGER_REVIEW", "CHALLENGER_APPENDIX",
                                    "DATA_GAPS_AND_LIMITATIONS")
    ):
        issues.append(
            ValidationIssue(
                code="NARRATOR_CANNOT_NARRATE_INVALID_SECTION",
                message="This section contains an INVALID_AGAINST_BUNDLE claim; narrator text "
                "here could read as an unqualified conclusion",
            )
        )

    universe = _numeric_universe_for_section(section)
    for token in extract_numeric_tokens(candidate.text):
        if token.value not in universe:
            issues.append(
                ValidationIssue(
                    code="UNSUPPORTED_NUMERIC_TOKEN",
                    message=f"{token.value} is not already present in this section's own "
                    "deterministic content",
                )
            )

    if issues:
        return None, RejectedNarrativeBlock(raw_block=raw_block, issues=tuple(issues))

    evidence_refs = tuple(
        dict.fromkeys(
            cell.evidence_ref
            for table in section.tables
            for row in table.rows
            for cell in row
            if cell.evidence_ref is not None
        )
    ) or tuple(dict.fromkeys(ref for st in section.statements for ref in st.evidence_refs))
    source_claim_ids = tuple(
        dict.fromkeys(cid for st in section.statements for cid in st.source_claim_ids)
    )
    if not (evidence_refs or source_claim_ids):
        return None, RejectedNarrativeBlock(
            raw_block=raw_block,
            issues=(
                ValidationIssue(
                    code="NO_CITABLE_CONTENT_IN_SECTION",
                    message="Section has no deterministic content for narrator text to attach to",
                ),
            ),
        )
    statement = ReportStatement(
        statement_id=uuid4(),
        section=candidate.section,
        text=candidate.text,
        epistemic_class="INTERPRETATION",
        origin="NARRATOR",
        source_claim_ids=source_claim_ids,
        evidence_refs=evidence_refs,
    )
    return statement, None


def validate_narrative_response(
    raw_response: RawAnalystResponse, sections: tuple[ReportSection, ...]
) -> tuple[tuple[ReportStatement, ...], tuple[RejectedNarrativeBlock, ...]]:
    raw_blocks = raw_response.structured_payload.get("narrative_blocks", [])
    if not isinstance(raw_blocks, list):
        return (), (
            RejectedNarrativeBlock(
                raw_block={"narrative_blocks": raw_blocks},
                issues=(
                    ValidationIssue(
                        code="MALFORMED_RESPONSE", message="narrative_blocks must be a list"
                    ),
                ),
            ),
        )
    accepted: list[ReportStatement] = []
    rejected: list[RejectedNarrativeBlock] = []
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            rejected.append(
                RejectedNarrativeBlock(
                    raw_block={"value": raw_block},
                    issues=(ValidationIssue(code="MALFORMED_RESPONSE", message="not an object"),),
                )
            )
            continue
        statement, rejection = validate_narrative_block(raw_block, sections)
        if statement is not None:
            accepted.append(statement)
        elif rejection is not None:
            rejected.append(rejection)
    return tuple(accepted), tuple(rejected)


__all__ = [
    "FORBIDDEN_COMPLETENESS_CLAIM",
    "validate_narrative_block",
    "validate_narrative_response",
]
