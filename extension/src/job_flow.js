/**
 * Pure job-update/reconnection decision logic (Part 6B-B), extracted out of the DOM
 * controller so it is unit-testable without Chrome. No research/financial semantics -
 * only "what should local storage do next" given an already-fetched job/error.
 */

/** @param {string} status @returns {boolean} */
export function isTerminal(status) {
  return status === "COMPLETED" || status === "FAILED";
}

/**
 * @param {{status: string, report_snapshot_id?: string|null, source_url?: string}} job
 * @returns {{clearActiveJob: boolean, lastReport: {reportId: string, sourceUrl: string} | null}}
 */
export function storageActionsForJobUpdate(job) {
  if (!isTerminal(job.status)) {
    return { clearActiveJob: false, lastReport: null };
  }
  const lastReport =
    job.status === "COMPLETED" && job.report_snapshot_id
      ? { reportId: job.report_snapshot_id, sourceUrl: job.source_url }
      : null;
  // A previously stored report pointer (from an earlier completed job) is never
  // discarded merely because a *different* job just failed - only ever replaced by a
  // newer completed one, and this function only ever returns a value to overwrite
  // with, never an instruction to clear the existing pointer.
  return { clearActiveJob: true, lastReport };
}

/**
 * Given an error thrown while fetching an in-flight job's status, decide the honest
 * local outcome. Never pretend a lost job is still running.
 * @param {{status?: number} | null} error
 * @returns {"LOST" | "TRANSIENT"}
 */
export function interpretJobFetchError(error) {
  if (error && error.status === 404) {
    return "LOST";
  }
  return "TRANSIENT";
}
