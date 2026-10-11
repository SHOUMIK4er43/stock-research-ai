"""Box 12 market data: Kite quotes and daily candles -> dated snapshot, history, statistics.

Every number is recomputed here from raw dated candles (never copied from a third party).
Methods, all exchange-local (IST) and Decimal at 50 digits, outputs quantized to 6 places:
  returns       end = latest candle; start = last candle on or before (end - period), where
                1D is the previous session and months are calendar months; a start more than
                7 days before its target is a gap -> UNAVAILABLE. Price-only (no dividends).
  CAGR          (end/start)^(365/elapsed_days) - 1, for periods of one year or more
  52-week       candle highs/lows over [end - 364 days, end]; requires history from the window
                start; distances = last close / extreme - 1
  drawdown      close-based peak-to-trough, worst (earliest on ties), 1Y and full history
  volatility    sample stdev of simple daily close returns over the last 365 days x sqrt(252),
                at least 200 returns
  liquidity     mean volume and mean(close x volume) over the last 20 / 252 sessions; the
                traded value is an approximation (no VWAP) and is labelled so
  benchmarks    both series on their common sessions with the same anchors; excess = c - b
                (percentage points), relative = (1+c)/(1+b) - 1
  beta          BetaMethodology: daily simple returns on sessions consecutive in both series,
                365-day lookback, >= 200 observations, sample cov / sample var
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any
from uuid import UUID

from stock_research_ai.application.exchange_intelligence import (
    decode_json,
    issue,
    parse_number,
    uid,
)
from stock_research_ai.domain.market_intelligence import (
    CRORE,
    IST,
    MAX_CANDLES,
    MAX_PRICE,
    MAX_VOLUME,
    Authority,
    BenchmarkComparison,
    BenchmarkRole,
    BetaEstimate,
    BetaMethodology,
    Candle,
    Freshness,
    InstrumentRef,
    Issue,
    MarketSnapshot,
    MarketStatistic,
    MetricStatus,
    PeriodReturn,
    PriceHistory,
    Provider,
    RawPayload,
    ReturnPeriod,
    StatisticKind,
)

QUANTUM = Decimal("0.000001")
GAP_TOLERANCE_DAYS = 7
CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_PERIODS: dict[ReturnPeriod, tuple[str, int]] = {
    ReturnPeriod.D1: ("SESSION", 1),
    ReturnPeriod.W1: ("DAYS", 7),
    ReturnPeriod.M1: ("MONTHS", 1),
    ReturnPeriod.M3: ("MONTHS", 3),
    ReturnPeriod.M6: ("MONTHS", 6),
    ReturnPeriod.Y1: ("MONTHS", 12),
    ReturnPeriod.Y3: ("MONTHS", 36),
    ReturnPeriod.Y5: ("MONTHS", 60),
    ReturnPeriod.Y10: ("MONTHS", 120),
}
_ANNUAL = {ReturnPeriod.Y1, ReturnPeriod.Y3, ReturnPeriod.Y5, ReturnPeriod.Y10}
RETURN_METHOD = "PRICE_RETURN_LAST_CANDLE_ON_OR_BEFORE_TARGET_GAP<=7D; CAGR_ACTUAL_365"


def q(value: Decimal) -> Decimal:
    with localcontext(CONTEXT):
        return value.quantize(QUANTUM)


def months_before(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 - months
    year, month = divmod(total, 12)
    month += 1
    for candidate in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    raise ValueError("unreachable")


def _price(value: object) -> Decimal | None:
    number = parse_number(value, maximum=MAX_PRICE)
    return number if number is not None and number > 0 else None


def _naive_ist(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    elif parsed.utcoffset() != timedelta(hours=5, minutes=30):
        return None
    return parsed if 1990 <= parsed.year <= 2100 else None


def freshness(
    stamp: datetime | None, as_of: datetime, current_seconds: int, stale_days: int
) -> tuple[Freshness, int | None]:
    if stamp is None:
        return Freshness.UNKNOWN, None
    age = int((as_of - stamp).total_seconds())
    if age < -300:  # from the future: clock or payload is wrong
        return Freshness.UNKNOWN, None
    age = max(age, 0)
    if age <= current_seconds:
        return Freshness.CURRENT, age
    if age <= stale_days * 86_400:
        return Freshness.DELAYED, age
    return Freshness.STALE, age


# --- quote ----------------------------------------------------------------------------------


def parse_quote(
    payload: RawPayload,
    pid: UUID,
    key: str,
    instrument: InstrumentRef,
    *,
    as_of: datetime,
    current_seconds: int,
    stale_days: int,
    issues: list[Issue],
) -> MarketSnapshot | None:
    body = decode_json(payload, pid, issues)
    if body is None:
        return None
    data = body.get("data") if isinstance(body, dict) and body.get("status") == "success" else None
    entry = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        issues.append(issue("QUOTE_INSTRUMENT_MISSING", "Quote response lacks the instrument",
                            severity="ERROR", payload=pid))
        return None
    own: list[Issue] = []
    last = _price(entry.get("last_price"))
    if last is None:
        issues.append(issue("INVALID_QUOTE", "Quote has no positive last price",
                            severity="ERROR", payload=pid))
        return None
    token = entry.get("instrument_token")
    if (
        instrument.instrument_token is not None
        and isinstance(token, int)
        and str(token) != instrument.instrument_token
    ):
        issues.append(issue("QUOTE_TOKEN_MISMATCH", "Quote token differs from identity",
                            severity="ERROR", payload=pid))
        return None
    ohlc = entry.get("ohlc") if isinstance(entry.get("ohlc"), dict) else {}
    assert isinstance(ohlc, dict)
    fields: dict[str, Decimal | None] = {}
    for name, raw in (("open", ohlc.get("open")), ("high", ohlc.get("high")),
                      ("low", ohlc.get("low")), ("previous_close", ohlc.get("close")),
                      ("average_price", entry.get("average_price"))):
        fields[name] = _price(raw)
        if raw is not None and fields[name] is None and parse_number(raw) == 0:
            own.append(issue("ZERO_TREATED_AS_UNAVAILABLE", f"Provider sent 0 for {name}",
                             severity="INFO", payload=pid))
    volume = parse_number(entry.get("volume"), maximum=MAX_VOLUME)
    if volume is not None and volume < 0:
        volume = None
    if instrument.kind == "INDEX":
        volume = None
    bid = ask = None
    depth_available = False
    depth = entry.get("depth")
    if isinstance(depth, dict):
        buys, sells = depth.get("buy"), depth.get("sell")
        if isinstance(buys, list) and buys and isinstance(buys[0], dict):
            bid = _price(buys[0].get("price"))
        if isinstance(sells, list) and sells and isinstance(sells[0], dict):
            ask = _price(sells[0].get("price"))
        depth_available = any(
            isinstance(level, dict) and (parse_number(level.get("quantity")) or 0) > 0
            for side in (buys, sells) if isinstance(side, list) for level in side[:5]
        )
    high, low = fields["high"], fields["low"]
    if high is not None and low is not None and high < low:
        own.append(issue("INCONSISTENT_OHLC", "High below low; range dropped", payload=pid))
        fields["high"] = fields["low"] = None
    exchange_ts = _naive_ist(entry.get("timestamp"))
    trade_ts = _naive_ist(entry.get("last_trade_time"))
    stamp = exchange_ts or trade_ts
    state, age = freshness(stamp, as_of, current_seconds, stale_days)
    if state == Freshness.STALE:
        own.append(issue("STALE_QUOTE", "Quote is older than the stale bound; not current",
                         payload=pid))
    if state == Freshness.UNKNOWN:
        own.append(issue("QUOTE_TIME_UNKNOWN", "Quote has no usable exchange timestamp",
                         payload=pid))
    return MarketSnapshot(
        snapshot_id=uid("snapshot", pid, key),
        instrument=instrument,
        last_price=last,
        previous_close=fields["previous_close"],
        open=fields["open"],
        high=fields["high"],
        low=fields["low"],
        volume=volume,
        average_price=fields["average_price"],
        bid=bid,
        ask=ask,
        depth_available=depth_available,
        last_trade_time=trade_ts,
        exchange_timestamp=exchange_ts,
        observed_at=payload.retrieved_at,
        market_date=stamp.astimezone(IST).date() if stamp else None,
        freshness=state,
        age_seconds=age,
        source=Provider.KITE,
        authority=Authority.MARKET_PROVIDER,
        payload_id=pid,
        locator=f"$.data[{key!r}]",
        issues=tuple(own),
    )


# --- history ---------------------------------------------------------------------------------


def parse_history(
    payloads: Sequence[tuple[RawPayload, UUID]],
    instrument: InstrumentRef,
    *,
    as_of: datetime,
    stale_days: int,
) -> PriceHistory:
    issues: list[Issue] = []
    candles: dict[date, Candle] = {}
    conflicts: set[date] = set()
    used: list[UUID] = []
    for payload, pid in payloads:
        body = decode_json(payload, pid, issues)
        if body is None:
            continue
        rows = (
            body.get("data", {}).get("candles")
            if isinstance(body, dict) and body.get("status") == "success"
            and isinstance(body.get("data"), dict) else None
        )
        if not isinstance(rows, list):
            issues.append(issue("INVALID_SOURCE_DATA", "Historical response has no candle list",
                                severity="ERROR", payload=pid))
            continue
        used.append(pid)
        bad = 0
        for i, row in enumerate(rows[: MAX_CANDLES * 2]):
            candle = _candle(row, pid, f"$.data.candles[{i}]", instrument)
            if candle is None:
                bad += 1
                continue
            day = candle.trading_date
            if day in conflicts:
                continue
            prior = candles.get(day)
            if prior is not None and (prior.open, prior.high, prior.low, prior.close,
                                      prior.volume) != (candle.open, candle.high, candle.low,
                                                        candle.close, candle.volume):
                conflicts.add(day)
                del candles[day]
                continue
            if prior is None:
                candles[day] = candle
        if len(rows) > MAX_CANDLES * 2:
            issues.append(issue("HISTORY_TRUNCATED", "Response exceeded the candle bound",
                                severity="ERROR", payload=pid))
        if bad:
            issues.append(issue("INVALID_CANDLE", f"{bad} malformed candles excluded",
                                payload=pid))
    if conflicts:
        issues.append(issue("CONFLICTING_DUPLICATE_CANDLE",
                            f"{len(conflicts)} dates had conflicting candles; all excluded",
                            severity="ERROR"))
    ordered = [candles[d] for d in sorted(candles)]
    if len(ordered) > MAX_CANDLES:
        ordered = ordered[-MAX_CANDLES:]
        issues.append(issue("HISTORY_TRUNCATED", "Only the latest candles were kept",
                            severity="ERROR"))
    today = as_of.astimezone(IST).date()
    future = [c for c in ordered if c.trading_date > today]
    if future:
        issues.append(issue("FUTURE_CANDLE", "Candles dated after as_of excluded",
                            severity="ERROR"))
        ordered = [c for c in ordered if c.trading_date <= today]
    lag = (today - ordered[-1].trading_date).days if ordered else None
    state = (
        Freshness.UNKNOWN if lag is None
        else Freshness.CURRENT if lag <= stale_days else Freshness.STALE
    )
    if state == Freshness.STALE:
        issues.append(issue("STALE_HISTORY", "Latest candle is older than the stale bound"))
    if not ordered:
        issues.append(issue("NO_HISTORY", "No valid candles; nothing is calculated",
                            severity="ERROR"))
    return PriceHistory(
        history_id=uid("history", instrument.exchange, instrument.symbol, *sorted(map(str, used))),
        instrument=instrument,
        candles=tuple(ordered),
        earliest=ordered[0].trading_date if ordered else None,
        latest=ordered[-1].trading_date if ordered else None,
        freshness=state,
        lag_days=lag,
        payload_ids=tuple(sorted(set(used), key=str)),
        issues=tuple(issues),
    )


def _candle(row: object, pid: UUID, locator: str, instrument: InstrumentRef) -> Candle | None:
    if not isinstance(row, list) or len(row) not in (5, 6, 7):
        return None
    stamp = _naive_ist(row[0]) if isinstance(row[0], str) else None
    if stamp is None or stamp.utcoffset() is None or (stamp.hour, stamp.minute, stamp.second) != (
        0, 0, 0
    ):
        return None
    values = [_price(v) for v in row[1:5]]
    if any(v is None for v in values):
        return None
    volume = parse_number(row[5], maximum=MAX_VOLUME) if len(row) > 5 else None
    if volume is not None and volume < 0:
        return None
    try:
        return Candle(
            trading_date=stamp.date(),
            open=values[0], high=values[1], low=values[2], close=values[3],  # type: ignore[arg-type]
            volume=None if instrument.kind == "INDEX" else volume,
            payload_id=pid,
            locator=locator,
        )
    except ValueError:
        return None


# --- statistics ------------------------------------------------------------------------------


def _start_for(candles: Sequence[Candle], end: Candle, period: ReturnPeriod) -> tuple[
    date | None, Candle | None, str | None
]:
    unit, amount = _PERIODS[period]
    if unit == "SESSION":
        index = len(candles) - 1
        return None, (candles[index - 1] if index >= 1 else None), (
            None if index >= 1 else "INSUFFICIENT_HISTORY"
        )
    target = end.trading_date - timedelta(days=amount) if unit == "DAYS" else months_before(
        end.trading_date, amount
    )
    eligible = [c for c in candles if c.trading_date <= target]
    if not eligible:
        return target, None, "INSUFFICIENT_HISTORY"
    start = eligible[-1]
    if (target - start.trading_date).days > GAP_TOLERANCE_DAYS:
        return target, None, "HISTORY_GAP_AT_START"
    return target, start, None


def _growth(end: Decimal, start: Decimal) -> Decimal:
    return (end / start - 1) * 100


def period_returns(history: PriceHistory) -> tuple[PeriodReturn, ...]:
    out = []
    candles = history.candles
    for period in ReturnPeriod:
        if not candles:
            out.append(PeriodReturn(period=period, status=MetricStatus.UNAVAILABLE,
                                    method=RETURN_METHOD, reason="NO_HISTORY"))
            continue
        end = candles[-1]
        target, start, reason = _start_for(candles, end, period)
        if start is None:
            out.append(PeriodReturn(period=period, status=MetricStatus.UNAVAILABLE,
                                    target_start=target, end_date=end.trading_date,
                                    method=RETURN_METHOD, reason=reason))
            continue
        days = (end.trading_date - start.trading_date).days
        with localcontext(CONTEXT):
            absolute = _growth(end.close, start.close)
            cagr = None
            if period in _ANNUAL and days > 0:
                cagr = ((end.close / start.close) ** (Decimal(365) / Decimal(days)) - 1) * 100
        out.append(
            PeriodReturn(
                period=period, status=MetricStatus.AVAILABLE, target_start=target,
                start_date=start.trading_date, end_date=end.trading_date,
                start_close=start.close, end_close=end.close, elapsed_days=days,
                absolute_return_pct=q(absolute), cagr_pct=q(cagr) if cagr is not None else None,
                method=RETURN_METHOD,
            )
        )
    return tuple(out)


def _unavailable(kind: StatisticKind, unit: str, method: str, reason: str) -> MarketStatistic:
    return MarketStatistic.model_validate(
        {"kind": kind, "status": "UNAVAILABLE", "unit": unit, "method": method, "reason": reason}
    )


def _drawdown(window: Sequence[Candle]) -> tuple[Decimal, date, date] | None:
    if len(window) < 2:
        return None
    peak = window[0]
    worst, worst_peak, worst_trough = Decimal(0), window[0], window[0]
    with localcontext(CONTEXT):
        for candle in window:
            if candle.close > peak.close:
                peak = candle
            loss = (1 - candle.close / peak.close) * 100
            if loss > worst:
                worst, worst_peak, worst_trough = loss, peak, candle
    return worst, worst_peak.trading_date, worst_trough.trading_date


def _sample_stdev(values: Sequence[Decimal]) -> Decimal:
    with localcontext(CONTEXT):
        n = Decimal(len(values))
        mean = sum(values, Decimal(0)) / n
        variance = sum(((v - mean) ** 2 for v in values), Decimal(0)) / (n - 1)
        return variance.sqrt()


def statistics(history: PriceHistory) -> tuple[MarketStatistic, ...]:
    candles = history.candles
    S = StatisticKind
    out: list[MarketStatistic] = []
    if not candles:
        units = {S.HIGH_52W: "PRICE", S.LOW_52W: "PRICE", S.AVERAGE_VOLUME_20D: "SHARES",
                 S.AVERAGE_VOLUME_252D: "SHARES", S.AVERAGE_TRADED_VALUE_20D: "INR_CRORE",
                 S.AVERAGE_TRADED_VALUE_252D: "INR_CRORE"}
        return tuple(_unavailable(k, units.get(k, "PERCENT"), "NO_HISTORY", "NO_HISTORY")
                     for k in S)
    end = candles[-1]
    start_52 = end.trading_date - timedelta(days=364)
    window = [c for c in candles if c.trading_date >= start_52]
    method_52 = "CANDLE_HIGH_LOW_OVER_[END-364D,END]"
    if candles[0].trading_date > start_52:
        for kind, unit in ((S.HIGH_52W, "PRICE"), (S.LOW_52W, "PRICE"),
                           (S.DISTANCE_FROM_HIGH_52W, "PERCENT"),
                           (S.DISTANCE_FROM_LOW_52W, "PERCENT")):
            out.append(_unavailable(kind, unit, method_52, "INSUFFICIENT_HISTORY"))
    else:
        high = max(window, key=lambda c: (c.high, -c.trading_date.toordinal()))
        low = min(window, key=lambda c: (c.low, c.trading_date.toordinal()))
        common = {"window_start": start_52, "window_end": end.trading_date,
                  "observations": len(window), "method": method_52, "status": "AVAILABLE"}
        with localcontext(CONTEXT):
            out += [
                MarketStatistic.model_validate({**common, "kind": S.HIGH_52W, "value": high.high,
                                                "unit": "PRICE",
                                                "reference_date": high.trading_date}),
                MarketStatistic.model_validate({**common, "kind": S.LOW_52W, "value": low.low,
                                                "unit": "PRICE",
                                                "reference_date": low.trading_date}),
                MarketStatistic.model_validate({**common, "kind": S.DISTANCE_FROM_HIGH_52W,
                                                "value": q(_growth(end.close, high.high)),
                                                "unit": "PERCENT",
                                                "reference_date": high.trading_date}),
                MarketStatistic.model_validate({**common, "kind": S.DISTANCE_FROM_LOW_52W,
                                                "value": q(_growth(end.close, low.low)),
                                                "unit": "PERCENT",
                                                "reference_date": low.trading_date}),
            ]
    year_start = end.trading_date - timedelta(days=365)
    year = [c for c in candles if c.trading_date >= year_start]
    for kind, rows, method, needs_full_year in (
        (S.MAX_DRAWDOWN_1Y, year, "CLOSE_PEAK_TO_TROUGH_LAST_365D", True),
        (S.MAX_DRAWDOWN_FULL_HISTORY, list(candles), "CLOSE_PEAK_TO_TROUGH_ALL_CANDLES", False),
    ):
        result = _drawdown(rows)
        if result is None or (needs_full_year and candles[0].trading_date > year_start):
            out.append(_unavailable(kind, "PERCENT", method, "INSUFFICIENT_HISTORY"))
            continue
        worst, peak_day, trough_day = result
        out.append(MarketStatistic(
            kind=kind, status=MetricStatus.AVAILABLE, value=q(worst), unit="PERCENT",
            window_start=rows[0].trading_date, window_end=end.trading_date,
            observations=len(rows), reference_date=peak_day, second_date=trough_day,
            method=method,
        ))
    vol_method = "SAMPLE_STDEV_SIMPLE_DAILY_RETURNS_LAST_365D_X_SQRT252; MIN_200"
    with localcontext(CONTEXT):
        returns = [b.close / a.close - 1 for a, b in zip(year, year[1:], strict=False)]
    if len(returns) < 200 or candles[0].trading_date > year_start:
        out.append(_unavailable(S.ANNUALIZED_VOLATILITY_1Y, "PERCENT", vol_method,
                                "INSUFFICIENT_OBSERVATIONS"))
    else:
        with localcontext(CONTEXT):
            vol = _sample_stdev(returns) * Decimal(252).sqrt() * 100
        out.append(MarketStatistic(
            kind=S.ANNUALIZED_VOLATILITY_1Y, status=MetricStatus.AVAILABLE, value=q(vol),
            unit="PERCENT", window_start=year[0].trading_date, window_end=end.trading_date,
            observations=len(returns), method=vol_method,
        ))
    for sessions, vol_kind, value_kind in ((20, S.AVERAGE_VOLUME_20D, S.AVERAGE_TRADED_VALUE_20D),
                                           (252, S.AVERAGE_VOLUME_252D,
                                            S.AVERAGE_TRADED_VALUE_252D)):
        last = list(candles[-sessions:])
        vmethod = f"MEAN_VOLUME_LAST_{sessions}_SESSIONS"
        tmethod = f"MEAN_CLOSE_X_VOLUME_LAST_{sessions}_SESSIONS_APPROXIMATION_NO_VWAP"
        if history.instrument.kind == "INDEX":
            out.append(_unavailable(vol_kind, "SHARES", vmethod, "VOLUME_NOT_APPLICABLE"))
            out.append(_unavailable(value_kind, "INR_CRORE", tmethod, "VOLUME_NOT_APPLICABLE"))
            continue
        if len(last) < sessions or any(c.volume is None for c in last):
            out.append(_unavailable(vol_kind, "SHARES", vmethod, "INSUFFICIENT_OBSERVATIONS"))
            out.append(_unavailable(value_kind, "INR_CRORE", tmethod,
                                    "INSUFFICIENT_OBSERVATIONS"))
            continue
        with localcontext(CONTEXT):
            volumes = [c.volume for c in last if c.volume is not None]
            mean_volume = sum(volumes, Decimal(0)) / sessions
            traded = sum((c.close * v for c, v in zip(last, volumes, strict=True)), Decimal(0))
            mean_value = traded / sessions / CRORE
        common = {"window_start": last[0].trading_date, "window_end": end.trading_date,
                  "observations": sessions, "status": "AVAILABLE"}
        out.append(MarketStatistic.model_validate({**common, "kind": vol_kind,
                                                   "value": q(mean_volume), "unit": "SHARES",
                                                   "method": vmethod}))
        out.append(MarketStatistic.model_validate({**common, "kind": value_kind,
                                                   "value": q(mean_value), "unit": "INR_CRORE",
                                                   "method": tmethod}))
    return tuple(out)


# --- benchmarks ---------------------------------------------------------------------------


def _common(company: PriceHistory, benchmark: PriceHistory) -> tuple[list[Candle], list[Candle]]:
    theirs = {c.trading_date: c for c in benchmark.candles}
    pairs = [(c, theirs[c.trading_date]) for c in company.candles if c.trading_date in theirs]
    return [a for a, _ in pairs], [b for _, b in pairs]


def compare(
    company: PriceHistory, benchmark: PriceHistory, role: BenchmarkRole
) -> tuple[BenchmarkComparison, ...]:
    mine, theirs = _common(company, benchmark)
    out = []
    for period in ReturnPeriod:
        base: dict[str, Any] = {"benchmark": benchmark.instrument, "role": role, "period": period}
        if len(mine) < 2:
            out.append(BenchmarkComparison(**base, status=MetricStatus.UNAVAILABLE,
                                           reason="NO_COMMON_HISTORY"))
            continue
        _, start, reason = _start_for(mine, mine[-1], period)
        if start is None:
            out.append(BenchmarkComparison(**base, status=MetricStatus.UNAVAILABLE,
                                           reason=reason))
            continue
        i = next(k for k, c in enumerate(mine) if c.trading_date == start.trading_date)
        with localcontext(CONTEXT):
            c = _growth(mine[-1].close, mine[i].close)
            b = _growth(theirs[-1].close, theirs[i].close)
            excess = c - b
            relative = ((1 + c / 100) / (1 + b / 100) - 1) * 100
        out.append(BenchmarkComparison(
            **base, status=MetricStatus.AVAILABLE, start_date=start.trading_date,
            end_date=mine[-1].trading_date, company_return_pct=q(c), benchmark_return_pct=q(b),
            excess_return_pp=q(excess), relative_performance_pct=q(relative),
        ))
    return tuple(out)


def beta(company: PriceHistory, benchmark: PriceHistory) -> BetaEstimate:
    method = BetaMethodology()
    base: dict[str, Any] = {"benchmark": benchmark.instrument, "methodology": method}
    mine, theirs = _common(company, benchmark)
    if not mine:
        return BetaEstimate(**base, status=MetricStatus.UNAVAILABLE, reason="NO_COMMON_HISTORY")
    end = mine[-1].trading_date
    start = end - timedelta(days=method.lookback_days)
    own_next = {a.trading_date: b.trading_date for a, b in
                zip(company.candles, company.candles[1:], strict=False)}
    bench_next = {a.trading_date: b.trading_date for a, b in
                  zip(benchmark.candles, benchmark.candles[1:], strict=False)}
    xs: list[Decimal] = []
    ys: list[Decimal] = []
    with localcontext(CONTEXT):
        for k in range(1, len(mine)):
            prev, cur = mine[k - 1].trading_date, mine[k].trading_date
            if prev < start or own_next.get(prev) != cur or bench_next.get(prev) != cur:
                continue
            ys.append(mine[k].close / mine[k - 1].close - 1)
            xs.append(theirs[k].close / theirs[k - 1].close - 1)
    if len(xs) < method.minimum_observations or (
        company.earliest is not None and company.earliest > start
    ):
        return BetaEstimate(**base, status=MetricStatus.UNAVAILABLE, observations=len(xs),
                            reason="INSUFFICIENT_OBSERVATIONS")
    with localcontext(CONTEXT):
        n = Decimal(len(xs))
        mx, my = sum(xs, Decimal(0)) / n, sum(ys, Decimal(0)) / n
        cov = sum(((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)), Decimal(0)) / (n - 1)
        var = sum(((x - mx) ** 2 for x in xs), Decimal(0)) / (n - 1)
    if var == 0:
        return BetaEstimate(**base, status=MetricStatus.UNAVAILABLE, observations=len(xs),
                            reason="ZERO_BENCHMARK_VARIANCE")
    with localcontext(CONTEXT):
        value = q(cov / var)
    return BetaEstimate(**base, status=MetricStatus.AVAILABLE, value=value,
                        observations=len(xs), start_date=start, end_date=end)
