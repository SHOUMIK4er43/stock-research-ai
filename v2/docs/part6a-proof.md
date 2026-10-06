# Part 6A implementation and proof

Verified locally: 695 full tests passed (649 from Parts 1-5C, unchanged, plus 46 new Part
6A tests: 26 in `test_report.py`, 20 in `test_report_pipeline.py`). Ruff passed on the full
tree. Mypy passed across 77 source files. A fresh offline proof
(`scripts/verify_report.py`, into a new, never-reused output directory) ran both required
tracks and produced all three canonical artifacts for INFY and YESBANK. No dependencies
were added. No commit, tag, or push was made as part of this implementation pass.

## Scope and the mandatory chain

Part 6A is the **Final Report Engine**. It transforms a frozen, pinned Part 5C
`ChallengeSnapshot` into `ResearchReportSnapshot` (canonical, persisted),
`COMPANY_INSTITUTIONAL_RESEARCH.md`, `COMPANY_INVESTOR_BRIEF.md`, and
`COMPANY_RESEARCH.json`. It mints no new evidence, no new arithmetic, no recommendation.

The chain is enforced structurally, not by convention:

```
evidence -> deterministic calculations -> Part 5B AnalysisSnapshot
         -> Part 5C ChallengeSnapshot -> Part 6A ResearchReportSnapshot
```

`ReportRequest = {challenge_snapshot_id: UUID}` is the *only* field. There is no
`analysis_snapshot_id` field anywhere on `ReportRequest`, and `ResearchEngine.generate_report`
has exactly one code path: fetch the pinned `ChallengeSnapshot` via
`ChallengeRepository.get_challenge`, raise `ValueError("Unknown challenge snapshot")` if it
doesn't exist. There is no fallback that accepts a bare `AnalysisSnapshot`
(`test_report_requires_challenge_snapshot`).

## Speaker attribution (final corrected rule)

`domain/report.py::ReportQuoteAttribution = Literal["MANAGEMENT_STATEMENT", "TRANSCRIPT_STATEMENT"]`
is a **report-rendering-layer-only** label — `domain/analysis.py::ManagementStatement` is
untouched, unrenamed, still frozen. The deterministic rule
(`application/report.py::quote_attribution`) only ever consults the cited chunk's owning
`ParsedDocument.document_type` — never prose style, never position, never a guessed speaker
name:

```python
_SINGLE_VOICE_DOCUMENT_TYPES = {ANNUAL_REPORT, INVESTOR_PRESENTATION, EARNINGS_PRESENTATION, ANNOUNCEMENT}
quote_attribution(doc_type) = "MANAGEMENT_STATEMENT" if doc_type in _SINGLE_VOICE_DOCUMENT_TYPES else "TRANSCRIPT_STATEMENT"
```

`CONCALL_TRANSCRIPT` (structurally known to mix management, analysts, and moderators, with no
speaker field anywhere in `DocumentChunk`/`DocumentPage` to disambiguate them) always renders
`TRANSCRIPT_STATEMENT`. `CREDIT_RATING` and `OTHER` render `TRANSCRIPT_STATEMENT` too, for the
same reason (third-party or unclassified voice). Proved against **real** YESBANK evidence: the
real proof run's `CONCALL_INTELLIGENCE` section statements are checked and none carry
`MANAGEMENT_STATEMENT`; a real investor-presentation statement does. Unit-proved in
`test_report.py::TestQuoteAttribution` for every `DocumentType` value, and end-to-end in
`test_report_pipeline.py`.

## Coverage vocabulary: unchanged, plus a closed reason-code overlay

`ReportSectionCoverage.status` is `domain/analysis.py::SectionCoverageStatus` — the exact
frozen 8-value Literal, imported directly, never subclassed or widened. Pydantic itself
proves this: constructing `ReportSectionCoverage(status="NOT_YET_SUPPORTED_BY_UPSTREAM", ...)`
raises `ValidationError` (`test_only_frozen_statuses_accepted`) because that string was never
part of the type in the first place — there is no code path that could produce it even by
accident.

