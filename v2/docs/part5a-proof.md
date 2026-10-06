# Part 5A implementation and proof

Verified locally: 549 full tests passed (was 548 before this fix, plus one new
regression proof added below); targeted valuation suite: 105 passed. Extraction
suite (containing the one schema-inventory fix): 10 passed. Ruff passed on the
full tree. Mypy passed across 65 source files. A fresh offline production-CLI
proof run (`output/part5a-proof-final`, generated for this verification pass and
not reused from any earlier run) executed 13 cases and passed every component
comparison, SHA and history check. No dependencies, lockfile, or existing golden
fixtures were changed. No Git commit, tag or push was made.

## Six valuation methods and profile applicability

All method, formula and definition versions are **1**. `profile_version` is
pinned to **1**; any other value or an unregistered `profile_id` refuses
`NOT_APPLICABLE` (`UNKNOWN_OR_INCOMPATIBLE_PROFILE`).

| Method ID | Version | Classification | Category | Applicable profiles |
|---|---:|---|---|---|
| `PRICE_TO_EARNINGS` | 1 | CALCULATION | OBSERVED_MULTIPLE_CALCULATION | IT_SERVICES_PROFILE, BANK_PROFILE |
| `PRICE_TO_BOOK` | 1 | CALCULATION | OBSERVED_MULTIPLE_CALCULATION | IT_SERVICES_PROFILE, BANK_PROFILE |
| `FCFF_DCF` | 1 | SCENARIO | SCENARIO_CALCULATION | IT_SERVICES_PROFILE only |
| `IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE` | 1 | SCENARIO | SCENARIO_EXPECTATION | IT_SERVICES_PROFILE only |
| `REQUIRED_EPS_CAGR_FOR_RETURN` | 1 | SCENARIO | SCENARIO_EXPECTATION | IT_SERVICES_PROFILE, BANK_PROFILE |
| `REQUIRED_BVPS_CAGR_FOR_RETURN` | 1 | SCENARIO | SCENARIO_EXPECTATION | BANK_PROFILE only |

`IT_SERVICES_PROFILE` requires `classification.classification == NON_FINANCIAL`;
`BANK_PROFILE` requires `classification.classification == BANK`. The two FCFF/DCF
methods assume no financial-firm enterprise structure and refuse for banks
(`METHOD_NOT_APPLICABLE_TO_PROFILE`); `REQUIRED_BVPS_CAGR_FOR_RETURN` is bank-only
because book value per share is the bank valuation anchor, not an IT-services one.
`PRICE_TO_EARNINGS`, `PRICE_TO_BOOK` and `REQUIRED_EPS_CAGR_FOR_RETURN` apply to
both registered profiles.

## Real-source scope (locked, per V1 directive)

Real retained INFY (standalone) and YESBANK (consolidated) Screener evidence is
used **only** to prove the two observed-multiple methods, because those are the
only methods whose full semantic contract is genuinely satisfied by the retained
source facts (a pinned current price and a pinned annual EPS/BVPS observation).
DCF, reverse-DCF and the two required-CAGR expectation methods require WACC,
terminal growth, forecast FCFF, exit multiples and required-return assumptions
that no public source page discloses as fact — these are proved exclusively
against the independent synthetic golden (`tests/fixtures/valuation/`), never
invented for INFY or YESBANK. This was verified, not re-implemented, during this
pass: no new real-source scenario test was added.

### INFY — `PRICE_TO_EARNINGS`: **AVAILABLE**

- Price: FACT, semantic `PRICE`, value `1087` INR, mapping canonical `PRICE`
  status `VERIFIED`, financial observation `metric_id=current_price`,
  reporting scope `unknown` (point-in-time top-metric row, no fiscal period).
- Denominator: FACT, semantic `EPS`, value `72.03` INR, mapping canonical `EPS`
  status `VERIFIED`, financial observation `metric_id=eps`, fiscal label
  `Mar 2026`, reporting scope `standalone`, annual frequency.
- Financial extraction: the Part 2 `CompanyExtractionResult` produced by replaying
  `tests/fixtures/screener/infy_standalone.html`. Its raw artifact content hash
  is stable across independent runs — `d5d98e1b7e9360d48d63320c3f106ae027027745651c22dcc18d5f7b0a0031bf`,
  confirmed identical on two separately executed offline proof runs in this
  session — because it hashes the fixture's actual HTML bytes. The extraction's
  own payload hash (`payload_sha256`) is independently recomputed and matched
  against the pinned value at persistence time on every run (see Raw SHA /
  tamper proof below); it differs run-to-run because each fresh acquisition
  assigns a new extraction snapshot UUID, so no single payload hash is quoted
  here as a fixed constant.
