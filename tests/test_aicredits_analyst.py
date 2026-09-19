"""P1 transport and grounding proofs: static responses and MockTransport only."""

import hashlib
import json

import httpx
import pytest
from test_analysis import world as evidence_world
from test_anthropic_analyst import _bundle_and_ref, _execution, _projection

from stock_research_ai.adapters.aicredits import analyst as adapter
from stock_research_ai.adapters.aicredits.analyst import AICreditsAnalystModel
from stock_research_ai.adapters.analyst_contract import AnalystProviderError
from stock_research_ai.application.analysis_validation import validate_response
from stock_research_ai.application.analyst_model_input import ModelInputError, build_model_input
from stock_research_ai.application.hashing import digest

world = evidence_world
SECRET = "p1-test-only-credential-not-real"
MODEL = "configured-catalog-id"


def response(arguments='{"claims": []}', name="emit_claims", model=MODEL):
    return {
        "id": "completion-123",
        "model": model,
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
            }
        ],
    }


async def generate(handler, bundle=None, projection=None, **kwargs):
    bundle = bundle or _bundle_and_ref()[0]
    return await AICreditsAnalystModel(
        api_key=SECRET, model=MODEL, transport=httpx.MockTransport(handler), **kwargs
    ).generate(bundle, _execution(), model_input=projection or _projection(bundle))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        pass

    monkeypatch.setattr(adapter.asyncio, "sleep", instant)


async def test_exact_wire_and_provenance():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response(), headers={"x-request-id": "request-123"})

    bundle, _ = _bundle_and_ref()
    metadata = await generate(handler, bundle)
    request = calls[0]
    assert str(request.url) == "https://api.aicredits.in/v1/chat/completions"
    assert request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["max_tokens"] == 4096
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == metadata.request.template.content
    tool = body["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "emit_claims"
    assert tool["function"]["parameters"]["required"] == ["claims"]
    assert body["tool_choice"] == {"type": "function", "function": {"name": "emit_claims"}}
    assert body["parallel_tool_calls"] is False
    assert request.content.decode() == metadata.request.rendered_text
    assert hashlib.sha256(request.content).hexdigest() == metadata.request.rendered_sha256
    assert metadata.request.bundle_sha256 == digest(bundle)
    assert metadata.provider == "aicredits"
    assert metadata.model_id == metadata.model_version == MODEL
    provenance = metadata.raw_response.structured_payload["_provider_metadata"]
    assert provenance == {"response_id": "completion-123", "request_id": "request-123"}
    assert SECRET not in metadata.model_dump_json()
    assert "authorization" not in metadata.model_dump_json().lower()
    assert len(calls) == 1


async def test_real_frozen_evidence_and_unchanged_validator(world):
    _, _, _, document, bundle, evidence = world
    projection = build_model_input(bundle, evidence)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    metadata = await generate(handler, bundle, projection)
    wire = json.loads(calls[0]["messages"][1]["content"])
    assert wire["model_input"]["bundle_sha256"] == digest(bundle)
    assert [i["reference"] for i in wire["model_input"]["items"]] == [
        r.model_dump(mode="json") for r in bundle.sources
    ]
    assert document.chunks[0].text in json.dumps(wire).replace("\\n", "\n")
    assert any(i["content"] for i in wire["model_input"]["items"])
    assert validate_response(
        metadata.raw_response, bundle, evidence, bundle.request.profile_id
    ) == ((), ())


@pytest.mark.parametrize("mutation", ["hash", "missing", "extra"])
async def test_bad_projection_fails_before_transport(mutation):
    bundle, _ = _bundle_and_ref()
    projection = _projection(bundle)
    update = {
        "hash": {"bundle_sha256": "b" * 64},
        "missing": {"items": ()},
        "extra": {"items": projection.items + projection.items},
    }[mutation]
    projection = projection.model_copy(update=update)

    def forbidden(request):
        raise AssertionError("Transport must not run")

    with pytest.raises(ModelInputError):
        await generate(forbidden, bundle, projection)


@pytest.mark.parametrize(
    "arguments",
    [
        "not json",
        "[]",
        "null",
        '{"claims":NaN}',
        '{"claims":[{"value":1e999}]}',
        '{"claims":[],"claims":[]}',
        '{"claims":"bad"}',
    ],
)
async def test_bad_arguments_do_not_retry(arguments):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response(arguments=arguments))

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == "MALFORMED_ARGUMENTS"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kind,code",
    [
        ("wrong", "UNEXPECTED_TOOL_CALL"),
        ("missing", "MISSING_TOOL_CALL"),
        ("multiple", "UNEXPECTED_TOOL_CALL"),
        ("malformed", "MALFORMED_RESPONSE"),
        ("choices", "MALFORMED_RESPONSE"),
        ("truncated", "MALFORMED_RESPONSE"),
        ("nonjson", "MALFORMED_RESPONSE"),
        ("mismatch", "MODEL_MISMATCH"),
    ],
)
async def test_invalid_response_is_typed_without_semantic_retry(kind, code):
    payload = response()
    if kind == "wrong":
        payload = response(name="other")
    elif kind == "missing":
        payload["choices"][0]["message"].pop("tool_calls")
    elif kind == "multiple":
        payload["choices"][0]["message"]["tool_calls"] *= 2
    elif kind == "malformed":
        payload = []
    elif kind == "choices":
        payload["choices"] *= 2
    elif kind == "truncated":
        payload["choices"][0]["finish_reason"] = "length"
    elif kind == "mismatch":
        payload["model"] = "another-model"
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(200, content=b"not json")
            if kind == "nonjson"
            else httpx.Response(200, json=payload)
        )

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == code
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,code,attempts",
    [
        (401, "AUTHENTICATION_FAILED", 1),
        (403, "PERMISSION_DENIED", 1),
        (402, "INSUFFICIENT_CREDITS", 1),
        (404, "MODEL_UNAVAILABLE", 1),
        (400, "INVALID_REQUEST", 1),
        (429, "RATE_LIMITED", 3),
        (500, "PROVIDER_UNAVAILABLE", 3),
        (503, "PROVIDER_UNAVAILABLE", 3),
    ],
)
async def test_http_errors_are_safe_and_bounded(status, code, attempts):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": SECRET}})

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == code
    assert SECRET not in str(exc.value)
    assert len(calls) == attempts
    assert len({r.content for r in calls}) == 1


