# Part 6B0 implementation and proof — Live Research Execution Adapter

Verified locally: 745 full tests passed (649 from Parts 1-5C, unchanged, plus 57 Part 6A
tests, unchanged, plus 39 new Part 6B0 tests: 10 in `test_sector_resolution.py`, 7 in
`test_document_topics.py`, 17 in `test_anthropic_analyst.py`, 5 in `test_research_plan.py`).
Ruff passed on the full tree. Mypy passed across 83 source files (77 + 6 new: `adapters/
screener/sector.py`, `application/sector_resolution.py`, `application/document_topics.py`,
`application/research_plan.py`, `adapters/anthropic/__init__.py`, `adapters/anthropic/
analyst.py`). No dependencies were added. No commit, tag, or push was made.

## Scope

Part 6B0 makes the existing research system genuinely executable end-to-end for a real
company, without altering any Part 1-6A semantics:

```
real Screener company -> genuine evidence acquisition -> deterministic sector resolution
  -> deterministic calculations/valuation -> Part 5B live AnalystModel
  -> Part 5C deterministic challenge -> Part 6A deterministic report
```

It is not Chrome extension work, not browser API work, not new financial/valuation/
challenge/report semantics, not a new LLM analyst architecture.

## 1. Live AnalystModel: `adapters/anthropic/analyst.py`

`AnthropicAnalystModel` implements the existing, frozen `application.ports.AnalystModel`
Protocol exactly (`async def generate(bundle, execution) -> ModelMetadata`). It owns only
transport: an authenticated HTTPS request to `POST /v1/messages`, forced tool-use
(`tool_choice={"type":"tool","name":"emit_claims"}`) against a hand-authored JSON Schema
mirroring `AnalystClaimCandidate`, provider response parsing, provider metadata
extraction, and a typed failure taxonomy (`AUTHENTICATION_FAILED`, `PERMISSION_DENIED`,
`RATE_LIMITED`, `TIMEOUT`, `PROVIDER_UNAVAILABLE`, `MALFORMED_RESPONSE`,
`UNSUPPORTED_RESPONSE_SCHEMA`, `RESPONSE_TOO_LARGE`, `MODEL_UNAVAILABLE`). Bounded retry
(max 3 attempts, 1s/4s backoff) only for `TIMEOUT`/429/5xx; no retry for auth/schema/
model errors. Exactly one model call per `generate()` - no agent loop.

It does **not** own research prompt semantics: the actual `PromptTemplate`/
`RenderedModelRequest`/`RawAnalystResponse`/`ModelMetadata` contract is Part 5B's own,
unchanged, and the adapter's own instructional text is a short, generic, transport-level
wrapper (`_TRANSPORT_INSTRUCTIONS`) - there is no `adapters/anthropic/prompts.py` and no
second copy of research semantics. All model output remains untrusted until the existing,
frozen `application.analysis_validation.validate_response` accepts it.

A Claude Pro/Claude Code subscription is **not** an Anthropic API credential. Production
use requires a separately configured Anthropic Console API key, read only from
`Settings` (`STOCK_RESEARCH_AI_ANALYST_PROVIDER`, `_ANALYST_API_KEY`, `_ANALYST_MODEL`,
`_ANALYST_BASE_URL`, `_ANALYST_TIMEOUT_SECONDS`, `_ANALYST_MAX_OUTPUT_TOKENS`) - never
persisted, logged, or placed in provenance/exports (`test_api_key_never_appears_in_any_
persisted_representation`).

`bootstrap.live_analyst_model(settings)` mirrors the existing `KiteSource.from_
environment()` convention exactly: absent configuration returns `(None, None)`;
unsupported `ANALYST_PROVIDER` raises explicitly; `FakeAnalystModel` is never imported
outside `tests/`. `Capabilities.analysis_available` is unchanged - it already means
"an `AnalystServices` with `provider_kind=="LIVE"` is wired," which is now genuinely
achievable.

## 2. Sector resolution: `adapters/screener/sector.py` + `application/sector_resolution.py`

