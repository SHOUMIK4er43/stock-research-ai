# Part 6B-C Proof: Full Institutional Report Viewer + Evidence Drill-Down + Challenger + Document Intelligence + Artifact UX

Status: **implementation complete, not committed** (per instruction: DO NOT COMMIT / TAG / PUSH).

## 0. Pre-commit audit addendum

A follow-up pre-commit schema-fidelity + artifact audit found ONE genuine defect: the hand-written
`extension/tests/fixtures/report_fixtures.js` used plausible-looking but **nonexistent** `EvidenceKind`
values (`FINANCIAL_STATEMENT_LINE_ITEM`, `CONCALL_TRANSCRIPT_CHUNK`, `DOCUMENT_SECTION`) instead of the
real, closed 13-value Literal in `domain/analysis.py` (`COMPANY, FINANCIAL, SHAREHOLDING, MARKET,
FINANCIAL_CALCULATION, SECTOR_KPI, PATTERN, MARKET_CALCULATION, TECHNICAL, VALUATION, DOCUMENT_CHUNK,
PUBLICATION, MACRO`). This was a **fixture-only** defect — the viewer itself renders `kind` as opaque
passthrough text and never branches on its value, so no rendering logic was wrong — but it broke schema
fidelity of the test data. Fixed by replacing the three invented values with real ones
(`FINANCIAL_STATEMENT_LINE_ITEM`→`FINANCIAL`, `CONCALL_TRANSCRIPT_CHUNK`→`DOCUMENT_CHUNK`,
`DOCUMENT_SECTION`→`PUBLICATION`), and added a permanent regression test
(`report_fixture_fidelity.test.js`) that recursively walks any fixture and fails if any enum-shaped field
uses a value outside its real closed Literal.

The audit also added `extension/tests/fixtures/real_yesbank_report.json` — a real, offline
`ResearchReportSnapshot` payload extracted (read-only, no live research) from an existing prior proof run's
SQLite database (`output/part6a-proof-final/YESBANK/proof.sqlite3`, gitignored/local), with only array
*counts* trimmed (table rows, per-section statements, limitations, monitoring, document entries) to keep
the committed fixture small — every remaining value is real, unaltered backend output. Re-validated
against the real `ResearchReportSnapshot` Pydantic model before being committed. `report_real_fixture.test.js`
exercises the full pipeline (real JSON → `groupSections` → `renderNav`/`renderSection`/`renderStatement`)
against it, including a real `CHALLENGED` statement and a real `TRANSCRIPT_STATEMENT`-attributed statement
whose text happens to embed a plain-prose regulatory-filing URL — proving it renders as inert text, not a
fabricated clickable link (there is no structured URL field to back one).

No viewer/render logic changed as a result of this audit — only the fixture and new regression tests. See
`docs/` conversation history / commit message for the full 31-item pre-commit audit response.

## 1. Scope delivered

A report viewer built entirely inside the existing Chrome extension (`extension/`), consuming the frozen
`GET /v1/reports/{report_id}` and `GET /v1/reports/{report_id}/artifacts/{artifact_kind}` endpoints
unchanged. Zero Python files were read for modification purposes beyond re-confirming the frozen
`domain/report.py` / `domain/analysis.py` / `domain/challenge.py` schemas (read-only) and the artifact
rendering adapter (read-only, to confirm content-types). **No Python file was modified.**

### New files
- `extension/src/report/section_registry.js` — canonical `ReportSectionId` (42 values) → {group, label,
  order} mapping. Unknown future section IDs fall back to an `OTHER` group safely.
- `extension/src/report/labels.js` — presentation label maps for every frozen backend vocabulary
  (`SectionCoverageStatus` [8], `EpistemicClass` [5], `ClaimReviewOutcome` [5], `ReportQuoteAttribution`
  [2], `DocumentType`, `ResearchCompleteness` [3], `ValuationEvidenceState` [3], monitoring direction [4],
  document coverage status [3]), plus `humanizeStatus()` for `ReportMetric.status`'s open `str` field.
- `extension/src/report/dom.js` — the single safe-DOM-construction primitive (`el()`, `safeLink()`,
  `clear()`). Every other rendering function is built on `el()`, so this is the one place the
  no-innerHTML/XSS-safety guarantee needs proving.
