# Part 4D review evidence

Scope: exactly six deterministic financial patterns. No extra pattern families, source-ratio reinterpretation, dependencies or commits.

## Six real-source goldens

All six passed through the real production CLI with network access blocked. Expected findings were authored separately from implementation; source inputs were checked independently against retained HTML. Exact fractions use no tolerance. Decimal representation is 50 significant digits, with separate two-place HALF_EVEN display.

### INFY — CFO_TO_NET_PROFIT

Expected: `AVAILABLE / OBSERVATION_ONLY`. Actual: `AVAILABLE / OBSERVATION_ONLY`. **PASS**.

- Expected exact: `29409/25568` RATIO_X.
- Actual exact (reduced): `29409/25568` — **PASS**.
- Expected Decimal: `1.1502268460575719649561952440550688360450563204005`.
- Actual Decimal: `1.1502268460575719649561952440550688360450563204005` — **PASS**.
- Expected display `1.15`; actual `1.15` — **PASS**.

Snapshot: `5aa5a0f3-b864-4253-b9aa-d7205440a7e3`. Raw SHA, extraction SHA and persistence: **PASS**.

### INFY — PROFIT_VS_CFO_DIVERGENCE

Expected: `AVAILABLE / TRIGGERED`. Actual: `AVAILABLE / TRIGGERED`. **PASS**.

This directional rule exposes the verified source quantities and conditions; it does not invent an aggregate numeric score.

Snapshot: `2cc52404-a59e-4d81-ba5a-b84112280add`. Raw SHA, extraction SHA and persistence: **PASS**.

### INFY — REPEATED_NEGATIVE_CFO

Expected: `AVAILABLE / NOT_TRIGGERED`. Actual: `AVAILABLE / NOT_TRIGGERED`. **PASS**.

This directional rule exposes the verified source quantities and conditions; it does not invent an aggregate numeric score.

Snapshot: `a3eb74e6-cb9a-4234-a650-3937564f1998`. Raw SHA, extraction SHA and persistence: **PASS**.

### INFY — OPERATING_MARGIN_DETERIORATION

Expected: `AVAILABLE / NOT_TRIGGERED`. Actual: `AVAILABLE / NOT_TRIGGERED`. **PASS**.

- Expected exact: `-186255770000/17611216336` BASIS_POINTS.
- Actual exact (reduced): `-11640985625/1100701021` — **PASS**.
- Expected Decimal: `-10.575974222704023457065549501293685090531046214047`.
- Actual Decimal: `-10.575974222704023457065549501293685090531046214047` — **PASS**.
- Expected display `-10.58`; actual `-10.58` — **PASS**.

Snapshot: `5ad93524-df62-48b1-b734-eb8d22169696`. Raw SHA, extraction SHA and persistence: **PASS**.

### YESBANK — DEPOSIT_CONTRACTION

Expected: `AVAILABLE / NOT_TRIGGERED`. Actual: `AVAILABLE / NOT_TRIGGERED`. **PASS**.

- Expected exact: `1819000/266230` PERCENT.
- Actual exact (reduced): `181900/26623` — **PASS**.
- Expected Decimal: `6.8324381174172707809037298576418885925703339217969`.
- Actual Decimal: `6.8324381174172707809037298576418885925703339217969` — **PASS**.
- Expected display `6.83`; actual `6.83` — **PASS**.

Snapshot: `c593dbdd-ab1c-4917-9f02-befb4157287c`. Raw SHA, extraction SHA and persistence: **PASS**.

### YESBANK — DEPOSIT_GROWTH_DECELERATION

Expected: `AVAILABLE / OBSERVATION_ONLY`. Actual: `AVAILABLE / OBSERVATION_ONLY`. **PASS**.

- Expected exact: `-45253122300/2893680493` PERCENTAGE_POINTS.
- Actual exact (reduced): `-45253122300/2893680493` — **PASS**.
- Expected Decimal: `-15.638603643170082357810607115980582449190253431342`.
- Actual Decimal: `-15.638603643170082357810607115980582449190253431342` — **PASS**.
- Expected display `-15.64`; actual `-15.64` — **PASS**.

Snapshot: `f45012dd-af9a-4d0d-99a7-78d9122d5962`. Raw SHA, extraction SHA and persistence: **PASS**.

## Synthetic goldens

