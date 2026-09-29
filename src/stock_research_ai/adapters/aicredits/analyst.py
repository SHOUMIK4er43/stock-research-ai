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
from stock_research_ai.application.analyst_model_input import MAX_INPUT_BYTES, serialize_model_input
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
from stock_research_ai.logging_setup import get_logger

_URL = "https://api.aicredits.in/v1/chat/completions"
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (1.0, 4.0)
_MAX_LOGGED_KEY_NAMES = 32
_MAX_LOGGED_KEY_NAME_LENGTH = 200


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


def _kind(value: object) -> str:
    if value is None:
        return "null"
    return {
        dict: "object",
        list: "array",
        str: "string",
        bool: "boolean",
        int: "number",
        float: "number",
    }.get(type(value), "unknown")


def _bounded_key_names(keys: list[str]) -> list[str]:
    return [key[:_MAX_LOGGED_KEY_NAME_LENGTH] for key in keys[:_MAX_LOGGED_KEY_NAMES]]


def _response_diagnostics(
    payload: dict[str, Any], request_id: str | None, api_key: str
) -> dict[str, object]:
    """Bounded shape metadata only, never content, arguments or arbitrary prose.

    Emitted before semantic validation so exceptions cannot discard the evidence.
    Strings must be short identifier tokens; rejected values become null and their
    field names are recorded, distinguishing redaction from a provider null.
    """
    redacted: list[str] = []

    def identifier(field: str, value: object) -> str | None:
        if value is None:
            return None
        if (
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value)
            and api_key not in value
            and not re.search(
                r"(?i)(authorization|bearer|api[_-]?key|pairing[_-]?token|^sk-)", value
            )
        ):
            return value
        redacted.append(field)
        return None

    kind = _kind

    choices = payload.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    choice = first if isinstance(first, dict) else {}
    message = choice.get("message")
    msg = message if isinstance(message, dict) else {}
    calls = msg.get("tool_calls")
    first_call = calls[0] if isinstance(calls, list) and calls else None
    call = first_call if isinstance(first_call, dict) else {}
    function = call.get("function")
    fn = function if isinstance(function, dict) else {}
    arguments = fn.get("arguments")
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    diagnostic: dict[str, object] = {
        "event": "aicredits_response_metadata",
        "schema_version": 1,
        "http_status": 200,
        "response_id": identifier("response_id", payload.get("id")),
        "request_id": identifier("request_id", request_id),
        "reported_model": identifier("reported_model", payload.get("model")),
        "choices_count": len(choices) if isinstance(choices, list) else None,
        "choice_type": kind(first),
        "message_is_object": isinstance(message, dict),
        "message_gate_passed": isinstance(message, dict),
        "finish_reason": identifier("finish_reason", choice.get("finish_reason")),
        "finish_reason_gate_passed": choice.get("finish_reason") in (None, "tool_calls"),
        "content_present": "content" in msg,
        "content_type": kind(msg.get("content")),
        "tool_calls_present": "tool_calls" in msg,
        "tool_calls_type": kind(calls),
        "tool_calls_count": len(calls) if isinstance(calls, list) else None,
        "first_tool_call_type": identifier("first_tool_call_type", call.get("type")),
        "first_function_name": identifier("first_function_name", fn.get("name")),
        "arguments_present": "arguments" in fn,
        "arguments_type": kind(arguments),
        "arguments_length": len(arguments) if isinstance(arguments, str) else None,
        "usage": {
            key: usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if type(usage.get(key)) in (int, float) and 0 <= usage[key] <= 2**53 - 1
        },
        "redacted_fields": redacted,
    }
    return diagnostic


def _transport_diagnostics(
    exc: BaseException, attempt: int, max_attempts: int, *, timeout: bool
) -> dict[str, object]:
    """Sanitized shape-only diagnostics for a request that never received a response.

    Deliberately excludes `str(exc)`: httpx transport-error messages are not a bounded,
    audited shape the way the response-metadata diagnostic's fields are, so only class
    names and (when present) a plain OSError.errno integer are captured.
    """
    cause = exc.__cause__
    root = cause
    while root is not None and root.__cause__ is not None:
        root = root.__cause__
    os_errno = None
    for candidate in (exc, cause, root):
        if isinstance(candidate, OSError) and isinstance(candidate.errno, int):
            os_errno = candidate.errno
            break
    return {
        "event": "aicredits_transport_error",
        "schema_version": 1,
        "attempt_number": attempt + 1,
        "max_attempts": max_attempts,
        "exception_type": type(exc).__name__,
        "cause_type": type(cause).__name__ if cause is not None else None,
        "root_cause_type": type(root).__name__ if root is not None else None,
        "os_errno": os_errno,
        "timeout": timeout,
        "timestamp": datetime.now(UTC).isoformat(),
    }


_RETRYABLE_ERROR_CODES = frozenset({"RATE_LIMITED", "PROVIDER_UNAVAILABLE"})


