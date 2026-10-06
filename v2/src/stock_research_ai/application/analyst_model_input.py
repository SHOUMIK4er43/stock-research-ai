"""Exact-reference, bounded, provider-neutral projection. No IO or new arithmetic.

Explicit field allowlists deliberately exclude nested input observations, sibling
results, corpus pages and retrieval request parameters. No latest/search fallback.
"""

import calendar
import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from stock_research_ai.application.analysis import (
    ResolvedEvidence,
    _reference_status,
    build_source_references,
)
from stock_research_ai.application.evidence_matching import limitation_codes, source_epistemic_class
from stock_research_ai.application.hashing import digest
from stock_research_ai.domain.analysis import AnstInputBundle, EvidenceReference
from stock_research_ai.domain.analyst_model_input import AnalystModelEvidence, AnalystModelInput
from stock_research_ai.domain.calculations import CalculationResult
from stock_research_ai.domain.financial import FinancialPeriod

MAX_INPUT_BYTES = 400_000
MAX_ITEMS = 300
MAX_DOCUMENT_CHUNKS = 16
MAX_DOCUMENT_CHUNK_BYTES = 16_384

#: Conservative input-budget selection (Part 6D): recency caps applied per financial
#: sub-category before the byte/item ceilings are enforced. Not configurable at
#: runtime - a fixed, deterministic, auditable policy, matching the rest of this
#: module's "no config surface for evidence shape" convention.
_ANNUAL_PERIOD_CAP = 5
_QUARTERLY_PERIOD_CAP = 8
_SHAREHOLDING_PERIOD_CAP = 4
#: Floors (Part 6D-3): reserved *before* any family expands past them, so
#: profit_loss/quarterly cannot exhaust the budget before balance_sheet, cashflow
#: and ratios get any representation at all.
_ANNUAL_FLOOR_PERIODS = 2
_QUARTERLY_FLOOR_PERIODS = 4
_ANNUAL_FAMILY_ORDER = ("profit_loss", "balance_sheet", "cashflow", "ratios")
_FINANCIAL_CATEGORY_ORDER = (
    "top_metrics",
    "profit_loss",
    "quarterly",
    "balance_sheet",
    "cashflow",
    "ratios",
)
_CALCULATION_LIKE_KINDS = frozenset(
    {"FINANCIAL_CALCULATION", "SECTOR_KPI", "MARKET_CALCULATION", "VALUATION", "PATTERN"}
)
#: Selection targets a margin below MAX_INPUT_BYTES because the final serialized
#: payload also carries per-item wrapper fields (reference/status/epistemic_class/
#: limitations) and a second copy of every reference inside `bundle.sources` -
#: overhead this estimate approximates via _SELECTION_ITEM_OVERHEAD_BYTES rather than
#: fully re-deriving. serialize_model_input's own exact byte check remains the
#: authoritative final guard regardless of this estimate's accuracy.
_SELECTION_BYTE_TARGET_RATIO = 0.85
_SELECTION_ITEM_OVERHEAD_BYTES = 700

# Fields are copied from already normalized/calculated records, never recalculated.
_RESULT_FIELDS = {
    "FINANCIAL_CALCULATION": "classification snapshot_id extraction_id metric_id label formula "
    "alignment sector period comparison reporting_scope calculated_at status value display_value "
    "exact_numerator exact_denominator unit issues",
    "SECTOR_KPI": "classification snapshot_id extraction_id definition profile sector alignment "
    "period comparison reporting_scope calculated_at status value display_value exact_numerator "
    "exact_denominator issues",
    "PATTERN": "id snapshot_id definition evaluation_status rule_outcome observed issues statement",
    "MARKET_CALCULATION": "classification snapshot_id metric_id formula status calculated_at "
    "requested_start requested_end actual_start actual_end alignment_policy price_field "
    "adjustment_basis corporate_action_coverage corporate_action_reason issues value display_value "
    "exact_numerator exact_denominator numerical_method unit currency annualization_policy "
    "elapsed_days elapsed_years observation_count return_count annualization_factor "
    "return_definition "
    "variance_definition window episode company_price_return benchmark_price_return",
    "VALUATION": "id snapshot_id selection_index definition status assumption_ids scenario_id "
    "values solver issues",
}
_OBSERVATION_FIELDS = (
    "id company_id metric metric_id mapping_status original_label series_key publication_at "
    "source_revision value value_status unit period reporting_scope provenance original_value "
    "original_unit"
)