- No market extraction pinned (price comes from the same Screener snapshot).
- Calculated multiple: `108700/7203` = `15.090934332916840205469943079272525336665278356240...`,
  matching the fresh-run production output to 50 stored digits within `1e-45`
  absolute tolerance (exact fraction is non-terminating).
- Issues retained: `POINT_IN_TIME_AVAILABILITY_UNVERIFIED`, `UNVERIFIED_ADJUSTMENT`,
  `SOURCE_REPORTED_ANNUAL_EPS_BASIC_DILUTED_BASIS_UNSPECIFIED`.

### INFY — `PRICE_TO_BOOK`: **UNVERIFIED_METRIC_SEMANTICS** (refused, correctly)

- Denominator: semantic `BVPS`, mapping `None`, `semantic_verified=False`. The
  only retained balance-sheet ratio is the pre-existing `metric_id=book_value`
  ("Book Value"), which has no semantic mapping to canonical `BVPS` — that
  canonical mapping only matches the label `"Book Value Per Share in Rs"`, which
  neither real fixture contains. The evaluator refuses rather than treat "Book
  Value" as book value **per share**.

### YESBANK — `PRICE_TO_EARNINGS`: **AVAILABLE**

- Price: FACT, semantic `PRICE`, value `22.4` INR, mapping `PRICE` VERIFIED,
  `metric_id=current_price`.
- Denominator: FACT, semantic `EPS`, value `1.12` INR, mapping `EPS` VERIFIED,
  `metric_id=eps`, fiscal label `Mar 2026`, reporting scope `consolidated`.
- Financial extraction from `tests/fixtures/screener/yesbank_consolidated.html`.
  Raw artifact content hash `3e7818ae35a5e72dec57c9f72e6d58724ce9a4204846df23bcb89242dbf73ffb`,
  likewise stable across independent runs; extraction payload hash independently
  re-verified per run as above, not quoted as a fixed constant.
- Calculated multiple: exactly `20`.
- Issues: `POINT_IN_TIME_AVAILABILITY_UNVERIFIED`, `UNVERIFIED_ADJUSTMENT`,
  `SOURCE_REPORTED_ANNUAL_EPS_BASIC_DILUTED_BASIS_UNSPECIFIED`.

### YESBANK — `PRICE_TO_BOOK`: **UNVERIFIED_METRIC_SEMANTICS** (refused, correctly)

Same reason as INFY: only a `book_value` ("Book Value") ratio is retained, not a
mapped `BVPS` observation.

### YESBANK — `FCFF_DCF` / `IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE`: **NOT_APPLICABLE**

`METHOD_NOT_APPLICABLE_TO_PROFILE` — both methods are registered only for
`IT_SERVICES_PROFILE`; YESBANK is declared `BANK_PROFILE`.

### INFY — `REQUIRED_BVPS_CAGR_FOR_RETURN`: **NOT_APPLICABLE**

`METHOD_NOT_APPLICABLE_TO_PROFILE` — registered only for `BANK_PROFILE`; INFY is
declared `IT_SERVICES_PROFILE`.

## Source-reported vs. calculated multiple separation

The Screener "Stock P/E" row is retained as `source_reported_multiples`
(`ValuationSnapshot.source_reported_classification = FACT`,
`source_reported_category = SOURCE_REPORTED_VALUATION_EVIDENCE`) and is never
substituted into a calculated `ValuationResult`:

- INFY: source-reported `Stock P/E = 14.2`; calculated `PRICE_TO_EARNINGS`
  multiple `≈ 15.0909...`. Distinct, and asserted distinct in
  `test_real_source_and_separate_reported_multiple`.
- YESBANK: source-reported `Stock P/E = 18.6`; calculated multiple `= 20`.
  Also asserted distinct.
- Structural guarantee, not just observed-value inequality: this pass adds
  `test_source_reported_multiple_cannot_satisfy_calculated_denominator`, which
  directly pins the source-reported `price_earnings` observation as the
  `PRICE_TO_EARNINGS` EPS denominator and proves it refuses
  `UNVERIFIED_METRIC_SEMANTICS`. Two independent guards enforce this: (1) no
  semantic mapping resolves that observation to canonical `EPS`, and (2) even if
  one existed, the observation's `FinancialUnit.kind` is `ratio`, not `money`,
  which `semantic_verified` requires for a financial FACT. `export_valuation`
  also writes source-reported evidence only into the top-level snapshot
  document, never into `multiples.json` (which is filtered to
  `OBSERVED_MULTIPLE_CALCULATION` results only).

