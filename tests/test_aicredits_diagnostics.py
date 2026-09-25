"""Offline response metadata proofs: no live transport or persisted model text."""

import json
import logging

import httpx
import pytest
from test_aicredits_analyst import MODEL, SECRET, generate, response

from stock_research_ai.adapters.aicredits import analyst as adapter
from stock_research_ai.adapters.analyst_contract import AnalystProviderError


@pytest.fixture
def diagnostic_log(tmp_path, monkeypatch):
    path = tmp_path / "service.log"
    logger = logging.Logger("diagnostics-test", logging.INFO)
    handler = logging.FileHandler(path)
    logger.addHandler(handler)
    monkeypatch.setattr(adapter, "get_logger", lambda _: logger, raising=False)
    yield path
    handler.close()


def _events(path) -> list[dict]:
    text = path.read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _response_metadata_event(path) -> dict:
    events = [e for e in _events(path) if e["event"] == "aicredits_response_metadata"]
    assert len(events) == 1
    return events[0]


@pytest.mark.parametrize(
    "case,code,finish,message_object",
    [
        ("missing_message", "MALFORMED_RESPONSE", "tool_calls", False),
        ("nonobject_message", "MALFORMED_RESPONSE", "tool_calls", False),
        ("length", "MALFORMED_RESPONSE", "length", True),
        ("stop", "MALFORMED_RESPONSE", "stop", True),
        ("valid", None, "tool_calls", True),
        ("missing_calls", "MISSING_TOOL_CALL", "tool_calls", True),
        ("bad_arguments", "MALFORMED_ARGUMENTS", "tool_calls", True),
    ],
)
async def test_metadata_survives_validation_without_body(
    diagnostic_log, case, code, finish, message_object
):
    payload = response()
    choice = payload["choices"][0]
    choice["finish_reason"] = finish
    choice["message"]["content"] = "PRIVATE ASSISTANT CONTENT"
    if case == "missing_message":
        del choice["message"]
    elif case == "nonobject_message":
        choice["message"] = "PRIVATE ASSISTANT CONTENT"
    elif case == "missing_calls":
        del choice["message"]["tool_calls"]
    elif case == "bad_arguments":
        choice["message"]["tool_calls"][0]["function"]["arguments"] = "PRIVATE BAD ARGUMENTS"
    payload["usage"] = {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=payload, headers={"x-request-id": "request-123"})

    if code:
        with pytest.raises(AnalystProviderError) as exc:
            await generate(transport)
        assert exc.value.code == code
    else:
        await generate(transport)
    assert len(calls) == 1
    diagnostic = _response_metadata_event(diagnostic_log)
    assert diagnostic["http_status"] == 200
    assert diagnostic["response_id"] == "completion-123"
    assert diagnostic["request_id"] == "request-123"
    assert diagnostic["reported_model"] == MODEL
    assert diagnostic["choices_count"] == 1
    assert diagnostic["choice_type"] == "object"
    assert diagnostic["message_is_object"] is message_object
    assert diagnostic["finish_reason"] == finish
    assert diagnostic["message_gate_passed"] is message_object
    assert diagnostic["finish_reason_gate_passed"] is (finish == "tool_calls")
    assert diagnostic["usage"] == payload["usage"]
    has_calls = message_object and case != "missing_calls"
    assert diagnostic["tool_calls_present"] is has_calls
    assert diagnostic["tool_calls_count"] == (1 if has_calls else None)
    if has_calls:
        assert diagnostic["first_tool_call_type"] == "function"
        assert diagnostic["first_function_name"] == "emit_claims"
        assert diagnostic["arguments_present"] is True
        assert diagnostic["arguments_type"] == "string"
        assert diagnostic["arguments_length"] == len(
            choice["message"]["tool_calls"][0]["function"]["arguments"]
        )
    text = diagnostic_log.read_text()
    for forbidden in (SECRET, "PRIVATE", '{"claims": []}', "Authorization", "Bearer"):
        assert forbidden not in text
    for line in text.splitlines():
        assert len(line) < 2500


async def test_untrusted_metadata_is_bounded_and_redacted(diagnostic_log):
    payload = response(name=SECRET)
    payload["id"] = SECRET
    choice = payload["choices"][0]
    choice["finish_reason"] = "arbitrary provider prose " + "x" * 5000
    choice["message"]["content"] = {"Authorization": "Bearer private"}
    choice["message"]["tool_calls"][0]["type"] = "Bearer private"
    choice["message"]["tool_calls"][0]["function"]["arguments"] = SECRET
    payload["usage"] = {"prompt_tokens": True, "completion_tokens": "PRIVATE", "total_tokens": -1}
    with pytest.raises(AnalystProviderError):
        await generate(
            lambda _: httpx.Response(200, json=payload, headers={"x-request-id": SECRET})
        )
    d = _response_metadata_event(diagnostic_log)
    assert d["response_id"] is None and d["request_id"] is None
    assert d["finish_reason"] is None and d["finish_reason_gate_passed"] is False
    assert d["first_function_name"] is None and d["first_tool_call_type"] is None
    assert d["usage"] == {}
    assert d["content_type"] == "object"
    assert set(d["redacted_fields"]) == {
        "response_id",
        "request_id",
        "finish_reason",
        "first_tool_call_type",
        "first_function_name",
    }
    text = diagnostic_log.read_text()
    for forbidden in (SECRET, "PRIVATE", "Bearer", "Authorization", "arbitrary provider prose"):
        assert forbidden not in text
    for line in text.splitlines():
        assert len(line) < 2500


@pytest.mark.parametrize("finish", [None, "tool_calls"])
async def test_accepted_finish_reasons_remain_accepted(diagnostic_log, finish):
    payload = response()
    payload["choices"][0]["finish_reason"] = finish
    await generate(lambda _: httpx.Response(200, json=payload))
    d = _response_metadata_event(diagnostic_log)
    assert d["finish_reason"] == finish
    assert d["finish_reason_gate_passed"] is True
    assert d["content_present"] is False and d["content_type"] == "null"


@pytest.mark.parametrize("choices", [None, [], "private prose", [None], ["private prose"]])
async def test_malformed_choices_have_safe_shape_metadata(diagnostic_log, choices):
    payload = response()
    payload["choices"] = choices
    with pytest.raises(AnalystProviderError, match="Exactly one choice required"):
        await generate(lambda _: httpx.Response(200, json=payload))
    d = _response_metadata_event(diagnostic_log)
    assert d["choices_count"] == (len(choices) if isinstance(choices, list) else None)
    assert d["message_is_object"] is False
    assert "private prose" not in diagnostic_log.read_text()


async def test_model_metadata_secret_and_unsafe_usage_are_not_logged(diagnostic_log):
    payload = response(model=SECRET)
    payload["usage"] = {"prompt_tokens": 10**100, "completion_tokens": 4.5, "total_tokens": None}
    with pytest.raises(AnalystProviderError, match="Response model differs"):
        await generate(lambda _: httpx.Response(200, json=payload))
    d = _response_metadata_event(diagnostic_log)
    assert d["reported_model"] is None
    assert d["redacted_fields"] == ["reported_model"]
    assert d["usage"] == {"completion_tokens": 4.5}
    assert SECRET not in diagnostic_log.read_text()
