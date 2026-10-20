"""DETAILED_RESEARCH_V1 Box 7: BusinessIntelligenceDataset assembly.

Box 1B profile + Box 3 evidence + Box 4 canonical dataset
  -> business evidence, segment / geography / KPI observations, project facts
  -> explicit names, lineage and structure comparability
  -> reconciliation (canonical values, total bridges, Box 4 references)
  -> segment analytics -> BusinessIntelligenceDataset (deterministic, provenance-complete).
"""

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from uuid import UUID, uuid5

from stock_research_ai.application.business_analytics import (
    FORMULAS,
    Builder,
    geography_analytics,
    kpi_analytics,
    segment_analytics,
)
from stock_research_ai.application.business_extraction import Extractor, box4_segments
from stock_research_ai.application.business_lineage import (
    SegmentAlias,
    lineage_events,
    name_relations,
    presentations,
    structures_and_comparability,
)
from stock_research_ai.application.business_lines import NS
from stock_research_ai.application.business_reconciliation import (
    canonical_geographies,
    canonical_kpis,
    canonical_segments,
    geography_totals,
    segment_totals,
)
from stock_research_ai.application.business_registry import sector_family
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.domain.business_intelligence import (
    BUSINESS_SCHEMA,
    FORMULA_VERSION,
    REGISTRY_VERSION,
    AnalyticStatus,
    BusinessEvidence,
    BusinessEvidenceKind,
    BusinessIntelligenceDataset,
    BusinessLimits,
    BusinessStatistics,
    BusinessStructure,
    BusinessWarning,
    CanonicalGeographyValue,
    CanonicalKPIValue,
    CanonicalSegmentValue,
    CapacityFact,
    CapacityStatus,
    ClassificationRef,
    Comparability,
    Domain,
    GeographyDefinition,
    GeographyObservation,
    LineageKind,
    NameMatch,
    OperatingKPIObservation,
    OrderBookFact,
    ReconOutcome,
    Restated,
    SectorFamily,
    SegmentAnalytic,
    SegmentDefinition,
    SegmentLevel,
    SegmentObservation,
    SegmentRole,
    Severity,
    SourceKind,
    SourceLocation,
    TotalOutcome,
    UpstreamReference,
)
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.evidence import AUTHORITY_RANK, EvidenceCatalogue, SourceAuthority
from stock_research_ai.domain.financial_canonical import CanonicalFinancialDataset, Confidence
from stock_research_ai.domain.screener_dataset import DetailedScreenerDataset


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _profile_evidence(screener: DetailedScreenerDataset) -> list[BusinessEvidence]:
    out: list[BusinessEvidence] = []
    profile = screener.profile
    for field, text, kind in (
        ("about", profile.about, BusinessEvidenceKind.BUSINESS_DESCRIPTION),
        ("key_points", profile.key_points, BusinessEvidenceKind.PRODUCT_SERVICE),
    ):
        if not text:
            continue
        out.append(
            BusinessEvidence(
                evidence_id=uuid5(NS, f"evidence|box1b|{screener.source.content_sha256}|{field}"),
                company_key=screener.company_key,
                evidence_kind=kind,
                raw_text=text[:4000],
                normalized_text=" ".join(text.split())[:4000],
                section=None,
                source=SourceLocation(
                    source_kind=SourceKind.BOX1B_PROFILE,
                    source_url=str(screener.source.page_url),
                    artifact_sha256=screener.source.content_sha256,
                    authority=SourceAuthority.SECONDARY_AGGREGATOR,
                    locator=f"profile.{field}",
                ),
                confidence=Confidence.MEDIUM,
                tags=(kind.value, "BOX1B_PROFILE"),
                metadata={"field": field},
            )
        )
    return out