The one **additive** field is `reason_code: ReportCoverageReasonCode | None`, a closed
four-value enum, populated **only** on Part-6A-native sections (never on a section that maps
1:1 to a Part 5B `AnalysisSection`, where `status`+`reason` are copied byte-for-byte from the
embedded `SectionCoverage`):

| reason_code | status paired with it | sections |
|---|---|---|
| `REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED` | `INSUFFICIENT_INPUT` | Guidance & Commitments, Guidance vs Actual, Management Credibility, Guidance-vs-Actual Appendix |
| `UPSTREAM_CAPABILITY_NOT_IMPLEMENTED` | `UNAVAILABLE` | AGM Intelligence, Governance & Capital Allocation, Industry & Competitive Structure |
| `NO_DOCUMENTS_OF_TYPE_PINNED` | `UNAVAILABLE` | Concall/Annual-Report/Investor-Presentation Intelligence, Document Corpus (when empty) |
| `NARRATOR_NOT_CONFIGURED` | (per-block, not section-level) | narrator-gated interpretive content only |

Proved directly (`test_only_frozen_statuses_accepted`, `test_unknown_reason_code_rejected`)
and end-to-end for every placeholder section against real INFY/YESBANK data
(`test_guidance_and_agm_sections_render_honest_placeholders`,
`run_infy`/`run_yesbank` in `scripts/verify_report.py`).

## Part 5B + Part 5C synthesis rules (structural, not conventional)

| Outcome | Rendering |
|---|---|
| `SUPPORTED_BY_BUNDLE` | Statement rendered as-is, `epistemic_class` stays `INTERPRETATION`. |
| `SUPPORTED_WITH_LIMITATIONS` | Statement rendered with every limitation attached to `ReportStatement.limitations`. |
| `CHALLENGED` | Statement rendered *alongside* sibling `ReportStatement`s for each `ChallengeFinding`, in the same section. |
| `UNVERIFIABLE_FROM_BUNDLE` | Rendered, tagged `NOT_INDEPENDENTLY_VERIFIABLE_FROM_BUNDLE`, excluded from `synthesis.supporting_claim_ids`. |
| `INVALID_AGAINST_BUNDLE` | **`_claim_report_statements` returns `()` for this outcome — no `ReportStatement` is ever constructed for it in a topical section.** It surfaces only inside `INDEPENDENT_CHALLENGER_REVIEW`, the Challenger Appendix, and Data Gaps, always paired with its finding. |

This is enforced by a function that literally cannot produce the forbidden output, not by a
downstream filter — `test_invalid_claim_never_appears_as_topical_conclusion` tampers a
persisted claim's own statement into forbidden-recommendation language, confirms Part 5C
correctly marks it `INVALID_AGAINST_BUNDLE`, then proves the resulting report's `VALUATION`
section contains **no** statement citing that claim, while `INDEPENDENT_CHALLENGER_REVIEW`
does, and `synthesis.invalid_claim_ids` (not `supporting_claim_ids`) contains it.
`ReportStatement`'s own validator adds a second, independent check:
`challenge_outcome == "INVALID_AGAINST_BUNDLE"` requires non-empty `challenge_finding_ids` —
so even a hand-constructed invalid statement cannot exist without exposing its finding
(`test_invalid_outcome_requires_finding_ids`).

## No global investment stance (removed after the addendum)

`ReportSynthesis` has no `research_evidence_stance` field of any kind — I attempted a
threshold-free decision tree during the architecture addendum and concluded even an
existence-based "worst case dominates" rule implicitly asserts an unapproved cross-claim
importance ranking, so it was removed rather than kept in a weakened form.
`test_research_completeness_and_valuation_state_are_deterministic` asserts
`not hasattr(result.synthesis, "research_evidence_stance")`. What remains, all mechanical:

