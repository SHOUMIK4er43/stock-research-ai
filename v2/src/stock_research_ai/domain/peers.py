"""Peer membership is a source snapshot, without guessed internal identities."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from stock_research_ai.domain.financial import ParsedValue
from stock_research_ai.domain.provenance import ObservationProvenance, SourceLink


class PeerMetric(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str
    parsed: ParsedValue
    provenance: ObservationProvenance


class PeerEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    company_id: UUID | None = None
    link: SourceLink
    metrics: tuple[PeerMetric, ...] = ()


class PeerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entries: tuple[PeerEntry, ...] = ()
