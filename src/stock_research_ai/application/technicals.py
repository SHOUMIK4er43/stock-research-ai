"""Fixed technical indicators; bounded Decimal recursive state and explicit point graph."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from uuid import uuid4

from stock_research_ai.application import market_calculations as market
from stock_research_ai.domain.calculations import CalculationIssue
from stock_research_ai.domain.market import MarketExtractionResult, MarketObservation
from stock_research_ai.domain.market_calculations import MarketCalculationInput
from stock_research_ai.domain.technicals import (
    PUBLIC_INDICATORS,
    IndicatorDefinition,
    IndicatorPoint,
    IndicatorSeries,
    PrecisionPolicy,
    TechnicalRequest,
    TechnicalSnapshot,
)

FIRST = dict(
    zip(PUBLIC_INDICATORS, (20, 15, 26, 34, 34, 2, 15, 20, 20, 20, 15, 15, 28), strict=True)
)


def definition(
    name: str, first: int, parameters: tuple[tuple[str, str], ...] = ()
) -> IndicatorDefinition:
    seed, smoothing = {
        "EMA20": ("SMA first N closes", "alpha=2/(N+1); alpha*close+(1-alpha)*previous"),
        "RSI14": (
            "Mean gains/losses of transitions 2..15",
            "WILDER14 gain/loss; 100-100/(1+gain/loss); rising=100; falling=0; "
            "FLAT_RSI_MIDPOINT_V1=50",
        ),
        "MACD12_26": ("Independent SMA-seeded EMA12 and EMA26", "EMA12-EMA26"),
        "MACD_SIGNAL9": (
            "Mean of nine MACD points at observations 26..34",
            "alpha=2/10; alpha*MACD+(1-alpha)*previous_signal",
        ),
        "MACD_HISTOGRAM": ("Both MACD and signal available", "working_MACD-working_signal"),
        "TRUE_RANGE": (
            "No value at observation 1; transitions start at 2",
            "max(high-low,abs(high-previous_close),abs(low-previous_close))",
        ),
        "ATR14": ("Mean TR at observations 2..15", "(previous*13+current_TR)/14"),
        "BOLLINGER_MIDDLE20": ("First 20 closes", "mean(20 closes)"),
        "BOLLINGER_UPPER20_2": (
            "First 20 closes",
            "mean+2*sqrt(mean((close-mean)^2)); POPULATION_VARIANCE",
        ),
        "BOLLINGER_LOWER20_2": (
            "First 20 closes",
            "mean-2*sqrt(mean((close-mean)^2)); POPULATION_VARIANCE",
        ),
        "PLUS_DI14": (
            "Mean TR/+DM from transitions 2..15; positive up>down else zero",
            "100*WILDER14(+DM)/WILDER14(TR); zero TR gives zero DI",
        ),
        "MINUS_DI14": (
            "Mean TR/-DM from transitions 2..15; positive down>up else zero",
            "100*WILDER14(-DM)/WILDER14(TR); zero TR gives zero DI",
        ),
        "ADX14": (
            "Mean DX at observations 15..28",
            "DX=100*abs(+DI-(-DI))/(+DI+(-DI)); zero denominator gives zero DX; "
            "ADX=(previous*13+current_DX)/14",
        ),
    }.get(name, ("Input/dependent point references define seed", "Typed audit intermediate V1"))
    return IndicatorDefinition(
        indicator_id=name,
        formula_id=name,
        first_observation=first,
        parameters=parameters,
        public=name in PUBLIC_INDICATORS,
        seed_policy="REQUEST_RANGE_ONLY_V1; " + seed,
        smoothing_policy=smoothing,
    )


REGISTRY = tuple(
    definition(
        name,
        FIRST[name],
        (("period", "20"), ("multiplier", "2"))
        if name.startswith("BOLLINGER")
        else (("fast", "12"), ("slow", "26"), ("signal", "9"))
        if name.startswith("MACD")
        else (("period", "20"),)
        if name == "EMA20"
        else (("period", "1"),)
        if name == "TRUE_RANGE"
        else (("period", "14"),),
    )
    for name in PUBLIC_INDICATORS
)


class Builder:
    def __init__(
        self,
        rows: list[MarketObservation],
        request: TechnicalRequest,
        price_unit: str,
        currency: str | None,
        registry: tuple[IndicatorDefinition, ...],
    ) -> None:
        self.rows, self.request, self.price_unit, self.currency = (
            rows,
            request,
            price_unit,
            currency,
        )
        self.registry = {d.indicator_id: d for d in registry}
        self.values: dict[str, list[Decimal | None]] = {}
        self.series: dict[str, IndicatorSeries] = {}

    def record(
        self,
        name: str,
        values: list[Decimal | None],
        deps: list[list[tuple[str, int]]],
        observations: list[list[int]],
        recursive: bool = False,
        first: int = 1,
        unit: str | None = None,
        method: str = "DECIMAL_RECURSIVE",
        period: int = 0,
    ) -> None:
        points: list[IndicatorPoint] = []
        for i, value in enumerate(values):
            prev = (
                points[i - 1].id
                if recursive and i > 0 and values[i - 1] is not None and value is not None
                else None
            )
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                stored = +value if value is not None else None
                display = value.quantize(Decimal(".01")) if value is not None else None
            points.append(
                IndicatorPoint.model_validate(
                    dict(
                        id=uuid4(),
                        indicator_id=name,
                        definition_version=self.registry[name].version
                        if name in self.registry
                        else 1,
                        date=self.rows[i].trading_date,
                        status="AVAILABLE" if value is not None else "INSUFFICIENT_INPUT",
                        value=stored,
                        display_value=display,
                        numerical_method=method,
                        observation_ids=tuple(self.rows[j].id for j in observations[i])
                        if not (recursive and prev is None)
                        else (),
                        seed_observation_ids=tuple(self.rows[j].id for j in observations[i])
                        if recursive and prev is None and value is not None
                        else (),
                        previous_point_id=prev,
                        dependent_point_ids=tuple(self.series[n].points[j].id for n, j in deps[i])
                        if value is not None
                        else (),
                        issue=None if value is not None else "INITIALIZATION_REQUIRED",
                    )
                )
            )
        spec = self.registry.get(name) or definition(
            name, first, (("period", str(period)),) if period else ()
        )
        self.values[name] = values
        self.series[name] = IndicatorSeries.model_validate(
            dict(
                definition=spec,
                status=points[-1].status,
                unit=unit or self.price_unit,
                currency=self.currency if (unit or self.price_unit) == "PRICE" else None,
                numerical_method=method,
                points=tuple(points),
                requested=name in self.request.indicators,
            )
        )

    def smooth(self, name: str, source: str | None, n: int, ema: bool = False) -> None:
        seq = self.values[source] if source else [o.close for o in self.rows]
        out: list[Decimal | None] = [None] * len(seq)
        deps: list[list[tuple[str, int]]] = [[] for _ in seq]
        obs: list[list[int]] = [[] for _ in seq]
        valid = [i for i, v in enumerate(seq) if v is not None]
        if len(valid) >= n:
            seed = valid[n - 1]
            out[seed] = market.numeric_mean([v for v in seq[valid[0] : seed + 1] if v is not None])
            if source:
                deps[seed] = [(source, j) for j in valid[:n]]
            else:
                obs[seed] = valid[:n]
            alpha = Decimal(2) / Decimal(n + 1) if ema else Decimal(1) / Decimal(n)
            for i in valid[n:]:
                previous, current = out[i - 1], seq[i]
                assert previous is not None and current is not None
                out[i] = (
                    alpha * current + (1 - alpha) * previous
                    if ema
                    else (previous * (n - 1) + current) / n
                )
                if source:
                    deps[i] = [(source, i)]
                else:
                    obs[i] = [i]
        first = (valid[0] + n) if valid else n
        self.record(name, out, deps, obs, True, first, period=n)

    def combine(
        self,
        name: str,
        sources: tuple[str, ...],
        fn: Callable[[list[Decimal]], Decimal],
        unit: str | None = None,
        method: str = "DECIMAL_RECURSIVE",
    ) -> None:
        values = []
        deps = []
        for i in range(len(self.rows)):
            args = [self.values[n][i] for n in sources]
            values.append(
                fn([v for v in args if v is not None]) if all(v is not None for v in args) else None
            )
            deps.append([(n, i) for n in sources])
        first = next((i + 1 for i, v in enumerate(values) if v is not None), len(values) + 1)
        self.record(name, values, deps, [[] for _ in values], first=first, unit=unit, method=method)

    def raw_transitions(self) -> None:
        if "TRUE_RANGE" in self.values:
            return
        arrays: dict[str, list[Decimal | None]] = {
            n: [None]
            for n in (
                "TRUE_RANGE",
                "PLUS_DM_INTERNAL",
                "MINUS_DM_INTERNAL",
                "GAIN_INTERNAL",
                "LOSS_INTERNAL",
            )
        }
        for prior, current in zip(self.rows, self.rows[1:], strict=False):
            up, down = current.high - prior.high, prior.low - current.low
            arrays["TRUE_RANGE"].append(
                max(
                    current.high - current.low,
                    abs(current.high - prior.close),
                    abs(current.low - prior.close),
                )
            )
            arrays["PLUS_DM_INTERNAL"].append(up if up > 0 and up > down else Decimal(0))
            arrays["MINUS_DM_INTERNAL"].append(down if down > 0 and down > up else Decimal(0))
            arrays["GAIN_INTERNAL"].append(max(current.close - prior.close, Decimal(0)))
            arrays["LOSS_INTERNAL"].append(max(prior.close - current.close, Decimal(0)))
        for name, values in arrays.items():
            self.record(
                name,
                values,
                [[] for _ in values],
                [[]] + [[i - 1, i] for i in range(1, len(values))],
                first=2,
                method="DECIMAL_FINITE_WINDOW",
            )

    def ensure(self, name: str) -> None:
        if name in self.values:
            return
        if name == "EMA20":
            self.smooth(name, None, int(dict(self.registry[name].parameters)["period"]), True)
        elif name.startswith("MACD"):
            if "MACD12_26" not in self.values:
                self.smooth("EMA12_INTERNAL", None, 12, True)
                self.smooth("EMA26_INTERNAL", None, 26, True)
                self.combine(
                    "MACD12_26", ("EMA12_INTERNAL", "EMA26_INTERNAL"), lambda v: v[0] - v[1]
                )
            if name != "MACD12_26" and "MACD_SIGNAL9" not in self.values:
                self.smooth("MACD_SIGNAL9", "MACD12_26", 9, True)
            if name == "MACD_HISTOGRAM":
                self.combine(name, ("MACD12_26", "MACD_SIGNAL9"), lambda v: v[0] - v[1])
        elif name.startswith("BOLLINGER"):
            if "BOLLINGER_MIDDLE20" not in self.values:
                mean: list[Decimal | None] = [None] * len(self.rows)
                var = mean.copy()
                obs: list[list[int]] = [[] for _ in mean]
                for i in range(19, len(mean)):
                    window = [o.close for o in self.rows[i - 19 : i + 1]]
                    m = market.numeric_mean(window)
                    mean[i] = m
                    var[i] = market.numeric_mean([(v - m) ** 2 for v in window])
                    obs[i] = list(range(i - 19, i + 1))
                self.record(
                    "BOLLINGER_MIDDLE20",
                    mean,
                    [[] for _ in mean],
                    obs,
                    first=20,
                    method="DECIMAL_FINITE_WINDOW",
                )
                self.record(
                    "VARIANCE_INTERNAL",
                    var,
                    [[] for _ in mean],
                    obs,
                    first=20,
                    unit="SQUARED_" + self.price_unit,
                    period=20,
                    method="DECIMAL_FINITE_WINDOW",
                )
                self.combine(
                    "STDDEV_INTERNAL",
                    ("VARIANCE_INTERNAL",),
                    lambda v: market.decimal_sqrt(v[0]),
                    method="DECIMAL_SQRT",
                )
            if name != "BOLLINGER_MIDDLE20":
                sign = 1 if name == "BOLLINGER_UPPER20_2" else -1
                self.combine(
                    name,
                    ("BOLLINGER_MIDDLE20", "STDDEV_INTERNAL"),
                    lambda v: v[0] + sign * 2 * v[1],
                    method="DECIMAL_SQRT",
                )
        else:
            self.raw_transitions()
            if name == "TRUE_RANGE":
                return
            if name == "RSI14":
                self.smooth("AVG_GAIN_INTERNAL", "GAIN_INTERNAL", 14)
                self.smooth("AVG_LOSS_INTERNAL", "LOSS_INTERNAL", 14)
                self.combine(
                    name,
                    ("AVG_GAIN_INTERNAL", "AVG_LOSS_INTERNAL"),
                    lambda v: (
                        Decimal(50)
                        if v[0] == v[1] == 0
                        else Decimal(100)
                        if v[1] == 0
                        else 100 - 100 / (1 + v[0] / v[1])
                    ),
                    unit="DIMENSIONLESS_0_100",
                )
            else:
                if "ATR14" not in self.values:
                    self.smooth("ATR14", "TRUE_RANGE", 14)
                if name == "ATR14":
                    return
                if "DX_INTERNAL" not in self.values:
                    self.smooth("SMOOTH_PLUS_DM_INTERNAL", "PLUS_DM_INTERNAL", 14)
                    self.smooth("SMOOTH_MINUS_DM_INTERNAL", "MINUS_DM_INTERNAL", 14)
                    for result, src in (
                        ("PLUS_DI14", "SMOOTH_PLUS_DM_INTERNAL"),
                        ("MINUS_DI14", "SMOOTH_MINUS_DM_INTERNAL"),
                    ):
                        self.combine(
                            result,
                            (src, "ATR14"),
                            lambda v: 100 * v[0] / v[1] if v[1] else Decimal(0),
                            unit="DIMENSIONLESS_0_100",
                        )
                    self.combine(
                        "DX_INTERNAL",
                        ("PLUS_DI14", "MINUS_DI14"),
                        lambda v: (
                            100 * abs(v[0] - v[1]) / (v[0] + v[1]) if v[0] + v[1] else Decimal(0)
                        ),
                        unit="DIMENSIONLESS_0_100",
                    )
                if name == "ADX14":
                    self.smooth("ADX14", "DX_INTERNAL", 14)
                    self.series[name] = self.series[name].model_copy(
                        update={"unit": "DIMENSIONLESS_0_100", "currency": None}
                    )


DEFAULT_PRECISION = PrecisionPolicy()


def calculate(
    source: MarketExtractionResult,
    request: TechnicalRequest,
    precision: PrecisionPolicy = DEFAULT_PRECISION,
    registry: tuple[IndicatorDefinition, ...] = REGISTRY,
) -> TechnicalSnapshot:
    now = datetime.now(UTC)
    issues = [
        market.issue("CORPORATE_ACTIONS_NOT_PROVIDED_BY_SOURCE"),
        market.issue("PINNED_RETRIEVAL_NOT_HISTORICAL_AVAILABILITY_PROOF"),
    ]
    rows: list[MarketObservation] = []
    basis = "UNKNOWN"
    try:
        market.validate_source(source, request.extraction_id, request.instrument_id)
        rows = market.select_range(source, request.start_date, request.end_date, issues)
        for pin, day in (
            (request.start_observation_id, request.start_date),
            (request.end_observation_id, request.end_date),
        ):
            if pin and not any(o.id == pin and o.trading_date == day for o in rows):
                raise market.MarketInputFailure("INCOMPATIBLE_SERIES", "STALE_OBSERVATION_ID")
        expected = source.request.expected_sessions
        if expected is None:
            issues.append(market.issue("OBSERVED_SESSIONS_ONLY"))
        if (
            expected is not None
            and set(d for d in expected if request.start_date <= d <= request.end_date)
            - {o.trading_date for o in rows}
        ) or any(
            request.start_date <= d <= request.end_date for d in source.coverage.missing_dates
        ):
            raise market.MarketInputFailure("INSUFFICIENT_INPUT", "EXPECTED_SESSION_MISSING")
        for o in rows:
            if (
                any(not v.is_finite() or v <= 0 for v in (o.open, o.high, o.low, o.close))
                or o.high < max(o.open, o.close, o.low)
                or o.low > min(o.open, o.close)
            ):
                raise market.MarketInputFailure("INVALID_INPUT", "INVALID_REQUIRED_OHLC", (o.id,))
            artifact = next(
                (a for a in source.raw_artifacts if a.id == o.provenance.raw_artifact_id), None
            )
            if (
                not artifact
                or not artifact.complete
                or not any(
                    r.id == artifact.retrieval_id and r.status == "success"
                    for r in source.retrievals
                )
            ):
                raise market.MarketInputFailure(
                    "INVALID_INPUT", "INPUT_EVIDENCE_UNAVAILABLE", (o.id,)
                )
        bases = {o.adjustment_basis for o in rows}
        if len(bases) != 1:
            raise market.MarketInputFailure("INCOMPATIBLE_SERIES", "MIXED_ADJUSTMENT_BASIS")
        basis = rows[0].adjustment_basis
        if basis == "UNKNOWN":
            issues.append(market.issue("UNVERIFIED_ADJUSTMENT"))
        assert source.instrument is not None
        with localcontext(Context(prec=precision.working_digits, rounding=ROUND_HALF_EVEN)):
            build = Builder(
                rows, request, source.instrument.price_unit, source.instrument.currency, registry
            )
            for name in request.indicators:
                build.ensure(name)
        series = tuple(build.series.values())
    except (market.MarketInputFailure, ArithmeticError) as exc:
        status = exc.status if isinstance(exc, market.MarketInputFailure) else "INVALID_INPUT"
        code = exc.code if isinstance(exc, market.MarketInputFailure) else "DECIMAL_RANGE_ERROR"
        issues.append(CalculationIssue(code=code, severity="ERROR", message=code))
        # Keep invalid originals in the source snapshot; failed output has no fabricated points.
        rows = []
        series = tuple(
            IndicatorSeries.model_validate(
                dict(
                    definition=next(d for d in registry if d.indicator_id == n),
                    status=status,
                    unit="DIMENSIONLESS_0_100"
                    if n in {"RSI14", "PLUS_DI14", "MINUS_DI14", "ADX14"}
                    else source.instrument.price_unit
                    if source.instrument
                    else "PRICE",
                    numerical_method="DECIMAL_RECURSIVE",
                    points=(),
                    requested=True,
                    issues=tuple(issues),
                )
            )
            for n in request.indicators
        )
    return TechnicalSnapshot.model_validate(
        dict(
            id=uuid4(),
            request=request,
            calculated_at=now,
            source=market.source_reference(source),
            selected_start=rows[0].trading_date if rows else None,
            selected_end=rows[-1].trading_date if rows else None,
            inputs=tuple(
                MarketCalculationInput(extraction_id=source.id, observation=o) for o in rows
            ),
            precision=precision,
            series=series,
            adjustment_basis=basis,
            issues=issues,
        )
    )