- `extension/src/report/view_model.js` — pure transforms: `groupSections`, `isSectionAvailable`,
  `hasSectionContent`, `groupDocumentCorpus`, `materialChallengedStatements`, `findSection`.
- `extension/src/report/render.js` — DOM-wiring layer: statement cards (epistemic/challenge/attribution
  badges + "Why does the report say this?" evidence drawer), tables (`display_value`/`unit` verbatim,
  `humanizeStatus` fallback), generic section renderer (coverage badge + reason), bespoke Executive
  Snapshot, Research Synthesis, Document Corpus, Monitoring, and Data Gaps renderers, and grouped
  navigation.
- `extension/tests/fixtures/report_fixtures.js` — realistic `ResearchReportSnapshot`-shaped fixtures built
  directly against the real schema, covering: available/partial/not-applicable/unavailable/
  unverified-metric-semantics coverage, FACT/CALCULATION/INTERPRETATION/ASSUMPTION/SCENARIO statements,
  CHALLENGED/INVALID_AGAINST_BUNDLE outcomes, an unknown future section ID, an XSS payload embedded
  verbatim in statement text, document corpus entries, monitoring, and data gaps. Also exports
  `REAL_EVIDENCE_KINDS`, the real closed 13-value `EvidenceKind` list, after the pre-commit audit found
  and fixed three invented `kind` values (see §0 above).
- `extension/tests/fixtures/fake_dom.js` — a tiny hand-rolled fake `document`/`Element` (no jsdom
  dependency) used to unit-test `dom.js`/`render.js` in plain Node, including a `serialize()` helper used
  only by tests to prove text is HTML-escaped rather than parsed as markup.
- `extension/tests/fixtures/real_yesbank_report.json` — a real, offline, count-trimmed
  `ResearchReportSnapshot` payload extracted from an existing Part 6A proof run (added during the
  pre-commit audit; see §0).
- `extension/tests/report_section_registry.test.js`, `report_labels.test.js`, `report_dom.test.js`,
  `report_view_model.test.js`, `report_render.test.js` — new test files for the modules above.
- `extension/tests/report_fixture_fidelity.test.js`, `report_real_fixture.test.js` — added during the
  pre-commit audit (schema-fidelity walker; real-fixture end-to-end pipeline proof).

### Modified files
- `extension/src/api/client.js` — added `getArtifact(token, reportId, artifactKind)` (raw text fetch,
  bypassing the JSON-only `_request` helper since artifacts are markdown/JSON-as-text) and the closed
  `ARTIFACT_KINDS` enum. Still loopback-validated per-call and Bearer-authenticated, token never in the
  URL.
- `extension/src/state.js` — added `REPORT_LOADING`, `REPORT_READY`, `REPORT_NOT_FOUND`,
  `REPORT_INTEGRITY_ERROR`, `REPORT_LOAD_FAILED` to the *same* state machine (not a parallel one), with a
  `reportView` fact that outranks page-support/job status but is itself outranked by
  service/version/pairing checks.
- `extension/src/job_flow.js` — added `isReportForCurrentPage(lastReport, currentSourceUrl)`, the pure
  decision behind the persisted-report-reopen and active-page-change behavior.
