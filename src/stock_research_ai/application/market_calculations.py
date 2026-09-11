"""Six deterministic calculations on pinned close observations; no acquisition."""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction
from typing import Any
from uuid import UUID, uuid4

from stock_research_ai.application.hashing import digest
from stock_research_ai.application.numerics import root_growth
from stock_research_ai.domain.calculations import CalculationIssue, FormulaDefinition
from stock_research_ai.domain.market import MarketExtractionResult, MarketObservation
from stock_research_ai.domain.market_calculations import (
    DrawdownEpisode,
    MarketCalculationInput,
    MarketCalculationRequest,
    MarketCalculationResult,
    MarketCalculationSnapshot,
    MarketCalculationSpec,
    MarketSourceReference,
)

FORMULAS = {
    name: FormulaDefinition(id=name, expression=expression, working_precision=70)
    for name, expression in {
        "PRICE_RETURN": "(end_close / start_close - 1) * 100",
        "PRICE_CAGR": "(exp(ln(end_close / start_close) / (elapsed_days / 365)) - 1) * 100",
        "MAX_DRAWDOWN": "max(1 - close / running_peak) * 100; earliest tied episode",
        "REALIZED_VOLATILITY": (
            "sqrt(sum((r - mean(r))**2)/(n-1)) * sqrt(252) * 100; r=close/prior_close-1"
        ),
        "SMA": "sum(last_N_closes) / N",
        "BENCHMARK_RELATIVE_RETURN": "company_PRICE_RETURN - benchmark_PRICE_RETURN",
    }.items()
}


class MarketInputFailure(Exception):
    def __init__(self, status: str, code: str, ids: tuple[UUID, ...] = ()) -> None:
        self.status, self.code, self.ids = status, code, ids


def issue(code: str, ids: tuple[UUID, ...] = ()) -> CalculationIssue:
    return CalculationIssue(code=code, severity="LIMITATION", message=code, observation_ids=ids)


def decimal(ratio: Fraction) -> Decimal:
    return Decimal(ratio.numerator) / Decimal(ratio.denominator)


def source_reference(source: MarketExtractionResult) -> MarketSourceReference:
    return MarketSourceReference(
        extraction_id=source.id,
        instrument=source.instrument,
        payload_sha256=digest(source),
    )


def calculate_market(
    source: MarketExtractionResult,
    request: MarketCalculationRequest,
    benchmark: MarketExtractionResult | None = None,
) -> MarketCalculationSnapshot:
    sources = {s.id: s for s in (source, benchmark) if s is not None}
    refs = tuple(source_reference(s) for s in sources.values())
    now, snapshot_id = datetime.now(UTC), uuid4()
    results = []
    for spec in request.calculations:
        data: dict[str, Any] = dict(
            snapshot_id=snapshot_id,
            metric_id=spec.metric_id,
            formula=FORMULAS[spec.metric_id],
            status="AVAILABLE",
            calculated_at=now,
            requested_start=request.start_date,
            requested_end=request.end_date,
            sources=refs,
            alignment_policy=request.benchmark_alignment_policy
            if spec.metric_id == "BENCHMARK_RELATIVE_RETURN"
            else request.alignment_policy,
        )
        issues = [
            issue("CORPORATE_ACTIONS_NOT_PROVIDED_BY_SOURCE"),
            issue("PINNED_RETRIEVAL_NOT_HISTORICAL_AVAILABILITY_PROOF"),
        ]
        used: list[MarketCalculationInput] = []
        try:
            with localcontext(Context(prec=70, rounding=ROUND_HALF_EVEN)):
                value = evaluate(source, request, spec, benchmark, data, used, issues)
                if isinstance(value, Fraction):
                    data.update(
                        exact_numerator=value.numerator, exact_denominator=value.denominator
                    )
                    value = decimal(value)
                else:
                    data["numerical_method"] = "DECIMAL_TRANSCENDENTAL"
                with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                    data["value"] = +value
                    data["display_value"] = value.quantize(Decimal("0.01"))
        except MarketInputFailure as exc:
            data["status"] = exc.status
            issues.append(
                CalculationIssue(
                    code=exc.code, severity="ERROR", message=exc.code, observation_ids=exc.ids
                )
            )
        except ArithmeticError:
            data["status"] = "INVALID_INPUT"
            issues.append(
                CalculationIssue(
                    code="DECIMAL_RANGE_ERROR",
                    severity="ERROR",
                    message="Value exceeds fixed Decimal context",
                )
            )
        if data["status"] != "AVAILABLE":
            for key in (
                "value",
                "display_value",
                "exact_numerator",
                "exact_denominator",
                "episode",
                "company_price_return",
                "benchmark_price_return",
            ):
                data.pop(key, None)
        data.update(inputs=tuple(used), issues=tuple(issues))
        results.append(MarketCalculationResult.model_validate(data))
    return MarketCalculationSnapshot(
        id=snapshot_id, request=request, calculated_at=now, sources=refs, results=tuple(results)
    )


