"""DETAILED_RESEARCH_V1 Box 8: ManagementIntelligenceDataset assembly.

Box 3 transcripts / company documents
  -> events, participants, turns, Q&A pairs           (management_calls)
  -> management statements                            (management_statements)
  -> guidance items, lifecycle, timelines              (management_guidance)
  -> outcomes vs trusted Box 4 / 5 / 7 actuals, counts  (management_outcomes)
  -> language metrics, themes                         (management_language)
  -> ManagementIntelligenceDataset (deterministic, provenance-complete, bounded).
"""

import hashlib
import json
from collections import Counter
from datetime import date, datetime
from typing import TypeVar
from uuid import UUID, uuid5

from stock_research_ai.application.business_intelligence import canonical_business_json
from stock_research_ai.application.business_registry import sector_family
from stock_research_ai.application.document_intelligence import canonical_bundle_json
from stock_research_ai.application.evidence_catalogue import canonical_catalogue_json
from stock_research_ai.application.financial_analytics import canonical_analytics_json
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.management_calls import (
    DOCUMENT_TYPES,
    TRANSCRIPT_TYPES,
    CallParser,
    Source,
    TurnText,
)
from stock_research_ai.application.management_guidance import GuidanceBuilder, Segment
from stock_research_ai.application.management_language import language_metrics, themes
from stock_research_ai.application.management_outcomes import (
    OutcomeEngine,
    actual_index,
    summaries,
)
from stock_research_ai.application.management_registry import NS
from stock_research_ai.application.management_statements import extract_statements
from stock_research_ai.domain.business_intelligence import (
    BusinessIntelligenceDataset,
    SegmentRole,
)
from stock_research_ai.domain.document_intelligence import EvidenceBundle, EvidenceKind
from stock_research_ai.domain.evidence import EvidenceCatalogue
from stock_research_ai.domain.financial_analytics import FinancialAnalyticsDataset
from stock_research_ai.domain.financial_canonical import CanonicalFinancialDataset
from stock_research_ai.domain.management_intelligence import (
    ENGINE_VERSION,
    LANGUAGE_LEXICON_VERSION,
    MANAGEMENT_SCHEMA,
    OUTCOME_RULES_VERSION,
    REGISTRY_VERSION,
    GuidanceItem,
    ManagementIntelligenceDataset,
    ManagementLimits,
    ManagementStatistics,
    ManagementWarning,
    Provenance,
    Severity,
    SubjectLayer,
    UpstreamReference,
)

