"""Diagnostics-only proofs for the pre-shape-check argument-structure event and the
pre-transport request-size event: sanitized shape/size metadata only, existing parser
acceptance and rejection behavior unchanged.
"""

import json
import logging

import httpx
import pytest
from test_aicredits_analyst import MODEL, SECRET, generate, response

from stock_research_ai.adapters.aicredits import analyst as adapter
from stock_research_ai.adapters.analyst_contract import (
    _TRANSPORT_INSTRUCTIONS,
    AnalystProviderError,
)


@pytest.fixture
def diagnostic_log(tmp_path, monkeypatch):
    path = tmp_path / "service.log"
    logger = logging.Logger("argument-diagnostics-test", logging.INFO)
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


def _request_event(path) -> dict:
    events = [e for e in _events(path) if e["event"] == "aicredits_request_metadata"]
    assert len(events) == 1
    return events[0]


async def test_valid_claims_object_logs_safe_structure_and_still_passes(diagnostic_log):
    await generate(lambda r: httpx.Response(200, json=response(arguments='{"claims": []}')))

    event = _argument_event(diagnostic_log)
    assert event["parsed_top_level_type"] == "object"
    assert event["top_level_key_names"] == ["claims"]
    assert event["top_level_key_count"] == 1
    assert event["claims_key_present"] is True
    assert event["claims_value_type"] == "array"
    assert event["claims_item_count"] == 0
    assert event["parsed_is_list"] is False
    assert event["parsed_list_length"] is None
    assert event["wrapper_candidate_count"] == 0
    assert event["wrapper_key_names"] == []
    assert event["function_name"] == "emit_claims"
    assert event["strict_claims_shape_passed"] is True


async def test_top_level_list_logs_type_and_length_and_still_fails(diagnostic_log):
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments="[1,2,3]")))

    assert exc.value.code == "MALFORMED_ARGUMENTS"
    event = _argument_event(diagnostic_log)
    assert event["parsed_top_level_type"] == "array"
    assert event["parsed_is_list"] is True
    assert event["parsed_list_length"] == 3
    assert event["top_level_key_names"] is None
    assert event["claims_key_present"] is False
    assert event["strict_claims_shape_passed"] is False


async def test_wrapper_object_logs_wrapper_key_names_only_and_still_fails(diagnostic_log):
    arguments = json.dumps({"result": {"claims": []}})
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    assert exc.value.code == "MALFORMED_ARGUMENTS"
    event = _argument_event(diagnostic_log)
    assert event["parsed_top_level_type"] == "object"
    assert event["top_level_key_names"] == ["result"]
    assert event["top_level_key_count"] == 1
    assert event["claims_key_present"] is False
    assert event["wrapper_candidate_count"] == 1
    assert event["wrapper_key_names"] == ["result"]
    assert event["strict_claims_shape_passed"] is False


async def test_claims_as_object_logs_claims_value_type_object_and_still_fails(diagnostic_log):
    arguments = json.dumps({"claims": {"not": "a list"}})
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    assert exc.value.code == "MALFORMED_ARGUMENTS"
    event = _argument_event(diagnostic_log)
    assert event["claims_key_present"] is True
    assert event["claims_value_type"] == "object"
    assert event["claims_item_count"] is None
    # A single key that already IS "claims" is a wrong-value-type case, not a wrapper.
    assert event["wrapper_candidate_count"] == 0
    assert event["wrapper_key_names"] == []
    assert event["strict_claims_shape_passed"] is False


async def test_extra_key_logs_key_names_and_still_fails(diagnostic_log):
    arguments = json.dumps({"claims": [], "extra": "unexpected"})
    with pytest.raises(AnalystProviderError) as exc:
        await generate(lambda r: httpx.Response(200, json=response(arguments=arguments)))

    assert exc.value.code == "MALFORMED_ARGUMENTS"
    event = _argument_event(diagnostic_log)
    assert set(event["top_level_key_names"]) == {"claims", "extra"}
    assert event["top_level_key_count"] == 2
    assert event["claims_key_present"] is True
    assert event["wrapper_candidate_count"] == 0
    assert event["wrapper_key_names"] == []
    assert event["strict_claims_shape_passed"] is False


async def test_no_argument_values_claim_text_evidence_or_secrets_are_logged(diagnostic_log):
    # A distinct marker, not the adapter's own API key: embedding the real credential
    # in claim content would (correctly) trip the existing UNSAFE_RESPONSE guard in
    # generate(), which is a different, already-covered behavior than this test.
    sensitive_statement = "SENSITIVE CLAIM STATEMENT TEXT MARKER-9f3a1c"
    claim = {
        "section": "VALUATION",
        "statement": sensitive_statement,
        "materiality": "PRIMARY",
        "citations": [],
    }
    arguments = json.dumps({"claims": [claim]})
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response(arguments=arguments))

    await generate(handler)

    request_body = json.loads(captured[0].content)
    user_message = request_body["messages"][1]["content"]

    text = diagnostic_log.read_text()
    for forbidden in (
        SECRET,
        sensitive_statement,
        "SENSITIVE CLAIM STATEMENT TEXT",
        arguments,
        user_message,
        _TRANSPORT_INSTRUCTIONS,
        "Authorization",
        "Bearer",
    ):
        assert forbidden not in text
    for line in text.splitlines():
        assert len(line) < 2500


async def test_request_metadata_logs_only_sizes_and_counts_never_content(diagnostic_log):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=response())

    await generate(handler)

    request_body = json.loads(captured[0].content)
    system_message = request_body["messages"][0]["content"]
    user_message = request_body["messages"][1]["content"]

    event = _request_event(diagnostic_log)
    assert set(event) == {
        "event",
        "schema_version",
        "serialized_request_bytes",
        "system_message_characters",
        "user_message_characters",
        "tool_schema_bytes",
        "message_count",
        "tool_count",
        "model",
        "max_tokens",
        "evidence_item_count",
        "timestamp",
    }
    assert event["serialized_request_bytes"] == len(captured[0].content)
    assert event["system_message_characters"] == len(system_message)
    assert event["user_message_characters"] == len(user_message)
    assert isinstance(event["tool_schema_bytes"], int) and event["tool_schema_bytes"] > 0
    assert event["message_count"] == 2
    assert event["tool_count"] == 1
    assert event["model"] == MODEL
    assert event["max_tokens"] == 4096
    assert isinstance(event["evidence_item_count"], int)

    text = diagnostic_log.read_text()
    assert system_message not in text
    assert user_message not in text
    for line in text.splitlines():
        assert len(line) < 2500


async def _assert_rejected_without_retry(arguments: str) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response(arguments=arguments))

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == "MALFORMED_ARGUMENTS"
    assert len(calls) == 1


async def test_existing_parser_semantics_unchanged_across_valid_and_invalid_shapes(diagnostic_log):
    # Same cases test_aicredits_analyst.py's test_bad_arguments_do_not_retry already
    # proves reject - re-proving here that the new diagnostics do not alter accept/
    # reject outcomes or the retry count (still no retry on a 200 with bad content).
    for arguments in (
        "not json",
        "[]",
        "null",
        '{"claims":[],"claims":[]}',
        '{"claims":"bad"}',
    ):
        await _assert_rejected_without_retry(arguments)

    calls = []

    def valid_handler(request):
        calls.append(request)
        return httpx.Response(200, json=response(arguments='{"claims": []}'))

    await generate(valid_handler)
    assert len(calls) == 1
