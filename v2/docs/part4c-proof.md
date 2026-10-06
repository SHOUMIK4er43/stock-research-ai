# Part 4C implementation proof

Verified against unchanged retained Part 2 HTML and independently authored inputs/outputs.
No live requests, dependency changes, commits or tags were made. This is the first BANK/IT_SERVICES slice only.

## Numerical and eligibility goldens

All 11 outcomes passed through the real offline CLI. Exact rational equality uses no tolerance.
Values below are percent except OPERATING_MARGIN_CHANGE_BPS (basis points). Decimal strings use
the formula’s recorded 50-digit HALF_EVEN representation; the fraction remains the exact result.
Display rounding is separate, two decimal places.

### YESBANK — DEPOSIT_GROWTH

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `1819000 / 266230`.
- Actual exact fraction (reduced): `181900 / 26623` — **PASS**.
- Expected Decimal: `6.8324381174172707809037298576418885925703339217969`.
- Actual Decimal: `6.8324381174172707809037298576418885925703339217969` — **PASS**.
- Expected display: `6.83`; actual: `6.83` — **PASS**.

### YESBANK — AVERAGE_ASSETS_RETURN

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `489400 / 830478`.
- Actual exact fraction (reduced): `244700 / 415239` — **PASS**.
- Expected Decimal: `0.58929917469216523496107061234614282377137022293185`.
- Actual Decimal: `0.58929917469216523496107061234614282377137022293185` — **PASS**.
- Expected display: `0.59`; actual: `0.59` — **PASS**.

### YESBANK — SOURCE_REVENUE_GROWTH

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `331300 / 27606`.
- Actual exact fraction (reduced): `165650 / 13803` — **PASS**.
- Expected Decimal: `12.001014272259653698471346808664783018184452655220`.
- Actual Decimal: `12.001014272259653698471346808664783018184452655220` — **PASS**.
- Expected display: `12.00`; actual: `12.00` — **PASS**.

### YESBANK — OPERATING_MARGIN

- Expected status: NOT_APPLICABLE; actual: NOT_APPLICABLE — **PASS**.
- Expected and actual numerical values: null; no fabricated calculation.

### YESBANK — NIM

- Expected status: INSUFFICIENT_INPUT; actual: INSUFFICIENT_INPUT — **PASS**.
- Expected and actual numerical values: null; no fabricated calculation.

### INFY — SALES_GROWTH

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `765900 / 128933`.
- Actual exact fraction (reduced): `765900 / 128933` — **PASS**.
- Expected Decimal: `5.9402945715992026866667183731085137241823272552411`.
- Actual Decimal: `5.9402945715992026866667183731085137241823272552411` — **PASS**.
- Expected display: `5.94`; actual: `5.94` — **PASS**.

### INFY — NET_PROFIT_GROWTH

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `-166600 / 27234`.
- Actual exact fraction (reduced): `-4900 / 801` — **PASS**.
- Expected Decimal: `-6.1173533083645443196004993757802746566791510611735`.
- Actual Decimal: `-6.1173533083645443196004993757802746566791510611735` — **PASS**.
- Expected display: `-6.12`; actual: `-6.12` — **PASS**.

### INFY — OPERATING_MARGIN

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `3349900 / 136592`.
- Actual exact fraction (reduced): `837475 / 34148` — **PASS**.
- Expected Decimal: `24.524862363828042637929014876420288157432353285698`.
- Actual Decimal: `24.524862363828042637929014876420288157432353285698` — **PASS**.
- Expected display: `24.52`; actual: `24.52` — **PASS**.

### INFY — OPERATING_MARGIN_CHANGE_BPS

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `-186255770000 / 17611216336`.
- Actual exact fraction (reduced): `-11640985625 / 1100701021` — **PASS**.
- Expected Decimal: `-10.575974222704023457065549501293685090531046214047`.
- Actual Decimal: `-10.575974222704023457065549501293685090531046214047` — **PASS**.
- Expected display: `-10.58`; actual: `-10.58` — **PASS**.