@pytest.mark.parametrize(
    "failure,code", [(httpx.ReadTimeout, "TIMEOUT"), (httpx.ConnectError, "CONNECTION_FAILED")]
)
async def test_transport_errors_are_safe_and_bounded(failure, code):
    calls = []

    def handler(request):
        calls.append(request)
        raise failure(SECRET, request=request)

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == code
    assert SECRET not in str(exc.value)
    assert exc.value.__suppress_context__
    assert len(calls) == 3


async def test_retry_can_succeed_without_changing_model_or_body():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=response())

    await generate(handler)
    assert len(calls) == 2
    assert calls[0].content == calls[1].content


async def test_absent_response_model_is_not_invented():
    payload = response()
    del payload["model"]
    metadata = await generate(lambda r: httpx.Response(200, json=payload))
    assert metadata.model_id == MODEL
    assert metadata.model_version is None


async def test_secret_echo_cannot_be_persisted():
    payload = response(arguments=json.dumps({"claims": [{"statement": SECRET}]}))
    with pytest.raises(AnalystProviderError, match="UNSAFE_RESPONSE"):
        await generate(lambda r: httpx.Response(200, json=payload))


async def test_candidate_shape_and_validation_remain_authoritative(world):
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
    claims = [candidate, {**candidate, "statement": "BUY now."}]
    metadata = await generate(
        lambda r: httpx.Response(200, json=response(arguments=json.dumps({"claims": claims}))),
        bundle,
        build_model_input(bundle, evidence),
    )
    assert metadata.raw_response.structured_payload["claims"] == claims
    accepted, rejected = validate_response(
        metadata.raw_response, bundle, evidence, bundle.request.profile_id
    )
    assert len(accepted) == len(rejected) == 1
    assert accepted[0].statement == candidate["statement"]
    assert accepted[0].epistemic_class == "INTERPRETATION"


async def test_unpinned_financial_content_absent_from_document_only_request(world):
    *_, document, bundle, evidence = world
    ref = next(r for r in bundle.sources if r.kind == "DOCUMENT_CHUNK")
    narrow = bundle.model_copy(update={"sources": (ref,)})
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    await generate(handler, narrow, build_model_input(narrow, evidence))
    projection = json.loads(captured[0]["messages"][1]["content"])["model_input"]
    assert len(projection["items"]) == 1
    assert projection["items"][0]["reference"]["kind"] == "DOCUMENT_CHUNK"
    assert projection["items"][0]["content"]["chunk"]["text"] == document.chunks[0].text


async def test_projection_overflow_precedes_transport(monkeypatch):
    import stock_research_ai.application.analyst_model_input as r1

    monkeypatch.setattr(r1, "MAX_INPUT_BYTES", 1)

    def forbidden(request):
        raise AssertionError("Transport must not run")

    with pytest.raises(ModelInputError):
        await generate(forbidden)


