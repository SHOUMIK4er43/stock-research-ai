"""Independent mathematical reference, authored BEFORE production technical code.

Not imported by production or pytest. Explicit opt-in writes reference files once.
Fraction recurrences and 100-digit Newton square root independently verify Decimal V1.
"""

import json
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction as F
from pathlib import Path

ROOT = Path(__file__).parent


def build_inputs():
    result = {}
    for name in ("mixed", "linear", "rising", "falling", "flat"):
        rows = []
        for i in range(64):
            c = 100 + i + 1 if name != "falling" else 200 - i
            if name == "flat":
                c = 100
            if name == "mixed" and i >= 20:
                c = 121 + [8, -5, 18, -12, 3, 0, -7, 9][(i - 20) % 8] + (i - 20) // 8
            h, candle_low = c + 1, c - 1
            if name == "mixed":
                h, candle_low = c + 1 + i % 3, c - 1 - (i + 1) % 3
            if name == "flat":
                h = candle_low = c
            if name == "mixed" and i == 30:
                h, candle_low = rows[-1][2] + 2, rows[-1][3] - 2
                c = (h + candle_low) // 2
            rows.append(
                [(date(2025, 1, 1) + timedelta(days=i)).isoformat(), c, h, candle_low, c, 1000 + i]
            )
        result[name] = rows
    return result


def reference(rows):
    close = [F(r[4]) for r in rows]
    high = [F(r[2]) for r in rows]
    low = [F(r[3]) for r in rows]
    size = len(rows)
    values = {}

    def smooth(name, source, n, ema=False):
        available = [i for i, v in enumerate(source) if v is not None]
        out = [None] * size
        if len(available) >= n:
            first = available[n - 1]
            out[first] = sum(source[i] for i in available[:n]) / n
            for i in available[n:]:
                out[i] = (
                    (out[i - 1] * (n - 1) + source[i] * (2 if ema else 1)) / (n + 1 if ema else n)
                    if ema
                    else (out[i - 1] * (n - 1) + source[i]) / n
                )
        values[name] = out
        return out

    # EMA algebra: (N-1)*previous/(N+1) + 2*current/(N+1).
    smooth("EMA20", close, 20, True)
    e12 = smooth("EMA12_INTERNAL", close, 12, True)
    e26 = smooth("EMA26_INTERNAL", close, 26, True)
    macd = [a - b if b is not None else None for a, b in zip(e12, e26, strict=True)]
    values["MACD12_26"] = macd
    signal = smooth("MACD_SIGNAL9", macd, 9, True)
    values["MACD_HISTOGRAM"] = [
        a - b if b is not None else None for a, b in zip(macd, signal, strict=True)
    ]
    gains = [None] + [max(b - a, F(0)) for a, b in zip(close, close[1:], strict=False)]
    losses = [None] + [max(a - b, F(0)) for a, b in zip(close, close[1:], strict=False)]
    values["GAIN_INTERNAL"] = gains
    values["LOSS_INTERNAL"] = losses
    ag = smooth("AVG_GAIN_INTERNAL", gains, 14)
    al = smooth("AVG_LOSS_INTERNAL", losses, 14)
    values["RSI14"] = [
        None if a is None else F(50) if a == b == 0 else F(100) if b == 0 else 100 * a / (a + b)
        for a, b in zip(ag, al, strict=True)
    ]
    tr = [None]
    plus = [None]
    minus = [None]
    for i in range(1, size):
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
        u, d = high[i] - high[i - 1], low[i - 1] - low[i]
        plus.append(u if u > 0 and u > d else F(0))
        minus.append(d if d > 0 and d > u else F(0))
    values.update(TRUE_RANGE=tr, PLUS_DM_INTERNAL=plus, MINUS_DM_INTERNAL=minus)
    atr = smooth("ATR14", tr, 14)
    ap = smooth("SMOOTH_PLUS_DM_INTERNAL", plus, 14)
    am = smooth("SMOOTH_MINUS_DM_INTERNAL", minus, 14)
    pd = [None if t is None else 100 * p / t if t else F(0) for p, t in zip(ap, atr, strict=True)]
    md = [None if t is None else 100 * m / t if t else F(0) for m, t in zip(am, atr, strict=True)]
    dx = [
        None if p is None else 100 * abs(p - m) / (p + m) if p + m else F(0)
        for p, m in zip(pd, md, strict=True)
    ]
    values.update(PLUS_DI14=pd, MINUS_DI14=md, DX_INTERNAL=dx)
    smooth("ADX14", dx, 14)
    middle = [None] * size
    variance = [None] * size
    std = [None] * size
    upper = [None] * size
    lower = [None] * size
    for i in range(19, size):
        window = close[i - 19 : i + 1]
        mean = sum(window) / 20
        v = sum(x * x for x in window) / 20 - mean * mean
        middle[i] = mean
        variance[i] = v
        with localcontext() as ctx:
            ctx.prec = 100
            number = Decimal(v.numerator) / Decimal(v.denominator)
            x = max(number, Decimal(1))
            if number:
                for _ in range(400):
                    y = (x + number / x) / 2
                    if x == y:
                        break
                    x = y
            else:
                x = Decimal(0)
            std[i] = x
            upper[i] = Decimal(mean.numerator) / Decimal(mean.denominator) + 2 * x
            lower[i] = Decimal(mean.numerator) / Decimal(mean.denominator) - 2 * x
    values.update(
        BOLLINGER_MIDDLE20=middle,
        VARIANCE_INTERNAL=variance,
        STDDEV_INTERNAL=std,
        BOLLINGER_UPPER20_2=upper,
        BOLLINGER_LOWER20_2=lower,
    )
    return values


def main():
    inputs = build_inputs()
    (ROOT / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    golden = []
    for scenario, rows in inputs.items():
        for name, series in reference(rows).items():
            available = [i for i, v in enumerate(series) if v is not None]
            checkpoints = {available[0], available[0] + 1, 63}
            if scenario == "mixed" and name in {
                "DX_INTERNAL",
                "PLUS_DI14",
                "MINUS_DI14",
                "ADX14",
                "ATR14",
                "SMOOTH_PLUS_DM_INTERNAL",
                "SMOOTH_MINUS_DM_INTERNAL",
            }:
                checkpoints.update(range(14, 29))
            if scenario == "mixed" and name in {
                "TRUE_RANGE",
                "PLUS_DM_INTERNAL",
                "MINUS_DM_INTERNAL",
            }:
                checkpoints.update([20, 21, 22, 23, 30])
            for i in sorted(checkpoints):
                v = series[i]
                if v is None:
                    continue
                with localcontext() as ctx:
                    ctx.prec = 100
                    decimal = (
                        Decimal(v.numerator) / Decimal(v.denominator) if isinstance(v, F) else v
                    )
                    full = str(decimal)
                finite = False
                if isinstance(v, F):
                    d = v.denominator
                    for factor in (2, 5):
                        while d % factor == 0:
                            d //= factor
                    finite = d == 1
                golden.append(
                    dict(
                        scenario=scenario,
                        indicator=name,
                        observation=i + 1,
                        date=rows[i][0],
                        expected=full,
                        numerator=str(v.numerator) if isinstance(v, F) else None,
                        denominator=str(v.denominator) if isinstance(v, F) else None,
                        tolerance="0" if finite else "1e-45",
                    )
                )
    (ROOT / "expected.json").write_text(json.dumps(golden, indent=2) + "\n")


if __name__ == "__main__":
    main()