**Audit finding**: real Screener pages already carry a deterministic "Broad Industry"
breadcrumb inside the `#peers` section (`title="Broad Industry"`), confirmed present on
both real fixture captures: `Banks` (YESBANK) and `IT - Software` (INFY). No existing
code parsed it. No `SectorDeclaration`/`ProfileClassification` is constructed anywhere in
production `src/` before this milestone; every BANK/IT_SERVICES_PROFILE classification in
the existing test suite is hand-declared by the test author (`test_calculations.py::
request()`).

**Design constraint discovered mid-implementation**: `application/sector_resolution.py`
is under `application/`, and `tests/test_engine.py::test_core_imports_only_domain_
application_pydantic_and_standard_library` forbids `application/*` from importing any
`adapters.*` module. The first implementation attempt (resolver calling the Screener HTML
parser directly) failed this frozen test. Fixed by adding one new, additive Protocol,
`application.ports.BroadIndustrySource` (`extract(content, encoding) -> tuple[str,
SourceLocation] | None`), which `adapters/screener/sector.py::ScreenerBroadIndustrySource`
implements and `bootstrap.py` injects - the same dependency-injection shape every other
adapter capability in this codebase already uses. This is the one file outside the
originally-proposed list (`config.py`, `bootstrap.py`, `__main__.py`, plus the Screener
adapter composition) that needed touching, and it is purely additive: one new Protocol,
zero changes to any existing Protocol/class in `ports.py`.

**Resolution tiers**, exactly as specified:
- **A (verified/existing profile)**: not yet implemented - no persisted company->profile
  registry exists to consult.
- **B (deterministic mapping)**: `SCREENER_INDUSTRY_PROFILE_MAP_V1 = {"Banks": ("BANK",
  BANK_PROFILE), "IT - Software": ("NON_FINANCIAL", IT_SERVICES_PROFILE)}` - an exact
  (never fuzzy/substring) match against the label read from the SAME already-acquired
  Screener artifact `extract_company` retrieved (re-read via the injected `RawArtifactStore`
  - no second web fetch). A match produces `ProfileClassification(basis="VERIFIED_MAPPING",
  provenance=...)` with real `ObservationProvenance` pointing at the exact page anchor.
- **C (unmapped/absent)**: `UNKNOWN`/`CALLER_DECLARED`, sector KPI evaluation skipped for
  that company only (not for every company, per the standing correction).

`application/sector_kpis.py` was **not** modified - `BANK`/`IT` constants and `PROFILES`
are imported and reused read-only.

## 3. Document intelligence

### 3a. Discovery vs. acquisition vs. selection - final audit

| Type | Discovery | Acquisition | Type classification | Auto-eligible for chunk selection |
|---|---|---|---|---|
| ANNUAL_REPORT | live (Part 2, unchanged) | live | exact | yes |
| QUARTERLY_RESULT | falls under ANNOUNCEMENT/OTHER | live | **not distinguished** (kept as limitation, not widened) | only as ANNOUNCEMENT/OTHER |
| INVESTOR_PRESENTATION | live | live | exact | yes |
| CONCALL_TRANSCRIPT | live | live | exact | yes |
| AGM | falls under ANNOUNCEMENT/OTHER | live if discovered | **not distinguished** (kept as limitation) | only incidentally |

Confirmed via a real fixture probe: YESBANK's real page discovers 86 documents (35
investor presentations, 17 concall transcripts, 15 annual reports, 7 other, 6
announcements, 6 credit ratings); INFY discovers 76. None carry a `<time>` tag in these
particular fixtures, so every selected document triggers `DOCUMENT_RECENCY_UNVERIFIED`
(§4 below) rather than being silently called "most recent."

### 3b. Ingestion caps (`application/research_plan.py::DOCUMENT_INGESTION_CAPS`)

```
ANNUAL_REPORT: 3   INVESTOR_PRESENTATION: 4   CONCALL_TRANSCRIPT: 4
EARNINGS_PRESENTATION: 4   CREDIT_RATING: 2   ANNOUNCEMENT: 2   OTHER: 2
```

