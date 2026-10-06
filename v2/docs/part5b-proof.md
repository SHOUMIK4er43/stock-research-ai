# Part 5B implementation and proof

Verified locally: 602 full tests passed (549 from Parts 1-5A, unchanged, plus 53 Part 5B
tests: 39 in `test_analysis.py`, 14 in `test_analysis_pipeline.py`, including three added
during a follow-up review pass to prove analysis persistence is atomic — see Persistence,
below). Ruff passed on the full tree. Mypy passed across 69 source files. A fresh offline
proof (`output/part5b-proof-atomic-fix`) ran both required tracks and the production-CLI
provider-unavailable check. No dependencies were added. No commit, tag, or push was made
as part of this implementation pass.

## Scope

Part 5B is the **Institutional Analyst Engine** — an interpretation layer that produces
structured, evidence-cited `AnalystClaim`s over Parts 1-5A evidence. It creates no new
source facts, no recommendation, no final report, no challenger, no browser extension.

## Non-goals

No BUY/SELL/HOLD, target price, or investment recommendation. No final report/brief/JSON
generation (Part 6A). No independent challenger (Part 5C). No browser extension (Part 6B).
No new deterministic financial/market formula is ever computed by the analyst — every
number a claim cites must already exist as a canonical stored value somewhere in Parts
4A-5A.

## Evidence taxonomy (13 kinds, exactly as approved)