| Controlled source input / condition | Exact expected result | Expected = actual outcome | Result |
|---|---|---|---|
| Prior operating profit 25, current 24.5, both sales 100 | -50 bps | TRIGGERED | PASS |
| Prior operating profit 25, current 24.50000001, both sales 100 | -49999999/1000000 bps (-49.999999) | NOT_TRIGGERED | PASS |
| Prior operating profit 25, current 24.49, both sales 100 | -51 bps | TRIGGERED | PASS |
| Three consecutive annual source CFO observations made negative | All three < 0 | TRIGGERED | PASS |
| YESBANK current deposits 250000, prior 266230 | -1623000/266230 percent | TRIGGERED | PASS |
| Negative nonzero INFY PAT | -29409/25568 ratio; explicit interpretation limitation | OBSERVATION_ONLY | PASS |
| Negative INFY CFO with positive PAT | Valid negative ratio | OBSERVATION_ONLY | PASS |

Synthetic inputs are controlled temporary/in-memory test evidence, never revisions of the retained golden HTML. Threshold boundary tests still call the production Part 4C margin calculation, not a manually fabricated margin result.

## Wrong-sector goldens

| Company / requested pattern | Expected = actual status | Outcome | Result |
|---|---|---|---|
| YESBANK / CFO_TO_NET_PROFIT | NOT_APPLICABLE | null | PASS |
| YESBANK / PROFIT_VS_CFO_DIVERGENCE | NOT_APPLICABLE | null | PASS |
| YESBANK / REPEATED_NEGATIVE_CFO | NOT_APPLICABLE | null | PASS |
| YESBANK / OPERATING_MARGIN_DETERIORATION | NOT_APPLICABLE | null | PASS |
| INFY / DEPOSIT_CONTRACTION | NOT_APPLICABLE | null | PASS |
| INFY / DEPOSIT_GROWTH_DECELERATION | NOT_APPLICABLE | null | PASS |
| Unknown profile / classification | NOT_APPLICABLE + UNKNOWN_SECTOR_PROFILE | null | PASS |

## Mandatory evidence refusals

| Condition | Expected = actual | Result |
|---|---|---|
| Missing CFO | INSUFFICIENT_INPUT | PASS |
| Candidate CFO without approved mapping | UNVERIFIED_METRIC_SEMANTICS | PASS |
| Duplicate CFO mapping | UNVERIFIED_METRIC_SEMANTICS | PASS |
| Altered provider, definition-version binding or provenance section | UNVERIFIED_METRIC_SEMANTICS | PASS |
| Annual PAT + quarterly CFO | INCOMPATIBLE_PERIOD | PASS |
| Standalone + consolidated | INCOMPATIBLE_SCOPE | PASS |
| Different currencies | INVALID_INPUT | PASS |
| Missing middle year in the N=3 rule | INSUFFICIENT_INPUT | PASS |
| Zero PAT denominator | INVALID_INPUT; no rule outcome | PASS |
| Corrupt original artifact | Rejected before prerequisite/pattern writes | PASS |
| Wrong pinned snapshot, role, metric, extraction, company, scope or period | Rejected; old history unchanged | PASS |
| AUTO_CREATE request also supplies pins | Rejected; no silent mode mixing | PASS |
| False status/outcome or invalid mapping/snapshot/profile/execution lineage | Domain validation rejects | PASS |

## Reuse and provenance

- CFO semantic proof: exact `cash_operating` / `source:Cash from Operating Activity`, provider `screener`, CASH_FLOW locator, definition and extractor version 1. Canonical identity `SOURCE_REPORTED_CFO` retains only source-reported meaning. Every successful input has exactly one verified mapping and its original provenance.
- Part 4A/4C dispatch spy: one CFO ratio formula, one margin-change formula, two calls to the existing operating-margin primitive, and two existing deposit-growth calls. Shared current growth is reused within a multi-pattern evaluation. Part 4D never recalculates growth or margins itself.
- Deposit deceleration retains all three observation IDs, both growth references and exact growth quantities. Common `PERCENTAGE_POINT_DIFFERENCE/1` subtracts exact fractions, not displays.
- AUTO_CREATE records every prerequisite snapshot; reruns create new prerequisite and pattern UUIDs. PINNED references exactly the supplied historical snapshots and creates none. References validate unique metric identity, company/source/scope/period, profile/classification/alignment, formula and actual input observations.
- Raw artifact SHA and source-payload digests are checked in the real CLI proofs. References retain calculation payload SHA. Findings carry no fabricated raw locator: lineage terminates through original input observations.

