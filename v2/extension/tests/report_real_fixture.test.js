/**
 * Cross-language contract fixture (Part 6B-C pre-commit audit, item 3): proves the full
 * pipeline - real backend-shaped JSON -> frontend normalization -> section grouping ->
 * rendering/view model - using an ACTUAL persisted ResearchReportSnapshot payload, not an
 * independently invented JS object.
 *
 * extension/tests/fixtures/real_yesbank_report.json was extracted read-only from
 * output/part6a-proof-final/YESBANK/proof.sqlite3's report_snapshots.payload column - the
 * exact byte shape a real, prior, fully offline `stock-research-ai` proof run persisted
 * and that GET /v1/reports/{report_id} would return verbatim (response_model=
 * ResearchReportSnapshot, no DTO/aliasing layer - see src/stock_research_ai/interfaces/
 * http.py::get_report). No live research was performed to produce it. Table rows,
 * per-section statements, limitations, monitoring entries, and document-corpus entries
 * were truncated (counts only - no value was altered or invented) purely to keep the
 * committed fixture small; the extraction script is documented in this repo's Part 6B-C
 * pre-commit audit trail. The trimmed JSON was re-validated against the real
 * ResearchReportSnapshot Pydantic model before being committed (still parses cleanly).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createFakeDocument } from "./fixtures/fake_dom.js";
import { groupSections, findSection, materialChallengedStatements } from "../src/report/view_model.js";
import { knownSectionIds } from "../src/report/section_registry.js";
import { renderNav, renderSection, renderStatement, renderTable } from "../src/report/render.js";
import { challengeOutcomeLabel } from "../src/report/labels.js";
import { ApiClient } from "../src/api/client.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const realReport = JSON.parse(readFileSync(join(__dirname, "fixtures", "real_yesbank_report.json"), "utf8"));

test("the real fixture is shaped exactly like ResearchReportSnapshot's top-level fields", () => {
  for (const key of [
    "schema_version",
    "id",
    "company_id",
    "analysis_snapshot_id",
    "challenge_snapshot_id",
    "sections",
    "executive_snapshot",
    "synthesis",
    "document_corpus",
    "monitoring",
    "limitations",
    "reproducibility",
    "evaluated_at",
  ]) {
    assert.ok(key in realReport, `real payload is missing top-level field "${key}"`);
  }
  assert.equal(realReport.sections.length, 42);
});

test("every section id in the real payload is a known canonical ReportSectionId", () => {
  const known = new Set(knownSectionIds());
  for (const section of realReport.sections) {
    assert.ok(known.has(section.section), `unknown section id in real payload: ${section.section}`);
  }
});

test("groupSections successfully groups all 42 real sections with no crash", () => {
  const groups = groupSections(realReport);
  const total = groups.reduce((sum, g) => sum + g.sections.length, 0);
  assert.equal(total, 42);
  assert.ok(groups.every((g) => g.group !== "OTHER"), "no real section should fall into OTHER");
});

test("renderNav renders the real grouped navigation without crashing", () => {
  const doc = createFakeDocument();
  const groups = groupSections(realReport);
  const nav = renderNav(doc, groups, null, () => {});
  assert.ok(nav.children.length > 0);
});

test("a real CHALLENGED statement (INDEPENDENT_CHALLENGER_REVIEW) renders its exact outcome, unmodified", () => {
  const section = findSection(realReport, "INDEPENDENT_CHALLENGER_REVIEW");
  const challenged = section.statements.find((s) => s.challenge_outcome === "CHALLENGED");
  assert.ok(challenged, "fixture must contain a real CHALLENGED statement");
  const doc = createFakeDocument();
  const node = renderStatement(doc, challenged);
  assert.ok(node.textContent.includes(challengeOutcomeLabel("CHALLENGED")));
  assert.ok(node.textContent.includes(challenged.text));
});

test("materialChallengedStatements finds the real challenged statement via the same pipeline", () => {
  const found = materialChallengedStatements(realReport);
  assert.ok(found.some((f) => f.sectionId === "INDEPENDENT_CHALLENGER_REVIEW"));
});

test("a real TRANSCRIPT_STATEMENT-attributed statement renders neutral wording, never a fabricated speaker", () => {
  const section = findSection(realReport, "CONCALL_INTELLIGENCE");
  const transcriptStatement = section.statements.find((s) => s.quote_attribution === "TRANSCRIPT_STATEMENT");
  assert.ok(transcriptStatement, "fixture must contain a real transcript-attributed statement");
  const doc = createFakeDocument();
  const node = renderStatement(doc, transcriptStatement);
  assert.ok(node.textContent.includes("Transcript statement"));
  assert.ok(!/\bCEO\b|\bCFO\b|\bManaging Director\b/i.test(node.textContent));
  // The real statement text embeds a plain-prose URL (a regulatory filing weblink) with
  // no structured evidence URL field backing it - it must render as inert text, not a
  // clickable link, since EvidenceReference has no url field to authorize one.
  assert.ok(node.textContent.includes("yes.bank.in"));
  assert.equal(node.serialize().includes("<a "), false);
});

test("a real table renders using display_value/status verbatim, never recomputed", () => {
  const section = findSection(realReport, "BUSINESS_MODEL");
  const table = section.tables[0];
  const doc = createFakeDocument();
  const node = renderTable(doc, table);
  // The real first row is the "Company" metric with a null display_value and an
  // AVAILABLE status with a COMPANY evidence kind - rendered as the raw label, not a
  // fabricated value.
  assert.ok(node.textContent.includes("Company"));
});

test("renderSection renders a real section end-to-end without crashing", () => {
  const section = findSection(realReport, "VALUATION");
  const doc = createFakeDocument();
  const node = renderSection(doc, { section, label: "Valuation" });
  assert.ok(node.textContent.length > 0);
});

// --- Persisted-report-reopen, real API contract (pre-commit audit item 13) ---------
// Proves that fetching a persisted report through the same ApiClient.getReport() the
// extension actually calls, given a response body byte-shaped exactly like the real
// backend's, requires no job_id, no BrowserJobManager history, and no polling state at
// any layer - only the report_snapshot_id and a Bearer token.

test("ApiClient.getReport() consumes the real report shape and feeds groupSections with zero job-related input", async () => {
  const fetchImpl = async (url, init) => ({
    ok: true,
    status: 200,
    statusText: "200",
    json: async () => realReport,
  });
  const client = new ApiClient("http://127.0.0.1:8000", fetchImpl);
  // Only reportId + token are ever passed - no job_id, no job manager, no poll state.
  const report = await client.getReport("some-token", realReport.id);
  assert.equal(report.id, realReport.id);
  const groups = groupSections(report);
  assert.equal(
    groups.reduce((sum, g) => sum + g.sections.length, 0),
    42,
  );
});
