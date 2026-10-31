"""DETAILED_RESEARCH_V1 Box 9: IndustryIntelligenceDataset assembly.

target + peer CompanyLayers (Box 1B, 4, 5, 7, 8 - each reused, never recomputed)
  -> candidates from the Box 1B peer table + explicit competitor mentions  (peer_selection)
  -> eligibility, bounded canonical peer set
  -> financial benchmarks, KPI benchmarks, segment comparisons            (peer_financials,
     per-peer dimension comparability                                      peer_kpis,
                                                                           peer_comparability)
  -> competitive mentions, market-position claims                         (competitive_evidence)
  -> structure observations, demand / pricing signals, themes, conflicts   (industry_structure)
  -> IndustryIntelligenceDataset (deterministic, bounded, provenance-verified).
"""

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import TypeVar
from uuid import UUID, uuid5

from stock_research_ai.application.business_intelligence import canonical_business_json
from stock_research_ai.application.competitive_evidence import (
    competitive_mentions,
    identity,
    market_claims,
    text_units,
)
from stock_research_ai.application.financial_analytics import canonical_analytics_json
from stock_research_ai.application.financial_reconciliation import canonical_dataset_json
from stock_research_ai.application.industry_structure import (
    industry_themes,
    market_signals,
    signal_conflicts,
    structure_observations,
)
from stock_research_ai.application.management_intelligence import canonical_management_json
from stock_research_ai.application.peer_comparability import assess
from stock_research_ai.application.peer_financials import financial_benchmarks, positions
from stock_research_ai.application.peer_kpis import kpi_benchmarks, segment_comparisons
from stock_research_ai.application.peer_registry import METRIC_INDEX, NS
from stock_research_ai.application.peer_selection import (
    NEUTRAL_ONLY,
    CompanyLayers,
    model_label,
    select_peers,
    symbol_of,
)
from stock_research_ai.application.screener_dataset import (
    canonical_dataset_json as canonical_screener_json,
)
from stock_research_ai.domain.document_intelligence import EvidenceBundle
from stock_research_ai.domain.industry_intelligence import (
    ENGINE_VERSION,
    INDUSTRY_SCHEMA,
    REGISTRY_VERSION,
    SELECTION_VERSION,
    EvidenceRef,
    IndustryIntelligenceDataset,
    IndustryLimits,
    IndustryStatistics,
    IndustryWarning,
    KPIEquivalence,
    ObservationStatus,
    Resolution,
    Severity,
    SourceLayer,
    UnresolvedItem,
    UpstreamReference,
)

