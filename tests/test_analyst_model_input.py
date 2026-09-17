"""R1: frozen evidence content reaches a model without widening its citation universe."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from test_analysis import world as evidence_world

from stock_research_ai.adapters.anthropic.analyst import AnthropicAnalystModel
from stock_research_ai.application.hashing import digest
from stock_research_ai.domain.analysis import AnalystExecutionSpec
from stock_research_ai.domain.financial import ValueStatus

world = evidence_world


def project(world):
    from stock_research_ai.application.analyst_model_input import build_model_input

    *_, bundle, evidence = world
    return build_model_input(bundle, evidence)


def content(item):
    return json.loads(item.content_json)


async def test_actual_document_content_reaches_anthropic_request(world):
    """Initially fails against the frozen adapter: it sends references but no passage."""
    *_, document, bundle, evidence = world
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "offline-model",
                "content": [{"type": "tool_use", "name": "emit_claims", "input": {"claims": []}}],
            },
        )

    adapter = AnthropicAnalystModel(
        api_key="test-only", model="offline-model", transport=httpx.MockTransport(handler)
    )
    metadata = await adapter.generate(
        bundle,
        AnalystExecutionSpec(template_id="r1-test", template_version=1),
        model_input=project(world),
    )
    wire = captured[0]["messages"][0]["content"]
    assert json.dumps(document.chunks[0].text) in wire
    assert metadata.request.bundle_sha256 == digest(bundle)
    assert wire in metadata.request.rendered_text
    assert (
        metadata.request.rendered_sha256
        == hashlib.sha256(metadata.request.rendered_text.encode()).hexdigest()
    )
    actual_items = json.loads(wire)["model_input"]["items"]
    observation = next(o for o in world[0].profit_loss if o.value is not None)
    actual = next(
        i["content"] for i in actual_items if i["reference"]["item_key"] == str(observation.id)
    )
    assert actual["value"] == str(observation.value)
    assert actual["period"] == observation.period.model_dump(mode="json")
    assert actual["unit"] == observation.unit.model_dump(mode="json")
    assert actual["reporting_scope"] == observation.reporting_scope.value
    calc = next(
        i["content"] for i in actual_items if i["reference"]["kind"] == "FINANCIAL_CALCULATION"
    )
    assert calc["display_value"] == str(world[1].results[0].display_value)
    print(f"R1 fixture: {len(actual_items)} items, {len(wire.encode())} UTF-8 bytes")


async def test_financial_value_period_unit_scope_and_locator(world):
    source, *rest = world
    observation = next(o for o in source.profit_loss if o.value is not None)
    item = next(i for i in project(world).items if i.reference.item_key == str(observation.id))
    payload = content(item)
    assert payload["value"] == str(observation.value)
    assert payload["period"] == observation.period.model_dump(mode="json")
    assert payload["unit"] == observation.unit.model_dump(mode="json")
    assert payload["reporting_scope"] == observation.reporting_scope.value
    assert payload["provenance"] == observation.provenance.model_dump(mode="json")


async def test_calculation_result_and_valuation_reach_projection(world):
    _, calculation, valuation, *_ = world
    items = project(world).items
    calc = next(i for i in items if i.reference.kind == "FINANCIAL_CALCULATION")
    assert content(calc)["display_value"] == str(calculation.results[0].display_value)
    assert content(calc)["snapshot_id"] == str(calculation.id)
    assert content(calc)["formula"] == calculation.results[0].formula.model_dump(mode="json")
    val = next(i for i in items if i.reference.kind == "VALUATION")
    assert content(val)["id"] == str(valuation.results[0].id)
    assert content(val)["values"] == [
        v.model_dump(mode="json") for v in valuation.results[0].values
    ]


async def test_only_selected_document_chunk_is_visible(world):
    from stock_research_ai.application.analyst_model_input import build_model_input

    _, _, _, document, bundle, evidence = world
    selected = next(r for r in bundle.sources if r.kind == "DOCUMENT_CHUNK")
    narrow = bundle.model_copy(update={"sources": (selected,)})
    result = build_model_input(narrow, evidence)
    assert len(result.items) == 1
    assert content(result.items[0])["chunk"]["text"] == document.chunks[0].text
    assert "pages" not in content(result.items[0])
    assert "chunks" not in content(result.items[0])


async def test_unpinned_owners_and_sibling_results_cannot_enter(world):
    from stock_research_ai.application.analyst_model_input import build_model_input

    _, calculation, _, _, bundle, evidence = world
    ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")
    narrow = bundle.model_copy(update={"sources": (ref,)})
    extra = calculation.model_copy(update={"id": uuid4()})
    enlarged = replace(evidence, calculations={**evidence.calculations, extra.id: extra})
    before = build_model_input(narrow, evidence)
    after = build_model_input(narrow, enlarged)
    assert before == after
    payload = content(after.items[0])
    assert "inputs" not in payload  # no expansion into separately unpinned observations
    assert "results" not in payload


async def test_stable_serialization_and_frozen_bundle_hash(world):
    from stock_research_ai.application.analyst_model_input import (
        build_model_input,
        serialize_model_input,
    )

    *_, bundle, evidence = world
    original = bundle.model_dump_json()
    reordered = replace(evidence, calculations=dict(reversed(list(evidence.calculations.items()))))
    first = build_model_input(bundle, evidence)
    second = build_model_input(bundle, reordered)
    assert serialize_model_input(bundle, first) == serialize_model_input(bundle, second)
    assert first == build_model_input(bundle, evidence)
    assert tuple(i.reference for i in first.items) == bundle.sources
    assert first.bundle_sha256 == digest(bundle)
    assert bundle.model_dump_json() == original
    assert first.bundle_sha256 == hashlib.sha256(original.encode()).hexdigest()


async def test_unavailable_values_remain_null(world):
    from stock_research_ai.application.analysis import assemble_bundle
    from stock_research_ai.application.analyst_model_input import build_model_input

    source, _, _, _, bundle, evidence = world
    observation = source.profit_loss[0].model_copy(
        update={"value": None, "value_status": ValueStatus.MISSING}
    )
    altered = source.model_copy(update={"profit_loss": (observation,)})
    local = replace(evidence, financial=altered)
    frozen = assemble_bundle(bundle.request, local)
    result = build_model_input(frozen, local)
    item = next(i for i in result.items if i.reference.item_key == str(observation.id))
    assert content(item)["value"] is None
    assert item.status == "INSUFFICIENT_INPUT"


async def test_decimal_digits_are_not_converted_to_float(world):
    from stock_research_ai.application.analysis import assemble_bundle
    from stock_research_ai.application.analyst_model_input import build_model_input

    source, _, _, _, bundle, evidence = world
    exact = Decimal("12345678901234567890.123456789012345678901234567890")
    observation = source.profit_loss[0].model_copy(update={"value": exact})
    local = replace(evidence, financial=source.model_copy(update={"profit_loss": (observation,)}))
    frozen = assemble_bundle(bundle.request, local)
    result = build_model_input(frozen, local)
    item = next(i for i in result.items if i.reference.item_key == str(observation.id))
    assert content(item)["value"] == str(exact)


@pytest.mark.parametrize("fault", ["missing_owner", "wrong_hash", "unknown_item", "raw_hash"])
async def test_unresolvable_or_changed_pins_fail_closed(world, fault):
    from stock_research_ai.application.analyst_model_input import (
        ModelInputError,
        build_model_input,
    )

    *_, bundle, evidence = world
    ref = next(r for r in bundle.sources if r.kind == "FINANCIAL_CALCULATION")
    if fault == "missing_owner":
        evidence = replace(evidence, calculations={})
    elif fault == "wrong_hash":
        ref = ref.model_copy(update={"snapshot_payload_sha256": "0" * 64})
    elif fault == "unknown_item":
        ref = ref.model_copy(update={"item_key": "not-pinned"})
    else:
        ref = ref.model_copy(update={"raw_artifact_sha256": "0" * 64})
    with pytest.raises(ModelInputError):
        build_model_input(bundle.model_copy(update={"sources": (ref,)}), evidence)


@pytest.mark.parametrize("limit", ["bytes", "items", "chunks", "chunk_bytes"])
async def test_budget_overflow_fails_without_truncation(world, limit, monkeypatch):
    from stock_research_ai.application import analyst_model_input as projection

    names = {
        "bytes": "MAX_INPUT_BYTES",
        "items": "MAX_ITEMS",
        "chunks": "MAX_DOCUMENT_CHUNKS",
        "chunk_bytes": "MAX_DOCUMENT_CHUNK_BYTES",
    }
    monkeypatch.setattr(projection, names[limit], 1 if limit != "chunks" else 0)
    *_, bundle, evidence = world
    with pytest.raises(projection.ModelInputError, match="budget"):
        projection.build_model_input(bundle, evidence)


async def test_adapter_rejects_projection_for_different_bundle_before_transport(world):
    from stock_research_ai.application.analyst_model_input import ModelInputError

    *_, bundle, _ = world
    adapter = AnthropicAnalystModel(
        api_key="test-only",
        model="offline",
        transport=httpx.MockTransport(lambda _: pytest.fail("must not send a request")),
    )
    with pytest.raises(ModelInputError):
        await adapter.generate(
            bundle.model_copy(update={"bundle_id": uuid4()}),
            AnalystExecutionSpec(template_id="r1", template_version=1),
            model_input=project(world),
        )


@pytest.mark.parametrize("fault", ["citation", "numeric", "quote"])
async def test_projection_does_not_authorize_unsupported_claims(world, fault):
    from test_analysis import citation_dict

    from stock_research_ai.application.analysis_validation import validate_candidate

    *_, bundle, evidence = world
    project(world)
    ref = next(r for r in bundle.sources if r.kind == "DOCUMENT_CHUNK")
    candidate = dict(
        section="MANAGEMENT_AND_DOCUMENT_INTELLIGENCE",
        statement="Management commentary is available.",
        materiality="PRIMARY",
        citations=[citation_dict(ref)],
    )
    if fault == "citation":
        candidate["citations"][0]["item_key"] = "outside-frozen-bundle"
    elif fault == "numeric":
        candidate["statement"] = "Revenue grew 99999999%."
    else:
        candidate["management_quote"] = dict(chunk=citation_dict(ref), text="Invented quote.")
    claim, rejected = validate_candidate(candidate, bundle, evidence, bundle.request.profile_id)
    assert claim is None and rejected is not None


async def test_all_evidence_kinds_resolve_only_selected_items(world, tmp_path):
    from typing import get_args

    from test_context import macro, publication
    from test_market_calculations import request as market_request
    from test_patterns import INPUTS
    from test_patterns import evaluate as patterns
    from test_patterns import request as pattern_request
    from test_sector_kpis import evaluate as kpis
    from test_sector_kpis import request as kpi_request
    from test_technicals import acquire
    from test_technicals import request as technical_request

    from stock_research_ai.application.analysis import assemble_bundle
    from stock_research_ai.application.analyst_model_input import build_model_input
    from stock_research_ai.application.market_calculations import calculate_market
    from stock_research_ai.application.technicals import calculate as technicals
    from stock_research_ai.bootstrap import ingest_macro, ingest_publication, initialize_database
    from stock_research_ai.config import Settings
    from stock_research_ai.domain.analysis import EvidenceKind

    source, _, _, _, bundle, evidence = world
    settings = Settings(
        database_path=tmp_path / "additional.sqlite3", artifact_path=tmp_path / "raw2"
    )
    await initialize_database(settings)
    market = await acquire(settings, tmp_path, "mixed", symbol="INFY")
    mc = calculate_market(market, market_request(market, metrics=["PRICE_RETURN"]))
    tech = technicals(market, technical_request(market, names=("EMA20",)))
    sector = kpis(source, kpi_request(source, symbol="INFY"))
    pattern = await patterns(source, pattern_request(source, "INFY", [next(iter(INPUTS["INFY"]))]))
    pub = await ingest_publication(settings, publication(), tmp_path / "pub")
    macro_snapshot = await ingest_macro(settings, macro(), tmp_path / "macro")
    extra = replace(
        evidence,
        markets={market.id: market},
        calculations={**evidence.calculations, sector.id: sector},
        market_calculations={mc.id: mc},
        technicals={tech.id: tech},
        patterns={pattern.id: pattern},
        publications={pub.id: pub},
        macros={macro_snapshot.id: macro_snapshot},
    )
    request = bundle.request.model_copy(
        update={
            "macro_snapshot_lookups": ((macro_snapshot.id, "2025-01"),),
        }
    )
    full = assemble_bundle(request, extra)
    refs = tuple(
        next(r for r in full.sources if r.kind == kind) for kind in sorted(get_args(EvidenceKind))
    )
    narrow = full.model_copy(update={"sources": refs})
    result = build_model_input(narrow, extra)
    assert tuple(i.reference for i in result.items) == refs
    by_kind = {i.reference.kind: content(i) for i in result.items}
    assert by_kind["MACRO"]["observation"]["reference_period"] == "2025-01"
    assert "observations" not in by_kind["MACRO"]
    assert by_kind["PUBLICATION"]["passage"]["text"] == pub.passages[0].text
    assert "passages" not in by_kind["PUBLICATION"]
    assert "points" not in by_kind["TECHNICAL"]
    assert by_kind["TECHNICAL"]["point"]["display_value"] is not None
    assert by_kind["MARKET"]["observation"]["close"] is not None
    selected_metric = next(r.item_key for r in refs if r.kind == "SECTOR_KPI")
    selected_kpi = next(r for r in sector.results if r.definition.metric_id == selected_metric)
    assert by_kind["SECTOR_KPI"]["definition"] == selected_kpi.definition.model_dump(mode="json")
    assert "inputs" not in by_kind["PATTERN"]
    # Two calculation owners now exist; dictionary insertion order is not content order.
    reordered = replace(extra, calculations=dict(reversed(list(extra.calculations.items()))))
    assert build_model_input(narrow, reordered) == result


async def test_unselected_sibling_chunk_is_absent(world):
    from stock_research_ai.application.analysis import assemble_bundle
    from stock_research_ai.application.analyst_model_input import (
        build_model_input,
        serialize_model_input,
    )

    _, _, _, doc, bundle, evidence = world
    unselected = doc.chunks[0].model_copy(update={"id": uuid4(), "text": "UNPINNED-SIBLING-TEXT"})
    changed_doc = doc.model_copy(update={"chunks": (*doc.chunks, unselected)})
    local = replace(evidence, documents={doc.version_id: changed_doc})
    frozen = assemble_bundle(bundle.request, local)
    wire = serialize_model_input(frozen, build_model_input(frozen, local))
    assert "UNPINNED-SIBLING-TEXT" not in wire


async def test_duplicate_pinned_item_fails_closed(world):
    from stock_research_ai.application.analysis import assemble_bundle
    from stock_research_ai.application.analyst_model_input import ModelInputError, build_model_input

    source, _, _, _, bundle, evidence = world
    changed = source.model_copy(
        update={"profit_loss": (*source.profit_loss, source.profit_loss[0])}
    )
    local = replace(evidence, financial=changed)
    frozen = assemble_bundle(bundle.request, local)
    with pytest.raises(ModelInputError, match="exactly once"):
        build_model_input(frozen, local)


async def test_float_projection_is_refused_before_transport(world):
    from stock_research_ai.application.analyst_model_input import (
        ModelInputError,
        serialize_model_input,
    )

    *_, bundle, _ = world
    projection = project(world)
    first = projection.items[0].model_copy(update={"content_json": '{"value":1.25}'})
    invalid = projection.model_copy(update={"items": (first, *projection.items[1:])})
    with pytest.raises(ModelInputError, match="Floating-point"):
        serialize_model_input(bundle, invalid)


@pytest.mark.parametrize(
    "status", ["NOT_APPLICABLE", "INSUFFICIENT_INPUT", "UNVERIFIED_METRIC_SEMANTICS"]
)
async def test_unavailable_valuation_never_gains_outputs(world, status):
    from stock_research_ai.application.analysis import assemble_bundle
    from stock_research_ai.application.analyst_model_input import build_model_input

    _, _, valuation, _, bundle, evidence = world
    result = valuation.results[0].model_copy(update={"status": status, "values": ()})
    changed = valuation.model_copy(update={"results": (result,)})
    local = replace(evidence, valuations={changed.id: changed})
    frozen = assemble_bundle(bundle.request, local)
    item = next(
        i for i in build_model_input(frozen, local).items if i.reference.kind == "VALUATION"
    )
    assert item.status == status
    assert content(item)["status"] == status
    assert content(item)["values"] == []
