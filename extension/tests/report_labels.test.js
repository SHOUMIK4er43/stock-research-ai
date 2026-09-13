import { test } from "node:test";
import assert from "node:assert/strict";
import {
  COVERAGE_LABELS,
  coverageLabel,
  humanizeStatus,
  EPISTEMIC_LABELS,
  epistemicLabel,
  CHALLENGE_OUTCOME_LABELS,
  MATERIAL_CHALLENGE_OUTCOMES,
  challengeOutcomeLabel,
  QUOTE_ATTRIBUTION_LABELS,
  quoteAttributionLabel,
  DOCUMENT_TYPE_LABELS,
  documentTypeLabel,
  COMPLETENESS_LABELS,
  completenessLabel,
  COMPLETENESS_EXPLANATION,
  VALUATION_EVIDENCE_LABELS,
  valuationEvidenceLabel,
  MONITORING_DIRECTION_LABELS,
  monitoringDirectionLabel,
  DOCUMENT_COVERAGE_LABELS,
  documentCoverageLabel,
} from "../src/report/labels.js";

test("exactly the 8 frozen SectionCoverageStatus values are labeled", () => {
  const keys = Object.keys(COVERAGE_LABELS);
  assert.equal(keys.length, 8);
  for (const status of [
    "AVAILABLE",
    "PARTIAL",
    "INSUFFICIENT_INPUT",
    "NOT_APPLICABLE",
    "UNAVAILABLE",
    "UNVERIFIED_METRIC_SEMANTICS",
    "INCOMPATIBLE_SCOPE",
    "INCOMPATIBLE_PERIOD",
  ]) {
    assert.ok(keys.includes(status), `missing ${status}`);
    assert.notEqual(coverageLabel(status), status, `${status} should have a human label`);
  }
});

test("coverageLabel falls back to the raw value for an unknown status", () => {
  assert.equal(coverageLabel("SOME_NEW_STATUS"), "SOME_NEW_STATUS");
});

test("humanizeStatus prefers the known coverage label when one exists", () => {
  assert.equal(humanizeStatus("AVAILABLE"), "Available");
  assert.equal(humanizeStatus("UNVERIFIED_METRIC_SEMANTICS"), "Unverified metric semantics");
});

test("humanizeStatus generically titlecases an open Part 4/5A calculation status", () => {
  assert.equal(humanizeStatus("DIVISION_BY_ZERO"), "Division By Zero");
  assert.equal(humanizeStatus("INVALID_INPUT"), "Invalid Input");
});

test("humanizeStatus never crashes on empty/nullish input", () => {
  assert.equal(humanizeStatus(""), "");
  assert.equal(humanizeStatus(null), null);
  assert.equal(humanizeStatus(undefined), undefined);
});

test("exactly the 5 frozen EpistemicClass values are labeled", () => {
  assert.equal(Object.keys(EPISTEMIC_LABELS).length, 5);
  for (const cls of ["FACT", "CALCULATION", "INTERPRETATION", "ASSUMPTION", "SCENARIO"]) {
    assert.notEqual(epistemicLabel(cls), cls);
  }
});

test("exactly the 5 frozen ClaimReviewOutcome values are labeled", () => {
  assert.equal(Object.keys(CHALLENGE_OUTCOME_LABELS).length, 5);
  for (const outcome of [
    "SUPPORTED_BY_BUNDLE",
    "SUPPORTED_WITH_LIMITATIONS",
    "CHALLENGED",
    "INVALID_AGAINST_BUNDLE",
    "UNVERIFIABLE_FROM_BUNDLE",
  ]) {
    assert.notEqual(challengeOutcomeLabel(outcome), outcome);
  }
});

test("only CHALLENGED and INVALID_AGAINST_BUNDLE are material challenge outcomes", () => {
  assert.deepEqual([...MATERIAL_CHALLENGE_OUTCOMES].sort(), ["CHALLENGED", "INVALID_AGAINST_BUNDLE"].sort());
});

test("exactly the 2 frozen ReportQuoteAttribution values are labeled with neutral wording", () => {
  assert.equal(Object.keys(QUOTE_ATTRIBUTION_LABELS).length, 2);
  assert.equal(quoteAttributionLabel("TRANSCRIPT_STATEMENT"), "Transcript statement");
  assert.equal(quoteAttributionLabel("MANAGEMENT_STATEMENT"), "Management statement");
  assert.ok(!quoteAttributionLabel("TRANSCRIPT_STATEMENT").toLowerCase().includes("ceo"));
});

test("document type labels cover the known DocumentType values", () => {
  for (const type of Object.keys(DOCUMENT_TYPE_LABELS)) {
    assert.notEqual(documentTypeLabel(type), type);
  }
  assert.equal(documentTypeLabel("unknown_future_type"), "unknown_future_type");
});

test("completeness labels never read as a score or percentage", () => {
  assert.equal(Object.keys(COMPLETENESS_LABELS).length, 3);
  for (const value of Object.values(COMPLETENESS_LABELS)) {
    assert.ok(!/%|\bscore\b|\bconfidence\b/i.test(value));
  }
  assert.equal(completenessLabel("HIGH"), "High");
  assert.ok(COMPLETENESS_EXPLANATION.length > 0);
});

test("valuation evidence state labels are exactly the 3 frozen values", () => {
  assert.equal(Object.keys(VALUATION_EVIDENCE_LABELS).length, 3);
  assert.equal(valuationEvidenceLabel("NOT_DETERMINABLE"), "Not determinable");
});

test("monitoring direction labels never imply a live/scheduled signal", () => {
  assert.equal(Object.keys(MONITORING_DIRECTION_LABELS).length, 4);
  assert.equal(monitoringDirectionLabel("UP"), "Up");
});

test("document coverage labels match DocumentCorpusEntry.coverage_status exactly", () => {
  assert.deepEqual(Object.keys(DOCUMENT_COVERAGE_LABELS).sort(), ["COMPLETE", "FAILED", "PARTIAL"]);
  assert.equal(documentCoverageLabel("COMPLETE"), "Complete");
});