`COMPANY`, `FINANCIAL`, `SHAREHOLDING` (same owning extraction as `FINANCIAL`, never a
fabricated separate snapshot), `MARKET`, `FINANCIAL_CALCULATION`, `SECTOR_KPI`, `PATTERN`,
`MARKET_CALCULATION`, `TECHNICAL`, `VALUATION`, `DOCUMENT_CHUNK`, `PUBLICATION`, `MACRO`.
Each `EvidenceReference` carries `kind`, `owning_snapshot_id`, `item_key` (string —
UUID-stringified where the underlying item has its own id, or a natural key such as
`metric_id`/`reference_period`/ordinal index where it doesn't), `snapshot_payload_sha256`,
and `raw_artifact_sha256` where directly applicable (`FINANCIAL`, `MARKET`,
`DOCUMENT_CHUNK`, `PUBLICATION`, `MACRO`); derived kinds carry `None` there because their
raw lineage is verified transitively (see below), not duplicated.

## AnstInputBundle

Assembled only by `ResearchEngine.analyze_company`, from IDs the caller explicitly pinned
in `AnalysisRequest` — the analyst performs no discovery, no SQLite/Screener/Kite/document
querying of its own. `application/analysis.py::resolve_evidence` resolves every pin (or
raises `MissingEvidenceError`); `build_source_references` enumerates every individually
citable item across all pinned snapshots, sorted deterministically by
`(kind, owning_snapshot_id, item_key)` before hashing (`AnstInputBundle.canonical_order`
enforces this at the domain layer). `compute_section_coverage` runs immediately after,
before any model is ever called.

## Canonical hashing (`application/hashing.py`)

The exact expression `hashlib.sha256(value.model_dump_json().encode()).hexdigest()` was
already duplicated five times across `application/{valuation,patterns,sector_kpis,
market_calculations,calculations}.py` before this pass. It is now extracted once into
`application/hashing.py::digest()`, and all five call sites import it (two via explicit
`import ... as digest` re-export, so `valuation.digest(...)` and
`from stock_research_ai.application.valuation import digest` keep working unchanged).
Byte-identical output confirmed by the full existing Parts 1-5A test suite passing
unchanged (549/549, same as before this refactor) — no historical hash value was edited.

## INTERPRETATION-only enforcement

`AnalystClaimCandidate` (the *only* shape `AnstModel` may emit) has no
`epistemic_class`/`source_epistemic_classes`/`support`/`time_scope` field at all —
`extra="forbid"` on the shared `CalculationModel` base means a raw candidate dict
containing any of those keys fails Pydantic validation before it ever reaches the
validator logic (`test_model_cannot_supply_trusted_epistemic_or_support_fields`).
`AnalystClaim.epistemic_class` is `Literal["INTERPRETATION"]` — structurally, not by
convention. `source_epistemic_classes` is derived per-citation by
`analysis_validation.source_epistemic_class()`: FACT for `COMPANY`/`FINANCIAL`/
`SHAREHOLDING`/`MARKET`/`DOCUMENT_CHUNK`/`PUBLICATION`/`MACRO`; CALCULATION for the
derived-snapshot kinds; and for `VALUATION`, read per-item from the cited
`ValuationResult.definition.classification` (CALCULATION vs SCENARIO).

## Section coverage (safeguard 1C)

`SectionCoverage` is computed before any model call, independent of whatever claims are
later produced. The aggregation policy in `application/analysis.py::_aggregate`
implements exactly the five-case table from the approved safeguard: no relevant evidence
→ `UNAVAILABLE`; all `AVAILABLE` → `AVAILABLE`; mixed → `PARTIAL`; one distinct
non-available status → propagated verbatim; multiple distinct non-available statuses →
`UNAVAILABLE` with `reason="HETEROGENEOUS_NON_AVAILABLE_INPUTS"` and `upstream_statuses`
preserving every distinct code, sorted. `domain/analysis.py::SectionCoverage`'s own
validator enforces this contract structurally (a `SectionCoverage` with 2+
`upstream_statuses` that isn't `UNAVAILABLE`/`HETEROGENEOUS_NON_AVAILABLE_INPUTS` is
rejected at construction). Proved directly with fabricated status lists
(`test_heterogeneous_nonavailable_coverage_policy`,
`test_single_nonavailable_status_propagates_verbatim`,
`test_partial_coverage_when_mixed_available_and_not`) and against real evidence in the
fresh proof run (YESBANK `SECTOR_KPIS` → `PARTIAL`, since `DEPOSIT_GROWTH` and
`AVERAGE_ASSETS_RETURN` are `AVAILABLE` while `NIM`/`GNPA`/`NNPA`/`CASA_RATIO` are
`INSUFFICIENT_INPUT`).

`VALUATION` and `EXPECTATIONS` are additionally filtered by `ValuationResult.definition.
category` (`OBSERVED_MULTIPLE_CALCULATION`/`SCENARIO_CALCULATION` vs
`SCENARIO_EXPECTATION`) so a pinned DCF result can never be mistaken for coverage of the
`EXPECTATIONS` section, and vice versa. `VARIANT_PERCEPTION` requires both a pinned
`SCENARIO_EXPECTATION` valuation result and separate fundamental
(`FINANCIAL_CALCULATION`/`SECTOR_KPI`) evidence; `BULL_BASE_BEAR_INTERPRETATION` requires
a pinned valuation scenario that actually carries a caller-declared label. `BUSINESS_MODEL`
treats `DOCUMENT_CHUNK` as the only evidence that can make it `AVAILABLE`;
`COMPANY`/`FINANCIAL` registry facts are supporting-only and can produce at best
`INSUFFICIENT_INPUT` — proved directly
(`test_business_model_coverage_never_available_from_registry_facts_alone`) and in the real
proof (INFY, which has no document evidence, shows `BUSINESS_MODEL → INSUFFICIENT_INPUT`,
not `UNAVAILABLE`, because `COMPANY`/`FINANCIAL` facts are present as supporting-only
context).

## SupportAssessment

`basis ∈ {SINGLE_SOURCE, CORROBORATED_MULTI_SOURCE, CONFLICTING_EVIDENCE,
INSUFFICIENT_SUPPORT}` — no percentage, no score. `CONFLICTING_EVIDENCE` is only ever
flagged when citations share the same semantic metric key, period, and reporting scope,
and their canonical values disagree under the same unit family
(`_has_conflicting_evidence`); two citations lacking a stable comparable key (i.e. every
kind except `FINANCIAL_CALCULATION`/`SECTOR_KPI`/`MARKET_CALCULATION`) can never be called
conflicting. `INSUFFICIENT_SUPPORT` applies when every resolved citation's own upstream
status is non-`AVAILABLE`. `cites_unverified_semantics` is derived from each `FINANCIAL`/
`SHAREHOLDING` citation's actual `mapping_status`, never asserted by the model.

## Numeric validation

For every numeric token in a candidate statement, `extract_numeric_tokens` classifies it
into `PERCENT`, `BASIS_POINTS`, `MULTIPLE_X`, `CURRENCY`, or plain `NUMBER` by local
lexical pattern (`%`, `bps`/"basis points", trailing `x`, `₹`/`Rs`/`INR` prefix). A token
is accepted only if some cited reference's canonical `display_value` (never the raw
working-precision value) equals it exactly, with the same sign, in the same unit family.
No arithmetic — sums, differences, growth rates, unit conversions — is ever performed by
the validator. Proved: right number wrong sign rejected, right number wrong unit rejected,
a fabricated sum of two real cited values rejected, an uncited-but-real value from a
*different* citation in the bundle rejected, and a correctly-cited, correctly-signed,
correctly-unit-suffixed value accepted.

