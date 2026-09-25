"""Transport-failure diagnostics: sanitized shape-only metadata for requests that never
received a response (DNS/connect/TLS/reset failures, and provider timeouts)."""

import json
import logging

import httpx
import pytest
from test_aicredits_analyst import SECRET, generate, no_sleep  # noqa: F401

from stock_research_ai.adapters.aicredits import analyst as adapter


@pytest.fixture
def diagnostic_log(tmp_path, monkeypatch):
    path = tmp_path / "service.log"
    logger = logging.Logger("transport-diagnostics-test", logging.INFO)
    handler = logging.FileHandler(path)
    logger.addHandler(handler)
    monkeypatch.setattr(adapter, "get_logger", lambda _: logger, raising=False)
    yield path
    handler.close()


def _read_events(path) -> list[dict]:
    text = path.read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _transport_events(path) -> list[dict]:
    return [e for e in _read_events(path) if e["event"] == "aicredits_transport_error"]


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError],
)
async def test_non_timeout_transport_errors_emit_connection_failed_diagnostics(
    diagnostic_log, failure
):
    calls = []

    def handler(request):
        calls.append(request)
        raise failure(SECRET, request=request)

    with pytest.raises(adapter.AnalystProviderError) as exc:
        await generate(handler)

    assert exc.value.code == "CONNECTION_FAILED"
    assert len(calls) == 3

    events = _transport_events(diagnostic_log)
    assert len(events) == 3
    for i, event in enumerate(events):
        assert event["event"] == "aicredits_transport_error"
        assert event["exception_type"] == failure.__name__
        assert event["timeout"] is False
        assert event["attempt_number"] == i + 1
        assert event["max_attempts"] == 3


async def test_timeout_exception_remains_timeout_and_is_not_mislabeled(diagnostic_log):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET, request=request)

    with pytest.raises(adapter.AnalystProviderError) as exc:
        await generate(handler)

    assert exc.value.code == "TIMEOUT"
    events = _transport_events(diagnostic_log)
    assert len(events) == 3
    for event in events:
        assert event["event"] == "aicredits_transport_error"
        assert event["exception_type"] == "ReadTimeout"
        assert event["timeout"] is True


async def test_diagnostic_survives_final_retry_failure(diagnostic_log):
    def handler(request):
        raise httpx.ConnectError(SECRET, request=request)

    with pytest.raises(adapter.AnalystProviderError):
        await generate(handler)

    events = _transport_events(diagnostic_log)
    final = events[-1]
    assert final["attempt_number"] == final["max_attempts"] == 3
    assert final["event"] == "aicredits_transport_error"


async def test_os_errno_and_cause_chain_are_captured_when_present(diagnostic_log):
    def handler(request):
        try:
            raise ConnectionRefusedError(61, "Connection refused")
        except ConnectionRefusedError as os_exc:
            raise httpx.ConnectError("connect failed", request=request) from os_exc

    with pytest.raises(adapter.AnalystProviderError):
        await generate(handler)

    events = _transport_events(diagnostic_log)
    event = events[0]
    assert event["cause_type"] == "ConnectionRefusedError"
    assert event["root_cause_type"] == "ConnectionRefusedError"
    assert event["os_errno"] == 61


async def test_no_secret_or_request_body_leakage(diagnostic_log):
    def handler(request):
        raise httpx.ConnectError(SECRET, request=request)

    with pytest.raises(adapter.AnalystProviderError):
        await generate(handler)

    text = diagnostic_log.read_text()
    for forbidden in (SECRET, "Authorization", "Bearer", "claims"):
        assert forbidden not in text
    for event in _transport_events(diagnostic_log):
        assert set(event) == {
            "event",
            "schema_version",
            "attempt_number",
            "max_attempts",
            "exception_type",
            "cause_type",
            "root_cause_type",
            "os_errno",
            "timeout",
            "timestamp",
        }
