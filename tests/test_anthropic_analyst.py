"""Offline tests for the Anthropic-backed production AnalystModel adapter. All network
is mocked via httpx.MockTransport - no real Anthropic call is ever made in normal pytest
(see scripts/live_smoke_full_research.py for the separate, opt-in live smoke path).
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from stock_research_ai.adapters.anthropic import analyst as analyst_module
from stock_research_ai.adapters.anthropic.analyst import AnalystProviderError, AnthropicAnalystModel
from stock_research_ai.application.hashing import digest
from stock_research_ai.config import Settings
from stock_research_ai.domain.analysis import (
    AnalysisRequest,
    AnalystExecutionSpec,
    AnstInputBundle,
    EvidenceReference,
    SectionCoverage,
)
from stock_research_ai.domain.analyst_model_input import AnalystModelEvidence, AnalystModelInput

FAKE_HASH = "a" * 64


def _bundle_and_ref():
    ref = EvidenceReference(
        kind="VALUATION",
        owning_snapshot_id=uuid4(),
        item_key="k1",
        snapshot_payload_sha256=FAKE_HASH,
    )
    request = AnalysisRequest(
        company_id=uuid4(),
        classification=dict(
            classification="NON_FINANCIAL", basis="CALLER_DECLARED", declaration="x"
        ),
        profile_id="IT_SERVICES_PROFILE",
        sections=("VALUATION",),
    )
    coverage = (
        SectionCoverage(
            section="VALUATION",
            status="AVAILABLE",
            reason="ok",
            contributing_evidence_kinds=("VALUATION",),
        ),
    )
    bundle = AnstInputBundle(
        bundle_id=uuid4(),
        request=request,
        assembled_at=datetime.now(UTC),
        sources=(ref,),
        section_coverage=coverage,
    )
    return bundle, ref


def _projection(bundle):
    # Transport-only fixture, not a claim-grounding proof. R1 tests resolve real owners.
    return AnalystModelInput(
        bundle_id=bundle.bundle_id, bundle_sha256=digest(bundle),
        items=tuple(AnalystModelEvidence(reference=ref, status="INSUFFICIENT_INPUT",
                    source_epistemic_class="CALCULATION", content_json='{"values":[]}')
                    for ref in bundle.sources),
    )


def _execution() -> AnalystExecutionSpec:
    return AnalystExecutionSpec(template_id="full-research-v1", template_version=1)


def _tool_use_response(model="claude-sonnet-5-live", tool_input=None, status=200):
    payload = {"claims": []} if tool_input is None else tool_input
    return httpx.Response(
        status,
        json={
            "model": model,
            "content": [{"type": "tool_use", "name": "emit_claims", "input": payload}],
        },
    )


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(analyst_module.asyncio, "sleep", instant_sleep)


async def test_valid_structured_response_round_trips_into_raw_response():
    bundle, ref = _bundle_and_ref()
    claim = {
        "section": "VALUATION",
        "statement": "Observed valuation evidence is cited.",
        "materiality": "PRIMARY",
        "citations": [
            {
                "kind": ref.kind,
                "owning_snapshot_id": str(ref.owning_snapshot_id),
                "item_key": ref.item_key,
            }
        ],
    }
    transport = httpx.MockTransport(
        lambda request: _tool_use_response(tool_input={"claims": [claim]})
    )
    model = AnthropicAnalystModel(api_key="sk-test", model="claude-sonnet-5", transport=transport)

    metadata = await model.generate(bundle, _execution(), model_input=_projection(bundle))

    assert metadata.provider == "anthropic"
    assert metadata.model_id == "claude-sonnet-5"
    assert metadata.model_version == "claude-sonnet-5-live"  # from the response, not config
    assert metadata.raw_response.structured_payload == {"claims": [claim]}
    assert metadata.request.bundle_sha256
    assert metadata.request.template.template_id == "full-research-v1"


async def test_api_key_never_appears_in_any_persisted_representation():
    bundle, _ref = _bundle_and_ref()
    transport = httpx.MockTransport(lambda request: _tool_use_response())
    secret = "sk-ant-super-secret-value"
    model = AnthropicAnalystModel(api_key=secret, model="claude-sonnet-5", transport=transport)

    metadata = await model.generate(bundle, _execution(), model_input=_projection(bundle))

    assert secret not in metadata.model_dump_json()
    assert secret not in repr(metadata)


async def test_malformed_json_body_raises_malformed_response():
    def handler(request):
        return httpx.Response(
            200, content=b"not json", headers={"content-type": "application/json"}
        )

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "MALFORMED_RESPONSE"


async def test_missing_content_field_raises_malformed_response():
    def handler(request):
        return httpx.Response(200, json={"model": "m"})  # no "content" at all

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "MALFORMED_RESPONSE"


async def test_no_matching_tool_use_block_raises_unsupported_schema():
    def handler(request):
        return httpx.Response(
            200, json={"model": "m", "content": [{"type": "text", "text": "no tool call"}]}
        )

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "UNSUPPORTED_RESPONSE_SCHEMA"


async def test_tool_input_not_an_object_raises_unsupported_schema():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "m",
                "content": [{"type": "tool_use", "name": "emit_claims", "input": "not-an-object"}],
            },
        )

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "UNSUPPORTED_RESPONSE_SCHEMA"


async def test_extra_and_missing_fields_pass_through_untouched():
    """The adapter never filters/validates - it hands the raw tool input through exactly;
    Part 5B's own frozen validate_response is the sole downstream authority."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "m",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "emit_claims",
                        "input": {"claims": [{"unexpected_field": "x"}]},
                    }
                ],
            },
        )

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    metadata = await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert metadata.raw_response.structured_payload == {"claims": [{"unexpected_field": "x"}]}