## Management quote policy

`ManagementStatement` exists only for QUOTE mode — `text` must be an exact substring of
the cited `DocumentChunk`'s actual text (`test_exact_management_quote_accepted`,
`test_altered_management_quote_rejected`). There is no PARAPHRASE variant; paraphrased
commentary is an ordinary `AnalystClaim` citing the chunk, with no faithfulness guarantee
implied (`test_paraphrase_is_ordinary_interpretation_not_a_management_statement`).
Guidance-vs-actual remains out of scope: no first-class guidance/consensus evidence layer
exists in the repository today; this is a later prerequisite evidence slice, not part of
5B, 5C, or 6A.

## Sector policy

`BANK_PROFILE` statements are lexically checked against industrial-company terms (`net
debt`, `EV/EBITDA`, `free cash flow yield`, `fcff`) and rejected if present
(`test_bank_industrial_language_rejected`); the identical statement is allowed under
`IT_SERVICES_PROFILE` (`test_same_statement_allowed_for_it_profile`). This is a lexical
defense-in-depth layer — the stronger, structural guarantee is that a `SECTOR_KPI`
citation can only exist in a bundle if that KPI's `applicable_profiles` includes the
request's profile (enforced by `sector_kpis.py`'s own eligibility gate, unchanged).
Confirmed by real evidence: most BANK KPIs (`NIM`, `GNPA`, `NNPA`, `CASA_RATIO`) are
registered but not formula-backed in Part 4C — this is a pre-existing Part 4C gap Part 5B
surfaces honestly (`INSUFFICIENT_INPUT`) rather than fills.

## Temporal policy

