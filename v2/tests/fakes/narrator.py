"""Deterministic offline stand-in for ReportNarrator. No network, no provider SDK."""

import hashlib
from datetime import UTC, datetime

from fakes.analyst import hash_json
from stock_research_ai.domain.analysis import (
    ModelMetadata,
    PromptTemplate,
    RawAnalystResponse,
    RenderedModelRequest,
)
from stock_research_ai.domain.report import ReportExecutionSpec, ReportSection


class FakeReportNarrator:
    """Returns a pre-programmed list of NarrativeBlockCandidate-shaped dicts (under
    `structured_payload["narrative_blocks"]`), or raises to simulate a provider failure."""

    def __init__(
        self,
        blocks: list[dict[str, object]] | None = None,
        raw_payload: dict[str, object] | None = None,
        fail: bool = False,
        model_id: str = "fake-narrator-v1",
        model_version: str | None = "1",
        provider: str = "stub",
    ) -> None:
        self._blocks = blocks if blocks is not None else []
        self._raw_payload = raw_payload
        self._fail = fail
        self._model_id = model_id
        self._model_version = model_version
        self._provider = provider

    async def narrate(
        self, sections: tuple[ReportSection, ...], execution: ReportExecutionSpec
    ) -> ModelMetadata:
        if self._fail:
            raise RuntimeError("Simulated narrator provider failure")
        structured_payload = (
            self._raw_payload
            if self._raw_payload is not None
            else {"narrative_blocks": self._blocks}
        )
        template_content = (
            f"narrator-template:{execution.template_id}:v{execution.template_version}"
        )
        template = PromptTemplate(
            template_id=execution.template_id,
            template_version=execution.template_version,
            content=template_content,
            content_sha256=hashlib.sha256(template_content.encode()).hexdigest(),
        )
        sections_hash = hash_json({"sections": [s.section for s in sections]})
        rendered_text = f"{template_content}\nsections:{sections_hash}"
        request = RenderedModelRequest(
            bundle_sha256=sections_hash,
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
