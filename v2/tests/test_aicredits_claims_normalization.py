"""Narrow AI Credits/OpenAI-compatible transport compatibility fix: a response whose
tool-call arguments decode to exactly {"claims": "<json>"} (claims stringified one
extra time) is normalized - exactly one additional decode - before the existing,
unmodified strict shape gate and all downstream candidate/citation/semantic
validation run. Two, and only two, decoded outcomes are accepted: the nested value
is itself the claims list directly, or the nested value is a dict containing
exactly one key "claims" whose value is the claims list (a second, deeper observed
transport variant - {"claims": "{\\"claims\\": [...]}"}). Every other shape keeps
its existing MALFORMED_ARGUMENTS behavior unchanged.
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
    assert event["claims_nested_object_unwrapped"] is False


async def test_valid_claims_list_accepted_without_normalization(world, diagnostic_log):
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

    # claims is already a native list: the one-time nested-string decode never
    # triggers at all (its type check requires claims to be a string first).
    event = _argument_event(diagnostic_log)
    assert event["claims_nested_decode_attempted"] is False
    assert event["claims_nested_decode_succeeded"] is False
    assert event["claims_nested_decoded_type"] is None
    assert event["claims_nested_decode_error_type"] is None
    assert event["claims_nested_list_length"] is None
    assert event["claims_nested_object_unwrapped"] is False


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
    # The nested-decode fields describe the one attempt itself: it ran, succeeded,
    # produced a (empty) list, and recorded no error.
    assert event["claims_nested_decode_attempted"] is True
    assert event["claims_nested_decode_succeeded"] is True
    assert event["claims_nested_decoded_type"] == "array"
    assert event["claims_nested_decode_error_type"] is None
    assert event["claims_nested_list_length"] == 0
    # Nested decode produced a list, not a dict: the object-shape fields never apply.
    assert event["claims_nested_object_key_count"] is None
    assert event["claims_nested_object_exact_claims_wrapper"] is None
    assert event["claims_nested_object_claims_present"] is None
    assert event["claims_nested_object_claims_value_type"] is None
    assert event["claims_nested_object_claims_list_length"] is None
    assert event["claims_nested_object_unwrapped"] is False


async def test_stringified_valid_claims_array_is_normalized_and_validated(world, diagnostic_log):
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

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_decode_attempted"] is True
    assert event["claims_nested_decode_succeeded"] is True
    assert event["claims_nested_decoded_type"] == "array"
    assert event["claims_nested_decode_error_type"] is None
    assert event["claims_nested_list_length"] == 1
    assert event["claims_nested_object_key_count"] is None
    assert event["claims_nested_object_exact_claims_wrapper"] is None
    assert event["claims_nested_object_claims_present"] is None
    assert event["claims_nested_object_claims_value_type"] is None
    assert event["claims_nested_object_claims_list_length"] is None
    assert event["claims_nested_object_unwrapped"] is False


# --- FAIL: normalization must not trigger, or must not coerce ---------------------


# object_key_count, object_exact_wrapper, object_claims_present, object_claims_value_type,
# object_list_length: only the "nested_decodes_to_object" row decodes to a dict (an empty
# one, with no "claims" key), so it is the only row where these are populated rather than
# None - every non-dict/not-attempted/failed-decode row leaves them unpopulated (H).
# Every row here is a shape the fix must NOT accept - including exact_wrapper=True
# rows whose inner "claims" value is not itself a list (still no unwrap: the elif
# requires isinstance(nested_claims["claims"], list) too). Only the genuinely new
# {"claims": "{\"claims\": [...]}"} shape (covered separately below) now succeeds.
@pytest.mark.parametrize(
    "arguments,label,attempted,succeeded,decoded_type,error_type,list_length,"
    "obj_key_count,obj_exact_wrapper,obj_claims_present,obj_claims_type,obj_list_length",
    [
        (
            json.dumps({"claims": "not json"}),
            "invalid_nested_json",
            True,
            False,
            None,
            "JSONDecodeError",
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": "{}"}),
            "nested_decodes_to_object",
            True,
            True,
            "object",
            None,
            None,
            0,
            False,
            False,
            None,
            None,
        ),
        (
            json.dumps({"claims": json.dumps("nested string")}),
            "nested_decodes_to_string",
            True,
            True,
            "string",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": 123}),
            "claims_is_number",
            False,
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": None}),
            "claims_is_null",
            False,
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": {"x": 1}}),
            "claims_is_object",
            False,
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": json.dumps([]), "extra": 1}),
            "extra_top_level_key",
            False,
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        ("[]", "top_level_array", False, False, None, None, None, None, None, None, None, None),
        (
            json.dumps({"claims": json.dumps(json.dumps([]))}),
            "double_stringified_still_a_string",
            True,
            True,
            "string",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": "[{bad json"}),
            "malformed_nested_json",
            True,
            False,
            None,
            "JSONDecodeError",
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": "123"}),
            "nested_decodes_to_number",
            True,
            True,
            "number",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": "true"}),
            "nested_decodes_to_boolean",
            True,
            True,
            "boolean",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        (
            json.dumps({"claims": "null"}),
            "nested_decodes_to_null",
            True,
            True,
            "null",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    ],
)
async def test_shapes_that_must_not_normalize_still_fail(
    diagnostic_log,
    arguments,
    label,
    attempted,
    succeeded,
    decoded_type,
    error_type,
    list_length,
    obj_key_count,
    obj_exact_wrapper,
    obj_claims_present,
    obj_claims_type,
    obj_list_length,
):
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))
    assert exc.value.code == "MALFORMED_ARGUMENTS", label

    # Every parametrized shape here is a validly-JSON-encoded outer arguments string
    # (only the inner/nested content is ever malformed or wrong-typed), so each one
    # reaches the argument-structure diagnostic before the final gate raises.
    event = _argument_event(diagnostic_log)
    assert event["claims_string_normalized"] is False, label
    assert event["strict_claims_shape_passed"] is False, label
    assert event["claims_nested_object_unwrapped"] is False, label
    assert event["claims_nested_decode_attempted"] is attempted, label
    assert event["claims_nested_decode_succeeded"] is succeeded, label
    assert event["claims_nested_decoded_type"] == decoded_type, label
    assert event["claims_nested_decode_error_type"] == error_type, label
    assert event["claims_nested_list_length"] == list_length, label
    assert event["claims_nested_object_key_count"] == obj_key_count, label
    assert event["claims_nested_object_exact_claims_wrapper"] == obj_exact_wrapper, label
    assert event["claims_nested_object_claims_present"] == obj_claims_present, label
    assert event["claims_nested_object_claims_value_type"] == obj_claims_type, label
    assert event["claims_nested_object_claims_list_length"] == obj_list_length, label


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
        "claims_nested_decode_attempted",
        "claims_nested_decode_succeeded",
        "claims_nested_decoded_type",
        "claims_nested_decode_error_type",
        "claims_nested_list_length",
        "claims_nested_object_key_count",
        "claims_nested_object_exact_claims_wrapper",
        "claims_nested_object_claims_present",
        "claims_nested_object_claims_value_type",
        "claims_nested_object_claims_list_length",
        "claims_nested_object_unwrapped",
        "timestamp",
    }


# --- NESTED-OBJECT SHAPE DIAGNOSTICS + EXACT-WRAPPER UNWRAP -----------------------
# When the nested decode succeeds to a dict, the claims_nested_object_* fields
# describe that dict's shape safely, testing nothing but the literal known key
# "claims" and never logging any other key name. Exactly one shape is now also
# accepted and unwrapped - a dict containing only "claims" mapped to a list - via
# the SAME already-decoded `nested_claims` value (zero additional JSON decoding).
# Every other dict shape still raises MALFORMED_ARGUMENTS, unchanged.


def _nested_object_arguments(nested: object) -> str:
    return json.dumps({"claims": json.dumps(nested)})


async def test_nested_object_exact_claims_array_wrapper_is_unwrapped(diagnostic_log):
    arguments = _nested_object_arguments({"claims": []})
    metadata = await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))
    assert metadata.raw_response.structured_payload["claims"] == []

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_decoded_type"] == "object"
    assert event["strict_claims_shape_passed"] is True
    # This is the second, deeper transport variant, not the direct-stringified-list
    # one: claims_string_normalized stays False; claims_nested_object_unwrapped is
    # the field that records this path was taken.
    assert event["claims_string_normalized"] is False
    assert event["claims_nested_object_unwrapped"] is True
    assert event["claims_nested_object_key_count"] == 1
    assert event["claims_nested_object_claims_present"] is True
    assert event["claims_nested_object_exact_claims_wrapper"] is True
    assert event["claims_nested_object_claims_value_type"] == "array"
    assert event["claims_nested_object_claims_list_length"] == 0


async def test_nested_object_exact_claims_array_wrapper_with_candidate(world, diagnostic_log):
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
    arguments = _nested_object_arguments({"claims": [candidate]})
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

    event = _argument_event(diagnostic_log)
    assert event["strict_claims_shape_passed"] is True
    assert event["claims_nested_object_unwrapped"] is True
    assert event["claims_nested_object_key_count"] == 1
    assert event["claims_nested_object_claims_present"] is True
    assert event["claims_nested_object_exact_claims_wrapper"] is True
    assert event["claims_nested_object_claims_value_type"] == "array"
    assert event["claims_nested_object_claims_list_length"] == 1


async def test_nested_object_exact_wrapper_invalid_candidate_still_rejected_downstream(
    world, diagnostic_log
):
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
    arguments = _nested_object_arguments({"claims": [good, bad]})
    metadata = await generate(
        lambda r: httpx.Response(200, json=response(arguments=arguments)),
        bundle,
        build_model_input(bundle, evidence),
    )
    # Transport-layer unwrap succeeded - both candidates reached structured_payload.
    assert metadata.raw_response.structured_payload["claims"] == [good, bad]

    # Existing, unmodified semantic validation still rejects the bad candidate.
    accepted, rejected = validate_response(
        metadata.raw_response, bundle, evidence, bundle.request.profile_id
    )
    assert len(accepted) == 1 and len(rejected) == 1
    assert accepted[0].statement == good["statement"]

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_object_unwrapped"] is True


async def test_nested_object_claims_value_is_string(diagnostic_log):
    arguments = _nested_object_arguments({"claims": "something"})
    with pytest.raises(AnalystProviderError):
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_object_exact_claims_wrapper"] is True
    assert event["claims_nested_object_claims_value_type"] == "string"
    assert event["claims_nested_object_claims_list_length"] is None
    assert event["claims_nested_object_unwrapped"] is False


async def test_nested_object_claims_value_is_object(diagnostic_log):
    arguments = _nested_object_arguments({"claims": {}})
    with pytest.raises(AnalystProviderError):
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_object_claims_value_type"] == "object"
    assert event["claims_nested_object_unwrapped"] is False


async def test_nested_object_claims_value_is_null(diagnostic_log):
    arguments = _nested_object_arguments({"claims": None})
    with pytest.raises(AnalystProviderError):
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_object_claims_present"] is True
    assert event["claims_nested_object_claims_value_type"] == "null"
    assert event["claims_nested_object_claims_list_length"] is None
    assert event["claims_nested_object_unwrapped"] is False


async def test_nested_object_claims_value_is_number(diagnostic_log):
    arguments = _nested_object_arguments({"claims": 123})
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))
    assert exc.value.code == "MALFORMED_ARGUMENTS"

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_object_exact_claims_wrapper"] is True
    assert event["claims_nested_object_claims_value_type"] == "number"
    assert event["claims_nested_object_claims_list_length"] is None
    assert event["claims_nested_object_unwrapped"] is False


async def test_nested_object_with_extra_key_is_not_exact_wrapper(diagnostic_log):
    arguments = _nested_object_arguments({"claims": [], "extra": 1})
    with pytest.raises(AnalystProviderError):
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_object_key_count"] == 2
    assert event["claims_nested_object_claims_present"] is True
    assert event["claims_nested_object_exact_claims_wrapper"] is False
    assert event["claims_nested_object_claims_value_type"] == "array"
    assert event["claims_nested_object_unwrapped"] is False
    assert "extra" not in diagnostic_log.read_text()


async def test_nested_object_without_claims_key(diagnostic_log):
    arguments = _nested_object_arguments({"other": []})
    with pytest.raises(AnalystProviderError):
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    event = _argument_event(diagnostic_log)
    assert event["claims_nested_object_key_count"] == 1
    assert event["claims_nested_object_claims_present"] is False
    assert event["claims_nested_object_exact_claims_wrapper"] is False
    assert event["claims_nested_object_claims_value_type"] is None
    assert event["claims_nested_object_claims_list_length"] is None
    assert event["claims_nested_object_unwrapped"] is False
    assert "other" not in diagnostic_log.read_text()


async def test_double_stringified_wrapper_requires_second_decode_and_is_rejected():
    # {"claims": "<json>"} where <json> decodes to {"claims": "<json>"} again (a
    # second, still-stringified layer): the accepted dict shape requires
    # nested_claims["claims"] to ALREADY be a list, not another string requiring a
    # further decode - so this must still fail, proving no recursion was added.
    inner = json.dumps({"claims": json.dumps([])})
    arguments = _nested_object_arguments({"claims": inner})
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))
    assert exc.value.code == "MALFORMED_ARGUMENTS"


async def test_exact_wrapper_unwrap_performs_exactly_one_nested_decode(monkeypatch):
    load_calls: list[str] = []
    real_load = adapter._load

    def counting_load(text):
        load_calls.append(text)
        return real_load(text)

    monkeypatch.setattr(adapter, "_load", counting_load)

    arguments = _nested_object_arguments({"claims": []})
    metadata = await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))
    assert metadata.raw_response.structured_payload["claims"] == []
    # _load is also used once, separately, to decode the raw HTTP response body
    # (bytes, not str) before arguments parsing even begins - isolate the string
    # (argument-string) decodes to prove exactly one outer + one nested decode,
    # never a second/nested-of-nested call.
    string_calls = [c for c in load_calls if isinstance(c, str)]
    assert string_calls == [arguments, '{"claims": []}']


async def test_object_shape_diagnostic_never_leaks_nested_key_names(diagnostic_log):
    arguments = _nested_object_arguments(
        {
            "claims": [],
            "secret_internal_key_marker_9f2c": "sensitive_marker_value_7c2e9a",
            "another_key": 1,
        }
    )
    with pytest.raises(AnalystProviderError):
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    text = diagnostic_log.read_text()
    for forbidden in (
        "secret_internal_key_marker_9f2c",
        "another_key",
        "sensitive_marker_value_7c2e9a",
    ):
        assert forbidden not in text

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
        "claims_nested_decode_attempted",
        "claims_nested_decode_succeeded",
        "claims_nested_decoded_type",
        "claims_nested_decode_error_type",
        "claims_nested_list_length",
        "claims_nested_object_key_count",
        "claims_nested_object_exact_claims_wrapper",
        "claims_nested_object_claims_present",
        "claims_nested_object_claims_value_type",
        "claims_nested_object_claims_list_length",
        "claims_nested_object_unwrapped",
        "timestamp",
    }
    assert event["claims_nested_object_key_count"] == 3
    assert event["claims_nested_object_exact_claims_wrapper"] is False
    assert event["claims_nested_object_claims_present"] is True
    assert event["claims_nested_object_claims_value_type"] == "array"