def build_business_intelligence(
    *,
    box4: CanonicalFinancialDataset,
    bundle: EvidenceBundle | None,
    catalogue: EvidenceCatalogue | None,
    screener: DetailedScreenerDataset | None,
    generated_at: datetime,
    limits: BusinessLimits | None = None,
    aliases: Sequence[SegmentAlias] = (),
) -> BusinessIntelligenceDataset:
    bounds = limits or BusinessLimits()
    if screener is not None and screener.company_key != box4.company_key:
        raise ValueError("the Box 1B dataset belongs to another company")
    if bundle is not None and bundle.company_key != box4.company_key:
        raise ValueError("the Box 3 bundle belongs to another company")
    classification = list(screener.profile.classification) if screener else []
    family, family_reason = sector_family(box4.industry, [c.name for c in classification],
                                          box4.classification)
    extractor = Extractor(
        company_key=box4.company_key,
        family=family,
        fy_end=box4.fiscal_year_end_month,
        catalogue=catalogue or EvidenceCatalogue.model_construct(documents=()),
        limits=bounds,
    )
    if bundle is not None and catalogue is not None:
        extractor.run(bundle)
    else:
        extractor.out.warn("SOURCE_MISSING", Severity.HIGH, "no Box 3 evidence bundle supplied")
    out = extractor.out
    if screener is not None:
        out.evidence.extend(_profile_evidence(screener))
    else:
        out.warn("SOURCE_MISSING", Severity.LOW, "no Box 1B profile supplied")
    out.segments.extend(box4_segments(box4, extractor))

    # the latest period each document reports (restatement / lineage ordering)
    reference: dict[UUID, date] = {}
    observed: list[SegmentObservation | GeographyObservation | OperatingKPIObservation] = [
        *out.segments,
        *out.geographies,
        *out.kpis,
    ]
    for item in observed:
        doc = item.source.document_id
        period = item.period
        end = period.period_end if period is not None else None
        if doc is not None and end is not None and (doc not in reference or end > reference[doc]):
            reference[doc] = end

    known = {
        o.segment_key
        for o in out.segments
        if o.segment_role in (SegmentRole.REPORTABLE, SegmentRole.SUBSEGMENT)
    }
    events = lineage_events(out.evidence, reference, known)
    mapping, relations = name_relations(events, aliases, known)

    def resolve(key: str) -> str:
        seen = {key}
        while key in mapping and mapping[key] not in seen:
            key = mapping[key]
            seen.add(key)
        return key

    canonical_key = {o.observation_id: resolve(o.segment_key) for o in out.segments}
    display: dict[str, str] = {}
    for o in sorted(
        out.segments,
        key=lambda o: (
            canonical_key[o.observation_id] != o.segment_key,
            AUTHORITY_RANK[o.source.authority],
            -(
                reference.get(o.source.document_id, date.min).toordinal()
                if o.source.document_id
                else 0
            ),
            str(o.observation_id),
        ),
    ):
        display.setdefault(canonical_key[o.observation_id], o.segment_raw_name)
    segments = [
        o.model_copy(update={"segment_canonical_name": display[canonical_key[o.observation_id]]})
        if o.segment_role in (SegmentRole.REPORTABLE, SegmentRole.SUBSEGMENT)
        else o
        for o in out.segments
    ]
    segments.sort(key=lambda o: str(o.observation_id))

    def structure_changed(old: date | None, new: date | None) -> bool:
        if old is None or new is None:
            return False
        return any(
            e.kind != LineageKind.RENAMED
            and e.effective_period_end is not None
            and old < e.effective_period_end <= new
            and e.restated_comparatives != Restated.YES
            for e in events
        )

    seg_records, seg_values = canonical_segments(
        segments, lambda o: canonical_key[o.observation_id], reference, structure_changed, display
    )
    geographies = sorted(out.geographies, key=lambda o: str(o.observation_id))
    geo_records, geo_values = canonical_geographies(geographies, reference)
    kpis = sorted(out.kpis, key=lambda o: str(o.kpi_observation_id))
    kpi_records, kpi_values, box4_conflicts = canonical_kpis(kpis, reference, box4)

    presented = presentations(segments, canonical_key, reference, out.restated_frames)
    renamed_pairs = {(r.from_key, r.to_key) for r in relations if r.outcome == NameMatch.RENAMED}
    structures, comparability, possible = structures_and_comparability(
        presented, events, renamed_pairs
    )
    relations = sorted([*relations, *possible], key=lambda r: str(r.relation_id))
    financial = family in (SectorFamily.FINANCIAL, SectorFamily.UNKNOWN)  # restricted
    totals = [*segment_totals(segments, box4, financial), *geography_totals(geographies, box4)]

    canonical_of: dict[UUID, UUID] = {}
    all_values: list[CanonicalSegmentValue | CanonicalGeographyValue | CanonicalKPIValue] = [
        *seg_values,
        *geo_values,
        *kpi_values,
    ]
    for value in all_values:
        for observation in (value.selected_observation_id, *value.supporting_observation_ids):
            canonical_of[observation] = value.canonical_value_id
    builder = Builder([], canonical_of)
    segment_analytics(
        segments, canonical_key, presented, comparability, seg_values, totals, builder,
        industrial=not financial,
    )
    geography_analytics(geographies, geo_values, builder)
    kpi_analytics(kpi_values, builder)
    first: dict[UUID, SegmentAnalytic] = {}
    for analytic in builder.analytics:
        first.setdefault(analytic.analytic_id, analytic)
    analytics = sorted(first.values(), key=lambda a: str(a.analytic_id))
    if len(analytics) > bounds.max_analytics:
        out.partial = True
        out.warn(
            "RESOURCE_LIMIT_REACHED", Severity.MEDIUM, f"analytics limit {bounds.max_analytics}"
        )
        analytics = analytics[: bounds.max_analytics]

    capacity, order_book = _facts(kpis)
    projects = sorted(out.projects, key=lambda p: str(p.project_id))
    evidence = sorted(
        {e.evidence_id: e for e in out.evidence}.values(), key=lambda e: str(e.evidence_id)
    )
    unmapped = sorted({u.line_id: u for u in out.unmapped}.values(), key=lambda u: str(u.line_id))

    warnings = list(out.warnings)
    _warn_summary(warnings, segments, geographies, kpis, unmapped, capacity, order_book)
    for record in [*seg_records, *geo_records, *kpi_records]:
        if record.outcome in (
            ReconOutcome.CONFLICT,
            ReconOutcome.SOURCE_PRECEDENCE,
            ReconOutcome.AMBIGUOUS,
        ):
            code = {
                Domain.SEGMENT: "SEGMENT_RECONCILIATION_CONFLICT",
                Domain.GEOGRAPHY: "GEOGRAPHY_RECONCILIATION_CONFLICT",
            }.get(record.domain, "KPI_RECONCILIATION_CONFLICT")
            severity = Severity.HIGH if record.outcome == ReconOutcome.CONFLICT else Severity.MEDIUM
            warnings.append(
                BusinessWarning(
                    code=code,
                    severity=severity,
                    detail=f"{record.outcome.value}: {record.key}"[:1000],
                )
            )
    for detail in box4_conflicts:
        warnings.append(
            BusinessWarning(
                code="KPI_RECONCILIATION_CONFLICT",
                severity=Severity.HIGH,
                detail=f"vs Box 4: {detail}"[:1000],
            )
        )
    for check in totals:
        if check.outcome == TotalOutcome.CONFLICT:
            code = (
                "SEGMENT_RECONCILIATION_CONFLICT"
                if check.domain == Domain.SEGMENT
                else "GEOGRAPHY_RECONCILIATION_CONFLICT"
            )
            warnings.append(
                BusinessWarning(
                    code=code,
                    severity=Severity.MEDIUM,
                    detail=f"total {check.metric} {check.period.period_end}: {check.explanation}"[
                        :1000
                    ],
                )
            )
    for assessment in comparability:
        if assessment.status == Comparability.STRUCTURE_CHANGED:
            warnings.append(
                BusinessWarning(
                    code="SEGMENT_STRUCTURE_CHANGED",
                    severity=Severity.MEDIUM,
                    detail=f"{assessment.reporting_basis.value} {assessment.frequency} "
                    f"{assessment.from_period_end} -> {assessment.to_period_end}: "
                    f"+{list(assessment.added_segments)} -{list(assessment.removed_segments)}"[
                        :1000
                    ],
                )
            )
    for relation in relations:
        if relation.outcome in (NameMatch.POSSIBLE_RENAME, NameMatch.UNKNOWN):
            warnings.append(
                BusinessWarning(
                    code="SEGMENT_ALIAS_AMBIGUOUS",
                    severity=Severity.LOW,
                    detail=f"{relation.from_key} -> {relation.to_key}: {relation.reason}"[:1000],
                )
            )
    if not segments and not geographies and not kpis:
        warnings.append(
            BusinessWarning(
                code="PARTIAL_BUSINESS_DATA",
                severity=Severity.HIGH,
                detail="no segment, geography or KPI table interpreted",
            )
        )
    warnings = sorted(set(warnings), key=lambda w: (w.code, w.detail or "", w.severity.value))

    definitions = _segment_definitions(segments, canonical_key, display, relations, evidence)
    structure = BusinessStructure(
        company_key=box4.company_key,
        company_name=box4.company_name,
        sector_family=family,
        sector_reason=family_reason,
        industry=box4.industry,
        classification=tuple(
            ClassificationRef(level=c.level, name=c.name, source_url=str(c.url))
            for c in classification
        ),
        reporting_bases=tuple(sorted({o.reporting_basis for o in segments}, key=lambda b: b.value)),
        segments=definitions,
        geographies=_geography_definitions(geographies),
        description_evidence_ids=_ids(evidence, BusinessEvidenceKind.BUSINESS_DESCRIPTION),
        product_service_evidence_ids=_ids(evidence, BusinessEvidenceKind.PRODUCT_SERVICE),
        end_market_evidence_ids=_ids(evidence, BusinessEvidenceKind.END_MARKET),
        customer_channel_evidence_ids=_ids(
            evidence,
            BusinessEvidenceKind.CUSTOMER_CHANNEL,
            BusinessEvidenceKind.CUSTOMER_CONCENTRATION,
        ),
        segment_description_evidence_ids=_ids(
            evidence, BusinessEvidenceKind.SEGMENT_DESCRIPTION, BusinessEvidenceKind.SEGMENT_CHANGE
        ),
    )
    box4_sha = _sha(canonical_dataset_json(box4))
    source_box3 = (
        UpstreamReference(
            layer="BOX3",
            dataset_id=bundle.bundle_id,
            sha256=_sha(bundle.model_dump_json()),
            schema_version=bundle.schema_version,
        )
        if bundle is not None
        else None
    )
    source_box1b = (
        UpstreamReference(
            layer="BOX1B",
            dataset_id=None,
            sha256=screener.source.content_sha256,
            schema_version=screener.schema_version,
        )
        if screener is not None
        else None
    )
    dataset_id = uuid5(
        NS,
        "|".join(
            [
                BUSINESS_SCHEMA,
                REGISTRY_VERSION,
                FORMULA_VERSION,
                box4_sha,
                source_box3.sha256 if source_box3 else "-",
                _sha(catalogue.model_dump_json()) if catalogue is not None else "-",
                source_box1b.sha256 if source_box1b else "-",
                bounds.model_dump_json(),
                json.dumps([[a.alias, a.canonical, a.evidence] for a in aliases]),
            ]
        ),
    )
    records = sorted([*seg_records, *geo_records, *kpi_records], key=lambda r: str(r.record_id))
    all_kpi_codes = {o.canonical_kpi for o in kpis}
    statistics = BusinessStatistics(
        documents_consumed=out.documents,
        frames_interpreted=out.frames,
        business_evidence_items=len(evidence),
        segment_observations=len(segments),
        canonical_segment_values=len(seg_values),
        geography_observations=len(geographies),
        canonical_geography_values=len(geo_values),
        kpi_observations=len(kpis),
        canonical_kpi_values=len(kpi_values),
        capacity_facts=len(capacity),
        order_book_facts=len(order_book),
        project_facts=len(projects),
        mapped_kpis=len(all_kpi_codes),
        unmapped_kpis=sum(1 for u in unmapped if u.domain == Domain.KPI),
        unmapped_lines=len(unmapped),
        segment_conflicts=sum(
            1
            for r in seg_records
            if r.outcome
            in (ReconOutcome.CONFLICT, ReconOutcome.SOURCE_PRECEDENCE, ReconOutcome.AMBIGUOUS)
        ),
        geography_conflicts=sum(
            1
            for r in geo_records
            if r.outcome
            in (ReconOutcome.CONFLICT, ReconOutcome.SOURCE_PRECEDENCE, ReconOutcome.AMBIGUOUS)
        ),
        kpi_conflicts=sum(
            1
            for r in kpi_records
            if r.outcome
            in (ReconOutcome.CONFLICT, ReconOutcome.SOURCE_PRECEDENCE, ReconOutcome.AMBIGUOUS)
        )
        + len(box4_conflicts),
        segment_structure_changes=sum(
            1 for a in comparability if a.status == Comparability.STRUCTURE_CHANGED
        ),
        analytics_calculated=sum(1 for a in analytics if a.status == AnalyticStatus.CALCULATED),
        analytics_not_calculable=sum(1 for a in analytics if a.status != AnalyticStatus.CALCULATED),
        box4_segment_observations_considered=out.box4_considered,
        box4_segment_observations_accepted=out.box4_accepted,
        warnings_by_code=dict(sorted(Counter(w.code for w in warnings).items())),
    )
    return BusinessIntelligenceDataset(
        dataset_id=dataset_id,
        company_key=box4.company_key,
        company_name=box4.company_name,
        sector=family,
        generated_at=generated_at,
        source_box1b=source_box1b,
        source_box3=source_box3,
        source_box4=UpstreamReference(
            layer="BOX4",
            dataset_id=box4.dataset_id,
            sha256=box4_sha,
            schema_version=box4.schema_version,
        ),
        limits=bounds,
        status="PARTIAL" if out.partial else "COMPLETE",
        business_structure=structure,
        business_evidence=tuple(evidence),
        segment_observations=tuple(segments),
        canonical_segment_values=tuple(sorted(seg_values, key=lambda v: str(v.canonical_value_id))),
        segment_name_relations=tuple(relations),
        segment_structures=tuple(structures),
        segment_comparability=tuple(comparability),
        segment_lineage=tuple(sorted(events, key=lambda e: str(e.event_id))),
        geography_observations=tuple(geographies),
        canonical_geography_values=tuple(
            sorted(geo_values, key=lambda v: str(v.canonical_value_id))
        ),
        operating_kpi_observations=tuple(kpis),
        canonical_kpi_values=tuple(sorted(kpi_values, key=lambda v: str(v.canonical_value_id))),
        capacity_facts=tuple(capacity),
        order_book_facts=tuple(order_book),
        project_facts=tuple(projects),
        formulas=FORMULAS,
        segment_analytics=tuple(analytics),
        reconciliation_records=tuple(records),
        total_reconciliations=tuple(sorted(totals, key=lambda t: str(t.check_id))),
        unmapped_lines=tuple(unmapped),
        warnings=tuple(warnings),
        statistics=statistics,
    )