QUARTERLY_RESULT/AGM-specific counts were **not** locked to the illustrative "4 quarters"
figure: `DocumentReference`/`ParsedDocument` do not carry a machine-usable fiscal period
for discovery-time documents (only an optional raw `publication_at` timestamp), so a
period-based cap would overstate precision the evidence doesn't have. Document-count caps
per existing `DocumentType` are the honest V1 mechanism.

### 3c. Ordering (§4 correction)

`_select_documents_for_ingestion` sorts by `publication_at` descending first; documents
without a publication date are appended in discovery order, never relabeled "recent." Any
selection containing an undated document emits a typed `DOCUMENT_RECENCY_UNVERIFIED`
warning (`stage="EVIDENCE"`).

### 3d. Deterministic chunk selection: `application/document_topics.py`

**Existing retrieval audit**: chunk storage (`ParsedDocument.chunks`) and search
(`ResearchEngine.search_company_documents` -> plain case-insensitive substring match, no
ranking, no type/period filter) both already exist. No BM25, no semantic retrieval, no
type/period filtering at the repository layer.

**Design chosen**: rather than querying that repository-wide search and post-filtering
(which would need extra work to guarantee run-scope isolation), `match_document_topics`
operates directly on the `ParsedDocument` objects **this run itself just ingested** -
still the exact same substring-match primitive, but run-scope isolation holds by
construction, not by an added filter (`test_run_scope_isolation_only_passed_documents_
are_considered`).

Three topics - the smallest set, each mapped 1:1 onto a real, existing `AnalysisSection`
(`domain.analysis.AnalysisSection`), not invented names:

```
BUSINESS_MODEL_V1  -> BUSINESS_MODEL                  (ANNUAL_REPORT, INVESTOR_PRESENTATION)
RISKS_V1            -> RISKS                           (ANNUAL_REPORT)
MGMT_DOC_INTEL_V1    -> MANAGEMENT_AND_DOCUMENT_INTELLIGENCE (CONCALL_TRANSCRIPT, INVESTOR_PRESENTATION)
```

`EXPECTATIONS`/`CATALYSTS` were deliberately excluded: selecting chunks for them via
keyword search would look like guidance extraction, which this milestone does not
implement (see §3e).

Budgets: `max_chunks` 4/4/6 per topic, `per_document_cap` 2, `TOTAL_CHUNK_BUDGET_V1 = 14`
hard ceiling across every topic combined - enforced deterministically
(`test_per_document_cap_and_topic_max_chunks_are_enforced`, `test_total_chunk_budget_
caps_across_topics`).

Per selected chunk: `document_version_id`, `chunk_id`, `document_type`,
`publication_date`, `fiscal_label`, `topic_id`, `matched_term`, `rank` - never anything
that reinterprets the chunk's epistemic status.

### 3e. Management/guidance safety

A `SelectedChunk` is only ever this reference metadata (`test_selected_chunk_carries_
only_reference_metadata_never_a_management_statement` asserts its exact field set). It
becomes a `ManagementStatement` only if Part 5B's own frozen `validate_response` accepts
an explicit `ManagementQuoteCandidate` citing it - unchanged. Query terms never include
guidance-style words (`expect`/`guide`/`target`); `test_guidance_style_wording_is_not_
selected_by_an_unrelated_topic` confirms a chunk containing such wording is not selected
by an unrelated topic merely because it "sounds like" guidance.

### 3f. Wiring into `document_chunk_refs` - no Part 5B change

Selected `(document_version_id, chunk_id)` pairs are passed into `AnalysisRequest.
document_chunk_refs` exactly as a human test author already does today - `resolve_
evidence`/`assemble_bundle` (both frozen, unmodified) turn them into ordinary
`EvidenceReference(kind="DOCUMENT_CHUNK")` entries, citable by any section, validated
identically. **Confirmed: this required zero changes to `domain/analysis.py`,
`application/analysis.py`, `application/analysis_validation.py`, or `application/
evidence_matching.py`** - the full offline proof in `test_research_plan.py` exercises this
exact path end to end.

## 4. `FULL_RESEARCH_V1`: `application/research_plan.py`