## Point-in-time semantics

`as_of_policy` is fixed to `CURRENT_PINNED_SNAPSHOT_V1`: a valuation pins an
exact financial extraction and (optionally) an exact market observation by ID,
and is numerically reproducible from those pinned IDs alone. This does not
assert that the *financial* fact (e.g., an annual EPS) was publicly known on the
same date as the pinned market close. Every result — synthetic and real,
`AVAILABLE` or not — carries the `POINT_IN_TIME_AVAILABILITY_UNVERIFIED` issue.
`test_six_independent_goldens` and `test_pinned_market_and_temporal_discipline`
assert this issue is present on every `AVAILABLE` result. Pinning a market
observation at an earlier date (`test_pinned_market_and_temporal_discipline`,
case `valid`, trading date `2025-01-02`) still numerically succeeds
(`multiple == 20`) while retaining the same unresolved-availability issue and
`as_of_policy == CURRENT_PINNED_SNAPSHOT_V1` — historical price plus a
present-pinned financial extraction is never promoted to a historically-known
valuation.

## P/E and P/B denominator policy, with edge proofs

`PRICE_TO_EARNINGS` requires an EPS observation with `period.frequency == annual`
and a resolved `end_date` (`ANNUAL_EPS_PERIOD_REQUIRED` otherwise —
`test_financial_compatibility[ttm]` proves a TTM-frequency EPS refuses
`INCOMPATIBLE_PERIOD`). `PRICE_TO_BOOK` requires an instant-period BVPS
observation with a resolved `end_date` (`DATED_BVPS_REQUIRED`). Both require
`reporting_scope` to match the request's declared scope
(`test_financial_compatibility[scope]` → `INCOMPATIBLE_SCOPE`) and price/
denominator currency to match (`test_financial_compatibility[currency]` →
`INVALID_INPUT`). Denominator sign is checked explicitly and independently for
each method:

| Denominator | Value | Result |
|---|---|---|
| EPS | `0` | `INVALID_INPUT` / `ZERO_EPS_MULTIPLE_NOT_MEANINGFUL` |
| EPS | `< 0` | `INVALID_INPUT` / `NEGATIVE_EPS_MULTIPLE_NOT_MEANINGFUL` |
| BVPS | `0` | `INVALID_INPUT` / `ZERO_BVPS_MULTIPLE_NOT_MEANINGFUL` |
| BVPS | `< 0` | `INVALID_INPUT` / `NEGATIVE_BVPS_MULTIPLE_NOT_MEANINGFUL` |

(`test_multiple_nonpositive_denominator`, parametrized over both methods and
both signs; `test_nonpositive_expectation_base` proves the equivalent refusal
for the two required-CAGR expectation methods, whose denominator sign check
uses the same code path.)

## DCF golden (locked, independently hand-derived — not regenerated)

Hand-specified scenario: FCFF₁=110, FCFF₂=121, WACC=10%, terminal growth=0%,
net debt=200, shares=10.

| Component | Expected | Actual (fresh proof run) |
|---|---|---|
| PV FCFF₁ | 100 | 100 |
| PV FCFF₂ | 100 | 100 |
| Terminal FCFF | 121 | 121 |
| Terminal value | 1210 | 1210 |
| PV terminal | 1000 | 1000 |
| Enterprise value | 1200 | 1200 |
| Equity value | 1000 | 1000 |
| Scenario value/share | 100 | 100 |

Signed-scenario variants are also proved (`test_signed_forward_scenarios`):
negative net debt, negative individual FCFF years (including a
double-negative-year case), and a zero-FCFF year — each with independently
hand-derived expected per-share values (`140`, `80`, `-140`, `-10`, `-80`).
`FORECAST_YEARS_MUST_BE_ORDERED_CONSECUTIVE` and `UNUSED_SCENARIO_ASSUMPTION`
guard against gapped, reordered or extraneous forecast-year assumptions
(`test_scenario_shape_and_provenance`).

## Reverse DCF golden and solver trace