- `research_completeness ∈ {HIGH, MODERATE, LIMITED}` — a mechanical, family-based rule
  over the **full assembled Part 6A report** (`CORE_SECTIONS`/`MAJOR_COMPLEMENTARY_SECTIONS`
  in `application/report.py`), not merely the narrower set of `AnalysisSection`s a
  particular Part 5B request happened to ask for. An earlier version of this rule only
  looked at the requested sections' own coverage, which let a report requesting just two
  sections (e.g. `SECTOR_KPIS`+`VALUATION`) register `HIGH` purely because those two
  happened to resolve `AVAILABLE` — semantically wrong, since completeness must reflect
  breadth of usable research coverage, not narrow execution success. The corrected rule:
  `LIMITED` if 2+ `CORE_SECTIONS` are outside `{AVAILABLE, PARTIAL, NOT_APPLICABLE}`;
  `MODERATE` if exactly 1 such core gap, or 1+ `MAJOR_COMPLEMENTARY_SECTIONS` gaps with no
  core gap; `HIGH` otherwise. Sections whose `reason_code` marks a permanent V1 capability
  boundary (`UPSTREAM_CAPABILITY_NOT_IMPLEMENTED`/`REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED`)
  are excluded from scoring entirely — that gap reflects what Part 6A can build today, not
  what evidence exists for this company, and counting it would make `HIGH` permanently
  unreachable regardless of research thoroughness. **`research_completeness` therefore
  measures completeness relative to currently implemented/reportable V1 capabilities, not
  completeness against the full 41-section master research architecture — a `HIGH` result
  never implies every master-report capability (guidance tracking, AGM intelligence,
  governance, competitive structure) exists; it means every capability Part 6A can build
  today was genuinely populated for this company.** No percentage, no weighted score, no
  narrator/model input — `NarrativeBlockCandidate` has no such field, proved by
  `test_narrator_cannot_smuggle_a_completeness_value`. Every branch is proved directly in
  `TestResearchCompleteness` (`test_report.py`): broad coverage → `HIGH`; a single core gap
  or a complementary gap alone → `MODERATE`; two-plus core gaps → `LIMITED`;
  `NOT_APPLICABLE` never counts as a gap; `UNAVAILABLE`/`INSUFFICIENT_INPUT` always do;
  capability-gap reason codes are excluded from scoring.
- `valuation_evidence_state ∈ {OBSERVED, SCENARIO_DEPENDENT, NOT_DETERMINABLE}` — existence
  over the `VALUATION` section's own coverage plus the `category` of cited `ValuationResult`s.

No BUY/SELL/HOLD, no ATTRACTIVE/FAIR/EXPENSIVE, no score, anywhere — `FORBIDDEN_RECOMMENDATION`
(reused unchanged from Part 5B/5C's `evidence_matching.py`, already covering buy/sell/hold/
target price) is re-applied as a validator sweep over all narrator text.

## Report input / persistence: Option B, chosen and justified

`ResearchReportSnapshot` does **not** re-embed the full `ChallengeSnapshot`/`AnalysisSnapshot`
JSON. It pins `analysis_snapshot_id`+`analysis_snapshot_sha256` and
`challenge_snapshot_id`+`challenge_snapshot_sha256`, plus a `ReproducibilityManifest` that
cross-validates those same pins (`ResearchReportSnapshot.lineage` validator). Since every
upstream row in this system is append-only and never updated, re-fetching by ID always
returns byte-identical content — pin+hash gives the same reconstruction guarantee as full
embedding, without duplicating the (now three-levels-deep) nested payload. `ReportTable`/
`ReportMetric` cells still carry their own single scalar `display_value`+`unit`+`period` (a
table must show values to be useful) plus an `evidence_ref` for traceability — this is
bounded, per-cell materialization, categorically different from re-embedding whole snapshots.

## Atomic integrity (`SQLiteReportRepository.save_report`)

One transaction, one connection view:
1. Re-fetch + re-hash the pinned `ChallengeSnapshot` row (new helper,
   `_load_challenge_snapshot_in_session`).
2. Re-fetch + re-hash the transitively-referenced `AnalysisSnapshot` row (reused
   `_load_analysis_snapshot_in_session`, unchanged from Part 5C).
3. Verify the challenge's own `analysis_snapshot_id` still points at the pinned analysis.
4. Re-verify every distinct evidence source in the reviewed snapshot's own `sources`, and
   every derived kind's upstream lineage (`_load_evidence_owner_in_session`/
   `_verify_upstream_lineage_in_session`, reused unchanged from Part 5B/5C).