def validate_source(
    source: MarketExtractionResult, extraction_id: UUID, instrument_id: UUID
) -> None:
    if source.id != extraction_id or not source.instrument or source.instrument.id != instrument_id:
        raise MarketInputFailure("INCOMPATIBLE_SERIES", "EXTRACTION_INSTRUMENT_MISMATCH")
    rows = source.observations
    dates = [o.trading_date for o in rows]
    if len(set(dates)) != len(dates) or len({o.id for o in rows}) != len(rows):
        raise MarketInputFailure(
            "INVALID_INPUT", "DUPLICATE_SOURCE_OBSERVATIONS", tuple(o.id for o in rows)
        )
    if dates != sorted(dates) or any(i.code == "UNORDERED" for i in source.coverage.issues):
        raise MarketInputFailure("INVALID_INPUT", "UNSORTED_SOURCE_OBSERVATIONS")
    if any(o.instrument_id != instrument_id for o in rows):
        raise MarketInputFailure("INCOMPATIBLE_SERIES", "OBSERVATION_INSTRUMENT_MISMATCH")


def evaluate(
    source: MarketExtractionResult,
    request: MarketCalculationRequest,
    spec: MarketCalculationSpec,
    benchmark: MarketExtractionResult | None,
    data: dict[str, Any],
    used: list[MarketCalculationInput],
    issues: list[CalculationIssue],
) -> Fraction | Decimal:
    validate_source(source, request.extraction_id, request.instrument_id)
    if request.end_date < request.start_date or (
        request.end_date == request.start_date
        and spec.metric_id in {"PRICE_RETURN", "PRICE_CAGR", "BENCHMARK_RELATIVE_RETURN"}
    ):
        raise MarketInputFailure("INVALID_PERIOD", "NONPOSITIVE_INTERVAL")
    for pinned, day in (
        (request.start_observation_id, request.start_date),
        (request.end_observation_id, request.end_date),
    ):
        if pinned is not None and not any(
            o.id == pinned and o.trading_date == day for o in source.observations
        ):
            raise MarketInputFailure(
                "INVALID_INPUT", "OBSERVATION_NOT_IN_REQUESTED_EXTRACTION_PERIOD", (pinned,)
            )

    def select(s: MarketExtractionResult, relative: bool = False) -> list[MarketObservation]:
        return select_range(s, request.start_date, request.end_date, issues, relative)

    rows = select(source, spec.metric_id == "BENCHMARK_RELATIVE_RETURN")
    if spec.metric_id in {"PRICE_RETURN", "PRICE_CAGR", "BENCHMARK_RELATIVE_RETURN"}:
        rows = [rows[0], rows[-1]]
    else:
        minimum = (
            spec.window
            if spec.metric_id == "SMA"
            else 3
            if spec.metric_id == "REALIZED_VOLATILITY"
            else 2
        )
        if len(rows) < minimum:
            raise MarketInputFailure("INSUFFICIENT_INPUT", "TOO_FEW_OBSERVATIONS")
        if spec.metric_id == "SMA":
            rows = rows[-spec.window :]
            data["window"] = spec.window
        expected = source.request.expected_sessions
        if expected is None:
            issues.append(issue("OBSERVED_SESSIONS_ONLY"))
        else:
            selected_dates = {o.trading_date for o in rows}
            missing = {
                d for d in expected if rows[0].trading_date <= d <= rows[-1].trading_date
            } - selected_dates
            if missing:
                raise MarketInputFailure("INSUFFICIENT_INPUT", "EXPECTED_SESSION_MISSING")
        if any(
            rows[0].trading_date <= d <= rows[-1].trading_date
            for d in source.coverage.missing_dates
        ):
            raise MarketInputFailure("INSUFFICIENT_INPUT", "EXPECTED_SESSION_MISSING")
    data.update(
        actual_start=rows[0].trading_date,
        actual_end=rows[-1].trading_date,
        observation_count=len(rows),
    )

    def retain(s: MarketExtractionResult, selected: list[MarketObservation]) -> list[Fraction]:
        invalid = tuple(o.id for o in selected if not o.close.is_finite() or o.close <= 0)
        if invalid:
            raise MarketInputFailure("INVALID_INPUT", "NONPOSITIVE_OR_NONFINITE_CLOSE", invalid)
        used.extend(MarketCalculationInput(extraction_id=s.id, observation=o) for o in selected)
        artifact_map = {a.id: a for a in s.raw_artifacts}
        retrieval_map = {r.id: r for r in s.retrievals}
        for o in selected:
            artifact = artifact_map.get(o.provenance.raw_artifact_id)
            retrieval = (
                retrieval_map.get(artifact.retrieval_id)
                if artifact and artifact.retrieval_id
                else None
            )
            if (
                not artifact
                or not retrieval
                or not artifact.complete
                or retrieval.status != "success"
            ):
                raise MarketInputFailure("INVALID_INPUT", "INPUT_EVIDENCE_UNAVAILABLE", (o.id,))
        return [Fraction(o.close) for o in selected]

    closes = retain(source, rows)
    bases = {o.adjustment_basis for o in rows}
    if len(bases) != 1:
        raise MarketInputFailure("INCOMPATIBLE_SERIES", "MIXED_ADJUSTMENT_BASIS")
    data["adjustment_basis"] = rows[0].adjustment_basis
    if "UNKNOWN" in bases:
        issues.append(issue("UNVERIFIED_ADJUSTMENT"))
    if spec.metric_id == "PRICE_RETURN":
        return (closes[-1] / closes[0] - 1) * 100
    if spec.metric_id == "PRICE_CAGR":
        days = (rows[-1].trading_date - rows[0].trading_date).days
        years = Decimal(days) / Decimal("365")
        data.update(
            annualization_policy="ACTUAL_DAYS_365_FIXED_V1", elapsed_days=days, elapsed_years=years
        )
        return root_growth(decimal(closes[-1] / closes[0]), years) * 100
    if spec.metric_id == "SMA":
        assert source.instrument is not None
        data.update(unit=source.instrument.price_unit, currency=source.instrument.currency)
        if source.instrument.price_unit == "PRICE" and source.instrument.currency is None:
            issues.append(issue("SOURCE_CURRENCY_UNKNOWN"))
        return numeric_mean(closes)
    if spec.metric_id == "REALIZED_VOLATILITY":
        returns = [b / a - 1 for a, b in zip(closes, closes[1:], strict=False)]
        n = len(returns)
        mean = sum(returns, Fraction()) / n
        variance = sum(((r - mean) ** 2 for r in returns), Fraction()) / (n - 1)
        data.update(
            return_count=n,
            annualization_factor=252,
            return_definition="ARITHMETIC_SIMPLE_RETURN",
            variance_definition="SAMPLE_VARIANCE",
            annualization_policy="ARITHMETIC_SAMPLE_252_V1",
        )
        return decimal_sqrt(decimal(variance)) * decimal_sqrt(Decimal(252)) * 100
    if spec.metric_id == "MAX_DRAWDOWN":
        peak = peak_index = trough_index = 0
        worst = Fraction()
        for i, close in enumerate(closes):
            if close > closes[peak]:
                peak = i
            loss = (1 - close / closes[peak]) * 100
            if loss > worst:
                worst, peak_index, trough_index = loss, peak, i
        if worst:
            recovery = next(
                (
                    rows[i]
                    for i in range(trough_index + 1, len(rows))
                    if closes[i] >= closes[peak_index]
                ),
                None,
            )
            p, t = rows[peak_index], rows[trough_index]
            data["episode"] = DrawdownEpisode(
                peak_observation_id=p.id,
                peak_date=p.trading_date,
                peak_value=p.close,
                trough_observation_id=t.id,
                trough_date=t.trading_date,
                trough_value=t.close,
                recovery_observation_id=recovery.id if recovery else None,
                recovery_date=recovery.trading_date if recovery else None,
                recovery_value=recovery.close if recovery else None,
            )
            if recovery is None:
                issues.append(issue("NOT_RECOVERED_WITHIN_WINDOW"))
        return worst
    if (
        benchmark is None
        or request.benchmark_extraction_id is None
        or request.benchmark_instrument_id is None
    ):
        raise MarketInputFailure("INCOMPATIBLE_SERIES", "BENCHMARK_REQUIRED")
    if request.benchmark_instrument_id == request.instrument_id:
        raise MarketInputFailure("INCOMPATIBLE_SERIES", "BENCHMARK_EQUALS_COMPANY")
    validate_source(benchmark, request.benchmark_extraction_id, request.benchmark_instrument_id)
    benchmark_rows = select(benchmark, True)
    benchmark_rows = [benchmark_rows[0], benchmark_rows[-1]]
    benchmark_closes = retain(benchmark, benchmark_rows)
    if {o.adjustment_basis for o in benchmark_rows} != bases:
        raise MarketInputFailure("INCOMPATIBLE_SERIES", "INCOMPATIBLE_ADJUSTMENT_BASIS")
    company_return = (closes[-1] / closes[0] - 1) * 100
    benchmark_return = (benchmark_closes[-1] / benchmark_closes[0] - 1) * 100
    data.update(
        unit="PERCENTAGE_POINTS",
        company_price_return=decimal(company_return),
        benchmark_price_return=decimal(benchmark_return),
    )
    return company_return - benchmark_return


def select_range(
    s: MarketExtractionResult,
    start_date: date,
    end_date: date,
    issues: list[CalculationIssue],
    relative: bool = False,
) -> list[MarketObservation]:
    rows = [o for o in s.observations if start_date <= o.trading_date <= end_date]
    if not rows or rows[0].trading_date != start_date or rows[-1].trading_date != end_date:
        raise MarketInputFailure(
            "INCOMPATIBLE_SERIES" if relative else "INSUFFICIENT_INPUT",
            "EXACT_ENDPOINT_MISSING",
        )
    if any(start_date <= d <= end_date for d in s.coverage.duplicate_dates):
        raise MarketInputFailure("INVALID_INPUT", "SOURCE_DUPLICATE_DATE_EXCLUDED")
    if s.coverage.overall != "COMPLETE":
        issues.append(issue("PARTIAL_SOURCE_COVERAGE"))
    return rows


def numeric_mean[Number: (Decimal, Fraction)](values: Sequence[Number]) -> Number:
    total = values[0]
    for value in values[1:]:
        total += value
    return total / len(values)


def decimal_sqrt(value: Decimal) -> Decimal:
    return value.sqrt()
