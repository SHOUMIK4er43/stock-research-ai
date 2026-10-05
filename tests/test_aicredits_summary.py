"""SUMMARY_V1 AI Credits adapter: MockTransport only, never a live provider call."""

import json
import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from test_summary_input import INFY, replay

from stock_research_ai.adapters.aicredits import summary as adapter
from stock_research_ai.adapters.aicredits.summary import AICreditsSummaryModel
from stock_research_ai.application.summary import project_summary_input
from stock_research_ai.application.summary_generation import (
    build_summary_messages,
    generate_summary,
)
from stock_research_ai.config import Settings
from stock_research_ai.domain.summary import (
    SummaryErrorCode,
    SummaryGenerationError,
    SummaryUsage,
)

SECRET = "summary-test-only-credential-not-real"
MODEL = "anthropic/claude-sonnet-5"
SYSTEM = "SUMMARY_PROMPT_V1 test system PROMPT-MARKER"
USER = 'SUMMARY_INPUT_V1 JSON:\n{"company":"USER-MARKER"}'
TEXT = "Company overview\nExample Ltd reports standalone figures.\n\nOverall summary\nNeutral."
BODY_MARKER = "RESPONSE-BODY-MARKER"


def payload(
    content: object = TEXT,
    *,
    finish_reason: object = "stop",
    model: object = "claude-sonnet-5",
    choices: int = 1,
    tool_calls: object = None,
    usage: object = None,
) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, object] = {
        "id": "completion-1",
        "model": model,
        "choices": [
            {"index": i, "finish_reason": finish_reason, "message": message} for i in range(choices)
        ],
        "usage": usage or {"prompt_tokens": 3100, "completion_tokens": 700, "total_tokens": 3800},
    }
    return body


class Recorder:
    """A MockTransport handler that records every invocation."""

    def __init__(self, respond) -> None:
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def ok(body: object) -> Recorder:
    return Recorder(lambda _r: httpx.Response(200, json=body))


def raw(status: int, content: bytes) -> Recorder:
    return Recorder(lambda _r: httpx.Response(status, content=content))


def raising(exc: Exception) -> Recorder:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise exc

    return Recorder(respond)


def model_for(handler: Recorder, **kwargs: object) -> AICreditsSummaryModel:
    return AICreditsSummaryModel(
        api_key=SECRET, model=MODEL, transport=httpx.MockTransport(handler), **kwargs
    )


async def complete(handler: Recorder, system: str = SYSTEM, user: str = USER):
    return await model_for(handler).complete(system=system, user=user)


async def expect_error(handler: Recorder, code: SummaryErrorCode) -> SummaryGenerationError:
    with pytest.raises(SummaryGenerationError) as error:
        await complete(handler)
    assert error.value.code == code
    assert len(handler.requests) <= 1
    return error.value


@pytest.fixture
def logs():
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("stock_research_ai.aicredits.summary")
    handler = Capture(level=logging.DEBUG)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(previous)


async def test_valid_response_maps_to_output():
    handler = ok(payload())
    output = await complete(handler)
    assert len(handler.requests) == 1
    assert output.summary_text == TEXT
    assert output.requested_model == MODEL
    assert output.reported_model == "claude-sonnet-5"
    assert output.finish_reason == "stop"
    assert output.usage == SummaryUsage(prompt_tokens=3100, completion_tokens=700)


async def test_exact_request_body_and_headers():
    handler = ok(payload())
    await complete(handler)
    (request,) = handler.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.aicredits.in/v1/chat/completions"
    body = json.loads(request.content)
    assert body == {
        "model": MODEL,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
    }
    assert "tools" not in body and "tool_choice" not in body
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert request.headers["content-type"] == "application/json"
    assert SECRET.encode() not in request.content


async def test_content_is_returned_untrimmed():
    padded = "\n  " + TEXT + "  \n"
    assert (await complete(ok(payload(padded)))).summary_text == padded


@pytest.mark.parametrize(
    "body",
    [
        payload(None),
        payload([{"type": "text", "text": TEXT}]),
        payload({"text": TEXT}),
        payload(""),
        payload(" \n\t "),
        payload(choices=2),
        payload(choices=0),
        payload(tool_calls=[{"id": "c", "type": "function", "function": {"name": "x"}}]),
        payload(finish_reason="length"),
        payload(finish_reason="tool_calls"),
        payload(finish_reason=None),
        payload(model=7),
        {"choices": "not-a-list"},
        ["not", "an", "object"],
    ],
)
async def test_invalid_response_shapes_are_rejected(body):
    handler = ok(body)
    await expect_error(handler, SummaryErrorCode.INVALID_PROVIDER_RESPONSE)
    assert len(handler.requests) == 1