5. Only then insert `_ReportRow`.

Raw-artifact byte re-verification happens earlier, in `resolve_report_evidence` (application
layer, before the transaction) — the same place and the same mechanism Part 5C added
(`direct_raw_artifacts`/`transitive_raw_artifacts` + `artifacts.read()`), reused unchanged.
Any failure raises `ReportIntegrityError` (a `ValueError` subclass) and the transaction rolls
back. Proved: `test_challenge_snapshot_corruption_between_build_and_append_refuses_atomically`
(genuine build-then-tamper-then-persist TOCTOU, mirroring Part 5C's own regression test
exactly — the report object is built from a pristine read, the DB row is tampered afterward,
and `save_report` still catches it), `test_analysis_snapshot_corruption_blocks_report_append`,
`test_raw_artifact_corruption_blocks_report_generation`,
`test_failed_integrity_leaves_row_count_unchanged` (row count identical before/after a failed
attempt), `test_report_revisions_append_only_and_reconstruct` (old report reconstructs
byte-identical from history).

## Document corpus: `PINNED_REPORT_INPUT_ONLY`, never a completeness claim

`DocumentCorpus.corpus_scope: Literal["PINNED_REPORT_INPUT_ONLY"]` is a structural constant.
Entries are built only from `ResolvedEvidence.documents` — which itself only ever contains
documents the original Part 5B `AnalysisRequest.document_chunk_refs` explicitly pinned; there
is no code path that queries the document repository for "everything retained for this
company." Confirmed for INFY (zero documents were ever pinned → corpus is honestly empty,
`document_corpus_entries: 0` in the real proof) and for YESBANK (exactly the two documents
explicitly ingested and pinned → `document_corpus_entries: 2`, no more, no fewer, even though
other documents could theoretically exist in the same database).
`report_validation.py::FORBIDDEN_COMPLETENESS_CLAIM` is a dedicated regex
(`"all annual reports"`, `"every filing"`, `"complete document history"`, ...), applied only
to the Document Corpus section, that rejects any narrator sentence implying full-universe
completeness (`test_forbidden_corpus_completeness_claim_rejected`).

## Deterministic assembler + optional narrator (evaluated, adopted)

`application/report.py::assemble_report` never calls a model and is fully sufficient on its
own — every real-proof report above was generated with `execution=None`. The optional
`ReportNarrator` receives the already-built `tuple[ReportSection, ...]` and may return only
`NarrativeBlockCandidate{section, text}` — `extra="forbid"` means it cannot express an
evidence ID, hash, number, unit, period, scope, coverage status, reason code, challenge
outcome, or research-completeness/valuation-state value even by accident; those fields don't
exist on the candidate type at all.

**Numeric closure** (`report_validation.py::_numeric_universe_for_section`): every numeric
token in narrator text must already appear in that *same section's own* tables/statements —
not re-verified against raw evidence a second time, but against the strictly narrower,
already-assembled report content itself. **Forbidden-language sweep**: `FORBIDDEN_RECOMMENDATION`/
`FORBIDDEN_CERTAINTY` (reused unchanged). **Invalid-section guard**: a section containing an
`INVALID_AGAINST_BUNDLE` statement refuses narrator text entirely (outside the Challenger/Data
Gaps sections), so a narrator sentence can never read as an unqualified endorsement of
something Part 5C already invalidated. A rejected block is recorded in
`rejected_narrative_blocks` and the section simply keeps its deterministic-only content — the
report never fails wholesale because of one bad narrator sentence
(`test_narrator_numeric_closure_and_fallback`, and the synthetic proof track, which sends a
"We recommend investors buy the stock immediately" block and confirms it is rejected and never
leaks into the persisted report text).

## Guidance-vs-actual, AGM, governance, competitive structure: honest placeholders