Starting FCFF=100, N=2, WACC=10%, terminal growth=0%, net debt=200, shares=10,
observed price=100. Hand bracket: at growth=0%, price=80 (residual -20); at
growth=20%, price=1340/11 (residual 240/11 ≈ 21.818...). The exact midpoint of
that bracket, 10%, is the hand-specified root.

Fresh-run solver trace (`DECIMAL_BISECTION_V1`, version 1):

```
lower=0, upper=0.2
lower_residual=-20.000000000000000000000000000000000000000000000000000000000000000000
upper_residual=21.8181818181818181818181818181818181818181818181818181818181818181818
iterations=1
final_lower=0, final_upper=0.2
final_residual=0
termination=EXACT_ROOT
implied_growth=10.0
```

`residual_tolerance` and `growth_tolerance` are both `1e-30`, at 70 working
digits — matching the declared `SolverPolicy` and `ValuationPrecision` defaults.
Boundary and failure cases (`test_reverse_boundaries`): a root exactly at the
lower bracket endpoint (0 iterations), exactly at the upper endpoint (0
iterations), no sign change in the declared bracket
(`NO_SOLUTION_IN_DECLARED_BRACKET`, bracket never silently widened), a
one-iteration cap forcing non-convergence (`SOLVER_DID_NOT_CONVERGE`), and a
negative starting FCFF that still converges to the same 10% root while
retaining a `NEGATIVE_STARTING_FCFF_SCENARIO` limitation issue.
`test_ambient_precision_independent` proves the solver reaches the same result
even when the caller's ambient `Decimal` context is set to 6 digits with the
`Inexact` trap enabled — the evaluator's own `localcontext` fully isolates it.

## Required EPS CAGR golden

Price=100, EPS=5, required return=10%, N=2, exit P/E=20.

| Output | Expected | Actual |
|---|---|---|
| Required future price | 121 | 121.0000 |
| Required future EPS | 6.05 | 6.0500 |
| Required EPS CAGR | 10% | 10.00 |

## Required BVPS CAGR golden

Price=100, BVPS=50, required return=10%, N=2, exit P/B=2.

| Output | Expected | Actual |
|---|---|---|
| Required future price | 121 | 121.0000 |
| Required future BVPS | 60.5 | 60.5000 |
| Required BVPS CAGR | 10% | 10.00 |

Both expectation methods share edge proofs (`test_scenario_invalid_inputs`):
`required_return <= -100%` → `INVALID_REQUIRED_RETURN`; `exit_pe <= 0` or
`exit_pb <= 0` → `NONPOSITIVE_EXIT_MULTIPLE`. Exit multiples are structurally
forced `CALLER_DECLARED` (`ValuationAssumption.provenance` validator rejects a
source-evidenced `exit_pe`/`exit_pb`) since no source page publishes a forward
exit multiple as fact.

## Bank/profile refusal proof

- `YESBANK` + `FCFF_DCF` → `NOT_APPLICABLE` (`test_bank_enterprise_refusal`, and
  reproduced live above).
- `YESBANK` + `IMPLIED_FCFF_GROWTH_FROM_MARKET_PRICE` → `NOT_APPLICABLE` (same
  test, and reproduced live above).
- `IT_SERVICES_PROFILE` + `REQUIRED_BVPS_CAGR_FOR_RETURN` → `NOT_APPLICABLE`
  (`test_it_bank_expectation_refusal`).
- Unregistered `profile_id` (`UNKNOWN`, `MANUFACTURING_PROFILE`) →
  `NOT_APPLICABLE` (`test_unknown_profile`).
- `BANK_PROFILE` + `PRICE_TO_EARNINGS` / `REQUIRED_EPS_CAGR_FOR_RETURN` →
  `AVAILABLE` (`test_bank_eps_methods_are_eligible`) — both are eligible for
  banks; only the enterprise-value-based methods and the BVPS-CAGR method are
  profile-restricted to one side.

## FACT / CALCULATION / SCENARIO classification proof

`ValuationInput.classification` is `FACT` for any pinned financial or market
observation and `CALCULATION` for any pinned upstream calculation metric (e.g.
a Part 4A/4C ratio); the model validator (`evidence_value`) rejects a FACT input
without its original observation object and rejects a CALCULATION input that
carries one. `ValuationDefinition.classification` is `CALCULATION` for the two
observed multiples and `SCENARIO` for the four scenario/expectation methods —
asserted directly for every method in `test_six_independent_goldens`.
`ValuationAssumption.classification` is fixed to the literal `SCENARIO`.
`test_assumption_provenance_contract` proves a `FACT`-classified assumption,
a `SYSTEM_DEFINED` provenance basis, a missing declaration string, and a
source-evidenced exit multiple are all rejected at the Pydantic layer before
evaluation ever runs.

