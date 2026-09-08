"""Small strict CSV/JSON adapter. No adjustment, calendar, or action inference."""

import csv
import hashlib
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

from stock_research_ai.domain.market import (
    MarketCoverage,
    MarketExtractionResult,
    MarketInstrument,
    MarketIssue,
    MarketObservation,
    MarketRequest,
)
from stock_research_ai.domain.provenance import (
    ObservationProvenance,
    RawArtifact,
    SourceLocation,
    SourceRetrieval,
)

VERSION = "kite-daily/1"


def provenance(artifact: RawArtifact, locator: str) -> ObservationProvenance:
    return ObservationProvenance(
        raw_artifact_id=artifact.id,
        source_location=SourceLocation(locator=locator),
        normalizer_version=VERSION,
    )


def verify(content: bytes, artifact: RawArtifact) -> None:
    if not artifact.complete or hashlib.sha256(content).hexdigest() != artifact.content_sha256:
        raise ValueError("Incomplete or corrupt evidence")


class KiteNormalizer:
    def identify(
        self, request: MarketRequest, content: bytes, artifact: RawArtifact
    ) -> MarketInstrument:
        verify(content, artifact)
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        segment = "INDICES" if request.kind == "INDEX" else "NSE"
        matches = [
            (i, row)
            for i, row in enumerate(rows)
            if (
                row.get("exchange") == request.exchange
                and row.get("tradingsymbol") == request.symbol
                and row.get("segment") == segment
                and row.get("instrument_type") == "EQ"
            )
        ]
        if len(matches) != 1:
            raise ValueError("Instrument master must contain exactly one matching identity")
        i, row = matches[0]
        token = row.get("instrument_token", "")
        if not token.isascii() or not token.isdigit():
            raise ValueError("Invalid instrument token")
        return MarketInstrument(
            id=uuid5(NAMESPACE_URL, json.dumps([request.exchange, request.symbol, request.kind])),
            symbol=request.symbol,
            exchange=request.exchange,
            kind=request.kind,
            provider="kite",
            provider_identifier=token,
            exchange_identifier=row.get("exchange_token") or None,
            name=row.get("name") or None,
            price_unit="INDEX_POINTS" if request.kind == "INDEX" else "PRICE",
            provenance=provenance(artifact, f"csv:data-row[{i + 1}]"),
        )

    def normalize(
        self,
        request: MarketRequest,
        instrument: MarketInstrument | None,
        evidence: tuple[tuple[SourceRetrieval, RawArtifact | None, bytes | None], ...],
        identity_error: str | None = None,
    ) -> MarketExtractionResult:
        issues: list[MarketIssue] = []
        if identity_error:
            issues.append(MarketIssue(code="IDENTITY_FAILED", message=identity_error))
        observations: dict[date, MarketObservation] = {}
        duplicates: set[date] = set()
        seen: set[date] = set()
        received = malformed = 0
        for retrieval, artifact, content in evidence:
            if retrieval.status != "success" or not retrieval.complete:
                issues.append(
                    MarketIssue(code="ACQUISITION_FAILED", message=retrieval.error or "Failed")
                )
                continue
            if artifact is None or content is None or artifact.source_type != "market_candles":
                continue
            if instrument is None:
                continue
            verify(content, artifact)
            try:
                body = json.loads(content, parse_float=Decimal, parse_int=Decimal)
                if not isinstance(body, dict) or body.get("status") != "success":
                    raise ValueError("Unsuccessful JSON response")
                candles = body["data"]["candles"]
                if not isinstance(candles, list):
                    raise ValueError("Candles must be an array")
            except (ValueError, KeyError, TypeError):
                issues.append(
                    MarketIssue(code="MALFORMED_PAYLOAD", message="Invalid candle response")
                )
                continue
            previous: date | None = None
            for i, row in enumerate(candles):
                received += 1
                locator = f"$.data.candles[{i}]"
                try:
                    if not isinstance(row, list) or len(row) != 6:
                        raise ValueError("Expected six candle fields")
                    if not isinstance(row[0], str):
                        raise ValueError("Timestamp must be a string")
                    stamp = datetime.fromisoformat(row[0])
                    offset = stamp.utcoffset()
                    if offset is None or offset.total_seconds() != 19800:
                        raise ValueError("Expected explicit India timezone offset")
                    if stamp.time().replace(tzinfo=None).isoformat() != "00:00:00":
                        raise ValueError("Daily candle must start at midnight")
                    day = stamp.date()
                    if not request.start <= day <= request.end:
                        raise ValueError("Candle outside requested range")
                    if day in seen:
                        duplicates.add(day)
                        observations.pop(day, None)
                        raise ValueError("Duplicate date: all conflicting rows excluded")
                    seen.add(day)
                    if previous is not None and day < previous:
                        issues.append(
                            MarketIssue(
                                code="UNORDERED", message="Source dates unordered", locator=locator
                            )
                        )
                    previous = day
                    if any(not isinstance(v, Decimal) for v in row[1:]):
                        raise ValueError("OHLCV must contain JSON numbers")
                    observation = MarketObservation(
                        id=uuid5(
                            NAMESPACE_URL, f"{artifact.content_sha256}:{instrument.id}:{locator}"
                        ),
                        instrument_id=instrument.id,
                        trading_date=day,
                        candle_timestamp=stamp,
                        open=row[1],
                        high=row[2],
                        low=row[3],
                        close=row[4],
                        volume=row[5],
                        volume_semantics="PROVIDER_REPORTED"
                        if instrument.kind == "EQUITY"
                        else "UNKNOWN",
                        session_status="OBSERVATION_PRESENT",
                        provenance=provenance(artifact, locator),
                    )
                    observations[day] = observation
                except (ValueError, TypeError, OverflowError):
                    malformed += 1
                    issues.append(
                        MarketIssue(
                            code="INVALID_CANDLE",
                            message="Invalid OHLCV, date or duplicate",
                            locator=locator,
                        )
                    )
        dates = sorted(observations)
        missing = tuple(sorted(set(request.expected_sessions or ()) - set(dates)))
        unexpected = (
            tuple(sorted(set(dates) - set(request.expected_sessions or ())))
            if request.expected_sessions is not None
            else ()
        )
        if missing:
            issues.append(
                MarketIssue(
                    code="MISSING_SESSIONS", message="Expected observations absent; cause unknown"
                )
            )
        if unexpected:
            issues.append(
                MarketIssue(
                    code="UNEXPECTED_SESSIONS", message="Outside caller-declared session set"
                )
            )
        if request.expected_sessions is None:
            issues.append(
                MarketIssue(
                    code="SESSION_COVERAGE_UNKNOWN",
                    message="No authoritative session calendar supplied",
                )
            )
        if not dates:
            issues.append(
                MarketIssue(
                    code="NO_OBSERVATIONS", message="No valid observations; not empty success"
                )
            )
        coverage = MarketCoverage(
            overall="FAILED" if not dates else "PARTIAL" if issues else "COMPLETE",
            requested_start=request.start,
            requested_end=request.end,
            earliest=dates[0] if dates else None,
            latest=dates[-1] if dates else None,
            records_received=received,
            records_normalized=len(dates),
            malformed_records=malformed,
            records_excluded=received - len(dates),
            duplicate_dates=tuple(sorted(duplicates)),
            missing_dates=missing,
            unexpected_dates=unexpected,
            expected_sessions=request.expected_sessions,
            session_basis="CALLER_DECLARED" if request.expected_sessions is not None else "UNKNOWN",
            issues=tuple(issues),
        )
        return MarketExtractionResult(
            id=uuid4(),
            created_at=datetime.now(UTC),
            request=request,
            instrument=instrument,
            observations=tuple(observations[d] for d in dates),
            retrievals=tuple(e[0] for e in evidence),
            raw_artifacts=tuple(e[1] for e in evidence if e[1] is not None),
            coverage=coverage,
            normalizer_version=VERSION,
        )