No synthetic `ManagementGuidance` object was fabricated anywhere. These sections render an
existing coverage status plus the closed reason code, with the future upstream contract
already documented in the architecture addendum
(`ManagementGuidance`/`GuidanceComparator`/`GuidanceOutcome ∈ {MET, MISSED, EXCEEDED,
PARTIALLY_MET, NOT_COMPARABLE, INSUFFICIENT_INPUT}`) but not implemented — no `MET`/`MISSED`
result exists anywhere in this codebase. Proved end-to-end against real INFY/YESBANK data
(`test_guidance_and_agm_sections_render_honest_placeholders`).

## Narrative-change detection: reduced, honest V1 scope

`ReportRequest` carries only `challenge_snapshot_id` — no caller-declared topic keyword.
`MANAGEMENT_NARRATIVE_CHANGE_DETECTION` therefore only shows the *structural* fact that
multiple retained versions of the same `document_id` are pinned (`PARTIAL`) or that fewer than
two exist (`INSUFFICIENT_INPUT`) — it never diffs statement content or invents a tone/sentiment
judgement, since doing so would require a topic to compare on that this contract doesn't
provide.

## Static monitoring dashboard

`MonitoringEntry.direction` is always `"UNKNOWN"` in this implementation — no prior-value
comparison logic was added, because no upstream mechanism supplies a genuinely comparable
"prior" observation for an arbitrary metric within one closed evidence bundle. Entries are
populated only from metrics already cited by a `CHALLENGED`/`SUPPORTED_WITH_LIMITATIONS` claim
or flagged by Part 5C's own omission review — `monitoring_rationale` is always one of two fixed
strings tied to that existing research state. No trigger threshold, scheduler, or watcher
exists (`test_monitoring_is_static_snapshot_only`).

## Executive snapshot, investor brief, COMPANY_RESEARCH.json

`ExecutiveResearchSnapshot` is a pure projection (`_build_executive_snapshot`) of
already-built `ReportSection`s — no independent authoring. The **investor brief**
(`render_investor_brief`/`export_investor_brief`) and **`COMPANY_RESEARCH.json`**
(`export_report`) are both pure reshaping functions of the *same* persisted
`ResearchReportSnapshot` — neither re-runs Part 5B, re-runs Part 5C, or performs new
evidence resolution, so the brief cannot contradict the institutional report by construction
(`test_investor_brief_and_json_derive_from_same_snapshot` checks the brief's rendered
valuation bullet text is a subset of the same statements that appear in the JSON's own
`valuation.statements`).

## Rendered artifacts vs. canonical snapshot

`COMPANY_INSTITUTIONAL_RESEARCH.md`/`COMPANY_INVESTOR_BRIEF.md` (`adapters/markdown_export.py`)
and `COMPANY_RESEARCH.json`/`investor_brief.json` (`adapters/json_export.py`) are all
deterministic renderings of the one persisted `ResearchReportSnapshot` row — re-rendering in
another format never re-runs research, since all three exporters take the already-built
snapshot object as their only input.

## Capabilities

`deterministic_report_assembly_available` is `True` once `ReportServices` is wired — no
narrator needed. `live_report_narrator_available` is `True` only when `provider_kind == "LIVE"`.
No live provider ships in this part; `FakeReportNarrator` is test/proof-only and is never
imported by `bootstrap.py` (`test_capabilities_report_deterministic_available_narrator_not`).

## CLI

```
stock-research-ai --database PATH generate-report --challenge-snapshot UUID --output PATH [--execution execution.json]
```
Exit codes: `0` success (a report full of `CHALLENGED`/`INVALID_AGAINST_BUNDLE` content is an
honest result, not a failure); `2` malformed request (unknown challenge snapshot, bad
JSON/paths); `4` narrator unavailable (`--execution` given without an injected narrator); `5`
validator/system failure (`pydantic.ValidationError`); `6` persistence/integrity failure
(`ReportIntegrityError`).

## Real proof (INFY, YESBANK)

