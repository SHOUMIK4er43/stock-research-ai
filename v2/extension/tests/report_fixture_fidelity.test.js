/**
 * Cross-language contract fidelity (Part 6B-C pre-commit audit, item 2/3): walks the
 * fixture tree and proves every enum-shaped value used anywhere in it belongs to the
 * REAL closed backend Literal - not a frontend-invented alternative. This is what
 * caught (and now guards against regressing) the fact that an earlier draft of
 * report_fixtures.js used plausible-looking but nonexistent EvidenceKind values
 * ("FINANCIAL_STATEMENT_LINE_ITEM", "CONCALL_TRANSCRIPT_CHUNK", "DOCUMENT_SECTION")
 * instead of the real 13-value Literal from domain/analysis.py.
 *
 * Every list below is copied verbatim from the frozen Python Literal/StrEnum
 * definitions (domain/analysis.py, domain/report.py, domain/challenge.py,
 * domain/documents.py) - if the backend ever changes one of these, this test must be
 * updated deliberately, not silently widened.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildFullReport, REAL_EVIDENCE_KINDS } from "./fixtures/report_fixtures.js";

// domain/analysis.py::SectionCoverageStatus
const REAL_COVERAGE_STATUSES = [
  "AVAILABLE",
  "PARTIAL",
  "INSUFFICIENT_INPUT",
  "NOT_APPLICABLE",
  "UNAVAILABLE",
  "UNVERIFIED_METRIC_SEMANTICS",
  "INCOMPATIBLE_SCOPE",
  "INCOMPATIBLE_PERIOD",
];
// domain/report.py::EpistemicClass
const REAL_EPISTEMIC_CLASSES = ["FACT", "CALCULATION", "INTERPRETATION", "ASSUMPTION", "SCENARIO"];
// domain/challenge.py::ClaimReviewOutcome
const REAL_CHALLENGE_OUTCOMES = [
  "SUPPORTED_BY_BUNDLE",
  "SUPPORTED_WITH_LIMITATIONS",
  "CHALLENGED",
  "INVALID_AGAINST_BUNDLE",
  "UNVERIFIABLE_FROM_BUNDLE",
];
// domain/report.py::ReportQuoteAttribution
const REAL_QUOTE_ATTRIBUTIONS = ["MANAGEMENT_STATEMENT", "TRANSCRIPT_STATEMENT"];
// domain/documents.py::DocumentType (StrEnum values)
const REAL_DOCUMENT_TYPES = [
  "annual_report",
  "investor_presentation",
  "earnings_presentation",
  "concall_transcript",
  "announcement",
  "credit_rating",
  "other",
];
// domain/report.py::DocumentCorpusEntry.coverage_status
const REAL_DOCUMENT_COVERAGE_STATUSES = ["COMPLETE", "PARTIAL", "FAILED"];
// domain/report.py::MonitoringEntry.direction
const REAL_MONITORING_DIRECTIONS = ["UP", "DOWN", "FLAT", "UNKNOWN"];
// domain/report.py::ResearchCompleteness
const REAL_COMPLETENESS_VALUES = ["HIGH", "MODERATE", "LIMITED"];
// domain/report.py::ValuationEvidenceState
const REAL_VALUATION_EVIDENCE_STATES = ["OBSERVED", "SCENARIO_DEPENDENT", "NOT_DETERMINABLE"];
// domain/report.py::ReportCoverageReasonCode
const REAL_REASON_CODES = [
  "UPSTREAM_CAPABILITY_NOT_IMPLEMENTED",
  "REQUIRED_STRUCTURED_GUIDANCE_NOT_RETAINED",
  "NO_DOCUMENTS_OF_TYPE_PINNED",
  "NARRATOR_NOT_CONFIGURED",
];

function isReportSectionCoverageShape(obj) {
  return obj && typeof obj === "object" && "status" in obj && "reason" in obj;
}

/** Recursively visits every plain object/array in the fixture, validating enum-shaped
 * fields by key name. Deliberately does NOT validate ReportMetric.status (a plain open
 * str per the real schema, not a closed enum) - only ReportSectionCoverage.status
 * (identified by shape: an object with both `status` and `reason`) is checked. */
function walk(node, violations) {
  if (Array.isArray(node)) {
    for (const item of node) walk(item, violations);
    return;
  }
  if (!node || typeof node !== "object") return;

  if ("kind" in node && "owning_snapshot_id" in node) {
    if (!REAL_EVIDENCE_KINDS.includes(node.kind)) {
      violations.push(`EvidenceReference.kind "${node.kind}" is not a real EvidenceKind`);
    }
  }
  if ("epistemic_class" in node) {
    if (!REAL_EPISTEMIC_CLASSES.includes(node.epistemic_class)) {
      violations.push(`epistemic_class "${node.epistemic_class}" is not real`);
    }
  }
  if ("challenge_outcome" in node && node.challenge_outcome !== null && node.challenge_outcome !== undefined) {
    if (!REAL_CHALLENGE_OUTCOMES.includes(node.challenge_outcome)) {
      violations.push(`challenge_outcome "${node.challenge_outcome}" is not real`);
    }
  }
  if ("quote_attribution" in node && node.quote_attribution !== null && node.quote_attribution !== undefined) {
    if (!REAL_QUOTE_ATTRIBUTIONS.includes(node.quote_attribution)) {
      violations.push(`quote_attribution "${node.quote_attribution}" is not real`);
    }
  }
  if ("document_type" in node) {
    if (!REAL_DOCUMENT_TYPES.includes(node.document_type)) {
      violations.push(`document_type "${node.document_type}" is not real`);
    }
  }
  if ("coverage_status" in node) {
    if (!REAL_DOCUMENT_COVERAGE_STATUSES.includes(node.coverage_status)) {
      violations.push(`coverage_status "${node.coverage_status}" is not real`);
    }
  }
  if ("direction" in node) {
    if (!REAL_MONITORING_DIRECTIONS.includes(node.direction)) {
      violations.push(`direction "${node.direction}" is not real`);
    }
  }
  if ("research_completeness" in node) {
    if (!REAL_COMPLETENESS_VALUES.includes(node.research_completeness)) {
      violations.push(`research_completeness "${node.research_completeness}" is not real`);
    }
  }
  if ("valuation_evidence_state" in node) {
    if (!REAL_VALUATION_EVIDENCE_STATES.includes(node.valuation_evidence_state)) {
      violations.push(`valuation_evidence_state "${node.valuation_evidence_state}" is not real`);
    }
  }
  if ("reason_code" in node && node.reason_code !== null && node.reason_code !== undefined) {
    if (!REAL_REASON_CODES.includes(node.reason_code)) {
      violations.push(`reason_code "${node.reason_code}" is not real`);
    }
  }
  if (isReportSectionCoverageShape(node)) {
    if (!REAL_COVERAGE_STATUSES.includes(node.status)) {
      violations.push(`ReportSectionCoverage.status "${node.status}" is not real`);
    }
  }

  for (const value of Object.values(node)) {
    walk(value, violations);
  }
}

test("every enum-shaped value in the full report fixture belongs to a real, closed backend Literal", () => {
  const report = buildFullReport();
  const violations = [];
  walk(report, violations);
  assert.deepEqual(violations, []);
});

test("REAL_EVIDENCE_KINDS is exactly the 13-value closed EvidenceKind Literal", () => {
  assert.equal(REAL_EVIDENCE_KINDS.length, 13);
  assert.equal(new Set(REAL_EVIDENCE_KINDS).size, 13);
});
