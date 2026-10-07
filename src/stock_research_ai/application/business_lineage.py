"""DETAILED_RESEARCH_V1 Box 7: segment names, lineage and structure comparability.

Names are unified only when they are identical after documented normalization (EXACT),
when a configured alias carries stated evidence (EXPLICIT_ALIAS), or when the company
itself says a segment was renamed (RENAMED). A segment that disappears while another
appears, with no statement, is a POSSIBLE_RENAME and is never merged.

A segment structure is the set of reportable segments in the latest filing that presents
a period. Consecutive periods are COMPARABLE when the structures match and no
reorganisation lies between them, RESTATED_COMPARABLE when one later filing presents
both periods under the same structure, and STRUCTURE_CHANGED otherwise - which later
blocks growth analytics and trend charts across that boundary.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid5

from stock_research_ai.application.business_lines import NS
from stock_research_ai.application.business_registry import clean_name, name_key
from stock_research_ai.domain.business_intelligence import (
    BusinessEvidence,
    BusinessEvidenceKind,
    Comparability,
    ComparabilityAssessment,
    LineageKind,
    NameMatch,
    Restated,
    SegmentLevel,
    SegmentLineageEvent,
    SegmentMetric,
    SegmentNameRelation,
    SegmentObservation,
    SegmentRole,
    SegmentStructure,
)
from stock_research_ai.domain.evidence import AUTHORITY_RANK
from stock_research_ai.domain.financial_canonical import PeriodType, ReportingBasis


@dataclass(frozen=True)
class SegmentAlias:
    """A configured alias: `alias` is treated as `canonical` only with stated evidence."""

    alias: str
    canonical: str
    evidence: str


_NAME = r"[\"“'‘]?(?P<{g}>[A-Z][\w&,\-/ ]{{1,60}}?)[\"”'’]?"
#: where a captured segment name stops
_END = (
    r"(?=\s*(?:segments?\b|[.,;)]|$)|\s+(?:with|from|effective|w\.e\.f|during|in order|to "
    r"reflect|pursuant|following|consequent)\b)"
)
_RENAME = (
    re.compile(
        _NAME.format(g="old") + r"\s+segment\s+(?:has been|was|is|stands)\s+re-?named\s+"
        r"(?:as|to)\s+" + _NAME.format(g="new") + _END,
        re.I,
    ),
    re.compile(
        r"re-?named\s+(?:its|the)\s+(?:segment\s+)?"
        + _NAME.format(g="old")
        + r"\s+(?:segment\s+)?(?:as|to)\s+"
        + _NAME.format(g="new")
        + _END,
        re.I,
    ),
    re.compile(
        _NAME.format(g="new")
        + r"\s+\((?:segment\s+)?formerly(?:\s+known\s+as)?\s+"
        + _NAME.format(g="old")
        + r"\)",
        re.I,
    ),
)
_MERGE = re.compile(
    _NAME.format(g="a") + r"\s+and\s+" + _NAME.format(g="b") + r"\s+(?:segments\s+)?(?:have been|"
    r"were|are)\s+(?:merged|combined)\s+(?:into|to form|as)\s+(?:a single segment,?\s+)?"
    + _NAME.format(g="new")
    + _END,
    re.I,
)
_SPLIT = re.compile(
    _NAME.format(g="old") + r"\s+segment\s+(?:has been|was)\s+(?:split|divided|bifurcated)\s+"
    r"into\s+" + _NAME.format(g="a") + r"\s+and\s+" + _NAME.format(g="b") + _END,
    re.I,
)
_REORG = re.compile(
    r"re-?organi[sz](?:ed|ation of)\s+(?:its|the)?\s*(?:reportable\s+|operating\s+|business\s+)?"
    r"segments|change[sd]?\s+(?:in\s+)?(?:the\s+)?(?:composition\s+of\s+)?(?:its\s+|the\s+)?"
    r"(?:reportable|operating|business)\s+segments|re-?aligned\s+(?:its|the)\s+segments",
    re.I,
)
_RESTATED_YES = re.compile(
    r"(?:comparative|previous|prior|corresponding)[^.]{0,120}(?:have been|has been|are|were)\s+"
    r"(?:restated|re-?classified|regrouped|re-?presented|recast)",
    re.I,
)
_RESTATED_NO = re.compile(
    r"not\s+(?:been\s+)?(?:restated|re-?presented|recast)|(?:impracticable|not practicable)\s+"
    r"to\s+restate",
    re.I,
)


def restated_from(text: str) -> Restated:
    if _RESTATED_NO.search(text):
        return Restated.NO
    if _RESTATED_YES.search(text):
        return Restated.YES
    return Restated.UNKNOWN


def _trim(name: str, known: set[str], keep_end: bool) -> str:
    """The longest run of words naming a segment some table uses: a suffix for the old
    name ('The Crop Care' -> 'Crop Care'), a prefix for the new one ('Agri Solutions with
    no change ...' -> 'Agri Solutions'). Unknown names stay as captured."""
    words = clean_name(name).split()
    spans = (
        [words[i:] for i in range(len(words))]
        if keep_end
        else [words[:j] for j in range(len(words), 0, -1)]
    )
    for span in spans:
        if name_key(" ".join(span)) in known:
            return " ".join(span)
    trimmed = words[1:] if words and words[0].lower() == "the" else words
    return " ".join(trimmed)


def lineage_events(
    evidence: Sequence[BusinessEvidence], reference: dict[UUID, date], known: set[str]
) -> list[SegmentLineageEvent]:
    events: list[SegmentLineageEvent] = []
    seen: set[tuple[str, tuple[str, ...], tuple[str, ...], UUID | None]] = set()
    for item in sorted(evidence, key=lambda e: str(e.evidence_id)):
        if item.evidence_kind not in (
            BusinessEvidenceKind.SEGMENT_CHANGE,
            BusinessEvidenceKind.SEGMENT_DESCRIPTION,
        ):
            continue
        text = " ".join(item.raw_text.split())
        restated = restated_from(text)
        effective = reference.get(item.source.document_id) if item.source.document_id else None
        found: list[tuple[LineageKind, tuple[str, ...], tuple[str, ...]]] = []

        def about_segments(m: re.Match[str], text: str = text) -> bool:
            return bool(re.search(r"segment", text[max(0, m.start() - 150) : m.end() + 150], re.I))

        for pattern in _RENAME:
            for m in pattern.finditer(text):
                if not about_segments(m):
                    continue
                found.append(
                    (
                        LineageKind.RENAMED,
                        (_trim(m.group("old"), known, True),),
                        (_trim(m.group("new"), known, False),),
                    )
                )
        for m in _MERGE.finditer(text):
            if not about_segments(m):
                continue
            found.append(
                (
                    LineageKind.MERGED,
                    (_trim(m.group("a"), known, True), _trim(m.group("b"), known, False)),
                    (_trim(m.group("new"), known, False),),
                )
            )
        for m in _SPLIT.finditer(text):
            if not about_segments(m):
                continue
            found.append(
                (
                    LineageKind.SPLIT,
                    (_trim(m.group("old"), known, True),),
                    (clean_name(m.group("a")), clean_name(m.group("b"))),
                )
            )
        if not found and _REORG.search(text):
            found.append((LineageKind.REORGANIZED, (), ()))
        for kind, old, new in found:
            key = (kind.value, old, new, item.source.document_id)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                SegmentLineageEvent(
                    event_id=uuid5(NS, f"lineage|{item.evidence_id}|{kind.value}|{old}|{new}"),
                    kind=kind,
                    effective_period_end=effective,
                    old_segments=old,
                    new_segments=new,
                    restated_comparatives=restated,
                    raw_text=item.raw_text[:4000],
                    evidence_id=item.evidence_id,
                    source=item.source,
                )
            )
    return events


