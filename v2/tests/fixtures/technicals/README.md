# Independent Part 4E technical goldens

`inputs.json` contains five separate 64-observation synthetic OHLCV histories.
`expected.json` contains 483 literal independently derived checkpoints. Neither file
imports or calls the production implementation. These expectations were authored
before production indicator code and have not been adjusted to match its output.

`derive_reference.py` records that independent derivation. It is not imported by
pytest or production. Do not run it to repair a failing test: golden changes require
source evidence and explicit approval under AGENTS.md. The legacy 4B payload is a
separate serialization compatibility fixture copied from an existing proof export.
No original Part 3B or Part 4B fixtures are changed.

The dates 2025-01-01 through 2025-03-05 are **declared synthetic sessions**, including
weekends. They do not claim to represent an NSE calendar or real historical prices.
The mixed series has nontrivial high/low ranges, gap up/down, rising/falling segments,
and equal directional-movement candidates. Flat, rising, falling and linear series
separately establish edge conventions and simple closed-form identities.

## Independent mathematics

The reference uses exact Fraction state for its short, offline histories. EMA is
written as `((N-1)*previous + 2*current)/(N+1)`, independently of production's Decimal
alpha formulation. Wilder smoothing uses the literal sum-over-period recurrence.
RSI uses `100*average_gain/(average_gain+average_loss)` with separately stated zero
cases. Population variance uses `mean(x*x)-mean(x)**2`, independently of production's
mean squared deviations. Square roots use Newton iteration at 100 significant digits,
not production's Decimal square-root helper.

Literal numerator/denominator values accompany rational expectations. A zero tolerance
requires exact equality for terminating Decimal reference values. Repeating rational
and square-root checkpoints have absolute tolerance `1e-45`: this covers rounding to
50 significant stored digits at these fixture magnitudes, while testing the 70-digit
recursive state against higher-precision independent truth. It is not a price/display
rounding tolerance and must not be loosened to accommodate an implementation failure.

Useful direct input checks:

- Linear closes are 101..164. EMA20 seed = mean(101..120) = 110.5;
  observation 21 = 111.5; terminal = 154.5.
- Linear EMA12 seed = 106.5; EMA26 seed = 113.5. MACD = 7 from observation 26;
  signal first = mean(nine sevens) = 7 at 34; histogram = 0.
- Linear 20-close population variance = (20²−1)/12 = 33.25. The first mean is
  110.5 and bands are 110.5 ± 2√33.25. This is not annualized volatility.
- Mixed observation 21: prior close 120, high 132, low 128. TR=max(4,12,8)=12.
  Observation 22: prior close 129, high 117, low 114. TR=max(3,12,15)=15.
- Mixed observation 31: prior high/low 120/116, current 122/114. Both movement
  candidates equal 2, so +DM and −DM are both zero.
- Linear TR and ATR = 2, smoothed +DM = 1, −DM = 0, +DI = 50, −DI = 0,
  DX and ADX = 100 after their respective initialization periods.
- Rising/falling/flat RSI = 100/0/50. Flat RSI names the internal convention
  `FLAT_RSI_MIDPOINT_V1`. Identical positive OHLC gives zero TR/ATR/DI/DX/ADX.

The mixed reference retains each initial DX from observations 15..28, so initial
ADX at 28 is independently checkable as their mean. Observation 29 checks the first
Wilder recursion. All public series include seed, first recursion and terminal
checkpoints; the audit series supply EMA, gain/loss, DM, variance and square-root proof.