- **INFY**: `SALES_GROWTH` + `OPERATING_MARGIN_DETERIORATION` (reusing the exact real
  `ANALYTICAL_TENSION` finding Part 5C's own proof already demonstrated) + observed P/E. Zero
  documents pinned → an honestly empty Document Corpus. `research_completeness=LIMITED` —
  honest, since this proof's `AnalysisRequest` only ever pinned `SECTOR_KPIS`+`VALUATION`,
  leaving five other `CORE_SECTIONS` (company identity, annual/quarterly/balance-sheet/
  cash-flow financials, market-implied expectations) genuinely `UNAVAILABLE`.
  `valuation_evidence_state=OBSERVED`. No FCFF row (never an IT-services concern to begin
  with, but confirmed absent regardless).
- **YESBANK**: `DEPOSIT_GROWTH` + `DEPOSIT_GROWTH_DECELERATION` tension. **P/E is genuinely
  observed** (`AVAILABLE`, a real multiple). **P/B is genuinely, correctly refused** —
  real YESBANK `book_value` has no `VERIFIED` BVPS semantic mapping, so the frozen Part 5A
  rule (`application/valuation.py::validate_fact`, `require(item.semantic_verified, ...)`)
  refuses it with `status=UNVERIFIED_METRIC_SEMANTICS` and zero output values; Part 6A
  renders that refusal exactly (`display_value=None`, same status, no fabricated multiple)
  and performs no calculation of its own (`_metric_from_reference` only re-displays
  `canonical_values()`, which returns nothing for a refused result). This was verified by
  direct provenance audit and is now asserted in the real proof script itself and in
  `test_refused_price_to_book_is_preserved_and_never_calculated_by_part6a`. An earlier
  draft of this document incorrectly stated "P/E and P/B observed" — that was a proof-script
  bug (an ambiguous `next()` over both valuation refs happened to bind to the P/E one), not
  a Part 5A/6A semantic error; it has been corrected here and in the script. **No FCFF row**
  (never requested for a bank profile — the safest possible representation, since no row is
  more honest than a fabricated `NOT_APPLICABLE`). Both
  genuinely retained document fixtures (`yesbank_presentation.pdf`,
  `yesbank_transcript.pdf`) ingested through the normal Part 3A pipeline and cited via
  real `ManagementStatement` quotes — Document Corpus shows exactly 2 entries,
  `CONCALL_INTELLIGENCE` is `AVAILABLE` with every statement `TRANSCRIPT_STATEMENT` (never
  `MANAGEMENT_STATEMENT`), `INVESTOR_PRESENTATION_INTELLIGENCE` is `AVAILABLE`.
  `research_completeness=LIMITED` for the same reason as INFY — this proof's request only
  pins `SECTOR_KPIS`+`VALUATION`+`MANAGEMENT_AND_DOCUMENT_INTELLIGENCE`, leaving the
  financial-statement `CORE_SECTIONS` genuinely `UNAVAILABLE`. `valuation_evidence_state=
  OBSERVED`.

## Synthetic proof

A narrator sending forbidden recommendation language is rejected and never leaks into the
persisted report; two independent report revisions plus the narrated one are proved
append-only and reconstructable from history; raw-artifact corruption after a first
successful generation blocks a second attempt with `ReportIntegrityError`.

## Known V1 limitations

- Guidance & Commitments, Guidance vs Actual, Management Credibility, AGM Intelligence,
  Governance & Capital Allocation, and the competitive-structure half of Industry &
  Competitive Structure are all honest placeholders — no upstream data model exists for any
  of them yet.
- Narrative-change detection shows only structural document-revision existence, not a
  content-level diff, because `ReportRequest` carries no topic keyword.
- Document Corpus visibility is bounded by what the *original* Part 5B `AnalysisRequest`
  pinned — not the full document universe retained for the company. This is a deliberate,
  accepted consequence of the closed-input boundary, not a bug.
- Monitoring `direction` is always `UNKNOWN` — no prior-value comparison mechanism exists yet.
- Concall speaker-role attribution (which participant said what) is not attempted at all.

## Part 6A / Part 6B boundary

Part 6A ships `ResearchReportSnapshot` (canonical), `COMPANY_RESEARCH.json`, and two Markdown
artifacts — stable, versioned interfaces. Nothing about browser-extension UX, popup design, or
content-script injection is decided here.