T = TypeVar("T")
_SEVERITY = {
    "RESOURCE_LIMIT_REACHED": Severity.HIGH,
    "PEER_TABLE_MISSING": Severity.HIGH,
    "SOURCE_MISSING": Severity.MEDIUM,
    "NO_CANONICAL_PEERS": Severity.MEDIUM,
    "TARGET_BASIS_UNAVAILABLE": Severity.MEDIUM,
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def references(company: CompanyLayers) -> list[UpstreamReference]:
    out = []
    key = company.company_key
    if company.screener is not None:
        out.append(
            UpstreamReference(
                company_key=key,
                layer="BOX1B",
                dataset_id=None,
                sha256=_sha(canonical_screener_json(company.screener)),
                schema_version=company.screener.schema_version,
            )
        )
    if company.box4 is not None:
        out.append(
            UpstreamReference(
                company_key=key,
                layer="BOX4",
                dataset_id=company.box4.dataset_id,
                sha256=_sha(canonical_dataset_json(company.box4)),
                schema_version=company.box4.schema_version,
            )
        )
    if company.box5 is not None:
        out.append(
            UpstreamReference(
                company_key=key,
                layer="BOX5",
                dataset_id=company.box5.analytics_id,
                sha256=_sha(canonical_analytics_json(company.box5)),
                schema_version=company.box5.schema_version,
            )
        )
    if company.box7 is not None:
        out.append(
            UpstreamReference(
                company_key=key,
                layer="BOX7",
                dataset_id=company.box7.dataset_id,
                sha256=_sha(canonical_business_json(company.box7)),
                schema_version=company.box7.schema_version,
            )
        )
    if company.box8 is not None:
        out.append(
            UpstreamReference(
                company_key=key,
                layer="BOX8",
                dataset_id=company.box8.dataset_id,
                sha256=_sha(canonical_management_json(company.box8)),
                schema_version=company.box8.schema_version,
            )
        )
    return out


def build_industry_intelligence(
    *,
    target: CompanyLayers,
    peers: Sequence[CompanyLayers],
    generated_at: datetime,
    limits: IndustryLimits | None = None,
) -> IndustryIntelligenceDataset:
    bounds = limits or IndustryLimits()
    for layer in (target.screener, target.box4, target.box5, target.box7, target.box8):
        if layer is not None and layer.company_key != target.company_key:
            raise ValueError("a target dataset belongs to another company")
    by_key = {p.company_key: p for p in sorted(peers, key=lambda p: p.company_key)}
    warnings: list[tuple[str, str | None]] = []
    partial = False
    family, _family_reason = target.family()
    family = family or "UNKNOWN"  # never assumed GENERAL
    for name, layer in (
        ("Box 4", target.box4),
        ("Box 5", target.box5),
        ("Box 7", target.box7),
        ("Box 8", target.box8),
    ):
        if layer is None:
            warnings.append(("SOURCE_MISSING", f"no target {name} dataset"))

    # identity: the target and every company of its peer table (names + registered aliases)
    known: dict[str, tuple[str | None, str | None]] = {
        target.company_key: (
            target.screener.profile.name if target.screener else None,
            target.symbol(),
        ),
    }
    table = target.screener.peers if target.screener is not None else None
    for row in table.peers if table is not None else ():
        if row.screener_company_id and not row.is_subject_company:
            known[f"screener:{row.screener_company_id}"] = (
                row.name,
                symbol_of(str(row.company_url) if row.company_url else None),
            )
    ident = identity(known)
    self_keys = {key: {key} for key in known}
    target_units = text_units(target, bounds.max_excerpt_chars)
    target_mentions = competitive_mentions(target_units, ident, self_keys)
    mentioned: dict[str, list[UUID]] = {}
    for m in target_mentions:
        if m.mentioned_company_key is not None:
            mentioned.setdefault(m.mentioned_company_key, []).append(m.mention_id)

    selection = select_peers(target, by_key, mentioned, bounds.max_peers, bounds.max_candidates)
    warnings.extend(selection.warnings)
    if any(w[0] == "RESOURCE_LIMIT_REACHED" for w in selection.warnings):
        partial = True
    peer_set = selection.peer_set
    assert peer_set is not None
    candidates = {c.peer_candidate_id: c for c in selection.candidates}
    canonical_ids = [*peer_set.included_ids, *peer_set.limited_ids]
    canonical_keys = [candidates[i].peer_company_key for i in canonical_ids]
    canonical = [by_key.get(k, CompanyLayers(company_key=k)) for k in canonical_keys]
    if not canonical:
        warnings.append(("NO_CANONICAL_PEERS", "no INCLUDED / INCLUDED_LIMITED peer"))

    # INCLUDED_LIMITED peers proven compatible at the business-class level only enter
    # business-model-neutral metrics only; EXCLUDED / UNKNOWN peers are never observed
    neutral_only = frozenset(
        candidates[i].peer_company_key
        for i in peer_set.limited_ids
        if NEUTRAL_ONLY & set(candidates[i].exclusion_reasons)
    )
    financial, blocked, basis_warnings = financial_benchmarks(
        target, canonical, family, bounds.min_peers_for_rank, neutral_only
    )
    warnings.extend(basis_warnings)
    kpis = kpi_benchmarks(target, canonical, bounds.min_peers_for_rank, neutral_only)
    segments = segment_comparisons(target, canonical)
    comparability = [
        assess(
            target,
            by_key.get(candidates[i].peer_company_key),
            candidates[i],
            financial,
            kpis,
            segments,
        )
        for i in canonical_ids
    ]
    name = target.name or target.company_key

    peer_units = [u for p in canonical for u in text_units(p, bounds.max_excerpt_chars)]
    units = target_units + peer_units
    mentions = target_mentions + competitive_mentions(peer_units, ident, self_keys)
    claims, claim_conflicts = market_claims(units, ident, self_keys)
    structure = structure_observations(units)
    demand, pricing = market_signals(units)
    conflicts = claim_conflicts + signal_conflicts(demand) + signal_conflicts(pricing)
    themes = industry_themes([target, *canonical])

    unresolved: list[UnresolvedItem] = []
    for m in mentions:
        if m.resolution == Resolution.AMBIGUOUS:
            unresolved.append(
                UnresolvedItem(
                    item_id=uuid5(NS, f"unresolved|{m.mention_id}"),
                    kind="AMBIGUOUS_COMPANY_ALIAS",
                    text=m.quote,
                    reason=f"{m.raw_mention!r} may mean {', '.join(m.candidates)}",
                    source=m.source,
                )
            )
    for c in selection.candidates:
        if c.eligibility.value == "UNKNOWN":
            unresolved.append(
                UnresolvedItem(
                    item_id=uuid5(NS, f"unresolved|{c.peer_candidate_id}"),
                    kind="PEER_UNRESOLVED",
                    text=c.raw_label,
                    reason=", ".join(c.exclusion_reasons),
                    source=None,
                )
            )
    for k in kpis:
        for o in k.peers:
            if o.equivalence == KPIEquivalence.NOT_COMPARABLE:
                unresolved.append(
                    UnresolvedItem(
                        item_id=uuid5(NS, f"unresolved|{k.benchmark_id}|{o.company_key}"),
                        kind="KPI_DEFINITION_MISMATCH",
                        text=f"{k.kpi_code}: {o.detail}",
                        reason="same canonical KPI with a different qualifier / unit",
                        source=o.source,
                    )
                )

    def bound(items: list[T], limit: int, what: str) -> list[T]:
        nonlocal partial
        if len(items) > limit:
            warnings.append(("RESOURCE_LIMIT_REACHED", f"{what}: {len(items)} > {limit}"))
            partial = True
            return items[:limit]
        return items

    financial = bound(financial, bounds.max_benchmarks, "financial_benchmarks")
    kpis = bound(kpis, bounds.max_kpi_benchmarks, "kpi_benchmarks")
    segments = bound(segments, bounds.max_segment_comparisons, "segment_comparisons")
    mentions = bound(mentions, bounds.max_mentions, "competitive_mentions")
    kept_mentions = {m.mention_id for m in mentions}
    candidate_list = [
        c
        if set(c.mention_ids) <= kept_mentions
        else c.model_copy(
            update={"mention_ids": tuple(i for i in c.mention_ids if i in kept_mentions)}
        )
        for c in selection.candidates
    ]
    claims = bound(claims, bounds.max_claims, "market_claims")
    kept_claims = {c.claim_id for c in claims}
    claims = [
        c
        if {*c.corroborating_ids, *c.conflicting_ids} <= kept_claims
        else c.model_copy(
            update={
                "corroborating_ids": tuple(i for i in c.corroborating_ids if i in kept_claims),
                "conflicting_ids": tuple(i for i in c.conflicting_ids if i in kept_claims),
            }
        )
        for c in claims
    ]
    structure = bound(structure, bounds.max_structure_observations, "structure_observations")
    demand = bound(demand, bounds.max_signals, "demand_signals")
    pricing = bound(pricing, bounds.max_signals, "pricing_signals")
    themes = bound(themes, bounds.max_themes, "themes")
    known_records = (
        kept_claims
        | {s.signal_id for s in (*demand, *pricing)}
        | {o.observation_id for o in structure}
        | kept_mentions
    )
    conflicts = bound(
        [c for c in conflicts if {c.record_a, c.record_b} <= known_records],
        bounds.max_conflicts,
        "conflicts",
    )
    unresolved = bound(unresolved, bounds.max_unresolved, "unresolved")
    kept_benchmarks = {b.benchmark_id for b in financial}
    position_list = [p for p in positions(name, financial) if p.benchmark_id in kept_benchmarks]

    sources = references(target) + [r for p in canonical for r in references(p)]
    dataset_id = uuid5(
        NS,
        "|".join(
            [
                INDUSTRY_SCHEMA,
                ENGINE_VERSION,
                REGISTRY_VERSION,
                SELECTION_VERSION,
                bounds.model_dump_json(),
                *(f"{r.company_key}:{r.layer}:{r.sha256}" for r in sources),
            ]
        ),
    )
    structured = [
        IndustryWarning(
            code=code,
            severity=_SEVERITY.get(code, Severity.INFO),
            detail=detail[:1000] if detail else None,
        )
        for code, detail in sorted(set(warnings), key=lambda w: (w[0], w[1] or ""))
    ]
    statistics = IndustryStatistics(
        candidates=len(candidate_list),
        eligibility_counts=dict(
            sorted(Counter(c.eligibility.value for c in candidate_list).items())
        ),
        canonical_peers=len(canonical),
        financial_benchmarks=len(financial),
        ranked_benchmarks=sum(1 for b in financial if b.ranking is not None),
        kpi_benchmarks=len(kpis),
        kpi_equivalence_counts=dict(
            sorted(Counter(o.equivalence.value for k in kpis for o in k.peers).items())
        ),
        segment_comparisons=len(segments),
        competitive_mentions=len(mentions),
        mention_resolution_counts=dict(
            sorted(Counter(m.resolution.value for m in mentions).items())
        ),
        market_claims=len(claims),
        claim_status_counts=dict(sorted(Counter(c.status.value for c in claims).items())),
        structure_observations=len(structure),
        demand_signals=len(demand),
        pricing_signals=len(pricing),
        themes=len(themes),
        conflicts=len(conflicts),
        unresolved=len(unresolved),
        not_applicable_metrics=len(blocked),
        warnings_by_code=dict(sorted(Counter(w.code for w in structured).items())),
    )
    return IndustryIntelligenceDataset(
        dataset_id=dataset_id,
        company_key=target.company_key,
        company_name=name,
        industry=target.box4.industry.value if target.box4 is not None else "UNKNOWN",
        sector_family=family,
        classification=target.classification(),
        generated_at=generated_at,
        business_model=model_label(target.box4),
        sources=tuple(sources),
        limits=bounds,
        status="PARTIAL" if partial else "COMPLETE",
        candidates=tuple(candidate_list),
        peer_set=peer_set,
        comparability=tuple(comparability),
        financial_benchmarks=tuple(financial),
        not_applicable_metrics=tuple(blocked),
        kpi_benchmarks=tuple(kpis),
        segment_comparisons=tuple(segments),
        positions=tuple(position_list),
        competitive_mentions=tuple(mentions),
        market_claims=tuple(claims),
        structure_observations=tuple(structure),
        demand_signals=tuple(demand),
        pricing_signals=tuple(pricing),
        themes=tuple(themes),
        conflicts=tuple(conflicts),
        unresolved=tuple(unresolved),
        warnings=tuple(structured),
        statistics=statistics,
    )


def canonical_industry_json(dataset: IndustryIntelligenceDataset) -> str:
    return json.dumps(
        dataset.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _squeeze(text: str) -> str:
    return "".join(text.split())


def verify_industry_provenance(
    dataset: IndustryIntelligenceDataset,
    target: CompanyLayers,
    peers: Sequence[CompanyLayers],
    bundles: Sequence[EvidenceBundle] = (),
) -> list[str]:
    """Walk every Box 9 record back to the upstream record it names, check the value / text
    is still there, and - for Box 3 locations whose bundle is supplied - to the block text."""
    problems: list[str] = []
    companies = {c.company_key: c for c in (target, *peers)}
    blocks = {b.block_id: b for bundle in bundles for e in bundle.extractions for b in e.blocks}
    extractions = {e.extraction_id: e for bundle in bundles for e in bundle.extractions}

    # candidates -> the target's Box 1B peer-table row; classification -> the peer's Box 1B
    table = target.screener.peers if target.screener is not None else None
    for c in dataset.candidates:
        what = f"candidate {c.peer_candidate_id}"
        ref = c.table_ref
        if ref is not None:
            if (
                table is None
                or table.content_sha256 != ref.content_sha256
                or (ref.row_index >= len(table.peers))
            ):
                problems.append(f"{what}: peer table / row missing")
            else:
                row = table.peers[ref.row_index]
                if (
                    row.name != ref.displayed_name
                    or row.screener_company_id != (ref.screener_company_id)
                    or f"screener:{row.screener_company_id}" != c.peer_company_key
                ):
                    problems.append(f"{what}: peer-table row differs")
        peer = companies.get(c.peer_company_key)
        if c.source_classification:
            levels = {(x.level, x.name) for x in peer.classification()} if peer else set()
            if not {(x.level, x.name) for x in c.source_classification} <= levels:
                problems.append(f"{what}: classification not in the peer's Box 1B profile")

    def check_ref(
        what: str, ref: EvidenceRef | None, value: object = None, text: str | None = None
    ) -> None:
        if ref is None:
            return
        company = companies.get(ref.company_key)
        if company is None:
            problems.append(f"{what}: company {ref.company_key} not supplied")
            return
        layer = ref.layer
        if layer == SourceLayer.BOX5_METRIC:
            metrics = {m.metric_id: m for m in company.box5.all_metrics()} if company.box5 else {}
            m = metrics.get(ref.record_id)  # type: ignore[arg-type]
            values = (
                {v.canonical_value_id for v in company.box4.canonical_values}
                if company.box4
                else set()
            )
            if (
                m is None
                or (value is not None and m.value != value)
                or not (set(m.input_value_ids) <= values)
            ):
                problems.append(f"{what}: Box 5 metric missing / changed / unresolved inputs")
        elif layer == SourceLayer.BOX4_VALUE:
            cvs = (
                {v.canonical_value_id: v for v in company.box4.canonical_values}
                if (company.box4)
                else {}
            )
            obs = {o.observation_id for o in company.box4.observations} if company.box4 else set()
            v4 = cvs.get(ref.record_id)  # type: ignore[arg-type]
            if (
                v4 is None
                or (value is not None and v4.value != value)
                or (v4.selected_observation_id not in obs)
            ):
                problems.append(f"{what}: Box 4 value missing / changed")
        elif layer == SourceLayer.BOX7_KPI:
            kpis = (
                {k.canonical_value_id: k for k in company.box7.canonical_kpi_values}
                if (company.box7)
                else {}
            )
            obs7 = (
                {o.kpi_observation_id for o in company.box7.operating_kpi_observations}
                if company.box7
                else set()
            )
            k = kpis.get(ref.record_id)  # type: ignore[arg-type]
            if (
                k is None
                or (value is not None and k.value != value)
                or (k.selected_observation_id not in obs7)
            ):
                problems.append(f"{what}: Box 7 KPI missing / changed")
        elif layer == SourceLayer.BOX7_EVIDENCE:
            ev = {e.evidence_id: e for e in company.box7.business_evidence} if company.box7 else {}
            e = ev.get(ref.record_id)  # type: ignore[arg-type]
            if e is None or (
                text is not None and _squeeze(text) not in _squeeze(e.normalized_text)
            ):
                problems.append(f"{what}: Box 7 business evidence missing / text differs")
        elif layer == SourceLayer.BOX8_STATEMENT:
            st = {s.statement_id: s for s in company.box8.statements} if company.box8 else {}
            s = st.get(ref.record_id)  # type: ignore[arg-type]
            if s is None or (text is not None and _squeeze(text) not in _squeeze(s.quote)):
                problems.append(f"{what}: Box 8 statement missing / text differs")
        elif layer == SourceLayer.BOX8_TURN:
            tt = {t.turn_id: t for t in company.box8.turns} if company.box8 else {}
            t = tt.get(ref.record_id)  # type: ignore[arg-type]
            if t is None or (text is not None and _squeeze(text) not in _squeeze(t.text)):
                problems.append(f"{what}: Box 8 turn missing / text differs")
        else:
            problems.append(f"{what}: unexpected layer {layer.value}")
        loc = ref.location
        if loc is not None and extractions:
            ex = extractions.get(loc.extraction_id)
            if ex is None:
                return  # that company's bundle was not supplied
            if ex.artifact_sha256 != loc.artifact_sha256 or ex.document_id != loc.document_id:
                problems.append(f"{what}: Box 3 artifact mismatch")
                return
            found = [blocks[b] for b in loc.block_ids if b in blocks]
            if len(found) != len(loc.block_ids):
                problems.append(f"{what}: Box 3 block missing")
            elif text is not None and _squeeze(text) not in _squeeze(
                " ".join(b.normalized_text for b in found)
            ):
                problems.append(f"{what}: text not in its Box 3 blocks")

    for b in dataset.financial_benchmarks:
        for o in (b.target, *b.peers):
            if o.source is not None:
                check_ref(f"benchmark {b.metric_code} {o.company_key}", o.source, o.value)
    for kb in dataset.kpi_benchmarks:
        for k in (kb.target, *kb.peers):
            check_ref(f"kpi {kb.kpi_code} {k.company_key}", k.source, k.value)
    for m in dataset.competitive_mentions:
        check_ref(f"mention {m.mention_id}", m.source, text=m.quote)
    for claim in dataset.market_claims:
        check_ref(f"claim {claim.claim_id}", claim.source, text=claim.quote)
    for observation in dataset.structure_observations:
        check_ref(
            f"structure {observation.observation_id}",
            observation.source,
            text=observation.statement,
        )
    for s in (*dataset.demand_signals, *dataset.pricing_signals):
        check_ref(f"signal {s.signal_id}", s.source, text=s.quote)
    for u in dataset.unresolved:
        if u.source is not None and u.source.layer in (
            SourceLayer.BOX8_STATEMENT,
            SourceLayer.BOX8_TURN,
        ):
            check_ref(f"unresolved {u.item_id}", u.source, text=u.text)
    for theme in dataset.themes:
        ids = {
            t.theme_id
            for c in theme.companies
            if (x := companies.get(c)) and x.box8
            for t in x.box8.themes
        }
        if not set(theme.evidence_ids) <= ids:
            problems.append(f"theme {theme.topic}: Box 8 theme missing")
    # eligibility: only canonical peers' observations, with the target's exact definition
    # (neutral-only peers on business-model-neutral metrics only), enter a statistic
    by_key = {c.peer_company_key: c for c in dataset.candidates}
    for b in dataset.financial_benchmarks:
        neutral = METRIC_INDEX[b.metric_code].neutral if b.metric_code in METRIC_INDEX else False
        for o in b.peers:
            if o.status != ObservationStatus.COMPARABLE:
                continue
            seen = by_key.get(o.company_key)
            if seen is None or seen.eligibility.value not in ("INCLUDED", "INCLUDED_LIMITED"):
                problems.append(f"benchmark {b.metric_code}: non-canonical peer {o.company_key}")
            elif not neutral and NEUTRAL_ONLY & set(seen.exclusion_reasons):
                problems.append(f"benchmark {b.metric_code}: neutral-only peer {o.company_key}")
            if o.definition != b.definition:
                problems.append(f"benchmark {b.metric_code}: definition differs {o.company_key}")
    for b in dataset.financial_benchmarks:
        if b.ranking is not None:
            n = sum(1 for o in b.peers if o.status == ObservationStatus.COMPARABLE)
            if b.ranking.of != n + 1 or b.directionality.value == "NOT_REGISTERED":
                problems.append(f"benchmark {b.metric_code}: ranking inconsistent")
    return problems
