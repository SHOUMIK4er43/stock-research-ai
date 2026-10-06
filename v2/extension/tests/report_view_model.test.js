import { test } from "node:test";
import assert from "node:assert/strict";
import {
  groupSections,
  isSectionAvailable,
  hasSectionContent,
  groupDocumentCorpus,
  materialChallengedStatements,
  findSection,
} from "../src/report/view_model.js";
import { GROUPS } from "../src/report/section_registry.js";
import { buildFullReport, buildEmptyCorpusReport } from "./fixtures/report_fixtures.js";

test("groupSections groups every present section and preserves canonical group order", () => {
  const report = buildFullReport();
  const groups = groupSections(report);
  const groupOrderIndex = (g) => GROUPS.indexOf(g);
  for (let i = 1; i < groups.length; i++) {
    assert.ok(groupOrderIndex(groups[i - 1].group) <= groupOrderIndex(groups[i].group));
  }
  const totalSections = groups.reduce((sum, g) => sum + g.sections.length, 0);
  assert.equal(totalSections, report.sections.length);
});

test("groupSections skips empty groups entirely (never a blank group header)", () => {
  const report = buildFullReport();
  const groups = groupSections(report);
  for (const group of groups) {
    assert.ok(group.sections.length > 0);
  }
});

test("groupSections places an unknown future section id into OTHER without crashing", () => {
  const report = buildFullReport();
  const groups = groupSections(report);
  const other = groups.find((g) => g.group === "OTHER");
  assert.ok(other);
  assert.ok(other.sections.some((s) => s.id === "SOME_FUTURE_SECTION_NOT_YET_KNOWN"));
});

test("isSectionAvailable is true only for AVAILABLE coverage", () => {
  assert.equal(isSectionAvailable({ coverage: { status: "AVAILABLE" } }), true);
  assert.equal(isSectionAvailable({ coverage: { status: "PARTIAL" } }), false);
  assert.equal(isSectionAvailable({ coverage: { status: "UNAVAILABLE" } }), false);
});

test("isSectionAvailable fails safe (false) for a genuinely unknown future coverage status - never defaults to available", () => {
  assert.equal(isSectionAvailable({ coverage: { status: "SOME_FUTURE_COVERAGE_STATUS" } }), false);
});

test("hasSectionContent is true when tables or statements are present", () => {
  assert.equal(hasSectionContent({ tables: [], statements: [] }), false);
  assert.equal(hasSectionContent({ tables: [{}], statements: [] }), true);
  assert.equal(hasSectionContent({ tables: [], statements: [{}] }), true);
});

test("groupDocumentCorpus groups entries by document_type", () => {
  const report = buildFullReport();
  const groups = groupDocumentCorpus(report);
  const types = groups.map((g) => g.documentType).sort();
  assert.deepEqual(types, ["annual_report", "concall_transcript"]);
});

test("groupDocumentCorpus returns an empty array for an empty corpus", () => {
  const report = buildEmptyCorpusReport();
  assert.deepEqual(groupDocumentCorpus(report), []);
});

test("materialChallengedStatements finds CHALLENGED and INVALID_AGAINST_BUNDLE statements across sections", () => {
  const report = buildFullReport();
  const found = materialChallengedStatements(report);
  assert.equal(found.length, 2);
  const outcomes = found.map((f) => f.statement.challenge_outcome).sort();
  assert.deepEqual(outcomes, ["CHALLENGED", "INVALID_AGAINST_BUNDLE"]);
});

test("findSection returns the exact section or null", () => {
  const report = buildFullReport();
  assert.ok(findSection(report, "BUSINESS_MODEL"));
  assert.equal(findSection(report, "NONEXISTENT_SECTION"), null);
});
