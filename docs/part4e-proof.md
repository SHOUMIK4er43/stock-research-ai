# Part 4E implementation and proof

Verified locally: 444 full tests passed; targeted technical plus existing market formula
suite: 106 passed (64 new technical tests and 42 existing market formula tests).
Ruff passed. Mypy passed across 62 source files. Eight offline production CLI cases
passed all 996 comparisons against 483 distinct independent checkpoints. No dependencies,
lockfile, existing golden fixtures, database tables, or Git commits were changed.

## Definitions and initialization

All public definition and formula versions are **1**. Formula IDs equal the indicator
IDs below. Dates refer only to the declared synthetic range 2025-01-01..2025-03-05.
They are not claims about real exchange sessions.

| Indicator ID | First observation | First date | V1 initialization |
|---|---:|---|---|
| EMA20 | 20 | 2025-01-20 | SMA of closes 1..20; alpha 2/21 |
| RSI14 | 15 | 2025-01-15 | Mean gains/losses from changes 2..15; Wilder14 |
| MACD12_26 | 26 | 2025-01-26 | Separately SMA-seeded EMA12 minus EMA26 |
| MACD_SIGNAL9 | 34 | 2025-02-03 | Mean MACD26..34; then EMA9 |
| MACD_HISTOGRAM | 34 | 2025-02-03 | Working MACD minus working signal |
| TRUE_RANGE | 2 | 2025-01-02 | Max(high-low, abs(high-prevclose), abs(low-prevclose)) |
| ATR14 | 15 | 2025-01-15 | Mean TR2..15; then Wilder14 |
| BOLLINGER_MIDDLE20 | 20 | 2025-01-20 | Mean of 20 closes |
| BOLLINGER_UPPER20_2 | 20 | 2025-01-20 | Mean + 2 sqrt(population variance) |
| BOLLINGER_LOWER20_2 | 20 | 2025-01-20 | Mean - 2 sqrt(population variance) |
| PLUS_DI14 | 15 | 2025-01-15 | 100 smoothed +DM / smoothed TR |
| MINUS_DI14 | 15 | 2025-01-15 | 100 smoothed -DM / smoothed TR |
| ADX14 | 28 | 2025-01-28 | Mean DX15..28; first recursion at 29 |

The explicit request is the seed universe (`REQUEST_RANGE_ONLY_V1`). Earlier source
observations never seed these calculations. Public parameters cannot be customized.
Price-valued outputs use PRICE for equities and INDEX_POINTS for indices; RSI, DI and
ADX use DIMENSIONLESS_0_100. Variance audit values use squared price/index units.

## Every independent checkpoint

The complete local replay report is
[checkpoints.json](../output/part4e-final-proof/checkpoints.json). **Every record** contains
scenario, instrument, indicator, one-based observation, date, independently expected
value, actual production value, numerical method, literal tolerance and PASS/FAIL.
All 996 are PASS. The separately retained [summary](../output/part4e-final-proof/summary.json)
contains each snapshot/source identity, first dates, hash and history checks.

The 483 immutable unique expectations live in `tests/fixtures/technicals/expected.json`;
source inputs are separate in `inputs.json`. The fixture README and independent
reference explain the exact Fraction and 100-digit Newton derivation. No production
formula creates expected values. Checkpoints with terminating rational values compare
exactly (tolerance zero); repeating fractions and square roots use absolute `1e-45`
tolerance solely for finite Decimal storage/working precision. Production does not
claim its recursive values are exact rational numbers.

Specific initialization and edge proofs:

- RSI first at 15 and recursion at 16: mixed numerical checkpoints plus all-rising 100,
  all-falling 0 and flat 50 (`FLAT_RSI_MIDPOINT_V1`). Flat is an internal convention.
- MACD: separate EMA12/26 seeds, first difference at 26, nine-point signal seed at 34,
  histogram at 34, recursion at 35 and terminal outputs. Signal never starts at zero.
- Mixed gap-up at 21 has TR=12; gap-down at 22 has TR=15. First observation has no TR.
- ATR seed covers TR2..15, with independently checked recursion at 16 and terminal.
- Linear variance is exactly 33.25; mean, independently computed square root and both
  bands are checked at initialization and terminal. Negative synthetic lower band is valid.
- Mixed +DM/-DM, smoothed TR and DM, +DI/-DI/DX are independently checked for each
  observation 15..29. Initial ADX at 28 is the mean of DX15..28, approximately
  60.84087663098446936480604407789784253049446693920386. First recursion at 29 is
  approximately 57.96440993282412276789720997761446396535127480342916.
  Full independent and actual values are in the checkpoint report, not these display excerpts.
- Equal candidates at observation 31 are both 2, producing +DM=−DM=0. Flat positive
  OHLC gives zero TR/ATR/DI/DX/ADX. Zero denominators are explicitly handled.

## Validation and precision

