"""Evidence subjects are associations, never substitutes for evidence identity."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.provenance import ObservationProvenance


class EvidenceSubject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["COMPANY", "SECTOR", "INDUSTRY", "GEOGRAPHY", "MACRO_SERIES"]
    identifier: str = Field(min_length=1)
    label: str = Field(min_length=1)
    basis: Literal["SOURCE_DECLARED", "CALLER_DECLARED", "VERIFIED_MAPPING"]
    declaration: str = Field(min_length=1)
    provenance: ObservationProvenance | None = None

    @model_validator(mode="after")
    def evidence_required(self) -> Self:
        if self.basis != "CALLER_DECLARED" and self.provenance is None:
            raise ValueError("Source/mapping associations require evidence provenance")
        return self