T = TypeVar("T")
_TRANSCRIPT_EVIDENCE = {
    EvidenceKind.TRANSCRIPT_QUESTION,
    EvidenceKind.TRANSCRIPT_ANSWER,
    EvidenceKind.MANAGEMENT_GUIDANCE,
    EvidenceKind.MANAGEMENT_CLAIM,
}
_WARNING_SEVERITY = {
    "RESOURCE_LIMIT_REACHED": Severity.HIGH,
    "TRANSCRIPT_MISSING": Severity.HIGH,
    "SOURCE_MISSING": Severity.MEDIUM,
    "EVENT_TITLE_MISSING": Severity.MEDIUM,
    "ROLE_UNKNOWN": Severity.LOW,
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_management_intelligence(
    *,
    bundle: EvidenceBundle,
    catalogue: EvidenceCatalogue,
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset | None,
    box7: BusinessIntelligenceDataset | None,
    generated_at: datetime,
    limits: ManagementLimits | None = None,
) -> ManagementIntelligenceDataset:
    bounds = limits or ManagementLimits()
    for name, key in (
        ("Box 3 bundle", bundle.company_key),
        ("Box 5 dataset", box5.company_key if box5 else box4.company_key),
        ("Box 7 dataset", box7.company_key if box7 else box4.company_key),
    ):
        if key != box4.company_key:
            raise ValueError(f"the {name} belongs to another company")
    warnings: list[tuple[str, str | None]] = []
    partial = False

    if box7 is not None:
        family = box7.sector.value
    else:
        family = sector_family(box4.industry, [])[0].value
        warnings.append(("SOURCE_MISSING", "no Box 7 dataset: sector from Box 4 industry only"))
    if box5 is None:
        warnings.append(("SOURCE_MISSING", "no Box 5 dataset: Box 5 actuals unavailable"))
    segments: list[Segment] = []
    if box7 is not None:
        for s in box7.business_structure.segments:
            if s.role != SegmentRole.REPORTABLE:
                continue
            names = tuple(dict.fromkeys(n for n in (s.canonical_name, *s.raw_names) if len(n) >= 3))
            segments.append(Segment(s.segment_key, names))

    documents = {d.document_id: d for d in catalogue.documents}
    parser = CallParser(box4.company_key, box4.fiscal_year_end_month, bounds)
    usable = sorted(
        (
            e
            for e in bundle.extractions
            if e.document_type in TRANSCRIPT_TYPES | DOCUMENT_TYPES
            and e.status.value in ("SUCCESS", "PARTIAL")
        ),
        key=lambda e: str(e.document_id),
    )
    if len(usable) > bounds.max_documents:
        warnings.append(
            ("RESOURCE_LIMIT_REACHED", f"documents: {len(usable)} > {bounds.max_documents}")
        )
        usable = usable[: bounds.max_documents]
        partial = True
    consumed = 0
    for ex in usable:
        doc = documents.get(ex.document_id)
        evidence_by_block: dict[UUID, list[UUID]] = {}
        for item in ex.evidence:
            if item.kind in _TRANSCRIPT_EVIDENCE:
                for block_id in item.block_ids:
                    evidence_by_block.setdefault(block_id, []).append(item.evidence_id)
        source = Source(
            extraction=ex,
            source_url=str(doc.source_url) if doc else ex.artifact_reference,
            title=doc.title if doc else None,
            evidence_by_block=evidence_by_block,
        )
        before = len(parser.out.events)
        if ex.document_type in TRANSCRIPT_TYPES:
            parser.transcript(source)
        else:
            parser.document(source, doc.published_date if doc else None)
        consumed += len(parser.out.events) > before
    out = parser.out
    warnings.extend(out.warnings)
    if not any(e.document_type in TRANSCRIPT_TYPES for e in out.events):
        warnings.append(("TRANSCRIPT_MISSING", "no concall / AGM transcript event was found"))

    # chronological event order: printed call date, then document, then position
    position = {e.event_id: i for i, e in enumerate(out.events)}
    events = sorted(
        out.events,
        key=lambda e: (e.call_date or date.max, str(e.document_id), position[e.event_id]),
    )
    rank = {e.event_id: i for i, e in enumerate(events)}
    turns = sorted(out.turns, key=lambda t: (rank[t.turn.event_id], t.turn.sequence))
    participants = sorted(out.participants, key=lambda p: (rank[p.event_id], p.person_key))
    qa_pairs = sorted(out.qa_pairs, key=lambda q: (rank[q.event_id], str(q.qa_pair_id)))
    turn_seq = {t.turn.turn_id: t.turn.sequence for t in turns}
    qa_pairs.sort(key=lambda q: (rank[q.event_id], turn_seq[q.question_turn_ids[0]]))

    by_event = {e.event_id: e for e in events}
    contexts = extract_statements(turns, bounds.max_excerpt_chars, by_event)
    built = GuidanceBuilder(
        by_event, family, segments, box4.fiscal_year_end_month, bounds.max_excerpt_chars
    ).run(contexts)
    engine = OutcomeEngine(actual_index(box4, box5, box7), box7, by_event)
    outcomes = [engine.evaluate(g) for g in built.guidance]
    statements = [c.statement for c in contexts]

    # --- resource bounds: never silently: PARTIAL + RESOURCE_LIMIT_REACHED --------------------
    def bound(items: list[T], limit: int, what: str) -> list[T]:
        nonlocal partial
        if len(items) > limit:
            warnings.append(("RESOURCE_LIMIT_REACHED", f"{what}: {len(items)} > {limit}"))
            partial = True
            return items[:limit]
        return items

    events = bound(events, bounds.max_events, "events")
    kept_events = {e.event_id for e in events}
    participants = bound(
        [p for p in participants if p.event_id in kept_events],
        bounds.max_participants,
        "participants",
    )
    kept_people = {p.participant_id for p in participants}
    turn_texts: list[TurnText] = bound(
        [
            t
            for t in turns
            if t.turn.event_id in kept_events and t.turn.participant_id in kept_people
        ],
        bounds.max_turns,
        "turns",
    )
    kept_turns = {t.turn.turn_id for t in turn_texts}
    qa_pairs = bound(
        [
            q
            for q in qa_pairs
            if {*q.question_turn_ids, *q.answer_turn_ids} <= kept_turns
            and {q.questioner_id, *q.responder_ids} <= kept_people
        ],
        bounds.max_qa_pairs,
        "qa_pairs",
    )
    statements = bound(
        [s for s in statements if s.turn_id in kept_turns], bounds.max_statements, "statements"
    )
    kept_statements = {s.statement_id for s in statements}
    guidance: list[GuidanceItem] = bound(
        [g for g in built.guidance if set(g.statement_ids) <= kept_statements],
        bounds.max_guidance,
        "guidance",
    )
    kept_guidance = {g.guidance_id for g in guidance}
    guidance = [
        g
        if g.previous_guidance_id is None or g.previous_guidance_id in kept_guidance
        else g.model_copy(
            update={"previous_guidance_id": None, "warnings": (*g.warnings, "PREDECESSOR_DROPPED")}
        )
        for g in guidance
    ]
    outcomes = bound(
        [o for o in outcomes if o.guidance_id in kept_guidance], bounds.max_outcomes, "outcomes"
    )
    timelines = []
    for line in built.timelines:
        ids = [i for i in line.guidance_ids if i in kept_guidance]
        if ids:
            index = [line.guidance_ids.index(i) for i in ids]
            timelines.append(
                line.model_copy(
                    update={
                        "guidance_ids": tuple(ids),
                        "lifecycles": tuple(line.lifecycles[i] for i in index),
                    }
                )
            )
    unparsed = bound(
        [u for u in built.unparsed if u.statement_id is None or u.statement_id in kept_statements],
        bounds.max_unparsed,
        "unparsed",
    )
    kept_outcomes = {o.guidance_id for o in outcomes}
    scored = [g for g in guidance if g.guidance_id in kept_outcomes]
    accuracy = summaries(scored, outcomes)
    language = language_metrics(events, turn_texts)
    topic_themes = themes(events, statements, qa_pairs)

    # --- identity, upstream references, statistics -------------------------------------------
    box3_ref = UpstreamReference(
        layer="BOX3",
        dataset_id=bundle.bundle_id,
        sha256=_sha(canonical_bundle_json(bundle)),
        schema_version=bundle.schema_version,
    )
    box4_ref = UpstreamReference(
        layer="BOX4",
        dataset_id=box4.dataset_id,
        sha256=_sha(canonical_dataset_json(box4)),
        schema_version=box4.schema_version,
    )
    box5_ref = (
        UpstreamReference(
            layer="BOX5",
            dataset_id=box5.analytics_id,
            sha256=_sha(canonical_analytics_json(box5)),
            schema_version=box5.schema_version,
        )
        if box5 is not None
        else None
    )
    box7_ref = (
        UpstreamReference(
            layer="BOX7",
            dataset_id=box7.dataset_id,
            sha256=_sha(canonical_business_json(box7)),
            schema_version=box7.schema_version,
        )
        if box7 is not None
        else None
    )
    dataset_id = uuid5(
        NS,
        "|".join(
            [
                MANAGEMENT_SCHEMA,
                ENGINE_VERSION,
                REGISTRY_VERSION,
                OUTCOME_RULES_VERSION,
                LANGUAGE_LEXICON_VERSION,
                box3_ref.sha256,
                _sha(canonical_catalogue_json(catalogue)),
                box4_ref.sha256,
                box5_ref.sha256 if box5_ref else "-",
                box7_ref.sha256 if box7_ref else "-",
                bounds.model_dump_json(),
            ]
        ),
    )
    structured = [
        ManagementWarning(
            code=code,
            severity=_WARNING_SEVERITY.get(code, Severity.INFO),
            detail=detail[:1000] if detail else None,
        )
        for code, detail in sorted(set(warnings), key=lambda w: (w[0], w[1] or ""))
    ]
    warning_codes = Counter(w.code for w in structured)
    for e in events:
        warning_codes.update(e.warnings)
    for g in guidance:
        warning_codes.update(g.warnings)
    for o in outcomes:
        warning_codes.update(o.warnings)
    statistics = ManagementStatistics(
        documents_consumed=consumed,
        events=len(events),
        participants=len(participants),
        participants_by_role=dict(sorted(Counter(p.role.value for p in participants).items())),
        turns=len(turn_texts),
        qa_pairs=len(qa_pairs),
        answer_status_counts=dict(sorted(Counter(q.answer_status.value for q in qa_pairs).items())),
        statements=len(statements),
        statements_by_type=dict(
            sorted(Counter(s.statement_types[0].value for s in statements).items())
        ),
        guidance=len(guidance),
        guidance_linked_exact=sum(1 for g in guidance if g.subject.equivalence.value == "EXACT"),
        guidance_linked_proxy=sum(1 for g in guidance if g.subject.equivalence.value == "PROXY"),
        guidance_unlinked=sum(1 for g in guidance if g.subject.equivalence.value == "NONE"),
        lifecycle_counts=dict(sorted(Counter(g.lifecycle.value for g in guidance).items())),
        outcome_counts=dict(sorted(Counter(o.result.value for o in outcomes).items())),
        unparsed=len(unparsed),
        warnings_by_code=dict(sorted(warning_codes.items())),
    )
    return ManagementIntelligenceDataset(
        dataset_id=dataset_id,
        company_key=box4.company_key,
        company_name=box4.company_name,
        sector=family,
        generated_at=generated_at,
        source_box3=box3_ref,
        source_box4=box4_ref,
        source_box5=box5_ref,
        source_box7=box7_ref,
        limits=bounds,
        status="PARTIAL" if partial else "COMPLETE",
        events=tuple(events),
        participants=tuple(participants),
        turns=tuple(t.turn for t in turn_texts),
        qa_pairs=tuple(qa_pairs),
        statements=tuple(statements),
        guidance=tuple(guidance),
        timelines=tuple(timelines),
        outcomes=tuple(outcomes),
        accuracy=tuple(accuracy),
        language=tuple(language),
        themes=tuple(topic_themes),
        unparsed=tuple(unparsed),
        warnings=tuple(structured),
        statistics=statistics,
    )


def canonical_management_json(dataset: ManagementIntelligenceDataset) -> str:
    return json.dumps(
        dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _squeeze(text: str) -> str:
    return "".join(text.split())


def verify_management_provenance(
    dataset: ManagementIntelligenceDataset,
    bundle: EvidenceBundle,
    catalogue: EvidenceCatalogue,
    box4: CanonicalFinancialDataset,
    box5: FinancialAnalyticsDataset | None,
    box7: BusinessIntelligenceDataset | None,
) -> list[str]:
    """Walk every Box 8 record back: outcome -> guidance -> statement -> turn -> participant /
    event -> Box 3 block / evidence -> extraction -> artifact -> catalogue document; and every
    actual -> its Box 4 / 5 / 7 record -> that record's upstream values / observations."""
    problems: list[str] = []
    extractions = {e.extraction_id: e for e in bundle.extractions}
    by_document = {e.document_id: e for e in bundle.extractions}
    blocks = {b.block_id: b for e in bundle.extractions for b in e.blocks}
    items = {i.evidence_id for e in bundle.extractions for i in e.evidence}
    documents = {d.document_id: d for d in catalogue.documents}

    def check(what: str, p: Provenance, text: str | None = None) -> None:
        ex = extractions.get(p.extraction_id)
        if (
            ex is None
            or ex.artifact_sha256 != p.artifact_sha256
            or ex.document_id != (p.document_id)
        ):
            problems.append(f"{what}: extraction / artifact / document mismatch")
            return
        doc = documents.get(p.document_id)
        if doc is not None and str(doc.source_url) != p.source_url:
            problems.append(f"{what}: source URL differs from the catalogue")
        for block_id in p.block_ids:
            block = blocks.get(block_id)
            if block is None or not p.page_start <= block.page_number <= p.page_end:
                problems.append(f"{what}: block {block_id} missing or off-page")
                return
        if any(i not in items for i in p.evidence_ids):
            problems.append(f"{what}: Box 3 evidence item missing")
        if text is not None and p.block_ids:
            source = _squeeze(" ".join(blocks[b].normalized_text for b in p.block_ids))
            if _squeeze(text) not in source:
                problems.append(f"{what}: text not found in its Box 3 blocks")

    events = {e.event_id: e for e in dataset.events}
    for e in dataset.events:
        ex = by_document.get(e.document_id)
        if ex is None or ex.artifact_sha256 != e.artifact_sha256:
            problems.append(f"event {e.event_id}: document / artifact missing")
    people = {p.participant_id: p for p in dataset.participants}
    for person in dataset.participants:
        if person.provenance is not None:
            check(f"participant {person.participant_id}", person.provenance)
    turns = {t.turn_id: t for t in dataset.turns}
    for t in dataset.turns:
        check(f"turn {t.turn_id}", t.provenance, t.text[:200] if t.provenance.block_ids else None)
        if events[t.event_id].document_id != t.provenance.document_id:
            problems.append(f"turn {t.turn_id}: provenance from another document")
    statements = {s.statement_id: s for s in dataset.statements}
    for s in dataset.statements:
        check(f"statement {s.statement_id}", s.provenance, s.quote)
        turn = turns.get(s.turn_id)
        if turn is None or turn.participant_id != s.speaker_id or turn.event_id != s.event_id:
            problems.append(f"statement {s.statement_id}: turn / speaker mismatch")
        elif people[s.speaker_id].role != s.speaker_role:
            problems.append(f"statement {s.statement_id}: speaker role mismatch")
    guidance = {g.guidance_id: g for g in dataset.guidance}
    for g in dataset.guidance:
        for sid in g.statement_ids:
            statement = statements.get(sid)
            if statement is None or statement.event_id != g.event_id:
                problems.append(f"guidance {g.guidance_id}: statement {sid} missing")
            elif g.low is not None or g.value is not None:
                if _squeeze(g.raw_target_text) not in _squeeze(statement.quote):
                    problems.append(f"guidance {g.guidance_id}: target text not in the quote")
    box4_values = {v.canonical_value_id: v for v in box4.canonical_values}
    box4_observations = {o.observation_id for o in box4.observations}
    box5_metrics = {m.metric_id: m for m in box5.all_metrics()} if box5 else {}
    box7_kpis = {k.canonical_value_id: k for k in box7.canonical_kpi_values} if box7 else {}
    box7_segments = {a.analytic_id: a for a in box7.segment_analytics} if box7 else {}
    box7_kpi_obs = (
        {o.kpi_observation_id for o in box7.operating_kpi_observations} if box7 else set()
    )
    box7_seg_values = (
        {v.canonical_value_id for v in box7.canonical_segment_values} if box7 else set()
    )
    for o in dataset.outcomes:
        if o.guidance_id not in guidance:
            problems.append(f"outcome {o.outcome_id}: guidance missing")
        a = o.actual
        if a is None:
            continue
        what = f"outcome {o.outcome_id} actual {a.record_id}"
        if a.layer == SubjectLayer.BOX4_METRIC:
            v4 = box4_values.get(a.record_id)
            if v4 is None or v4.value != a.value or not set(a.upstream_ids) <= box4_observations:
                problems.append(f"{what}: Box 4 value missing / changed")
        elif a.layer == SubjectLayer.BOX5_METRIC:
            m5 = box5_metrics.get(a.record_id)
            if m5 is None or m5.value != a.value or not set(a.upstream_ids) <= set(box4_values):
                problems.append(f"{what}: Box 5 metric missing / changed / unresolved inputs")
        elif a.layer == SubjectLayer.BOX7_KPI:
            k7 = box7_kpis.get(a.record_id)
            if k7 is None or k7.value != a.value or not set(a.upstream_ids) <= box7_kpi_obs:
                problems.append(f"{what}: Box 7 KPI missing / changed")
        elif a.layer == SubjectLayer.BOX7_SEGMENT:
            s7 = box7_segments.get(a.record_id)
            if s7 is None or s7.value != a.value or not set(a.upstream_ids) <= box7_seg_values:
                problems.append(f"{what}: Box 7 segment analytic missing / changed")
        else:
            problems.append(f"{what}: unknown actual layer")
    for q in dataset.qa_pairs:
        for tid in (*q.question_turn_ids, *q.answer_turn_ids):
            if turns[tid].event_id != q.event_id:
                problems.append(f"Q&A {q.qa_pair_id}: turn from another event")
    for u in dataset.unparsed:
        check(f"unparsed {u.item_id}", u.provenance)
    return problems