### INFY — EPS_GROWTH

- Expected status: AVAILABLE; actual: AVAILABLE — **PASS**.
- Expected exact fraction: `-40500 / 6561`.
- Actual exact fraction (reduced): `-500 / 81` — **PASS**.
- Expected Decimal: `-6.1728395061728395061728395061728395061728395061728`.
- Actual Decimal: `-6.1728395061728395061728395061728395061728395061728` — **PASS**.
- Expected display: `-6.17`; actual: `-6.17` — **PASS**.

### INFY — CASA_RATIO

- Expected status: NOT_APPLICABLE; actual: NOT_APPLICABLE — **PASS**.
- Expected and actual numerical values: null; no fabricated calculation.

## Negative proofs

| Controlled case | Expected and actual outcome | Result |
|---|---|---|
| YESBANK industrial operating margin | NOT_APPLICABLE | PASS |
| INFY CASA ratio | NOT_APPLICABLE | PASS |
| UNKNOWN classification | NOT_APPLICABLE / UNKNOWN_SECTOR_PROFILE | PASS |
| Classification/profile mismatch | NOT_APPLICABLE / PROFILE_CLASSIFICATION_MISMATCH | PASS |
| Missing required annual prior observation | INSUFFICIENT_INPUT; no quarterly substitution | PASS |
| Missing NIM and other unsupported banking conventions | INSUFFICIENT_INPUT | PASS |
| Retained synthetic source:NIM candidate without mapping | UNVERIFIED_METRIC_SEMANTICS | PASS |
| Altered exact identity, provider, mapping version, definition or section locator | UNVERIFIED_METRIC_SEMANTICS | PASS |
| Duplicate candidate or canonical registry mapping | UNVERIFIED_METRIC_SEMANTICS | PASS |
| Cross-scope input or request/source scope mismatch | INCOMPATIBLE_SCOPE | PASS |
| Wrong frequency, conflicting duration/date, fiscal endpoint or quarterly bank asset/deposit request | INCOMPATIBLE_PERIOD | PASS |
| Incompatible currency | INVALID_INPUT | PASS |
| Zero growth denominator | INVALID_INPUT / ZERO_PRIOR_VALUE | PASS |
| Classification outside its effective interval | NOT_APPLICABLE | PASS |
| Unknown profile/KPI identifier | Request rejected, no made-up definition | PASS |
| Unimplemented formula definition | INVALID_INPUT | PASS |
| Corrupt raw artifact or incorrect pinned identity | Rejected before persistence | PASS |

Negative current/prior growth values are also tested: valid negative values remain AVAILABLE under
the unchanged Part 4A absolute-prior convention, with an explicit limitation for negative prior values.
Quarterly YoY and sequential proofs select exactly the prior-year or previous-quarter observations.

## Architecture and history evidence

- **Exact semantic mapping:** each successful input retains provider, exact source ID, section, canonical identity, immutable mapping ID/version, and the original observation provenance. Modified or duplicate bindings fail. No fuzzy or company-name matching.
- **Eligibility:** only BANK_PROFILE/1 and IT_SERVICES_PROFILE/1 exist in the production registry. Generic source Revenue growth is absent from the bank KPI inventory and exported separately. No upgraded bank income meaning or computed source-displayed ratios.
- **Formula reuse:** a dispatch spy proves five growth calls, one existing average-assets call, one BPS call and three existing margin calls (standalone margin plus both BPS components). Formula versions remain 1. BPS retains four inputs and uses exact fractions; rounded margins would wrongly give -11 bps.
- **Lineage:** raw-byte SHA checks and extraction-payload digest checks pass for both companies. Each successful observation matches the pinned extraction exactly. Results retain definition/profile/classification/alignment metadata, with no fabricated raw locator for derived KPIs.
- **Backwards compatibility:** the literal pre-4C, unwrapped schema 4.1 fixture deserializes after the changes. Its stored JSON bytes remain unchanged after new financial and sector rows are saved/read. New version-1 envelopes decode mixed history unambiguously; unknown payload types/versions are rejected. No new table or migration.
- **Revisions:** the same temporary database retains V1 and V2 profile, mapping, KPI-definition and classification metadata. V2 removes average-assets eligibility in a controlled test; V1 still reconstructs unchanged. A separately replayed deposit source revision produces new raw/extraction/calculation IDs and values; old source and calculated snapshots remain unchanged. Duplicate snapshot inserts fail.
- **Real CLI:** both source replay and calculate-sector-kpis executed with subprocess network blocked. YESBANK has 3/5 available outcomes (including one generic calculation), INFY 5/6. Exit 3 deliberately signals the requested unavailable/not-applicable outcomes, while all expected results pass. Both persisted snapshots equal their exported models.