def name_relations(
    events: Sequence[SegmentLineageEvent],
    aliases: Sequence[SegmentAlias],
    known: set[str],
) -> tuple[dict[str, str], list[SegmentNameRelation]]:
    """(raw key -> canonical key, relations). Renames map old -> new only when both names
    are ones the filings actually use; configured aliases need stated evidence."""
    mapping: dict[str, str] = {}
    relations: list[SegmentNameRelation] = []
    for alias in sorted(aliases, key=lambda a: (a.alias, a.canonical)):
        if not alias.evidence.strip():
            continue
        a, c = name_key(alias.alias), name_key(alias.canonical)
        mapping[a] = c
        relations.append(
            SegmentNameRelation(
                relation_id=uuid5(NS, f"relation|alias|{a}|{c}"),
                from_key=a,
                to_key=c,
                outcome=NameMatch.EXPLICIT_ALIAS,
                reason=f"configured alias: {alias.evidence}"[:1000],
            )
        )
    for event in events:
        if event.kind != LineageKind.RENAMED:
            continue
        old, new = name_key(event.old_segments[0]), name_key(event.new_segments[0])
        if old == new:
            continue
        if old in known and new in known:
            mapping[old] = mapping.get(new, new)
            outcome, reason = NameMatch.RENAMED, "the filing states the segment was renamed"
        else:
            outcome = NameMatch.UNKNOWN
            reason = "rename statement names a segment no segment table uses; not applied"
        relations.append(
            SegmentNameRelation(
                relation_id=uuid5(NS, f"relation|rename|{old}|{new}|{event.event_id}"),
                from_key=old,
                to_key=new,
                outcome=outcome,
                reason=reason,
                evidence_ids=(event.evidence_id,),
            )
        )
    return mapping, relations


