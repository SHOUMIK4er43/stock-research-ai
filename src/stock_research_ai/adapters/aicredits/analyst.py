"""AI Credits Chat Completions adapter. No evidence selection or claim authority.

One forced function call, exact frozen R1 content, bounded transport retries only.
The exact header-free request bytes are persisted; provider IDs live in reserved
adapter-owned raw-response metadata so frozen domain schemas stay unchanged.
"""

import asyncio
import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from stock_research_ai.adapters.analyst_contract import (
    _CLAIM_SCHEMA,
    _TOOL_NAME,
    _TRANSPORT_INSTRUCTIONS,
    AnalystProviderError,
)
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

_URL = "https://api.aicredits.in/v1/chat/completions"
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 4.0)


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("Non-finite JSON constant")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def _load(text: str | bytes) -> Any:
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )


class AICreditsAnalystModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip() or not model.strip() or model != model.strip():
            raise AnalystProviderError("PROVIDER_CONFIGURATION", "Key and exact model ID required")
        self._api_key = api_key
        self._model = model
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
        # Shared R1 binding/size checks must run before any transport opens.
        bundle_text = serialize_model_input(bundle, model_input)
        parameters = dict(execution.generation_parameters)
        if set(parameters) - {"max_output_tokens", "temperature", "top_p"}:
            raise AnalystProviderError("INVALID_REQUEST", "Unsupported generation parameter")
        parameters.setdefault("max_output_tokens", self._max_output_tokens)
        tokens = parameters["max_output_tokens"]
        if type(tokens) is not int or tokens < 1:
            raise AnalystProviderError(
                "INVALID_REQUEST", "max_output_tokens must be positive integer"
            )
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": tokens,
            "messages": [
                {"role": "system", "content": _TRANSPORT_INSTRUCTIONS},
                {"role": "user", "content": bundle_text},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": _TOOL_NAME,
                        "description": (
                            "Emit the structured analyst claims for this evidence bundle."
                        ),
                        "parameters": _CLAIM_SCHEMA,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": _TOOL_NAME}},
            "parallel_tool_calls": False,
        }
        for name, maximum in (("temperature", 2.0), ("top_p", 1.0)):
            if name in parameters:
                value = parameters[name]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 0 <= value <= maximum
                ):
                    raise AnalystProviderError("INVALID_REQUEST", "Invalid sampling parameter")
                body[name] = value
        rendered = _json(body)
        if self._api_key in rendered:
            raise AnalystProviderError("UNSAFE_REQUEST", "Credential detected in request content")
        request = RenderedModelRequest(
            bundle_sha256=digest(bundle),
            template=PromptTemplate(
                template_id=execution.template_id,
                template_version=execution.template_version,
                content=_TRANSPORT_INSTRUCTIONS,
                content_sha256=_sha(_TRANSPORT_INSTRUCTIONS),
            ),
            generation_parameters=parameters,
            generation_parameters_sha256=_sha(_json(parameters)),
            rendered_text=rendered,
            rendered_sha256=_sha(rendered),
        )
        timeout = httpx.Timeout(connect=10.0, read=self._timeout_seconds, write=10.0, pool=10.0)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout, trust_env=False, follow_redirects=False
        ) as client:
            payload, request_id = await self._post_with_retries(client, rendered)
        reported_model = payload.get("model")
        # AI Credits may omit exactly this leading provider namespace in its echo.
        # Compare only; retain both original strings in request/response provenance.
        matches_model = reported_model == self._model or (
            self._model.startswith("anthropic/")
            and reported_model == self._model.removeprefix("anthropic/")
            and reported_model != ""
        )
        if reported_model is not None and not matches_model:
            raise AnalystProviderError(
                "MODEL_MISMATCH", "Response model differs from requested model"
            )
        claims = self._parse_response(payload)
        identifiers = {}
        for identifier_name, identifier in (
            ("response_id", payload.get("id")),
            ("request_id", request_id),
        ):
            if identifier is not None:
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or len(identifier) > 256
                    or not identifier.isprintable()
                ):
                    raise AnalystProviderError("MALFORMED_RESPONSE", "Invalid provider identifier")
                identifiers[identifier_name] = identifier
        raw: dict[str, object] = {"claims": claims["claims"], "_provider_metadata": identifiers}
        raw_text = _json(raw)
        if self._api_key in raw_text or re.search(r"(?i)authorization:\s*bearer\s+", raw_text):
            raise AnalystProviderError("UNSAFE_RESPONSE", "Unsafe provider response content")
        return ModelMetadata(
            provider="aicredits",
            model_id=self._model,
            model_version=reported_model,
            request=request,
            generated_at=datetime.now(UTC),
            raw_response=RawAnalystResponse(
                structured_payload=raw, structured_payload_sha256=_sha(raw_text)
            ),
        )

    async def _post_with_retries(
        self, client: httpx.AsyncClient, rendered: str
    ) -> tuple[dict[str, Any], str | None]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with client.stream(
                    "POST", _URL, content=rendered.encode(), headers=headers
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_RESPONSE_BYTES:
                            raise AnalystProviderError(
                                "RESPONSE_TOO_LARGE", "Response cap exceeded"
                            )
                    status = response.status_code
                    request_id = response.headers.get("x-request-id")
            except httpx.TimeoutException:
                error = AnalystProviderError("TIMEOUT", "AI Credits request timed out")
            except httpx.TransportError:
                error = AnalystProviderError("CONNECTION_FAILED", "AI Credits connection failed")
            else:
                if status == 200:
                    try:
                        payload = _load(bytes(content))
                    except (ValueError, RecursionError):
                        raise AnalystProviderError(
                            "MALFORMED_RESPONSE", "Invalid response JSON"
                        ) from None
                    if not isinstance(payload, dict):
                        raise AnalystProviderError(
                            "MALFORMED_RESPONSE", "Response must be an object"
                        )
                    return payload, request_id
                code = self._error_code(status, bytes(content))
                error = AnalystProviderError(code, f"AI Credits HTTP {status}")
                if code not in ("RATE_LIMITED", "PROVIDER_UNAVAILABLE"):
                    raise error
            if attempt == _MAX_ATTEMPTS - 1:
                raise error from None
            await asyncio.sleep(_BACKOFF_SECONDS[attempt])
        raise AssertionError("Unreachable retry state")

    @staticmethod
    def _error_code(status: int, content: bytes) -> str:
        # Exact machine codes only; never persist or echo arbitrary error-body prose.
        try:
            body = _load(content)
            error = body.get("error") if isinstance(body, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
        except (ValueError, RecursionError):
            code = None
        if status == 402 or code in (
            "insufficient_credits",
            "insufficient_quota",
            "payment_required",
        ):
            return "INSUFFICIENT_CREDITS"
        if status == 401:
            return "AUTHENTICATION_FAILED"
        if status == 403:
            return "PERMISSION_DENIED"
        if status == 404 or code in ("model_not_found", "model_unavailable"):
            return "MODEL_UNAVAILABLE"
        if status == 429:
            return "RATE_LIMITED"
        if 500 <= status <= 599:
            return "PROVIDER_UNAVAILABLE"
        return "INVALID_REQUEST"

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise AnalystProviderError("MALFORMED_RESPONSE", "Exactly one choice required")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict) or choice.get("finish_reason") not in (None, "tool_calls"):
            raise AnalystProviderError("MALFORMED_RESPONSE", "Incomplete tool response")
        calls = message.get("tool_calls")
        if calls is None or calls == []:
            raise AnalystProviderError("MISSING_TOOL_CALL", "Expected emit_claims function")
        if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
            raise AnalystProviderError("UNEXPECTED_TOOL_CALL", "Exactly one function required")
        call = calls[0]
        function = call.get("function")
        if (
            call.get("type") != "function"
            or not isinstance(function, dict)
            or function.get("name") != _TOOL_NAME
        ):
            raise AnalystProviderError("UNEXPECTED_TOOL_CALL", "Unexpected function")
        arguments = function.get("arguments")
        try:
            parsed = _load(arguments) if isinstance(arguments, str) else None
        except (ValueError, RecursionError):
            raise AnalystProviderError(
                "MALFORMED_ARGUMENTS", "Invalid function arguments"
            ) from None
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"claims"}
            or not isinstance(parsed["claims"], list)
        ):
            raise AnalystProviderError("MALFORMED_ARGUMENTS", "Expected claims array object")
        # Individual candidates remain untrusted; unchanged Part 5B validators own them.
        return parsed