## Verification results

- Targeted: **65 passed** (35 Part 4C + 30 Part 4A), 28.40 seconds.
- Full required pytest command: **333 passed**, 58.64 seconds.
- Required Ruff command: **All checks passed**.
- Required mypy command: **Success: no issues found in 58 source files**.
- Dependency changes: **none**; pyproject.toml and uv.lock unchanged. No environment installation or synchronization.

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

## Reproduction and review artifacts

The completed local run is Git-ignored at `output/part4c-proof/`. Its `golden-proof.json` contains
independent expected values alongside actual results, rational fractions, statuses, SHA/persistence
results and CLI exit codes. Each company has profile, coverage and normalized output JSON.

For a fresh proof, use a new/empty output directory:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run python scripts/verify_sector_kpis.py --output output/part4c-proof-new
```

## Limitations

- Only explicitly selected KPIs are evaluated; the registry is not automatically expanded into a report.
- Classification/profile membership and fiscal calendar remain explicit declarations. No sector inference.
- Missing financial duration start dates remain recorded limitations under the caller-declared alignment policy.
- NIM, CASA, PCR, credit cost, CRAR, CET1, bank ROA/ROE, advances/CD ratio, GNPA and NNPA are unavailable as calculated KPIs. Source-displayed ratios remain source evidence.
- YESBANK Revenue retains only its source-reported meaning. Average-assets return is not labelled ROA.
- Existing source fixtures report PEERS unavailable; their extraction coverage remains PARTIAL. Selected KPI inputs are present and independently checked.
- Proofs use retained source snapshots, not live current data or historical point-in-time availability claims.
- No new bank ratio conventions, extra sector profiles, valuation, peer ranking, indicators, recommendations or AI.

## Git review state

Branch `main`, HEAD `25871a0` (`feat: complete part 4b market analytics`). No staged files,
new commits or tags. Ten modified files and ten new untracked files:

```text
 M README.md
 M docs/architecture.md
 M src/stock_research_ai/__main__.py
 M src/stock_research_ai/adapters/json_export.py
 M src/stock_research_ai/adapters/sqlite.py
 M src/stock_research_ai/application/calculations.py
 M src/stock_research_ai/application/engine.py
 M src/stock_research_ai/application/ports.py
 M src/stock_research_ai/bootstrap.py
 M src/stock_research_ai/domain/calculations.py
?? docs/part4c-proof.md
?? scripts/verify_sector_kpis.py
?? src/stock_research_ai/adapters/screener/semantic_mappings.py
?? src/stock_research_ai/application/sector_kpis.py
?? src/stock_research_ai/domain/sector_kpis.py
?? tests/fixtures/sector_kpis/README.md
?? tests/fixtures/sector_kpis/expected_inputs.json
?? tests/fixtures/sector_kpis/expected_outputs.json
?? tests/fixtures/sector_kpis/legacy_4a_snapshot.json
?? tests/test_sector_kpis.py
```

`git diff --check` passed. Existing Screener/Part 4A fixtures, `pyproject.toml` and `uv.lock`
have no diff. Proof SQLite/artifacts/exports remain ignored under `output/`.