async def test_empty_tool_call_list_is_not_a_tool_call():
    assert (await complete(ok(payload(tool_calls=[])))).summary_text == TEXT


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b'{"choices": [',
        b'{"model": "a", "model": "b", "choices": []}',
        b'{"choices": [], "x": NaN}',
    ],
)
async def test_malformed_and_duplicate_key_json_is_rejected(content):
    await expect_error(raw(200, content), SummaryErrorCode.INVALID_PROVIDER_RESPONSE)


async def test_oversized_response_body_is_rejected():
    await expect_error(
        raw(200, b" " * (adapter.MAX_RESPONSE_BYTES + 1)),
        SummaryErrorCode.INVALID_PROVIDER_RESPONSE,
    )


async def test_oversized_request_is_rejected_before_any_http():
    handler = ok(payload())
    with pytest.raises(SummaryGenerationError) as error:
        await complete(handler, user="x" * adapter.MAX_REQUEST_BYTES)
    assert error.value.code == SummaryErrorCode.SUMMARY_REQUEST_TOO_LARGE
    assert handler.requests == []


async def test_credential_in_request_content_is_refused_before_http():
    handler = ok(payload())
    with pytest.raises(SummaryGenerationError) as error:
        await complete(handler, user=f"leak {SECRET}")
    assert error.value.code == SummaryErrorCode.UNSAFE_REQUEST
    assert handler.requests == []


@pytest.mark.parametrize(
    "status,code",
    [
        (401, SummaryErrorCode.CREDENTIAL_UNAVAILABLE),
        (402, SummaryErrorCode.PROVIDER_HTTP_ERROR),
        (403, SummaryErrorCode.PROVIDER_HTTP_ERROR),
        (404, SummaryErrorCode.PROVIDER_HTTP_ERROR),
        (418, SummaryErrorCode.PROVIDER_HTTP_ERROR),
        (429, SummaryErrorCode.PROVIDER_HTTP_ERROR),
        (500, SummaryErrorCode.PROVIDER_HTTP_ERROR),
        (503, SummaryErrorCode.PROVIDER_HTTP_ERROR),
    ],
)
async def test_http_errors_map_to_fixed_codes_with_one_request(status, code):
    body = json.dumps({"error": {"code": "x", "message": BODY_MARKER}}).encode()
    handler = raw(status, body)
    error = await expect_error(handler, code)
    assert len(handler.requests) == 1
    assert BODY_MARKER not in str(error)


@pytest.mark.parametrize(
    "exc,code,ambiguous",
    [
        (httpx.ReadTimeout("EXC-MARKER"), SummaryErrorCode.PROVIDER_TIMEOUT, True),
        (httpx.WriteTimeout("EXC-MARKER"), SummaryErrorCode.PROVIDER_TIMEOUT, True),
        (httpx.ConnectError("EXC-MARKER"), SummaryErrorCode.PROVIDER_UNAVAILABLE, False),
        (httpx.ConnectTimeout("EXC-MARKER"), SummaryErrorCode.PROVIDER_UNAVAILABLE, False),
        (httpx.ReadError("EXC-MARKER"), SummaryErrorCode.PROVIDER_CONNECTION_LOST, True),
        (httpx.RemoteProtocolError("EXC-MARKER"), SummaryErrorCode.PROVIDER_CONNECTION_LOST, True),
    ],
)
async def test_transport_failures_are_never_retried(exc, code, ambiguous):
    handler = raising(exc)
    error = await expect_error(handler, code)
    assert len(handler.requests) == 1
    assert error.ambiguous is ambiguous
    assert "EXC-MARKER" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__


@pytest.mark.parametrize("reported", [MODEL, "claude-sonnet-5", None])
async def test_accepted_model_echoes(reported):
    assert (await complete(ok(payload(model=reported)))).reported_model == reported


@pytest.mark.parametrize("reported", ["anthropic/claude-opus-5-5", "sonnet-5", "", "x" * 500])
async def test_different_model_is_a_mismatch(reported):
    await expect_error(ok(payload(model=reported)), SummaryErrorCode.MODEL_MISMATCH)


async def test_requested_model_without_prefix_does_not_accept_a_prefixed_echo():
    handler = ok(payload(model=MODEL))
    with pytest.raises(SummaryGenerationError) as error:
        await AICreditsSummaryModel(
            api_key=SECRET, model="claude-sonnet-5", transport=httpx.MockTransport(handler)
        ).complete(system=SYSTEM, user=USER)
    assert error.value.code == SummaryErrorCode.MODEL_MISMATCH


