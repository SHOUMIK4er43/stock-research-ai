"""AI Credits Chat Completions adapter for SUMMARY_V1: one plain-text completion.

Independent of the analyst adapter: no tools, no claims, no evidence, no retries.
Exactly one HTTP POST per call. Provider output is validated, never repaired or trimmed.
Every failure is a fixed SummaryGenerationError code; logs are shape-only metadata.
"""

import json
import math
from datetime import UTC, datetime
from typing import Any

import httpx

from stock_research_ai.adapters.secret_store import SecretStore, resolve_analyst_api_key
from stock_research_ai.application.summary_generation import model_matches
from stock_research_ai.config import Settings
from stock_research_ai.domain.summary import (
    SummaryErrorCode,
    SummaryGenerationError,
    SummaryModelOutput,
    SummaryUsage,
)
from stock_research_ai.logging_setup import get_logger

_URL = "https://api.aicredits.in/v1/chat/completions"
MAX_OUTPUT_TOKENS = 1500
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 262_144
DEFAULT_TIMEOUT_SECONDS = 90.0


def _json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


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


def _load(content: bytes) -> Any:
    return json.loads(
        content,
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
    )


def _fail(code: SummaryErrorCode) -> SummaryGenerationError:
    return SummaryGenerationError(code)


def _log(event: dict[str, object]) -> None:
    # Shape-only metadata: sizes, counts, status codes and exception class names.
    get_logger("aicredits.summary").info(
        "%s", _json({**event, "timestamp": datetime.now(UTC).isoformat()})
    )


def _usage(value: object) -> SummaryUsage | None:
    if not isinstance(value, dict):
        return None
    prompt, completion = value.get("prompt_tokens"), value.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int or prompt < 0 or completion < 0:
        return None
    return SummaryUsage(prompt_tokens=prompt, completion_tokens=completion)


class AICreditsSummaryModel:
    """SummaryModel over AI Credits. The key is used only in the Authorization header."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not model.strip() or model != model.strip() or len(model) > 120:
            raise _fail(SummaryErrorCode.PROVIDER_NOT_CONFIGURED)
        if not api_key.strip():
            raise _fail(SummaryErrorCode.CREDENTIAL_UNAVAILABLE)
        if not 0 < timeout_seconds <= 600 or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise _fail(SummaryErrorCode.PROVIDER_NOT_CONFIGURED)
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._transport = transport

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        secret_store: SecretStore | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> "AICreditsSummaryModel":
        """Existing credential precedence (environment, then Keychain) for AI Credits."""
        if settings.analyst_provider != "aicredits" or not settings.aicredits_model:
            raise _fail(SummaryErrorCode.PROVIDER_NOT_CONFIGURED)
        api_key, _source = resolve_analyst_api_key(settings, secret_store)
        if not api_key:
            raise _fail(SummaryErrorCode.CREDENTIAL_UNAVAILABLE)
        return cls(api_key=api_key, model=settings.aicredits_model, transport=transport)

    async def complete(self, *, system: str, user: str) -> SummaryModelOutput:
        body = {
            "model": self._model,
            "max_tokens": self._max_output_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        rendered = _json(body).encode("utf-8")
        if len(rendered) > MAX_REQUEST_BYTES:
            raise _fail(SummaryErrorCode.SUMMARY_REQUEST_TOO_LARGE)
        if self._api_key.encode("utf-8") in rendered:
            raise _fail(SummaryErrorCode.UNSAFE_REQUEST)
        _log(
            {
                "event": "aicredits_summary_request",
                "request_bytes": len(rendered),
                "model": self._model,
                "max_tokens": self._max_output_tokens,
            }
        )
        status, content = await self._post_once(rendered)
        if status != 200:
            _log({"event": "aicredits_summary_http_error", "http_status": status})
            if status == 401:
                raise _fail(SummaryErrorCode.CREDENTIAL_UNAVAILABLE)
            raise _fail(SummaryErrorCode.PROVIDER_HTTP_ERROR)
        return self._parse(content)

    async def _post_once(self, rendered: bytes) -> tuple[int, bytes]:
        """Exactly one POST. Never retried: a repeat could duplicate billed work."""
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(connect=10.0, read=self._timeout_seconds, write=10.0, pool=10.0)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream("POST", _URL, content=rendered, headers=headers) as resp:
                    content = bytearray()
                    async for chunk in resp.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise _fail(SummaryErrorCode.INVALID_PROVIDER_RESPONSE)
                    return resp.status_code, bytes(content)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never left this process.
            self._log_transport(exc)
            raise _fail(SummaryErrorCode.PROVIDER_UNAVAILABLE) from None
        except httpx.TimeoutException as exc:
            self._log_transport(exc)
            raise _fail(SummaryErrorCode.PROVIDER_TIMEOUT) from None
        except httpx.TransportError as exc:
            self._log_transport(exc)
            raise _fail(SummaryErrorCode.PROVIDER_CONNECTION_LOST) from None

    @staticmethod
    def _log_transport(exc: BaseException) -> None:
        # Exception class names only; httpx messages are not an audited shape.
        _log({"event": "aicredits_summary_transport_error", "exception_type": type(exc).__name__})

    def _parse(self, content: bytes) -> SummaryModelOutput:
        invalid = SummaryErrorCode.INVALID_PROVIDER_RESPONSE
        try:
            payload = _load(content)
        except (ValueError, RecursionError):
            raise _fail(invalid) from None
        if not isinstance(payload, dict):
            raise _fail(invalid)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _fail(invalid)
        choice = choices[0]
        message = choice.get("message")
        finish_reason = choice.get("finish_reason")
        text = message.get("content") if isinstance(message, dict) else None
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        reported = payload.get("model")
        usage = _usage(payload.get("usage"))
        _log(
            {
                "event": "aicredits_summary_response",
                "http_status": 200,
                "choices_count": len(choices),
                "finish_reason": finish_reason if finish_reason in ("stop", "length") else None,
                "content_type": type(text).__name__,
                "content_length": len(text) if isinstance(text, str) else None,
                "tool_calls_present": tool_calls not in (None, []),
                "reported_model_matches": isinstance(reported, str)
                and model_matches(self._model, reported),
                "usage": usage.model_dump() if usage else None,
            }
        )
        if (
            not isinstance(message, dict)
            or tool_calls not in (None, [])
            or finish_reason != "stop"
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise _fail(invalid)
        if reported is not None and not isinstance(reported, str):
            raise _fail(invalid)
        if not model_matches(self._model, reported):
            raise _fail(SummaryErrorCode.MODEL_MISMATCH)
        if self._api_key in text:
            raise _fail(SummaryErrorCode.UNSAFE_CONTENT)
        return SummaryModelOutput(
            summary_text=text,
            requested_model=self._model,
            reported_model=reported,
            finish_reason="stop",
            usage=usage,
        )