def _ids(evidence: Sequence[BusinessEvidence], *kinds: BusinessEvidenceKind) -> tuple[UUID, ...]:
    return tuple(e.evidence_id for e in evidence if e.evidence_kind in kinds)


def _facts(
    kpis: Sequence[OperatingKPIObservation],
) -> tuple[list[CapacityFact], list[OrderBookFact]]:
    capacity: list[CapacityFact] = []
    order_book: list[OrderBookFact] = []
    for o in kpis:
        if o.canonical_kpi == "CAPACITY":
            capacity.append(
                CapacityFact(
                    fact_id=uuid5(NS, f"capacity|{o.kpi_observation_id}"),
                    capacity_status=CapacityStatus(o.qualifier or "UNKNOWN"),
                    value=o.normalized_value,
                    unit=o.unit,
                    raw_unit=o.raw_unit,
                    as_of=o.as_of,
                    period=o.period,
                    segment_key=o.segment_key,
                    site=None,
                    geography_key=o.geography_key,
                    raw_label=o.raw_label,
                    kpi_observation_id=o.kpi_observation_id,
                    source=o.source,
                    confidence=o.confidence,
                )
            )
        elif o.canonical_kpi in ("ORDER_BOOK", "ORDER_INFLOW", "BACKLOG"):
            scope = o.segment_raw_name or o.geography_raw_name or "COMPANY"
            executable = next(
                (d for d in (o.definition_text or "").split("; ") if "execut" in d.lower()), None
            )
            order_book.append(
                OrderBookFact(
                    fact_id=uuid5(NS, f"orderbook|{o.kpi_observation_id}"),
                    kind=o.canonical_kpi,  # type: ignore[arg-type]
                    value=o.parsed.value,
                    currency=o.currency,
                    scale=o.scale,
                    normalized_value=o.normalized_value,
                    as_of=o.as_of if o.canonical_kpi != "ORDER_INFLOW" else None,
                    period=o.period,
                    scope=scope,
                    definition=o.definition_text,
                    executable_period=executable,
                    raw_label=o.raw_label,
                    kpi_observation_id=o.kpi_observation_id,
                    source=o.source,
                    confidence=o.confidence,
                )
            )
    return capacity, order_book