## Persistence and independent revision proofs

- Exactly one additive table: `pattern_snapshots` with id, extraction FK, evaluated_at, schema_version and immutable JSON. No singular calculation FK, association table or migration framework. Stored calculation references are validated in the insert transaction.
- A multi-pattern YESBANK evaluation uses two prerequisite snapshots. New evaluations append; duplicate pattern UUID insertion fails. PINNED replay retains exactly the first evaluation’s references. Prior pattern JSON stays unchanged.
- PatternDefinition V1→V2: old finding retains V1 metadata and reconstructs byte-for-byte.
- ThresholdPolicy V1→V2: the controlled threshold changes from -50 to -5 bps; V2 triggers for the same source margin change while V1 stays NOT_TRIGGERED with its original threshold.
- CFO semantic mapping V1→V2: new finding retains mapping version 2; saved V1 mapping and finding are unchanged.
- Source revision: temporary replay changes CFO 29409→29410, yielding new artifact/extraction/prerequisite/pattern snapshots and a different ratio. Old source/pattern history remains intact.
- Calculation revision: AUTO_CREATE reruns create new prerequisite IDs and new findings. PINNED rerun explicitly references old prerequisites. Both generations remain reconstructible.

## CLI proof and limitations

The six real CLI runs, request files, normalized exports, raw artifacts and SQLite are retained under Git-ignored `output/part4d-proof/`. `golden-proof.json` contains expected values separately from actual findings. INFY divergence uses FY2023; other real proofs use FY2025.

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run python scripts/verify_patterns.py --output output/part4d-proof-new
```

- Only annual declared fiscal endpoints are supported by these six rules. Unknown duration starts remain explicit limitations; no dates are invented.
- Source money is normalized by its explicit scale for comparison; original values/units remain in input observations.
- The -50-bps threshold is an internal review heuristic. CFO/PAT and deposit deceleration have no trigger threshold.
- Source fixtures remain PARTIAL because peers are unavailable. Required financial inputs for these patterns are verified independently of overall record count.
- Source-reported NPA ratios, receivables/inventory, debt/interest, dilution and other pattern families remain deferred. No source periods are changed.
- Prerequisites and final pattern are separate append operations. Failure after prerequisite creation can leave valid prerequisites without a pattern row; no history is rewritten.
- This proof uses retained evidence; it makes no live-current-data or historical-availability assertion.

## Final verification

- Targeted pytest: **112 passed** in 42.28 seconds (47 Part 4D tests plus 65 Part 4A/4C regressions).
- Full pytest: **380 passed** in 78.29 seconds.
- Ruff: **All checks passed**.
- mypy: **Success: no issues found in 60 source files**.
- No dependencies were added, installed, upgraded or synchronized. `pyproject.toml` and `uv.lock` are unchanged.
- Part 2/4A/4C golden fixtures are unchanged. The existing schema-inventory test was updated solely to include the approved `pattern_snapshots` table.

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

## Git review state

Branch `main`, HEAD `13477c7` (Part 4C baseline). No commit/tag created and nothing staged.

```text
 M README.md
 M docs/architecture.md
 M src/stock_research_ai/__main__.py
 M src/stock_research_ai/adapters/json_export.py
 M src/stock_research_ai/adapters/screener/semantic_mappings.py
 M src/stock_research_ai/adapters/sqlite.py
 M src/stock_research_ai/application/calculations.py
 M src/stock_research_ai/application/engine.py
 M src/stock_research_ai/application/ports.py
 M src/stock_research_ai/application/sector_kpis.py
 M src/stock_research_ai/bootstrap.py
 M src/stock_research_ai/domain/calculations.py
 M tests/test_extraction.py
?? docs/part4d-proof.md
?? scripts/verify_patterns.py
?? src/stock_research_ai/application/patterns.py
?? src/stock_research_ai/domain/patterns.py
?? tests/fixtures/patterns/README.md
?? tests/fixtures/patterns/expected_findings.json
?? tests/fixtures/patterns/expected_inputs.json
?? tests/test_pattern_pipeline.py
?? tests/test_patterns.py
```