async def test_oversized_response_fails_without_retry(monkeypatch):
    monkeypatch.setattr(adapter, "_MAX_RESPONSE_BYTES", 10)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * 11)

    with pytest.raises(AnalystProviderError, match="RESPONSE_TOO_LARGE"):
        await generate(handler)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,provider_code,expected",
    [
        (400, "model_not_found", "MODEL_UNAVAILABLE"),
        (429, "insufficient_quota", "INSUFFICIENT_CREDITS"),
        (400, "insufficient_credits", "INSUFFICIENT_CREDITS"),
    ],
)
async def test_machine_error_codes_fail_once(status, provider_code, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": provider_code, "message": SECRET}})

    with pytest.raises(AnalystProviderError) as exc:
        await generate(handler)
    assert exc.value.code == expected
    assert len(calls) == 1


async def test_requested_model_and_effective_parameters_exact_on_wire():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response(model="second-catalog-model"))

    bundle, _ = _bundle_and_ref()
    from stock_research_ai.domain.analysis import AnalystExecutionSpec

    parameters = {"temperature": 0.25, "top_p": 0.5, "max_output_tokens": 900}
    result = await AICreditsAnalystModel(
        SECRET, "second-catalog-model", transport=httpx.MockTransport(handler)
    ).generate(
        bundle,
        AnalystExecutionSpec(
            template_id="test", template_version=1, generation_parameters=parameters
        ),
        model_input=_projection(bundle),
    )
    body = json.loads(calls[0].content)
    assert body["model"] == result.model_id == result.model_version == "second-catalog-model"
    assert body["max_tokens"] == 900
    assert body["temperature"] == 0.25
    assert body["top_p"] == 0.5
    assert result.request.generation_parameters == parameters
    expected = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    assert (
        result.request.generation_parameters_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    )
    assert (
        result.raw_response.structured_payload_sha256
        == hashlib.sha256(
            json.dumps(
                result.raw_response.structured_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"unknown": True},
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"max_output_tokens": "123"},
        {"temperature": "0.5"},
        {"temperature": 3},
        {"top_p": -1},
        {"top_p": float("nan")},
    ],
)
async def test_invalid_generation_configuration_precedes_transport(parameters):
    from stock_research_ai.domain.analysis import AnalystExecutionSpec

    bundle, _ = _bundle_and_ref()

    def forbidden(request):
        raise AssertionError("Transport must not run")

    with pytest.raises(AnalystProviderError, match="INVALID_REQUEST"):
        await AICreditsAnalystModel(
            SECRET, MODEL, transport=httpx.MockTransport(forbidden)
        ).generate(
            bundle,
            AnalystExecutionSpec(
                template_id="test", template_version=1, generation_parameters=parameters
            ),
            model_input=_projection(bundle),
        )


@pytest.mark.parametrize("identifier", [SECRET, "Authorization: Bearer something", "a\nb", 123])
async def test_unsafe_provider_identifier_never_persisted(identifier):
    payload = response()
    payload["id"] = identifier
    with pytest.raises(AnalystProviderError):
        await generate(lambda r: httpx.Response(200, json=payload))


async def test_aicredits_persistence_challenge_report_chain(world, tmp_path):
    from stock_research_ai.adapters.sqlite import SQLiteDatabase
    from stock_research_ai.bootstrap import analyze_company, challenge_analysis, generate_report
    from stock_research_ai.config import Settings
    from stock_research_ai.domain.challenge import ChallengeRequest
    from stock_research_ai.domain.report import ReportRequest

    *_, bundle, _ = world
    settings = Settings(
        _env_file=None, database_path=tmp_path / "proof.sqlite3", artifact_path=tmp_path / "raw"
    )
    calls = []

    def handler(request):
        calls.append(request)
        wire = json.loads(json.loads(request.content)["messages"][1]["content"])
        ref = next(r for r in wire["bundle"]["sources"] if r["kind"] == "VALUATION")
        claim = {
            "section": "VALUATION",
            "statement": "Observed valuation evidence is cited.",
            "materiality": "PRIMARY",
            "citations": [{k: ref[k] for k in ("kind", "owning_snapshot_id", "item_key")}],
        }
        return httpx.Response(200, json=response(arguments=json.dumps({"claims": [claim]})))

    model = AICreditsAnalystModel(SECRET, MODEL, transport=httpx.MockTransport(handler))
    analysis = await analyze_company(
        settings,
        bundle.request,
        _execution(),
        tmp_path / "analysis",
        model=model,
        provider_kind="LIVE",
    )
    challenge = await challenge_analysis(
        settings, ChallengeRequest(analysis_snapshot_id=analysis.id), tmp_path / "challenge"
    )
    report = await generate_report(
        settings, ReportRequest(challenge_snapshot_id=challenge.id), tmp_path / "report"
    )
    assert len(calls) == 1
    assert len(analysis.claims) == 1
    assert analysis.claims[0].epistemic_class == "INTERPRETATION"
    assert report.analysis_snapshot_sha256 == digest(analysis)
    assert report.challenge_snapshot_sha256 == digest(challenge)
    assert report.analysis_snapshot_id == analysis.id
    database = SQLiteDatabase(settings.database_path)
    try:
        persisted = await database.analyses.get_analysis(analysis.id)
        assert persisted == analysis
        assert persisted.model_metadata.raw_response.structured_payload["_provider_metadata"] == {
            "response_id": "completion-123"
        }
    finally:
        await database.close()
    assert SECRET not in analysis.model_dump_json()