def _http_error_diagnostics(
    status: int,
    mapped_error_code: str,
    attempt: int,
    max_attempts: int,
    response_headers: "httpx.Headers",
) -> dict[str, object]:
    """Sanitized shape-only diagnostics for a non-200 AI Credits HTTP response.

    Never logs the response body, arbitrary provider error prose, or any header
    beyond the two safe, bounded metadata values named below.
    """
    return {
        "event": "aicredits_http_error",
        "schema_version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "http_status": status,
        "mapped_error_code": mapped_error_code,
        "attempt_number": attempt + 1,
        "max_attempts": max_attempts,
        "retryable": mapped_error_code in _RETRYABLE_ERROR_CODES,
        "response_content_type": response_headers.get("content-type"),
        "response_content_length": response_headers.get("content-length"),
    }


def _request_diagnostics(
    body: dict[str, object],
    rendered: str,
    system_message: str,
    user_message: str,
    *,
    model: str,
    max_tokens: int,
    evidence_item_count: int,
) -> dict[str, object]:
    """Sanitized size/count-only metadata about an outgoing request, emitted before
    transport. Never logs system/user message text, evidence content, headers, tokens
    or the credential - sizes and counts only.
    """
    tools = body.get("tools")
    tool_count = len(tools) if isinstance(tools, list) else None
    tool_schema_bytes = (
        len(_json(tools[0]).encode("utf-8")) if isinstance(tools, list) and tools else None
    )
    messages = body.get("messages")
    message_count = len(messages) if isinstance(messages, list) else None
    return {
        "event": "aicredits_request_metadata",
        "schema_version": 1,
        "serialized_request_bytes": len(rendered.encode("utf-8")),
        "system_message_characters": len(system_message),
        "user_message_characters": len(user_message),
        "tool_schema_bytes": tool_schema_bytes,
        "message_count": message_count,
        "tool_count": tool_count,
        "model": model,
        "max_tokens": max_tokens,
        "evidence_item_count": evidence_item_count,
        "selected_evidence_item_count": evidence_item_count,
        "input_budget_bytes": MAX_INPUT_BYTES,
        "budget_bytes_used": len(user_message.encode("utf-8")),
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _argument_structure_diagnostics(
    parsed: object,
    arguments: object,
    *,
    strict_claims_shape_passed: bool,
    claims_string_normalized: bool,
    claims_nested_decode_attempted: bool,
    claims_nested_decode_succeeded: bool,
    claims_nested_decoded_type: str | None,
    claims_nested_decode_error_type: str | None,
    claims_nested_list_length: int | None,
) -> dict[str, object]:
    """Sanitized shape-only metadata for decoded tool-call arguments, emitted after
    JSON-decoding (and, if applicable, the one-time stringified-claims compatibility
    normalization below) but before the strict shape check. Never logs argument
    values, claim text, evidence, or any nested/arbitrary string content - key names
    only, defensively bounded, and only ever for the one fixed, known-safe function
    name already validated by the caller. `parsed` here reflects the
    post-normalization value, so these fields describe exactly what the strict gate
    below actually evaluates. The `claims_nested_*` fields describe only the single
    one-time nested decode attempt itself (type/status), never its decoded content.
    """
    top_level_keys: list[str] | None = None
    top_level_key_count: int | None = None
    claims_present = False
    claims_value_type: str | None = None
    claims_item_count: int | None = None
    wrapper_key_names: list[str] = []
    wrapper_candidate_count = 0
    if isinstance(parsed, dict):
        keys = [key for key in parsed if isinstance(key, str)]
        top_level_keys = _bounded_key_names(keys)
        top_level_key_count = len(parsed)
        claims_present = "claims" in parsed
        if claims_present:
            claims_value = parsed["claims"]
            claims_value_type = _kind(claims_value)
            if isinstance(claims_value, list):
                claims_item_count = len(claims_value)
        if len(parsed) == 1 and not claims_present:
            wrapper_candidate_count = 1
            wrapper_key_names = top_level_keys
    parsed_is_list = isinstance(parsed, list)
    return {
        "event": "aicredits_argument_structure",
        "schema_version": 1,
        "parsed_top_level_type": _kind(parsed),
        "top_level_key_names": top_level_keys,
        "top_level_key_count": top_level_key_count,
        "claims_key_present": claims_present,
        "claims_value_type": claims_value_type,
        "claims_item_count": claims_item_count,
        "parsed_is_list": parsed_is_list,
        "parsed_list_length": len(parsed) if isinstance(parsed, list) else None,
        "wrapper_candidate_count": wrapper_candidate_count,
        "wrapper_key_names": wrapper_key_names,
        "function_name": _TOOL_NAME,
        "arguments_character_length": len(arguments) if isinstance(arguments, str) else None,
        "strict_claims_shape_passed": strict_claims_shape_passed,
        "claims_string_normalized": claims_string_normalized,
        "claims_nested_decode_attempted": claims_nested_decode_attempted,
        "claims_nested_decode_succeeded": claims_nested_decode_succeeded,
        "claims_nested_decoded_type": claims_nested_decoded_type,
        "claims_nested_decode_error_type": claims_nested_decode_error_type,
        "claims_nested_list_length": claims_nested_list_length,
        "timestamp": datetime.now(UTC).isoformat(),
    }


class AICreditsAnalystModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 4096,
        max_attempts: int = _MAX_ATTEMPTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip() or not model.strip() or model != model.strip():
            raise AnalystProviderError("PROVIDER_CONFIGURATION", "Key and exact model ID required")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._max_attempts = max_attempts
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
        get_logger("aicredits").info(
            "%s",
            _json(
                _request_diagnostics(
                    body,
                    rendered,
                    _TRANSPORT_INSTRUCTIONS,
                    bundle_text,
                    model=self._model,
                    max_tokens=tokens,
                    evidence_item_count=len(model_input.items),
                )
            ),
        )
        timeout = httpx.Timeout(connect=10.0, read=self._timeout_seconds, write=10.0, pool=10.0)
        async with httpx.AsyncClient(
            transport=self._transport, timeout=timeout, trust_env=False, follow_redirects=False
        ) as client:
            payload, request_id = await self._post_with_retries(client, rendered)
        get_logger("aicredits").info(
            "%s", _json(_response_diagnostics(payload, request_id, self._api_key))
        )
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
        for attempt in range(self._max_attempts):
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
                    response_headers = response.headers
            except httpx.TimeoutException as exc:
                get_logger("aicredits").info(
                    "%s",
                    _json(_transport_diagnostics(exc, attempt, self._max_attempts, timeout=True)),
                )
                error = AnalystProviderError("TIMEOUT", "AI Credits request timed out")
            except httpx.TransportError as exc:
                get_logger("aicredits").info(
                    "%s",
                    _json(_transport_diagnostics(exc, attempt, self._max_attempts, timeout=False)),
                )
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
                get_logger("aicredits").info(
                    "%s",
                    _json(
                        _http_error_diagnostics(
                            status, code, attempt, self._max_attempts, response_headers
                        )
                    ),
                )
                error = AnalystProviderError(code, f"AI Credits HTTP {status}")
                if code not in _RETRYABLE_ERROR_CODES:
                    raise error
            if attempt == self._max_attempts - 1:
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
        # Narrow AI Credits/OpenAI-compatible transport compatibility case: some
        # responses wrap the (otherwise-valid) claims array in one extra layer of
        # JSON-string encoding - {"claims": "[...]"} instead of {"claims": [...]}.
        # This never happens on the Anthropic-native adapter, which never decodes
        # tool_use.input as a string at all. Exactly one extra decode attempt is
        # made, only for this exact shape, and only accepted if the nested value is
        # a list; any other outcome (invalid JSON, non-list result, or the shape not
        # matching at all) leaves `parsed` untouched and falls through to the
        # unchanged strict gate below - never a leaked lower-level exception, never
        # a second attempt, never a coercion, regex repair, or wrapper guess.
        claims_string_normalized = False
        claims_nested_decode_attempted = False
        claims_nested_decode_succeeded = False
        claims_nested_decoded_type: str | None = None
        claims_nested_decode_error_type: str | None = None
        claims_nested_list_length: int | None = None
        if (
            isinstance(parsed, dict)
            and set(parsed) == {"claims"}
            and isinstance(parsed["claims"], str)
        ):
            claims_nested_decode_attempted = True
            try:
                nested_claims = _load(parsed["claims"])
            except (ValueError, RecursionError) as exc:
                nested_claims = None
                claims_nested_decode_error_type = type(exc).__name__
            else:
                claims_nested_decode_succeeded = True
                claims_nested_decoded_type = _kind(nested_claims)
                if isinstance(nested_claims, list):
                    claims_nested_list_length = len(nested_claims)
            if isinstance(nested_claims, list):
                parsed = {"claims": nested_claims}
                claims_string_normalized = True
        strict_claims_shape_passed = (
            isinstance(parsed, dict)
            and set(parsed) == {"claims"}
            and isinstance(parsed["claims"], list)
        )
        get_logger("aicredits").info(
            "%s",
            _json(
                _argument_structure_diagnostics(
                    parsed,
                    arguments,
                    strict_claims_shape_passed=strict_claims_shape_passed,
                    claims_string_normalized=claims_string_normalized,
                    claims_nested_decode_attempted=claims_nested_decode_attempted,
                    claims_nested_decode_succeeded=claims_nested_decode_succeeded,
                    claims_nested_decoded_type=claims_nested_decoded_type,
                    claims_nested_decode_error_type=claims_nested_decode_error_type,
                    claims_nested_list_length=claims_nested_list_length,
                )
            ),
        )
        # Unchanged from before diagnostics were added: exact original gate, inline so
        # mypy's isinstance-narrowing on `parsed` still applies to the `return` below.
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"claims"}
            or not isinstance(parsed["claims"], list)
        ):
            raise AnalystProviderError("MALFORMED_ARGUMENTS", "Expected claims array object")
        # Individual candidates remain untrusted; unchanged Part 5B validators own them.
        return parsed
