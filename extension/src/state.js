/**
 * Pure client UI state model (Part 6B-B). No Chrome APIs, no network calls - a
 * deterministic function of already-known facts, unit-testable in isolation.
 */

export const States = Object.freeze({
  SERVICE_UNAVAILABLE: "SERVICE_UNAVAILABLE",
  INCOMPATIBLE: "INCOMPATIBLE",
  UNPAIRED: "UNPAIRED",
  UNSUPPORTED_PAGE: "UNSUPPORTED_PAGE",
  READY: "READY",
  LAUNCHING: "LAUNCHING",
  RUNNING: "RUNNING",
  COMPLETED: "COMPLETED",
  FAILED: "FAILED",
});

/**
 * @typedef {Object} StateFacts
 * @property {boolean} serviceReachable
 * @property {boolean} versionCompatible
 * @property {boolean} paired
 * @property {boolean} pageSupported
 * @property {boolean} [launching]
 * @property {{status: string} | null} [job]
 */

/**
 * Precedence, most fundamental first: an unreachable service outranks everything else;
 * an incompatible version outranks pairing; being unpaired outranks page support; page
 * support outranks an in-flight job (a job cannot exist without having been on a
 * supported page, but this keeps the function honest under any input); a transient
 * "launching" flag outranks any stale prior job; finally the job's own status.
 *
 * @param {StateFacts} facts
 * @returns {string} one of States
 */
export function deriveState(facts) {
  const {
    serviceReachable,
    versionCompatible,
    paired,
    pageSupported,
    launching = false,
    job = null,
  } = facts;

  if (!serviceReachable) return States.SERVICE_UNAVAILABLE;
  if (!versionCompatible) return States.INCOMPATIBLE;
  if (!paired) return States.UNPAIRED;
  if (!pageSupported) return States.UNSUPPORTED_PAGE;
  if (launching) return States.LAUNCHING;
  if (job) {
    if (job.status === "COMPLETED") return States.COMPLETED;
    if (job.status === "FAILED") return States.FAILED;
    return States.RUNNING; // QUEUED or RUNNING
  }
  return States.READY;
}

/** Real backend stages only (Part 6B0 ProgressSink) - never a fake percentage. */
export const STAGE_LABELS = Object.freeze({
  PREPARING: "Preparing research",
  EVIDENCE: "Collecting & processing evidence",
  ANALYSIS: "Institutional analysis",
  CHALLENGE: "Independent challenge",
  REPORT: "Building final report",
  COMPLETED: "Complete",
});

/** @param {string | null | undefined} stage @returns {string} */
export function stageLabel(stage) {
  return STAGE_LABELS[stage] ?? "Working…";
}

/** Typed backend failure codes (Part 6B-A) mapped to concise user-facing text. */
export const FAILURE_LABELS = Object.freeze({
  UNSUPPORTED_PORTAL: "This portal is not supported.",
  UNSUPPORTED_SOURCE_URL: "This is not a supported Screener company page.",
  LOCAL_CONFIGURATION_ERROR: "The local service is misconfigured.",
  ANALYST_UNAVAILABLE: "The live analyst is not configured.",
  EVIDENCE_ACQUISITION_FAILED: "Research could not collect the required company evidence.",
  ANALYSIS_FAILED: "Institutional analysis failed.",
  CHALLENGE_FAILED: "Independent challenge failed.",
  REPORT_GENERATION_FAILED: "Final report generation failed.",
  INTEGRITY_FAILURE: "Stored research failed an integrity check.",
  INTERNAL_ERROR: "An internal error occurred.",
  JOB_LOST_AFTER_SERVER_RESTART: "The local service restarted before this research finished.",
});

/** @param {string | null | undefined} code @returns {string} */
export function failureLabel(code) {
  return FAILURE_LABELS[code] ?? "Research failed for an unknown reason.";
}