def _warn_summary(
    warnings: list[BusinessWarning],
    segments: Sequence[SegmentObservation],
    geographies: Sequence[GeographyObservation],
    kpis: Sequence[OperatingKPIObservation],
    unmapped: Sequence[object],
    capacity: Sequence[CapacityFact],
    order_book: Sequence[OrderBookFact],
) -> None:
    from stock_research_ai.domain.business_intelligence import UnmappedOperatingLine

    counts: Counter[str] = Counter()
    observed: list[SegmentObservation | GeographyObservation | OperatingKPIObservation] = [
        *segments,
        *geographies,
        *kpis,
    ]
    for o in observed:
        for code in o.warnings:
            counts[code] += 1
    mapping = {
        "UNKNOWN_GEOGRAPHY": "UNKNOWN_GEOGRAPHY",
        "GEOGRAPHY_ATTRIBUTION_UNKNOWN": "GEOGRAPHY_ATTRIBUTION_UNKNOWN",
        "UNKNOWN_KPI_UNIT": "UNKNOWN_KPI_UNIT",
        "UNKNOWN_KPI_PERIOD": "UNKNOWN_KPI_PERIOD",
        "UNKNOWN_BASIS": "LOW_CONFIDENCE_SOURCE",
        "UNKNOWN_UNIT": "LOW_CONFIDENCE_SOURCE",
    }
    for code, count in sorted(counts.items()):
        if code in mapping:
            warnings.append(
                BusinessWarning(
                    code=mapping[code],
                    severity=Severity.LOW,
                    detail=f"{code}: {count} observation(s)",
                )
            )
    reasons = Counter(u.reason for u in unmapped if isinstance(u, UnmappedOperatingLine))
    for reason, count in sorted(reasons.items()):
        code = (
            "UNKNOWN_KPI"
            if reason in ("UNKNOWN_KPI", "ALIAS_AMBIGUOUS")
            else (
                "UNKNOWN_SEGMENT"
                if reason.startswith(("SEGMENT", "MATRIX"))
                else "PARTIAL_BUSINESS_DATA"
            )
        )
        warnings.append(
            BusinessWarning(
                code=code, severity=Severity.LOW, detail=f"{reason}: {count} unmapped line(s)"
            )
        )
    unknown_status = sum(1 for c in capacity if c.capacity_status == CapacityStatus.UNKNOWN)
    if unknown_status:
        warnings.append(
            BusinessWarning(
                code="CAPACITY_STATUS_UNKNOWN",
                severity=Severity.LOW,
                detail=f"{unknown_status} capacity fact(s)",
            )
        )
    undated = sum(1 for f in order_book if f.kind != "ORDER_INFLOW" and f.as_of is None)
    if undated:
        warnings.append(
            BusinessWarning(
                code="ORDER_BOOK_DATE_UNKNOWN",
                severity=Severity.MEDIUM,
                detail=f"{undated} order-book fact(s)",
            )
        )
    low = sum(1 for o in observed if o.confidence == Confidence.LOW)
    if low:
        warnings.append(
            BusinessWarning(
                code="LOW_CONFIDENCE_SOURCE",
                severity=Severity.LOW,
                detail=f"{low} low-confidence observation(s) kept, never canonical",
            )
        )


