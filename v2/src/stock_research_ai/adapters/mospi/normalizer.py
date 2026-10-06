import calendar
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from stock_research_ai.adapters.mospi.source import decode
from stock_research_ai.domain.macro import (
    MacroCoverage,
    MacroObservation,
    MacroRequest,
    MacroSeriesIdentity,
    MacroSnapshot,
)
from stock_research_ai.domain.provenance import (
    ObservationProvenance,
    RawArtifact,
    SourceLocation,
    SourceRetrieval,
)

VERSION = "mospi-cpi/1"


class MacroNormalizer:
    def normalize(
        self,
        request: MacroRequest,
        evidence: tuple[tuple[SourceRetrieval, RawArtifact | None, bytes | None], ...],
    ) -> MacroSnapshot:
        issues: list[str] = []
        unknown: set[str] = set()
        observations: dict[str, MacroObservation] = {}
        duplicates: set[str] = set()
        seen: set[str] = set()
        series = None
        malformed = 0
        try:
            decoded = []
            for r, a, b in evidence:
                if r.status != "success" or not a or b is None or not a.complete:
                    raise ValueError("SOURCE_UNAVAILABLE")
                if hashlib.sha256(b).hexdigest() != a.content_sha256:
                    raise ValueError("SHA_MISMATCH")
                decoded.append(decode(b))
            meta: Any = decoded[0]["data"]
            for name, code, label in (
                ("state", 99, "All India"),
                ("sector", 3, "Combined"),
                ("group", "0", "General"),
                ("subgroup", "0.99", "General-Overall"),
            ):
                if not any(
                    str(row[name + "_code"]) == str(code) and row[name + "_name"] == label
                    for row in meta[name]
                ):
                    raise ValueError("UNVERIFIED_SERIES_MAPPING")
            data: Any = decoded[1]
            if data["meta_data"]["totalPages"] != 1:
                issues.append("PAGINATION_INCOMPLETE")
            retrieval, artifact, _ = evidence[1]
            assert artifact is not None
            rows = data["data"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("INVALID_ROWS")
            metadata_artifact = evidence[0][1]
            assert metadata_artifact is not None
            semantics = set()
            for row in rows:
                semantics.add(
                    (
                        str(row.get("baseyear", "UNKNOWN")),
                        row.get("seasonal_adjustment", "UNKNOWN"),
                        row.get("unit", "INDEX_POINTS"),
                    )
                )
            if len(semantics) != 1:
                raise ValueError("AMBIGUOUS_SERIES_SEMANTICS")
            base, seasonal, unit = next(iter(semantics))
            if base != request.base_year:
                raise ValueError("BASE_YEAR_MISMATCH")
            if seasonal not in {"ADJUSTED", "UNADJUSTED", "UNKNOWN"}:
                seasonal = "UNKNOWN"
            if unit != "INDEX_POINTS":
                unit = "UNKNOWN"
            if seasonal == "UNKNOWN":
                unknown.add("seasonal_adjustment")
            if unit == "UNKNOWN":
                unknown.add("unit")
            key = json.dumps(
                [
                    "MoSPI",
                    "CPI",
                    "Current",
                    "All India",
                    "Combined",
                    "General-Overall",
                    "MONTHLY",
                    base,
                    seasonal,
                    unit,
                    "1",
                ]
            )
            series = MacroSeriesIdentity.model_validate(
                {
                    "id": uuid5(NAMESPACE_URL, key),
                    "publisher": "MoSPI",
                    "dataset": "CPI",
                    "source_series_key": "Current/99/3/0/0.99",
                    "geography": "All India",
                    "population": "Combined",
                    "frequency": "MONTHLY",
                    "unit": unit,
                    "scale": "1",
                    "base_year": base,
                    "seasonal_adjustment": seasonal,
                    "mapping_provenance": ObservationProvenance(
                        raw_artifact_id=metadata_artifact.id,
                        source_location=SourceLocation(locator="MCP.result.structuredContent.data"),
                        normalizer_version=VERSION,
                    ),
                    "provenance": ObservationProvenance(
                        raw_artifact_id=artifact.id,
                        source_location=SourceLocation(
                            locator="MCP.result.structuredContent.data[0]"
                        ),
                        normalizer_version=VERSION,
                    ),
                }
            )
            for i, row in enumerate(rows):
                try:
                    if any(
                        row[k] != v
                        for k, v in (
                            ("state", "All India"),
                            ("sector", "Combined"),
                            ("group", "General"),
                            ("subgroup", "General-Overall"),
                        )
                    ):
                        raise ValueError("Wrong series")
                    month = list(calendar.month_name).index(row["month"])
                    if not 1 <= month <= 12:
                        raise ValueError("Invalid month")
                    year = str(row["year"])
                    if len(year) != 4 or not year.isascii() or not year.isdigit():
                        raise ValueError("Invalid explicit year")
                    period = f"{year}-{month:02d}"
                    if period not in request.periods:
                        raise ValueError("Unrequested period")
                    if period in seen:
                        duplicates.add(period)
                        observations.pop(period, None)
                        raise ValueError("Duplicate reference period")
                    seen.add(period)
                    if isinstance(row["index"], bool):
                        raise ValueError("Boolean index")
                    value = Decimal(str(row["index"]))
                    if not value.is_finite():
                        raise ValueError("Nonfinite index")
                    status = row.get("status")
                    # Preserve opaque F/P flags without claiming a verified code mapping.
                    revision = (
                        status.upper()
                        if status in {"Final", "Provisional", "Revised"}
                        else "UNKNOWN"
                    )
                    if revision == "UNKNOWN":
                        unknown.add("revision_status")
                    if not row.get("published_at"):
                        unknown.add("published_at")
                    if not row.get("provider_vintage_at"):
                        unknown.add("provider_vintage_at")
                    observations[period] = MacroObservation.model_validate(
                        {
                            "series_id": series.id,
                            "reference_period": period,
                            "value": value,
                            "source_status": status,
                            "revision_status": revision,
                            "published_at": row.get("published_at"),
                            "provider_vintage_at": row.get("provider_vintage_at"),
                            "retrieved_at": retrieval.retrieved_at,
                            "provenance": ObservationProvenance(
                                raw_artifact_id=artifact.id,
                                source_location=SourceLocation(
                                    locator=f"MCP.result.structuredContent.data[{i}].index"
                                ),
                                normalizer_version=VERSION,
                            ),
                        }
                    )
                except (ValueError, TypeError, KeyError, InvalidOperation):
                    malformed += 1
                    issues.append(f"MALFORMED_ROW:{i}")
        except (ValueError, KeyError, TypeError, IndexError, UnicodeError) as exc:
            issues.append(str(exc))
        periods = tuple(sorted(observations))
        missing = tuple(sorted(set(request.periods) - set(periods)))
        if missing:
            issues.append("MISSING_PERIODS")
        coverage = MacroCoverage(
            overall="FAILED"
            if not observations
            else "PARTIAL"
            if issues or unknown
            else "COMPLETE",
            requested_periods=request.periods,
            returned_periods=periods,
            missing_periods=missing,
            duplicate_periods=tuple(sorted(duplicates)),
            malformed_records=malformed,
            unknown_metadata=tuple(sorted(unknown)),
            issues=tuple(issues),
        )
        return MacroSnapshot(
            id=uuid4(),
            request=request,
            series=series,
            observations=tuple(observations[p] for p in periods),
            retrievals=tuple(e[0] for e in evidence),
            artifacts=tuple(e[1] for e in evidence if e[1]),
            coverage=coverage,
            normalizer_version=VERSION,
        )