@dataclass(frozen=True)
class Presentation:
    frame_id: UUID
    document_id: UUID | None
    reference_end: date | None
    authority_rank: int
    restated: bool
    keys: frozenset[str]
    observations: tuple[SegmentObservation, ...]


def _frequency(period_type: PeriodType) -> str | None:
    return {PeriodType.FY: "FY", PeriodType.QUARTER: "QUARTER"}.get(period_type)


def presentations(
    observations: Sequence[SegmentObservation],
    canonical: dict[UUID, str],
    reference: dict[UUID, date],
    restated_frames: set[UUID],
) -> dict[tuple[ReportingBasis, str, date], list[Presentation]]:
    """Per (basis, frequency, period end): every frame presenting reportable segment
    revenue for that period, latest filing first."""
    grouped: dict[tuple[ReportingBasis, str, date, UUID], list[SegmentObservation]] = {}
    for o in observations:
        frequency = _frequency(o.period.period_type)
        if (
            frequency is None
            or o.period.period_end is None
            or o.metric != SegmentMetric.REVENUE
            or o.segment_role != SegmentRole.REPORTABLE
            or o.segment_level != SegmentLevel.PRIMARY
            or o.reporting_basis == ReportingBasis.UNKNOWN
            or o.normalized_value is None
        ):
            continue
        grouped.setdefault(
            (o.reporting_basis, frequency, o.period.period_end, o.frame_id), []
        ).append(o)
    out: dict[tuple[ReportingBasis, str, date], list[Presentation]] = {}
    for (basis, frequency, end, frame_id), members in grouped.items():
        doc = members[0].source.document_id
        out.setdefault((basis, frequency, end), []).append(
            Presentation(
                frame_id,
                doc,
                reference.get(doc) if doc else None,
                min(AUTHORITY_RANK[o.source.authority] for o in members),
                frame_id in restated_frames or any(o.restated for o in members),
                frozenset(canonical[o.observation_id] for o in members),
                tuple(sorted(members, key=lambda o: str(o.observation_id))),
            )
        )
    for key in out:
        out[key].sort(
            key=lambda p: (
                -(p.reference_end.toordinal() if p.reference_end else 0),
                p.authority_rank,
                str(p.frame_id),
            )
        )
    return out


