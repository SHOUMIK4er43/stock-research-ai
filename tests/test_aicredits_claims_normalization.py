"""Narrow AI Credits/OpenAI-compatible transport compatibility fix: a response whose
tool-call arguments decode to exactly {"claims": "<json>"} (claims stringified one
extra time) is normalized - exactly one additional decode, accepted only if the
result is a list - before the existing, unmodified strict shape gate and all
downstream candidate/citation/semantic validation run. Every other shape keeps its
existing MALFORMED_ARGUMENTS behavior unchanged.
"""

import json
import logging

import httpx
import pytest
from test_aicredits_analyst import SECRET, generate, response
from test_aicredits_analyst import world as evidence_world

from stock_research_ai.adapters.aicredits import analyst as adapter
from stock_research_ai.adapters.analyst_contract import AnalystProviderError
from stock_research_ai.application.analysis_validation import validate_response
from stock_research_ai.application.analyst_model_input import build_model_input

world = evidence_world


@pytest.fixture
def diagnostic_log(tmp_path, monkeypatch):
    path = tmp_path / "service.log"
    logger = logging.Logger("claims-normalization-test", logging.INFO)
    handler = logging.FileHandler(path)
    logger.addHandler(handler)
    monkeypatch.setattr(adapter, "get_logger", lambda _: logger, raising=False)
    yield path
    handler.close()


def _events(path) -> list[dict]:
    text = path.read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _argument_event(path) -> dict:
    events = [e for e in _events(path) if e["event"] == "aicredits_argument_structure"]
    assert len(events) == 1
    return events[0]


# --- PASS WITHOUT NORMALIZATION ----------------------------------------------------


async def test_empty_claims_array_accepted_without_normalization(diagnostic_log):
    await generate(lambda r: httpx.Response(200, json=response(arguments='{"claims":[]}')))
    event = _argument_event(diagnostic_log)
    assert event["strict_claims_shape_passed"] is True
    assert event["claims_string_normalized"] is False


async def test_valid_claims_list_accepted_without_normalization(world):
    *_, bundle, evidence = world
    ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    candidate = {
        "section": "VALUATION",
        "statement": "The valuation evidence provides context.",
        "materiality": "PRIMARY",
        "citations": [
            {
                "kind": ref.kind,
                "owning_snapshot_id": str(ref.owning_snapshot_id),
                "item_key": ref.item_key,
            }
        ],
    }
    arguments = json.dumps({"claims": [candidate]})
    metadata = await generate(
        lambda r: httpx.Response(200, json=response(arguments=arguments)),
        bundle,
        build_model_input(bundle, evidence),
    )
    assert metadata.raw_response.structured_payload["claims"] == [candidate]


# --- PASS WITH ONE-TIME NORMALIZATION ----------------------------------------------


async def test_stringified_empty_claims_array_is_normalized(diagnostic_log):
    arguments = json.dumps({"claims": json.dumps([])})
    metadata = await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))
    assert metadata.raw_response.structured_payload["claims"] == []

    event = _argument_event(diagnostic_log)
    assert event["strict_claims_shape_passed"] is True
    assert event["claims_string_normalized"] is True
    # Diagnostics reflect the post-normalization value: an array, not a string.
    assert event["claims_value_type"] == "array"
    assert event["claims_item_count"] == 0


async def test_stringified_valid_claims_array_is_normalized_and_validated(world):
    *_, bundle, evidence = world
    ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    candidate = {
        "section": "VALUATION",
        "statement": "The valuation evidence provides context.",
        "materiality": "PRIMARY",
        "citations": [
            {
                "kind": ref.kind,
                "owning_snapshot_id": str(ref.owning_snapshot_id),
                "item_key": ref.item_key,
            }
        ],
    }
    arguments = json.dumps({"claims": json.dumps([candidate])})
    metadata = await generate(
        lambda r: httpx.Response(200, json=response(arguments=arguments)),
        bundle,
        build_model_input(bundle, evidence),
    )
    assert metadata.raw_response.structured_payload["claims"] == [candidate]
    accepted, rejected = validate_response(
        metadata.raw_response, bundle, evidence, bundle.request.profile_id
    )
    assert len(accepted) == 1 and len(rejected) == 0
    assert accepted[0].statement == candidate["statement"]


# --- FAIL: normalization must not trigger, or must not coerce ---------------------