## Assumption provenance proof

Every `ValuationAssumption` must declare `provenance_basis` as either
`SOURCE_EVIDENCE` (requires a resolvable `EvidencePin`, and that pin's resolved
value/currency must exactly equal the declared assumption value/currency —
`ASSUMPTION_EVIDENCE_MISMATCH` otherwise) or `CALLER_DECLARED` (requires a
non-empty free-text `declaration` and forbids an evidence pin). `exit_pe` and
`exit_pb` are the one semantic pair structurally forced to `CALLER_DECLARED`
regardless of caller intent. `test_assumption_provenance_contract` and
`test_scenario_shape_and_provenance[source_assumption]` both exercise this from
different angles (schema-level rejection and evaluation-time
`ASSUMPTION_EVIDENCE_MISMATCH`).

## Evidence/assumption lineage proof

`ValuationSnapshot.lineage` (a `model_validator`) independently re-checks, on
every constructed snapshot: every pinned source referenced by a selection or
assumption exists in `sources`; every result's `snapshot_id`/`selection_index`/
`definition.method` matches its selection; every result's `scenario_id` and
`assumption_ids` exactly match its selection's scenario; every retained input's
pin resolves to a real source and, where an observation is attached, that
observation's ID matches the pin and its company matches the request.
`test_lineage_corruption_rejected` mutates a stored snapshot's assumption ID,
result-level snapshot ID, a retained FACT's value, a retained FACT's
classification, and a semantic mapping's observation ID — each independently
raises `ValidationError` on reconstruction. `test_wrong_pinned_identity`
proves evaluation itself (not just reconstruction) refuses a wrong financial
extraction ID, wrong company ID, wrong observation ID, and a calculation pin
substituted where a FINANCIAL pin was expected.

## Raw SHA / tamper proof

`SQLiteValuationRepository.save_valuation` independently reloads every pinned
source (financial extraction, market extraction, or calculation snapshot) from
its own repository row inside the append transaction and recomputes its
sha256 digest, comparing it against the `payload_sha256` recorded on the
`ValuationSnapshot` at evaluation time — a mismatch raises `ValueError` and
nothing is appended (`test_raw_corruption_refuses_before_append` tampers with
the persisted raw artifact bytes on disk after a first successful valuation,
then proves a second valuation attempt against the same pinned extraction
raises before any second row exists — `valuation_history` still returns exactly
one snapshot). `scripts/verify_valuation.py` independently re-hashes every raw
artifact on disk against its recorded `content_sha256` for all three real/
synthetic company fixtures on every run.

## Valuation persistence/history proof

`test_engine_export_sha_history` proves: `evaluate_valuation` persists exactly
one `valuation_snapshots` row; `valuation_history` returns it verbatim;
re-saving the identical snapshot ID raises `IntegrityError` (append-only, no
upsert); a snapshot with a damaged source hash raises `ValueError` before
persisting; all four `normalized/*.json` export files exist and
`valuation_snapshot.json` round-trips byte-identically through
`ValuationSnapshot.model_validate_json`; and the original financial extraction's
JSON is byte-identical before and after (valuation persistence never mutates
upstream Part-2 evidence).

## Immutability / revision proof

Independent revision tests, each producing a distinct `ValuationSnapshot.id`
while every prior history entry reconstructs byte-identical
(`model_dump_json()` compared before/after each new save):

- Scenario assumption values — WACC, terminal growth, an individual forecast
  FCFF year, net debt, diluted shares, required return, exit P/E
  (`test_assumption_revision_history`, parametrized over all seven fields).
- Valuation method/formula definition version bump
  (`test_policy_and_source_revisions`, `version=2`/`formula_version=2` copy of
  `DEFINITIONS`).
- `ValuationPrecision` policy version/working-digit change (same test,
  `ValuationPrecision(version=2, working_digits=80)`).
- `SolverPolicy` version and `growth_tolerance` change (same test).
- Financial extraction (source) revision — a re-acquired extraction with a
  changed underlying value produces a new snapshot whose
  `financial_extraction_id` differs, while re-pinning the *original* extraction
  ID still reconstructs the original source hash exactly.