def _segment_definitions(
    segments: Sequence[SegmentObservation],
    canonical_key: dict[UUID, str],
    display: dict[str, str],
    relations: Sequence[object],
    evidence: Sequence[BusinessEvidence],
) -> tuple[SegmentDefinition, ...]:
    from stock_research_ai.application.business_registry import name_key
    from stock_research_ai.domain.business_intelligence import SegmentNameRelation

    grouped: dict[str, list[SegmentObservation]] = {}
    for o in segments:
        if o.segment_role in (SegmentRole.REPORTABLE, SegmentRole.SUBSEGMENT):
            grouped.setdefault(canonical_key[o.observation_id], []).append(o)
    renamed = {
        r.to_key
        for r in relations
        if isinstance(r, SegmentNameRelation) and r.outcome == NameMatch.RENAMED
    }
    aliased = {
        r.to_key: r.reason
        for r in relations
        if isinstance(r, SegmentNameRelation) and r.outcome == NameMatch.EXPLICIT_ALIAS
    }
    descriptions = [
        e for e in evidence if e.evidence_kind == BusinessEvidenceKind.SEGMENT_DESCRIPTION
    ]
    out: list[SegmentDefinition] = []
    for key, members in sorted(grouped.items()):
        children = sorted(
            {canonical_key[o.observation_id] for o in segments if o.segment_parent_key == key}
        )
        parent = next((o.segment_parent_key for o in members if o.segment_parent_key), None)
        ends = [o.period.period_end for o in members if o.period.period_end]
        match = (
            NameMatch.RENAMED
            if key in renamed
            else NameMatch.EXPLICIT_ALIAS
            if key in aliased
            else NameMatch.EXACT
        )
        name = display.get(key, members[0].segment_raw_name)
        described = tuple(
            e.evidence_id
            for e in descriptions
            if name_key(name) and f"{name_key(name)} include" in name_key(e.normalized_text)
        )
        out.append(
            SegmentDefinition(
                segment_key=key,
                canonical_name=name,
                raw_names=tuple(sorted({o.segment_raw_name for o in members})),
                role=SegmentRole.SUBSEGMENT if parent else SegmentRole.REPORTABLE,
                level=SegmentLevel.SUBSEGMENT if parent else (SegmentLevel.PRIMARY),
                parent_key=parent,
                child_keys=tuple(children),
                name_match=match,
                alias_evidence=(aliased[key],) if key in aliased else (),
                reporting_bases=tuple(
                    sorted({o.reporting_basis for o in members}, key=lambda b: b.value)
                ),
                first_period_end=min(ends) if ends else None,
                last_period_end=max(ends) if ends else None,
                observation_count=len(members),
                description_evidence_ids=described,
            )
        )
    return tuple(out)


