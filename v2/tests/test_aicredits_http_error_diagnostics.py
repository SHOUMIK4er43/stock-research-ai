"""Safe non-200 HTTP diagnostics: aicredits_http_error never logs the response body,
provider prose, request content, or the credential - status/code/attempt shape only.
"""

import json
import logging

import httpx
import pytest
from test_aicredits_analyst import SECRET, generate, no_sleep  # noqa: F401

from stock_research_ai.adapters.aicredits import analyst as adapter
from stock_research_ai.adapters.analyst_contract import AnalystProviderError


@pytest.fixture
def diagnostic_log(tmp_path, monkeypatch):
    path = tmp_path / "service.log"
    logger = logging.Logger("http-error-diagnostics-test", logging.INFO)
    handler = logging.FileHandler(path)
    logger.addHandler(handler)
    monkeypatch.setattr(adapter, "get_logger", lambda _: logger, raising=False)
    yield path
    handler.close()


def _events(path) -> list[dict]:
    text = path.read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _http_error_events(path) -> list[dict]:
    return [e for e in _events(path) if e["event"] == "aicredits_http_error"]


@pytest.mark.parametrize(
    "status,expected_code,retryable,attempts",
    [
        (401, "AUTHENTICATION_FAILED", False, 1),
        (402, "INSUFFICIENT_CREDITS", False, 1),
        (429, "RATE_LIMITED", True, 3),
        (503, "PROVIDER_UNAVAILABLE", True, 3),
        (418, "INVALID_REQUEST", False, 1),
    ],
)
async def test_non_200_status_produces_http_error_event(
    diagnostic_log, status, expected_code, retryable, attempts
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": SECRET}},
            headers={"content-type": "application/json", "content-length": "42"},
        )

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == expected_code
    assert len(calls) == attempts

    events = _http_error_events(diagnostic_log)
    assert len(events) == attempts
    for i, event in enumerate(events):
        assert event["http_status"] == status
        assert event["mapped_error_code"] == expected_code
        assert event["attempt_number"] == i + 1
        assert event["max_attempts"] == 3
        assert event["retryable"] is retryable
        assert event["response_content_type"] == "application/json"
        assert event["response_content_length"] == "42"
        assert set(event) == {
            "event",
            "schema_version",
            "timestamp",
            "http_status",
            "mapped_error_code",
            "attempt_number",
            "max_attempts",
            "retryable",
            "response_content_type",
            "response_content_length",
        }


async def test_retryable_flag_matches_existing_retry_semantics(diagnostic_log):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={"error": {"message": SECRET}})

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == "PROVIDER_UNAVAILABLE"
    assert len(calls) == 3

    events = _http_error_events(diagnostic_log)
    assert len(events) == 3
    assert all(e["retryable"] is True for e in events)
    # Same code, exhausted retries: adapter still raises after the 3rd attempt -
    # the diagnostic's retryable flag describes the code class, not the outcome.
    assert events[-1]["attempt_number"] == events[-1]["max_attempts"] == 3


async def test_no_secret_or_body_leakage_in_http_error_events(diagnostic_log):
    def handler(request):
        body = b'{"error":{"message":"' + SECRET.encode() + b'","code":"insufficient_credits"}}'
        return httpx.Response(
            402,
            content=body,
            headers={"content-type": "application/json"},
        )

    with pytest.raises(AnalystProviderError):
        await generate(handler)

    text = diagnostic_log.read_text()
    for forbidden in (SECRET, "Authorization", "Bearer", "insufficient_credits", "claims"):
        assert forbidden not in text
    for line in text.splitlines():
        assert len(line) < 1000