`as_of_policy` is fixed to `ANALYSIS_CURRENT_PINNED_BUNDLE_V1`. `TimeScope` keeps
`observation_period`, `reporting_scope`, and `evaluated_at` (the bundle's own assembly
time, never "now") distinct — never collapsed into one date. If any citation's own
upstream result carries `POINT_IN_TIME_AVAILABILITY_UNVERIFIED` (Part 5A's exact code),
the validator sets `point_in_time_unverified=True` and adds the code to the claim's
`limitations` — the model never has to remember this
(`test_temporal_limitation_propagated`). Statements implying a fact was historically known
at a market date (`"was already known"`, `"was known to the market"`, etc.) are rejected
whenever `point_in_time_unverified` is true
(`test_historical_known_wording_rejected_when_unverified`).

## Variant perception

`VariantPerception` requires a `SCENARIO_EXPECTATION`-category valuation citation, a
fundamental (`FINANCIAL_CALCULATION`/`SECTOR_KPI`) citation, and
`dependency_assumption_ids` that resolve exactly to the cited scenario's own assumption
IDs — a nonexistent assumption ID is rejected
(`VARIANT_PERCEPTION_ASSUMPTION_LINEAGE_INVALID`,
`test_variant_perception_requires_exact_assumption_lineage`). Real INFY/YESBANK proof
requests no `VARIANT_PERCEPTION` section at all (no real caller-declared scenario exists
for either company under Part 5A's own locked real-source scope); the mechanism is proved
only against the synthetic scenario, where it resolves `AVAILABLE`.

## Bull/base/bear

No new domain type — `BULL`/`BASE`/`BEAR` are exactly Part 5A's own free-text
`ValuationScenario.label`. `BULL_BASE_BEAR_INTERPRETATION` coverage requires a pinned
scenario with a label at all; "most likely"/"most probable"/"highest probability"/
"expected case" language is rejected whenever attached to a `BASE`-labeled scenario
(`BASE_LABEL_LIKELIHOOD_LANGUAGE`, `test_base_label_receives_no_probability_language`), and
the identical claim without likelihood language is accepted.

## Persistence

Single append-only `analysis_snapshots` table (`_AnalysisRow`/`SQLiteAnalysisRepository`),
same JSON-payload shape as `_ValuationRow`. `AnalysisSnapshot` persists the exact `request`,
`execution` spec, the **exact `input_bundle`** (not just its hash — reconstructable
forever, independent of whether any referenced row is later revised), `bundle_sha256`,
`sources`, `model_metadata` (full `RenderedModelRequest` + `RawAnalystResponse`,
including the literal template content and rendered text, not just their hashes),
`section_coverage`, accepted `claims`, `management_statements`, `rejected` candidates
with their validation issues, and `evaluated_at`.

**Atomic, transaction-scoped integrity verification.** `SQLiteAnalysisRepository.save_analysis`
opens exactly one `session`/`session.begin()` and, inside it: (1) reloads every one of the
snapshot's distinct pinned sources — across all thirteen `EvidenceKind`s — via
`_load_evidence_owner_in_session`, a neutral dispatch helper that reads the exact same
row classes (`_ExtractionRow`, `_MarketExtractionRow`, `_CalculationSnapshotRow`,
`_PatternSnapshotRow`, `_MarketCalculationRow`, `_ValuationRow`, `_DocumentExtractionRow`,
`_PublicationSnapshotRow`, `_MacroSnapshotRow`, plus a shared `_load_company` helper now
used by both `SQLiteCompanyRepository.get` and this check) already defined in this module,
and recomputes `digest()` against `snapshot_payload_sha256`; (2) for every derived kind
(`FINANCIAL_CALCULATION`, `SECTOR_KPI`, `PATTERN`, `MARKET_CALCULATION`, `TECHNICAL`,
`VALUATION`), `_verify_upstream_lineage_in_session` walks — using the same open
session/transaction — to that snapshot's own originating extraction(s)/market
extraction(s) via its own already-existing lineage fields (`request.extraction_id`,
`source.extraction_id`, `ValuationSource.snapshot_id`, recursing one further hop for a
`VALUATION`'s own cited `CALCULATION`-kind source) and re-verifies those against their own
recorded hashes too; only if every check passes does `session.add(_AnalysisRow(...))` run,
still inside the same transaction. No financial/calculation/technical formula is
reimplemented or recomputed anywhere in this path — every step is a row read plus a
SHA-256 digest comparison. Because the checks and the insert share one transaction, a
failure raises before `session.add` is ever reached and the `async with ... session.begin()`
context rolls back automatically — there is no window in which another writer's change
between "check" and "append" could go unnoticed, and no partial row is ever written.

This replaces the prior design (verification through separate sibling-repository sessions,
then a later, separate insert), which was flagged as a theoretical TOCTOU gap during
review. It is fixed, not a documented limitation: `test_snapshot_payload_corruption_
refuses_insert_atomically` proves it directly — a calculation snapshot's DB row payload is
tampered with, via raw SQL, *after* a valid `AnalysisSnapshot` object has already been
built (i.e., after any earlier, separate check would have already run and passed), and
`SQLiteAnalysisRepository.save_analysis` is then called directly and still refuses,
because its own in-transaction re-read sees the tampered row. `test_failed_integrity_
check_leaves_row_count_unchanged` proves the `analysis_snapshots` row count is identical
before and after a refused append (no partial insert); `test_successful_append_and_old_
snapshots_remain_reconstructable` proves a second successful append leaves the first row
byte-identical and both remain queryable through `analysis_history`.

Raw *artifact byte* integrity (as opposed to DB row integrity) remains a read-time check
via `RawArtifactStore.read()`, called by `ResearchEngine.analyze_company` before the model
is even invoked — this is not a TOCTOU-prone check to begin with, since it re-hashes the
actual file bytes at the moment of reading them and those bytes are never reused
afterward; there is nothing later for a race to invalidate. Proved: pinning **only** a
`FINANCIAL_CALCULATION` snapshot (no `FINANCIAL` evidence pinned at all), then corrupting
the raw bytes underneath its *originating* extraction, still refuses persistence
(`test_derived_snapshot_upstream_raw_corruption_refuses_persistence`); a second test
proves the simpler direct case (`test_direct_raw_artifact_corruption_refuses_persistence`).

## Model request/response provenance

`ModelMetadata` carries `provider`, `model_id`, `model_version`, the full
`RenderedModelRequest` (bundle hash, `PromptTemplate` with its literal `content` and
`content_sha256`, actual non-secret `generation_parameters` and their hash, the literal
`rendered_text` and its hash), and the full `RawAnalystResponse` (the literal
`structured_payload` and its hash) plus `generated_at`. No secret ever appears in any of
these fields; `AnstExecutionSpec` (template selection + generation parameters) carries no
API key or vendor SDK object, and `AnalysisRequest` carries no provider configuration at
all. Rejected candidates are never discarded — `AnalysisSnapshot.rejected` retains the
exact raw candidate plus its `ValidationIssue`s
(`test_rejected_candidate_persisted_alongside_accepted`).

## Real-vs-synthetic proof separation

**Track 1 — Real Retained Evidence (INFY, YESBANK).** Only genuinely real fixtures:
Screener extractions, Part 4A/4C calculations, Part 5A observed-multiple valuations, real
shareholding data, and — for YESBANK only — the real retained investor-presentation PDF.
`MARKET_PERFORMANCE`, `TECHNICAL_STATE`, `INDUSTRY_AND_MACRO`, `EXPECTATIONS`,
`VARIANT_PERCEPTION`, and `BULL_BASE_BEAR_INTERPRETATION` are **never requested** for
either real company, because no genuine retained evidence supports them (confirmed by a
fresh read-only Part 3C inventory, below).

**Track 2 — Synthetic Mechanical Proof.** A fully synthetic company/scenario (reusing Part
5A's own `tests/fixtures/valuation/synthetic.html`, plus a labeled `BASE` DCF scenario and
a labeled `MARKET_IMPLIED` reverse-DCF scenario) exercises exactly the sections and
validator paths real evidence cannot yet support. The two tracks are never combined in one
result — the fresh proof's `proof.json` reports them as separate, explicitly labeled
entries.

## Part 3C real-coverage inventory (verified, not assumed)

Re-confirmed by direct inspection for this pass: the only retained `PublicationSnapshot`/
`MacroSnapshot` evidence in the repository is generic synthetic fixtures
(`tests/fixtures/context/publication_request.json` → a synthetic RBI press release tagged
only `{kind: SECTOR, identifier: "banking"}`, caller-declared, never verified; synthetic
CPI macro data with no company or sector linkage at all) plus one ad-hoc, non-retained
test-only synthetic PDF in `test_context.py` with a caller-declared `{kind: COMPANY,
identifier: "YESBANK"}` tag that is never tied to YESBANK's actual registered
`company_id`. **No real, verified-linkage publication or macro evidence exists for either
company.** Both real proof runs therefore correctly report:

```
INFY    INDUSTRY_AND_MACRO -> UNAVAILABLE
YESBANK INDUSTRY_AND_MACRO -> UNAVAILABLE
```

## Production capability

`Capabilities.analysis_available` is now `bool` (was `Literal[False]`), computed as
`self._analyst is not None and self._analyst.provider_kind == "LIVE"`.
`FakeAnalystModel` lives only under `tests/`; `bootstrap.py`'s production wiring path has
no import of it. `bootstrap.analyze_company` requires the caller to pass **both** `model`
and `provider_kind` explicitly — omitting either (which is what the CLI always does, since
no `LIVE` adapter ships) raises `RuntimeError` before any database is even opened. The real
CLI, run with no injected model, was proved in the fresh run to exit **4** with
`"provider unavailable"` in its stderr — never silently substituting a fake
(`test_production_bootstrap_never_uses_fake_model`,
`test_cli_reports_provider_unavailable_without_secretly_using_fake`,
`test_analysis_available_false_without_live_provider`).

## CLI

```sh
stock-research-ai --database PATH analyze-company \
    --request request.json --execution execution.json --output PATH
```

Routes only through `ResearchEngine.analyze_company`; no provider logic in `__main__.py`.
Exit codes: `0` (analysis completed, including any honest mix of `AVAILABLE`/`PARTIAL`/
`INSUFFICIENT_INPUT`/`NOT_APPLICABLE`/`UNAVAILABLE` coverage — never treated as failure),
`2` (malformed request), `4` (model/provider unavailable or execution failure — the only
code reachable in this build, since no `LIVE` provider ships), `5` (validator/system
failure, reserved for when a provider is wired), `6` (persistence/integrity failure).

## Real INFY proof (fresh run)

| Section | Status |
|---|---|
| COMPANY_OVERVIEW | AVAILABLE |
| HISTORICAL_FUNDAMENTALS | AVAILABLE |
| SECTOR_KPIS | AVAILABLE (`SALES_GROWTH`, `NET_PROFIT_GROWTH`, `OPERATING_MARGIN` all computed) |
| VALUATION | PARTIAL (`PRICE_TO_EARNINGS` available; `PRICE_TO_BOOK` refuses `UNVERIFIED_METRIC_SEMANTICS`, per Part 5A's own locked semantics) |
| OWNERSHIP_AND_SHAREHOLDING | AVAILABLE (point-in-time; the retained extraction has one shareholding snapshot) |
| BUSINESS_MODEL | INSUFFICIENT_INPUT (no document evidence for INFY; only registry/financial facts, which are supporting-only) |
| MANAGEMENT_AND_DOCUMENT_INTELLIGENCE | UNAVAILABLE (no document chunks pinned) |
| INDUSTRY_AND_MACRO | UNAVAILABLE (per the Part 3C inventory above) |

4 claims accepted, 0 rejected.

## Real YESBANK proof (fresh run)

| Section | Status |
|---|---|
| COMPANY_OVERVIEW | AVAILABLE |
| HISTORICAL_FUNDAMENTALS | AVAILABLE |
| SECTOR_KPIS | PARTIAL — `DEPOSIT_GROWTH` and `AVERAGE_ASSETS_RETURN` AVAILABLE; `NIM`, `GNPA`, `NNPA`, `CASA_RATIO` all `INSUFFICIENT_INPUT` (registered, not yet formula-backed in Part 4C) |
| VALUATION | PARTIAL (`PRICE_TO_EARNINGS` available; `PRICE_TO_BOOK` refuses `UNVERIFIED_METRIC_SEMANTICS`) |
| OWNERSHIP_AND_SHAREHOLDING | AVAILABLE (point-in-time only) |
| BUSINESS_MODEL | AVAILABLE (from the real retained YES BANK Q1FY26 investor presentation) |
| MANAGEMENT_AND_DOCUMENT_INTELLIGENCE | AVAILABLE (same real document) |
| INDUSTRY_AND_MACRO | UNAVAILABLE |

5 claims accepted, 0 rejected. This asymmetry (YESBANK richer in document coverage, INFY
richer in nothing YESBANK lacks structurally — both share the same sector-KPI/valuation
gaps in different ways) is exactly the proof that BANK and IT_SERVICES produce materially
different, evidence-driven analysis structures — not a hand-tuned outcome.

## Synthetic mechanical proof (fresh run)

`VALUATION`, `EXPECTATIONS`, `VARIANT_PERCEPTION`, and `BULL_BASE_BEAR_INTERPRETATION` all
resolve `AVAILABLE` against the synthetic fixture (observed P/E, a `BASE`-labeled DCF
scenario, and a `MARKET_IMPLIED`-labeled reverse-DCF scenario). 6 claims accepted, 0
rejected.

## Known limitations

Most BANK sector KPIs remain registered-but-not-formula-backed in Part 4C — a
pre-existing gap this pass surfaces, does not fix. No guidance/consensus evidence exists,
so guidance-vs-actual and consensus-based variant perception remain impossible until a
dedicated evidence-acquisition slice exists (a later prerequisite, no milestone assigned
here). Sector-language validation is lexical, not a semantic proof. `CATALYSTS`/`RISKS`
sections have no dedicated evidence type and can only be derivative of evidence already
cited elsewhere. `INDUSTRY_AND_MACRO` has zero real coverage for any company today.

The append-time source re-verification in `SQLiteAnalysisRepository` is no longer a
documented TOCTOU trade-off: all thirteen evidence kinds are now re-verified, including
derived-kind upstream lineage, inside the same transaction that performs the insert (see
Persistence, above). The remaining, unavoidable characteristic — raw artifact *file* reads
still happen at the engine layer, outside any SQL transaction, because file bytes cannot
participate in a database transaction — is not a gap: that check re-hashes the actual
bytes at the moment of reading them and never reuses them afterward, so there is no later
window for a race to exploit.

## Reproduce

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 \
  uv run python scripts/verify_analysis.py --output output/part5b-proof-manual
```

Drives ingestion/calculation/valuation/document steps through the real production CLI
subprocess; `analyze-company` generation itself calls `bootstrap.analyze_company` directly
with `FakeAnalystModel` (the sanctioned offline path — the CLI itself is separately proved
to refuse with exit 4 when no model is injected, exactly as production behaves).

```sh
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run ruff check .
UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run mypy src
```

Results: **602 passed; Ruff all checks passed; mypy no issues in 69 source files**.

## Files and Git state

New: `src/stock_research_ai/domain/analysis.py`,
`src/stock_research_ai/application/{analysis,analysis_validation,hashing}.py`,
`tests/{test_analysis,test_analysis_pipeline}.py`, `tests/fakes/{__init__,analyst}.py`,
`scripts/verify_analysis.py`, this file.

Modified (integration wiring, additive): `src/stock_research_ai/application/ports.py`
(+`AnalystModel`, +`AnalysisRepository` Protocols, +2 `ContextRepository` getters),
`src/stock_research_ai/application/engine.py` (+`AnalystServices`, +`analyze_company`,
`Capabilities.analysis_available` widened from `Literal[False]` to `bool`),
`src/stock_research_ai/adapters/sqlite.py` (+`_AnalysisRow`, +`SQLiteAnalysisRepository`
with atomic transaction-scoped integrity verification across all 13 evidence kinds, +a
shared `_load_company` helper factored out of `SQLiteCompanyRepository.get` and reused by
that verification, +2 getters on `SQLiteContextRepository`),
`src/stock_research_ai/adapters/json_export.py` (+`export_analysis`),
`src/stock_research_ai/bootstrap.py` (+`analyze_company`),
`src/stock_research_ai/__main__.py` (+`analyze-company` subcommand), `README.md`.

Modified (hash-refactor only, zero output change, verified by the unchanged Parts 1-5A
suite — confirmed genuinely five duplicate sites at the frozen `e9c5cc1` baseline, via
`git show HEAD:<path> | grep hashlib`): `application/{valuation,patterns,sector_kpis,
market_calculations,calculations}.py`. `application/technicals.py` needed no change — at
the frozen baseline it never contained a `hashlib` call of its own; it already shared
`market_calculations.py`'s `source_reference()` helper (line ~479:
`source=market.source_reference(source)`), which is on the refactored list already.

Modified (deliberate, reviewed schema-inventory update — same class of change as Part 5A's
own): `tests/test_extraction.py` (added `"analysis_snapshots"` to the hardcoded table set).

No Part 1-5A calculation semantics changed anywhere.

Final Git state: branch `main`, HEAD `e9c5cc1` (unchanged), tag
`part-5a-valuation-expectations`. No commit, tag, or push was made.
