"""Anthropic-backed implementation of the provider-neutral AnalystModel protocol.

Provider adapter responsibilities are limited to: an authenticated HTTPS request,
forced-tool-use structured-output transport, provider response parsing, provider
metadata extraction, and typed provider failures. It does NOT own research prompt
semantics - it renders exactly one request per `generate()` call (no agent loop) built
from the frozen Part 5B bundle and its application-owned content projection, using the existing
PromptTemplate/RenderedModelRequest/RawAnalystResponse/ModelMetadata contract every
other AnalystModel (including tests/fakes/analyst.py) already uses. All model output
remains untrusted until the existing, frozen application.analysis_validation.validate_
response accepts it - this adapter has zero authority over what becomes an AnalystClaim.
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from stock_research_ai.adapters.analyst_contract import (
    _CLAIM_SCHEMA,
    _TOOL_NAME,
    _TRANSPORT_INSTRUCTIONS,
)
from stock_research_ai.adapters.analyst_contract import AnalystProviderError as AnalystProviderError
from stock_research_ai.application.analyst_model_input import serialize_model_input
from stock_research_ai.application.hashing import digest
from stock_research_ai.domain.analysis import (
    AnalystExecutionSpec,
    AnstInputBundle,
    ModelMetadata,
    PromptTemplate,
    RawAnalystResponse,
    RenderedModelRequest,
)
from stock_research_ai.domain.analyst_model_input import AnalystModelInput

_API_VERSION = "2023-06-01"
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 4.0)


def _hash_json(value: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class AnthropicAnalystModel:
    """Implements AnalystModel.generate exactly. Constructed only from explicit
    configuration (never Claude Code/Claude Pro session credentials, which are not an
    Anthropic API credential); never persists or logs its own API key."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._transport = transport

    async def generate(
        self,
        bundle: AnstInputBundle,
        execution: AnalystExecutionSpec,
        *,
        model_input: AnalystModelInput,
    ) -> ModelMetadata:
        template_content = _TRANSPORT_INSTRUCTIONS
        template = PromptTemplate(
            template_id=execution.template_id,
            template_version=execution.template_version,
            content=template_content,
            content_sha256=hashlib.sha256(template_content.encode()).hexdigest(),
        )
        bundle_text = serialize_model_input(bundle, model_input)
        rendered_text = f"{template_content}\n\nEvidence bundle:\n{bundle_text}"
        generation_parameters = dict(execution.generation_parameters)
        request = RenderedModelRequest(
            bundle_sha256=digest(bundle),
            template=template,
            generation_parameters=generation_parameters,
            generation_parameters_sha256=_hash_json(generation_parameters),
            rendered_text=rendered_text,
            rendered_sha256=hashlib.sha256(rendered_text.encode()).hexdigest(),
        )

        max_tokens = int(generation_parameters.get("max_output_tokens", self._max_output_tokens))
        body = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": template_content,
            "messages": [{"role": "user", "content": bundle_text}],
            "tools": [
                {
                    "name": _TOOL_NAME,
                    "description": "Emit the structured analyst claims for this evidence bundle.",
                    "input_schema": _CLAIM_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        }

        timeout = httpx.Timeout(connect=10.0, read=self._timeout_seconds, write=10.0, pool=10.0)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout, trust_env=False
        ) as client:
            response_payload = await self._post_with_retries(client, body)

        claims_payload = self._parse_response(response_payload)
        raw_response = RawAnalystResponse(
            structured_payload=claims_payload,
            structured_payload_sha256=_hash_json(claims_payload),
        )
        return ModelMetadata(
            provider="anthropic",
            model_id=self._model,
            model_version=str(response_payload.get("model", self._model)),
            request=request,
            raw_response=raw_response,
            generated_at=datetime.now(UTC),
        )

    async def _post_with_retries(
        self, client: httpx.AsyncClient, body: dict[str, object]
    ) -> dict[str, Any]:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        last_error: AnalystProviderError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = await client.post(
                    f"{self._base_url}/v1/messages", json=body, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_error = AnalystProviderError("TIMEOUT", str(exc))
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise last_error from exc

            if response.status_code == 200:
                if len(response.content) > _MAX_RESPONSE_BYTES:
                    raise AnalystProviderError(
                        "RESPONSE_TOO_LARGE",
                        f"Response body of {len(response.content)} bytes exceeds the "
                        f"{_MAX_RESPONSE_BYTES}-byte V1 cap",
                    )
                try:
                    parsed: dict[str, Any] = response.json()
                except ValueError as exc:
                    raise AnalystProviderError(
                        "MALFORMED_RESPONSE", f"Non-JSON response body: {exc}"
                    ) from exc
                return parsed
            if response.status_code == 401:
                raise AnalystProviderError(
                    "AUTHENTICATION_FAILED", "Anthropic rejected the configured API key"
                )
            if response.status_code == 403:
                raise AnalystProviderError(
                    "PERMISSION_DENIED", "Anthropic denied permission for this request"
                )
            if response.status_code in (400, 404):
                raise AnalystProviderError(
                    "MODEL_UNAVAILABLE", f"HTTP {response.status_code}: {response.text[:500]}"
                )
            if response.status_code == 429 or response.status_code >= 500:
                code = "RATE_LIMITED" if response.status_code == 429 else "PROVIDER_UNAVAILABLE"
                last_error = AnalystProviderError(code, f"HTTP {response.status_code}")
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise last_error
            raise AnalystProviderError(
                "PROVIDER_UNAVAILABLE", f"Unexpected HTTP {response.status_code}"
            )
        assert last_error is not None
        raise last_error

    def _parse_response(self, payload: dict[str, Any]) -> dict[str, object]:
        content = payload.get("content")
        if not isinstance(content, list):
            raise AnalystProviderError("MALFORMED_RESPONSE", "Missing content list in response")
        tool_use = next(
            (
                block
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == _TOOL_NAME
            ),
            None,
        )
        if tool_use is None:
            raise AnalystProviderError(
                "UNSUPPORTED_RESPONSE_SCHEMA", f"No {_TOOL_NAME} tool_use block in response"
            )
        tool_input = tool_use.get("input")
        if not isinstance(tool_input, dict):
            raise AnalystProviderError(
                "UNSUPPORTED_RESPONSE_SCHEMA", "tool_use.input was not a JSON object"
            )
        return tool_input
