"""Versioned market evidence; observation time is not system knowledge time."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stock_research_ai.domain.provenance import (
    ObservationProvenance,
    RawArtifact,
    SourceRetrieval,
)


class MarketModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class MarketRequest(MarketModel):
    symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    kind: Literal["EQUITY", "INDEX"]
    start: date
    end: date
    master_file: Path | None = None
    candles_file: Path | None = None
    expected_sessions: tuple[date, ...] | None = None
    evidence_kind: Literal["PROVIDER", "SYNTHETIC"] = "PROVIDER"

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.end < self.start:
            raise ValueError("End precedes start")
        if bool(self.master_file) != bool(self.candles_file):
            raise ValueError("Replay requires both master and candles files")
        if self.evidence_kind == "SYNTHETIC" and self.master_file is None:
            raise ValueError("Synthetic evidence requires replay files")
        if self.expected_sessions is not None:
            if tuple(sorted(set(self.expected_sessions))) != self.expected_sessions:
                raise ValueError("Expected sessions must be unique and ordered")
            if any(not self.start <= d <= self.end for d in self.expected_sessions):
                raise ValueError("Expected session outside request")
        return self


class MarketInstrument(MarketModel):
    id: UUID
    symbol: str
    exchange: str
    kind: Literal["EQUITY", "INDEX"]
    provider: str
    provider_identifier: str
    exchange_identifier: str | None = None
    name: str | None = None
    currency: str | None = None
    price_unit: Literal["PRICE", "INDEX_POINTS"]
    provenance: ObservationProvenance


class MarketObservation(MarketModel):
    id: UUID
    instrument_id: UUID
    trading_date: date
    candle_timestamp: AwareDatetime
    provider_updated_at: AwareDatetime | None = None
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = Field(default=None, ge=0)
    volume_semantics: Literal["PROVIDER_REPORTED", "UNKNOWN"] = "UNKNOWN"
    adjusted_close: Decimal | None = None
    adjustment_basis: Literal["UNKNOWN", "UNADJUSTED", "PROVIDER_ADJUSTED"] = "UNKNOWN"
    session_status: Literal["OBSERVATION_PRESENT", "NOT_TRADED", "SUSPENDED", "UNKNOWN"]
    provenance: ObservationProvenance

    @model_validator(mode="after")
    def ohlc(self) -> Self:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("High below OHLC values")
        if self.low > min(self.open, self.close):
            raise ValueError("Low above open/close")
        return self


class CorporateAction(MarketModel):
    instrument_id: UUID
    kind: Literal["DIVIDEND", "SPLIT", "BONUS", "RIGHTS", "OTHER"]
    ex_date: date | None = None
    provider_published_at: AwareDatetime | None = None
    description: str
    provenance: ObservationProvenance


class MarketIssue(MarketModel):
    code: str
    message: str
    locator: str | None = None


class MarketCoverage(MarketModel):
    overall: Literal["COMPLETE", "PARTIAL", "FAILED"]
    requested_start: date
    requested_end: date
    earliest: date | None = None
    latest: date | None = None
    records_received: int = Field(ge=0)
    records_normalized: int = Field(ge=0)
    malformed_records: int = Field(ge=0)
    records_excluded: int = Field(ge=0)
    duplicate_dates: tuple[date, ...] = ()
    missing_dates: tuple[date, ...] = ()
    unexpected_dates: tuple[date, ...] = ()
    expected_sessions: tuple[date, ...] | None = None
    session_basis: Literal["CALLER_DECLARED", "UNKNOWN"]
    missing_reason: Literal["UNKNOWN"] = "UNKNOWN"
    corporate_actions: Literal["NOT_PROVIDED_BY_SOURCE"] = "NOT_PROVIDED_BY_SOURCE"
    corporate_action_status: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    adjustment_coverage: Literal["UNKNOWN"] = "UNKNOWN"
    unavailable_fields: tuple[str, ...] = (
        "adjusted_close",
        "provider_updated_at",
        "verified_session_status",
        "currency",
    )
    issues: tuple[MarketIssue, ...] = ()


class MarketExtractionResult(MarketModel):
    schema_version: Literal["3B.1"] = "3B.1"
    id: UUID
    created_at: AwareDatetime
    request: MarketRequest
    instrument: MarketInstrument | None
    observations: tuple[MarketObservation, ...] = ()
    corporate_actions: tuple[CorporateAction, ...] = ()
    retrievals: tuple[SourceRetrieval, ...]
    raw_artifacts: tuple[RawArtifact, ...]
    coverage: MarketCoverage
    normalizer_version: str
    knowledge_basis: Literal["RETRIEVAL_TIME_ONLY"] = "RETRIEVAL_TIME_ONLY"

    @model_validator(mode="after")
    def lineage(self) -> Self:
        artifacts = {a.id: a for a in self.raw_artifacts}
        retrievals = {r.id for r in self.retrievals}
        if any(a.retrieval_id not in retrievals for a in artifacts.values()):
            raise ValueError("Artifact retrieval missing")
        if self.instrument is None and self.observations:
            raise ValueError("Observations require instrument identity")
        dates = [o.trading_date for o in self.observations]
        if dates != sorted(set(dates)):
            raise ValueError("Observation dates must be unique and ordered")
        if len(artifacts) != len(self.raw_artifacts) or len(retrievals) != len(self.retrievals):
            raise ValueError("Duplicate evidence IDs")
        if len({o.id for o in self.observations}) != len(self.observations):
            raise ValueError("Duplicate observation IDs")
        if self.instrument and (
            self.instrument.symbol != self.request.symbol
            or self.instrument.exchange != self.request.exchange
            or self.instrument.kind != self.request.kind
        ):
            raise ValueError("Instrument differs from request")
        if (self.coverage.requested_start, self.coverage.requested_end) != (
            self.request.start,
            self.request.end,
        ):
            raise ValueError("Coverage window differs from request")
        if self.coverage.expected_sessions != self.request.expected_sessions:
            raise ValueError("Coverage session evidence differs from request")
        if self.coverage.records_excluded != self.coverage.records_received - len(dates):
            raise ValueError("Excluded record count mismatch")
        if self.coverage.missing_dates != tuple(
            sorted(set(self.request.expected_sessions or ()) - set(dates))
        ):
            raise ValueError("Missing dates mismatch")
        if self.corporate_actions:
            raise ValueError("This snapshot version does not support corporate-action ingestion")
        provenances = [o.provenance for o in self.observations]
        if self.instrument:
            provenances.append(self.instrument.provenance)
        for provenance in provenances:
            if provenance.raw_artifact_id not in artifacts:
                raise ValueError("Missing evidence artifact")
        for observation in self.observations:
            if self.instrument is None or observation.instrument_id != self.instrument.id:
                raise ValueError("Wrong instrument lineage")
            if observation.trading_date != observation.candle_timestamp.date():
                raise ValueError("Observation date differs from source timestamp")
            if not self.request.start <= observation.trading_date <= self.request.end:
                raise ValueError("Observation outside requested window")
        if self.coverage.records_normalized != len(dates):
            raise ValueError("Coverage count mismatch")
        if self.coverage.earliest != (dates[0] if dates else None):
            raise ValueError("Coverage earliest mismatch")
        if self.coverage.latest != (dates[-1] if dates else None):
            raise ValueError("Coverage latest mismatch")
        if self.coverage.overall == "COMPLETE" and (
            not dates
            or self.coverage.issues
            or self.coverage.expected_sessions is None
            or set(dates) != set(self.coverage.expected_sessions)
            or any(r.status != "success" for r in self.retrievals)
        ):
            raise ValueError("Complete price coverage requires proven request coverage")
        return self