def _geography_definitions(
    geographies: Sequence[GeographyObservation],
) -> tuple[GeographyDefinition, ...]:
    grouped: dict[str, list[GeographyObservation]] = {}
    for o in geographies:
        if not o.is_total:
            grouped.setdefault(o.geography_key, []).append(o)
    return tuple(
        GeographyDefinition(
            geography_key=key,
            name=members[0].geography_raw_name,
            raw_names=tuple(sorted({o.geography_raw_name for o in members})),
            dimension=members[0].dimension,
            attributions=tuple(sorted({o.attribution for o in members}, key=lambda a: a.value)),
        )
        for key, members in sorted(grouped.items())
    )


def canonical_business_json(dataset: BusinessIntelligenceDataset) -> str:
    return json.dumps(
        dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def verify_business_provenance(
    dataset: BusinessIntelligenceDataset,
    bundle: EvidenceBundle | None,
    box4: CanonicalFinancialDataset,
) -> list[str]:
    """Every observation / fact / evidence item walks back to its Box 3 block line (whose
    text holds the raw value), Box 3 evidence item, Box 4 observation or Box 1B page."""
    problems: list[str] = []
    extractions = {e.extraction_id: e for e in bundle.extractions} if bundle else {}
    blocks = {b.block_id: b for e in extractions.values() for b in e.blocks}
    items = {i.evidence_id for e in extractions.values() for i in e.evidence}
    box4_ids = {o.observation_id for o in box4.observations}

    def check(what: str, source: SourceLocation, raw: str | None) -> None:
        if source.source_kind == SourceKind.BOX4_OBSERVATION:
            if source.box4_observation_id not in box4_ids:
                problems.append(f"{what}: Box 4 observation missing")
            return
        if source.source_kind == SourceKind.BOX1B_PROFILE:
            if (
                dataset.source_box1b is None
                or dataset.source_box1b.sha256 != source.artifact_sha256
            ):
                problems.append(f"{what}: Box 1B artifact mismatch")
            return
        extraction = extractions.get(source.extraction_id) if source.extraction_id else None
        if extraction is None or extraction.artifact_sha256 != source.artifact_sha256:
            problems.append(f"{what}: extraction/artifact missing")
            return
        if extraction.document_id != source.document_id:
            problems.append(f"{what}: document mismatch")
        if source.source_kind == SourceKind.BOX3_EVIDENCE:
            if source.box3_evidence_id not in items:
                problems.append(f"{what}: Box 3 evidence item missing")
            return
        for block_id in source.block_ids:
            block = blocks.get(block_id)
            if block is None or not (source.page_start or 0) <= block.page_number <= (
                source.page_end or 0
            ):
                problems.append(f"{what}: block {block_id} missing or off-page")
                return
        if raw is not None and source.line_index is not None:
            block = blocks[source.block_ids[-1]]
            lines = block.normalized_text.split("\n")
            line = lines[source.line_index] if source.line_index < len(lines) else ""
            squeezed = "".join(line.split())
            if raw.replace(" ", "") not in squeezed:
                problems.append(f"{what}: raw value {raw!r} not on its line")

    for o in dataset.segment_observations:
        check(
            f"segment {o.observation_id}",
            o.source,
            o.raw_value if o.source.source_kind == SourceKind.BOX3_BLOCK else None,
        )
    for g in dataset.geography_observations:
        check(f"geography {g.observation_id}", g.source, g.raw_value)
    for k in dataset.operating_kpi_observations:
        check(f"kpi {k.kpi_observation_id}", k.source, k.raw_value)
    for e in dataset.business_evidence:
        check(f"evidence {e.evidence_id}", e.source, None)
    for p in dataset.project_facts:
        check(f"project {p.project_id}", p.source, None)
    for u in dataset.unmapped_lines:
        check(f"unmapped {u.line_id}", u.source, None)
    return problems
