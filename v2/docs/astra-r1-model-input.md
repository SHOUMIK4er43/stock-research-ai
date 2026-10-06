# ASTRA R1 — Live analyst evidence content contract

Review state: implementation pending review; no commit authorized.
Baseline: `8453c72b265cb08b43c7b882726a9e941c977355` on `astra-development`.

## Confirmed defect and root cause

The frozen Part 5B type is named `AnstInputBundle` in code. It contains
`schema_version`, `bundle_id`, `request`, `assembled_at`, `sources`, and
`section_coverage`. Its request contains company/classification/profile identifiers,
exact snapshot/chunk/period selections and requested sections. Each source contains
kind, owning snapshot ID, optional item key, owner payload hash and optional raw
artifact hash. Coverage contains status, reason, contributing kinds and upstream
statuses.

Before R1, `ResearchEngine.analyze_company` resolved the actual evidence and verified
raw artifacts locally, then passed only this bundle to `AnalystModel.generate`.
The production adapter serialized only `bundle.model_dump(mode="json")` into its
user message. Its system instructions, tool schema and generation settings supplied
no missing research content. Financial observations, calculation results, valuation
values and selected document text remained in local `ResolvedEvidence`. Validators
could check output against that content, but the model could not read it.

```text
BEFORE
Pinned request → ResearchEngine.resolve_evidence → local ResolvedEvidence
                          │                              │
                          └→ freeze AnstInputBundle      │
                                  │                     │
                     model: IDs/hashes/coverage only    │
                                  ↓                     │
                           raw model response           │
                                  └→ deterministic validators ←┘
                                           ↓
                                append-only AnalysisSnapshot

AFTER
Pinned request → resolve evidence + verify raw artifacts
                          ↓
              freeze the SAME AnstInputBundle
                          ↓
        build_model_input(bundle, already-resolved evidence)
        recheck hashes → exact-item projection → budgets
                          ↓
       AnalystModel.generate(bundle, execution, model_input=projection)
                          ↓
       adapter: canonical {bundle, model_input} JSON → model
                          ↓
       raw response → UNCHANGED local deterministic validators
                          ↓
       AnalysisSnapshot, including exact rendered input + its hash
                          ↓
       Part 5C: same frozen sources, independently resolved/checked
                          ↓
       Part 6A: unchanged report semantics
```

## New contract

`domain/analyst_model_input.py` defines immutable `AnalystModelInput`:

- `projection_version`: literal `ANALYST_MODEL_INPUT_V1`.
- `bundle_id`: unchanged frozen UUID.
- `bundle_sha256`: the existing `digest(bundle)`, not a new serialization hash.
- `items`: tuple of `AnalystModelEvidence`, one per source, in source order.

Every item has `reference` (the full existing `EvidenceReference`), `status`,
`source_epistemic_class` (the existing locally computed source classification),
`limitations` (existing upstream issue codes), and `content_json` (immutable
canonical JSON text). The adapter serializer decodes `content_json` into the wire
field `content`; it does not send double-encoded JSON. Source classification does
not promote model output: accepted claims remain `INTERPRETATION`.

The application owns projection and serialization; the provider port adds required
keyword `model_input`. The Anthropic adapter cannot silently fall back to references
only. Existing test fakes accept it; the shared fake retains its old two-argument
form only for existing synthetic snapshot-construction tests.

### Exact content selection

The executable field allowlists are `_OBSERVATION_FIELDS`, `_RESULT_FIELDS` and
`_content` in `application/analyst_model_input.py`. Nested values below retain the
existing domain JSON structure; no new financial arithmetic is performed.