async def test_timeout_retries_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return _tool_use_response()

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    metadata = await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert len(attempts) == 2
    assert metadata.raw_response.structured_payload == {"claims": []}


async def test_timeout_exhausted_raises_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "TIMEOUT"


@pytest.mark.parametrize(
    "status,code", [(401, "AUTHENTICATION_FAILED"), (403, "PERMISSION_DENIED")]
)
async def test_auth_and_permission_failures_never_retry(status, code):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(status, json={"error": "no"})

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == code
    assert len(attempts) == 1  # no retry for a non-transient failure


@pytest.mark.parametrize("status", [400, 404])
async def test_unsupported_model_or_bad_request_raises_model_unavailable(status):
    def handler(request):
        return httpx.Response(status, json={"error": "unsupported model"})

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "MODEL_UNAVAILABLE"


async def test_rate_limit_retries_then_raises_after_bounded_attempts():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429, json={"error": "rate limited"})

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "RATE_LIMITED"
    assert len(attempts) == 3  # bounded: no unbounded retry loop


async def test_provider_5xx_retries_then_raises_after_bounded_attempts():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503, json={"error": "unavailable"})

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "PROVIDER_UNAVAILABLE"
    assert len(attempts) == 3


async def test_response_too_large_is_rejected():
    def handler(request):
        huge = {"claims": [{"filler": "x" * 3_000_000}]}
        return httpx.Response(
            200,
            json={
                "model": "m",
                "content": [{"type": "tool_use", "name": "emit_claims", "input": huge}],
            },
        )

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(AnalystProviderError) as exc:
        await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert exc.value.code == "RESPONSE_TOO_LARGE"


async def test_only_one_model_call_per_generate_invocation_on_success():
    attempts = []

    def handler(request):
        attempts.append(1)
        return _tool_use_response()

    bundle, _ = _bundle_and_ref()
    model = AnthropicAnalystModel(api_key="k", model="m", transport=httpx.MockTransport(handler))
    await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert len(attempts) == 1  # no agent loop, no follow-up calls


def test_settings_analyst_api_key_is_secret_str_and_never_leaks_via_repr_or_dump():
    """config.Settings.analyst_api_key must be SecretStr, not a plain str: an ordinary
    str field's value appears verbatim in repr()/str()/model_dump(), which would leak the
    credential through logging or a debug print of the whole Settings object."""
    secret = "sk-ant-super-secret-value-should-never-appear"
    settings = Settings(analyst_provider="anthropic", analyst_api_key=secret)

    assert secret not in repr(settings)
    assert secret not in str(settings)
    assert secret not in json.dumps(settings.model_dump(mode="json"))
    assert settings.analyst_api_key is not None
    assert settings.analyst_api_key.get_secret_value() == secret
