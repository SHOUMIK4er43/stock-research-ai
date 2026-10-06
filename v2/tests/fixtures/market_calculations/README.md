# Independent synthetic market calculation goldens

`inputs.json` contains artificial market inputs. Test fixture code wraps those literal closes in
the documented Kite candle envelope with O=H=L=C and synthetic volume; no production calculator
creates inputs or expected outputs. Original synthetic instrument-master evidence is reused.
Dates are explicitly declared fixture sessions, not a claim about an NSE calendar.

`expected.json` was written before the production formulas. Hand arithmetic:

- YESBANK return: (120 - 100)/100 * 100 = 20; INFY: (69 - 50)/50 * 100 = 38.
- YESBANK SMA20: (100 + 120 + 90 + 17*120)/20 = 2350/20 = 117.5.
  (There are 18 closes of 120 in total.) INFY SMA20: (50+69)/2 = 59.5.
- Drawdown: peak 120 on Jan 3, trough 90 on Jan 6: 25%. Recovery Jan 7 is
  exactly 120, proving >= rather than >. Falling 100 to 70: 30%, unrecovered.
- Benchmark returns: (1100-1000)/1000 = 10%; (2100-2000)/2000 = 5%.
  Relative returns: 20-10 = 10 percentage points and 20-5 = 15 percentage points.
- Nineteen YESBANK arithmetic returns: 1/5, -1/4, 1/3, then sixteen zeroes.
  Sum = 17/60; sum of squares = 769/3600; mean = 17/1140.
  Sample variance = (769/3600 - (17/60)^2/19)/18 = 2387/205200.
  Annual volatility squared (percent units) = variance * 252 * 10000 = 1670900/57.
- CAGR Jan 3 2023 to Jan 2 2025: 730 elapsed days / 365 = 2 convention-years.
  Result = (sqrt(6/5)-1)*100.

Irrational golden decimals were independently checked before production implementation using
80-digit Newton iteration on these hand-derived ratios (not production ln/exp/sqrt). Absolute
1e-40 tolerances accommodate a 50-digit recorded result; do not widen them to make tests pass.
Exact rational proofs require Decimal equality. The expected JSON is literal, not generated
from a calculation result. Existing real and synthetic Part 3B goldens remain unchanged.
