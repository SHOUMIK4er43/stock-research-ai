"""Ephemeral model transport projection; the frozen 5B bundle remains authoritative."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from stock_research_ai.domain.analysis import EvidenceReference, SourceEpistemicClass
from stock_research_ai.domain.calculations import CalculationModel


class AnalystModelEvidence(CalculationModel):
    reference: EvidenceReference
    status: str
    source_epistemic_class: SourceEpistemicClass
    limitations: tuple[str, ...] = ()
    # Canonical JSON text keeps the nested content immutable, including through copies.
    # The application serializer emits this as an object, never a double-encoded string.
    content_json: str


class AnalystModelInput(CalculationModel):
    projection_version: Literal["ANALYST_MODEL_INPUT_V1"] = "ANALYST_MODEL_INPUT_V1"
    bundle_id: UUID
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    items: tuple[AnalystModelEvidence, ...]
