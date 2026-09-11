"""Deterministic offline stand-in for AnalystModel. No network, no provider SDK."""

import hashlib
import json
from datetime import UTC, datetime

from stock_research_ai.application.hashing import digest
from stock_research_ai.domain.analysis import (
    AnalystExecutionSpec,
    AnstInputBundle,
    ModelMetadata,
    PromptTemplate,
    RawAnalystResponse,
    RenderedModelRequest,
)


def hash_json(value: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class FakeAnalystModel:
    """Returns a pre-programmed structured payload, or raises to simulate a provider
    failure. `claims` is the literal `structured_payload["claims"]` list a test wants
    the validator to receive - callers construct it as plain dicts matching
    AnalystClaimCandidate's shape (or deliberately malformed, for negative tests)."""

    def __init__(
        self,
        claims: list[dict[str, object]] | None = None,
        raw_payload: dict[str, object] | None = None,
        fail: bool = False,
        model_id: str = "fake-analyst-v1",
        model_version: str | None = "1",
    ) -> None:
        self._claims = claims if claims is not None else []
        self._raw_payload = raw_payload
        self._fail = fail
        self._model_id = model_id
        self._model_version = model_version

    async def generate(
        self, bundle: AnstInputBundle, execution: AnalystExecutionSpec
    ) -> ModelMetadata:
        if self._fail:
            raise RuntimeError("Simulated provider failure")
        structured_payload = (
            self._raw_payload if self._raw_payload is not None else {"claims": self._claims}
        )
        template_content = f"template:{execution.template_id}:v{execution.template_version}"
        template = PromptTemplate(
            template_id=execution.template_id,
            template_version=execution.template_version,
            content=template_content,
            content_sha256=hashlib.sha256(template_content.encode()).hexdigest(),
        )
        rendered_text = f"{template_content}\nbundle:{bundle.bundle_id}"
        request = RenderedModelRequest(
            bundle_sha256=digest(bundle),
            template=template,
            generation_parameters=execution.generation_parameters,
            generation_parameters_sha256=hash_json(dict(execution.generation_parameters)),
            rendered_text=rendered_text,
            rendered_sha256=hashlib.sha256(rendered_text.encode()).hexdigest(),
        )
        raw_response = RawAnalystResponse(
            structured_payload=structured_payload,
            structured_payload_sha256=hash_json(structured_payload),
        )
        return ModelMetadata(
            provider="stub",
            model_id=self._model_id,
            model_version=self._model_version,
            request=request,
            raw_response=raw_response,
            generated_at=datetime.now(UTC),
        )