| Kind | Selected content |
| --- | --- |
| COMPANY | `id`, `name`, `listings`, `provider_identifiers` |
| FINANCIAL / SHAREHOLDING | `id`, `company_id`, `metric`, `metric_id`, `mapping_status`, `original_label`, `series_key`, `publication_at`, `source_revision`, `value`, `value_status`, `unit`, `period`, `reporting_scope`, `provenance`, `original_value`, `original_unit` |
| FINANCIAL_CALCULATION | `classification`, `snapshot_id`, `extraction_id`, `metric_id`, `label`, `formula`, `alignment`, `sector`, `period`, `comparison`, `reporting_scope`, `calculated_at`, `status`, `value`, `display_value`, `exact_numerator`, `exact_denominator`, `unit`, `issues` |
| SECTOR_KPI | `classification`, `snapshot_id`, `extraction_id`, `definition`, `profile`, `sector`, `alignment`, `period`, `comparison`, `reporting_scope`, `calculated_at`, `status`, `value`, `display_value`, `exact_numerator`, `exact_denominator`, `issues` |
| PATTERN | `id`, `snapshot_id`, `definition`, `evaluation_status`, `rule_outcome`, `observed`, `issues`, `statement` |
| MARKET | One `observation`: `id`, `instrument_id`, `trading_date`, `candle_timestamp`, `provider_updated_at`, `open`, `high`, `low`, `close`, `volume`, `volume_semantics`, `adjusted_close`, `adjustment_basis`, `session_status`, `provenance`; owning `instrument` identity and `knowledge_basis` |
| MARKET_CALCULATION | `classification`, `snapshot_id`, `metric_id`, `formula`, `status`, `calculated_at`, `requested_start`, `requested_end`, `actual_start`, `actual_end`, `alignment_policy`, `price_field`, `adjustment_basis`, `corporate_action_coverage`, `corporate_action_reason`, `issues`, `value`, `display_value`, `exact_numerator`, `exact_denominator`, `numerical_method`, `unit`, `currency`, `annualization_policy`, `elapsed_days`, `elapsed_years`, `observation_count`, `return_count`, `annualization_factor`, `return_definition`, `variance_definition`, `window`, `episode`, `company_price_return`, `benchmark_price_return` |
| TECHNICAL | Owning series `definition`, `unit`, `currency`, `numerical_method`, `issues`; one `point`: `id`, `indicator_id`, `definition_version`, `date`, `status`, `value`, `display_value`, `numerical_method`, `observation_ids`, `seed_observation_ids`, `previous_point_id`, `dependent_point_ids`, `issue` |
| VALUATION | `id`, `snapshot_id`, `selection_index`, `definition`, `status`, `assumption_ids`, `scenario_id`, `values`, `solver`, `issues`; this result's exact `selection` (including existing scenario assumptions and identifier pins), owner `reporting_scope` and `evaluated_at` |
| DOCUMENT_CHUNK | Owner `identity`, `version_id`, `title`, `document_type`, `classification_basis`, `publication_date`, `fiscal_label`, `source_reference`, `parser_version`; one `chunk`: `id`, `version_id`, `artifact_id`, `page_id`, `page_number`, `section_id`, `ordinal`, `start_char`, `end_char`, `text`, `text_sha256`, `character_count`, `word_count` |
| PUBLICATION | Owner `id`, `identity`, `title`, `publication_date`, `published_at`, `provider_updated_at`, `reference_period`, `retrieved_at`, `normalizer_version`; one 1-based indexed `passage`: `text`, `provenance` |
| MACRO | Owning `series` identity/semantics; one exact-period `observation`: `series_id`, `reference_period`, `value`, `source_status`, `revision_status`, `published_at`, `provider_vintage_at`, `retrieved_at`, `provenance` |

No nested input observations, sibling results, entire document pages, attachment
bodies, retrieval request parameters, raw artifact bytes or corpus search results
are projected. Associated identity, formula, unit, scenario and source-reference
metadata are from the same hash-verified owner. Identifier links do not authorize
citing or dereferencing additional evidence.

## Closure, immutability and provenance

The builder restricts local owners to frozen references, regenerates reference
metadata from those owners, and requires exact equality with every frozen
reference, including owner and available raw hashes. Actual item lookup must match
exactly once. Document and macro selections are intersected with the original
request. Only frozen source items are emitted; there is no repository lookup,
latest-snapshot fallback, fuzzy match or automatic replacement.

The original bundle is never changed. A local copy of its request is used only to
restrict reference checks for selected chunks/periods. Existing `digest`, bundle
schema, snapshot schema, SQLite tables/transactions, citation/numeric/quote
validators, challenger, report code and valuation engines are unchanged.

The adapter checks bundle ID/hash and the complete ordered reference tuple again.
The exact canonical wire JSON is included verbatim in `RenderedModelRequest.rendered_text`;
the existing `rendered_sha256` covers that text and instructions. The existing
`bundle_sha256` still hashes the original bundle with the original algorithm.
Append-only snapshot persistence retains accepted/rejected output and model
provenance. This projection is transport data, not a second source of truth or a
new authority for claims. Adapter binding checks are not an independent proof of
content correctness: trusted application construction resolves the owner content,
while existing validators independently judge model output against local evidence.