class ModelInputError(ValueError):
    """Pinned content is unresolved, inconsistent, or beyond the fixed V1 budget."""


def _no_floats(value: object) -> None:
    if isinstance(value, float):
        raise ModelInputError("Floating-point evidence is not permitted")
    if isinstance(value, dict):
        for nested in value.values():
            _no_floats(nested)
    elif isinstance(value, (tuple, list)):
        for nested in value:
            _no_floats(nested)


def _json(value: object) -> str:
    _no_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _fields(value: BaseModel, names: str) -> dict[str, Any]:
    selected = set(names.split())
    if not selected <= type(value).model_fields.keys():
        raise ModelInputError("Projection field contract does not match evidence schema")
    return value.model_dump(mode="json", include=selected)


def _one(values: Iterable[BaseModel]) -> BaseModel:
    matches = tuple(values)
    if len(matches) != 1:
        raise ModelInputError("Pinned item must resolve exactly once")
    return matches[0]


def _content(ref: EvidenceReference, evidence: ResolvedEvidence) -> dict[str, Any]:
    owner_id, key = ref.owning_snapshot_id, ref.item_key
    if ref.kind == "COMPANY":
        if evidence.company is None:
            raise ModelInputError("Missing company")
        return _fields(evidence.company, "id name listings provider_identifiers")
    if ref.kind in ("FINANCIAL", "SHAREHOLDING"):
        f = evidence.financial
        if f is None:
            raise ModelInputError("Missing financial extraction")
        observations = (
            (*f.top_metrics, *f.profit_loss, *f.quarterly, *f.balance_sheet, *f.cashflow, *f.ratios)
            if ref.kind == "FINANCIAL"
            else (
                *[h.observation for h in f.shareholding.holdings],
                *f.shareholding.shareholder_counts,
            )
        )
        selected = _one(o for o in observations if str(o.id) == key)
        return _fields(selected, _OBSERVATION_FIELDS)
    if ref.kind == "MARKET":
        market = evidence.markets[owner_id]
        selected = _one(o for o in market.observations if str(o.id) == key)
        return {
            "observation": _fields(
                selected,
                "id instrument_id trading_date candle_timestamp "
                "provider_updated_at open high low close volume volume_semantics adjusted_close "
                "adjustment_basis session_status provenance",
            ),
            "instrument": market.instrument.model_dump(mode="json") if market.instrument else None,
            "knowledge_basis": market.knowledge_basis,
        }
    if ref.kind in ("FINANCIAL_CALCULATION", "SECTOR_KPI"):
        calc = evidence.calculations[owner_id]
        selected = _one(
            r
            for r in calc.results
            if (r.metric_id if isinstance(r, CalculationResult) else r.definition.metric_id) == key
        )
        return _fields(selected, _RESULT_FIELDS[ref.kind])
    if ref.kind == "PATTERN":
        selected = _one(r for r in evidence.patterns[owner_id].findings if str(r.id) == key)
        return _fields(selected, _RESULT_FIELDS[ref.kind])
    if ref.kind == "MARKET_CALCULATION":
        selected = _one(
            r for r in evidence.market_calculations[owner_id].results if r.metric_id == key
        )
        return _fields(selected, _RESULT_FIELDS[ref.kind])
    if ref.kind == "TECHNICAL":
        matches = [
            (series, point)
            for series in evidence.technicals[owner_id].series
            for point in series.points
            if str(point.id) == key
        ]
        if len(matches) != 1:
            raise ModelInputError("Pinned technical point must resolve exactly once")
        series, point = matches[0]
        return {
            **_fields(series, "definition unit currency numerical_method issues"),
            "point": _fields(
                point,
                "id indicator_id definition_version date status value "
                "display_value numerical_method observation_ids seed_observation_ids "
                "previous_point_id dependent_point_ids issue",
            ),
        }
    if ref.kind == "VALUATION":
        valuation = evidence.valuations[owner_id]
        result = _one(r for r in valuation.results if str(r.id) == key)
        payload = _fields(result, _RESULT_FIELDS[ref.kind])
        # Selection metadata is part of this exact result's frozen owner, not a new pin.
        selection = valuation.request.selections[payload["selection_index"]]
        payload["selection"] = selection.model_dump(mode="json")
        payload["reporting_scope"] = valuation.request.reporting_scope.value
        payload["evaluated_at"] = valuation.model_dump(mode="json", include={"evaluated_at"})[
            "evaluated_at"
        ]
        return payload
    if ref.kind == "DOCUMENT_CHUNK":
        document = evidence.documents[owner_id]
        selected = _one(c for c in document.chunks if str(c.id) == key)
        payload = _fields(
            document,
            "identity version_id title document_type classification_basis "
            "publication_date fiscal_label source_reference parser_version",
        )
        payload["chunk"] = _fields(
            selected,
            "id version_id artifact_id page_id page_number "
            "section_id ordinal start_char end_char text text_sha256 character_count word_count",
        )
        return payload
    if ref.kind == "PUBLICATION":
        publication = evidence.publications[owner_id]
        selected = _one(p for index, p in enumerate(publication.passages, 1) if str(index) == key)
        payload = _fields(
            publication,
            "id identity title publication_date published_at "
            "provider_updated_at reference_period retrieved_at normalizer_version",
        )
        payload["passage"] = _fields(selected, "text provenance")
        return payload
    if ref.kind == "MACRO":
        macro = evidence.macros[owner_id]
        selected = _one(o for o in macro.observations if o.reference_period == key)
        return {
            "series": macro.series.model_dump(mode="json") if macro.series else None,
            "observation": _fields(
                selected,
                "series_id reference_period value source_status "
                "revision_status published_at provider_vintage_at retrieved_at provenance",
            ),
        }
    raise ModelInputError("Unsupported evidence kind")