- Market observation revision — two different pinned trading dates on the same
  instrument produce two snapshots with different IDs and different multiples
  (`20` vs `21`), while each individually remains reproducible
  (`test_pinned_market_and_temporal_discipline`,
  `test_market_pin_revision_persists_refusals_unchanged` — the latter also
  proves a *refused* result (`UNVERIFIED_METRIC_SEMANTICS`, unknown market
  currency) persists and reconstructs unchanged across a market revision, not
  only `AVAILABLE` results).

No historical `valuation_snapshots` row is ever rewritten; every revision is a
new append-only row with a new UUID.

## CLI / export behavior

`stock-research-ai evaluate-valuation --request <path> --output <dir>` exits
`0` when every requested selection resolves `AVAILABLE`, and `3` otherwise
(mirroring `calculate-technicals`) — proved live by `test_production_cli`
(parametrized true/false) and by the fresh `scripts/verify_valuation.py` run,
which drives the real CLI subprocess for all 13 cases. Export writes
`normalized/valuation_snapshot.json` (the full snapshot), `normalized/
multiples.json` (results filtered to `OBSERVED_MULTIPLE_CALCULATION`),
`normalized/scenarios.json` (`SCENARIO_CALCULATION`), `normalized/
reverse_expectations.json` (`SCENARIO_EXPECTATION`), and `coverage.json`
(per-selection method/status/issues only, no values — a lightweight index).

## Limitations

This slice intentionally: fixes all method/formula/precision/solver versions at
1; treats every result as `POINT_IN_TIME_AVAILABILITY_UNVERIFIED` since no
public-disclosure timestamp for a financial fact is independently confirmed
against a pinned market date; treats every price-bearing result as carrying an
`UNVERIFIED_ADJUSTMENT` limitation (no corporate-action/adjustment
reconstruction, consistent with Part 4E); never infers currency for a market
instrument that does not declare one; never infers BVPS from a generic "Book
Value" ratio; and proves DCF/reverse-DCF/required-CAGR mathematics only against
an independent synthetic scenario, never by inventing WACC, terminal growth,
forecast FCFF, an exit multiple or a required return for a real company. There
is no live valuation dashboard, no ranking/screening across companies, no
Monte Carlo or probabilistic scenario handling, and no dependency additions.

## Reproduce

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run python scripts/verify_valuation.py --output output/part5a-proof-manual
```

The script drives the real `stock-research-ai` CLI subprocess for `init-db`,
`extract-screener` (synthetic + real INFY/YESBANK replays) and
`evaluate-valuation` across 13 cases; exact commands are saved to
`commands.json`, full comparisons/inputs/issues/solver traces to `proof.json`.
It uses no network access, no Kite credentials, and no live Screener capture.

Required final checks executed successfully in this pass:

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

Results: **549 passed in 71.64s; Ruff all checks passed; mypy no issues in 65
source files**. A fresh proof run (`output/part5a-proof-final`, distinct from
both the original Codex-authored `output/part5a-proof` and this session's own
earlier `output/part5a-proof-verified`) printed: **13 production CLI cases
PASS; all component comparisons, SHA and history checks PASS**.

## Files and Git state

Fixed in this pass: `tests/test_extraction.py` (added `valuation_snapshots` to
the pre-existing schema-inventory assertion — no table removed, no expectation
weakened), `scripts/verify_valuation.py` (formatting-only line-length fix, no
change to proof semantics), `tests/test_valuation.py` (one new regression test:
`test_source_reported_multiple_cannot_satisfy_calculated_denominator`).

Added in this pass: `docs/part5a-proof.md` (this file) and the `## Part 5A`
section of `README.md`.

Everything else was authored in the prior (Codex) session and is unchanged by
this pass: `scripts/verify_valuation.py` (except the one line-length fix),
`src/stock_research_ai/application/numerics.py`,
`src/stock_research_ai/application/valuation.py`,
`src/stock_research_ai/domain/valuation.py`,
`tests/fixtures/valuation/`, `tests/test_valuation_pipeline.py`, and the
modifications to `__main__.py`, `adapters/json_export.py`,
`adapters/screener/{metrics,semantic_mappings,statements}.py`,
`adapters/sqlite.py`, `application/{engine,market_calculations,ports}.py` and
`bootstrap.py`.

Final Git state: branch `main`, HEAD `f652571` (unchanged, tag
`part-4e-technical-analytics`). No commit, tag or push was made. Exact changed
files (`git status --short`) are reproduced at the end of the accompanying
final report delivered in conversation.
