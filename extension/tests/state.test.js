import { test } from "node:test";
import assert from "node:assert/strict";
import { States, deriveState, stageLabel, failureLabel, STAGE_LABELS } from "../src/state.js";

const baseFacts = {
  serviceReachable: true,
  versionCompatible: true,
  paired: true,
  pageSupported: true,
};

test("service unreachable outranks everything else", () => {
  assert.equal(deriveState({ ...baseFacts, serviceReachable: false }), States.SERVICE_UNAVAILABLE);
  assert.equal(
    deriveState({ ...baseFacts, serviceReachable: false, paired: false }),
    States.SERVICE_UNAVAILABLE,
  );
});

test("incompatible version shown once service is reachable but before pairing", () => {
  assert.equal(deriveState({ ...baseFacts, versionCompatible: false }), States.INCOMPATIBLE);
});

test("unpaired shown before page-support is evaluated", () => {
  assert.equal(deriveState({ ...baseFacts, paired: false, pageSupported: false }), States.UNPAIRED);
});

test("unsupported page shown when paired but not on a supported page", () => {
  assert.equal(deriveState({ ...baseFacts, pageSupported: false }), States.UNSUPPORTED_PAGE);
});

test("ready when everything checks out and there is no active job", () => {
  assert.equal(deriveState(baseFacts), States.READY);
});

test("launching flag takes precedence over READY", () => {
  assert.equal(deriveState({ ...baseFacts, launching: true }), States.LAUNCHING);
});

test("an active QUEUED or RUNNING job reports RUNNING", () => {
  assert.equal(deriveState({ ...baseFacts, job: { status: "QUEUED" } }), States.RUNNING);
  assert.equal(deriveState({ ...baseFacts, job: { status: "RUNNING" } }), States.RUNNING);
});

test("a COMPLETED job reports COMPLETED", () => {
  assert.equal(deriveState({ ...baseFacts, job: { status: "COMPLETED" } }), States.COMPLETED);
});

test("a FAILED job reports FAILED", () => {
  assert.equal(deriveState({ ...baseFacts, job: { status: "FAILED" } }), States.FAILED);
});

test("stageLabel maps every real backend stage, with no invented percentage", () => {
  for (const stage of Object.keys(STAGE_LABELS)) {
    assert.equal(typeof stageLabel(stage), "string");
    assert.doesNotMatch(stageLabel(stage), /%/);
  }
});

test("stageLabel handles an unknown/future stage safely", () => {
  assert.equal(stageLabel("SOME_FUTURE_STAGE"), "Working…");
  assert.equal(stageLabel(undefined), "Working…");
  assert.equal(stageLabel(null), "Working…");
});

test("failureLabel maps every committed Part 6B-A typed failure code", () => {
  const codes = [
    "UNSUPPORTED_PORTAL",
    "UNSUPPORTED_SOURCE_URL",
    "LOCAL_CONFIGURATION_ERROR",
    "ANALYST_UNAVAILABLE",
    "EVIDENCE_ACQUISITION_FAILED",
    "ANALYSIS_FAILED",
    "CHALLENGE_FAILED",
    "REPORT_GENERATION_FAILED",
    "INTEGRITY_FAILURE",
    "INTERNAL_ERROR",
    "JOB_LOST_AFTER_SERVER_RESTART",
  ];
  for (const code of codes) {
    assert.notEqual(failureLabel(code), "Research failed for an unknown reason.");
  }
});

test("failureLabel handles an unknown code safely", () => {
  assert.equal(failureLabel("SOMETHING_NEW"), "Research failed for an unknown reason.");
});

test("reportView LOADING/READY/NOT_FOUND/INTEGRITY_ERROR/LOAD_FAILED map to their own states", () => {
  assert.equal(deriveState({ ...baseFacts, reportView: { status: "LOADING" } }), States.REPORT_LOADING);
  assert.equal(deriveState({ ...baseFacts, reportView: { status: "READY" } }), States.REPORT_READY);
  assert.equal(deriveState({ ...baseFacts, reportView: { status: "NOT_FOUND" } }), States.REPORT_NOT_FOUND);
  assert.equal(
    deriveState({ ...baseFacts, reportView: { status: "INTEGRITY_ERROR" } }),
    States.REPORT_INTEGRITY_ERROR,
  );
  assert.equal(deriveState({ ...baseFacts, reportView: { status: "LOAD_FAILED" } }), States.REPORT_LOAD_FAILED);
});

test("a reportView outranks page support and job status (sticky report view)", () => {
  assert.equal(
    deriveState({ ...baseFacts, pageSupported: false, reportView: { status: "READY" } }),
    States.REPORT_READY,
  );
  assert.equal(
    deriveState({
      ...baseFacts,
      job: { status: "RUNNING" },
      reportView: { status: "READY" },
    }),
    States.REPORT_READY,
  );
});

test("service unreachable / incompatible / unpaired still outrank an open reportView", () => {
  assert.equal(
    deriveState({ ...baseFacts, serviceReachable: false, reportView: { status: "READY" } }),
    States.SERVICE_UNAVAILABLE,
  );
  assert.equal(
    deriveState({ ...baseFacts, versionCompatible: false, reportView: { status: "READY" } }),
    States.INCOMPATIBLE,
  );
  assert.equal(
    deriveState({ ...baseFacts, paired: false, reportView: { status: "READY" } }),
    States.UNPAIRED,
  );
});

test("no reportView falls through to the ordinary page/job-driven state", () => {
  assert.equal(deriveState({ ...baseFacts, reportView: null }), States.READY);
});
