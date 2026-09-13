import { test } from "node:test";
import assert from "node:assert/strict";
import { isTerminal, storageActionsForJobUpdate, interpretJobFetchError } from "../src/job_flow.js";

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