### Execution graph (actual repository operations used)

```
PREPARING: extract_company (Screener) -> resolve_sector (re-reads the SAME artifact)
EVIDENCE:  select+ingest documents (best-effort) -> match_document_topics (best-effort)
           -> calculate_sector_kpis if profile MAPPED (best-effort)
           -> evaluate_valuation, PRICE_TO_EARNINGS/PRICE_TO_BOOK only (best-effort)
ANALYSIS:  build AnalysisRequest -> analyze_company (REQUIRED, live AnstModel)
CHALLENGE: challenge_analysis (REQUIRED, deterministic)
REPORT:    generate_report (REQUIRED, deterministic)
COMPLETED: FullResearchResult
```

Required: valid supported Screener company, core Screener acquisition/extraction, Part
5B live analysis, Part 5C challenge, Part 6A report. Best-effort: documents, sector KPIs,
valuation - a genuine absence of evidence produces a partial, honestly-warned result, not
an aborted run; a real failure inside a best-effort stage is recorded as a distinct typed
warning (`*_FAILED`) rather than conflated with genuine absence (`*_NOT_ATTEMPTED_*`).

### Explicitly NOT attempted in this V1 (disclosed, not hidden)

`calculate_financials` and `evaluate_patterns` are not wired into `FULL_RESEARCH_V1`: a
correct, general, sector-agnostic request (which growth metrics, which pattern IDs)
cannot be constructed without company/sector-specific knowledge this milestone did not
audit - every existing caller of these two operations hand-authors that list today. Part
5B still receives substantial raw FINANCIAL evidence directly from the extraction
regardless. Market/context (Kite/publication/macro) acquisition is also not attempted:
it requires per-company instrument/series identifiers Screener evidence alone does not
provide generically. Both are recorded via a `CALCULATIONS_NOT_ATTEMPTED_V1_SCOPE`
warning on every run (`test_calculations_not_attempted_v1_scope_is_disclosed_not_hidden`),
and both remain reachable by hand today exactly as before.

### A real, frozen constraint this milestone surfaced (not introduced)

`application/valuation.py`'s existing, frozen profile gate
(`UNKNOWN_OR_INCOMPATIBLE_PROFILE`) requires `profile_id` to resolve to exactly
`BANK_PROFILE` or `IT_SERVICES_PROFILE` for **any** valuation method - including
`PRICE_TO_EARNINGS`/`PRICE_TO_BOOK` - to be `AVAILABLE`. For a company whose sector
resolves to `UNKNOWN`, valuation is therefore correctly `NOT_APPLICABLE`, not merely
"not attempted." This is pre-existing Part 5A behavior; Part 6B0 does not route around it
(`profile_id or "UNRESOLVED_V1"` is passed straight through into the existing gate,
which then does exactly its job).

### Fiscal-label handling correction found during implementation

Real Screener `fiscal_label` values are `"Mar 2025"`-style text, not `"YYYY-MM"`. The
first implementation draft assumed the latter and would have silently never selected an
annual EPS observation or an annual sector-KPI period against real evidence. Fixed by
reusing the existing, frozen `application.calculations.period_key()` - the exact function
`application/sector_kpis.py` itself already uses for the same conversion - rather than
inventing a second one.

### Observation-only valuation

Only `PRICE_TO_EARNINGS`/`PRICE_TO_BOOK`, built from `current_price`/`eps`(most recent
annual, via `period_key`)/`book_value` FINANCIAL observations already on the extraction.
No `ValuationScenario` is ever constructed; no WACC/terminal growth/exit multiple/
forecast FCFF is invented (`test_no_invented_valuation_assumptions_only_observed_
methods_run` asserts the resulting method set is a subset of `{PRICE_TO_EARNINGS,
PRICE_TO_BOOK}`).

### `FullResearchRequest` / `FullResearchResult` / `ProgressSink`