async def test_credential_echoed_in_content_is_unsafe():
    await expect_error(ok(payload(f"text {SECRET}")), SummaryErrorCode.UNSAFE_CONTENT)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": -1, "completion_tokens": 2},
        {"prompt_tokens": True, "completion_tokens": 2},
        "bad",
    ],
)
async def test_unusable_usage_is_ignored_not_trusted(usage):
    assert (await complete(ok(payload(usage=usage)))).usage is None


async def test_errors_and_logs_never_carry_secrets_prompts_or_bodies(logs):
    handlers = [
        ok(payload(f"{BODY_MARKER} \x00", finish_reason="length")),
        raw(500, f'{{"error": "{BODY_MARKER}"}}'.encode()),
        raw(401, BODY_MARKER.encode()),
        raw(200, BODY_MARKER.encode()),
        raising(httpx.ReadTimeout(f"EXC-MARKER {SECRET}")),
        raising(httpx.ConnectError("EXC-MARKER")),
        ok(payload(model="other/model")),
    ]
    rendered: list[str] = []
    for handler in handlers:
        with pytest.raises(SummaryGenerationError) as error:
            await complete(handler)
        rendered.append(f"{error.value} {error.value.args!r} {error.value.message}")
    await complete(ok(payload()))
    rendered.extend(logs)
    assert logs, "shape-only diagnostics are still emitted"
    for text in rendered:
        for forbidden in (
            SECRET,
            "Authorization",
            "Bearer",
            "PROMPT-MARKER",
            "USER-MARKER",
            BODY_MARKER,
            "EXC-MARKER",
            TEXT,
        ):
            assert forbidden not in text


def test_construction_rejects_unusable_configuration():
    for kwargs, code in (
        ({"api_key": "", "model": MODEL}, SummaryErrorCode.CREDENTIAL_UNAVAILABLE),
        ({"api_key": SECRET, "model": " "}, SummaryErrorCode.PROVIDER_NOT_CONFIGURED),
        ({"api_key": SECRET, "model": MODEL + " "}, SummaryErrorCode.PROVIDER_NOT_CONFIGURED),
        (
            {"api_key": SECRET, "model": MODEL, "max_output_tokens": 1501},
            SummaryErrorCode.PROVIDER_NOT_CONFIGURED,
        ),
    ):
        with pytest.raises(SummaryGenerationError) as error:
            AICreditsSummaryModel(**kwargs)
        assert error.value.code == code


class FakeSecretStore:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.names: list[str] = []

    def get(self, name: str) -> str | None:
        self.names.append(name)
        return self.value

    def set(self, name: str, value: str) -> None:
        raise AssertionError("must not write credentials")

    def delete(self, name: str) -> None:
        raise AssertionError("must not delete credentials")


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "analyst_provider": "aicredits",
        "aicredits_model": MODEL,
        "aicredits_api_key": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


async def test_from_settings_uses_the_existing_keychain_resolution():
    store = FakeSecretStore(SECRET)
    handler = ok(payload())
    model = AICreditsSummaryModel.from_settings(
        settings(), store, transport=httpx.MockTransport(handler)
    )
    assert store.names == ["aicredits-api-key"]
    await model.complete(system=SYSTEM, user=USER)
    assert handler.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    assert json.loads(handler.requests[0].content)["model"] == MODEL


@pytest.mark.parametrize(
    "overrides,stored,code",
    [
        ({}, None, SummaryErrorCode.CREDENTIAL_UNAVAILABLE),
        ({"analyst_provider": None}, SECRET, SummaryErrorCode.PROVIDER_NOT_CONFIGURED),
        ({"analyst_provider": "anthropic"}, SECRET, SummaryErrorCode.PROVIDER_NOT_CONFIGURED),
        ({"aicredits_model": None}, SECRET, SummaryErrorCode.PROVIDER_NOT_CONFIGURED),
    ],
)
def test_from_settings_failures_are_typed(overrides, stored, code):
    with pytest.raises(SummaryGenerationError) as error:
        AICreditsSummaryModel.from_settings(settings(**overrides), FakeSecretStore(stored))
    assert error.value.code == code


async def test_generate_summary_through_the_adapter_with_mock_transport(tmp_path, fixture_dir):
    value = project_summary_input(await replay(tmp_path, fixture_dir, INFY))
    system, user = build_summary_messages(value)
    handler = ok(payload())
    result = await generate_summary(
        value,
        model_for(handler),
        now=datetime(2026, 10, 5, tzinfo=UTC),
        summary_id=UUID("00000000-0000-4000-8000-000000000002"),
    )
    (request,) = handler.requests
    body = json.loads(request.content)
    assert body["messages"] == [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    assert len(request.content) < adapter.MAX_REQUEST_BYTES
    assert result.company.name == "Infosys Ltd"
    assert result.summary_text == TEXT
    assert result.reported_model == "claude-sonnet-5"
    assert result.provider == "aicredits"
