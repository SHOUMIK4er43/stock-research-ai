"""Provider-neutral vocabulary used by versioned source mappings."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class MetricMappingStatus(StrEnum):
    MAPPED = "mapped"
    UNMAPPED = "unmapped"


class MetricDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    label: str
    definition: str
    version: str = "1"
