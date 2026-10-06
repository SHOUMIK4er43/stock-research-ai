"""Deterministic offline stand-in for ChallengerModel. No network, no provider SDK."""

import hashlib
from datetime import UTC, datetime

from fakes.analyst import hash_json
from stock_research_ai.application.hashing import digest
from stock_research_ai.domain.analysis import (
    ModelMetadata,
    PromptTemplate,
    RawAnalystResponse,
    RenderedModelRequest,
)
from stock_research_ai.domain.challenge import ChallengerExecutionSpec, ChallengerInputBundle


class FakeChallengerModel:
    """Returns a pre-programmed list of ChallengerFindingCandidate-shaped dicts (under
    `structured_payload["candidates"]`), or raises to simulate a provider failure."""

    def __init__(
        self,
        candidates: list[dict[str, object]] | None = None,
        raw_payload: dict[str, object] | None = None,
        fail: bool = False,
        model_id: str = "fake-challenger-v1",
        model_version: str | None = "1",
        provider: str = "stub",
    ) -> None:
        self._candidates = candidates if candidates is not None else []
        self._raw_payload = raw_payload
        self._fail = fail
        self._model_id = model_id
        self._model_version = model_version
        self._provider = provider

    async def review(
        self, bundle: ChallengerInputBundle, execution: ChallengerExecutionSpec
    ) -> ModelMetadata:
        if self._fail:
            raise RuntimeError("Simulated challenger provider failure")
        structured_payload = (
            self._raw_payload
            if self._raw_payload is not None
            else {"candidates": self._candidates}
        )
        template_content = (
            f"challenger-template:{execution.template_id}:v{execution.template_version}"
        )
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
            provider=self._provider,
            model_id=self._model_id,
            model_version=self._model_version,
            request=request,
            raw_response=raw_response,
            generated_at=datetime.now(UTC),
        )
