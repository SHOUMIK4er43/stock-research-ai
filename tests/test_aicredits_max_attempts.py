"""Configurable AI Credits transport retry count (analyst_max_attempts). Default
behavior (3 attempts, 1.0s/4.0s backoff) is unchanged; the attempt count is now
sourced from Settings/constructor instead of a fixed module constant. No parser,
normalization, schema, validator, evidence-budget, timeout-value, or Anthropic-adapter
behavior is touched by this change.
"""

import json
import logging

import httpx
import pytest
from pydantic import ValidationError
from test_aicredits_analyst import SECRET, generate, response

from stock_research_ai.adapters.aicredits import analyst as adapter
from stock_research_ai.adapters.analyst_contract import AnalystProviderError
from stock_research_ai.config import Settings


@pytest.fixture
def sleep_calls(monkeypatch):
    calls: list[float] = []

    async def spy(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(adapter.asyncio, "sleep", spy)
    return calls


@pytest.fixture
def diagnostic_log(tmp_path, monkeypatch):
    path = tmp_path / "service.log"
    logger = logging.Logger("max-attempts-test", logging.INFO)
    handler = logging.FileHandler(path)
    logger.addHandler(handler)
    monkeypatch.setattr(adapter, "get_logger", lambda _: logger, raising=False)
    yield path
    handler.close()


def _transport_error_events(path) -> list[dict]:
    text = path.read_text()
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [e for e in events if e["event"] == "aicredits_transport_error"]


# --- A/B/C: CONFIG --------------------------------------------------------------------


def test_default_max_attempts_is_three():
    assert Settings(_env_file=None).analyst_max_attempts == 3


def test_environment_override_resolves(monkeypatch):
    monkeypatch.setenv("STOCK_RESEARCH_AI_ANALYST_MAX_ATTEMPTS", "1")
    assert Settings(_env_file=None).analyst_max_attempts == 1


@pytest.mark.parametrize("value", [0, -1, 4])
def test_invalid_max_attempts_rejected(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, analyst_max_attempts=value)


# --- D/E/F/G: RETRY LOOP BEHAVIOR ------------------------------------------------------


async def test_one_attempt_no_retry_sleep_on_failure(sleep_calls):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET, request=request)

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler, max_attempts=1)
    assert exc.value.code == "TIMEOUT"
    assert len(calls) == 1
    assert sleep_calls == []


async def test_two_attempts_exactly_one_backoff(sleep_calls):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET, request=request)

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler, max_attempts=2)
    assert exc.value.code == "TIMEOUT"
    assert len(calls) == 2
    assert sleep_calls == [1.0]


async def test_three_attempts_default_backoff_preserved(sleep_calls):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET, request=request)

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)  # default max_attempts == 3
    assert exc.value.code == "TIMEOUT"
    assert len(calls) == 3
    assert sleep_calls == [1.0, 4.0]


@pytest.mark.parametrize("max_attempts", [1, 2, 3])
async def test_successful_first_attempt_never_retries(sleep_calls, max_attempts):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    metadata = await generate(handler, max_attempts=max_attempts)
    assert metadata.raw_response.structured_payload["claims"] == []
    assert len(calls) == 1
    assert sleep_calls == []


# --- H: TRANSPORT DIAGNOSTIC REFLECTS CONFIGURED VALUE ---------------------------------


async def test_transport_diagnostic_reflects_configured_max_attempts(
    diagnostic_log, sleep_calls
):
    def handler(request):
        raise httpx.ReadTimeout(SECRET, request=request)

    with pytest.raises(AnalystProviderError):
        await generate(handler, max_attempts=2)

    events = _transport_error_events(diagnostic_log)
    assert [e["attempt_number"] for e in events] == [1, 2]
    assert [e["max_attempts"] for e in events] == [2, 2]
