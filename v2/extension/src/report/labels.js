/**
 * Presentation-only label maps for the exact frozen backend vocabularies (Part 6A/5C).
 * No new status is introduced anywhere in this file - every key mirrors a real,
 * committed Literal from domain/report.py, domain/analysis.py, or domain/challenge.py.
 */

/** SectionCoverageStatus (domain/analysis.py) - exactly these 8 values, no more. */
export const COVERAGE_LABELS = Object.freeze({
  AVAILABLE: "Available",
  PARTIAL: "Partial",
  INSUFFICIENT_INPUT: "Insufficient input",
  NOT_APPLICABLE: "Not applicable",
  UNAVAILABLE: "Unavailable",
  UNVERIFIED_METRIC_SEMANTICS: "Unverified metric semantics",
  INCOMPATIBLE_SCOPE: "Incompatible scope",
  INCOMPATIBLE_PERIOD: "Incompatible period",
});

/** Coverage statuses that represent a genuinely fulfilled section. */
export const AVAILABLE_COVERAGE_STATUSES = Object.freeze(["AVAILABLE", "PARTIAL"]);

export function coverageLabel(status) {
  return COVERAGE_LABELS[status] ?? status;
}

/**
 * ReportMetric.status (domain/report.py) is deliberately a plain, open `str` in the
 * backend schema - not the closed SectionCoverageStatus Literal - because it must also
 * accommodate the broader Part 4/5A calculation-status vocabulary (e.g.
 * DIVISION_BY_ZERO, INVALID_INPUT). Rather than hand-enumerating every possible value
 * (which would risk silently going stale), this falls back to a generic, readable
 * transform of the raw status string when it isn't one of the known coverage labels -
 * never a fabricated or reinterpreted value.
 */
export function humanizeStatus(status) {
  if (!status) return status;
  if (COVERAGE_LABELS[status]) return COVERAGE_LABELS[status];
  return status
    .toLowerCase()
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** EpistemicClass (domain/report.py) - exactly these 5 values. */
export const EPISTEMIC_LABELS = Object.freeze({
  FACT: "Fact",
  CALCULATION: "Calculation",
  INTERPRETATION: "Interpretation",
  ASSUMPTION: "Assumption",
  SCENARIO: "Scenario",
});

export function epistemicLabel(cls) {
  return EPISTEMIC_LABELS[cls] ?? cls;
}

/** ClaimReviewOutcome (domain/challenge.py) - exactly these 5 values. */
export const CHALLENGE_OUTCOME_LABELS = Object.freeze({
  SUPPORTED_BY_BUNDLE: "Supported by evidence",
  SUPPORTED_WITH_LIMITATIONS: "Supported, with limitations",
  CHALLENGED: "Challenged",
  INVALID_AGAINST_BUNDLE: "Invalid against evidence",
  UNVERIFIABLE_FROM_BUNDLE: "Unverifiable from evidence",
});

/** Outcomes that must be visually difficult to miss (still professional, not alarmist). */
export const MATERIAL_CHALLENGE_OUTCOMES = Object.freeze(["CHALLENGED", "INVALID_AGAINST_BUNDLE"]);

export function challengeOutcomeLabel(outcome) {
  return CHALLENGE_OUTCOME_LABELS[outcome] ?? outcome;
}

/** ReportQuoteAttribution (domain/report.py) - neutral wording; never invents a speaker. */
export const QUOTE_ATTRIBUTION_LABELS = Object.freeze({
  MANAGEMENT_STATEMENT: "Management statement",
  TRANSCRIPT_STATEMENT: "Transcript statement",
});

export function quoteAttributionLabel(attribution) {
  return QUOTE_ATTRIBUTION_LABELS[attribution] ?? attribution;
}

/** DocumentType (domain/documents.py). */
export const DOCUMENT_TYPE_LABELS = Object.freeze({
  annual_report: "Annual report",
  investor_presentation: "Investor presentation",
  earnings_presentation: "Earnings presentation",
  concall_transcript: "Concall transcript",
  announcement: "Announcement",
  credit_rating: "Credit rating",
  other: "Other document",
});

export function documentTypeLabel(type) {
  return DOCUMENT_TYPE_LABELS[type] ?? type;
}

/** ResearchCompleteness (domain/report.py) - never rendered as a score or confidence. */
export const COMPLETENESS_LABELS = Object.freeze({
  HIGH: "High",
  MODERATE: "Moderate",
  LIMITED: "Limited",
});

export function completenessLabel(value) {
  return COMPLETENESS_LABELS[value] ?? value;
}

export const COMPLETENESS_EXPLANATION =
  "Reflects completeness relative to currently implemented V1 research capabilities, " +
  "not completeness against every possible future research capability.";

/** ValuationEvidenceState (domain/report.py). */
export const VALUATION_EVIDENCE_LABELS = Object.freeze({
  OBSERVED: "Observed",
  SCENARIO_DEPENDENT: "Scenario-dependent",
  NOT_DETERMINABLE: "Not determinable",
});

export function valuationEvidenceLabel(value) {
  return VALUATION_EVIDENCE_LABELS[value] ?? value;
}

/** MonitoringEntry.direction (domain/report.py) - static comparison, never a live signal. */
export const MONITORING_DIRECTION_LABELS = Object.freeze({
  UP: "Up",
  DOWN: "Down",
  FLAT: "Flat",
  UNKNOWN: "Unknown",
});

export function monitoringDirectionLabel(direction) {
  return MONITORING_DIRECTION_LABELS[direction] ?? direction;
}

/** DocumentCorpusEntry.coverage_status (domain/report.py). */
export const DOCUMENT_COVERAGE_LABELS = Object.freeze({
  COMPLETE: "Complete",
  PARTIAL: "Partial",
  FAILED: "Failed",
});

export function documentCoverageLabel(status) {
  return DOCUMENT_COVERAGE_LABELS[status] ?? status;
}