def _stable_sorted(refs: list[EvidenceReference]) -> list[EvidenceReference]:
    return sorted(refs, key=lambda r: (r.kind, str(r.owning_snapshot_id), r.item_key or ""))


def _parsed_fiscal_label_date(fiscal_label: str) -> date | None:
    """Some evidence kinds (e.g. shareholding, sourced from screener.in) never carry
    a `FinancialPeriod.end_date` at all - only a "Mon YYYY" fiscal_label. Without
    parsing it, every such period ties on end_date and recency ordering silently
    falls back to insertion order, which traces back to a UUID-keyed sort - exactly
    the arbitrary, non-domain-relevant ordering this module must not depend on."""
    try:
        parsed = datetime.strptime(fiscal_label.strip(), "%b %Y")
    except ValueError:
        return None
    last_day = calendar.monthrange(parsed.year, parsed.month)[1]
    return date(parsed.year, parsed.month, last_day)


def _resolved_period_date(identity: tuple[date | None, str]) -> date | None:
    end_date, fiscal_label = identity
    return end_date or _parsed_fiscal_label_date(fiscal_label)


def _period_identity_key(identity: tuple[date | None, str]) -> tuple[bool, date]:
    """Most-recent-first ordering. A period resolving to no date at all (e.g. "TTM",
    which has no end_date and no parseable fiscal_label) is treated as the most
    current available and ranks above every dated period."""
    resolved = _resolved_period_date(identity)
    return (resolved is None, resolved or date.min)


def _group_by_period(
    refs: list[EvidenceReference], period_by_item: dict[str, FinancialPeriod]
) -> list[list[EvidenceReference]]:
    """Group refs sharing a financial period, most-recent period first. Each returned
    group is later admitted atomically - all of a period's observations, or none -
    so no period is ever represented by an arbitrary subset of its own metrics."""
    groups: dict[tuple[date | None, str], list[EvidenceReference]] = {}
    for ref in refs:
        key = ref.item_key
        if key is None or key not in period_by_item:
            continue
        period = period_by_item[key]
        groups.setdefault((period.end_date, period.fiscal_label), []).append(ref)
    ordered_identities = sorted(groups, key=_period_identity_key, reverse=True)
    return [_stable_sorted(groups[identity]) for identity in ordered_identities]


