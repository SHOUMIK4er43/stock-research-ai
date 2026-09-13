import { test } from "node:test";
import assert from "node:assert/strict";
import { createFakeDocument } from "./fixtures/fake_dom.js";
import { buildFullReport, buildEmptyCorpusReport } from "./fixtures/report_fixtures.js";
import { findSection, groupSections } from "../src/report/view_model.js";
import {
  renderStatement,
  renderTable,
  renderSection,
  renderExecutiveSnapshot,
  renderSynthesis,
  renderDocumentCorpus,
  renderMonitoring,
  renderDataGaps,
  renderNav,
} from "../src/report/render.js";

const FORBIDDEN_STANCE_WORDS = /\b(buy|sell|hold|bullish|bearish|undervalued|overvalued|cheap|expensive)\b/i;
const FORBIDDEN_SCORE_WORDS = /\bconfidence\b|\brisk score\b|%/;

test("renderStatement renders text as inert content and never as markup", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const businessSection = findSection(report, "BUSINESS_MODEL");
  const xssStatement = businessSection.statements[0];
  const node = renderStatement(doc, xssStatement);
  assert.ok(node.textContent.includes("<script>alert(1)</script>"));
  assert.ok(!node.serialize().includes("<script>alert(1)</script>"), "must be escaped, not real markup");
});

test("renderStatement shows epistemic class and challenge outcome badges when present", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const challenged = findSection(report, "BUSINESS_MODEL").statements.find((s) => s.challenge_outcome === "CHALLENGED");
  const node = renderStatement(doc, challenged);
  assert.ok(node.textContent.includes("Fact") || node.textContent.includes("Calculation"));
  assert.ok(node.textContent.toLowerCase().includes("challenged"));
});

test("renderStatement uses neutral transcript wording, never invents a speaker", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const transcriptStatement = findSection(report, "BUSINESS_MODEL").statements.find(
    (s) => s.quote_attribution === "TRANSCRIPT_STATEMENT",
  );
  const node = renderStatement(doc, transcriptStatement);
  assert.ok(node.textContent.includes("Transcript statement"));
  assert.ok(!/\bCEO\b|\bCFO\b|\bChairman\b/i.test(node.textContent));
});

test("renderStatement's evidence drawer only exposes fields already present in the JSON", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const statement = findSection(report, "BUSINESS_MODEL").statements[1];
  const node = renderStatement(doc, statement);
  const drawer = node.children.find((c) => c.tagName === "DETAILS");
  assert.ok(drawer);
  assert.equal(drawer.children[0].textContent, "Why does the report say this?");
  assert.ok(node.textContent.includes("concall_q4fy24_chunk_12"));
});

test("renderTable uses display_value + unit verbatim, never recalculating", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const table = findSection(report, "ANNUAL_FINANCIAL_PERFORMANCE").tables[0];
  const node = renderTable(doc, table);
  assert.ok(node.textContent.includes("12,340 INR cr"));
});

test("renderTable falls back to humanized status, never a fabricated value, when display_value is absent", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const table = findSection(report, "ANNUAL_FINANCIAL_PERFORMANCE").tables[0];
  const node = renderTable(doc, table);
  assert.ok(node.textContent.includes("Revenue"));
  const tableEl = node.children[0];
  const tbody = tableEl.children[2];
  const row = tbody.children[0];
  const cell = row.children[2];
  assert.equal(cell.getAttribute("title"), "Insufficient input");
});

test("renderTable renders an UNVERIFIED_METRIC_SEMANTICS refusal honestly, not a fabricated multiple", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const table = findSection(report, "VALUATION").tables[0];
  const node = renderTable(doc, table);
  assert.ok(node.textContent.includes("Price / Book"));
  assert.ok(!/\d+\.\d+x/.test(node.textContent), "must never show a fabricated P/B multiple");
});

test("renderSection shows a coverage badge and reason for a non-available section, never a giant empty panel", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const guidanceEntry = groupSections(report).flatMap((g) => g.sections).find((s) => s.id === "MANAGEMENT_GUIDANCE_AND_COMMITMENTS");
  const node = renderSection(doc, { section: guidanceEntry.section, label: guidanceEntry.label });
  assert.ok(node.textContent.includes("Unavailable"));
  assert.ok(node.textContent.includes("No structured guidance was retained"));
});

test("renderSection for NOT_APPLICABLE shows the exact coverage status, never hidden entirely", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const section = findSection(report, "SECTOR_SPECIFIC_KPI_ANALYSIS");
  const node = renderSection(doc, { section, label: "Sector-Specific KPIs" });
  assert.ok(node.textContent.includes("Not applicable"));
});

test("renderExecutiveSnapshot never renders a stance, score, or BUY/SELL/HOLD label", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const node = renderExecutiveSnapshot(doc, report.executive_snapshot);
  const text = node.textContent;
  assert.ok(!FORBIDDEN_STANCE_WORDS.test(text), text);
  assert.ok(!FORBIDDEN_SCORE_WORDS.test(text), text);
  assert.ok(text.includes("Completeness: Moderate"));
  assert.ok(text.includes("Valuation evidence: Scenario-dependent"));
});

test("renderExecutiveSnapshot includes the frozen completeness definition text", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const node = renderExecutiveSnapshot(doc, report.executive_snapshot);
  assert.ok(node.textContent.includes("V1 research capabilities"));
});

test("renderExecutiveSnapshot surfaces disconfirming evidence and unresolved questions", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const node = renderExecutiveSnapshot(doc, report.executive_snapshot);
  assert.ok(node.textContent.includes("npa_ratio_fy24"));
  assert.ok(node.textContent.includes("Why did related-party disclosures change"));
});

