import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isTerminal,
  storageActionsForJobUpdate,
  interpretJobFetchError,
  isReportForCurrentPage,
} from "../src/job_flow.js";

test("QUEUED and RUNNING are not terminal", () => {
  assert.equal(isTerminal("QUEUED"), false);
  assert.equal(isTerminal("RUNNING"), false);
});

test("COMPLETED and FAILED are terminal", () => {
  assert.equal(isTerminal("COMPLETED"), true);
  assert.equal(isTerminal("FAILED"), true);
});

test("a running job produces no storage actions", () => {
  const actions = storageActionsForJobUpdate({ status: "RUNNING" });
  assert.equal(actions.clearActiveJob, false);
  assert.equal(actions.lastReport, null);
});

test("a completed job clears the active pointer and stores the report pointer", () => {
  const actions = storageActionsForJobUpdate({
    status: "COMPLETED",
    report_snapshot_id: "report-123",
    source_url: "https://www.screener.in/company/INFY/",
  });
  assert.equal(actions.clearActiveJob, true);
  assert.deepEqual(actions.lastReport, {
    reportId: "report-123",
    sourceUrl: "https://www.screener.in/company/INFY/",
  });
});

test("a failed job clears the active pointer but never invents a report pointer", () => {
  const actions = storageActionsForJobUpdate({ status: "FAILED" });
  assert.equal(actions.clearActiveJob, true);
  assert.equal(actions.lastReport, null);
});

test("missingness/partial coverage is not this module's concern - only status matters", () => {
  // storageActionsForJobUpdate only ever sees `status`; report coverage fields
  // (PARTIAL/UNAVAILABLE/etc.) live inside the report itself, never here.
  const actions = storageActionsForJobUpdate({
    status: "COMPLETED",
    report_snapshot_id: "r1",
    source_url: "u",
  });
  assert.equal(actions.clearActiveJob, true);
  assert.ok(actions.lastReport);
});

test("a 404 while polling an in-flight job is interpreted as LOST, never as still running", () => {
  assert.equal(interpretJobFetchError({ status: 404 }), "LOST");
});

test("any other error is interpreted as TRANSIENT (keep the poll loop alive)", () => {
  assert.equal(interpretJobFetchError({ status: 500 }), "TRANSIENT");
  assert.equal(interpretJobFetchError({ status: 0 }), "TRANSIENT");
  assert.equal(interpretJobFetchError(null), "TRANSIENT");
});

// --- Persisted-report-reopen (Part 6B-C) --------------------------------------------
// A completed report is keyed by report_snapshot_id, not job lifetime: it must remain
// viewable on reopen with no active job at all, and survives a backend restart that
// wiped the in-memory job that produced it (Part 6B-A's persistence boundary).

test("a persisted report pointer is offered for the exact page that produced it, with no job involved at all", () => {
  const lastReport = { reportId: "report-123", sourceUrl: "https://www.screener.in/company/INFY/" };
  // No job object anywhere in this call - proving the decision has zero dependency on
  // any in-flight or historical job state, only on the stored pointer and current page.
  assert.equal(isReportForCurrentPage(lastReport, "https://www.screener.in/company/INFY/"), true);
});

test("a persisted report pointer survives even when there is no active job in storage", () => {
  // Simulates reopening the side panel after a backend restart lost the job: storage
  // has only ever had a lastReport pointer, never an activeJob, and the decision still
  // succeeds purely from the pointer + current page.
  const lastReport = { reportId: "report-123", sourceUrl: "https://www.screener.in/company/INFY/" };
  assert.equal(isReportForCurrentPage(lastReport, "https://www.screener.in/company/INFY/"), true);
});

test("no stored report pointer means nothing is offered", () => {
  assert.equal(isReportForCurrentPage(null, "https://www.screener.in/company/INFY/"), false);
});

// --- Active-page-change (Part 6B-C) --------------------------------------------------
// Navigating to a different company's page must never mislabel an old report as the new
// page's research - but the stored pointer itself is left untouched (no global history
// system, and the old pointer stays available if the user navigates back).

test("a report pointer from a different company's page is never offered on the new page", () => {
  const lastReport = { reportId: "report-123", sourceUrl: "https://www.screener.in/company/INFY/" };
  assert.equal(isReportForCurrentPage(lastReport, "https://www.screener.in/company/TCS/"), false);
});

test("an unsupported/unknown current page never surfaces a stale report pointer", () => {
  const lastReport = { reportId: "report-123", sourceUrl: "https://www.screener.in/company/INFY/" };
  assert.equal(isReportForCurrentPage(lastReport, null), false);
});