- `extension/sidepanel.html` / `extension/css/sidepanel.css` — report-viewer markup/styles (nav, section
  content, evidence drawers, coverage/challenge badges, artifact buttons, back controls, a "View last
  research" affordance on the READY panel), consistent with the existing institutional dark/light design
  system.
- `extension/src/sidepanel.js` — wires report loading (`openReport`), rendering (`renderReportBody`),
  section selection, artifact download (Blob + object URL + `<a download>`, no `chrome.downloads`
  permission), the sticky report-view mode (page/tab activity never silently replaces an open report),
  and the persisted-report/"View last research" affordance gated by `isReportForCurrentPage`.

## 2. Backend contract: unchanged

No Python file was modified. `git status` confirms zero changes under `src/stock_research_ai/`. The
viewer consumes `GET /v1/reports/{report_id}` and the 3-artifact-kind endpoint exactly as committed in
Part 6B-A. No new backend route, field, or status value was needed — every rendering decision was made
from fields already present in `domain/report.py`.

## 3. Key schema-driven design decisions

- **No source URL anywhere**: `EvidenceReference` has no `url` field. `safeLink()` was built defensively
  (tested for https/http-only, `noopener noreferrer`, rejects `javascript:`/`data:`/`file:`, rejects
  malformed URLs) but has no real caller yet in `render.js` — evidence is shown as `{kind, item_key}`
  technical detail only, never a fabricated clickable link.
- **No document title/name**: `DocumentCorpusEntry` has no title field, so the Document Corpus view
  renders `{document_type, coverage_status, fiscal_label, publication_date}` only.
- **`ReportMetric.status` is an open `str`**, not the closed `SectionCoverageStatus`. `humanizeStatus()`
  falls back to a generic title-cased transform for any status outside the known coverage vocabulary
  (e.g. `DIVISION_BY_ZERO` → "Division By Zero") — never a fabricated or reinterpreted value.
- **Sticky report view**: a `reportView` state fact was added to (not layered beside) the existing state
  machine, taking precedence after service/version/pairing but before page/job — so an open report
  survives page navigation and tab events until the user explicitly clicks Back.
- **Persisted-report-reopen / active-page-change**: `isReportForCurrentPage()` is a pure function (mirrors
  the existing `job_flow.js` pattern for testable decision logic) — it decides only whether to *offer* the
  stored `lastReport` pointer as "this page's research"; it never clears or invalidates the stored pointer
  itself. This means navigating to a different company never mislabels an old report, but also never
  destroys the old pointer.

## 4. Test results

```
$ cd extension && npm test
ℹ tests 165
ℹ pass 165
ℹ fail 0
```

(Baseline before Part 6B-C: 74. After initial implementation: 148. After the pre-commit audit's
additional regression tests (fixture-fidelity walker, unknown-status fail-safety, extra XSS payloads, real
YESBANK-fixture pipeline test): 165, across 7 new test files plus additions to `api_client.test.js`,
`state.test.js`, and `job_flow.test.js`.)

```
$ UV_NO_SYNC=1 UV_FROZEN=1 PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
815 passed, 7 warnings in 169.27s
```
(Unchanged from the Part 6B-B baseline — 7 pre-existing deprecation warnings, unrelated to this phase.)

```
$ UV_NO_SYNC=1 UV_FROZEN=1 uv run ruff check .
All checks passed!

$ UV_NO_SYNC=1 UV_FROZEN=1 uv run mypy src
Success: no issues found in 88 source files
```

### Mandatory tests explicitly present
- **XSS-safety proof**: `report_dom.test.js` — `<script>alert(1)</script>` renders as inert text via a
  fake DOM's `serialize()` (HTML-escaped), never as a real child element.
  `report_render.test.js::"renderStatement renders text as inert content and never as markup"` repeats
  the proof at the higher `renderStatement` level using a real fixture statement.
- **Persisted-report-reopen**: `job_flow.test.js` — "a persisted report pointer survives even when there
  is no active job in storage" and "a persisted report pointer is offered for the exact page that produced
  it, with no job involved at all" prove the decision has zero dependency on job state, matching the Part
  6B-A persistence boundary this feature validates.
- **Active-page-change**: `job_flow.test.js` — "a report pointer from a different company's page is never
  offered on the new page" and "an unsupported/unknown current page never surfaces a stale report
  pointer".
- **No global stance/score/BUY-SELL-HOLD**: `report_render.test.js` — regex-asserts the absence of
  buy/sell/hold/bullish/bearish/undervalued/overvalued/cheap/expensive/confidence/risk-score/% across
  Executive Snapshot and Synthesis renders.
- **Coverage never hidden**: `report_render.test.js` — `NOT_APPLICABLE` and `UNAVAILABLE` sections render
  their exact coverage label and reason text rather than disappearing.
- **Valuation refusal honesty**: `report_render.test.js` — an `UNVERIFIED_METRIC_SEMANTICS` Price/Book
  metric never renders a fabricated multiple (`\d+\.\dx` pattern asserted absent).
- **Neutral transcript wording**: `report_render.test.js` — a `TRANSCRIPT_STATEMENT`-attributed statement
  renders "Transcript statement" and never a speaker title (CEO/CFO/Chairman).
- **Static monitoring wording**: `report_render.test.js` — asserts "Monitoring items" wording and the
  absence of "live alerts"/"watching"/"scheduled update" as an unqualified claim (the legitimate
  disclaimer sentence itself says "not live alerts", which is a negation, not a claim).
- **Document corpus wording**: `report_render.test.js` — "Documents in this research run" present; "All
  company documents" / "Complete filing history" (as unqualified claims) absent.
- **Artifact contract**: `api_client.test.js` — exact 3 routes, closed `ARTIFACT_KINDS` enum rejects a
  path-traversal-shaped kind without ever calling `fetch`, report ID is URL-encoded, token never appears
  in the URL, raw-text (non-JSON) parsing.

## 5. Static security audit

Grep audit of `extension/src/` and `manifest.json` (`extension/`):

| Pattern | Result | Explanation |
|---|---|---|
| `innerHTML` / `outerHTML` / `insertAdjacentHTML` | 0 real uses | Only appear inside comments describing the guarantee (`dom.js`, `render.js`). |
| `eval(` / `new Function(` | 0 matches | None anywhere. |
| `XMLHttpRequest` / `WebSocket` / `EventSource` | 0 matches | None anywhere; all network I/O goes through `ApiClient`'s `fetch`. |
| analytics/telemetry/sentry/posthog/mixpanel/amplitude | 0 real matches | Only an unrelated substring in a comment (`coverage_status`). |
| `javascript:` / `data:` / `file:` | 0 real uses | Only appear inside a comment documenting that `safeLink()` rejects them. |
| `fetch(` | 2 call sites, both in `client.js` | The single `ApiClient` client (`_request` and `getArtifact`), both loopback-validated and Bearer-authenticated. No other file calls `fetch`. |
| `http://` / `https://` literals | `storage.js` (default loopback base URL), `client.js` (loopback validation logic/docs), `manifest.json` host_permissions | All pre-existing from Part 6B-A/6B-B, unchanged this phase; no new remote host. |
| `chrome.downloads` | 0 matches | Deliberately avoided — artifact export uses Blob + object URL + `<a download>` instead. |
| `chrome.tabs` / `chrome.scripting` | `chrome.tabs.query`/`onActivated`/`onUpdated` in `sidepanel.js` | Pre-existing from Part 6B-B (unchanged this phase); `chrome.scripting` never used. |
| `content_scripts` / `<all_urls>` | 0 matches | Manifest has no `content_scripts` key; host_permissions remain exactly `["https://www.screener.in/*", "http://127.0.0.1/*"]`. |
| `console.` calls referencing `token` | 0 matches | No token logging anywhere. |
| `token` inside `src/report/*.js` | 0 matches | The report-rendering layer never touches the pairing token; only `sidepanel.js`/`client.js` handle it, exactly as before. |

`manifest.json` permissions are byte-for-byte unchanged from the Part 6B-B commit: `permissions:
["storage", "sidePanel"]`, `host_permissions: ["https://www.screener.in/*", "http://127.0.0.1/*"]`. No new
permission was requested or needed.

## 6. Manual demo plan

1. Start the backend: `stock-research-ai serve-http`.
2. Load the unpacked extension (`extension/`) in Chrome, open the side panel on a supported Screener
   company page.
3. Pair via `stock-research-ai pair-code` and the pairing-code UI.
4. Click "Research Company"; observe real stage progress (no fake percentage).
5. Wait for completion; click "View Research".
6. Confirm the Executive Snapshot renders first: completeness/valuation-evidence badges, supporting
   evidence, disconfirming evidence, unresolved questions, major data gaps — no BUY/SELL/HOLD, no score.
7. Use the grouped nav to open a Financials section; confirm table cells show `display_value`/`unit`
   verbatim.
8. Open a section with `UNAVAILABLE`/`NOT_APPLICABLE` coverage; confirm the badge + reason render instead
   of an empty panel or a hidden section.
9. Open a statement's "Why does the report say this?" drawer; confirm only already-present fields show
   (epistemic class, evidence refs by kind/item_key, limitations) — no fabricated source metadata.
10. Navigate to the Independent Challenger group / any CHALLENGED or INVALID_AGAINST_BUNDLE statement;
    confirm it is visually distinct (banner + badge) but professionally worded.
11. Open Document Corpus; confirm "Documents in this research run" wording and grouped-by-type entries
    with no title field (since the schema has none).
12. Click each of the three artifact buttons; confirm a file downloads via `<a download>` (no Chrome
    "downloads" permission prompt ever appears) with a sanitized filename containing no raw token.
13. Click Back; confirm the normal page-driven panel returns.
14. Navigate the browser tab to a *different* company's Screener page; confirm "View last research" does
    **not** appear (it belongs to the other company).
15. Navigate back to the original company's page, close and reopen the side panel, **restart the backend
    process**, then click "View last research": confirm the report still loads successfully (proving the
    Part 6B-A persistence boundary — completed reports survive server restart even though the in-memory
    job that produced them is gone).

## 7. Acceptance checklist

- [x] Backend remains source of truth; no metric/valuation recalculation, no invented sections, no
      recommendation vocabulary anywhere in `extension/src/report/`.
- [x] No Python file modified.
- [x] Report loading keyed by `report_snapshot_id`, survives backend restart (state machine + pure
      `isReportForCurrentPage` + persistence-independent `openReport`).
- [x] 42 canonical sections grouped into 11 user-facing families + safe `OTHER` fallback for unknown IDs.
- [x] All 8 `SectionCoverageStatus` values rendered with badge + reason; none hidden.
- [x] Completeness rendered with the frozen definition text, never as a score/percentage.
- [x] Executive Snapshot has no computed stance/score/BUY-SELL-HOLD.
- [x] Statements show epistemic class, evidence refs, challenge outcome, quote attribution, limitations,
      via a drill-down drawer using only already-present fields.
- [x] Evidence shown as technical `{kind, item_key}` detail (no URL field exists in the schema).
- [x] `safeLink()` built defensively for forward-compatibility, restricted to http/https,
      `noopener noreferrer`.
- [x] Document corpus says "Documents in this research run", never a completeness claim.
- [x] Neutral "Transcript statement" wording unless `quote_attribution` is structurally
      `MANAGEMENT_STATEMENT`.
- [x] No invented guidance MET/MISSED/EXCEEDED.
- [x] Financial tables use `display_value`/`unit`/`period`/`status` verbatim; `humanizeStatus()` fallback
      for the open `ReportMetric.status` vocabulary.
- [x] Valuation refusals (`UNVERIFIED_METRIC_SEMANTICS`) rendered honestly, never a fabricated multiple.
- [x] Challenger outcomes use the exact 5 `ClaimReviewOutcome` values; CHALLENGED/INVALID_AGAINST_BUNDLE
      visually distinct but professional.
- [x] Monitoring uses static wording only, never "live"/"watching"/scheduled.
- [x] Artifacts via Blob + object URL + `<a download>`; no `chrome.downloads` permission added.
- [x] Artifact requests use only the closed `artifact_kind` enum + `report_id`; filenames sanitized.
- [x] No `innerHTML`/`outerHTML`/`insertAdjacentHTML` anywhere; proven via fake-DOM XSS tests.
- [x] No new Chrome manifest permission.
- [x] No new external dependency (Python or JS).
- [x] 165/165 extension tests pass; 815/815 Python tests pass (unchanged); ruff clean; mypy clean (88
      files).
- [x] Static security audit performed, every match explained above.
- [x] Manual demo plan documented, ending in a backend-restart + persisted-report-reopen proof.

## 8. Final state

`git status --short` (from repo root) shows only `extension/` changes — no Python file touched:

```
 M extension/css/sidepanel.css
 M extension/sidepanel.html
 M extension/src/api/client.js
 M extension/src/job_flow.js
 M extension/src/sidepanel.js
 M extension/src/state.js
 M extension/tests/api_client.test.js
 M extension/tests/job_flow.test.js
 M extension/tests/state.test.js
?? extension/src/report/
?? extension/tests/fixtures/
?? extension/tests/report_dom.test.js
?? extension/tests/report_fixture_fidelity.test.js
?? extension/tests/report_labels.test.js
?? extension/tests/report_real_fixture.test.js
?? extension/tests/report_render.test.js
?? extension/tests/report_section_registry.test.js
?? extension/tests/report_view_model.test.js
```

No commit, tag, or push was made, per instruction.

**PART 6B-C IMPLEMENTATION COMPLETE — AWAITING REVIEW.**