Tests cover insufficient terminal history for every public indicator; duplicate and
unordered dates; a known missing session; absent session metadata; stale observation
IDs; wrong instrument/extraction; zero, negative, NaN and infinite OHLC; all high/low
consistency violations; rising/falling/flat cases; and a negative derived lower band.
No source prices are repaired and no missing sessions are filled. Invalid required
candles produce INVALID_INPUT; known gaps produce INSUFFICIENT_INPUT. Wrong extraction
identity is rejected. Without session metadata, otherwise valid outputs retain
OBSERVED_SESSIONS_ONLY.

Warm-up points remain INSUFFICIENT_INPUT while valid terminal series are AVAILABLE.
Both available and insufficient cases run through the real CLI, and latest.json
matches the exact requested terminal date without substitution.

Production recursive state remains at 70 digits. Stored values use 50 significant
digits and display uses two-place HALF_EVEN. Tests compare 70- and 80-digit computation,
isolate hostile ambient Decimal settings, and run 4,000 observations to verify bounded
Decimal state. They explicitly prove working state differs from the rounded stored
value, and recursive points do not duplicate complete candle histories.

## Lineage, compatibility and immutability

All four synthetic instruments (YESBANK, INFY, NIFTY 50, NIFTY BANK) pass acquisition,
normalization, calculation, export and retrieval. Original artifact bytes and canonical
market extraction payloads pass SHA-256 checks. Every selected observation retains its
actual Part 3B provenance; derived points create no raw locator.

Snapshot graph validation checks unique point and observation identities, ordered dates,
selected endpoints, matching definitions, resolvable dependencies, nonfuture inputs and
valid same-series predecessors. Tests corrupt each reference category and require refusal.
EMA seeds reference 20 observations, signal seeds nine MACD points and ADX seeds 14 DX
points. Intermediates remain in the snapshot, without additional rows.

Raw tampering is refused before another snapshot is saved. Duplicate snapshot inserts
fail. Definition, parameter, precision, requested range and changed raw-source revisions
append distinct snapshots; previously saved JSON reconstructs unchanged. Parameter
revision is injected privately in a test and does not add a public configuration.

Persistence reuses `market_calculation_snapshots`; **no DDL or new table**. Tests decode
a retained legacy Part 4B payload, mix legacy-unwrapped and new-enveloped scalar rows
with technical rows, and verify scalar history never returns technical objects. Legacy
stored payload bytes are not rewritten. Technical history is separately typed. Existing
Part 4B formula and pipeline tests pass in the full regression suite.

Every successful synthetic proof retains adjustment_basis=UNKNOWN and
UNVERIFIED_ADJUSTMENT, plus corporate actions UNAVAILABLE / NOT_PROVIDED_BY_SOURCE.
These limitations do not turn valid mathematics into unavailable outputs. Provider
retrieval time does not establish historical availability of the source value.

## Reproduce

From the repository root, select a new proof output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run python scripts/verify_technicals.py --output output/part4e-proof-manual
```

The script invokes the production market and technical CLIs; the exact commands are
also saved in `commands.json`. It uses only synthetic local evidence. No credentials,
network, real provider response capture or charting is required.

Required final checks executed successfully:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

Results: **444 passed in 83.37s; Ruff all checks passed; mypy no issues in 62 source files**.
The uv commands required sandbox permission to read the pre-existing external cache;
no dependency synchronization was enabled.

## Files and remaining limits

Created: domain/technicals.py, application/technicals.py, scripts/verify_technicals.py,
two technical test modules, the separate technical fixture directory, and this report.
Modified: CLI, bootstrap, ResearchEngine, ports, SQLite payload dispatch, JSON export,
shared market helpers, README and architecture documentation. Full paths appear in
`git status --short`; all changes remain uncommitted on main.

This slice intentionally has fixed V1 periods, request-range-only seeds, no inferred
exchange calendar, no adjustment reconstruction and no live technical-value comparison
to a third-party platform. Synthetic proof tests deterministic mathematics and plumbing,
not price accuracy of a live provider. There are no signals, labels, recommendations,
valuation, AI, dependency additions or historical-row migrations.

Final Git state: branch `main`, HEAD `d6f786f` (unchanged). Nine previously tracked
files are modified, and eleven new files are untracked, including five files in the
separate technical fixture directory. No changes are staged or committed.

```text
 M README.md
 M docs/architecture.md
 M src/stock_research_ai/__main__.py
 M src/stock_research_ai/adapters/json_export.py
 M src/stock_research_ai/adapters/sqlite.py
 M src/stock_research_ai/application/engine.py
 M src/stock_research_ai/application/market_calculations.py
 M src/stock_research_ai/application/ports.py
 M src/stock_research_ai/bootstrap.py
?? docs/part4e-proof.md
?? scripts/verify_technicals.py
?? src/stock_research_ai/application/technicals.py
?? src/stock_research_ai/domain/technicals.py
?? tests/fixtures/technicals/README.md
?? tests/fixtures/technicals/derive_reference.py
?? tests/fixtures/technicals/expected.json
?? tests/fixtures/technicals/inputs.json
?? tests/fixtures/technicals/legacy_4b_snapshot.json
?? tests/test_technical_pipeline.py
?? tests/test_technicals.py
```