#: Document chunks admitted before FINANCIAL's full recency expansion (Part 6D-2),
#: so document evidence cannot be crowded out purely because financial history is
#: large. Capped at MAX_DOCUMENT_CHUNKS regardless.
_DOCUMENT_CHUNK_FLOOR = 6


def select_bounded_sources(
    sources: tuple[EvidenceReference, ...], evidence: ResolvedEvidence
) -> tuple[EvidenceReference, ...]:
    """Deterministically bound the analyst-facing evidence universe to
    MAX_INPUT_BYTES/MAX_ITEMS/MAX_DOCUMENT_CHUNKS, favoring decision-relevant and
    recent evidence over exhaustive history. Every reference is kept whole or dropped
    whole - never partially included - and every kept reference is untouched, so
    citations against it remain exactly as resolvable as before.

    Reserved category budgeting (Part 6D-2/6D-3): a pure priority-ordered greedy fill
    lets financial history (hundreds of similarly-cheap observations) exhaust the
    byte budget before lower-priority categories - SHAREHOLDING and DOCUMENT_CHUNK -
    are ever reached; and *within* financial history, profit_loss/quarterly (admitted
    first) can just as easily exhaust it before balance_sheet/cashflow/ratios ever
    get a look in. Both are the same bug at different scopes, so both get the same
    fix: a cheap mandatory floor for every category/family that must never be
    entirely starved, admitted before anything expands past its own floor.

    Phase 1 floors, in this exact priority order (also the documented fallback order
    if the hard ceiling cannot fit them all): company; all calculations/valuations;
    top_metrics; one latest period each of profit_loss, balance_sheet, cashflow,
    quarterly, ratios (in that order); one latest shareholding period;
    up to _DOCUMENT_CHUNK_FLOOR document chunks. Phase 1B then completes each
    family's own declared floor round-robin - annual families (profit_loss,
    balance_sheet, cashflow, ratios) each to _ANNUAL_FLOOR_PERIODS periods one
    family at a time per round, then quarterly to _QUARTERLY_FLOOR_PERIODS quarters -
    so no single family's floor completes before the others' floors even start.

    Only after every floor is settled does phase 2 expand toward full targets:
    shareholding to _SHAREHOLDING_PERIOD_CAP periods, documents to
    MAX_DOCUMENT_CHUNKS, and finally financial history - again round-robin by period
    depth across the four annual families before quarterly's remaining quarters -
    toward _ANNUAL_PERIOD_CAP/_QUARTERLY_PERIOD_CAP. Financial history only ever
    consumes what the other categories' targets did not need, and no annual family
    is ever fully expanded while another still sits at its floor.

    Within any financial or shareholding period, every observation for that period
    is admitted atomically (all of it, or none) - a period is never represented by
    an arbitrary subset of its own metrics. Document chunks are ordered for
    admission by their place in the source document (page, then ordinal) - not by
    chunk UUID, which carries no relationship to reading order or importance.

    Used only when assembling the bundle that feeds the analyst model
    (`assemble_bundle`). `build_source_references` itself stays exhaustive - the
    challenge stage's fresh-evidence re-verification needs the full universe, not
    this budget-constrained subset.
    """
    financial = evidence.financial
    category_by_item: dict[str, str] = {}
    period_by_item: dict[str, FinancialPeriod] = {}
    if financial is not None:
        for category in _FINANCIAL_CATEGORY_ORDER:
            for observation in getattr(financial, category):
                key = str(observation.id)
                category_by_item[key] = category
                period_by_item[key] = observation.period
        for holding in financial.shareholding.holdings:
            period_by_item[str(holding.observation.id)] = holding.observation.period
        for count in financial.shareholding.shareholder_counts:
            period_by_item[str(count.id)] = count.period

    # (page_number, ordinal): a chunk's place in its source document - real reading
    # order, unlike its UUID, which is used nowhere in this function for ordering.
    chunk_order_by_item: dict[str, tuple[int, int]] = {}
    for document in evidence.documents.values():
        for chunk in document.chunks:
            chunk_order_by_item[str(chunk.id)] = (chunk.page_number, chunk.ordinal)

    company_refs: list[EvidenceReference] = []
    financial_refs: dict[str, list[EvidenceReference]] = {c: [] for c in _FINANCIAL_CATEGORY_ORDER}
    shareholding_refs: list[EvidenceReference] = []
    calculation_refs: list[EvidenceReference] = []
    document_refs: list[EvidenceReference] = []
    other_refs: list[EvidenceReference] = []

    for ref in sources:
        if ref.kind == "COMPANY":
            company_refs.append(ref)
        elif ref.kind == "FINANCIAL":
            matched_category = category_by_item.get(ref.item_key or "")
            if matched_category in financial_refs:
                financial_refs[matched_category].append(ref)
        elif ref.kind == "SHAREHOLDING":
            shareholding_refs.append(ref)
        elif ref.kind in _CALCULATION_LIKE_KINDS:
            calculation_refs.append(ref)
        elif ref.kind == "DOCUMENT_CHUNK":
            document_refs.append(ref)
        else:
            other_refs.append(ref)

    def document_reading_order(refs: list[EvidenceReference]) -> list[EvidenceReference]:
        return sorted(
            refs,
            key=lambda r: (
                str(r.owning_snapshot_id),
                chunk_order_by_item.get(r.item_key or "", (0, 0)),
                r.item_key or "",
            ),
        )

    profit_loss_periods = _group_by_period(financial_refs["profit_loss"], period_by_item)
    quarterly_periods = _group_by_period(financial_refs["quarterly"], period_by_item)
    balance_sheet_periods = _group_by_period(financial_refs["balance_sheet"], period_by_item)
    cashflow_periods = _group_by_period(financial_refs["cashflow"], period_by_item)
    ratios_periods = _group_by_period(financial_refs["ratios"], period_by_item)
    shareholding_periods = _group_by_period(shareholding_refs, period_by_item)
    document_candidates = document_reading_order(document_refs)
    document_floor = document_candidates[:_DOCUMENT_CHUNK_FLOOR]
    document_target_extra = document_candidates[_DOCUMENT_CHUNK_FLOOR:MAX_DOCUMENT_CHUNKS]

    selected: list[EvidenceReference] = []
    selected_ids: set[tuple[str, str, str]] = set()
    running_bytes = 0
    running_items = 0
    target_bytes = int(MAX_INPUT_BYTES * _SELECTION_BYTE_TARGET_RATIO)

    def ref_id(r: EvidenceReference) -> tuple[str, str, str]:
        return (r.kind, str(r.owning_snapshot_id), r.item_key or "")

    def admit_group(group: list[EvidenceReference]) -> bool:
        """Admit an entire group (a financial/shareholding period, or a single
        standalone reference) atomically. Returns whether it was admitted - callers
        that need graceful degradation simply ignore a False and move on, which is
        exactly the "skip cleanly" behavior the byte ceiling requires."""
        nonlocal running_bytes, running_items
        new_refs = [r for r in group if ref_id(r) not in selected_ids]
        if not new_refs:
            return True
        sizes = [
            len(_json(_content(r, evidence)).encode("utf-8")) + _SELECTION_ITEM_OVERHEAD_BYTES
            for r in new_refs
        ]
        total = sum(sizes)
        if running_items + len(new_refs) > MAX_ITEMS or running_bytes + total > target_bytes:
            return False
        for r in new_refs:
            selected.append(r)
            selected_ids.add(ref_id(r))
        running_bytes += total
        running_items += len(new_refs)
        return True

    def admit_each(refs: list[EvidenceReference]) -> None:
        for r in refs:
            admit_group([r])

    def admit_periods(groups: list[list[EvidenceReference]]) -> None:
        for group in groups:
            admit_group(group)

    annual_periods_by_family = {
        "profit_loss": profit_loss_periods,
        "balance_sheet": balance_sheet_periods,
        "cashflow": cashflow_periods,
        "ratios": ratios_periods,
    }

    def admit_annual_depth(depth: int) -> None:
        """One round-robin pass at a given period depth (0 = latest) across all four
        annual families, in _ANNUAL_FAMILY_ORDER - so completing one family's floor
        or target never precedes even starting the others' at the same depth."""
        for family in _ANNUAL_FAMILY_ORDER:
            periods = annual_periods_by_family[family]
            if depth < len(periods):
                admit_group(periods[depth])

    def admit_quarterly_depth(depth: int) -> None:
        if depth < len(quarterly_periods):
            admit_group(quarterly_periods[depth])

    # --- Phase 1: mandatory single-period floors, exact documented fallback order -
    # A. COMPANY
    admit_group(_stable_sorted(company_refs))
    # B. calculation/valuation/sector-KPI/market-calculation/pattern - retain all
    # unless impossible under the hard ceiling.
    admit_group(_stable_sorted(calculation_refs))
    # Cheap, high-value, non-historical snapshot metrics - always attempted early.
    admit_group(_stable_sorted(financial_refs["top_metrics"]))
    # Latest period of each annual family, in _ANNUAL_FAMILY_ORDER, before quarterly/
    # ratios/shareholding/documents - matches the documented fallback priority.
    for family in ("profit_loss", "balance_sheet", "cashflow"):
        periods = annual_periods_by_family[family]
        if periods:
            admit_group(periods[0])
    if quarterly_periods:
        admit_group(quarterly_periods[0])
    ratios_first = annual_periods_by_family["ratios"]
    if ratios_first:
        admit_group(ratios_first[0])
    # C floor: the latest shareholding period must always be represented.
    if shareholding_periods:
        admit_group(shareholding_periods[0])
    # D floor: at least _DOCUMENT_CHUNK_FLOOR document chunks when that many exist.
    admit_each(document_floor)

    # --- Phase 1B: complete each family's own declared floor, round-robin ---------
    # Annual families to _ANNUAL_FLOOR_PERIODS (depth 0 already admitted above).
    for depth in range(1, _ANNUAL_FLOOR_PERIODS):
        admit_annual_depth(depth)
    # Quarterly to _QUARTERLY_FLOOR_PERIODS (depth 0 already admitted above).
    for depth in range(1, _QUARTERLY_FLOOR_PERIODS):
        admit_quarterly_depth(depth)

    # --- Phase 2: target expansion, in this exact priority order ------------------
    # C target: up to _SHAREHOLDING_PERIOD_CAP periods total (period 0 already
    # admitted above, if it fit).
    admit_periods(shareholding_periods[1:_SHAREHOLDING_PERIOD_CAP])
    # D target: up to MAX_DOCUMENT_CHUNKS total.
    admit_each(document_target_extra)
    # E target: full recency windows, round-robin by depth across the four annual
    # families before quarterly's remaining quarters - only now, after shareholding/
    # document targets, so financial history cannot crowd them out first, and no
    # single annual family fully expands while another still sits at its floor.
    for depth in range(_ANNUAL_FLOOR_PERIODS, _ANNUAL_PERIOD_CAP):
        admit_annual_depth(depth)
    for depth in range(_QUARTERLY_FLOOR_PERIODS, _QUARTERLY_PERIOD_CAP):
        admit_quarterly_depth(depth)
    # F. Everything else (market/technical/publication/macro), deterministic order,
    # subject to whatever budget remains.
    admit_each(_stable_sorted(other_refs))

    def canonical_key(r: EvidenceReference) -> tuple[str, str, str]:
        return (r.kind, str(r.owning_snapshot_id), r.item_key or "")

    return tuple(sorted(selected, key=canonical_key))