## Determinism, nulls and budgets

- Item order is the frozen source order: kind, owner UUID string, item key.
- Object keys sort lexicographically; separators are compact; UTF-8 is emitted
  without ASCII escaping. Existing domain tuple order is retained.
- Domain JSON serialization preserves Decimal values as strings, UUIDs as strings,
  enums as values and dates/times in their existing ISO format. Floating-point JSON
  content is rejected. No scaling, rounding, imputation or new calculation occurs.
- Null remains null. Financial nulls carry `INSUFFICIENT_INPUT`, or `NOT_APPLICABLE`
  when explicitly so reported, plus original `value_status`. Derived results retain
  their exact status and empty/null outputs. Unverified semantic mappings remain
  visible; they are not upgraded. Unknown publication times and scopes stay unknown.
- Selected chunk text and offsets are copied exactly. No generated quote,
  attribution, excerpt expansion, summary or speaker identity is added. Existing
  quote validation remains authoritative.
- V1 cap: **2,097,152 UTF-8 bytes** for the complete canonical `{bundle, model_input}`
  user message; **2,000 items**; **32 document chunks**; **16,384 UTF-8 text bytes per
  selected chunk**. The fixed instructions/tool schema and model output are outside
  this user-message byte cap. Publication text is bounded by the total byte cap.
- Overflows fail with `ModelInputError` before transport. There is no truncation,
  sampling, silent omission or retry with a wider/different bundle. Unresolved,
  ambiguous or hash-mismatched evidence fails the same way. Engine-level failure
  precedes both model invocation and analysis insertion.

The initial 1 MiB candidate cap rejected the existing offline financial fixture.
The measured fixture has 738 items and approximately 1.34 MB of JSON; the explicit
V1 cap was set to 2 MiB. Identical frozen bundle and repository state produce
identical input; new bundle UUIDs or timestamps correctly produce different input.

## Verification scope and remaining limitations

Tests first demonstrated absent passage content with `httpx.MockTransport`, then
20 red contract cases before production implementation. Expanded regression tests
cover all 13 evidence kinds, values/periods/units/scopes at the actual adapter wire,
exact Decimal digits, stable ordering, no sibling/extra-owner expansion, missing or
changed pins, duplicate items, all four budgets, null/status honesty, unsupported
citation/numeric/quote rejection, persisted request provenance and Part 5C closure.
Golden expected truth is unchanged. Verification uses temporary databases/artifacts
and replay/synthetic evidence only.

Byte budgets are deterministic transport limits, not provider token-window or cost
guarantees. Large inputs may still exceed a configured model's context window.
Live output quality, context fit and provider behavior remain unverified. Future
selection changes must occur before bundle freezing and require separate review.
The existing validator accepts only its established numeric vocabulary; presenting
additional pinned fields does not authorize new arithmetic or broader claims.
Persisted rendered requests now contain evidence text and therefore increase local
storage and model-provider data volume. Third-party AnalystModel implementations
must accept the new required keyword. No dependencies were added.

Part 6C-C remains paused for the existing Anthropic insufficient-credit condition,
classified `PROVIDER_CONFIGURATION`. R1 is not a billing workaround and does not
constitute live release validation. No live Screener or Anthropic request, production
database access, commit, push, tag or Git history rewrite is part of this change.

## Final offline verification

All verification used the existing virtual environment with `UV_NO_SYNC=1`,
`UV_FROZEN=1`, and `PYTHONDONTWRITEBYTECODE=1`. Python test runs additionally used
`UV_OFFLINE=1`, a sanitized environment and an external working directory; pytest
cache writes were disabled. No dependency installation or synchronization occurred.

| Check | Result |
| --- | --- |
| R1 + Parts 5B, 5C, 6A, 6B0 targeted suites | 213 passed, including 29 new R1 cases |
| Full pytest | 960 passed in 222.29 seconds; 7 existing Starlette HTTP 422 deprecation warnings |
| Ruff (`check --no-cache .`) | All checks passed |
| Strict mypy (`--strict src/stock_research_ai`, external cache) | No issues in 96 source files |
| Extension (`node --test extension/tests/*.test.js`) | 165 passed, zero failures/skips |
| `git diff --check` | Clean |

The full baseline was 931 passing Python tests; R1 adds 29, with no golden edits.