@pytest.mark.parametrize(
    "arguments,label",
    [
        (json.dumps({"claims": "not json"}), "invalid_nested_json"),
        (json.dumps({"claims": "{}"}), "nested_decodes_to_object"),
        (json.dumps({"claims": json.dumps("nested string")}), "nested_decodes_to_string"),
        (json.dumps({"claims": 123}), "claims_is_number"),
        (json.dumps({"claims": None}), "claims_is_null"),
        (json.dumps({"claims": {"x": 1}}), "claims_is_object"),
        (json.dumps({"claims": json.dumps([]), "extra": 1}), "extra_top_level_key"),
        ("[]", "top_level_array"),
        (json.dumps({"claims": json.dumps(json.dumps([]))}), "double_stringified_still_a_string"),
        (json.dumps({"claims": "[{bad json"}), "malformed_nested_json"),
    ],
)
async def test_shapes_that_must_not_normalize_still_fail(diagnostic_log, arguments, label):
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))
    assert exc.value.code == "MALFORMED_ARGUMENTS", label

    # Every parametrized shape here is a validly-JSON-encoded outer arguments string
    # (only the inner/nested content is ever malformed or wrong-typed), so each one
    # reaches the argument-structure diagnostic before the final gate raises.
    event = _argument_event(diagnostic_log)
    assert event["claims_string_normalized"] is False, label
    assert event["strict_claims_shape_passed"] is False, label


# --- VALIDATION REGRESSION: normalization must not weaken semantic validation -----


async def test_normalized_invalid_candidate_is_still_rejected_by_existing_validation(world):
    *_, bundle, evidence = world
    ref = next(r for r in bundle.sources if r.kind == "VALUATION")
    citation = {
        "kind": ref.kind,
        "owning_snapshot_id": str(ref.owning_snapshot_id),
        "item_key": ref.item_key,
    }
    good = {
        "section": "VALUATION",
        "statement": "The valuation evidence provides context.",
        "materiality": "PRIMARY",
        "citations": [citation],
    }
    bad = {**good, "statement": "BUY now."}
    arguments = json.dumps({"claims": json.dumps([good, bad])})
    metadata = await generate(
        lambda r: httpx.Response(200, json=response(arguments=arguments)),
        bundle,
        build_model_input(bundle, evidence),
    )
    # Transport normalization succeeded - both candidates reached structured_payload.
    assert metadata.raw_response.structured_payload["claims"] == [good, bad]

    # Existing, unmodified semantic validation still rejects the bad candidate -
    # exactly the same behavior it has for a non-normalized claims array.
    accepted, rejected = validate_response(
        metadata.raw_response, bundle, evidence, bundle.request.profile_id
    )
    assert len(accepted) == 1 and len(rejected) == 1
    assert accepted[0].statement == good["statement"]


# --- DIAGNOSTIC LEAKAGE -------------------------------------------------------------


async def test_normalization_diagnostic_never_leaks_claim_or_argument_content(diagnostic_log):
    sensitive = "SENSITIVE CLAIM STATEMENT MARKER-7c2e9a"
    claim = {
        "section": "VALUATION",
        "statement": sensitive,
        "materiality": "PRIMARY",
        "citations": [],
    }
    arguments = json.dumps({"claims": json.dumps([claim])})
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response(arguments=arguments))

    # This candidate has zero citations, which the shared shape/semantic validators
    # (unrelated to this patch) will reject downstream - normalization itself must
    # still have succeeded and logged only safe structure before that happens.
    await generate(handler)

    text = diagnostic_log.read_text()
    forbidden_values = (
        SECRET,
        sensitive,
        "SENSITIVE CLAIM STATEMENT MARKER",
        arguments,
        "Authorization",
        "Bearer",
    )
    for forbidden in forbidden_values:
        assert forbidden not in text
    for line in text.splitlines():
        assert len(line) < 2500

    event = _argument_event(diagnostic_log)
    assert set(event) == {
        "event",
        "schema_version",
        "parsed_top_level_type",
        "top_level_key_names",
        "top_level_key_count",
        "claims_key_present",
        "claims_value_type",
        "claims_item_count",
        "parsed_is_list",
        "parsed_list_length",
        "wrapper_candidate_count",
        "wrapper_key_names",
        "function_name",
        "arguments_character_length",
        "strict_claims_shape_passed",
        "claims_string_normalized",
        "timestamp",
    }