```python
class FullResearchRequest:  # source_url, research_plan: Literal["FULL_RESEARCH_V1"]
class FullResearchWarning:  # code, stage, message
class FullResearchResult:   # company_id, extraction_id, sector_classification,
                             # document_versions_ingested, document_chunks_selected,
                             # analysis_snapshot_id, challenge_snapshot_id,
                             # report_snapshot_id, stage_warnings
class ProgressSink(Protocol):  # on_stage_change(PREPARING|EVIDENCE|ANALYSIS|CHALLENGE|REPORT|COMPLETED)
```

No sector field, no document URLs, no valuation assumptions on the request - fully
server-owned. No percentages in progress; stages match real orchestrator transitions
(`test_progress_stages_are_observed_in_order`).

### Re-ingestion

Unchanged, existing evidence architecture: `RawArtifactStore.put` deduplicates identical
bytes by content hash; every `extract_company`/`ingest_document` call creates a new
immutable retrieval/snapshot event; nothing is ever mutated in place. No incremental-sync
system was added.

## 5. CLI: `stock-research-ai full-research`

`__main__.py`'s `full-research` subcommand is a thin dispatcher (`--source-url`,
`--output`) delegating entirely to `bootstrap.full_research`, which itself only
constructs the live adapter and a fully-wired `ResearchEngine` before calling
`application.research_plan.run_full_research` - no orchestration logic lives in either
file. This is the pre-browser, real end-to-end proof command.

## 6. Offline test matrix (39 new tests, all network mocked)

- `test_sector_resolution.py` (10): real-fixture BANK/IT_SERVICES mapping, UNKNOWN on
  unmapped/absent label, no fuzzy matching, never a default profile.
- `test_document_topics.py` (7): topic matching, type filtering, run-scope isolation,
  per-document/topic/total budget enforcement, guidance-wording is not specially matched,
  `SelectedChunk`'s field set.
- `test_anthropic_analyst.py` (17): valid response, credential secrecy, malformed JSON,
  missing/wrong-shaped tool_use, extra/missing claim fields pass through untouched,
  timeout retry-then-succeed and retry-exhausted, auth/permission never retry, 400/404 ->
  `MODEL_UNAVAILABLE`, 429/5xx bounded retry, response-too-large, single-call-per-invocation.
- `test_research_plan.py` (5): a real YESBANK fixture through the full offline pipeline
  with a dynamic, bundle-inspecting fake analyst (never `tests/fakes/analyst.py`'s static
  fake) - sector resolves to BANK, documents are discovered/ingested/chunked/selected,
  `document_chunk_refs` reaches a real `AnalysisRequest`, a document-cited claim reaches
  Part 5B, Part 5C and Part 6A both run to a real `ResearchReportSnapshot` with
  `corpus_scope=PINNED_REPORT_INPUT_ONLY`; progress stages observed in order; only
  observation-based valuation methods appear; `DOCUMENT_RECENCY_UNVERIFIED` and
  `CALCULATIONS_NOT_ATTEMPTED_V1_SCOPE` warnings are both honestly present.

## 7. Live smoke (opt-in, never part of normal pytest)

`scripts/live_smoke_full_research.py` - outside `testpaths`, additionally gated by an
explicit `--confirm-live` flag and real `STOCK_RESEARCH_AI_ANALYST_PROVIDER`/`_API_KEY`
environment variables. Structural assertions only (snapshot IDs present); never asserts
on model-generated prose.

## 8. Known V1 limitations

- Sector resolution covers exactly `Banks` -> `BANK_PROFILE` and `IT - Software` ->
  `IT_SERVICES_PROFILE`; every other Screener Broad Industry label resolves to `UNKNOWN`,
  and valuation/sector-KPIs are then correctly `NOT_APPLICABLE`/skipped for that company.
- QUARTERLY_RESULT and AGM have no dedicated `DocumentType`; they are discovered/ingested
  only incidentally as `ANNOUNCEMENT`/`OTHER`, capped small.
- `calculate_financials`/`evaluate_patterns`/market/context acquisition are not yet
  wired into `FULL_RESEARCH_V1` (disclosed via `CALCULATIONS_NOT_ATTEMPTED_V1_SCOPE` on
  every run).
- No automatic re-run/incremental sync: each invocation re-runs the full pipeline.