test("renderSynthesis never renders a global directional stance", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const node = renderSynthesis(doc, report.synthesis);
  assert.ok(!FORBIDDEN_STANCE_WORDS.test(node.textContent));
});

test("renderDocumentCorpus uses 'Documents in this research run' wording, never a completeness claim", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const node = renderDocumentCorpus(doc, report);
  const text = node.textContent;
  assert.ok(text.includes("Documents in this research run"));
  // These exact phrases must never appear as an unqualified completeness claim - the
  // disclaimer itself legitimately says "not a claim of complete filing history", which
  // is a negation, not a claim, so it is checked for separately by exact wording above.
  assert.ok(!text.includes("All company documents"));
  assert.ok(!text.includes("Complete filing history"));
});

test("renderDocumentCorpus handles an empty corpus without crashing", () => {
  const doc = createFakeDocument();
  const report = buildEmptyCorpusReport();
  const node = renderDocumentCorpus(doc, report);
  assert.ok(node.textContent.includes("No documents were pinned"));
});

test("renderMonitoring uses static wording, never 'live' or 'watching'", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const node = renderMonitoring(doc, report.monitoring);
  const text = node.textContent.toLowerCase();
  assert.ok(text.includes("monitoring items"));
  // The disclaimer itself legitimately says "not live alerts" - a negation, not a claim.
  // What must never appear is an unqualified live/scheduled-monitoring claim.
  assert.ok(!text.includes("live alerts to"));
  assert.ok(!/\bwatching\b|\bscheduled update/.test(text));
});

test("renderMonitoring handles an empty list honestly", () => {
  const doc = createFakeDocument();
  const node = renderMonitoring(doc, []);
  assert.ok(node.textContent.includes("No monitoring items"));
});

test("renderDataGaps renders real structured limitations", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const node = renderDataGaps(doc, report.limitations);
  assert.ok(node.textContent.includes("Segment-level profitability"));
});

test("renderNav groups sections and marks the active one", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const groups = groupSections(report);
  const firstId = groups[0].sections[0].id;
  const nav = renderNav(doc, groups, firstId, () => {});
  assert.ok(nav.children.length > 0);
  const firstButton = nav.children[0].children.find((c) => c.tagName === "BUTTON");
  assert.ok(firstButton.className.includes("active"));
});

// --- Pre-commit audit additions: unknown-status fail-safety, additional XSS payloads --

test("renderTable never crashes on a genuinely unknown future ReportMetric.status and never fabricates a value", () => {
  const doc = createFakeDocument();
  const table = {
    table_id: "t1",
    title: "T",
    section: "VALUATION",
    columns: ["Metric"],
    rows: [
      [
        {
          label: "Some Future Metric",
          display_value: null,
          unit: null,
          period: null,
          reporting_scope: null,
          status: "SOME_FUTURE_METRIC_STATUS_2027",
          evidence_ref: null,
          issues: [],
        },
      ],
    ],
  };
  const node = renderTable(doc, table);
  assert.ok(node.textContent.includes("Some Future Metric"));
  const cell = node.children[0].children[2].children[0].children[0];
  // humanizeStatus falls back to a generic title-cased transform - never crashes, never
  // invents a semantic meaning (e.g. never renders it as if it were "Available").
  assert.equal(cell.getAttribute("title"), "Some Future Metric Status 2027");
});

test("renderSection never treats an unknown future coverage status as success/available", () => {
  const doc = createFakeDocument();
  const section = {
    section: "VALUATION",
    coverage: { status: "SOME_FUTURE_COVERAGE_STATUS", reason: "A brand new backend coverage state.", reason_code: null },
    tables: [],
    statements: [],
  };
  const node = renderSection(doc, { section, label: "Valuation" });
  assert.ok(node.textContent.includes("SOME_FUTURE_COVERAGE_STATUS"));
  const badge = node.children[0].children[1];
  assert.equal(badge.className.includes("badge-ok"), false, "an unknown coverage status must never render as the success/available style");
  assert.ok(badge.className.includes("badge-muted"));
});

test("renderStatement renders an <img onerror> payload as inert text, never a real image element", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const malicious = '<img src=x onerror=alert(1)>';
  const statement = {
    ...findSection(report, "BUSINESS_MODEL").statements[0],
    text: malicious,
  };
  const node = renderStatement(doc, statement);
  assert.ok(node.textContent.includes(malicious));
  assert.ok(!node.serialize().includes("<img"), "must never become a real <img> element");
  assert.equal(node.children.filter((c) => c.tagName === "IMG").length, 0);
});

test("renderStatement renders a javascript: payload embedded in text as inert text", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const statement = {
    ...findSection(report, "BUSINESS_MODEL").statements[0],
    text: "Click here: javascript:alert(1)",
  };
  const node = renderStatement(doc, statement);
  assert.ok(node.textContent.includes("javascript:alert(1)"));
  assert.equal(node.serialize().includes("<a "), false, "must never become a real anchor element");
});

test("renderNav's click handler invokes onSelect with the section id", () => {
  const doc = createFakeDocument();
  const report = buildFullReport();
  const groups = groupSections(report);
  const targetId = groups[0].sections[0].id;
  let selected = null;
  const nav = renderNav(doc, groups, null, (id) => {
    selected = id;
  });
  const firstButton = nav.children[0].children.find((c) => c.tagName === "BUTTON");
  firstButton.dispatch("click");
  assert.equal(selected, targetId);
});