def serialize_model_input(bundle: AnstInputBundle, model_input: AnalystModelInput) -> str:
    """Validate binding and budgets again at the adapter boundary; emit canonical JSON."""
    if (
        model_input.bundle_id != bundle.bundle_id
        or model_input.bundle_sha256 != digest(bundle)
        or tuple(i.reference for i in model_input.items) != bundle.sources
    ):
        raise ModelInputError("Model input does not match the frozen bundle")
    if len(model_input.items) > MAX_ITEMS:
        raise ModelInputError("Model input item budget exceeded")
    items = []
    chunks = 0
    for item in model_input.items:
        try:
            content = json.loads(item.content_json)
        except ValueError as exc:
            raise ModelInputError("Invalid content JSON") from exc
        if not isinstance(content, dict) or _json(content) != item.content_json:
            raise ModelInputError("Content must be a canonical JSON object")
        if item.reference.kind == "DOCUMENT_CHUNK":
            chunks += 1
            chunk = content.get("chunk")
            if not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str):
                raise ModelInputError("Missing document text")
            if len(chunk["text"].encode("utf-8")) > MAX_DOCUMENT_CHUNK_BYTES:
                raise ModelInputError("Document chunk byte budget exceeded")
        items.append({**item.model_dump(mode="json", exclude={"content_json"}), "content": content})
    if chunks > MAX_DOCUMENT_CHUNKS:
        raise ModelInputError("Document chunk count budget exceeded")
    payload = {
        "bundle": bundle.model_dump(mode="json"),
        "model_input": {
            **model_input.model_dump(mode="json", exclude={"items"}),
            "items": items,
        },
    }
    rendered = _json(payload)
    if len(rendered.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ModelInputError("Model input byte budget exceeded")
    return rendered


def build_model_input(bundle: AnstInputBundle, evidence: ResolvedEvidence) -> AnalystModelInput:
    """Resolve only frozen references. Recheck owner/raw hashes against local evidence."""
    if len(bundle.sources) > MAX_ITEMS:
        raise ModelInputError("Model input item budget exceeded")
    # Ignore owners not named by the frozen sources, even if a caller resolved extras.
    owners = {ref.owning_snapshot_id for ref in bundle.sources}
    kinds = {ref.kind for ref in bundle.sources}
    evidence = replace(
        evidence,
        company=evidence.company if "COMPANY" in kinds else None,
        financial=evidence.financial if kinds & {"FINANCIAL", "SHAREHOLDING"} else None,
        markets={k: v for k, v in evidence.markets.items() if k in owners},
        calculations={k: v for k, v in evidence.calculations.items() if k in owners},
        patterns={k: v for k, v in evidence.patterns.items() if k in owners},
        market_calculations={k: v for k, v in evidence.market_calculations.items() if k in owners},
        technicals={k: v for k, v in evidence.technicals.items() if k in owners},
        valuations={k: v for k, v in evidence.valuations.items() if k in owners},
        documents={k: v for k, v in evidence.documents.items() if k in owners},
        publications={k: v for k, v in evidence.publications.items() if k in owners},
        macros={k: v for k, v in evidence.macros.items() if k in owners},
    )
    request = bundle.request.model_copy(
        update={
            "document_chunk_refs": tuple(
                (owner, chunk)
                for owner, chunk in bundle.request.document_chunk_refs
                if any(
                    r.kind == "DOCUMENT_CHUNK"
                    and r.owning_snapshot_id == owner
                    and r.item_key == str(chunk)
                    for r in bundle.sources
                )
            ),
            "macro_snapshot_lookups": tuple(
                (owner, period)
                for owner, period in bundle.request.macro_snapshot_lookups
                if any(
                    r.kind == "MACRO" and r.owning_snapshot_id == owner and r.item_key == period
                    for r in bundle.sources
                )
            ),
        }
    )
    try:
        fresh = {
            (r.kind, r.owning_snapshot_id, r.item_key): r
            for r in build_source_references(request, evidence)
        }
        items = []
        for ref in bundle.sources:
            if fresh.get((ref.kind, ref.owning_snapshot_id, ref.item_key)) != ref:
                raise ModelInputError("Pinned owner, item or payload hash mismatch")
            content = _content(ref, evidence)
            status = _reference_status(ref, evidence)
            if ref.kind in ("FINANCIAL", "SHAREHOLDING") and content["value"] is None:
                status = (
                    "NOT_APPLICABLE"
                    if content["value_status"] == "not_applicable"
                    else "INSUFFICIENT_INPUT"
                )
            items.append(
                AnalystModelEvidence(
                    reference=ref,
                    status=status,
                    source_epistemic_class=source_epistemic_class(ref, evidence),
                    limitations=limitation_codes(ref, evidence),
                    content_json=_json(content),
                )
            )
        projection = AnalystModelInput(
            bundle_id=bundle.bundle_id, bundle_sha256=digest(bundle), items=tuple(items)
        )
        serialize_model_input(bundle, projection)
        return projection
    except (KeyError, IndexError, AssertionError) as exc:
        raise ModelInputError("Pinned evidence could not be resolved") from exc
