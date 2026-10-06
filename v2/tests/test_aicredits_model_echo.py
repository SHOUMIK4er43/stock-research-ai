"""P1.1: exact, one-leading-prefix model echo compatibility; offline transport only."""

import hashlib
import json

import httpx
import pytest
from test_aicredits_analyst import SECRET, response
from test_anthropic_analyst import _bundle_and_ref, _execution, _projection

from stock_research_ai.adapters.aicredits.analyst import AICreditsAnalystModel
from stock_research_ai.adapters.analyst_contract import AnalystProviderError
from stock_research_ai.adapters.anthropic.analyst import AnthropicAnalystModel
from stock_research_ai.domain.analysis import ModelMetadata


@pytest.mark.parametrize(
    "requested,reported,accepted",
    [
        ("anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5", True),
        ("claude-sonnet-5", "claude-sonnet-5", True),
        ("anthropic/claude-sonnet-5", "claude-sonnet-5", True),
        ("anthropic/another-exact-model", "another-exact-model", True),
        ("anthropic/claude-sonnet-5", "claude-sonnet-4.6", False),
        ("anthropic/claude-sonnet-5", "claude-sonnet-5-latest", False),
        ("anthropic/claude-sonnet-5", "CLAUDE-SONNET-5", False),
        ("anthropic/claude-sonnet-5", "provider/claude-sonnet-5", False),
        ("claude-sonnet-5", "claude-sonnet-5-extra", False),
        ("claude-sonnet-5", "anthropic/claude-sonnet-5", False),
        ("Anthropic/claude-sonnet-5", "claude-sonnet-5", False),
        ("other/anthropic/claude-sonnet-5", "claude-sonnet-5", False),
        ("anthropic/anthropic/claude-sonnet-5", "claude-sonnet-5", False),
        ("anthropic/claude-sonnet-5", " claude-sonnet-5", False),
        ("anthropic/claude-sonnet-5", "claude-sonnet-5 ", False),
        ("anthropic/claude-sonnet-5", "", False),
        ("anthropic/", "", False),
        ("anthropic/claude-sonnet-5", None, True),
        ("anthropic/claude-sonnet-5", 123, False),
    ],
)
async def test_exact_model_echo_rule_and_provenance(requested, reported, accepted, monkeypatch):
    calls = []

    async def forbidden_fallback(*args, **kwargs):
        pytest.fail("No Anthropic fallback may occur")

    monkeypatch.setattr(AnthropicAnalystModel, "generate", forbidden_fallback)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response(model=reported))

    bundle, _ = _bundle_and_ref()
    model = AICreditsAnalystModel(SECRET, requested, transport=httpx.MockTransport(handler))
    if accepted:
        result = await model.generate(bundle, _execution(), model_input=_projection(bundle))
        assert result.provider == "aicredits"
        assert result.model_id == requested
        assert result.model_version == reported
        assert ModelMetadata.model_validate_json(result.model_dump_json()) == result
        assert json.loads(result.request.rendered_text)["model"] == requested
        assert result.request.rendered_text.encode() == calls[0].content
        assert result.request.rendered_sha256 == hashlib.sha256(calls[0].content).hexdigest()
    else:
        with pytest.raises(AnalystProviderError) as exc:
            await model.generate(bundle, _execution(), model_input=_projection(bundle))
        assert exc.value.code == "MODEL_MISMATCH"
    assert len(calls) == 1  # no retry, model switch, catalog lookup, or alternate endpoint
    assert str(calls[0].url) == "https://api.aicredits.in/v1/chat/completions"
    assert json.loads(calls[0].content)["model"] == requested


async def test_missing_model_behavior_unchanged():
    payload = response()
    del payload["model"]
    bundle, _ = _bundle_and_ref()
    model = AICreditsAnalystModel(
        SECRET,
        "anthropic/claude-sonnet-5",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    result = await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert result.model_id == "anthropic/claude-sonnet-5"
    assert result.model_version is None


async def test_supplied_live_echo_fixture_preserves_unvalidated_candidate():
    # This is evidence of working transport, not a valid Part 5B analyst claim.
    arguments = '{"claims":[{"text":"AI Credits tool calling works"}]}'
    bundle, _ = _bundle_and_ref()
    model = AICreditsAnalystModel(
        SECRET,
        "anthropic/claude-sonnet-5",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=response(arguments=arguments, model="claude-sonnet-5")
            )
        ),
    )
    result = await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert result.model_id == "anthropic/claude-sonnet-5"
    assert result.model_version == "claude-sonnet-5"
    assert result.raw_response.structured_payload["claims"] == json.loads(arguments)["claims"]


async def test_direct_anthropic_reported_model_behavior_unchanged():
    bundle, _ = _bundle_and_ref()
    payload = {
        "model": "different-reported-version",
        "content": [{"type": "tool_use", "name": "emit_claims", "input": {"claims": []}}],
    }
    model = AnthropicAnalystModel(
        SECRET,
        "configured-model",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    result = await model.generate(bundle, _execution(), model_input=_projection(bundle))
    assert result.provider == "anthropic"
    assert result.model_id == "configured-model"
    assert result.model_version == "different-reported-version"
