"""Exact-reference, bounded, provider-neutral projection. No IO or new arithmetic.

Explicit field allowlists deliberately exclude nested input observations, sibling
results, corpus pages and retrieval request parameters. No latest/search fallback.
"""

import json
from collections.abc import Iterable
from dataclasses import replace
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

MAX_INPUT_BYTES = 2_097_152
MAX_ITEMS = 2000
MAX_DOCUMENT_CHUNKS = 32
MAX_DOCUMENT_CHUNK_BYTES = 16_384

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