def structures_and_comparability(
    presented: dict[tuple[ReportingBasis, str, date], list[Presentation]],
    events: Sequence[SegmentLineageEvent],
    renamed_pairs: set[tuple[str, str]],
) -> tuple[list[SegmentStructure], list[ComparabilityAssessment], list[SegmentNameRelation]]:
    structures: list[SegmentStructure] = []
    assessments: list[ComparabilityAssessment] = []
    relations: list[SegmentNameRelation] = []
    series: dict[tuple[ReportingBasis, str], list[date]] = {}
    for basis, frequency, end in presented:
        series.setdefault((basis, frequency), []).append(end)
    for (basis, frequency, end), items in sorted(
        presented.items(), key=lambda kv: (kv[0][0].value, kv[0][1], kv[0][2])
    ):
        latest = items[0]
        structures.append(
            SegmentStructure(
                structure_id=uuid5(NS, f"structure|{basis.value}|{frequency}|{end}"),
                reporting_basis=basis,
                frequency=frequency,
                period_end=end,
                segment_keys=tuple(sorted(latest.keys)),
                document_ids=tuple(
                    sorted({p.document_id for p in items if p.document_id}, key=str)
                ),
                presented_in_later_document=bool(
                    latest.reference_end and latest.reference_end > end
                ),
            )
        )
    for (basis, frequency), ends in sorted(
        series.items(), key=lambda kv: (kv[0][0].value, kv[0][1])
    ):
        ends = sorted(ends)
        for earlier, later in zip(ends, ends[1:], strict=False):
            first = presented[(basis, frequency, earlier)][0]
            second = presented[(basis, frequency, later)][0]
            between = [
                e
                for e in events
                if e.effective_period_end is not None
                and earlier < e.effective_period_end <= later
                and e.kind != LineageKind.RENAMED
            ]
            together = next(
                (
                    p
                    for p in presented[(basis, frequency, earlier)]
                    if p.frame_id == second.frame_id and p.keys == second.keys
                ),
                None,
            )
            added = tuple(sorted(second.keys - first.keys))
            removed = tuple(sorted(first.keys - second.keys))
            renamed = tuple(sorted(p for p in renamed_pairs if p[0] in removed and p[1] in added))
            if together is not None:
                restated = together.restated or any(
                    e.restated_comparatives == Restated.YES for e in between
                )
                status = (
                    Comparability.RESTATED_COMPARABLE
                    if restated or between
                    else Comparability.COMPARABLE
                )
                reason = "one filing presents both periods under the same structure" + (
                    " (comparatives restated)" if restated else ""
                )
            elif first.keys == second.keys and not between:
                status, reason = (
                    Comparability.COMPARABLE,
                    "same reportable segments, no reorganisation",
                )
            elif first.keys == second.keys:
                status = Comparability.STRUCTURE_CHANGED
                reason = (
                    "a reorganisation is stated between the filings; same names, "
                    "composition may differ"
                )
            else:
                status = Comparability.STRUCTURE_CHANGED
                reason = "different reportable segments"
                if len(added) == 1 and len(removed) == 1 and not renamed:
                    relations.append(
                        SegmentNameRelation(
                            relation_id=uuid5(
                                NS,
                                f"relation|possible|{removed[0]}|{added[0]}|"
                                f"{basis.value}|{frequency}|{later}",
                            ),
                            from_key=removed[0],
                            to_key=added[0],
                            outcome=NameMatch.POSSIBLE_RENAME,
                            reason="one segment disappears and one appears with no statement; "
                            "kept DISTINCT (never merged)",
                        )
                    )
            assessments.append(
                ComparabilityAssessment(
                    assessment_id=uuid5(
                        NS, f"comparability|{basis.value}|{frequency}|{earlier}|{later}"
                    ),
                    reporting_basis=basis,
                    frequency=frequency,
                    from_period_end=earlier,
                    to_period_end=later,
                    status=status,
                    added_segments=added,
                    removed_segments=removed,
                    renamed=renamed,
                    lineage_event_ids=tuple(sorted((e.event_id for e in between), key=str)),
                    reason=reason,
                )
            )
    return structures, assessments, relations
